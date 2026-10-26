"""Both interfaces ask before extending the allowlist, and `--yes` never answers for the person."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from gira_iso.additions import list_additions
from gira_iso.archive import ArchivePackage
from gira_iso.cli import app
from gira_iso.nl.client import Completion, CredentialCheck

runner = CliRunner()

NODE = ArchivePackage("nodejs", "18.19.1", "universe", "web", "Release")
TELNET = ArchivePackage("telnetd", "0.17", "universe", "net", "Release")


def missing(item: str = "node") -> str:
    return json.dumps(
        {
            "kind": "spec",
            "spec": {"packages": ["nginx"]},
            "unsupported": [{"item": item, "kind": "package", "reason": "no está"}],
        }
    )


def with_node() -> str:
    return json.dumps({"kind": "spec", "spec": {"packages": ["nginx", "nodejs"]}})


def suggestion(*names: str) -> str:
    return json.dumps({"candidates": list(names)})


class Model:
    def __init__(self, *replies: str) -> None:
        self._replies = list(replies)
        self.calls = 0

    def complete(self, system: str, user: str) -> Completion:
        self.calls += 1
        return Completion(text=self._replies.pop(0))


@pytest.fixture
def world(monkeypatch, tmp_path, make_build):
    """A fake archive, builder and model; returns the builds made and the model to script."""
    fake = make_build(tmp_path / "builds")
    built: list = []
    monkeypatch.setattr(
        "gira_iso.workflow.create.build_image", lambda spec, **kw: built.append(spec) or fake
    )
    monkeypatch.setattr(
        "gira_iso.archive.lookup",
        lambda package, base_version, fetch=None: {"nodejs": NODE, "telnetd": TELNET}.get(package),
    )
    monkeypatch.setenv("GIRA_ISO_LLM_API_KEY", "key")
    monkeypatch.setattr("gira_iso.tui.app.check_credentials", lambda c: CredentialCheck("ok"))

    class World:
        def __init__(self) -> None:
            self.model: Model | None = None
            self.built = built

        def use(self, *replies: str) -> Model:
            self.model = Model(*replies)
            monkeypatch.setattr("gira_iso.cli.create.OpenAiCompatible", lambda c: self.model)
            monkeypatch.setattr("gira_iso.tui.create.OpenAiCompatible", lambda c: self.model)
            return self.model

    return World()


def create(*extra: str, input: str = ""):
    return runner.invoke(app, ["create", "nginx y node", "--name", "web", *extra], input=input)


# --- command line ----------------------------------------------------------------------------


def test_the_cli_asks_and_an_approval_extends_the_list_and_builds(world) -> None:
    world.use(missing(), suggestion("nodejs"), with_node())

    result = create(input="y\ny\n")  # allow nodejs? then: build this image?

    assert result.exit_code == 0, result.output
    assert "the request needs packages that are not on the allowlist" in result.output
    assert "found     nodejs 18.19.1 (universe, web) in ubuntu 24.04" in result.output
    assert [a.package for a in list_additions()] == ["nodejs"]
    assert world.built[0].packages == ["nginx", "nodejs"]


def test_the_default_answer_is_no(world) -> None:
    world.use(missing(), suggestion("nodejs"))

    result = create(input="\ny\n")  # Enter at "allow nodejs?", then build anyway

    assert result.exit_code == 0, result.output
    assert list_additions() == []
    assert world.built[0].packages == ["nginx"]


def test_yes_does_not_approve_an_addition(world) -> None:
    """--yes skips the build prompt. It must not answer a question about widening the policy."""
    world.use(missing(), suggestion("nodejs"))

    result = create("--yes", input="n\n")

    assert result.exit_code == 0, result.output
    assert list_additions() == []
    assert "allow nodejs?" in result.output  # it was asked, and --yes did not answer it


def test_a_refusal_is_explained_in_the_cli(world) -> None:
    world.use(missing("telnetd"), suggestion())

    result = create("--yes", input="")

    assert "refused   telnetd: telnetd is on the list of packages that are never offered" in (
        result.output
    )
    assert list_additions() == []


def test_non_interactive_never_asks_and_reports_what_could_be_added(world) -> None:
    world.use(missing("nodejs"))

    result = create("--non-interactive", "--json")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["added"] == []
    assert [(p["package"], p["component"], p["base_version"]) for p in payload["addable"]] == [
        ("nodejs", "universe", "24.04")
    ]
    assert "addable   nodejs (universe 18.19.1): gira-iso allow add nodejs --base 24.04" in (
        result.stderr
    )
    assert list_additions() == []


def test_json_lists_what_was_added(world) -> None:
    world.use(missing(), suggestion("nodejs"), with_node())

    # Both questions (allow nodejs? build this image?) go to stderr; stdout stays pure JSON.
    result = create("--json", input="y\ny\n")

    assert result.exit_code == 0, result.output
    # CliRunner copies the typed answers into stdout (a real terminal echoes them itself), so
    # the JSON is read from its first brace; nothing the program printed comes before it.
    payload = json.loads(result.stdout[result.stdout.index("{") :])
    assert payload["added"] == ["nodejs"] and payload["addable"] == []
    assert "allow nodejs?" in result.stderr  # the question itself went to stderr


# --- menu ----------------------------------------------------------------------------------


def menu(*answers: str):
    return runner.invoke(app, [], input="\n".join(("k", "4", "", *answers)) + "\n")


def test_the_menu_shows_the_table_asks_and_builds_with_the_new_package(world, tmp_path) -> None:
    world.use(missing(), suggestion("nodejs"), with_node())

    result = menu("nginx y node", "y", "web", "y", "n", str(tmp_path), "0")

    assert result.exit_code == 0, result.output
    assert "Paquetes reales de Ubuntu" in result.output
    assert "universe" in result.output and "18.19.1" in result.output
    assert "como root" in result.output
    assert [a.package for a in list_additions()] == ["nodejs"]
    assert world.built[0].packages == ["nginx", "nodejs"]
    assert "nodejs" in result.output  # the second interpretation lists it under "Lo que entendí"


def test_declining_in_the_menu_builds_without_it(world, tmp_path) -> None:
    world.use(missing(), suggestion("nodejs"))

    result = menu("nginx y node", "n", "web", "y", "n", str(tmp_path), "0")

    assert result.exit_code == 0, result.output
    assert list_additions() == []
    assert world.built[0].packages == ["nginx"]


def test_the_menu_explains_a_package_that_is_held_back(world, tmp_path) -> None:
    world.use(missing("telnetd"), suggestion())

    result = menu("nginx y telnetd", "web", "y", "n", str(tmp_path), "0")

    assert "No se pueden agregar" in result.output
    assert "telnetd" in result.output and "never offered" in result.output


def test_the_menu_does_not_ask_when_nothing_is_missing(world, tmp_path) -> None:
    world.use(json.dumps({"kind": "spec", "spec": {"packages": ["nginx"]}}))

    result = menu("solo nginx", "web", "y", "n", str(tmp_path), "0")

    assert "Paquetes reales de Ubuntu" not in result.output
    assert world.model.calls == 1
