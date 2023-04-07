"""Unit tests for Admission Webhook Charm."""

from unittest.mock import MagicMock, patch

import pytest
from ops.model import ActiveStatus, MaintenanceStatus, WaitingStatus
from ops.testing import Harness
from ops.pebble import CheckStatus

from charm import AdmissionWebhookCharm


@pytest.fixture(scope="function")
def harness() -> Harness:
    """Create and return Harness for testing."""
    harness = Harness(AdmissionWebhookCharm)

    # setup container networking simulation
    harness.set_can_connect("admission-webhook", True)

    return harness


class TestCharm:
    """Test class for Admission Webhook."""

    @patch("charm.KubernetesServicePatch", lambda x, y, service_name: None)
    @patch("charm.AdmissionWebhookCharm.k8s_resource_handler")
    @patch("charm.AdmissionWebhookCharm.crd_resource_handler")
    def test_not_leader(
        self,
        k8s_resource_handler: MagicMock,
        crd_resource_handler: MagicMock,
        harness: Harness,
    ):
        """Test not a leader scenario."""
        harness.begin_with_initial_hooks()
        harness.container_pebble_ready("admission-webhook")
        assert harness.charm.model.unit.status == WaitingStatus("Waiting for leadership")

    @patch("charm.KubernetesServicePatch", lambda x, y, service_name: None)
    @patch("charm.AdmissionWebhookCharm.k8s_resource_handler")
    @patch("charm.AdmissionWebhookCharm.crd_resource_handler")
    def test_no_relation(
        self,
        k8s_resource_handler: MagicMock,
        crd_resource_handler: MagicMock,
        harness: Harness,
    ):
        """Test no relation scenario."""
        harness.set_leader(True)
        harness.add_oci_resource(
            "oci-image",
            {
                "registrypath": "ci-test",
                "username": "",
                "password": "",
            },
        )

        harness.begin_with_initial_hooks()
        harness.container_pebble_ready("admission-webhook")
        assert harness.charm.model.unit.status == ActiveStatus("")

    @patch("charm.KubernetesServicePatch", lambda x, y, service_name: None)
    @patch("charm.AdmissionWebhookCharm.k8s_resource_handler")
    @patch("charm.AdmissionWebhookCharm.crd_resource_handler")
    def test_pebble_layer(
        self,
        k8s_resource_handler: MagicMock,
        crd_resource_handler: MagicMock,
        harness: Harness,
    ):
        """Test creation of Pebble layer. Only testing specific items."""
        harness.set_leader(True)
        harness.set_model_name("test_kubeflow")
        harness.begin_with_initial_hooks()
        harness.container_pebble_ready("admission-webhook")
        pebble_plan = harness.get_container_pebble_plan("admission-webhook")
        assert pebble_plan
        assert pebble_plan._services
        pebble_plan_info = pebble_plan.to_dict()
        assert pebble_plan_info["services"]["admission-webhook"]["command"] == "/webhook"

    @patch("charm.KubernetesServicePatch", lambda x, y, service_name: None)
    @patch("charm.AdmissionWebhookCharm.k8s_resource_handler")
    @patch("charm.AdmissionWebhookCharm.crd_resource_handler")
    def test_apply_k8s_resources_success(
        self,
        k8s_resource_handler: MagicMock,
        crd_resource_handler: MagicMock,
        harness: Harness,
    ):
        """Test if K8S resource handler is executed as expected."""
        harness.begin()
        harness.charm._apply_k8s_resources()
        crd_resource_handler.apply.assert_called()
        k8s_resource_handler.apply.assert_called()
        assert isinstance(harness.charm.model.unit.status, MaintenanceStatus)

    @patch("charm.KubernetesServicePatch", lambda x, y, service_name: None)
    @patch("charm.AdmissionWebhookCharm._get_check_status")
    @pytest.mark.parametrize(
        "health_check_status, charm_status",
        [
            (CheckStatus.UP, ActiveStatus("")),
            (CheckStatus.DOWN, MaintenanceStatus("Workload failed health check")),
        ],
    )
    def test_update_status(
        self,
        _get_check_status: MagicMock,
        health_check_status,
        charm_status,
        harness: Harness,
    ):
        """
        Test update status handler.
        Check on the correct charm status when health check status is UP/DOWN.
        """
        harness.set_leader(True)
        harness.begin_with_initial_hooks()
        harness.container_pebble_ready("admission-webhook")

        _get_check_status.return_value = health_check_status

        # test successful update status
        harness.charm.on.update_status.emit()
        assert harness.charm.model.unit.status == charm_status

    @patch("charm.KubernetesServicePatch", lambda x, y, service_name: None)
    @pytest.mark.parametrize(
        "cert_data_dict, should_certs_refresh",
        [
            # Cases where we should generate a new cert
            # No cert data, we should refresh certs
            ({}, True),
            # We are missing one of the required cert data fields, we should refresh certs
            ({"ca": "x", "key": "x"}, True),
            ({"cert": "x", "key": "x"}, True),
            ({"cert": "x", "ca": "x"}, True),
            # Cases where we should not generate a new cert
            # Cert data already exists, we should not refresh certs
            (
                {
                    "cert": "x",
                    "ca": "x",
                    "key": "x",
                },
                False,
            ),
        ],
    )
    def test_gen_certs_if_missing(
        self, cert_data_dict, should_certs_refresh, harness: Harness, mocker
    ):
        """Test _gen_certs_if_missing.
        This tests whether _gen_certs_if_missing:
        * generates a new cert if there is no existing one
        * does not generate a new cert if there is an existing one
        """
        # Arrange
        # Mock away gen_certs so the class does not generate any certs unless we want it to
        mocked_gen_certs = mocker.patch("charm.AdmissionWebhookCharm._gen_certs", autospec=True)
        harness.begin()
        mocked_gen_certs.reset_mock()

        # Set any provided cert data to _stored
        for k, v in cert_data_dict.items():
            setattr(harness.charm._stored, k, v)

        # Act
        harness.charm._gen_certs_if_missing()

        # Assert that we have/have not called refresh_certs, as expected
        assert mocked_gen_certs.called == should_certs_refresh
