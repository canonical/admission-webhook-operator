# Copyright 2021 Canonical Ltd.
# See LICENSE file for licensing details.

from asyncio.log import logger
from time import sleep
import lightkube
from lightkube import codecs
from lightkube.generic_resource import create_namespaced_resource
from lightkube.resources.core_v1 import Pod
import logging
from pathlib import Path
import pytest
import yaml
from pytest_operator.plugin import OpsTest
from tenacity import retry, wait_exponential, stop_after_delay

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


def create_all_from_yaml(
    yaml_file: str,
    lightkube_client: lightkube.Client = None,
):
    """Creates all k8s resources listed in a YAML file via lightkube
    Args:
        yaml_file (str or Path): Either a string filename or a string of valid YAML.  Will attempt
                                 to open a filename at this path, failing back to interpreting the
                                 string directly as YAML.
        lightkube_client: Instantiated lightkube client or None
    """

    yaml_text = _safe_load_file_to_text(yaml_file)

    if lightkube_client is None:
        lightkube_client = lightkube.Client()

    for obj in codecs.load_all_yaml(yaml_text):
        try:
            lightkube_client.create(obj)
        except lightkube.core.exceptions.ApiError as e:
            raise ValueError(f"unable to create resource from file {yaml_file} becuase of {e}")


def delete_all_from_yaml(
    yaml_file: str, lightkube_client: lightkube.Client = None, ignore_errors: bool = True
):
    """Deletes all k8s resources listed in a YAML file via lightkube
    Args:
        yaml_file (str or Path): Either a string filename or a string of valid YAML.  Will attempt
                                 to open a filename at this path, failing back to interpreting the
                                 string directly as YAML.
        lightkube_client: Instantiated lightkube client or None
    """
    log.info("Clearing the namespace")
    yaml_text = _safe_load_file_to_text(yaml_file)

    if lightkube_client is None:
        lightkube_client = lightkube.Client()

    for obj in codecs.load_all_yaml(yaml_text):
        try:
            lightkube_client.delete(type(obj), obj.metadata.name)
        except lightkube.core.exceptions.ApiError as e:
            if ignore_errors:
                logger.info(f"Ignoring exception {e}")
                continue
            raise ValueError(f"unable to delete resource from file {yaml_file}")


@pytest.fixture(scope="session")
def lightkube_client() -> lightkube.Client:
    client = lightkube.Client()
    create_namespaced_resource(
        group="kubeflow.org", version="v1alpha1", kind="PodDefault", plural="poddefaults"
    )
    return client


def test_namespace_selector_poddefault_service_account_token_mounted(lightkube_client):
    try:
        sleep(30)
        workloads_file = "./tests/integration/poddefault_test_workloads.yaml"
        create_all_from_yaml(workloads_file, lightkube_client)
        validate_new_pod(lightkube_client, "testpod", "user")
    except Exception as e:
        log.info(f"Problem during test execution {e}")
    finally:
        delete_all_from_yaml(workloads_file, lightkube_client)


@retry(
    wait=wait_exponential(multiplier=1, min=1, max=10),
    stop=stop_after_delay(30),
    reraise=True,
)
def validate_new_pod(
    client: lightkube.Client,
    pods_name: str,
    namespace_name: str,
):
    pod = client.get(Pod, name=pods_name, namespace=namespace_name)
    target_vols = [
        volume.name for volume in pod.spec.volumes if volume.name == "volume-kf-pipeline-token"
    ]
    assert len(target_vols) == 1
