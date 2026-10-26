"""Render a spec into the files Packer consumes.

Only the HCL and shell scripts go through Jinja. The cloud-init seed is built as a
Python dict and dumped as YAML: file contents are arbitrary text, and hand-rolled
template indentation silently changes them (trailing spaces inside a block scalar,
for example). Dumping a dict means the content is escaped by the YAML library, not
by string concatenation.
"""

from __future__ import annotations

import hashlib
import stat
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, StrictUndefined

from gira_iso.spec.canonical import spec_hash
from gira_iso.spec.models import Spec

PLUGIN_VERSION = "1.1.7"
IMAGE_NAME = "gira-iso-ubuntu-24.04.qcow2"
BUILD_HOSTNAME = "gira-iso-build"
BUILD_USER = "packer"
BUILD_INSTANCE_ID = "gira-iso-build-001"


@dataclass(frozen=True)
class Rendered:
    packer_dir: Path
    seed_dir: Path
    provision_dir: Path
    template_sha256: str


def _environment() -> Environment:
    return Environment(
        undefined=StrictUndefined,
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )


def _template_text(name: str) -> str:
    return resources.files("gira_iso.templates").joinpath(name).read_text("utf-8")


def render_text(name: str, context: dict[str, Any]) -> str:
    return _environment().from_string(_template_text(name)).render(**context)


def disk_size_mb(disk_size: str) -> int:
    return int(disk_size.removesuffix("G")) * 1024


def effective_packages(spec: Spec, mandatory: frozenset[str]) -> list[str]:
    """Spec packages plus what the image needs to be cloud-ready, deduplicated."""
    return sorted(set(spec.packages) | set(mandatory))


def effective_services(spec: Spec) -> list[str]:
    return sorted(set(spec.services.enable))


def cloud_init(
    spec: Spec, build_authorized_key: str, mandatory_packages: frozenset[str]
) -> tuple[str, str]:
    """Return (user-data, meta-data) for the build VM's NoCloud seed."""
    users: list[dict[str, Any]] = [
        {
            "name": BUILD_USER,
            "shell": "/bin/bash",
            "sudo": "ALL=(ALL) NOPASSWD:ALL",
            "groups": ["sudo"],
            "ssh_authorized_keys": [build_authorized_key],
        }
    ]
    for user in spec.users:
        entry: dict[str, Any] = {
            "name": user.name,
            "shell": "/bin/bash",
            "ssh_authorized_keys": list(user.ssh_authorized_keys),
        }
        if user.sudo:
            entry["sudo"] = "ALL=(ALL) NOPASSWD:ALL"
            entry["groups"] = ["sudo"]
        users.append(entry)

    files: list[dict[str, Any]] = []
    for entry in spec.files:
        item: dict[str, Any] = {
            "path": entry.path,
            "content": entry.content,
            "permissions": entry.mode,
        }
        if entry.owner:
            item["owner"] = entry.owner
        files.append(item)

    runcmd = [f"systemctl enable {service}" for service in effective_services(spec)]
    runcmd += [f"systemctl disable {service} || true" for service in spec.services.disable]

    user_data: dict[str, Any] = {
        "hostname": BUILD_HOSTNAME,
        "ssh_pwauth": False,
        "package_update": True,
        "package_upgrade": False,
        "users": users,
        "packages": effective_packages(spec, mandatory_packages),
        "runcmd": runcmd,
    }
    if files:
        user_data["write_files"] = files

    dumped = "#cloud-config\n" + yaml.safe_dump(
        user_data, sort_keys=True, default_flow_style=False, width=10_000
    )
    meta_data = yaml.safe_dump(
        {"instance-id": BUILD_INSTANCE_ID, "local-hostname": BUILD_HOSTNAME},
        sort_keys=True,
    )
    return dumped, meta_data


def render(
    spec: Spec,
    build_dir: Path,
    mandatory_packages: frozenset[str],
    build_authorized_key: str,
) -> Rendered:
    """Write every file Packer needs under `build_dir` and return their locations."""
    packer_dir = build_dir / "packer"
    seed_dir = build_dir / "seed"
    provision_dir = build_dir / "provision"
    for directory in (packer_dir, seed_dir, provision_dir):
        directory.mkdir(parents=True, exist_ok=True)

    packages = effective_packages(spec, mandatory_packages)
    services = effective_services(spec)

    template = render_text(
        "packer/image.pkr.hcl.j2",
        {
            "spec_sha256": spec_hash(spec),
            "plugin_version": PLUGIN_VERSION,
            "image_name": IMAGE_NAME,
            "disk_size_mb": disk_size_mb(spec.disk_size),
            "cpus": 2,
            "memory": 2048,
            "packages": packages,
            "services": services,
        },
    )
    (packer_dir / "image.pkr.hcl").write_text(template, encoding="utf-8")

    user_data, meta_data = cloud_init(spec, build_authorized_key, mandatory_packages)
    (seed_dir / "user-data").write_text(user_data, encoding="utf-8")
    (seed_dir / "meta-data").write_text(meta_data, encoding="utf-8")

    assert_script = render_text(
        "scripts/assert.sh.j2", {"packages": packages, "services": services}
    )
    cleanup_script = render_text("scripts/cleanup.sh.j2", {})
    for name, body in (("assert.sh", assert_script), ("cleanup.sh", cleanup_script)):
        path = provision_dir / name
        path.write_text(body, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    return Rendered(
        packer_dir=packer_dir,
        seed_dir=seed_dir,
        provision_dir=provision_dir,
        template_sha256=hashlib.sha256(template.encode("utf-8")).hexdigest(),
    )
