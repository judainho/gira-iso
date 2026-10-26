"""Where the image goes: an absolute path the person has seen, never a surprise folder."""

from __future__ import annotations

from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from gira_iso import settings
from gira_iso.cli import app
from gira_iso.nl.client import CredentialCheck
from gira_iso.tui.common import ask_directory, remembered_directory

runner = CliRunner()


def console() -> Console:
    return Console(width=200, force_terminal=False)


def answer(monkeypatch, *replies: str) -> list[str]:
    """Feed rich's Prompt/Confirm from a script; returns the questions it was asked."""
    queue = list(replies)
    asked: list[str] = []

    def fake_input(self, console, prompt, password, stream=None):
        asked.append(str(prompt))
        return queue.pop(0)

    monkeypatch.setattr("rich.prompt.PromptBase.get_input", fake_input)
    return asked


# --- ask_directory -------------------------------------------------------------------------


def test_an_existing_folder_is_accepted_without_questions(monkeypatch, tmp_path: Path) -> None:
    answer(monkeypatch, str(tmp_path))

    assert ask_directory(console(), "where", "x") == tmp_path.resolve()


def test_a_relative_answer_becomes_an_absolute_path(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "out").mkdir()
    answer(monkeypatch, "out")

    result = ask_directory(console(), "where", "x")

    assert result == (tmp_path / "out").resolve()
    assert result.is_absolute()


def test_a_folder_that_does_not_exist_is_shown_in_full_and_confirmed(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    asked = answer(monkeypatch, "si", "y")  # the real mistake: "yes" typed where a folder was asked

    result = ask_directory(console(), "where", "x")

    assert result == (tmp_path / "si").resolve()
    assert any(str(tmp_path / "si") in question for question in asked), asked


def test_declining_the_creation_asks_again(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "real").mkdir()
    answer(monkeypatch, "si", "n", "real")

    assert ask_directory(console(), "where", "x") == (tmp_path / "real").resolve()
    assert not (tmp_path / "si").exists()  # asking never creates anything


def test_a_file_where_a_folder_is_expected_is_refused(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "file.txt").write_text("x")
    answer(monkeypatch, str(tmp_path / "file.txt"), str(tmp_path))

    assert ask_directory(console(), "where", "x") == tmp_path.resolve()


def test_the_home_shortcut_is_expanded(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "pics").mkdir()
    answer(monkeypatch, "~/pics")

    assert ask_directory(console(), "where", "x") == (tmp_path / "pics").resolve()


# --- what is remembered ----------------------------------------------------------------------


def test_an_absolute_remembered_folder_is_offered() -> None:
    assert remembered_directory("/srv/images") == "/srv/images"


@pytest.mark.parametrize("saved", [None, "", "si", "relative/dir"])
def test_a_relative_or_missing_one_falls_back_to_the_default(saved) -> None:
    assert remembered_directory(saved) == str(Path.home() / "gira-iso-images")


# --- end to end in the menu ----------------------------------------------------------------------


def test_the_menu_reports_and_remembers_an_absolute_path(monkeypatch, tmp_path: Path, make_build):
    import json

    import yaml

    spec = yaml.safe_load(Path("examples/nginx-web.yaml").read_text(encoding="utf-8"))
    fake = make_build(tmp_path / "builds")

    class Completer:
        def complete(self, system, user):
            from gira_iso.nl.client import Completion

            return Completion(text=json.dumps({"kind": "spec", "spec": spec}))

    monkeypatch.setattr("gira_iso.workflow.create.build_image", lambda spec, **k: fake)
    monkeypatch.setattr("gira_iso.tui.create.OpenAiCompatible", lambda config: Completer())
    monkeypatch.setattr("gira_iso.tui.app.check_credentials", lambda c: CredentialCheck("ok"))
    monkeypatch.delenv("GIRA_ISO_LLM_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)

    # "si" typed as the folder, then accepted when the full path is shown.
    result = runner.invoke(app, [], input="k\n4\n\nun nginx\nweb\ny\nn\nsi\ny\n0\n")

    assert result.exit_code == 0, result.output
    expected = tmp_path / "si"
    assert f"La carpeta {expected}" in result.output.replace("\n", "")
    assert str(expected / "web.qcow2") in result.output.replace("\n", "")
    assert (expected / "web.qcow2").is_file()
    assert settings.load().last_destination == str(expected)


# --- noisy log lines ----------------------------------------------------------------------------


def test_a_huge_log_line_is_cut_to_one_line_on_screen(monkeypatch, tmp_path: Path, make_build):
    import json

    import yaml

    spec = yaml.safe_load(Path("examples/nginx-web.yaml").read_text(encoding="utf-8"))
    fake = make_build(tmp_path / "builds")
    huge = "+ rm -rf " + " ".join(f"/var/lib/apt/lists/file_{i}" for i in range(400))

    class Completer:
        def complete(self, system, user):
            from gira_iso.nl.client import Completion

            return Completion(text=json.dumps({"kind": "spec", "spec": spec}))

    def build(spec, **kwargs):
        kwargs["on_line"](huge)
        kwargs["on_line"]("short line")
        return fake

    monkeypatch.setattr("gira_iso.workflow.create.build_image", build)
    monkeypatch.setattr("gira_iso.tui.create.OpenAiCompatible", lambda config: Completer())
    monkeypatch.setattr("gira_iso.tui.app.check_credentials", lambda c: CredentialCheck("ok"))
    monkeypatch.delenv("GIRA_ISO_LLM_API_KEY", raising=False)

    result = runner.invoke(app, [], input=f"k\n4\n\nx\nweb\ny\nn\n{tmp_path}\n0\n")

    log_lines = [line for line in result.output.splitlines() if "│ " in line and "rm -rf" in line]
    assert len(log_lines) == 1, "the huge line was wrapped over several lines"
    assert "…" in log_lines[0]
    assert len(log_lines[0]) <= 120
    assert "short line" in result.output
