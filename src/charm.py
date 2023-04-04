#!/usr/bin/env python3
# Copyright 2021 Canonical Ltd.
# See LICENSE file for licensing details.
"""A Juju Charm for Admission Webhook Operator."""

import json
import logging
import os
from base64 import b64encode
from pathlib import Path
from subprocess import check_call

import yaml
from charmhelpers.core import hookenv
from oci_image import OCIImageResource, OCIImageResourceError
from ops.charm import CharmBase
from ops.main import main
from lightkube import ApiError
from lightkube.models.core_v1 import ServicePort
from lightkube.generic_resource import load_in_cluster_generic_resources
from ops.model import ActiveStatus, Application, MaintenanceStatus, WaitingStatus
from ops.pebble import Layer
from charms.observability_libs.v1.kubernetes_service_patch import KubernetesServicePatch
from charmed_kubeflow_chisme.kubernetes import KubernetesResourceHandler
from charmed_kubeflow_chisme.exceptions import ErrorWithStatus, GenericCharmRuntimeError
from charmed_kubeflow_chisme.lightkube.batch import delete_many
from charmed_kubeflow_chisme.pebble import update_layer

K8S_RESOURCE_FILES = [
    "src/templates/webhook_configuration.yaml.j2",
    "src/templates/auth_manifests.yaml.j2",
]

CRD_RESOURCE_FILES = [
    "src/templates/crd.yaml.j2",
]

logger = logging.getLogger(__name__)


def gen_certs(namespace, service_name):
    """Generate certificates."""
    if Path("/run/cert.pem").exists():
        hookenv.log("Found existing cert.pem, not generating new cert.")
        return

    Path("/run/ssl.conf").write_text(
        f"""[ req ]
default_bits = 2048
prompt = no
default_md = sha256
req_extensions = req_ext
distinguished_name = dn
[ dn ]
C = GB
ST = Canonical
L = Canonical
O = Canonical
OU = Canonical
CN = 127.0.0.1
[ req_ext ]
subjectAltName = @alt_names
[ alt_names ]
DNS.1 = {service_name}
DNS.2 = {service_name}.{namespace}
DNS.3 = {service_name}.{namespace}.svc
DNS.4 = {service_name}.{namespace}.svc.cluster
DNS.5 = {service_name}.{namespace}.svc.cluster.local
IP.1 = 127.0.0.1
[ v3_ext ]
authorityKeyIdentifier=keyid,issuer:always
basicConstraints=CA:FALSE
keyUsage=keyEncipherment,dataEncipherment,digitalSignature
extendedKeyUsage=serverAuth,clientAuth
subjectAltName=@alt_names"""
    )

    check_call(["openssl", "genrsa", "-out", "/run/ca.key", "2048"])
    check_call(["openssl", "genrsa", "-out", "/run/server.key", "2048"])
    check_call(
        [
            "openssl",
            "req",
            "-x509",
            "-new",
            "-sha256",
            "-nodes",
            "-days",
            "3650",
            "-key",
            "/run/ca.key",
            "-subj",
            "/CN=127.0.0.1",
            "-out",
            "/run/ca.crt",
        ]
    )
    check_call(
        [
            "openssl",
            "req",
            "-new",
            "-sha256",
            "-key",
            "/run/server.key",
            "-out",
            "/run/server.csr",
            "-config",
            "/run/ssl.conf",
        ]
    )
    check_call(
        [
            "openssl",
            "x509",
            "-req",
            "-sha256",
            "-in",
            "/run/server.csr",
            "-CA",
            "/run/ca.crt",
            "-CAkey",
            "/run/ca.key",
            "-CAcreateserial",
            "-out",
            "/run/cert.pem",
            "-days",
            "365",
            "-extensions",
            "v3_ext",
            "-extfile",
            "/run/ssl.conf",
        ]
    )


class AdmissionWebhookCharm(CharmBase):
    """A Juju Charm for Admission Webhook Operator."""

    def __init__(self, framework):
        """Initialize charm and setup the container."""
        super().__init__(framework)
        self.image = OCIImageResource(self, "oci-image")
        self.framework.observe(self.on.install, self.set_pod_spec)
        self.framework.observe(self.on.upgrade_charm, self.set_pod_spec)
        self.framework.observe(self.on.leader_elected, self.set_pod_spec)
        self.framework.observe(
            self.on.pod_defaults_relation_changed,
            self.set_pod_spec,
        )
        self._container_name = "admission-webhook"
        self._container = self.unit.get_container(self._container_name)
        self._port = self.model.config["port"]
        self._lightkube_field_manager = "lightkube"
        self._namespace = self.model.name
        self._name = self.model.app.name

        # setup context to be used for updating K8S resources
        self._context = {
            "app_name": self._name,
            "namespace": self._namespace,
            "service_name": hookenv.service_name(),
            "port": self._port,
            "ca_bundle": "",  # b64encode(self._stored.ca.encode("ascii")).decode("utf-8"),
        }

        port = ServicePort(int(self._port), name=f"{self.app.name}")
        self.service_patcher = KubernetesServicePatch(self, [port])

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
    def _admission_webhook_layer(self) -> Layer:
        """Create and return Pebble framework layer."""
        layer_config = {
            "summary": "admission-webhook layer",
            "description": "Pebble config layer for admission-webhook-operator",
            "services": {
                self._container_name: {
                    "override": "replace",
                    "summary": "Entry point of admission-webhook-operator image",
                    "startup": "enabled",
                },
            },
        }
        return Layer(layer_config)

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

    def _on_install(self, _):
        """Installation only tasks."""
        # deploy K8S resources to speed up deployment
        self._apply_k8s_resources()

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
                self.logger.error(f"Failed to delete CRD resources, with error: {error}")
                delete_error = error
        try:
            delete_many(self.crd_resource_handler.lightkube_client, crd_resources_manifests)
        except ApiError as error:
            # do not log/report when resources were not found
            if error.status.code != 404:
                self.logger.error(f"Failed to delete K8S resources, with error: {error}")
                delete_error = error

        if delete_error is not None:
            raise delete_error

        self.unit.status = MaintenanceStatus("K8S resources removed")

    def _on_event(self, event, force_conflicts: bool = False) -> None:
        """Perform all required actions for the Charm.

        Args:
            force_conflicts (bool): Should only be used when need to resolved conflicts on K8S
                                    resources.
        """
        try:
            self._check_leader()
            self._apply_k8s_resources(force_conflicts=force_conflicts)
            update_layer(
                self._container_name,
                self._container,
                self._admission_webhook_layer,
                self.logger,
            )
        except ErrorWithStatus as err:
            self.model.unit.status = err.status
            self.logger.error(f"Failed to handle {event} with error: {err}")


if __name__ == "__main__":
    main(AdmissionWebhookCharm)
