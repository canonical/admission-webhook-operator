#!/usr/bin/env python3
# Copyright 2021 Canonical Ltd.
# See LICENSE file for licensing details.

import json
import logging
import os
from base64 import b64encode
from pathlib import Path
from subprocess import check_call

import yaml
from ops.charm import CharmBase
from ops.main import main
from ops.model import ActiveStatus, Application, MaintenanceStatus, WaitingStatus

from charmhelpers.core import hookenv
from oci_image import OCIImageResource, OCIImageResourceError

logger = logging.getLogger(__name__)


def gen_certs(namespace, service_name):
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


class CheckFailed(Exception):
    """Raise this exception if one of the checks in main fails."""

    def __init__(self, msg, status_type=None):
        super().__init__()

        self.msg = msg
        self.status_type = status_type
        self.status = status_type(msg)


class AdmissionWebhookCharm(CharmBase):
    """Deploys the admission-webhook service.

    Handles injecting common data such as secrets and environment variables
    into Kubeflow pods.
    """

    def __init__(self, framework):
        super().__init__(framework)
        self.image = OCIImageResource(self, "oci-image")
        self.framework.observe(self.on.install, self.set_pod_spec)
        self.framework.observe(self.on.upgrade_charm, self.set_pod_spec)
        self.framework.observe(
            self.on.pod_defaults_relation_changed,
            self.set_pod_spec,
        )

    def set_pod_spec(self, event):
        try:
            self._check_leader()

            image_details = self._check_image_details()
        except CheckFailed as check_failed:
            self.model.unit.status = check_failed.status
            return

        self.model.unit.status = MaintenanceStatus("Setting pod spec")

        pod_defaults = {
            key.name: dict(value)["pod-defaults"]
            for relation in self.model.relations["pod-defaults"]
            for key, value in relation.data.items()
            if isinstance(key, Application) and not key._is_our_app
        }
        custom_resources = {
            "poddefaults.kubeflow.org": [
                {
                    "apiVersion": "kubeflow.org/v1alpha1",
                    "kind": "PodDefault",
                    "metadata": {
                        "name": f"{charm}-{name}",
                    },
                    "spec": {
                        "selector": {
                            "matchLabels": {f"{charm}-{name}": "true"},
                        },
                        "env": [{"name": k, "value": v} for k, v in value["env"].items()],
                    },
                }
                for charm, defaults in pod_defaults.items()
                for name, value in json.loads(defaults).items()
            ],
        }

        model = os.environ["JUJU_MODEL_NAME"]

        gen_certs(model, hookenv.service_name())

        ca_bundle = b64encode(Path("/run/cert.pem").read_bytes()).decode("utf-8")

        self.model.pod.set_spec(
            {
                "version": 3,
                "serviceAccount": {
                    "roles": [
                        {
                            "global": True,
                            "rules": [
                                {
                                    "apiGroups": ["kubeflow.org"],
                                    "resources": ["poddefaults"],
                                    "verbs": [
                                        "get",
                                        "list",
                                        "watch",
                                        "update",
                                        "create",
                                        "patch",
                                        "delete",
                                    ],
                                },
                            ],
                        }
                    ],
                },
                "containers": [
                    {
                        "name": "admission-webhook",
                        "imageDetails": image_details,
                        "ports": [{"name": "webhook", "containerPort": 4443}],
                        "volumeConfig": [
                            {
                                "name": "certs",
                                "mountPath": "/etc/webhook/certs",
                                "files": [
                                    {
                                        "path": "cert.pem",
                                        "content": Path("/run/cert.pem").read_text(),
                                    },
                                    {
                                        "path": "key.pem",
                                        "content": Path("/run/server.key").read_text(),
                                    },
                                ],
                            }
                        ],
                    }
                ],
            },
            k8s_resources={
                "kubernetesResources": {
                    "customResourceDefinitions": [
                        {"name": crd["metadata"]["name"], "spec": crd["spec"]}
                        for crd in yaml.safe_load_all(Path("src/crds.yaml").read_text())
                    ],
                    "customResources": custom_resources,
                    "mutatingWebhookConfigurations": [
                        {
                            "name": "admission-webhook",
                            "webhooks": [
                                {
                                    # Probably not necessary, but keeps us in sync with upstream
                                    # which wasn't always using admissionReviewVersions/v1
                                    "admissionReviewVersions": [
                                        "v1beta1",
                                        "v1",
                                    ],
                                    "name": "admission-webhook.kubeflow.org",
                                    "failurePolicy": "Fail",
                                    "clientConfig": {
                                        "caBundle": ca_bundle,
                                        "service": {
                                            "name": hookenv.service_name(),
                                            "namespace": model,
                                            "path": "/apply-poddefault",
                                            "port": 4443,
                                        },
                                    },
                                    "namespaceSelector": {
                                        "matchLabels": {
                                            "app.kubernetes.io/part-of": "kubeflow-profile",
                                        },
                                    },
                                    "rules": [
                                        {
                                            "apiGroups": [""],
                                            "apiVersions": ["v1"],
                                            "operations": ["CREATE"],
                                            "resources": ["pods"],
                                        }
                                    ],
                                },
                            ],
                        }
                    ],
                }
            },
        )

        self.model.unit.status = ActiveStatus()

    def _check_leader(self):
        if not self.unit.is_leader():
            # We can't do anything useful when not the leader, so do nothing.
            raise CheckFailed("Waiting for leadership", WaitingStatus)

    def _check_image_details(self):
        try:
            image_details = self.image.fetch()
        except OCIImageResourceError as e:
            raise CheckFailed(f"{e.status.message}", e.status_type)
        return image_details


if __name__ == "__main__":
    main(AdmissionWebhookCharm)
