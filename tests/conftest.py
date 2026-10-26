"""Shared fixtures: a minimal known-good spec and the real allowlist."""

from __future__ import annotations

import base64
from typing import Any

import pytest

from vmforge.policy.evaluate import Allowlist, load_allowlist
from vmforge.spec.models import Spec

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
