"""`gira-iso allow`: add, list and remove, with the archive faked."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from gira_iso.additions import STRICT_ENV, list_additions
from gira_iso.archive import ArchivePackage, ArchiveUnavailable
from gira_iso.cli import app

runner = CliRunner()

ARCHIVE = {
    "nodejs": ArchivePackage("nodejs", "18.19.1", "universe", "web", "Release"),
    "jq2": ArchivePackage("jq2", "1.0", "main", "utils", "Release"),
    "nonfree": ArchivePackage("nonfree", "2.0", "multiverse", "misc", "Release"),
    "telnetd": ArchivePackage("telnetd", "0.17", "universe", "net", "Release"),
}


@pytest.fixture
def archive(monkeypatch):
    calls: list[tuple[str, str]] = []

    def fake_lookup(package, base_version, fetch=None):
        calls.append((package, base_version))
        return ARCHIVE.get(package)

    monkeypatch.setattr("gira_iso.cli.allow.lookup", fake_lookup)
    return calls


def test_a_confirmed_package_is_allowed_and_stored(archive) -> None:
    result = runner.invoke(app, ["allow", "add", "nodejs"], input="y\n")

    assert result.exit_code == 0, result.output
    assert "found     nodejs 18.19.1 (universe, web) in ubuntu 24.04" in result.output
    assert "como root" in result.output
    assert "allowed   nodejs (universe 18.19.1, ubuntu 24.04)" in result.output
    assert [(a.package, a.base_version) for a in list_additions()] == [("nodejs", "24.04")]


def test_declining_stores_nothing(archive) -> None:
    result = runner.invoke(app, ["allow", "add", "nodejs"], input="n\n")

    assert result.exit_code == 0
    assert "skipped   nodejs" in result.output
    assert list_additions() == []


def test_the_default_answer_is_no(archive) -> None:
    result = runner.invoke(app, ["allow", "add", "nodejs"], input="\n")

    assert list_additions() == [] and "skipped" in result.output


def test_yes_skips_the_question(archive) -> None:
    result = runner.invoke(app, ["allow", "add", "nodejs", "--yes"])

    assert result.exit_code == 0 and len(list_additions()) == 1


def test_the_release_can_be_chosen(archive) -> None:
    runner.invoke(app, ["allow", "add", "nodejs", "--base", "22.04", "-y"])

    assert archive == [("nodejs", "22.04")]
    assert list_additions()[0].base_version == "22.04"


def test_a_package_ubuntu_does_not_publish_is_refused_with_exit_1(archive) -> None:
    result = runner.invoke(app, ["allow", "add", "opencode", "-y"])

    assert result.exit_code == 1
    assert (
        "refused   opencode: ubuntu 24.04 does not publish a package named opencode"
        in result.output
    )
    assert list_additions() == []


def test_non_free_components_are_refused(archive) -> None:
    result = runner.invoke(app, ["allow", "add", "nonfree", "-y"])

    assert result.exit_code == 1 and "multiverse" in result.output and list_additions() == []


def test_the_never_list_is_refused_without_asking_the_archive(archive) -> None:
    result = runner.invoke(app, ["allow", "add", "telnetd", "-y"])

    assert result.exit_code == 1
    assert "never offered" in result.output
    assert archive == []  # settled by the rules, no network spent


def test_an_already_allowed_package_is_refused_without_asking_the_archive(archive) -> None:
    result = runner.invoke(app, ["allow", "add", "nginx", "-y"])

    assert result.exit_code == 1 and "already allowed" in result.output and archive == []


def test_one_refusal_does_not_stop_the_others(archive) -> None:
    result = runner.invoke(app, ["allow", "add", "opencode", "nodejs", "-y"])

    assert result.exit_code == 1
    assert [a.package for a in list_additions()] == ["nodejs"]


def test_repeated_names_are_checked_once(archive) -> None:
    runner.invoke(app, ["allow", "add", "nodejs", "nodejs", "-y"])

    assert archive == [("nodejs", "24.04")]


def test_more_than_five_at_once_is_refused_before_any_lookup(archive) -> None:
    names = [f"pkg{i}" for i in range(6)]

    result = runner.invoke(app, ["allow", "add", *names, "-y"])

    assert result.exit_code == 2 and "at most 5" in result.output and archive == []


def test_strict_mode_refuses_before_spending_any_network(monkeypatch, archive) -> None:
    monkeypatch.setenv(STRICT_ENV, "1")

    result = runner.invoke(app, ["allow", "add", "nodejs", "-y"])

    assert result.exit_code == 1
    assert "strict mode is on" in result.output
    assert archive == []


def test_an_unreachable_archive_is_exit_3_and_not_a_refusal(monkeypatch) -> None:
    def down(package, base_version, fetch=None):
        raise ArchiveUnavailable("could not ask the Ubuntu archive (Launchpad): offline")

    monkeypatch.setattr("gira_iso.cli.allow.lookup", down)

    result = runner.invoke(app, ["allow", "add", "nodejs", "-y"])

    assert result.exit_code == 3
    assert "refused" not in result.output and list_additions() == []


def test_a_base_without_a_release_name_is_an_input_error() -> None:
    result = runner.invoke(app, ["allow", "add", "nodejs", "--base", "26.10", "-y"])

    assert result.exit_code == 2 and "no known release name" in result.output


# --- list and remove ------------------------------------------------------------------


def test_list_says_when_nothing_was_added() -> None:
    result = runner.invoke(app, ["allow", "list"])

    assert result.exit_code == 0
    assert "packaged  42 package(s)" in result.output
    assert "local     0 package(s), active" in result.output


def test_list_shows_what_was_added(archive) -> None:
    runner.invoke(app, ["allow", "add", "nodejs", "-y"])

    result = runner.invoke(app, ["allow", "list"])

    assert "local     1 package(s), active" in result.output
    assert "nodejs" in result.output and "universe 18.19.1" in result.output


def test_list_json(archive) -> None:
    runner.invoke(app, ["allow", "add", "nodejs", "-y"])

    rows = json.loads(runner.invoke(app, ["allow", "list", "--json"]).stdout)

    assert [(r["package"], r["base_version"], r["component"]) for r in rows] == [
        ("nodejs", "24.04", "universe")
    ]


def test_list_marks_additions_as_inactive_in_strict_mode(monkeypatch, archive) -> None:
    runner.invoke(app, ["allow", "add", "nodejs", "-y"])
    monkeypatch.setenv(STRICT_ENV, "1")

    result = runner.invoke(app, ["allow", "list"])

    assert "inactive (strict mode)" in result.output and "nodejs" in result.output


def test_remove_stops_allowing_a_package(archive) -> None:
    runner.invoke(app, ["allow", "add", "nodejs", "-y"])

    result = runner.invoke(app, ["allow", "remove", "nodejs"])

    assert result.exit_code == 0 and "removed   nodejs (ubuntu 24.04)" in result.output
    assert list_additions() == []


def test_removing_what_was_never_added_is_an_error() -> None:
    result = runner.invoke(app, ["allow", "remove", "nodejs"])

    assert result.exit_code == 2 and "not among the packages you added" in result.output


def test_allow_appears_in_the_main_help() -> None:
    assert "allow" in runner.invoke(app, ["--help"]).output
