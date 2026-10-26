"""OpenStack upload: metadata derived from the manifest, and refusals that hold."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from vmforge.builder.manifest import Manifest
from vmforge.push.openstack import (
    PushError,
    image_name,
    image_properties,
    upload_image,
)


class FakeImages:
    def __init__(self) -> None:
        self.kwargs: dict | None = None

    def upload_image(self, **kwargs: object) -> dict[str, str]:
        self.kwargs = dict(kwargs)
        return {"id": "img-0001"}


class FakeConnection:
    def __init__(self) -> None:
        self.image = FakeImages()


def _build(tmp_path: Path, *, tamper: bool = False) -> Path:
    payload = b"qcow2 bytes"
    image = tmp_path / "image" / "vmforge-ubuntu-24.04.qcow2"
    image.parent.mkdir(parents=True)
    image.write_bytes(payload)
    digest = hashlib.sha256(payload if not tamper else b"other").hexdigest()
    manifest = {
        "spec_sha256": "a" * 64,
        "spec": {"packages": ["nginx"]},
        "base": {
            "os": "ubuntu",
            "version": "24.04",
            "url": "https://cloud-images.ubuntu.com/x.img",
            "sha256": "b" * 64,
        },
        "policy": {"source": "packaged:allowlist.yaml", "sha256": "c" * 64},
        "builder": {
            "packer_version": "1.16.2",
            "plugin_version": "1.1.7",
            "template_sha256": "d" * 64,
        },
        "packages": ["nginx"],
        "services": [],
        "artifact": {
            "file": image.name,
            "sha256": digest,
            "size_bytes": len(payload),
            "virtual_size_bytes": 8,
        },
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return tmp_path / "manifest.json"


def _manifest(tmp_path: Path) -> Manifest:
    return Manifest.model_validate_json(_build(tmp_path).read_text(encoding="utf-8"))


def test_metadata_comes_from_the_manifest(tmp_path: Path) -> None:
    properties = image_properties(_manifest(tmp_path))
    assert properties["os_distro"] == "ubuntu"
    assert properties["os_version"] == "24.04"
    assert properties["hw_qemu_guest_agent"] == "yes"
    assert properties["hw_disk_bus"] == "virtio"
    assert properties["vmforge_spec_sha256"] == "a" * 64
    assert properties["vmforge_policy_sha256"] == "c" * 64


def test_default_name_is_derived_from_the_spec_hash(tmp_path: Path) -> None:
    name = image_name(_manifest(tmp_path))
    assert name.startswith("ubuntu-24.04-")
    assert name.endswith("a" * 12)


def test_upload_sends_the_expected_request(tmp_path: Path) -> None:
    connection = FakeConnection()
    result = upload_image(_build(tmp_path), connection=connection)
    assert result.image_id == "img-0001"
    sent = connection.image.kwargs
    assert sent is not None
    assert sent["disk_format"] == "qcow2"
    assert sent["container_format"] == "bare"
    assert sent["is_public"] is False
    assert sent["properties"]["hw_qemu_guest_agent"] == "yes"


def test_a_tampered_artifact_is_never_uploaded(tmp_path: Path) -> None:
    connection = FakeConnection()
    with pytest.raises(PushError, match="does not match the manifest"):
        upload_image(_build(tmp_path, tamper=True), connection=connection)
    assert connection.image.kwargs is None


def test_unknown_disk_format_is_refused(tmp_path: Path) -> None:
    with pytest.raises(PushError, match="disk_format"):
        upload_image(_build(tmp_path), disk_format="vmdk", connection=FakeConnection())


def test_raw_is_accepted_for_ceph_backends(tmp_path: Path) -> None:
    connection = FakeConnection()
    result = upload_image(_build(tmp_path), disk_format="raw", connection=connection)
    assert result.disk_format == "raw"


def test_missing_image_file_is_an_error(tmp_path: Path) -> None:
    manifest_path = _build(tmp_path)
    (tmp_path / "image" / "vmforge-ubuntu-24.04.qcow2").unlink()
    with pytest.raises(PushError, match="does not exist"):
        upload_image(manifest_path, connection=FakeConnection())
