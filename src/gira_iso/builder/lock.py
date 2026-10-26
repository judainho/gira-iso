"""One build at a time per builds directory.

Two builds share the hypervisor, the disk and the Packer plugin cache; started together they
slow each other down and, with the same spec in the same second, used to collide on the
directory name. A lock file serialises them. The kernel drops the lock when the process dies,
so a crash never leaves it stuck.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
from collections.abc import Iterator
from pathlib import Path

from gira_iso.errors import EXIT_ENVIRONMENT, GiraError

LOCK_NAME = ".lock"


class BuildBusy(GiraError):
    exit_code = EXIT_ENVIRONMENT


@contextlib.contextmanager
def build_lock(work_root: Path) -> Iterator[None]:
    """Hold the builds directory for the duration of the block, or raise BuildBusy."""
    work_root.mkdir(parents=True, exist_ok=True)
    path = work_root / LOCK_NAME
    handle = path.open("a+")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.seek(0)
            holder = handle.read().strip()
            who = f" (process {holder})" if holder else ""
            raise BuildBusy(
                f"another build is running in {work_root}{who}; wait for it to finish"
            ) from None
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        handle.close()
