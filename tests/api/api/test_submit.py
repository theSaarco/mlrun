import base64
import unittest.mock
from http import HTTPStatus

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import mlrun
import mlrun.api.utils.auth.verifier
import mlrun.api.utils.clients.iguazio
from mlrun.api.schemas import AuthInfo
from mlrun.api.utils.singletons.k8s import get_k8s
from mlrun.config import config as mlconf


def test_submit_job_failure_function_not_found(db: Session, client: TestClient) -> None:
    function_reference = (
        "cat-and-dog-servers/aggregate@b145b6d958a7b4d84f12821a06459e31ea422308"
    )
    body = {
        "task": {
            "metadata": {"name": "task-name", "project": "project-name"},
            "spec": {"function": function_reference},
        },
    }
    resp = client.post("/api/submit_job", json=body)
    assert resp.status_code == HTTPStatus.NOT_FOUND.value
    assert f"Function not found {function_reference}" in resp.json()["detail"]["reason"]


username = "voldemort"
access_key = "12345"


@pytest.fixture()
def pod_create_mock():
    create_pod_orig_function = get_k8s().create_pod
    _get_project_secrets_raw_data_orig_function = (
        get_k8s()._get_project_secrets_raw_data
    )
    get_k8s().create_pod = unittest.mock.Mock(return_value=("pod-name", "namespace"))
    get_k8s()._get_project_secrets_raw_data = unittest.mock.Mock(return_value={})

    update_run_state_orig_function = (
        mlrun.runtimes.kubejob.KubejobRuntime._update_run_state
    )
    mlrun.runtimes.kubejob.KubejobRuntime._update_run_state = unittest.mock.Mock()

    mock_run_object = mlrun.RunObject()
    mock_run_object.metadata.uid = "1234567890"
    mock_run_object.metadata.project = "project-name"

    wrap_run_result_orig_function = mlrun.runtimes.base.BaseRuntime._wrap_run_result
    mlrun.runtimes.base.BaseRuntime._wrap_run_result = unittest.mock.Mock(
        return_value=mock_run_object
    )

    auth_info_mock = AuthInfo(
        username=username, session="some-session", data_session=access_key
    )

    authenticate_request_orig_function = (
        mlrun.api.utils.auth.verifier.AuthVerifier().authenticate_request
    )
    mlrun.api.utils.auth.verifier.AuthVerifier().authenticate_request = unittest.mock.Mock(
        return_value=auth_info_mock
    )

    yield get_k8s().create_pod

    # Have to revert the mocks, otherwise other tests are failing
    get_k8s().create_pod = create_pod_orig_function
    get_k8s()._get_project_secrets_raw_data = (
        _get_project_secrets_raw_data_orig_function
    )
    mlrun.runtimes.kubejob.KubejobRuntime._update_run_state = (
        update_run_state_orig_function
    )
    mlrun.runtimes.base.BaseRuntime._wrap_run_result = wrap_run_result_orig_function
    mlrun.api.utils.auth.verifier.AuthVerifier().authenticate_request = (
        authenticate_request_orig_function
    )


def _create_submit_job_body(function, project):
    return {
        "task": {
            "spec": {"output_path": "/some/fictive/path/to/make/everybody/happy"},
            "metadata": {"name": "task1", "project": project},
        },
        "function": function.to_dict(),
    }


def test_submit_job_auto_mount(
    db: Session, client: TestClient, pod_create_mock
) -> None:
    mlconf.storage.auto_mount_type = "v3io_credentials"
    api_url = "https://api/url"
    # Set different auto-mount-params, to ensure the auth info is overridden
    mlconf.storage.auto_mount_params = (
        f"api={api_url},user=invalid-user,access_key=invalid-access-key"
    )

    project = "my-proj1"
    function_name = "test-function"
    function_tag = "latest"
    function = mlrun.new_function(
        name=function_name,
        project=project,
        tag=function_tag,
        kind="job",
        image="mlrun/mlrun",
    )
    submit_job_body = _create_submit_job_body(function, project)

    resp = client.post("/api/submit_job", json=submit_job_body)
    assert resp
    expected_env_vars = {
        "V3IO_API": api_url,
        "V3IO_USERNAME": username,
        "V3IO_ACCESS_KEY": access_key,
    }
    _assert_pod_env_vars(pod_create_mock, expected_env_vars)


def test_submit_job_ensure_function_has_auth_set(
    db: Session, client: TestClient, pod_create_mock
) -> None:
    mlrun.mlconf.httpdb.authentication.mode = "iguazio"
    project = "my-proj1"
    function = mlrun.new_function(
        name="test-function",
        project=project,
        tag="latest",
        kind="job",
        image="mlrun/mlrun",
    )
    access_key = "some-access-key"
    function.metadata.credentials.access_key = access_key
    submit_job_body = _create_submit_job_body(function, project)
    resp = client.post("/api/submit_job", json=submit_job_body)
    assert resp

    expected_env_vars = {
        "MLRUN_AUTH_SESSION": access_key,
    }
    _assert_pod_env_vars(pod_create_mock, expected_env_vars)


def _mock_service_account_secrets(allowed_service_accounts, default_service_account):
    secrets = {}
    if default_service_account:
        secrets[
            mlrun.api.crud.secrets.Secrets().generate_service_account_secret_key(
                "default"
            )
        ] = base64.b64encode(default_service_account.encode()).decode("utf-8")
    if allowed_service_accounts:
        allowed_str = ",".join(allowed_service_accounts)
        secrets[
            mlrun.api.crud.secrets.Secrets().generate_service_account_secret_key(
                "allowed"
            )
        ] = base64.b64encode(allowed_str.encode()).decode("utf-8")

    get_k8s()._get_project_secrets_raw_data = unittest.mock.Mock(return_value=secrets)


def test_submit_job_service_accounts(db: Session, client: TestClient, pod_create_mock):
    _mock_service_account_secrets(["sa1", "sa2"], "sa1")

    project = "my-proj1"
    function_name = "test-function"
    function_tag = "latest"
    function = mlrun.new_function(
        name=function_name,
        project=project,
        tag=function_tag,
        kind="job",
        image="mlrun/mlrun",
    )
    submit_job_body = _create_submit_job_body(function, project)

    resp = client.post("/api/submit_job", json=submit_job_body)
    assert resp
    _assert_pod_service_account(pod_create_mock, "sa1")

    pod_create_mock.reset_mock()
    function.spec.service_account = "sa2"
    submit_job_body = _create_submit_job_body(function, project)

    resp = client.post("/api/submit_job", json=submit_job_body)
    assert resp
    _assert_pod_service_account(pod_create_mock, "sa2")

    # Invalid service-account
    pod_create_mock.reset_mock()
    function.spec.service_account = "sa3"
    submit_job_body = _create_submit_job_body(function, project)
    resp = client.post("/api/submit_job", json=submit_job_body)
    assert resp.status_code == HTTPStatus.BAD_REQUEST.value

    # Validate that without setting the secrets, any SA is allowed
    _mock_service_account_secrets(None, None)
    pod_create_mock.reset_mock()
    resp = client.post("/api/submit_job", json=submit_job_body)
    assert resp
    _assert_pod_service_account(pod_create_mock, "sa3")


def _assert_pod_env_vars(pod_create_mock, expected_env_vars):
    pod_create_mock.assert_called_once()
    args, _ = pod_create_mock.call_args
    pod_env = args[0].spec.containers[0].env
    pod_env_dict = {
        mlrun.runtimes.utils.get_item_name(
            env_item
        ): mlrun.runtimes.utils.get_item_name(env_item, "value")
        for env_item in pod_env
    }
    for key, value in expected_env_vars.items():
        assert pod_env_dict[key] == value


def _assert_pod_service_account(pod_create_mock, expected_service_account):
    pod_create_mock.assert_called_once()
    args, _ = pod_create_mock.call_args
    pod_spec = args[0].spec
    assert pod_spec.service_account == expected_service_account
