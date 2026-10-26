"""The manifest is the provenance record of a build.

Without it, an image on disk is an anonymous blob: you cannot say which spec made
it, which base it came from, or which Packer built it. Every field here exists to
answer one of those questions without rerunning anything.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from vmforge.spec.models import Spec

MANIFEST_NAME = "manifest.json"
MANIFEST_VERSION = "1"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class BaseRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    os: str
    version: str
    url: str
    sha256: str


class PolicyRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str
    sha256: str


class BuilderInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    packer_version: str
    plugin_version: str
    template_sha256: str


class Artifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file: str
    sha256: str
    size_bytes: int
    virtual_size_bytes: int


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manifest_version: Literal["1"] = MANIFEST_VERSION
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    spec_sha256: str
    spec: dict[str, Any]
    base: BaseRef
    policy: PolicyRef
    builder: BuilderInfo
    packages: list[str]
    services: list[str]
    artifact: Artifact

    def write(self, directory: Path) -> Path:
        path = directory / MANIFEST_NAME
        path.write_text(self.model_dump_json(indent=2) + "\n", encoding="utf-8")
        return path


def build_manifest(
    *,
    spec: Spec,
    spec_sha256: str,
    base: BaseRef,
    policy: PolicyRef,
    builder: BuilderInfo,
    packages: list[str],
    services: list[str],
    artifact_path: Path,
    virtual_size_bytes: int,
) -> Manifest:
    return Manifest(
        spec_sha256=spec_sha256,
        spec=spec.model_dump(mode="json"),
        base=base,
        policy=policy,
        builder=builder,
        packages=packages,
        services=services,
        artifact=Artifact(
            file=artifact_path.name,
            sha256=file_sha256(artifact_path),
            size_bytes=artifact_path.stat().st_size,
            virtual_size_bytes=virtual_size_bytes,
        ),
    )
