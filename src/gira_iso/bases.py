"""Base images the user registered locally, kept apart from the packaged policy.

The pinned bases in `policy/allowlist.yaml` stay the reviewed default. A base added
here is pinned by the sha256 computed at registration time, so the build still
verifies it byte for byte, but the trust decision is the user's, not the policy's.
Only qcow2 cloud images are accepted: an installer ISO cannot be used as a base.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml

VERSION_RE = re.compile(r"^[0-9]{2}\.[0-9]{2}$")
QCOW2_MAGIC = b"QFI\xfb"
ISO_SIGNATURE_OFFSET = 0x8001
ISO_SIGNATURE = b"CD001"
INDEX_NAME = "index.yaml"


class UserBaseError(RuntimeError):
    pass


@dataclass(frozen=True)
class UserBase:
    os: str
    version: str
    sha256: str
    path: Path
    name: str
    added: str

    @property
    def url(self) -> str:
        return self.path.resolve().as_uri()


def home() -> Path:
    """Where gira-iso keeps its own data; overridable so tests never touch the real one."""
    chosen = os.environ.get("GIRA_ISO_HOME")
    return Path(chosen).expanduser() if chosen else Path.home() / ".local" / "share" / "gira-iso"


def builds_dir() -> Path:
    """Default place for builds, the same for the menu and the scripted commands."""
    return home() / "builds"


def bases_dir() -> Path:
    return home() / "bases"


def _index_path() -> Path:
    return bases_dir() / INDEX_NAME


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def list_user_bases() -> list[UserBase]:
    index = _index_path()
    if not index.is_file():
        return []
    entries = yaml.safe_load(index.read_text(encoding="utf-8")) or []
    bases = []
    for entry in entries:
        path = bases_dir() / entry["file"]
        if path.is_file():
            bases.append(
                UserBase(
                    os=entry["os"],
                    version=entry["version"],
                    sha256=entry["sha256"],
                    path=path,
                    name=entry["name"],
                    added=entry["added"],
                )
            )
    return bases


def _check_format(source: Path) -> None:
    with source.open("rb") as handle:
        head = handle.read(4)
        handle.seek(ISO_SIGNATURE_OFFSET)
        iso = handle.read(len(ISO_SIGNATURE))
    if iso == ISO_SIGNATURE:
        raise UserBaseError(
            f"{source.name} is an installer ISO. A base must be a qcow2 cloud image "
            "(for Ubuntu: the *-server-cloudimg-amd64.img file)."
        )
    if head != QCOW2_MAGIC:
        raise UserBaseError(f"{source.name} is not a qcow2 image")


def register_base(
    source: Path,
    version: str,
    *,
    builtin: dict[str, dict[str, dict[str, str]]] | None = None,
    name: str | None = None,
) -> UserBase:
    """Copy a local Ubuntu cloud image into the store and pin it by checksum."""
    source = source.expanduser()
    if not source.is_file():
        raise UserBaseError(f"no such file: {source}")
    if not VERSION_RE.match(version):
        raise UserBaseError(f"invalid version {version!r}: expected something like 24.04")
    _check_format(source)

    taken = set((builtin or {}).get("ubuntu", {})) | {base.version for base in list_user_bases()}
    if version in taken:
        raise UserBaseError(f"ubuntu {version} is already registered")

    digest = sha256_of(source)
    target_dir = bases_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    stored = f"{digest[:12]}-{source.name}"
    shutil.copy2(source, target_dir / stored)

    entry = {
        "os": "ubuntu",
        "version": version,
        "sha256": digest,
        "file": stored,
        "name": name or source.name,
        "added": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    index = _index_path()
    entries = yaml.safe_load(index.read_text(encoding="utf-8")) or [] if index.is_file() else []
    entries.append(entry)
    index.write_text(yaml.safe_dump(entries, sort_keys=False), encoding="utf-8")
    return UserBase(
        os="ubuntu",
        version=version,
        sha256=digest,
        path=target_dir / stored,
        name=entry["name"],
        added=entry["added"],
    )


def merge_into(base_images: dict[str, dict[str, dict[str, str]]]) -> dict:
    """Return the packaged bases plus the registered ones; packaged entries win."""
    merged = {name: dict(versions) for name, versions in base_images.items()}
    for base in list_user_bases():
        merged.setdefault(base.os, {}).setdefault(
            base.version, {"url": base.url, "sha256": base.sha256}
        )
    return merged
