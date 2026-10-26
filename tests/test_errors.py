"""Every expected failure is a GiraError that knows its exit code (finding H2, H12)."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from gira_iso.bases import UserBaseError
from gira_iso.builder.base import BaseImageError
from gira_iso.builder.build import BuildError, BuildRejected
from gira_iso.builder.catalog import CatalogError, load_manifest, resolve_image
from gira_iso.builder.export import ExportError, ExportExists
from gira_iso.builder.packer import PackerError
from gira_iso.cli import app
from gira_iso.errors import (
    EXIT_ENVIRONMENT,
    EXIT_INTERRUPTED,
    EXIT_INVALID,
    EXIT_REJECTED,
    GiraError,
)
from gira_iso.naming import InvalidImageName, checked_name, is_valid_image_name
from gira_iso.nl.client import InterpretError
from gira_iso.policy.evaluate import Verdict
from gira_iso.push.openstack import PushError
from gira_iso.verify.verify import VerificationError
from gira_iso.verify.verify import load_manifest as verify_load_manifest
from gira_iso.verify.vm import VmError

runner = CliRunner()


def test_the_exit_codes_are_the_documented_ones() -> None:
    assert (EXIT_REJECTED, EXIT_INVALID, EXIT_ENVIRONMENT, EXIT_INTERRUPTED) == (1, 2, 3, 130)


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (BuildRejected(Verdict(violations=())), EXIT_REJECTED),
        (BuildError("x"), EXIT_ENVIRONMENT),
        (BaseImageError("x"), EXIT_ENVIRONMENT),
        (PackerError("x"), EXIT_ENVIRONMENT),
        (VmError("x"), EXIT_ENVIRONMENT),
        (VerificationError("x"), EXIT_INVALID),
        (InterpretError("x"), EXIT_INVALID),
        (UserBaseError("x"), EXIT_INVALID),
        (PushError("x"), EXIT_INVALID),
        (CatalogError("x"), EXIT_INVALID),
        (InvalidImageName("x"), EXIT_INVALID),
        (ExportError("x", invalid=True), EXIT_INVALID),
        (ExportError("x"), EXIT_REJECTED),
        (ExportExists("x"), EXIT_REJECTED),
    ],
)
def test_each_error_carries_its_exit_code(error: GiraError, code: int) -> None:
    assert isinstance(error, GiraError)
    assert isinstance(error, RuntimeError)  # code that still catches RuntimeError keeps working
    assert error.exit_code == code


# --- the command line never shows a traceback ----------------------------------------


def _create_with(monkeypatch, tmp_path: Path, error: BaseException):
    import gira_iso.cli as cli

    def explode(spec, **kwargs):
        raise error

    monkeypatch.setattr(cli, "build_image", explode)
    return runner.invoke(
        app,
        ["create", "examples/nginx-web.yaml", "--name", "web", "--work-root", str(tmp_path)],
    )


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (PackerError("packer exited with 1"), EXIT_ENVIRONMENT),
        (BaseImageError("checksum mismatch"), EXIT_ENVIRONMENT),
        (BuildError("build directory already exists"), EXIT_ENVIRONMENT),
        (KeyboardInterrupt(), EXIT_INTERRUPTED),
    ],
)
def test_create_turns_a_build_failure_into_a_message_and_a_code(
    monkeypatch, tmp_path: Path, error: BaseException, code: int
) -> None:
    result = _create_with(monkeypatch, tmp_path, error)

    assert result.exit_code == code
    assert not isinstance(result.exception, (PackerError, BaseImageError, BuildError))
    assert "Traceback" not in result.output
    assert ("error:" in result.output) or ("interrupted" in result.output)


def test_a_failed_verification_is_a_message_not_a_traceback(monkeypatch, tmp_path: Path) -> None:
    import gira_iso.cli as cli
    from gira_iso.builder.build import BuildResult

    image = tmp_path / "web.qcow2"
    image.write_bytes(b"x")
    fake = BuildResult(build_dir=tmp_path, image=image, manifest=tmp_path / "manifest.json")
    monkeypatch.setattr(cli, "build_image", lambda spec, **kwargs: fake)

    def broken(build_dir):
        raise VerificationError("manifest points at nowhere")

    monkeypatch.setattr(cli, "verify_build", broken)

    result = runner.invoke(
        app,
        [
            "create",
            "examples/nginx-web.yaml",
            "--name",
            "web",
            "--verify",
            "--work-root",
            str(tmp_path),
        ],
    )

    assert result.exit_code == EXIT_INVALID
    assert "manifest points at nowhere" in result.output


def test_the_hidden_build_command_is_guarded_too(monkeypatch, tmp_path: Path) -> None:
    import gira_iso.cli as cli

    def explode(spec, **kwargs):
        raise PackerError("packer exited with 1")

    monkeypatch.setattr(cli, "build_image", explode)

    result = runner.invoke(app, ["build", "examples/nginx-web.yaml", "--work-root", str(tmp_path)])

    assert result.exit_code == EXIT_ENVIRONMENT


# --- one rule for names ---------------------------------------------------------------


def test_names_have_a_single_definition() -> None:
    assert is_valid_image_name("web")
    assert is_valid_image_name("a" + "b" * 63)  # 64 characters, the longer of the old two rules
    assert not is_valid_image_name("a" + "b" * 64)
    assert not is_valid_image_name("-web")
    assert not is_valid_image_name("bad name")
    assert checked_name("web") == "web"
    with pytest.raises(InvalidImageName, match="invalid image name"):
        checked_name("bad name")


def test_the_menu_and_the_command_line_accept_the_same_names() -> None:
    import gira_iso.cli as cli
    import gira_iso.tui as tui

    assert not hasattr(cli, "_IMAGE_NAME")
    assert not hasattr(tui, "NAME_RE")


# --- one place that reads manifests ----------------------------------------------------


def test_a_missing_manifest_says_the_same_thing_everywhere(tmp_path: Path) -> None:
    with pytest.raises(CatalogError, match="no manifest at"):
        load_manifest(tmp_path)
    with pytest.raises(VerificationError, match="no manifest at"):
        verify_load_manifest(tmp_path)


def test_an_unreadable_manifest_says_the_same_thing_everywhere(tmp_path: Path) -> None:
    (tmp_path / "manifest.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(CatalogError, match="is not valid"):
        load_manifest(tmp_path)
    with pytest.raises(VerificationError, match="is not valid"):
        verify_load_manifest(tmp_path)


def test_resolving_an_unknown_image_is_an_input_error(tmp_path: Path) -> None:
    with pytest.raises(CatalogError, match="no image matches") as caught:
        resolve_image("nope", tmp_path)
    assert caught.value.exit_code == EXIT_INVALID
