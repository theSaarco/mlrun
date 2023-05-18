# Copyright 2018 Iguazio
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import time
import warnings

from kubernetes import client
from kubernetes.client.rest import ApiException

import mlrun.common.schemas
import mlrun.errors
from mlrun.runtimes.base import BaseRuntimeHandler

from ..db import RunDBError
from ..errors import err_to_str
from ..kfpops import build_op
from ..model import RunObject
from ..utils import get_in, logger
from .base import RunError, RuntimeClassMode
from .pod import KubeResource, kube_resource_spec_to_pod_spec
from .utils import get_k8s


class KubejobRuntime(KubeResource):
    kind = "job"
    _is_nested = True

    _is_remote = True

    def is_deployed(self):
        """check if the function is deployed (has a valid container)"""
        if self.spec.image:
            return True

        db = self._get_db()
        try:
            # getting builder status enriches the runtime when it needs to be fetched from the API,
            # otherwise it's a no-op
            db.get_builder_status(self, logs=False)
        except Exception:
            pass

        if self.spec.image:
            return True
        if self.status.state and self.status.state == "ready":
            return True
        return False

    def with_source_archive(
        self, source, workdir=None, handler=None, pull_at_runtime=True, target_dir=None
    ):
        """load the code from git/tar/zip archive at runtime or build

        :param source:          valid path to git, zip, or tar file, e.g.
                                git://github.com/mlrun/something.git
                                http://some/url/file.zip
        :param handler:         default function handler
        :param workdir:         working dir relative to the archive root (e.g. './subdir') or absolute to the image root
        :param pull_at_runtime: load the archive into the container at job runtime vs on build/deploy
        :param target_dir:      target dir on runtime pod or repo clone / archive extraction
        """
        if source.endswith(".zip") and not pull_at_runtime:
            logger.warn(
                "zip files are not natively extracted by docker, use tar.gz for faster loading during build"
            )

        self.spec.build.source = source
        if handler:
            self.spec.default_handler = handler
        if workdir:
            self.spec.workdir = workdir
        if target_dir:
            self.spec.clone_target_dir = target_dir

        self.spec.build.load_source_on_run = pull_at_runtime
        if (
            self.spec.build.base_image
            and not self.spec.build.commands
            and pull_at_runtime
            and not self.spec.image
        ):
            # if we load source from repo and don't need a full build use the base_image as the image
            self.spec.image = self.spec.build.base_image
        elif not pull_at_runtime:
            # clear the image so build will not be skipped
            self.spec.build.base_image = self.spec.build.base_image or self.spec.image
            self.spec.image = ""

    def build_config(
        self,
        image="",
        base_image=None,
        commands: list = None,
        secret=None,
        source=None,
        extra=None,
        load_source_on_run=None,
        with_mlrun=None,
        auto_build=None,
        requirements=None,
        overwrite=False,
        verify_base_image=False,
        prepare_image_for_deploy=True,
    ):
        """specify builder configuration for the deploy operation

        :param image:      target image name/path
        :param base_image: base image name/path
        :param commands:   list of docker build (RUN) commands e.g. ['pip install pandas']
        :param secret:     k8s secret for accessing the docker registry
        :param source:     source git/tar archive to load code from in to the context/workdir
                           e.g. git://github.com/mlrun/something.git#development
        :param extra:      extra Dockerfile lines
        :param load_source_on_run: load the archive code into the container at runtime vs at build time
        :param with_mlrun: add the current mlrun package to the container build
        :param auto_build: when set to True and the function require build it will be built on the first
                           function run, use only if you dont plan on changing the build config between runs
        :param requirements: requirements.txt file to install or list of packages to install
        :param overwrite:  overwrite existing build configuration

           * False: the new params are merged with the existing (currently merge is applied to requirements and
             commands)
           * True: the existing params are replaced by the new ones
        :param verify_base_image:           verify that the base image is configured
                                            (deprecated, use prepare_image_for_deploy)
        :param prepare_image_for_deploy:    prepare the image/base_image spec for deployment
        """

        self.spec.build.build_config(
            image,
            base_image,
            commands,
            secret,
            source,
            extra,
            load_source_on_run,
            with_mlrun,
            auto_build,
            requirements,
            overwrite,
        )

        if verify_base_image or prepare_image_for_deploy:
            if verify_base_image:
                # TODO: remove verify_base_image in 1.6.0
                warnings.warn(
                    "verify_base_image is deprecated in 1.4.0 and will be removed in 1.6.0, "
                    "use prepare_image_for_deploy",
                    category=FutureWarning,
                )
            self.prepare_image_for_deploy()

    def deploy(
        self,
        watch=True,
        with_mlrun=None,
        skip_deployed=False,
        is_kfp=False,
        mlrun_version_specifier=None,
        builder_env: dict = None,
        show_on_failure: bool = False,
    ) -> bool:
        """deploy function, build container with dependencies

        :param watch:                   wait for the deploy to complete (and print build logs)
        :param with_mlrun:              add the current mlrun package to the container build
        :param skip_deployed:           skip the build if we already have an image for the function
        :param is_kfp:                  deploy as part of a kfp pipeline
        :param mlrun_version_specifier: which mlrun package version to include (if not current)
        :param builder_env:             Kaniko builder pod env vars dict (for config/credentials)
                                        e.g. builder_env={"GIT_TOKEN": token}
        :param show_on_failure:         show logs only in case of build failure

        :return True if the function is ready (deployed)
        """

        build = self.spec.build

        if with_mlrun is None:
            if build.with_mlrun is not None:
                with_mlrun = build.with_mlrun
            else:
                with_mlrun = build.base_image and not (
                    build.base_image.startswith("mlrun/")
                    or "/mlrun/" in build.base_image
                )

        if (
            not build.source
            and not build.commands
            and not build.requirements
            and not build.extra
            and with_mlrun
        ):
            logger.info(
                "running build to add mlrun package, set "
                "with_mlrun=False to skip if its already in the image"
            )
        self.status.state = ""
        if build.base_image:
            # clear the image so build will not be skipped
            self.spec.image = ""

        # When we're in pipelines context we must watch otherwise the pipelines pod will exit before the operation
        # is actually done. (when a pipelines pod exits, the pipeline step marked as done)
        if is_kfp:
            watch = True

        ready = False
        if self._is_remote_api():
            db = self._get_db()
            data = db.remote_builder(
                self,
                with_mlrun,
                mlrun_version_specifier,
                skip_deployed,
                builder_env=builder_env,
            )
            self.status = data["data"].get("status", None)
            self.spec.image = get_in(data, "data.spec.image")
            self.spec.build.base_image = self.spec.build.base_image or get_in(
                data, "data.spec.build.base_image"
            )
            # get the clone target dir in case it was enriched due to loading source
            self.spec.clone_target_dir = get_in(data, "data.spec.clone_target_dir")
            ready = data.get("ready", False)
            if not ready:
                logger.info(
                    f"Started building image: {data.get('data', {}).get('spec', {}).get('build', {}).get('image')}"
                )
            if watch and not ready:
                state = self._build_watch(watch, show_on_failure=show_on_failure)
                ready = state == "ready"
                self.status.state = state

        if watch and not ready:
            raise mlrun.errors.MLRunRuntimeError("Deploy failed")
        return ready

    def _build_watch(self, watch=True, logs=True, show_on_failure=False):
        db = self._get_db()
        offset = 0
        try:
            text, _ = db.get_builder_status(self, 0, logs=logs)
        except RunDBError:
            raise ValueError("function or build process not found")

        def print_log(text):
            if text and (not show_on_failure or self.status.state == "error"):
                print(text, end="")

        print_log(text)
        offset += len(text)
        if watch:
            while self.status.state in ["pending", "running"]:
                time.sleep(2)
                if show_on_failure:
                    text = ""
                    db.get_builder_status(self, 0, logs=False)
                    if self.status.state == "error":
                        # re-read the full log on failure
                        text, _ = db.get_builder_status(self, offset, logs=logs)
                else:
                    text, _ = db.get_builder_status(self, offset, logs=logs)
                print_log(text)
                offset += len(text)

        print()
        return self.status.state

    def deploy_step(
        self,
        image=None,
        base_image=None,
        commands: list = None,
        secret_name="",
        with_mlrun=True,
        skip_deployed=False,
    ):
        function_name = self.metadata.name or "function"
        name = f"deploy_{function_name}"
        # mark that the function/image is built as part of the pipeline so other places
        # which use the function will grab the updated image/status
        self._build_in_pipeline = True
        return build_op(
            name,
            self,
            image=image,
            base_image=base_image,
            commands=commands,
            secret_name=secret_name,
            with_mlrun=with_mlrun,
            skip_deployed=skip_deployed,
        )

    def _run(self, runobj: RunObject, execution):
        command, args, extra_env = self._get_cmd_args(runobj)

        if runobj.metadata.iteration:
            self.store_run(runobj)
        new_meta = self._get_meta(runobj)

        self._add_secrets_to_spec_before_running(runobj)
        workdir = self._resolve_workdir()

        pod_spec = func_to_pod(
            self.full_image_path(
                client_version=runobj.metadata.labels.get("mlrun/client_version"),
                client_python_version=runobj.metadata.labels.get(
                    "mlrun/client_python_version"
                ),
            ),
            self,
            extra_env,
            command,
            args,
            workdir,
        )
        pod = client.V1Pod(metadata=new_meta, spec=pod_spec)
        try:
            pod_name, namespace = get_k8s().create_pod(pod)
        except ApiException as exc:
            raise RunError(err_to_str(exc))

        txt = f"Job is running in the background, pod: {pod_name}"
        logger.info(txt)
        runobj.status.status_text = txt

        return None

    def _resolve_workdir(self):
        """
        The workdir is relative to the source root, if the source is not loaded on run then the workdir
        is relative to the clone target dir (where the source was copied to).
        Otherwise, if the source is loaded on run, the workdir is resolved on the run as well.
        If the workdir is absolute, keep it as is.
        """
        workdir = self.spec.workdir
        if self.spec.build.source and self.spec.build.load_source_on_run:
            # workdir will be set AFTER the clone which is done in the pre-run of local runtime
            return None

        if workdir and os.path.isabs(workdir):
            return workdir

        if self.spec.clone_target_dir:
            workdir = workdir or ""
            if workdir.startswith("./"):
                # TODO: use 'removeprefix' when we drop python 3.7 support
                # workdir.removeprefix("./")
                workdir = workdir[2:]
            return os.path.join(self.spec.clone_target_dir, workdir)

        return workdir


def func_to_pod(image, runtime, extra_env, command, args, workdir):
    container = client.V1Container(
        name="base",
        image=image,
        env=extra_env + runtime.spec.env,
        command=[command],
        args=args,
        working_dir=workdir,
        image_pull_policy=runtime.spec.image_pull_policy,
        volume_mounts=runtime.spec.volume_mounts,
        resources=runtime.spec.resources,
    )

    pod_spec = kube_resource_spec_to_pod_spec(runtime.spec, container)

    if runtime.spec.image_pull_secret:
        pod_spec.image_pull_secrets = [
            client.V1LocalObjectReference(name=runtime.spec.image_pull_secret)
        ]

    return pod_spec


class KubeRuntimeHandler(BaseRuntimeHandler):
    kind = "job"
    class_modes = {RuntimeClassMode.run: "job", RuntimeClassMode.build: "build"}

    @staticmethod
    def _expect_pods_without_uid() -> bool:
        """
        builder pods are handled as part of this runtime handler - they are not coupled to run object, therefore they
        don't have the uid in their labels
        """
        return True

    @staticmethod
    def _are_resources_coupled_to_run_object() -> bool:
        return True

    @staticmethod
    def _get_object_label_selector(object_id: str) -> str:
        return f"mlrun/uid={object_id}"
