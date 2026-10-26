"""Find out, before spending anything, whether this machine can build an image at all.

Without it the same problems (no QEMU, no permission on /dev/kvm, a full disk) surfaced minutes
into a Packer run, after the model had already been paid for, as a log tail. Each problem here
says what is wrong and the exact command that fixes it. Nothing is installed or changed.
"""

from __future__ import annotations

import grp
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from gira_iso.errors import EXIT_ENVIRONMENT, GiraError

KVM = Path("/dev/kvm")
GIB = 1024**3
SPACE_MARGIN = 2 * GIB

# binary -> the Debian package that provides it (None: installed by ./install.sh, not by apt)
TOOLS: dict[str, str | None] = {
    "qemu-system-x86_64": "qemu-system-x86",
    "qemu-img": "qemu-utils",
    "cloud-localds": "cloud-image-utils",
    "ssh-keygen": "openssh-client",
    "packer": None,
    "goss": None,
}


@dataclass(frozen=True)
class Problem:
    what: str
    fix: str


class EnvironmentProblem(GiraError):
    exit_code = EXIT_ENVIRONMENT

    def __init__(self, problems: list[Problem]) -> None:
        self.problems = problems
        lines = ["this machine cannot build images yet:"]
        for problem in problems:
            lines.append(f"  - {problem.what}")
            lines.append(f"    fix: {problem.fix}")
        super().__init__("\n".join(lines))


def check_tools(which: Callable[[str], str | None] | None = None) -> list[Problem]:
    find = which or shutil.which  # looked up at call time, so a test can replace it
    missing = [name for name in TOOLS if find(name) is None]
    problems = []
    apt = sorted({TOOLS[name] for name in missing if TOOLS[name]})
    if apt:
        names = ", ".join(name for name in missing if TOOLS[name])
        problems.append(
            Problem(f"missing tools: {names}", f"sudo apt-get install -y {' '.join(apt)}")
        )
    pinned = [name for name in missing if TOOLS[name] is None]
    if pinned:
        problems.append(
            Problem(
                f"missing tools: {', '.join(pinned)}",
                "run ./install.sh from the gira-iso repository (pinned versions, checksummed)",
            )
        )
    return problems


def check_kvm(kvm: Path = KVM, access: Callable[[Path, int], bool] | None = None) -> list[Problem]:
    can_use = access or os.access
    if not kvm.exists():
        return [
            Problem(
                f"{kvm} does not exist, so there is no hardware virtualisation",
                "enable virtualisation in the BIOS; on WSL2 use a recent kernel with nested "
                "virtualisation enabled",
            )
        ]
    if can_use(kvm, os.R_OK | os.W_OK):
        return []
    try:
        group = grp.getgrgid(kvm.stat().st_gid).gr_name
    except (KeyError, OSError):
        group = "kvm"
    return [
        Problem(
            f"you cannot read and write {kvm} (it belongs to the group {group})",
            f"sudo usermod -aG {group} $USER, then log out and back in "
            f"(until then: sudo chmod 666 {kvm}, which lasts until the next reboot)",
        )
    ]


def check_space(
    work_root: Path,
    disk_gb: int,
    usage: Callable[[Path], tuple[int, int, int]] | None = None,
) -> list[Problem]:
    """Free space for the disk the image will have, plus a margin. Conservative on purpose."""
    probe = work_root
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    free = (usage or shutil.disk_usage)(probe)[2]
    needed = disk_gb * GIB + SPACE_MARGIN
    if free >= needed:
        return []
    return [
        Problem(
            f"not enough disk in {probe}: {free / GIB:.1f} GB free, "
            f"about {needed / GIB:.0f} GB needed",
            "free some space there, or pick a smaller disk_size, or move the builds directory "
            "with GIRA_ISO_HOME",
        )
    ]


def check_environment(
    which: Callable[[str], str | None] | None = None,
    kvm: Path = KVM,
    access: Callable[[Path, int], bool] | None = None,
) -> list[Problem]:
    return check_tools(which) + check_kvm(kvm, access)


def ensure_environment() -> None:
    problems = check_environment()
    if problems:
        raise EnvironmentProblem(problems)


def ensure_space(work_root: Path, disk_gb: int) -> None:
    problems = check_space(work_root, disk_gb)
    if problems:
        raise EnvironmentProblem(problems)
