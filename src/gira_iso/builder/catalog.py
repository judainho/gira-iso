"""Which images exist: find builds, read their manifests, resolve a user's reference.

The command line, the menu and the verifier each had their own copy of this. A build directory
without a valid manifest is not an image; it is invisible here on purpose.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from gira_iso.builder.manifest import MANIFEST_NAME, Manifest
from gira_iso.errors import EXIT_INVALID, GiraError


class CatalogError(GiraError):
    """The reference points at nothing, at several things, or at an unreadable manifest."""

    exit_code = EXIT_INVALID


def display_name(manifest: Manifest, build_dir: Path) -> str:
    return manifest.name or build_dir.name


def load_manifest(path: Path) -> tuple[Manifest, Path]:
    """Accept either a build directory or the manifest file itself."""
    manifest_path = path / MANIFEST_NAME if path.is_dir() else path
    if not manifest_path.is_file():
        raise CatalogError(f"no manifest at {manifest_path}")
    try:
        manifest = Manifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    except (ValidationError, OSError) as exc:
        raise CatalogError(f"manifest at {manifest_path} is not valid: {exc}") from exc
    return manifest, manifest_path


def scan_builds(work_root: Path) -> list[tuple[Manifest, Path]]:
    """Every build under `work_root` with a valid manifest, newest first."""
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


# A build directory the tool itself created has at least one of these, or the failure marker
# (builder/build.py). Anything else under the builds directory is somebody else's and is left alone.
_BUILD_SUBDIRS = ("packer", "provision", "seed", "logs")
FAILED_MARKER = "FAILED"


def find_incomplete(work_root: Path) -> list[Path]:
    """Build directories with no manifest: failed, cancelled or killed before finishing."""
    if not work_root.is_dir():
        return []
    found = []
    for child in sorted(work_root.iterdir()):
        if child.is_symlink() or not child.is_dir() or (child / MANIFEST_NAME).exists():
            continue
        if (child / FAILED_MARKER).exists() or any(
            (child / sub).is_dir() for sub in _BUILD_SUBDIRS
        ):
            found.append(child)
    return found


def directory_size(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def resolve_image(reference: str, work_root: Path) -> tuple[Manifest, Path]:
    """A path, an image name or a build id prefix, in that order."""
    candidate = Path(reference).expanduser()
    if candidate.exists():
        return load_manifest(candidate)

    matches: dict[Path, tuple[Manifest, Path]] = {}
    for manifest, manifest_path in scan_builds(work_root):
        build_dir = manifest_path.parent
        if display_name(manifest, build_dir) == reference or build_dir.name.startswith(reference):
            matches[build_dir] = (manifest, manifest_path)

    if not matches:
        raise CatalogError(f"no image matches {reference!r} under {work_root}")
    if len(matches) > 1:
        names = ", ".join(sorted(path.name for path in matches))
        raise CatalogError(f"{reference!r} is ambiguous: {names}")
    return next(iter(matches.values()))
