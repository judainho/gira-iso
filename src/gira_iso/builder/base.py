"""The base image, resolved from the allowlist and never from the spec.

The spec can only name `ubuntu 24.04`. The URL and its checksum come from the
policy file, so a spec can never point a build at an arbitrary download.
"""

from __future__ import annotations

import hashlib
import urllib.request
from collections.abc import Callable
from pathlib import Path
from urllib.parse import unquote, urlparse

from gira_iso.errors import EXIT_ENVIRONMENT, GiraError
from gira_iso.policy.evaluate import Allowlist

DOWNLOAD_TIMEOUT = 60  # seconds per socket operation


class BaseImageError(GiraError):
    """No trustworthy base could be obtained: network, checksum or a missing pin."""

    exit_code = EXIT_ENVIRONMENT


def cache_dir() -> Path:
    return Path.home() / ".cache" / "gira-iso" / "images"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_base_image(
    os_name: str,
    version: str,
    allowlist: Allowlist,
    on_progress: Callable[[int, int], None] | None = None,
) -> tuple[Path, str]:
    """Return the local path and verified checksum of the pinned base image."""
    reference = allowlist.base_image(os_name, version)
    if reference is None:
        raise BaseImageError(f"no pinned image for {os_name} {version}")

    expected = reference["sha256"]
    url = reference["url"]
    if url.startswith("file://"):
        # A registered base lives on disk already; verify it instead of copying it.
        local = Path(unquote(urlparse(url).path))
        actual = _sha256(local) if local.is_file() else ""
        if actual != expected:
            raise BaseImageError(f"registered base {local} does not match its checksum")
        return local, expected

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
    try:
        # The timeout applies to each socket operation, so a stalled connection fails
        # instead of hanging; a slow but alive download is unaffected.
        with (
            urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT) as response,
            partial.open("wb") as handle,
        ):
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            while chunk := response.read(1024 * 1024):
                handle.write(chunk)
                done += len(chunk)
                if on_progress is not None:
                    on_progress(done, total)

        actual = _sha256(partial)
        if actual != expected:
            raise BaseImageError(
                f"downloaded base image has checksum {actual}, expected {expected}"
            )
        partial.rename(target)
    except OSError as exc:
        raise BaseImageError(f"could not download {url}: {exc}") from exc
    finally:
        # Mismatch, network error, Ctrl+C: whatever stopped the download, no partial file stays.
        partial.unlink(missing_ok=True)
    return target, expected
