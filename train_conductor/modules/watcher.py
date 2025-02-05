# Copyright The Train Conductor Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Standard
import json
import threading
import base64
import pickle
import logging
import time
from enum import Enum

# Third Party
from kubernetes import client, watch
from kubernetes.client import Configuration, V1Job
import kubernetes

# First Party
import aconfig

# Local
from train_conductor.utils import error_check as error
from train_conductor.datastore.redis import RedisHelper
from train_conductor.types import TrainingStatus, COMPLETED_STATES


class Watcher:
    def __init__(self, config: aconfig.Config):
        """Function to initialize the Watcher."""
        self.config = config
        logging.info("Attempting connection to Redis")

        self.db_client = RedisHelper(self.config)

        self.tuning_image = self.config.trainer_config.tuning_image
        self.target_namespace = self.config.trainer_config.target_namespace
        error.type_check("<TCD18042451E>", str, tuning_image=self.tuning_image)

        logging.info("Attempting connection to K8S API server")
        try:
            kubernetes.config.load_incluster_config()
        except kubernetes.config.ConfigException:
            # Fallback for local development
            kubernetes.config.load_kube_config()

        # *********************
        # TEMP HACK to deal with self signed certs
        # *********************
        Configuration._default.verify_ssl = False
        #

        self.batch_v1_api = client.BatchV1Api()
        self.core_v1_api = client.CoreV1Api()
        resource_version = self.full_reconcile()

        # Start DB listener thread
        self._db_listener_thread = threading.Thread(
            target=self.db_client.start_listener(self.db_update_event_handler)
        )
        logging.info("Starting DB listener")
        self._db_listener_thread.start()

        # Start Full reconcile thread
        self._reconcile_interval = config.get("trainer_config.reconcile_interval") or 30
        self._reconcile_thread = threading.Thread(
            target=self.start_full_reconcile_task(self._reconcile_interval)
        )
        self._reconcile_thread.start()

        # Start Watching K8s
        while True:
            self.monitor_jobs(resource_version)

    def start_full_reconcile_task(self, interval):
        while True:
            time.sleep(interval)
            self.full_reconcile()

    def reconcile_state(self, job_id: str, db_entry: dict, k8s_entry: V1Job):
        """
        Centralized logic for reconciling database and k8s state
        """
        if not db_entry:
            db_entry = self.db_client.read_record(job_id)

        if not db_entry:
            logging.info(
                "Job exists in kubernetes but not database. Deleting {}".format(job_id)
            )
            self.delete_job(
                job_id=job_id,
                job_name=self.generate_k8s_job_name(job_id),
                namespace=self.target_namespace,
            )

        db_state = db_entry.get("status")
        if db_state:
            db_state = getattr(TrainingStatus, db_state)
        else:
            db_state = TrainingStatus.PLACEHOLDER_UNSET

        if db_state in COMPLETED_STATES and db_entry.get("deleted") and not k8s_entry:
            return

        if not k8s_entry:
            try:
                k8s_entry = self.batch_v1_api.read_namespaced_job(
                    namespace=self.target_namespace,
                    name=self.generate_k8s_job_name(job_id),
                )
            except client.exceptions.ApiException as e:
                logging.info("Job {} not found in k8s".format(job_id))
                if db_state in COMPLETED_STATES:
                    logging.info(
                        "Job {} already completed = {}".format(job_id, db_state.name)
                    )
                    if not db_entry.get("deleted"):
                        self.db_client.write_field(job_id, "deleted", "1")
                    return
                else:
                    logging.info(
                        "Current job state for job {}: {}".format(job_id, db_state.name)
                    )
                    logging.info("Launching job {} in Kubernetes".format(job_id))
                    env_vars = ""
                    params = db_entry.get("parameters")
                    if params:
                        try:
                            env_vars = json.loads(params)
                        except:
                            logging.error(
                                "Could not load env vars for job {}".format(job_id)
                            )
                    self.create_job(
                        job_id=job_id,
                        image=self.tuning_image,
                        image_pull_secrets=self.config.trainer_config.image_pull_secrets,
                        gpus=env_vars.get("num_gpus")
                        or self.config.trainer_config.default_resources.gpu,
                        env_vars=env_vars,
                    )
                    return

        # Job exists in DB and K8s. See if we need to update DB status.
        k8s_state = k8s_entry.status
        actual_state = self.k8s_job_status_to_enum(k8s_state)
        if db_state != actual_state:
            logging.info(
                "Actual state {} does not match state in database {} for job {}".format(
                    actual_state.name, db_state.name, job_id
                )
            )
            # If DB indicates canceled, we need to cancel and delete
            if db_state == TrainingStatus.CANCELED:
                logging.info("Canceling job " + job_id)
                self.delete_job(
                    job_id=job_id,
                    job_name=k8s_entry.metadata.name,
                    namespace=self.target_namespace,
                )
                return

            # Otherwise, update DB to reflect actual state in k8s
            self.db_client.write_field(job_id, "status", str(actual_state.name))
        if actual_state in COMPLETED_STATES and not db_entry.get("deleted"):
            logging.info("Job has compelted, deleting from k8s " + job_id)
            self.capture_failed_state(job_id=job_id, job=k8s_entry)
            self.delete_job(job_id, k8s_entry.metadata.name, self.target_namespace)

    def capture_failed_state(self, job_id: str, job: V1Job):
        logs = self.get_job_logs(job)
        self.db_client.write_field(job_id, "errors", logs)

    def get_job_logs(self, job: V1Job):
        logs = ""
        selector = "job-name=" + job.metadata.name
        pods = self.core_v1_api.list_namespaced_pod(
            namespace=self.target_namespace, label_selector=selector
        )
        for pod in pods.items:
            pod_name = pod.metadata.name
            pod_log = self.core_v1_api.read_namespaced_pod_log(
                pod_name, self.target_namespace
            )
            logs += pod_log
        print(logs)
        return logs

    def monitor_jobs(self, resource_version):
        """
        Function for keeping watch on events in Kubernetes, and reconciling each change
        """
        try:
            w = watch.Watch()
            for event in w.stream(
                self.batch_v1_api.list_namespaced_job,
                namespace=self.target_namespace,
                timeout_seconds=0,
                resource_version=resource_version,
            ):
                # Save new resource version to use on next iteration
                resource_version = event["object"].metadata.resource_version

                job_id = event["object"].metadata.labels.get("job_id")

                db_record = self.db_client.read_record(job_id)
                self.reconcile_state(job_id, db_record, event["object"])

        except client.exceptions.ApiException as e:
            if (
                e.status != 410
            ):  # Not a "Gone" exception, indicating resourceVersion too old
                raise
            # Our resource version was too old, so run a full reconcile
            logging.error("Encountered exception, starting full reconcile, " + e)
            resource_version = self.full_reconcile()
        except Exception as e:
            # Just log other errors
            logging.error("Exception in watch")
            logging.error(e)

    def scan_db_entries(self):
        """
        Utility for fetching multiple keys and values from the database at once
        """
        cursor = "0"
        while cursor != 0:
            cursor, keys = self.db_client.iterate_entries(cursor=cursor)
            values = self.db_client.read_many_entries(keys)
            yield from values.items()

    def full_reconcile(self):
        logging.info("Beginning full reconcile")
        job_list = self.batch_v1_api.list_namespaced_job(
            namespace=self.target_namespace
        )
        resource_version = job_list.metadata.resource_version
        current_jobs = job_list.items
        job_dict = {}
        for job in current_jobs:
            job_dict[job.metadata.labels.get("job_id")] = job

        for job_id, db_entry in self.scan_db_entries():
            logging.info(
                "Evaluating job " + job_id + " with status " + db_entry.get("status")
            )
            self.reconcile_state(job_id, db_entry, job_dict.pop(job_id, None))

        # Jobs in K8s that are not not in DB
        for job_id, job in job_dict.items():
            job_id = job.metadata.labels.get("job_id")
            db_entry = self.db_client.read_record(job_id)
            self.reconcile_state(job_id, db_entry, job)

        logging.info("Completed full reconcile")
        return resource_version

    def delete_job(self, job_id, job_name, namespace):
        try:
            # Propogation policy makes sure that pods belonging to the job get deleted as well, asynchronously
            self.batch_v1_api.delete_namespaced_job(
                name=job_name, namespace=namespace, propagation_policy="Background"
            )
            logging.info("Deleted job for id " + job_id)
            self.db_client.write_field(job_id, "deleted", "1")
        except Exception as e:
            logging.error(
                "Unable to delete job will try again later "
                + job_id
                + " Error: "
                + str(e)
            )

    def k8s_job_status_to_enum(self, k8s_job_status):
        job_status = TrainingStatus.QUEUED
        if k8s_job_status.start_time:
            if k8s_job_status.succeeded:
                job_status = TrainingStatus.COMPLETED
            elif k8s_job_status.failed:
                job_status = TrainingStatus.FAILED
            else:
                job_status = TrainingStatus.RUNNING
        return job_status

    def db_update_event_handler(self, msg):
        job_id = msg.get("data")
        logging.info("Recieved DB update for job {}".format(job_id))
        record = self.db_client.read_record(job_id)
        self.reconcile_state(job_id, record, None)

    def generate_k8s_job_name(self, job_id: str):
        return "train-conductor-tuning-job" + "." + job_id

    def create_job(
        self,
        job_id: str,
        image: str,
        app: str = "train-conductor-stack",
        container_name: str = "train-conductor-training",
        image_pull_secrets: str = None,
        gpus: int = 0,
        backoff_limit=0,
        env_vars: dict = None,
    ):
        job_name = self.generate_k8s_job_name(job_id)

        job_timeout = self.config.trainer_config.job_time_limit or 0

        # Define mounted volumes
        volumes = []
        volume_mounts = []
        if self.config.training_volumes:
            for volume in self.config.training_volumes:
                error.type_check("<TCD37116630E>", str, volume_name=volume.name)
                error.type_check("<TCD24985933E>", str, pvc=volume.pvc_name)
                error.type_check("<TCD48967099E>", str, mount_path=volume.mount_path)
                volumes.append(
                    client.V1Volume(
                        name=volume.name,
                        persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                            claim_name=volume.pvc_name
                        ),
                    )
                )
                volume_mounts.append(
                    client.V1VolumeMount(name=volume.name, mount_path=volume.mount_path)
                )

        # Define the container to run
        env_var_string = self._obj_to_txt(env_vars)
        container = client.V1Container(
            name=container_name,
            volume_mounts=volume_mounts,
            image=image,
            env=[
                client.V1EnvVar(
                    name="SFT_TRAINER_CONFIG_JSON_ENV_VAR", value=env_var_string
                ),
                client.V1EnvVar(name="ALLOW_DOWNLOADS", value="true"),
            ],
            resources=client.V1ResourceRequirements(limits={"nvidia.com/gpu": gpus}),
            command=["python", "/app/launch_training.py"],
        )

        # Define the Job template
        template = client.V1PodTemplateSpec(
            metadata=client.V1ObjectMeta(labels={"app": app, "job_id": job_id}),
            spec=client.V1PodSpec(
                restart_policy="Never",
                containers=[container],
                volumes=volumes,
                image_pull_secrets=[{"name": image_pull_secrets}],
            ),
        )

        # Define the Job spec
        job_spec = client.V1JobSpec(
            template=template,
            backoff_limit=backoff_limit,  # Number of retries before considering the Job as failed
            active_deadline_seconds=job_timeout,
        )

        # Define the Job
        job_body = client.V1Job(
            api_version="batch/v1",
            kind="Job",
            metadata=client.V1ObjectMeta(name=job_name),
            spec=job_spec,
        )

        try:
            job = self.batch_v1_api.create_namespaced_job(
                body=job_body, namespace=self.target_namespace
            )
        except Exception as e:
            logging.error(
                "Exception encountered attempting to create job. Will try again later. "
                + job_id
            )
            logging.error(e)
            return

        logging.info("Created job for id {}".format(job_id))
        self.db_client.write_field(
            job_id,
            "submission_timestamp",
            job.metadata.creation_timestamp.strftime("%m/%d/%Y %H:%M:%S"),
        )
        self.db_client.write_field(job_id, "job_name", job_name)
        self.db_client.write_field(job_id, "namespace", job.metadata.namespace)
        self.db_client.write_field(job_id, "status", TrainingStatus.PENDING.name)

    @staticmethod
    def _obj_to_txt(obj):
        message_bytes = pickle.dumps(obj)
        base64_bytes = base64.b64encode(message_bytes)
        txt = base64_bytes.decode("ascii")
        return txt


if __name__ == "__main__":
    # For testing purposes
    config = aconfig.Config.from_yaml("runtime_config.yml")
    Watcher(config)
