"""Run Packer as a subprocess.

Packer is an implementation detail behind this module: nothing else in gira-iso
shells out to it. Output is streamed and tee'd to a log file, so a failed build is
diagnosable after the fact instead of only in a terminal that is already gone.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

VERSION_RE = re.compile(r"Packer v(\d+\.\d+\.\d+)")


class PackerError(RuntimeError):
    def __init__(self, message: str, log_path: Path | None = None) -> None:
        super().__init__(message)
        self.log_path = log_path


def _binary() -> str:
    found = shutil.which("packer")
    if found is None:
        raise PackerError("packer not found in PATH")
    return found


def version() -> str:
    result = subprocess.run([_binary(), "version"], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise PackerError(f"'packer version' failed: {result.stderr.strip()}")
    match = VERSION_RE.search(result.stdout + result.stderr)
    return match.group(1) if match else "unknown"


def _run(
    args: list[str],
    log_path: Path,
    stream: bool,
    on_line: Callable[[str], None] | None = None,
) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, PACKER_NO_COLOR="1", CHECKPOINT_DISABLE="1")
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )
        assert process.stdout is not None
        for line in process.stdout:
            log.write(line)
            if stream:
                if on_line is not None:
                    on_line(line.rstrip("\n"))
                else:
                    sys.stderr.write(line)
        code = process.wait()

    if code != 0:
        tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]
        raise PackerError(f"packer exited with {code}; last lines:\n" + "\n".join(tail), log_path)


def init(template_dir: Path, log_path: Path) -> None:
    _run([_binary(), "init", str(template_dir)], log_path, stream=False)


def build(
    template_dir: Path,
    variables: dict[str, str],
    log_path: Path,
    on_line: Callable[[str], None] | None = None,
) -> None:
    args = [_binary(), "build"]
    for key, value in variables.items():
        args += ["-var", f"{key}={value}"]
    args.append(str(template_dir))
    _run(args, log_path, stream=True, on_line=on_line)
