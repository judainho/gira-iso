"""Derive a goss spec from the gira-iso spec.

Only what the spec actually claims gets checked. The spec does not declare ports,
so gira-iso does not invent a port assertion: "nginx is installed and enabled" is a
claim the spec makes, "nginx listens on 80" is a claim it does not.

File content is compared by sha256 rather than by regex. Content is arbitrary text
that regularly contains braces, brackets and slashes, and a regex built from it
would be either wrong or unreadable.
"""

from __future__ import annotations

import hashlib
from typing import Any

import yaml

from gira_iso.builder.render import BUILD_USER, effective_services
from gira_iso.spec.models import Spec

SUDO_GROUP = "sudo"


def file_sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _user_checks(spec: Spec) -> dict[str, Any]:
    # The user Packer connected as must not survive into the image (finalize.sh removes it).
    users: dict[str, Any] = {BUILD_USER: {"exists": False}}
    for user in spec.users:
        users[user.name] = {"exists": True}
    return users


def _file_checks(spec: Spec) -> dict[str, Any]:
    files: dict[str, Any] = {f"/home/{BUILD_USER}": {"exists": False}}
    for user in spec.users:
        files[f"/home/{user.name}/.ssh/authorized_keys"] = {
            "exists": True,
            "owner": user.name,
            "mode": "0600",
        }
    for entry in spec.files:
        expectation: dict[str, Any] = {
            "exists": True,
            "mode": entry.mode,
            "sha256": file_sha256(entry.content),
        }
        if entry.owner:
            expectation["owner"] = entry.owner.split(":")[0]
        files[entry.path] = expectation
    return files


def _command_checks(spec: Spec) -> dict[str, Any]:
    commands: dict[str, Any] = {
        "cloud-init-finished": {
            "exec": "cloud-init status",
            "exit-status": 0,
            "stdout": ["status: done"],
        },
        "no-build-user-sudo": {
            "exec": f"! grep -rqsE '^{BUILD_USER}[[:space:]]' /etc/sudoers /etc/sudoers.d",
            "exit-status": 0,
        },
    }
    for user in spec.users:
        if user.sudo:
            # Membership, not the exact group list: other groups are not a failure.
            commands[f"sudo-{user.name}"] = {
                "exec": f"id -nG {user.name}",
                "exit-status": 0,
                "stdout": [SUDO_GROUP],
            }
    return commands


def build_goss(spec: Spec, packages: list[str]) -> dict[str, Any]:
    """Return the goss document as a plain dict, ready to serialise to YAML."""
    document: dict[str, Any] = {}

    if packages:
        document["package"] = {name: {"installed": True} for name in sorted(packages)}

    services = effective_services(spec)
    if services:
        document["service"] = {name: {"enabled": True, "running": True} for name in services}

    users = _user_checks(spec)
    if users:
        document["user"] = users

    files = _file_checks(spec)
    if files:
        document["file"] = files

    commands = _command_checks(spec)
    if commands:
        document["command"] = commands

    return document


def to_yaml(spec: Spec, packages: list[str]) -> str:
    return yaml.safe_dump(build_goss(spec, packages), sort_keys=True, width=1_000_000)
