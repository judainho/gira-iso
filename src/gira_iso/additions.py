"""Packages a person added to the allowlist from this machine, kept apart from the packaged list.

The packaged `policy/allowlist.yaml` is reviewed; an addition here is a decision one person took
at one keyboard. So it is stored with its provenance (which Ubuntu, which component and version it
was checked against, when), recorded in an append-only log, listed in every manifest that uses it
and never loaded in strict mode, which an organisation that ships its own allowlist gets by default.

Nothing in this module talks to the model or the network: it only stores what a person approved.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml

from gira_iso.bases import home
from gira_iso.errors import EXIT_REJECTED, GiraError

STRICT_ENV = "GIRA_ISO_STRICT"
ALLOW_LOCAL_ENV = "GIRA_ISO_ALLOW_LOCAL"
ORG_ALLOWLIST_ENV = "GIRA_ISO_ALLOWLIST"  # same variable as policy.evaluate.ENV_VAR (a test checks)
_TRUE = {"1", "true", "yes", "on"}


class AdditionsDisabled(GiraError):
    """Strict mode: the allowlist cannot be extended from this machine."""

    exit_code = EXIT_REJECTED


@dataclass(frozen=True)
class Addition:
    package: str
    base_version: str  # the Ubuntu release it was checked against, e.g. "24.04"
    component: str  # "main" or "universe"
    version: str  # the package version seen in the archive when it was approved
    added_at: str
    request_sha256: str | None = None  # hash of the request that motivated it (never the text)
    note: str | None = None  # a description written by the model; advice, not evidence


def additions_path() -> Path:
    return home() / "allowlist-additions.yaml"


def log_path() -> Path:
    return home() / "allowlist-additions.log"


def strict_mode() -> bool:
    """True when local additions must be ignored and refused.

    Explicit with GIRA_ISO_STRICT=1. Also implied by an organisation's own allowlist
    (GIRA_ISO_ALLOWLIST): a list reviewed elsewhere must not be widened from a laptop, unless
    GIRA_ISO_ALLOW_LOCAL=1 says otherwise.
    """
    if os.environ.get(STRICT_ENV, "").lower() in _TRUE:
        return True
    own_list = bool(os.environ.get(ORG_ALLOWLIST_ENV))
    allowed_anyway = os.environ.get(ALLOW_LOCAL_ENV, "").lower() in _TRUE
    return own_list and not allowed_anyway


def _read() -> list[Addition]:
    try:
        raw = yaml.safe_load(additions_path().read_text(encoding="utf-8")) or []
    except (OSError, yaml.YAMLError):
        return []
    found = []
    for entry in raw if isinstance(raw, list) else []:
        try:
            found.append(Addition(**entry))
        except TypeError:
            continue  # a damaged entry is skipped, never trusted
    return found


def _write(entries: list[Addition]) -> None:
    path = additions_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump([asdict(e) for e in entries], sort_keys=False), encoding="utf-8")


def _log(event: str, addition: Addition) -> None:
    line = {
        "at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "event": event,
        "package": addition.package,
        "base_version": addition.base_version,
        "component": addition.component,
        "version": addition.version,
        "request_sha256": addition.request_sha256,
    }
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line, sort_keys=True) + "\n")


def list_additions(*, include_inactive: bool = False) -> list[Addition]:
    """The additions that count. In strict mode that is none, unless asked to show them anyway."""
    if strict_mode() and not include_inactive:
        return []
    return _read()


def _refuse_in_strict_mode() -> None:
    if strict_mode():
        raise AdditionsDisabled(
            "strict mode is on: the allowlist cannot be extended from this machine "
            f"(unset {STRICT_ENV}; or set {ALLOW_LOCAL_ENV}=1 if you use your own "
            f"{ORG_ALLOWLIST_ENV})"
        )


def add_addition(addition: Addition) -> Addition:
    """Store one approval. Adding the same package for the same release again changes nothing."""
    _refuse_in_strict_mode()
    entries = _read()
    for existing in entries:
        if (existing.package, existing.base_version) == (addition.package, addition.base_version):
            return existing
    entries.append(addition)
    _write(entries)
    _log("add", addition)
    return addition


def remove_addition(package: str, base_version: str | None = None) -> list[Addition]:
    """Remove a package (for one release, or for all). Returns what was removed."""
    _refuse_in_strict_mode()
    entries = _read()
    removed = [
        e for e in entries if e.package == package and base_version in (None, e.base_version)
    ]
    if removed:
        _write([e for e in entries if e not in removed])
        for entry in removed:
            _log("remove", entry)
    return removed


def local_packages_by_base() -> dict[str, frozenset[str]]:
    grouped: dict[str, set[str]] = {}
    for entry in list_additions():
        grouped.setdefault(entry.base_version, set()).add(entry.package)
    return {version: frozenset(names) for version, names in grouped.items()}


def canonical_text() -> str:
    """What the policy decision depends on, in a stable order, for the policy digest.

    Only (release, package): timestamps and notes do not change what is allowed, so editing them
    must not change the digest of the policy that built an image.
    """
    pairs = sorted(f"{e.base_version}:{e.package}" for e in list_additions())
    return "\n".join(pairs)
