"""The base image, resolved from the allowlist and never from the spec.

The spec can only name `ubuntu 24.04`. The URL and its checksum come from the
policy file, so a spec can never point a build at an arbitrary download.
"""

from __future__ import annotations

import hashlib
import shutil
import urllib.request
from pathlib import Path

from vmforge.policy.evaluate import Allowlist


class BaseImageError(RuntimeError):
    pass


def cache_dir() -> Path:
    return Path.home() / ".cache" / "vmforge" / "images"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_base_image(os_name: str, version: str, allowlist: Allowlist) -> tuple[Path, str]:
    """Return the local path and verified checksum of the pinned base image."""
    reference = allowlist.base_image(os_name, version)
    if reference is None:
        raise BaseImageError(f"no pinned image for {os_name} {version}")

    expected = reference["sha256"]
    url = reference["url"]
    target = cache_dir() / url.rsplit("/", 1)[-1]
    target.parent.mkdir(parents=True, exist_ok=True)

    if target.exists():
        actual = _sha256(target)
        if actual == expected:
            return target, expected
        # A cached file that does not match its pin is worse than no file at all.
        raise BaseImageError(
            f"cached base image {target} has checksum {actual}, expected {expected}; "
            "delete it and retry"
        )

    partial = target.with_suffix(target.suffix + ".part")
    with urllib.request.urlopen(url) as response, partial.open("wb") as handle:
        shutil.copyfileobj(response, handle)

    actual = _sha256(partial)
    if actual != expected:
        partial.unlink(missing_ok=True)
        raise BaseImageError(f"downloaded base image has checksum {actual}, expected {expected}")

    partial.rename(target)
    return target, expected
