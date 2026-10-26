"""Policy layer: decides whether a syntactically valid spec is allowed to build.

`evaluate` is a pure function over a spec and an allowlist. No network, no
filesystem, no model. That is deliberate: the security decision must be reproducible
and testable in isolation, and must not depend on anything the LLM produced beyond
the spec values themselves.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from vmforge.spec.models import FileEntry, Spec

DEFAULT_ALLOWLIST = "allowlist.yaml"


@dataclass(frozen=True)
class Violation:
    code: str
    message: str
    location: str


@dataclass(frozen=True)
class Verdict:
    violations: tuple[Violation, ...]

    @property
    def passed(self) -> bool:
        return not self.violations

    def codes(self) -> tuple[str, ...]:
        return tuple(v.code for v in self.violations)


@dataclass(frozen=True)
class Allowlist:
    base_images: dict[str, dict[str, dict[str, str]]]
    packages: frozenset[str]
    services: frozenset[str]
    required_services: frozenset[str]
    mandatory_packages: frozenset[str]
    file_prefixes: tuple[str, ...]
    forbidden_prefixes: tuple[str, ...]

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> Allowlist:
        return cls(
            base_images=data["base_images"],
            packages=frozenset(data["packages"]),
            services=frozenset(data["services"]),
            required_services=frozenset(data.get("required_services", [])),
            mandatory_packages=frozenset(data.get("mandatory_packages", [])),
            file_prefixes=tuple(data.get("file_prefixes", [])),
            forbidden_prefixes=tuple(data.get("forbidden_prefixes", [])),
        )

    def base_image(self, os_name: str, version: str) -> dict[str, str] | None:
        return self.base_images.get(os_name, {}).get(version)


ENV_VAR = "VMFORGE_ALLOWLIST"


def read_allowlist_text(path: str | None = None) -> tuple[str, str]:
    """Return (label, text) of the active allowlist.

    An organisation overrides the policy by pointing `VMFORGE_ALLOWLIST` at its own
    file; nothing in the package needs to change, and the build records which policy
    it used.
    """
    chosen = path or os.environ.get(ENV_VAR)
    if chosen:
        source = Path(chosen).expanduser()
        return str(source), source.read_text(encoding="utf-8")
    return f"packaged:{DEFAULT_ALLOWLIST}", resources.files("vmforge.policy").joinpath(
        DEFAULT_ALLOWLIST
    ).read_text("utf-8")


def allowlist_digest(path: str | None = None) -> tuple[str, str]:
    """Return (label, sha256) of the active allowlist, for build provenance."""
    label, text = read_allowlist_text(path)
    return label, hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_allowlist(path: str | None = None) -> Allowlist:
    _, text = read_allowlist_text(path)
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError("allowlist must be a mapping")
    return Allowlist.from_mapping(data)


def _matches(path: str, prefixes: tuple[str, ...]) -> bool:
    """A prefix ending in '/' matches a subtree; otherwise it must be exact.

    Exact matching for non-slash entries is what stops `/etc/motd` from also
    allowing `/etc/motd.backup-anything`.
    """
    for prefix in prefixes:
        if prefix.endswith("/"):
            if path.startswith(prefix):
                return True
        elif path == prefix:
            return True
    return False


def _check_file(entry: FileEntry, index: int, allowlist: Allowlist) -> list[Violation]:
    location = f"files[{index}]"
    violations: list[Violation] = []

    if _matches(entry.path, allowlist.forbidden_prefixes):
        violations.append(
            Violation(
                code="file.path.forbidden",
                message=f"{entry.path} is on the never-writable list",
                location=location,
            )
        )
    elif not _matches(entry.path, allowlist.file_prefixes):
        violations.append(
            Violation(
                code="file.path.outside_allowlist",
                message=f"{entry.path} is not under any allowed prefix",
                location=location,
            )
        )

    if (
        entry.owner is not None
        and entry.owner.split(":")[0] == "root"
        and entry.mode[1:2]
        in {
            "7",
            "6",
            "4",
        }
    ):
        # root-owned file with group/other read is usually a mistake, not a policy
        # breach; flagged so it never ships silently as a world-readable secret.
        violations.append(
            Violation(
                code="file.permissions.world_readable_root",
                message=f"{entry.path} is root owned and readable beyond root (mode {entry.mode})",
                location=location,
            )
        )

    return violations


def evaluate(spec: Spec, allowlist: Allowlist | None = None) -> Verdict:
    """Return every reason this spec must not be built. Empty means it may build."""
    allowlist = allowlist or load_allowlist()
    violations: list[Violation] = []

    if allowlist.base_image(spec.base.os, spec.base.version) is None:
        violations.append(
            Violation(
                code="base.unknown",
                message=f"no pinned image for {spec.base.os} {spec.base.version}",
                location="base",
            )
        )

    for index, package in enumerate(spec.packages):
        if package not in allowlist.packages:
            violations.append(
                Violation(
                    code="package.not_allowed",
                    message=f"{package} is not in the package allowlist",
                    location=f"packages[{index}]",
                )
            )

    for index, service in enumerate(spec.services.enable):
        if service not in allowlist.services:
            violations.append(
                Violation(
                    code="service.not_allowed",
                    message=f"{service} is not in the service allowlist",
                    location=f"services.enable[{index}]",
                )
            )

    for index, service in enumerate(spec.services.disable):
        if service in allowlist.required_services:
            violations.append(
                Violation(
                    code="service.required",
                    message=f"{service} cannot be disabled: the image must stay reachable "
                    "and cloud-ready",
                    location=f"services.disable[{index}]",
                )
            )
        elif service not in allowlist.services:
            violations.append(
                Violation(
                    code="service.not_allowed",
                    message=f"{service} is not in the service allowlist",
                    location=f"services.disable[{index}]",
                )
            )

    for index, entry in enumerate(spec.files):
        violations.extend(_check_file(entry, index, allowlist))

    return Verdict(violations=tuple(violations))
