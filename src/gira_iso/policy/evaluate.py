"""Policy layer: decides whether a syntactically valid spec is allowed to build.

`evaluate` is a pure function over a spec and an allowlist. No network, no
filesystem, no model. That is deliberate: the security decision must be reproducible
and testable in isolation, and must not depend on anything the LLM produced beyond
the spec values themselves.
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import re
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from gira_iso.additions import canonical_text, local_packages_by_base, strict_mode
from gira_iso.bases import merge_into
from gira_iso.spec.models import FileEntry, Spec

DEFAULT_ALLOWLIST = "allowlist.yaml"
SNAP_CHANNELS = frozenset({"stable", "candidate", "beta", "edge"})
SHA256 = re.compile(r"^[0-9a-f]{64}\Z")
# The fields below are interpolated into a provisioning script that runs as root and into
# root-owned paths. They come from the reviewed allowlist, never from the spec, but a policy
# edit must not be able to turn data into shell: no quotes, backslashes, whitespace, `$` or
# backticks, and no path traversal in an identifier. `\Z`, not `$`: `$` also matches before a
# trailing newline, which is exactly the character that would break out of a heredoc.
SNAP_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}\Z")
REPOSITORY_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}\Z")
SUITE = re.compile(r"^(\{codename\}|[a-z0-9][a-z0-9._-]{0,63})\Z")
COMPONENT = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}\Z")
HTTPS_URL = re.compile(r"^https://[A-Za-z0-9._~:/?#\[\]@!&()*+,;=%-]+\Z")


@dataclass(frozen=True)
class SnapPolicy:
    """How one allowlisted snap is installed. The spec only names it."""

    name: str
    channel: str = "stable"
    classic: bool = False
    revision: int | None = None


@dataclass(frozen=True)
class RepositoryPolicy:
    """A vendor apt repository, with its key pinned by hash."""

    id: str
    url: str
    suite: str
    components: tuple[str, ...]
    key_url: str
    key_sha256: str


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
    # Snaps the spec may name, and how each one is installed.
    snaps: dict[str, SnapPolicy] = field(default_factory=dict)
    # Vendor apt repositories, and which allowlisted package comes from which one.
    repositories: dict[str, RepositoryPolicy] = field(default_factory=dict)
    package_repository: dict[str, str] = field(default_factory=dict)
    # Packages a person added on this machine, by Ubuntu release (see additions.py). Empty in
    # strict mode and whenever a specific allowlist file is loaded.
    local_packages: dict[str, frozenset[str]] = field(default_factory=dict)
    # Never offered for addition, even if they exist in the archive.
    never_add: frozenset[str] = frozenset()

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> Allowlist:
        packages, package_repository = _package_entries(data["packages"])
        repositories = _repository_entries(data.get("repositories") or {}, package_repository)
        return cls(
            base_images=data["base_images"],
            packages=packages,
            services=frozenset(data["services"]),
            required_services=frozenset(data.get("required_services", [])),
            mandatory_packages=frozenset(data.get("mandatory_packages", [])),
            file_prefixes=tuple(data.get("file_prefixes", [])),
            forbidden_prefixes=tuple(data.get("forbidden_prefixes", [])),
            snaps=_snap_entries(data.get("snaps") or {}),
            repositories=repositories,
            package_repository=package_repository,
            never_add=frozenset(data.get("never_add", [])),
        )

    def base_image(self, os_name: str, version: str) -> dict[str, str] | None:
        return self.base_images.get(os_name, {}).get(version)

    def repository_of(self, package: str) -> str | None:
        """The repository a package comes from, or None when Ubuntu's archive provides it."""
        return self.package_repository.get(package)

    def allows_package(self, name: str, base_version: str) -> bool:
        """Packaged packages always; local additions only for the release they were checked on."""
        if name in self.packages:
            return True
        return (
            name in self.local_packages.get(base_version, frozenset())
            and name not in self.never_add
        )


def _package_entries(raw: Any) -> tuple[frozenset[str], dict[str, str]]:
    """Packages may be a bare name or `{name, repository}`. Both forms coexist."""
    names: set[str] = set()
    owned: dict[str, str] = {}
    for entry in raw:
        if isinstance(entry, str):
            names.add(entry)
            continue
        if not isinstance(entry, dict) or not entry.get("name") or not entry.get("repository"):
            raise ValueError(f"invalid package entry: {entry!r}")
        name, repository = str(entry["name"]), str(entry["repository"])
        if name in owned and owned[name] != repository:
            raise ValueError(f"package {name} is listed under two repositories")
        names.add(name)
        owned[name] = repository
    return frozenset(names), owned


def _snap_entries(raw: Any) -> dict[str, SnapPolicy]:
    snaps: dict[str, SnapPolicy] = {}
    for name, settings in raw.items():
        if not SNAP_NAME.match(str(name)):
            raise ValueError(
                f"snap {name!r}: name must be lowercase letters, digits and hyphens"
            )
        settings = settings or {}
        channel = str(settings.get("channel", "stable"))
        if channel not in SNAP_CHANNELS:
            known = ", ".join(sorted(SNAP_CHANNELS))
            raise ValueError(f"snap {name}: unknown channel {channel!r} (known: {known})")
        revision = settings.get("revision")
        if revision is not None and (not isinstance(revision, int) or revision < 1):
            raise ValueError(f"snap {name}: revision must be a positive integer")
        snaps[str(name)] = SnapPolicy(
            name=str(name),
            channel=channel,
            classic=bool(settings.get("classic", False)),
            revision=revision,
        )
    return snaps


def _repository_entries(
    raw: Any, package_repository: dict[str, str]
) -> dict[str, RepositoryPolicy]:
    repositories: dict[str, RepositoryPolicy] = {}
    for identifier, settings in raw.items():
        if not REPOSITORY_ID.match(str(identifier)):
            raise ValueError(
                f"repository {identifier!r}: id must be lowercase letters, digits and hyphens"
            )
        if not isinstance(settings, dict):
            raise ValueError(f"repository {identifier}: expected a mapping")
        key_sha256 = str(settings.get("key_sha256", ""))
        if not SHA256.match(key_sha256):
            raise ValueError(
                f"repository {identifier}: key_sha256 must be 64 lowercase hex characters"
            )
        for field_name in ("url", "key_url"):
            value = str(settings.get(field_name, ""))
            if not HTTPS_URL.match(value):
                raise ValueError(
                    f"repository {identifier}: {field_name} must be a plain https URL "
                    "without quotes, whitespace, `$` or backticks"
                )
        suite = str(settings.get("suite", ""))
        if not SUITE.match(suite):
            raise ValueError(
                f"repository {identifier}: suite {suite!r} must be a release suite name "
                "or {codename}"
            )
        components = tuple(str(component) for component in settings.get("components") or ())
        if not components:
            raise ValueError(f"repository {identifier}: at least one component is required")
        for component in components:
            if not COMPONENT.match(component):
                raise ValueError(
                    f"repository {identifier}: components must be lowercase names, "
                    f"got {component!r}"
                )
        repositories[str(identifier)] = RepositoryPolicy(
            id=str(identifier),
            url=str(settings["url"]),
            suite=suite,
            components=components,
            key_url=str(settings["key_url"]),
            key_sha256=key_sha256,
        )
    unknown = {name: repo for name, repo in package_repository.items() if repo not in repositories}
    if unknown:
        name, repo = sorted(unknown.items())[0]
        raise ValueError(f"package {name} points at unknown repository {repo}")
    return repositories


ENV_VAR = "GIRA_ISO_ALLOWLIST"

# Accounts the tool itself creates. They must match builder.render.BUILD_USER and
# verify.vm.VERIFY_USER (a test checks it). A spec user with one of these names would be
# deleted by the finalize script or clash with the verifier.
RESERVED_USERS = frozenset({"root", "packer", "gira-iso"})


def read_allowlist_text(path: str | None = None) -> tuple[str, str]:
    """Return (label, text) of the active allowlist.

    An organisation overrides the policy by pointing `GIRA_ISO_ALLOWLIST` at its own
    file; nothing in the package needs to change, and the build records which policy
    it used.
    """
    chosen = path or os.environ.get(ENV_VAR)
    if chosen:
        source = Path(chosen).expanduser()
        return str(source), source.read_text(encoding="utf-8")
    return f"packaged:{DEFAULT_ALLOWLIST}", resources.files("gira_iso.policy").joinpath(
        DEFAULT_ALLOWLIST
    ).read_text("utf-8")


def _local_additions_apply(path: str | None) -> bool:
    """An explicit allowlist file is a reviewed list: nothing local widens it."""
    return path is None and not strict_mode()


def allowlist_digest(path: str | None = None) -> tuple[str, str]:
    """Return (label, sha256) of the active allowlist, for build provenance.

    Local additions are part of the policy that decided, so they are part of the digest. With none
    the digest is the hash of the file alone, exactly as before additions existed.
    """
    label, text = read_allowlist_text(path)
    if _local_additions_apply(path):
        local = canonical_text()
        if local:
            text = f"{text}\n# local additions\n{local}\n"
    return label, hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_allowlist(path: str | None = None) -> Allowlist:
    _, text = read_allowlist_text(path)
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError("allowlist must be a mapping")
    data = {**data, "base_images": merge_into(data["base_images"])}
    loaded = Allowlist.from_mapping(data)
    if _local_additions_apply(path):
        loaded = dataclasses.replace(loaded, local_packages=local_packages_by_base())
    return loaded


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
        if not allowlist.allows_package(package, spec.base.version):
            violations.append(
                Violation(
                    code="package.not_allowed",
                    message=f"{package} is not in the package allowlist",
                    location=f"packages[{index}]",
                )
            )

    for index, snap in enumerate(spec.snaps):
        if snap not in allowlist.snaps:
            violations.append(
                Violation(
                    code="snap.not_allowed",
                    message=f"{snap} is not in the snap allowlist",
                    location=f"snaps[{index}]",
                )
            )

    for index, service in enumerate(spec.services.enable):
        if service in spec.snaps:
            # A snap starts and enables its own service. Asking systemd for a unit of the same
            # name would fail the build at the last step, with a message about a missing unit.
            violations.append(
                Violation(
                    code="service.snap_managed",
                    message=f"{service} is provided by the {service} snap and starts itself; "
                    "remove it from services.enable",
                    location=f"services.enable[{index}]",
                )
            )
        elif service not in allowlist.services:
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

    for index, user in enumerate(spec.users):
        if user.name in RESERVED_USERS:
            violations.append(
                Violation(
                    code="user.reserved",
                    message=f"{user.name} is reserved for the tool",
                    location=f"users[{index}].name",
                )
            )

    for index, entry in enumerate(spec.files):
        violations.extend(_check_file(entry, index, allowlist))

    return Verdict(violations=tuple(violations))
