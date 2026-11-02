"""Opt-in: upload a real build to Glance and leave the image behind for a boot check.

The default suite never runs this: it is marked `network`, and it also skips unless a cloud
and a build are configured. No cloud is configured in CI, so nothing here is pretend-tested.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

from gira_iso.push.openstack import upload_image

BUILD = os.environ.get("GIRA_ISO_INTEGRATION_BUILD", "")
HAS_SDK = importlib.util.find_spec("openstack") is not None
HAS_CLOUD = bool(os.environ.get("OS_CLOUD") or os.environ.get("OS_AUTH_URL"))

pytestmark = [
    pytest.mark.network,
    pytest.mark.skipif(not BUILD, reason="set GIRA_ISO_INTEGRATION_BUILD to a build directory"),
    pytest.mark.skipif(not HAS_SDK, reason="openstacksdk is not installed"),
    pytest.mark.skipif(not HAS_CLOUD, reason="no OpenStack credentials (OS_CLOUD or OS_AUTH_URL)"),
]


def test_upload_a_real_build_to_glance() -> None:
    build = Path(BUILD).expanduser()
    manifest = build / "manifest.json" if build.is_dir() else build

    result = upload_image(
        manifest,
        cloud=os.environ.get("OS_CLOUD"),
        name=os.environ.get("GIRA_ISO_INTEGRATION_NAME") or None,
    )

    print(f"\nuploaded {result.name} as {result.image_id} ({result.disk_format})")
    print(f"boot it with: openstack server create --image {result.image_id} ...")
    print(f"delete it with: openstack image delete {result.image_id}")

    assert result.image_id
