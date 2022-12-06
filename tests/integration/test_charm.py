# Copyright 2021 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from pathlib import Path
from time import sleep

import lightkube
import pytest
import yaml
from charmed_kubeflow_chisme.lightkube.batch import apply_many
from lightkube import codecs
from lightkube.generic_resource import create_namespaced_resource
from lightkube.resources.core_v1 import Namespace, Pod
from pytest_operator.plugin import OpsTest
from tenacity import retry, stop_after_delay, wait_exponential

log = logging.getLogger(__name__)

METADATA = yaml.safe_load(Path("./metadata.yaml").read_text())


@pytest.mark.abort_on_fail
async def test_build_and_deploy(ops_test: OpsTest):
    built_charm_path = await ops_test.build_charm(".")
    log.info(f"Built charm {built_charm_path}")

    image_path = METADATA["resources"]["oci-image"]["upstream-source"]
    resources = {"oci-image": image_path}

    await ops_test.model.deploy(
        entity_url=built_charm_path,
        resources=resources,
    )


async def test_is_active(ops_test: OpsTest):
    await ops_test.model.wait_for_idle(
        apps=["admission-webhook"],
        status="active",
        raise_on_blocked=True,
        raise_on_error=True,
        timeout=300,
    )


def _safe_load_file_to_text(filename: str):
    """Returns the contents of filename if it is an existing file, else it returns filename"""
    try:
        text = Path(filename).read_text()
    except FileNotFoundError:
        text = filename
    return text


@pytest.fixture(scope="session")
def lightkube_client() -> lightkube.Client:
    """Initiates the lightkube client with PodDefault crd resource"""
    client = lightkube.Client()
    create_namespaced_resource(
        group="kubeflow.org", version="v1alpha1", kind="PodDefault", plural="poddefaults"
    )
    return client


@pytest.fixture(scope="function", params=["./tests/integration/poddefault_test_workloads.yaml"])
def kubernetes_workloads(request, lightkube_client: lightkube.Client):
    """Deploys and removes the workloads defined in the workloads file"""
    sleep(30)  # to overcome this bug https://bugs.launchpad.net/juju/+bug/1981833
    try:
        workloads = codecs.load_all_yaml(_safe_load_file_to_text(request.param))
    except Exception as e:
        log.error(f"Unable to load workloads from {request.param}, ended up with {e}")

    apply_many(lightkube_client, workloads, "test")
    log.info("Workloads created")
    yield
    lightkube_client.delete(Namespace, name="test-admission-webhook-user-namespace")
    log.info("Workloads deleted")


def test_namespace_selector_poddefault_service_account_token_mounted(
    lightkube_client, kubernetes_workloads
):
    validate_token_mounted(lightkube_client, "testpod", "test-admission-webhook-user-namespace")


@retry(
    wait=wait_exponential(multiplier=1, min=1, max=10),
    stop=stop_after_delay(30),
    reraise=True,
)
def validate_token_mounted(
    client: lightkube.Client,
    pods_name: str,
    namespace_name: str,
):
    """Checks if the token was mounted successfully by checking the volumes on pod
    Args:
        client: Lightkube client
        pods_name: Name of the pod
        namespace_name: Name of the namespace
    """
    pod = client.get(Pod, name=pods_name, namespace=namespace_name)
    target_vols = [
        volume.name for volume in pod.spec.volumes if volume.name == "volume-kf-pipeline-token"
    ]
    assert len(target_vols) == 1
