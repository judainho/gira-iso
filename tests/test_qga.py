"""The guest-agent client against a scripted agent on a real unix socket.

No QEMU here: the protocol is the contract, and a wrong message shape is what breaks a real
verification at 3 a.m.
"""

from __future__ import annotations

import base64
import json
import socket
import threading
from pathlib import Path
from typing import Any

import pytest

from gira_iso.verify.qga import CHUNK_BYTES, GuestAgent, GuestAgentError


class FakeAgent:
    """One request per connection; replies are queued in order."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.requests: list[dict[str, Any]] = []
        self.replies: list[dict[str, Any]] = []
        self.close_replies_on_empty = True
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(path))
        self._server.listen(8)
        self._server.settimeout(0.2)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> FakeAgent:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        self._thread.join(timeout=3)
        self._server.close()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                connection, _ = self._server.accept()
            except OSError:
                continue
            with connection:
                raw = b""
                while not raw.endswith(b"\n"):
                    block = connection.recv(65536)
                    if not block:
                        break
                    raw += block
                if not raw.strip():
                    continue
                self.requests.append(json.loads(raw.decode("utf-8")))
                reply = self.replies.pop(0) if self.replies else {"return": {}}
                if reply.get("__close__"):
                    continue
                if "__raw__" in reply:
                    connection.sendall(reply["__raw__"].encode("utf-8") + b"\n")
                    continue
                connection.sendall(json.dumps(reply).encode("utf-8") + b"\n")


def _agent(tmp_path: Path) -> GuestAgent:
    return GuestAgent(tmp_path / "qga.sock", timeout=5)


def test_ping_reports_a_silent_agent_as_down(tmp_path: Path) -> None:
    with FakeAgent(tmp_path / "qga.sock") as fake:
        fake.replies.append({"__close__": True})

        assert _agent(tmp_path).ping() is False


def test_ping_returns_true_when_the_agent_answers(tmp_path: Path) -> None:
    with FakeAgent(tmp_path / "qga.sock"):
        assert _agent(tmp_path).ping() is True


def test_run_collects_stdout_and_exit_code(tmp_path: Path) -> None:
    with FakeAgent(tmp_path / "qga.sock") as fake:
        fake.replies += [
            {"return": {"pid": 42}},
            {"return": {"exited": True, "exitcode": 0, "out-data": _b64("hola\n")}},
        ]

        result = _agent(tmp_path).run("echo hola")

    assert result.stdout == "hola\n"
    assert result.returncode == 0
    assert fake.requests[0] == {
        "execute": "guest-exec",
        "arguments": {"path": "/bin/sh", "arg": ["-c", "echo hola"], "capture-output": True},
    }
    assert fake.requests[1] == {"execute": "guest-exec-status", "arguments": {"pid": 42}}


def test_run_keeps_stderr_and_a_non_zero_exit(tmp_path: Path) -> None:
    with FakeAgent(tmp_path / "qga.sock") as fake:
        fake.replies += [
            {"return": {"pid": 7}},
            {"return": {"exited": True, "exitcode": 3, "err-data": _b64("boom\n")}},
        ]

        result = _agent(tmp_path).run("false")

    assert result.returncode == 3
    assert result.stderr == "boom\n"
    assert result.stdout == ""


def test_an_agent_error_is_a_message_not_a_crash(tmp_path: Path) -> None:
    with FakeAgent(tmp_path / "qga.sock") as fake:
        fake.replies.append({"error": {"class": "GenericError", "desc": "no such path"}})

        with pytest.raises(GuestAgentError, match="guest agent refused guest-exec: no such path"):
            _agent(tmp_path).run("nope")


def test_upload_sends_the_file_in_chunks_and_closes_the_handle(tmp_path: Path) -> None:
    payload = b"x" * (CHUNK_BYTES * 2 + 10)
    source = tmp_path / "goss"
    source.write_bytes(payload)

    with FakeAgent(tmp_path / "qga.sock") as fake:
        fake.replies.append({"return": 5})  # guest-file-open answers with a bare handle
        fake.replies += [
            {"return": {"count": CHUNK_BYTES}},
            {"return": {"count": CHUNK_BYTES}},
            {"return": {"count": len(payload) - 2 * CHUNK_BYTES}},
        ]

        _agent(tmp_path).upload(source, "/tmp/goss")

    writes = [request for request in fake.requests if request["execute"] == "guest-file-write"]
    assert len(writes) == 3
    joined = b"".join(base64.b64decode(request["arguments"]["buf-b64"]) for request in writes)
    assert joined == payload
    assert fake.requests[0] == {
        "execute": "guest-file-open",
        "arguments": {"path": "/tmp/goss", "mode": "wb"},
    }
    assert fake.requests[-1] == {"execute": "guest-file-close", "arguments": {"handle": 5}}


def test_upload_closes_the_handle_even_when_a_write_fails(tmp_path: Path) -> None:
    source = tmp_path / "goss"
    source.write_bytes(b"y" * (CHUNK_BYTES + 1))

    with FakeAgent(tmp_path / "qga.sock") as fake:
        fake.replies += [
            {"return": 9},
            {"error": {"class": "GenericError", "desc": "disk full"}},
        ]

        with pytest.raises(GuestAgentError, match="disk full"):
            _agent(tmp_path).upload(source, "/tmp/goss")

    assert fake.requests[-1]["execute"] == "guest-file-close"


def test_shutdown_tolerates_a_channel_that_dies(tmp_path: Path) -> None:
    with FakeAgent(tmp_path / "qga.sock") as fake:
        fake.replies.append({"__close__": True})

        _agent(tmp_path).shutdown()  # must not raise

    assert fake.requests[0] == {"execute": "guest-shutdown", "arguments": {"mode": "powerdown"}}


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def test_a_zero_handle_is_a_valid_file_handle(tmp_path: Path) -> None:
    source = tmp_path / "small"
    source.write_bytes(b"abc")

    with FakeAgent(tmp_path / "qga.sock") as fake:
        fake.replies += [{"return": 0}, {"return": {"count": 3}}]

        _agent(tmp_path).upload(source, "/tmp/small")

    assert fake.requests[-1] == {"execute": "guest-file-close", "arguments": {"handle": 0}}


def test_a_short_write_is_an_error_not_a_truncated_file(tmp_path: Path) -> None:
    source = tmp_path / "small"
    source.write_bytes(b"abcdef")

    with FakeAgent(tmp_path / "qga.sock") as fake:
        fake.replies += [{"return": 1}, {"return": {"count": 2}}]

        with pytest.raises(GuestAgentError, match="wrote 2 of 6 bytes"):
            _agent(tmp_path).upload(source, "/tmp/small")


def test_run_gives_up_instead_of_polling_forever(tmp_path: Path) -> None:
    with FakeAgent(tmp_path / "qga.sock") as fake:
        fake.replies.append({"return": {"pid": 1}})
        fake.replies += [{"return": {"exited": False}} for _ in range(200)]

        with pytest.raises(GuestAgentError, match="timed out after 1s"):
            _agent(tmp_path).run("sleep 999", timeout=1)


def test_a_reply_that_is_not_an_object_is_refused(tmp_path: Path) -> None:
    with FakeAgent(tmp_path / "qga.sock") as fake:
        fake.replies.append({"__raw__": "[1, 2]"})

        with pytest.raises(GuestAgentError, match="unexpected guest-exec reply"):
            _agent(tmp_path).run("true")
