"""Boot a qcow2 on an overlay and talk to it over ssh.

The image is never modified: a qcow2 overlay on top of it plus a NoCloud seed ISO.
Cleanup is guaranteed by the context manager, so a failed verification never leaves
a QEMU process behind holding a port or a file lock.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

VERIFY_USER = "vmforge"
VERIFY_HOSTNAME = "vmforge-verify"


class VmError(RuntimeError):
    pass


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def image_virtual_size(image: Path) -> int:
    info = subprocess.run(
        ["qemu-img", "info", "--output=json", str(image)],
        check=True,
        capture_output=True,
        text=True,
    )
    import json

    return int(json.loads(info.stdout)["virtual-size"])


@dataclass
class VirtualMachine:
    """A throwaway guest booted from `image`."""

    image: Path
    workdir: Path
    memory: int = 2048
    cpus: int = 2
    boot_timeout: int = 240
    ssh_port: int = field(default_factory=free_port)

    overlay: Path = field(init=False)
    seed: Path = field(init=False)
    console: Path = field(init=False)
    pidfile: Path = field(init=False)
    key: Path = field(init=False)
    _process: subprocess.Popen[bytes] | None = field(init=False, default=None)

    # --- lifecycle --------------------------------------------------------

    def __enter__(self) -> VirtualMachine:
        self.workdir.mkdir(parents=True, exist_ok=True)
        self._require_tools()
        self._prepare_disk()
        self._prepare_seed()
        self._boot()
        self._wait_for_ssh()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()

    def _require_tools(self) -> None:
        for tool in ("qemu-system-x86_64", "qemu-img", "cloud-localds", "ssh", "ssh-keygen"):
            if shutil.which(tool) is None:
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
        self.key = self.workdir / "id_ed25519"
        if not self.key.exists():
            subprocess.run(
                [
                    "ssh-keygen",
                    "-t",
                    "ed25519",
                    "-N",
                    "",
                    "-C",
                    "vmforge-verify",
                    "-f",
                    str(self.key),
                ],
                check=True,
                capture_output=True,
            )
        public = self.key.with_suffix(".pub").read_text(encoding="utf-8").strip()

        user_data = self.workdir / "user-data"
        meta_data = self.workdir / "meta-data"
        user_data.write_text(
            "#cloud-config\n"
            f"hostname: {VERIFY_HOSTNAME}\n"
            "ssh_pwauth: false\n"
            "package_update: false\n"
            "package_upgrade: false\n"
            "users:\n"
            f"  - name: {VERIFY_USER}\n"
            "    shell: /bin/bash\n"
            "    sudo: ALL=(ALL) NOPASSWD:ALL\n"
            "    groups: [sudo]\n"
            "    ssh_authorized_keys:\n"
            f"      - {public}\n",
            encoding="utf-8",
        )
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
            # A real cloud exposes this channel, and the guest agent unit binds to
            # the virtio port device: without it the agent is enabled but inactive,
            # which says nothing about the image. The controller is required; a
            # virtserialport without a virtio-serial bus makes qemu refuse to start.
            "-device",
            "virtio-serial-pci",
            "-chardev",
            f"socket,path={self.workdir / 'qga.sock'},server=on,wait=off,id=qga0",
            "-device",
            "virtserialport,chardev=qga0,name=org.qemu.guest_agent.0",
            "-nic",
            f"user,model=virtio-net-pci,hostfwd=tcp:127.0.0.1:{self.ssh_port}-:22",
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

    def _wait_for_ssh(self) -> None:
        deadline = time.monotonic() + self.boot_timeout
        while time.monotonic() < deadline:
            if self._process is not None and self._process.poll() is not None:
                self._raise_boot_failure("qemu exited before ssh was reachable")
            result = self.ssh("true", check=False)
            if result.returncode == 0:
                return
            time.sleep(2)
        self._raise_boot_failure("ssh never became reachable")

    def _raise_boot_failure(self, reason: str) -> None:
        tail = ""
        if self.console.exists():
            lines = self.console.read_text(encoding="utf-8", errors="replace").splitlines()
            tail = "\n".join(lines[-30:])
        raise VmError(f"{reason}\n--- console ---\n{tail}")

    # --- interaction ------------------------------------------------------

    def _ssh_args(self) -> list[str]:
        return [
            "ssh",
            "-i",
            str(self.key),
            "-p",
            str(self.ssh_port),
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "LogLevel=ERROR",
            f"{VERIFY_USER}@127.0.0.1",
        ]

    def ssh(
        self, command: str, *, check: bool = True, timeout: int = 120
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*self._ssh_args(), command],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=check,
        )

    def upload(self, local: Path, remote: str) -> None:
        data = local.read_bytes()
        subprocess.run(
            [*self._ssh_args(), f"cat > {remote}"],
            input=data,
            check=True,
            capture_output=True,
            timeout=180,
        )

    def wait_for_cloud_init(self, timeout: int = 300) -> str:
        result = self.ssh("sudo cloud-init status --wait", check=False, timeout=timeout)
        return (result.stdout + result.stderr).strip()

    def stop(self) -> None:
        if self._process is None:
            return
        if self._process.poll() is None:
            subprocess.run(
                [*self._ssh_args(), "sudo poweroff"], check=False, capture_output=True, timeout=30
            )
            try:
                self._process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=10)
        self._process = None
