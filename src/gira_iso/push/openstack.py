"""OpenStack Glance upload.

Two refusals are built in. The artifact checksum is verified against the manifest
before a single byte is sent, and an image that cannot be described by its manifest
is not uploaded at all: a Glance image with guessed metadata is worse than no image.

`openstacksdk` is an optional dependency. The connection can be injected, which is
how this module is tested without a cloud.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from gira_iso.builder.manifest import Manifest, file_sha256

VALID_FORMATS = ("qcow2", "raw")


class PushError(RuntimeError):
    pass


class ImageApi(Protocol):
    def upload_image(self, **kwargs: Any) -> Any: ...


class Connection(Protocol):
    image: ImageApi


@dataclass(frozen=True)
class UploadResult:
    image_id: str
    name: str
    disk_format: str
    properties: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "image_id": self.image_id,
            "name": self.name,
            "disk_format": self.disk_format,
            "properties": self.properties,
        }


def image_properties(manifest: Manifest) -> dict[str, str]:
    """Glance properties derived from the manifest, never typed by hand."""
    return {
        "os_distro": manifest.base.os,
        "os_version": manifest.base.version,
        "hw_qemu_guest_agent": "yes",
        "hw_disk_bus": "virtio",
        "gira_iso_spec_sha256": manifest.spec_sha256,
        "gira_iso_policy_sha256": manifest.policy.sha256,
        "gira_iso_base_sha256": manifest.base.sha256,
        "gira_iso_manifest_version": manifest.manifest_version,
    }


def image_name(manifest: Manifest) -> str:
    return f"{manifest.base.os}-{manifest.base.version}-{manifest.spec_sha256[:12]}"


def resolve_connection(cloud: str | None) -> Connection:
    try:
        import openstack  # noqa: PLC0415 - optional dependency, imported on demand
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise PushError("openstacksdk is not installed: pip install 'gira-iso[openstack]'") from exc
    return openstack.connect(cloud=cloud)


def upload_image(
    manifest_path: Path,
    *,
    cloud: str | None = None,
    disk_format: str = "qcow2",
    connection: Connection | None = None,
    name: str | None = None,
) -> UploadResult:
    if disk_format not in VALID_FORMATS:
        raise PushError(f"disk_format must be one of {', '.join(VALID_FORMATS)}")

    manifest = Manifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    image = manifest_path.parent / "image" / manifest.artifact.file
    if not image.is_file():
        raise PushError(f"manifest points at {image}, which does not exist")

    actual = file_sha256(image)
    if actual != manifest.artifact.sha256:
        raise PushError(
            f"artifact checksum {actual[:16]}… does not match the manifest "
            f"{manifest.artifact.sha256[:16]}…; refusing to upload"
        )

    properties = image_properties(manifest)
    chosen_name = name or image_name(manifest)
    conn = connection or resolve_connection(cloud)

    created = conn.image.upload_image(
        name=chosen_name,
        filename=str(image),
        disk_format=disk_format,
        container_format="bare",
        properties=properties,
        is_public=False,
    )

    identifier = created["id"] if isinstance(created, dict) else getattr(created, "id", "")
    return UploadResult(
        image_id=str(identifier),
        name=chosen_name,
        disk_format=disk_format,
        properties=properties,
    )
