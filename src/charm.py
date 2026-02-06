#!/usr/bin/env python3
# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.
"""A Juju Charm for Admission Webhook Operator."""
import logging
from base64 import b64encode
from pathlib import Path

from charmed_kubeflow_chisme.exceptions import ErrorWithStatus, GenericCharmRuntimeError
from charmed_kubeflow_chisme.kubernetes import KubernetesResourceHandler
from charmed_kubeflow_chisme.lightkube.batch import delete_many
from charmed_kubeflow_chisme.pebble import update_layer
from charmed_kubeflow_chisme.service_mesh import generate_allow_all_authorization_policy
from charms.istio_beacon_k8s.v0.service_mesh import PolicyResourceManager, ServiceMeshConsumer
from charms.loki_k8s.v1.loki_push_api import LogForwarder
from charms.observability_libs.v1.kubernetes_service_patch import KubernetesServicePatch
from lightkube import ApiError, Client
from lightkube.generic_resource import load_in_cluster_generic_resources
from lightkube.models.core_v1 import ServicePort
from ops.charm import CharmBase
from ops.framework import StoredState
from ops.main import main
from ops.model import ActiveStatus, Container, MaintenanceStatus, ModelError, WaitingStatus
from ops.pebble import CheckStatus, Layer, PathError

from certs import gen_certs

K8S_RESOURCE_FILES = [
    "src/templates/webhook_configuration.yaml.j2",
    "src/templates/auth_manifests.yaml.j2",
]

CRD_RESOURCE_FILES = [
    "src/templates/crds.yaml.j2",
]

CONTAINER_CERTS_DEST = Path("/etc/webhook/certs")

SERVICE_MESH_RELATION_NAME = "service-mesh"


class AdmissionWebhookCharm(CharmBase):
    """A Juju Charm for Admission Webhook Operator."""

    _stored = StoredState()

    def __init__(self, framework):
        """Initialize charm and setup the container."""
        super().__init__(framework)

        # retrieve configuration and base settings
        self.logger = logging.getLogger(__name__)
        self._container_name = "admission-webhook"
        self._container = self.unit.get_container(self._container_name)
        self._port = self.model.config["port"]
        self._lightkube_field_manager = "lightkube"
        self._exec_command = "/webhook"
        self._namespace = self.model.name
        self._name = self.model.app.name
        self._service_name = self._name
        self._k8s_resource_handler = None
        self._crd_resource_handler = None

        # setup events to be handled by main event handler
        for event in [
            self.on.install,
            self.on.config_changed,
            self.on.admission_webhook_pebble_ready,
            self.on[SERVICE_MESH_RELATION_NAME].relation_changed,
        ]:
            self.framework.observe(event, self._on_event)
        # setup events to be handled by specific event handlers
        self.framework.observe(self.on.upgrade_charm, self._on_upgrade)
        self.framework.observe(self.on.remove, self._on_remove)
        self.framework.observe(self.on.update_status, self._on_update_status)

        # setup event to handle removing authorization policies on relation broken
        self.framework.observe(
            self.on[SERVICE_MESH_RELATION_NAME].relation_broken,
            self._remove_authorization_policies,
        )

        # generate certs
        self._gen_certs_if_missing()

        port = ServicePort(int(self._port), name=f"{self.app.name}")
        self.service_patcher = KubernetesServicePatch(
            self,
            [port],
            service_name=f"{self.model.app.name}",
        )
        self._logging = LogForwarder(charm=self)

        self._mesh = ServiceMeshConsumer(self)

        # Allow all policy needed to allow the K8s API to talk to the webhook
        self._allow_all_policy = generate_allow_all_authorization_policy(
            app_name=self.app.name,
            namespace=self.model.name,
        )

    @property
    def _context(self):
        return {
            "app_name": self._name,
            "namespace": self._namespace,
            "port": self._port,
            "ca_bundle": b64encode(self._cert_ca.encode("ascii")).decode("utf-8"),
            "service_name": self._service_name,
        }

    @property
    def container(self):
        """Return container."""
        return self._container

    @property
    def k8s_resource_handler(self):
        """Update K8S with K8S resources."""
        if not self._k8s_resource_handler:
            self._k8s_resource_handler = KubernetesResourceHandler(
                field_manager=self._lightkube_field_manager,
                template_files=K8S_RESOURCE_FILES,
                context=self._context,
                logger=self.logger,
            )
        load_in_cluster_generic_resources(self._k8s_resource_handler.lightkube_client)
        return self._k8s_resource_handler

    @k8s_resource_handler.setter
    def k8s_resource_handler(self, handler: KubernetesResourceHandler):
        self._k8s_resource_handler = handler

    @property
    def crd_resource_handler(self):
        """Update K8S with CRD resources."""
        if not self._crd_resource_handler:
            self._crd_resource_handler = KubernetesResourceHandler(
                field_manager=self._lightkube_field_manager,
                template_files=CRD_RESOURCE_FILES,
                context=self._context,
                logger=self.logger,
            )
        load_in_cluster_generic_resources(self._crd_resource_handler.lightkube_client)
        return self._crd_resource_handler

    @property
    def _policy_resource_manager(self) -> PolicyResourceManager:
        """Create and return PolicyResourceManager, used to manage authorization policies."""
        return PolicyResourceManager(
            charm=self,
            lightkube_client=Client(field_manager=f"{self.app.name}-{self.model.name}"),
            labels={
                "app.kubernetes.io/instance": f"{self.app.name}-{self.model.name}",
                "kubernetes-resource-handler-scope": f"{self.app.name}-allow-all",
            },
            logger=self.logger,
        )

    @property
    def _admission_webhook_layer(self) -> Layer:
        """Create and return Pebble framework layer."""
        layer_config = {
            "summary": "admission-webhook layer",
            "description": "Pebble config layer for admission-webhook-operator",
            "services": {
                self._container_name: {
                    "override": "replace",
                    "summary": "Pebble service for admission-webhook-operator",
                    "startup": "enabled",
                    "command": self._exec_command,
                    "on-check-failure": {"admission-webhook-up": "restart"},
                },
            },
            "checks": {
                "admission-webhook-up": {
                    "override": "replace",
                    "period": "30s",
                    "timeout": "20s",
                    "threshold": 4,
                    "tcp": {"port": self._port},
                }
            },
        }
        return Layer(layer_config)

    @property
    def _cert(self):
        return self._stored.cert

    @property
    def _cert_key(self):
        return self._stored.key

    @property
    def _cert_ca(self):
        return self._stored.ca

    def _check_leader(self):
        """Check if this unit is a leader."""
        if not self.unit.is_leader():
            self.logger.info("Not a leader, skipping setup")
            raise ErrorWithStatus("Waiting for leadership", WaitingStatus)

    def _check_and_report_k8s_conflict(self, error):
        """Return True if error status code is 409 (conflict), False otherwise."""
        if error.status.code == 409:
            self.logger.warning(f"Encountered a conflict: {error}")
            return True
        return False

    def _apply_k8s_resources(self, force_conflicts: bool = False) -> None:
        """Apply K8S resources.

        Args:
            force_conflicts (bool): *(optional)* Will "force" apply requests causing conflicting
                                    fields to change ownership to the field manager used in this
                                    charm.
                                    NOTE: This will only be used if initial regular apply() fails.
        """
        self.unit.status = MaintenanceStatus("Creating K8S resources")
        try:
            self.k8s_resource_handler.apply()
        except ApiError as error:
            if self._check_and_report_k8s_conflict(error) and force_conflicts:
                # conflict detected when applying K8S resources
                # re-apply K8S resources with forced conflict resolution
                self.unit.status = MaintenanceStatus("Force applying K8S resources")
                self.logger.warning("Apply K8S resources with forced changes against conflicts")
                self.k8s_resource_handler.apply(force=force_conflicts)
            else:
                raise GenericCharmRuntimeError("K8S resources creation failed") from error
        try:
            self.crd_resource_handler.apply()
        except ApiError as error:
            if self._check_and_report_k8s_conflict(error) and force_conflicts:
                # conflict detected when applying CRD resources
                # re-apply CRD resources with forced conflict resolution
                self.unit.status = MaintenanceStatus("Force applying CRD resources")
                self.logger.warning("Apply CRD resources with forced changes against conflicts")
                self.crd_resource_handler.apply(force=force_conflicts)
            else:
                raise GenericCharmRuntimeError("CRD resources creation failed") from error
        self.model.unit.status = MaintenanceStatus("K8S resources created")

    def _reconcile_policy_resource_manager(self):
        if self._mesh._relation:
            self._policy_resource_manager.reconcile(
                policies=[], mesh_type=self._mesh.mesh_type, raw_policies=[self._allow_all_policy]
            )

    def _remove_authorization_policies(self, _):
        self._policy_resource_manager.delete()

    def _gen_certs_if_missing(self):
        """Generate certificates if they don't already exist in _stored."""
        self.logger.info("Generating certificates if missing.")
        cert_attributes = ["cert", "ca", "key"]
        # Generate new certs if any cert attribute is missing
        for cert_attribute in cert_attributes:
            try:
                getattr(self._stored, cert_attribute)
            except AttributeError:
                self._gen_certs()
                break
        self.logger.info("Certificates already exist.")

    def _gen_certs(self):
        """Refresh the certificates, overwriting them if they already existed."""
        self.logger.info("Generating certificates..")
        certs = gen_certs(model=self._namespace, service_name=self._service_name)
        for k, v in certs.items():
            setattr(self._stored, k, v)

    def _upload_certs_to_container(self, event):
        """Upload generated certs to container."""
        if not self._certificate_files_exist():
            try:
                self.container.push(
                    CONTAINER_CERTS_DEST / "key.pem", self._cert_key, make_dirs=True
                )
                self.container.push(CONTAINER_CERTS_DEST / "cert.pem", self._cert, make_dirs=True)
            except PathError as e:
                raise GenericCharmRuntimeError("Failed to push certs to container") from e

    def _check_container_connection(self, container: Container) -> None:
        """Check if connection can be made with container.
        Args:
            container: the named container in a unit to check.
        Raises:
            ErrorWithStatus if the connection cannot be made.
        """
        if not container.can_connect():
            self.logger.warning("Connection cannot be established with container")
            raise ErrorWithStatus("Pod startup is not complete", MaintenanceStatus)

    def _get_check_status(self):
        return self.container.get_check("admission-webhook-up").status

    def _refresh_status(self):
        """Check leader, refresh status of workload, and set status accordingly."""
        self._check_leader()
        try:
            check = self._get_check_status()
        except ModelError as error:
            raise GenericCharmRuntimeError(
                "Failed to run health check on workload container"
            ) from error
        if check != CheckStatus.UP:
            self.logger.error(
                f"Container {self._container_name} failed health check. It will be restarted."
            )
            raise ErrorWithStatus("Workload failed health check", MaintenanceStatus)
        else:
            self.model.unit.status = ActiveStatus()

    def _on_remove(self, _):
        """Remove all resources."""
        delete_error = None
        self.unit.status = MaintenanceStatus("Removing K8S resources")
        k8s_resources_manifests = self.k8s_resource_handler.render_manifests()
        crd_resources_manifests = self.crd_resource_handler.render_manifests()
        try:
            delete_many(self.k8s_resource_handler.lightkube_client, k8s_resources_manifests)
        except ApiError as error:
            # do not log/report when resources were not found
            if error.status.code != 404:
                self.logger.error(f"Failed to delete K8S resources, with error: {error}")
                delete_error = error
        try:
            delete_many(self.crd_resource_handler.lightkube_client, crd_resources_manifests)
        except ApiError as error:
            # do not log/report when resources were not found
            if error.status.code != 404:
                self.logger.error(f"Failed to delete CRD resources, with error: {error}")
                delete_error = error

        if delete_error is not None:
            raise delete_error

        self._remove_authorization_policies(_)

        self.unit.status = MaintenanceStatus("K8S resources removed")

    def _on_upgrade(self, _):
        """Perform upgrade steps."""
        # force conflict resolution in K8S resources update
        self._on_event(_, force_conflicts=True)

    def _on_update_status(self, event):
        """Update status actions."""
        try:
            self._refresh_status()
        except ErrorWithStatus as err:
            self.model.unit.status = err.status

    def _certificate_files_exist(self) -> bool:
        """Check that the certificate and key files can be pulled from the container."""
        try:
            self.container.pull(CONTAINER_CERTS_DEST / "key.pem")
            self.container.pull(CONTAINER_CERTS_DEST / "cert.pem")
            return True
        except PathError:
            return False

    def _on_event(self, event, force_conflicts: bool = False) -> None:
        """Perform all required actions for the Charm.

        Args:
            force_conflicts (bool): Should only be used when need to resolved conflicts on K8S
                                    resources.
        """
        try:
            self._check_leader()
            self._check_container_connection(self.container)
            self._apply_k8s_resources(force_conflicts=force_conflicts)
            self._reconcile_policy_resource_manager()
            self._upload_certs_to_container(event)
            update_layer(
                self._container_name,
                self._container,
                self._admission_webhook_layer,
                self.logger,
            )
        except ErrorWithStatus as err:
            self.model.unit.status = err.status
            self.logger.error(f"Failed to handle {event} with error: {err}")
            return

        self.model.unit.status = ActiveStatus()


if __name__ == "__main__":
    main(AdmissionWebhookCharm)
