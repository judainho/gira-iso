"""Boot a qcow2 on an overlay and talk to it over the QEMU guest agent.

The image is never modified: a qcow2 overlay on top of it plus a NoCloud seed ISO. Cleanup is
guaranteed by the context manager, so a failed verification never leaves a QEMU process behind
holding a port or a file lock.

The transport is the guest agent on the virtio-serial bus, not SSH. The agent runs as root inside
the guest, so the seed declares no user and no sudo: a verification that created its own
`NOPASSWD` account could never claim the image has none.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from shutil import which

from gira_iso.errors import EXIT_ENVIRONMENT, GiraError
from gira_iso.verify.qga import GuestAgent, GuestAgentError

VERIFY_HOSTNAME = "gira-iso-verify"
AGENT_PORT = "org.qemu.guest_agent.0"


class VmError(GiraError):
    exit_code = EXIT_ENVIRONMENT


def seed_user_data() -> str:
    """The verification seed: a hostname and nothing else.

    `users: []` is explicit on purpose. Omitting it makes cloud-init create the distribution's
    default account, which on Ubuntu carries passwordless sudo, and then the image would be flagged
    for an account our own seed invented.
    """
    return (
        "#cloud-config\n"
        f"hostname: {VERIFY_HOSTNAME}\n"
        "ssh_pwauth: false\n"
        "package_update: false\n"
        "package_upgrade: false\n"
        "users: []\n"
    )


def image_virtual_size(image: Path) -> int:
    info = subprocess.run(
        ["qemu-img", "info", "--output=json", str(image)],
        check=True,
        capture_output=True,
        text=True,
    )
    return int(json.loads(info.stdout)["virtual-size"])


@dataclass
class VirtualMachine:
    """A throwaway guest booted from `image`."""

    image: Path
    workdir: Path
    memory: int = 2048
    cpus: int = 2
    boot_timeout: int = 240

    overlay: Path = field(init=False)
    seed: Path = field(init=False)
    console: Path = field(init=False)
    pidfile: Path = field(init=False)
    agent: GuestAgent = field(init=False)
    _process: subprocess.Popen[bytes] | None = field(init=False, default=None)

    # --- lifecycle --------------------------------------------------------

    def __enter__(self) -> VirtualMachine:
        self.workdir.mkdir(parents=True, exist_ok=True)
        # If __enter__ raises, Python never calls __exit__: stop the guest here, or a Ctrl+C
        # while it boots leaves a QEMU process running with the image open.
        try:
            self._require_tools()
            self._prepare_disk()
            self._prepare_seed()
            self._boot()
            self._wait_for_agent()
        except BaseException:
            self.stop()
            raise
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()

    def _require_tools(self) -> None:
        for tool in ("qemu-system-x86_64", "qemu-img", "cloud-localds"):
            if not which(tool):
                raise VmError(f"missing '{tool}'")

    def _prepare_disk(self) -> None:
        virtual = image_virtual_size(self.image)
        self.overlay = self.workdir / "overlay.qcow2"
        # An overlay smaller than its backing file truncates the guest disk and the
        # kernel drops to initramfs, with a misleading "label does not exist" error.
        subprocess.run(
            [
                "qemu-img",
                "create",
                "-q",
                "-f",
                "qcow2",
                "-F",
                "qcow2",
                "-b",
                str(self.image.resolve()),
                str(self.overlay),
                str(virtual),
            ],
            check=True,
        )

    def _prepare_seed(self) -> None:
        user_data = self.workdir / "user-data"
        meta_data = self.workdir / "meta-data"
        user_data.write_text(seed_user_data(), encoding="utf-8")
        meta_data.write_text(
            f"instance-id: {VERIFY_HOSTNAME}-001\nlocal-hostname: {VERIFY_HOSTNAME}\n",
            encoding="utf-8",
        )
        self.seed = self.workdir / "seed.iso"
        subprocess.run(
            ["cloud-localds", str(self.seed), str(user_data), str(meta_data)],
            check=True,
            capture_output=True,
        )

    def _boot(self) -> None:
        self.console = self.workdir / "console.log"
        self.pidfile = self.workdir / "qemu.pid"
        self.console.write_text("", encoding="utf-8")
        socket_path = self.workdir / "qga.sock"
        self.agent = GuestAgent(socket_path)
        command = [
            "qemu-system-x86_64",
            "-name",
            VERIFY_HOSTNAME,
            "-machine",
            "q35,accel=kvm",
            "-cpu",
            "host",
            "-smp",
            str(self.cpus),
            "-m",
            str(self.memory),
            "-drive",
            f"file={self.overlay},if=virtio,format=qcow2",
            "-drive",
            f"file={self.seed},if=virtio,format=raw,readonly=on",
            # The agent unit binds to this port device: without it the agent is enabled but
            # inactive, which says nothing about the image. The controller is required; a
            # virtserialport without a virtio-serial bus makes qemu refuse to start.
            "-device",
            "virtio-serial-pci",
            "-chardev",
            f"socket,path={socket_path},server=on,wait=off,id=qga0",
            "-device",
            f"virtserialport,chardev=qga0,name={AGENT_PORT}",
            # A real cloud gives the guest a NIC; keep one so cloud-init's network stage behaves
            # as it would there. Nothing is forwarded: the agent is the only channel.
            "-nic",
            "user,model=virtio-net-pci",
            "-display",
            "none",
            "-serial",
            f"file:{self.console}",
            "-pidfile",
            str(self.pidfile),
        ]
        self._process = subprocess.Popen(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

    def _wait_for_agent(self) -> None:
        deadline = time.monotonic() + self.boot_timeout
        while time.monotonic() < deadline:
            if self._process is not None and self._process.poll() is not None:
                self._raise_boot_failure("qemu exited before the guest agent answered")
            if self.agent.ping():
                return
            time.sleep(2)
        self._raise_boot_failure("the guest agent never answered")

    def _raise_boot_failure(self, reason: str) -> None:
        tail = ""
        if self.console.exists():
            lines = self.console.read_text(encoding="utf-8", errors="replace").splitlines()
            tail = "\n".join(lines[-30:])
        raise VmError(f"{reason}\n--- console ---\n{tail}")

    # --- interaction ------------------------------------------------------

    def run(self, command: str, *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
        try:
            return self.agent.run(command, timeout=timeout)
        except GuestAgentError as exc:
            raise VmError(str(exc)) from None

    def upload(self, local: Path, remote: str) -> None:
        try:
            self.agent.upload(local, remote)
        except GuestAgentError as exc:
            raise VmError(str(exc)) from None

    def wait_for_cloud_init(self, timeout: int = 300) -> str:
        result = self.run("cloud-init status --wait", timeout=timeout)
        return (result.stdout + result.stderr).strip()

    def stop(self) -> None:
        """Power the guest off, however the run ended."""
        if self._process is not None and self._process.poll() is None:
            agent = getattr(self, "agent", None)
            if agent is not None:
                agent.shutdown()
            try:
                self._process.wait(timeout=45)
            except subprocess.TimeoutExpired:
                self._process.kill()
                # After SIGKILL there is nothing else to try; a stuck wait here must not mask
                # the failure that triggered the stop.
                with contextlib.suppress(subprocess.TimeoutExpired):
                    self._process.wait(timeout=10)
        # The socket is a dead file once qemu is gone, and a stale one next to the evidence would
        # look like a guest still listening.
        socket_path = getattr(getattr(self, "agent", None), "socket_path", None)
        if socket_path is not None:
            with contextlib.suppress(OSError):
                Path(socket_path).unlink(missing_ok=True)
        self._process = None
