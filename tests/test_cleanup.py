"""C2: the build key never outlives the build, and failed builds can be found and cleaned."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from gira_iso.builder import build as build_module
from gira_iso.builder.build import FAILED_MARKER, BuildError, build_image
from gira_iso.builder.catalog import find_incomplete
from gira_iso.builder.export import remove_incomplete
from gira_iso.cli import app
from gira_iso.policy.evaluate import load_allowlist
from gira_iso.spec.models import Spec

runner = CliRunner()


def spec() -> Spec:
    return Spec.model_validate({"packages": ["nginx"]})


@pytest.fixture
def stubbed(monkeypatch, tmp_path):
    """Run build_image for real except for the slow, external parts."""
    base = tmp_path / "base.img"
    base.write_bytes(b"x")
    monkeypatch.setattr(build_module, "ensure_base_image", lambda *args, **kwargs: (base, "a" * 64))
    monkeypatch.setattr(build_module.packer, "init", lambda *args, **kwargs: None)
    monkeypatch.setattr(build_module.packer, "version", lambda: "1.16.2")
    monkeypatch.setattr(build_module, "_virtual_size", lambda path: 1)

    state = {"packer": lambda template_dir, variables, log, on_line=None: _produce_image(variables)}
    monkeypatch.setattr(
        build_module.packer, "build", lambda *args, **kwargs: state["packer"](*args, **kwargs)
    )
    return state


def _produce_image(variables: dict) -> None:
    image_dir = Path(variables["build_dir"]) / "image"
    image_dir.mkdir(parents=True, exist_ok=True)
    (image_dir / "gira-iso-ubuntu-24.04.qcow2").write_bytes(b"qcow2")


def key_files(work_root: Path) -> list[Path]:
    return [p for p in work_root.rglob("id_ed25519*") if p.is_file()]


# --- the key is always discarded ---------------------------------------------------------


def test_the_key_is_deleted_after_a_successful_build(stubbed, tmp_path: Path) -> None:
    result = build_image(spec(), work_root=tmp_path / "builds", allowlist=load_allowlist())

    assert result.image.is_file()
    assert key_files(tmp_path / "builds") == []
    assert not (result.build_dir / FAILED_MARKER).exists()


def test_the_key_is_deleted_when_packer_fails(stubbed, tmp_path: Path) -> None:
    def fail(*args, **kwargs):
        raise build_module.packer.PackerError("packer exited with 1")

    stubbed["packer"] = fail

    with pytest.raises(build_module.packer.PackerError):
        build_image(spec(), work_root=tmp_path / "builds", allowlist=load_allowlist())

    assert key_files(tmp_path / "builds") == []


def test_the_key_is_deleted_on_ctrl_c_and_the_build_is_marked(stubbed, tmp_path: Path) -> None:
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    stubbed["packer"] = interrupt

    with pytest.raises(KeyboardInterrupt):
        build_image(spec(), work_root=tmp_path / "builds", allowlist=load_allowlist())

    assert key_files(tmp_path / "builds") == []
    (build_dir,) = (tmp_path / "builds").iterdir()
    assert (build_dir / FAILED_MARKER).read_text().strip().endswith("interrupted")


def test_a_build_that_produced_no_image_is_marked_with_the_reason(stubbed, tmp_path: Path) -> None:
    stubbed["packer"] = lambda *args, **kwargs: None  # packer "succeeds" but writes nothing

    with pytest.raises(BuildError):
        build_image(spec(), work_root=tmp_path / "builds", allowlist=load_allowlist())

    (build_dir,) = (tmp_path / "builds").iterdir()
    assert "produced no image" in (build_dir / FAILED_MARKER).read_text()
    assert key_files(tmp_path / "builds") == []


def test_the_key_is_real_while_the_build_runs(stubbed, tmp_path: Path) -> None:
    seen = {}

    def inspect(template_dir, variables, log, on_line=None):
        key = Path(variables["ssh_private_key_file"])
        seen["exists"] = key.is_file()
        seen["private"] = oct(key.stat().st_mode & 0o777)
        _produce_image(variables)

    stubbed["packer"] = inspect

    build_image(spec(), work_root=tmp_path / "builds", allowlist=load_allowlist())

    assert seen == {"exists": True, "private": "0o600"}


# --- finding and removing what a failure leaves behind -------------------------------------


def make_orphan(
    root: Path, name: str, *, marker: bool = False, subdir: str | None = "logs"
) -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    if subdir:
        (directory / subdir).mkdir()
        (directory / subdir / "packer-build.log").write_text("log")
    if marker:
        (directory / FAILED_MARKER).write_text("when it failed")
    return directory


def test_incomplete_builds_are_found_and_finished_ones_are_not(tmp_path: Path, make_build) -> None:
    root = tmp_path / "builds"
    make_build(root)  # a complete build: has a manifest
    orphan = make_orphan(root, "20260102T000000Z-aaaaaaaaaaaa")

    assert find_incomplete(root) == [orphan]


def test_a_marker_alone_is_enough_to_recognise_a_failed_build(tmp_path: Path) -> None:
    root = tmp_path / "builds"
    orphan = make_orphan(root, "failed-early", marker=True, subdir=None)

    assert find_incomplete(root) == [orphan]


def test_directories_the_tool_did_not_create_are_left_alone(tmp_path: Path) -> None:
    root = tmp_path / "builds"
    (root / "my-notes").mkdir(parents=True)
    (root / "my-notes" / "todo.txt").write_text("keep me")
    (root / "loose-file.txt").write_text("keep me too")

    assert find_incomplete(root) == []
    assert remove_incomplete(root) == []
    assert (root / "my-notes" / "todo.txt").exists()


def test_symlinks_are_never_followed(tmp_path: Path) -> None:
    root = tmp_path / "builds"
    root.mkdir()
    precious = make_orphan(tmp_path, "precious", marker=True)
    (root / "link").symlink_to(precious, target_is_directory=True)

    assert find_incomplete(root) == []
    assert remove_incomplete(root) == []
    assert precious.exists()


def test_remove_incomplete_reports_what_it_freed(tmp_path: Path) -> None:
    root = tmp_path / "builds"
    orphan = make_orphan(root, "20260102T000000Z-aaaaaaaaaaaa")

    removed = remove_incomplete(root)

    assert [path for path, _ in removed] == [orphan]
    assert removed[0][1] == len("log")
    assert not orphan.exists()


def test_a_missing_builds_directory_is_not_an_error(tmp_path: Path) -> None:
    assert find_incomplete(tmp_path / "nowhere") == []


# --- the command and the menu ----------------------------------------------------------------


def test_clean_says_when_there_is_nothing_to_do(tmp_path: Path) -> None:
    result = runner.invoke(app, ["clean", "--work-root", str(tmp_path)])

    assert result.exit_code == 0
    assert "nothing to clean" in result.output


def test_clean_asks_before_deleting(tmp_path: Path) -> None:
    orphan = make_orphan(tmp_path / "builds", "20260102T000000Z-aaaaaaaaaaaa")

    declined = runner.invoke(app, ["clean", "--work-root", str(tmp_path / "builds")], input="n\n")
    assert declined.exit_code == 0 and orphan.exists()

    accepted = runner.invoke(app, ["clean", "--work-root", str(tmp_path / "builds")], input="y\n")
    assert accepted.exit_code == 0 and not orphan.exists()
    assert "removed   1 build(s)" in accepted.output


def test_clean_with_yes_does_not_ask(tmp_path: Path) -> None:
    orphan = make_orphan(tmp_path / "builds", "20260102T000000Z-aaaaaaaaaaaa")

    result = runner.invoke(app, ["clean", "--work-root", str(tmp_path / "builds"), "--yes"])

    assert result.exit_code == 0 and not orphan.exists()


def test_the_menu_offers_to_clean_incomplete_builds(monkeypatch, tmp_path: Path) -> None:
    from gira_iso.nl.client import CredentialCheck

    root = tmp_path / "builds"
    orphan = make_orphan(root, "20260102T000000Z-aaaaaaaaaaaa")
    monkeypatch.setattr("gira_iso.tui.app.check_credentials", lambda config: CredentialCheck("ok"))
    monkeypatch.setattr("gira_iso.tui.images.builds_dir", lambda: root)

    result = runner.invoke(app, [], input="k\n1\nlimpiar\n0\n")

    assert result.exit_code == 0, result.output
    assert "build(s) incompleto(s)" in result.output
    assert "Borrados 1 build(s)" in result.output
    assert not orphan.exists()
