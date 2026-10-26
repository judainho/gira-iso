"""C4: say what is wrong with the machine, and how to fix it, before spending anything."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from gira_iso.cli import app
from gira_iso.errors import EXIT_ENVIRONMENT
from gira_iso.spec.models import Spec
from gira_iso.workflow.create import CreateRequest, run_create
from gira_iso.workflow.preflight import (
    GIB,
    EnvironmentProblem,
    check_environment,
    check_kvm,
    check_space,
    check_tools,
    ensure_environment,
)

runner = CliRunner()
EVERYTHING = {
    "qemu-system-x86_64",
    "qemu-img",
    "cloud-localds",
    "ssh-keygen",
    "packer",
    "goss",
}

pytestmark = pytest.mark.real_preflight


def which_with(*present: str):
    return lambda name: f"/usr/bin/{name}" if name in present else None


# --- tools -------------------------------------------------------------------------------


def test_a_complete_toolchain_has_no_problems() -> None:
    assert check_tools(which_with(*EVERYTHING)) == []


def test_missing_apt_tools_come_with_one_install_command() -> None:
    problems = check_tools(which_with("packer", "goss", "ssh-keygen"))

    assert len(problems) == 1
    assert "qemu-system-x86_64" in problems[0].what and "cloud-localds" in problems[0].what
    assert problems[0].fix == (
        "sudo apt-get install -y cloud-image-utils qemu-system-x86 qemu-utils"
    )


def test_pinned_tools_point_at_the_installer_not_at_apt() -> None:
    problems = check_tools(
        which_with("qemu-system-x86_64", "qemu-img", "cloud-localds", "ssh-keygen")
    )

    assert len(problems) == 1
    assert "packer, goss" in problems[0].what
    assert "./install.sh" in problems[0].fix
    assert "apt-get" not in problems[0].fix


# --- kvm ---------------------------------------------------------------------------------


def test_no_kvm_device_explains_virtualisation(tmp_path: Path) -> None:
    problems = check_kvm(tmp_path / "kvm")

    assert "no hardware virtualisation" in problems[0].what
    assert "WSL2" in problems[0].fix


def test_a_kvm_you_can_use_is_fine(tmp_path: Path) -> None:
    device = tmp_path / "kvm"
    device.touch()

    assert check_kvm(device, access=lambda path, mode: True) == []


def test_a_kvm_you_cannot_use_names_the_group_and_the_fix(tmp_path: Path) -> None:
    device = tmp_path / "kvm"
    device.touch()

    (problem,) = check_kvm(device, access=lambda path, mode: False)

    group = Path("/etc/group").read_text().splitlines()
    owner_group = next(
        line.split(":")[0] for line in group if line.split(":")[2] == str(device.stat().st_gid)
    )
    assert owner_group in problem.what
    assert f"sudo usermod -aG {owner_group} $USER" in problem.fix


# --- disk space -------------------------------------------------------------------------------


def test_enough_space_has_no_problems(tmp_path: Path) -> None:
    assert check_space(tmp_path, 10, usage=lambda path: (0, 0, 100 * GIB)) == []


def test_not_enough_space_says_how_much_is_needed(tmp_path: Path) -> None:
    (problem,) = check_space(tmp_path, 10, usage=lambda path: (0, 0, 5 * GIB))

    assert "5.0 GB free" in problem.what and "12 GB needed" in problem.what
    assert "GIRA_ISO_HOME" in problem.fix


def test_space_is_measured_on_the_nearest_existing_parent(tmp_path: Path) -> None:
    seen = []

    def usage(path: Path):
        seen.append(path)
        return (0, 0, 100 * GIB)

    check_space(tmp_path / "does" / "not" / "exist", 8, usage=usage)

    assert seen == [tmp_path]


# --- the error ----------------------------------------------------------------------------------


def test_the_error_lists_every_problem_with_its_fix() -> None:
    problems = check_environment(which_with(), Path("/nonexistent/kvm"))

    error = EnvironmentProblem(problems)

    assert error.exit_code == EXIT_ENVIRONMENT
    assert str(error).startswith("this machine cannot build images yet:")
    assert str(error).count("fix:") == len(problems) >= 3


def test_ensure_environment_raises_when_something_is_missing(monkeypatch) -> None:
    monkeypatch.setattr("gira_iso.workflow.preflight.shutil.which", lambda name: None)

    with pytest.raises(EnvironmentProblem):
        ensure_environment()


# --- where it runs ------------------------------------------------------------------------------


class NoIo:
    interactive = True

    def __init__(self) -> None:
        self.calls: list[str] = []

    def emit(self, event):
        self.calls.append("emit")

    def answer(self, questions):
        self.calls.append("answer")
        return []

    def busy(self, message):
        self.calls.append("busy")
        from contextlib import nullcontext

        return nullcontext()

    def review(self, review):
        self.calls.append("review")

    def propose(self, proposals, refusals):
        return []

    def destination(self, name):
        return None


def test_the_workflow_stops_before_touching_the_model(monkeypatch) -> None:
    monkeypatch.setattr("gira_iso.workflow.preflight.shutil.which", lambda name: None)

    class Exploding:
        def complete(self, system, user):
            raise AssertionError("the model must not be called on a machine that cannot build")

    io = NoIo()
    with pytest.raises(EnvironmentProblem):
        run_create(CreateRequest(text="un nginx", completer=Exploding()), io)

    assert io.calls == []


def test_a_request_can_skip_the_preflight(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("gira_iso.workflow.preflight.shutil.which", lambda name: None)
    spec = Spec.model_validate({"packages": ["nginx"]})
    reached = []
    monkeypatch.setattr(
        "gira_iso.workflow.create.build_image",
        lambda *args, **kwargs: reached.append("build") or (_ for _ in ()).throw(KeyError("stop")),
    )

    with pytest.raises(KeyError):
        run_create(CreateRequest(spec=spec, review=False, preflight=False), NoIo())

    assert reached == ["build"]


def test_the_space_is_checked_with_the_size_the_spec_asks_for(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("gira_iso.workflow.create.ensure_environment", lambda: None)
    asked = []

    def record(root, gb):
        asked.append(gb)
        raise EnvironmentProblem([])

    monkeypatch.setattr("gira_iso.workflow.create.ensure_space", record)
    spec = Spec.model_validate({"packages": ["nginx"], "disk_size": "40G"})

    with pytest.raises(EnvironmentProblem):
        run_create(CreateRequest(spec=spec, review=False, work_root=tmp_path), NoIo())

    assert asked == [40]


def test_the_command_line_reports_it_as_exit_3_without_a_traceback(monkeypatch) -> None:
    monkeypatch.setattr("gira_iso.workflow.preflight.shutil.which", lambda name: None)

    result = runner.invoke(app, ["create", "examples/nginx-web.yaml", "--name", "web"])

    assert result.exit_code == EXIT_ENVIRONMENT
    assert "this machine cannot build images yet" in result.output
    assert "fix: sudo apt-get install -y" in result.output
    assert "Traceback" not in result.output


def test_the_menu_shows_what_is_missing_and_stays_usable(monkeypatch) -> None:
    from gira_iso.nl.client import CredentialCheck

    monkeypatch.setattr("gira_iso.tui.app.check_credentials", lambda config: CredentialCheck("ok"))
    monkeypatch.setattr("gira_iso.workflow.preflight.shutil.which", lambda name: None)

    result = runner.invoke(app, [], input="k\n0\n")

    assert result.exit_code == 0, result.output
    assert "Este equipo aún no puede construir" in result.output
    assert "arreglo: sudo apt-get install" in result.output
    assert "Menú" in result.output


def test_the_menu_says_when_everything_is_ready(monkeypatch) -> None:
    from gira_iso.nl.client import CredentialCheck

    monkeypatch.setattr("gira_iso.tui.app.check_credentials", lambda config: CredentialCheck("ok"))
    monkeypatch.setattr("gira_iso.tui.app.check_environment", lambda: [])

    result = runner.invoke(app, [], input="k\n0\n")

    assert "Entorno listo para construir" in result.output


def test_this_machine_check_does_not_crash() -> None:
    # Whatever the verdict, the real checks must run and return plain problems.
    problems = check_environment()

    assert all(problem.what and problem.fix for problem in problems)
    assert isinstance(os.environ, os._Environ)
