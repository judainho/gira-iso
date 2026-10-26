"""C3: one build at a time, and never a name collision."""

from __future__ import annotations

import fcntl
from pathlib import Path

import pytest

from gira_iso.builder import build as build_module
from gira_iso.builder.build import _build_dir, build_image
from gira_iso.builder.export import remove_incomplete
from gira_iso.builder.lock import LOCK_NAME, BuildBusy, build_lock
from gira_iso.errors import EXIT_ENVIRONMENT
from gira_iso.policy.evaluate import load_allowlist
from gira_iso.spec.models import Spec


def test_the_lock_is_free_after_the_block(tmp_path: Path) -> None:
    with build_lock(tmp_path):
        pass
    with build_lock(tmp_path):  # a second use works: nothing was left held
        pass


def test_a_second_holder_is_refused_with_a_clear_message(tmp_path: Path) -> None:
    with (
        build_lock(tmp_path),
        pytest.raises(BuildBusy, match="another build is running") as caught,
        build_lock(tmp_path),
    ):
        pass
    assert caught.value.exit_code == EXIT_ENVIRONMENT


def test_the_message_names_the_process_that_holds_it(tmp_path: Path) -> None:
    import os

    with build_lock(tmp_path), pytest.raises(BuildBusy) as caught, build_lock(tmp_path):
        pass
    assert f"process {os.getpid()}" in str(caught.value)


def test_the_lock_is_released_when_the_block_fails(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError), build_lock(tmp_path):
        raise RuntimeError("boom")

    with build_lock(tmp_path):
        pass


def test_a_lock_held_by_someone_else_is_respected(tmp_path: Path) -> None:
    tmp_path.mkdir(exist_ok=True)
    with (tmp_path / LOCK_NAME).open("a+") as other:
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BuildBusy), build_lock(tmp_path):
            pass


def test_build_image_refuses_to_start_while_another_build_runs(monkeypatch, tmp_path: Path) -> None:
    base = tmp_path / "base.img"
    base.write_bytes(b"x")
    monkeypatch.setattr(build_module, "ensure_base_image", lambda *a, **k: (base, "a" * 64))
    called = []
    monkeypatch.setattr(build_module.packer, "init", lambda *a, **k: called.append("init"))

    with build_lock(tmp_path / "builds"), pytest.raises(BuildBusy):
        build_image(
            Spec.model_validate({"packages": ["nginx"]}),
            work_root=tmp_path / "builds",
            allowlist=load_allowlist(),
        )

    assert called == []
    assert [p for p in (tmp_path / "builds").iterdir() if p.is_dir()] == []


def test_clean_will_not_delete_a_build_in_progress(tmp_path: Path) -> None:
    root = tmp_path / "builds"
    running = root / "20260102T000000Z-aaaaaaaaaaaa"
    (running / "logs").mkdir(parents=True)

    with build_lock(root), pytest.raises(BuildBusy):  # the build writing into `running`
        remove_incomplete(root)

    assert running.exists()
    assert [path for path, _ in remove_incomplete(root)] == [running]


def test_the_same_spec_in_the_same_second_gets_distinct_directories(
    monkeypatch, tmp_path: Path
) -> None:
    from datetime import UTC, datetime

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 1, 1, tzinfo=UTC)

    monkeypatch.setattr(build_module, "datetime", Frozen)

    first = _build_dir(tmp_path, "a" * 64)
    second = _build_dir(tmp_path, "a" * 64)
    third = _build_dir(tmp_path, "a" * 64)

    assert len({first, second, third}) == 3
    assert second.name.endswith("-2") and third.name.endswith("-3")
