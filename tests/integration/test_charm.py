# Copyright 2021 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from pathlib import Path
import pytest
import yaml
from pytest_operator.plugin import OpsTest

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
