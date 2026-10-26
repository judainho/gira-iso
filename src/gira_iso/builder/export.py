"""Find built images and copy them, checksum-verified, to a destination directory."""

from __future__ import annotations

import shutil
from pathlib import Path

from gira_iso.builder.catalog import directory_size, find_incomplete
from gira_iso.builder.lock import build_lock
from gira_iso.builder.manifest import MANIFEST_NAME, Manifest, file_sha256
from gira_iso.errors import EXIT_INVALID, EXIT_REJECTED, GiraError


class ExportError(GiraError):
    """`invalid` is bad input (missing file); otherwise the image was refused."""

    def __init__(self, message: str, *, invalid: bool = False) -> None:
        super().__init__(message)
        self.invalid = invalid
        self.exit_code = EXIT_INVALID if invalid else EXIT_REJECTED


class ExportExists(ExportError):
    """The destination already holds an image with this name."""


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
        raise ExportExists(f"{destination} already exists (use --force)")

    shutil.copy2(source, destination)
    if file_sha256(destination) != manifest.artifact.sha256:
        destination.unlink(missing_ok=True)
        raise ExportError("copied image does not match the manifest checksum")
    shutil.copy2(manifest_path, manifest_destination)
    return destination, manifest_destination


def delete_build(manifest_path: Path, work_root: Path) -> Path:
    """Remove one build directory. Refuses anything that is not a direct child of work_root."""
    build_dir = manifest_path.parent
    root = work_root.resolve()
    if build_dir.is_symlink() or build_dir.resolve().parent != root:
        raise ExportError(f"{build_dir} is not a build under {root}", invalid=True)
    if not (build_dir / MANIFEST_NAME).is_file():
        raise ExportError(f"{build_dir} has no manifest", invalid=True)
    resolved = build_dir.resolve()
    shutil.rmtree(resolved)
    return resolved


def remove_incomplete(work_root: Path) -> list[tuple[Path, int]]:
    """Delete every incomplete build under `work_root`; returns (directory, bytes freed).

    A build in progress looks incomplete too, so this takes the build lock first and raises
    BuildBusy instead of deleting a directory somebody is writing to.
    """
    removed = []
    with build_lock(work_root):
        for directory in find_incomplete(work_root):
            size = directory_size(directory)
            shutil.rmtree(directory)
            removed.append((directory, size))
    return removed


def describe(manifest: Manifest, build_dir: Path) -> list[tuple[str, str]]:
    """Label/value rows for a human-readable view of one build."""
    return [
        ("Nombre", manifest.name or build_dir.name),
        ("Creada (UTC)", manifest.created_at.strftime("%Y-%m-%d %H:%M:%S")),
        ("Base", f"{manifest.base.os} {manifest.base.version}"),
        ("Base sha256", manifest.base.sha256),
        ("Paquetes", ", ".join(manifest.packages) or "-"),
        ("Servicios", ", ".join(manifest.services) or "-"),
        (
            "Adiciones locales",
            ", ".join(
                f"{a.package} ({a.component} {a.version}, {a.added_at[:10]})"
                for a in manifest.policy.local_additions
            )
            or "-",
        ),
        ("Spec sha256", manifest.spec_sha256),
        ("Política", f"{manifest.policy.source}  ({manifest.policy.sha256[:16]}…)"),
        (
            "Constructor",
            f"packer {manifest.builder.packer_version}, "
            f"qemu plugin {manifest.builder.plugin_version}",
        ),
        ("Imagen", manifest.artifact.file),
        ("Imagen sha256", manifest.artifact.sha256),
        (
            "Tamaño",
            f"{manifest.artifact.size_bytes / 1024**2:.0f} MB en disco, "
            f"{manifest.artifact.virtual_size_bytes / 1024**3:.1f} GB virtual",
        ),
        ("Directorio", str(build_dir)),
    ]
