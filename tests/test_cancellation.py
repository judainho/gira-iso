"""C5 and C6: stopping early, by Ctrl+C or by the clock, leaves nothing running or behind."""

from __future__ import annotations

import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import gira_iso.builder.base as base_module
from gira_iso.builder.base import DOWNLOAD_TIMEOUT, BaseImageError, ensure_base_image
from gira_iso.builder.packer import (
    DEFAULT_BUILD_TIMEOUT,
    TIMEOUT_ENV,
    PackerError,
    _run,
    build_timeout,
)
from gira_iso.errors import EXIT_ENVIRONMENT
from gira_iso.policy.evaluate import Allowlist
from gira_iso.verify.vm import VirtualMachine


def allowlist(sha: str) -> Allowlist:
    return Allowlist.from_mapping(
        {
            "base_images": {"ubuntu": {"99.04": {"url": "https://x/base.img", "sha256": sha}}},
            "packages": [],
            "services": [],
        }
    )


@pytest.fixture
def cache(monkeypatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(base_module, "cache_dir", lambda: tmp_path / "cache")
    return tmp_path / "cache"


class Slow:
    """A response that delivers some bytes and then is interrupted."""

    headers = {"Content-Length": "100"}

    def __init__(self, error: BaseException) -> None:
        self._error = error
        self._sent = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, size: int) -> bytes:
        if not self._sent:
            self._sent = True
            return b"partial-bytes"
        raise self._error


# --- C5: downloads ----------------------------------------------------------------------------


def test_ctrl_c_during_a_download_removes_the_partial_file(monkeypatch, cache: Path) -> None:
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda url, timeout=None: Slow(KeyboardInterrupt())
    )

    with pytest.raises(KeyboardInterrupt):
        ensure_base_image("ubuntu", "99.04", allowlist("0" * 64))

    assert list(cache.iterdir()) == []


def test_a_network_failure_is_a_clean_error_and_leaves_nothing(monkeypatch, cache: Path) -> None:
    def fail(url, timeout=None):
        raise urllib.error.URLError("name resolution failed")

    monkeypatch.setattr(urllib.request, "urlopen", fail)

    with pytest.raises(BaseImageError, match="could not download https://x/base.img") as caught:
        ensure_base_image("ubuntu", "99.04", allowlist("0" * 64))

    assert caught.value.exit_code == EXIT_ENVIRONMENT
    assert list(cache.iterdir()) == []


def test_a_connection_that_drops_midway_is_a_clean_error(monkeypatch, cache: Path) -> None:
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda url, timeout=None: Slow(ConnectionResetError("reset"))
    )

    with pytest.raises(BaseImageError, match="could not download"):
        ensure_base_image("ubuntu", "99.04", allowlist("0" * 64))

    assert list(cache.iterdir()) == []


def test_the_download_has_a_timeout_so_it_cannot_hang(monkeypatch, cache: Path) -> None:
    seen = {}

    def spy(url, timeout=None):
        seen["timeout"] = timeout
        raise urllib.error.URLError("stop here")

    monkeypatch.setattr(urllib.request, "urlopen", spy)

    with pytest.raises(BaseImageError):
        ensure_base_image("ubuntu", "99.04", allowlist("0" * 64))

    assert seen["timeout"] == DOWNLOAD_TIMEOUT > 0


# --- C5: the verification VM ---------------------------------------------------------------------


class FakeGuest:
    def __init__(self) -> None:
        self.waited = False
        self.killed = False

    def poll(self):
        return None

    def wait(self, timeout=None):
        self.waited = True
        return 0

    def kill(self):
        self.killed = True


@pytest.mark.parametrize("error", [KeyboardInterrupt(), RuntimeError("no ssh")])
def test_a_guest_that_was_started_is_stopped_when_waiting_for_ssh_fails(
    monkeypatch, tmp_path: Path, error: BaseException
) -> None:
    guest = FakeGuest()
    machine = VirtualMachine(image=tmp_path / "image.qcow2", workdir=tmp_path / "run")
    poweroffs = []

    monkeypatch.setattr(machine, "_require_tools", lambda: None)
    monkeypatch.setattr(machine, "_prepare_disk", lambda: None)
    monkeypatch.setattr(machine, "_prepare_seed", lambda: None)
    monkeypatch.setattr(machine, "_ssh_args", lambda: ["ssh", "guest"])
    monkeypatch.setattr(machine, "_boot", lambda: setattr(machine, "_process", guest))

    def never_ready():
        raise error

    monkeypatch.setattr(machine, "_wait_for_ssh", never_ready)
    monkeypatch.setattr(
        "gira_iso.verify.vm.subprocess.run", lambda *args, **kwargs: poweroffs.append(args)
    )

    with pytest.raises(type(error)):
        machine.__enter__()

    assert poweroffs, "the guest was never asked to power off"
    assert guest.waited
    assert machine._process is None


# --- C6: the clock -------------------------------------------------------------------------------


def test_a_packer_that_overruns_is_stopped_and_reported(tmp_path: Path) -> None:
    started = time.monotonic()

    with pytest.raises(PackerError, match="did not finish within 0.5 seconds") as caught:
        _run(["sleep", "30"], tmp_path / "log", stream=False, timeout=0.5)

    assert time.monotonic() - started < 10, "the watchdog did not stop the process"
    assert caught.value.exit_code == EXIT_ENVIRONMENT
    assert TIMEOUT_ENV in str(caught.value)


def test_a_packer_that_finishes_in_time_is_not_disturbed(tmp_path: Path) -> None:
    _run(["true"], tmp_path / "log", stream=False, timeout=30)


def test_a_failing_packer_still_reports_its_exit_code_not_a_timeout(tmp_path: Path) -> None:
    with pytest.raises(PackerError, match="packer exited with 3"):
        _run(["sh", "-c", "exit 3"], tmp_path / "log", stream=False, timeout=30)


def test_no_timeout_means_no_watchdog(tmp_path: Path) -> None:
    _run(["true"], tmp_path / "log", stream=False)


def test_the_default_timeout_is_forty_five_minutes(monkeypatch) -> None:
    monkeypatch.delenv(TIMEOUT_ENV, raising=False)

    assert build_timeout() == DEFAULT_BUILD_TIMEOUT == 45 * 60


def test_the_timeout_can_be_raised_from_the_environment(monkeypatch) -> None:
    monkeypatch.setenv(TIMEOUT_ENV, "90")

    assert build_timeout() == 90 * 60


@pytest.mark.parametrize("value", ["abc", "0", "-5", ""])
def test_a_nonsense_timeout_falls_back_to_the_default(monkeypatch, value: str) -> None:
    monkeypatch.setenv(TIMEOUT_ENV, value)

    assert build_timeout() == DEFAULT_BUILD_TIMEOUT


# --- the verification key is a credential too -----------------------------------------------------


def _machine_with_key(monkeypatch, tmp_path: Path):
    machine = VirtualMachine(image=tmp_path / "image.qcow2", workdir=tmp_path / "run")
    machine.workdir.mkdir()
    machine.key = machine.workdir / "id_ed25519"
    machine.key.write_text("private")
    machine.key.with_suffix(".pub").write_text("public")
    (machine.workdir / "console.log").write_text("evidence")
    monkeypatch.setattr(machine, "_ssh_args", lambda: ["ssh", "guest"])
    monkeypatch.setattr("gira_iso.verify.vm.subprocess.run", lambda *args, **kwargs: None)
    return machine


def test_the_verification_key_is_deleted_when_the_guest_stops(monkeypatch, tmp_path: Path) -> None:
    machine = _machine_with_key(monkeypatch, tmp_path)
    machine._process = FakeGuest()

    machine.stop()

    assert not (machine.workdir / "id_ed25519").exists()
    assert not (machine.workdir / "id_ed25519.pub").exists()
    assert (machine.workdir / "console.log").read_text() == "evidence"  # the evidence stays


def test_the_key_is_deleted_even_if_powering_off_fails(monkeypatch, tmp_path: Path) -> None:
    machine = _machine_with_key(monkeypatch, tmp_path)

    class Stuck(FakeGuest):
        def wait(self, timeout=None):
            raise RuntimeError("cannot stop")

    machine._process = Stuck()

    with pytest.raises(RuntimeError):
        machine.stop()

    assert not (machine.workdir / "id_ed25519").exists()


def test_stopping_a_guest_that_never_started_still_removes_the_key(
    monkeypatch, tmp_path: Path
) -> None:
    machine = _machine_with_key(monkeypatch, tmp_path)  # seed prepared, boot never reached

    machine.stop()

    assert not (machine.workdir / "id_ed25519").exists()


def test_a_machine_without_a_key_stops_quietly(tmp_path: Path) -> None:
    machine = VirtualMachine(image=tmp_path / "image.qcow2", workdir=tmp_path / "run")

    machine.stop()  # no key attribute yet, no process: nothing to do, nothing to fail
