"""The QEMU guest agent protocol over the virtio-serial socket.

The agent runs as root inside the guest, so a command needs no user, no SSH key and no sudo. That
is the point: a verification seed that creates a user with `NOPASSWD` can never claim the image has
no such account.

One connection per request: the channel accepts them, and a half-open socket after a failed call
cannot poison the next one.
"""

from __future__ import annotations

import base64
import contextlib
import json
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

from gira_iso.errors import EXIT_ENVIRONMENT, GiraError

CHUNK_BYTES = 768 * 1024


class GuestAgentError(GiraError):
    exit_code = EXIT_ENVIRONMENT


class GuestAgent:
    """A thin, synchronous client for the four commands verification needs."""

    def __init__(self, socket_path: Path, *, timeout: float = 120.0) -> None:
        self.socket_path = socket_path
        self.timeout = timeout

    # --- transport --------------------------------------------------------

    def _exchange(self, execute: str, arguments: dict[str, Any] | None = None) -> Any:
        payload: dict[str, Any] = {"execute": execute}
        if arguments is not None:
            payload["arguments"] = arguments
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(self.timeout)
                connection.connect(str(self.socket_path))
                connection.sendall(json.dumps(payload).encode("utf-8") + b"\n")
                raw = b""
                while not raw.endswith(b"\n"):
                    block = connection.recv(65536)
                    if not block:
                        break
                    raw += block
        except OSError as exc:
            raise GuestAgentError(f"cannot talk to the guest agent: {exc}") from None
        if not raw.strip():
            raise GuestAgentError(f"the guest agent closed the connection during {execute}")
        try:
            message = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            raise GuestAgentError(f"the guest agent sent unreadable {execute} output") from None
        if not isinstance(message, dict):
            # Everything the agent sends is an object. Anything else is not a reply we understand,
            # and it must not escape as an AttributeError from the caller's cleanup path.
            raise GuestAgentError(f"the guest agent sent an unexpected {execute} reply")
        if "error" in message:
            error = message["error"] or {}
            raise GuestAgentError(
                f"guest agent refused {execute}: {error.get('desc', 'no description')}"
            )
        # `return` is not always an object: guest-file-open answers with a bare handle, and 0
        # is a valid handle, so it must not be coerced to "missing".
        value = message.get("return")
        return {} if value is None else value

    # --- commands ---------------------------------------------------------

    def ping(self) -> bool:
        """True when the agent answers; used to know the guest is up."""
        try:
            self._exchange("guest-ping")
        except GuestAgentError:
            return False
        return True

    def run(self, command: str, *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
        started = self._exchange(
            "guest-exec",
            {"path": "/bin/sh", "arg": ["-c", command], "capture-output": True},
        )
        pid = started.get("pid")
        if pid is None:
            raise GuestAgentError(f"the guest agent refused to run: {command}")

        deadline = time.monotonic() + timeout
        while True:
            status = self._exchange("guest-exec-status", {"pid": pid})
            if status.get("exited"):
                break
            if time.monotonic() > deadline:
                raise GuestAgentError(f"command timed out after {timeout}s: {command}")
            time.sleep(0.1)

        return subprocess.CompletedProcess(
            args=command,
            returncode=int(status.get("exitcode", 0)),
            stdout=_decoded(status.get("out-data")),
            stderr=_decoded(status.get("err-data")),
        )

    def upload(self, local: Path, remote: str) -> None:
        handle = self._exchange("guest-file-open", {"path": remote, "mode": "wb"})
        if not isinstance(handle, int):
            raise GuestAgentError(f"the guest agent refused to open {remote}")
        try:
            with local.open("rb") as source:
                while True:
                    block = source.read(CHUNK_BYTES)
                    if not block:
                        break
                    written = self._exchange(
                        "guest-file-write",
                        {
                            "handle": handle,
                            "buf-b64": base64.b64encode(block).decode("ascii"),
                        },
                    )
                    count = written.get("count") if isinstance(written, dict) else None
                    if count is not None and count != len(block):
                        raise GuestAgentError(
                            f"the guest agent wrote {count} of {len(block)} bytes to {remote}"
                        )
        finally:
            # Closing is best effort and must not mask the original failure.
            with contextlib.suppress(GuestAgentError):
                self._exchange("guest-file-close", {"handle": handle})

    def shutdown(self) -> None:
        """Ask systemd to power the guest off. An empty answer is the normal case."""
        # The agent may close the channel while shutting down; that is success.
        with contextlib.suppress(GuestAgentError):
            self._exchange("guest-shutdown", {"mode": "powerdown"})


def _decoded(payload: str | None) -> str:
    if not payload:
        return ""
    return base64.b64decode(payload).decode("utf-8", errors="replace")
