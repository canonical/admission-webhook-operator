# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from pathlib import Path
from time import sleep

import lightkube
import pytest
import yaml
from charmed_kubeflow_chisme.lightkube.batch import apply_many
from charmed_kubeflow_chisme.testing import (
    GRAFANA_AGENT_APP,
    assert_logging,
    assert_security_context,
    deploy_and_assert_grafana_agent,
    deploy_and_integrate_service_mesh_charms,
    generate_container_securitycontext_map,
    get_pod_names,
)
from lightkube import ApiError, Client, codecs
from lightkube.generic_resource import create_namespaced_resource
from lightkube.resources.apiextensions_v1 import CustomResourceDefinition
from lightkube.resources.core_v1 import Namespace, Pod, Service
from pytest_operator.plugin import OpsTest
from tenacity import retry, stop_after_delay, wait_exponential

log = logging.getLogger(__name__)

METADATA = yaml.safe_load(Path("./metadata.yaml").read_text())
APP_NAME = METADATA["name"]
CONTAINERS_SECURITY_CONTEXT_MAP = generate_container_securitycontext_map(METADATA)


@pytest.mark.skip_if_deployed
@pytest.mark.abort_on_fail
async def test_build_and_deploy(ops_test: OpsTest):
    built_charm_path = await ops_test.build_charm(".")
    log.info(f"Built charm {built_charm_path}")

    image_path = METADATA["resources"]["oci-image"]["upstream-source"]
    resources = {"oci-image": image_path}

    await ops_test.model.deploy(
        entity_url=built_charm_path,
        resources=resources,
        trust=True,
    )

    # Deploying grafana-agent-k8s and add all relations
    await deploy_and_assert_grafana_agent(
        ops_test.model,
        APP_NAME,
        metrics=False,
        dashboard=False,
        logging=True,
    )

    # Deploy ambient service mesh charms and relate
    await deploy_and_integrate_service_mesh_charms(
        app=APP_NAME, model=ops_test.model, relate_to_ingress_route_endpoint=False
    )


async def test_logging(ops_test: OpsTest):
    """Test logging is defined in relation data bag."""
    app = ops_test.model.applications[GRAFANA_AGENT_APP]
    await assert_logging(app)


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


@pytest.mark.parametrize("container_name", list(CONTAINERS_SECURITY_CONTEXT_MAP.keys()))
async def test_container_security_context(
    ops_test: OpsTest,
    lightkube_client: lightkube.Client,
    container_name: str,
):
    """Test container security context is correctly set.

    Verify that container spec defines the security context with correct
    user ID and group ID.
    """
    pod_name = get_pod_names(ops_test.model.name, APP_NAME)[0]
    assert_security_context(
        lightkube_client,
        pod_name,
        container_name,
        CONTAINERS_SECURITY_CONTEXT_MAP,
        ops_test.model.name,
    )


@pytest.mark.abort_on_fail
async def test_remove_with_resources_present(ops_test: OpsTest):
    """Test remove with all resources deployed.
    Verify that all deployed resources that need to be removed are removed.
    """

    # remove deployed charm and verify that it is removed
    await ops_test.model.remove_application(app_name=APP_NAME, block_until_done=True)
    assert APP_NAME not in ops_test.model.applications

    # verify that all resources that were deployed are removed
    lightkube_client = Client()

    # verify all CRDs in namespace are removed
    crd_list = lightkube_client.list(
        CustomResourceDefinition,
        labels=[("app.juju.is/created-by", "admission-webhook")],
        namespace=ops_test.model.name,
    )
    assert not list(crd_list)

    # verify that Service is removed
    try:
        _ = lightkube_client.get(
            Service,
            name="admission-webhook",
            namespace=ops_test.model.name,
        )
    except ApiError as error:
        if error.status.code != 404:
            # other error than Not Found
            assert False
