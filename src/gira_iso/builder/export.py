"""Find built images and copy them, checksum-verified, to a destination directory."""

from __future__ import annotations

import shutil
from pathlib import Path

from pydantic import ValidationError

from gira_iso.builder.manifest import MANIFEST_NAME, Manifest, file_sha256


class ExportError(RuntimeError):
    """`invalid` is bad input (missing file); otherwise the image was refused."""

    def __init__(self, message: str, *, invalid: bool = False) -> None:
        super().__init__(message)
        self.invalid = invalid


def scan_builds(work_root: Path) -> list[tuple[Manifest, Path]]:
    if not work_root.is_dir():
        return []
    found: list[tuple[Manifest, Path]] = []
    for manifest_path in sorted(work_root.glob(f"*/{MANIFEST_NAME}")):
        try:
            manifest = Manifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
        except (ValidationError, OSError):
            continue
        found.append((manifest, manifest_path))
    found.sort(key=lambda item: item[0].created_at, reverse=True)
    return found


def export_image(
    manifest: Manifest,
    manifest_path: Path,
    destination_dir: Path,
    label: str,
    *,
    force: bool = False,
) -> tuple[Path, Path]:
    """Copy image and manifest; returns their new paths."""
    source = manifest_path.parent / "image" / manifest.artifact.file
    if not source.is_file():
        raise ExportError(f"manifest points at {source}, which does not exist", invalid=True)
    if file_sha256(source) != manifest.artifact.sha256:
        raise ExportError("artifact does not match its manifest (checksum mismatch)")

    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / f"{label}{source.suffix}"
    manifest_destination = destination_dir / f"{label}.manifest.json"
    if (destination.exists() or manifest_destination.exists()) and not force:
        raise ExportError(f"{destination} already exists (use --force)")

    shutil.copy2(source, destination)
    if file_sha256(destination) != manifest.artifact.sha256:
        destination.unlink(missing_ok=True)
        raise ExportError("copied image does not match the manifest checksum")
    shutil.copy2(manifest_path, manifest_destination)
    return destination, manifest_destination
