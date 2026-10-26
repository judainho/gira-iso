"""Shared fixtures: a minimal known-good spec and the real allowlist."""

from __future__ import annotations

import base64
from typing import Any

import pytest

from gira_iso.policy.evaluate import Allowlist, load_allowlist
from gira_iso.spec.models import Spec

VALID_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAABAgMEBQYHCAkKCwwNDg8QERITFBUWFxgZGhscHR4f"


def ssh_blob(payload: bytes) -> str:
    """Build the base64 body of an ed25519 key from a 32-byte payload."""
    blob = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + payload
    return base64.b64encode(blob).decode()


def build_spec(**overrides: Any) -> Spec:
    """A valid spec; pass top-level fields to override them."""
    data: dict[str, Any] = {
        "base": {"os": "ubuntu", "version": "24.04"},
        "disk_size": "8G",
        "packages": ["nginx"],
        "services": {"enable": ["nginx"]},
        "users": [{"name": "deploy", "sudo": True, "ssh_authorized_keys": [VALID_KEY]}],
        "files": [
            {
                "path": "/etc/nginx/conf.d/site.conf",
                "content": "server { listen 8080; }\n",
                "mode": "0644",
            }
        ],
    }
    data.update(overrides)
    return Spec.model_validate(data)


@pytest.fixture()
def valid_spec() -> Spec:
    return build_spec()


@pytest.fixture(scope="session")
def allowlist() -> Allowlist:
    return load_allowlist()


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path_factory, monkeypatch):
    """Registered bases live under GIRA_ISO_HOME; tests must never see the real ones."""
    monkeypatch.setenv("GIRA_ISO_HOME", str(tmp_path_factory.mktemp("gira-home")))


@pytest.fixture
def make_build():
    """Create a fake build directory with a valid manifest and artifact; returns a BuildResult."""
    from gira_iso.builder.build import BuildResult
    from gira_iso.builder.manifest import (
        Artifact,
        BaseRef,
        BuilderInfo,
        Manifest,
        PolicyRef,
        file_sha256,
    )

    def factory(root, *, name: str = "web", content: bytes = b"qcow2-bytes") -> BuildResult:
        build_dir = root / "20260101T000000Z-abcdef123456"
        image = build_dir / "image" / "gira-iso-ubuntu-24.04.qcow2"
        image.parent.mkdir(parents=True)
        image.write_bytes(content)
        Manifest(
            name=name,
            spec_sha256="0" * 64,
            spec={"spec_version": "0"},
            base=BaseRef(os="ubuntu", version="24.04", url="https://x/y.img", sha256="1" * 64),
            policy=PolicyRef(source="test", sha256="2" * 64),
            builder=BuilderInfo(
                packer_version="1.16.2", plugin_version="1.1.7", template_sha256="3" * 64
            ),
            packages=["nginx"],
            services=["nginx"],
            artifact=Artifact(
                file=image.name,
                sha256=file_sha256(image),
                size_bytes=len(content),
                virtual_size_bytes=10 * 1024**3,
            ),
        ).write(build_dir)
        return BuildResult(build_dir=build_dir, image=image, manifest=build_dir / "manifest.json")

    return factory


@pytest.fixture(autouse=True)
def _no_real_preflight(monkeypatch, request):
    """The suite must pass on a machine without QEMU or KVM; preflight tests opt out."""
    if request.node.get_closest_marker("real_preflight"):
        return
    monkeypatch.setattr("gira_iso.workflow.create.ensure_environment", lambda: None)
    monkeypatch.setattr("gira_iso.workflow.create.ensure_space", lambda root, gb: None)
    monkeypatch.setattr("gira_iso.tui.app.check_environment", lambda: [])


@pytest.fixture(autouse=True)
def _no_real_archive(monkeypatch):
    """Nothing in the suite may ask Launchpad (the one opt-in test does, via -m network).

    The workflow calls `archive.lookup` through the module, so replacing it here leaves the names
    imported by test_archive.py and cli/allow.py untouched.
    """
    monkeypatch.setattr("gira_iso.archive.lookup", lambda package, base_version, fetch=None: None)
