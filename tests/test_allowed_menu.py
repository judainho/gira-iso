"""Menu option 5: the packages allowed on this machine."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from gira_iso.additions import STRICT_ENV, Addition, add_addition, list_additions
from gira_iso.archive import ArchivePackage, ArchiveUnavailable
from gira_iso.cli import app
from gira_iso.nl.client import CredentialCheck

runner = CliRunner()

ARCHIVE = {
    "nodejs": ArchivePackage("nodejs", "18.19.1", "universe", "web", "Release"),
    "nonfree": ArchivePackage("nonfree", "1.0", "multiverse", "misc", "Release"),
}


@pytest.fixture(autouse=True)
def _menu(monkeypatch):
    monkeypatch.setattr("gira_iso.tui.app.check_credentials", lambda c: CredentialCheck("ok"))
    monkeypatch.setenv("GIRA_ISO_LLM_API_KEY", "key")
    monkeypatch.setattr(
        "gira_iso.archive.lookup", lambda package, version, fetch=None: ARCHIVE.get(package)
    )


def stored(package: str = "nodejs", base: str = "24.04") -> Addition:
    return Addition(
        package=package,
        base_version=base,
        component="universe",
        version="18.19.1",
        added_at="2026-10-26T12:00:00Z",
    )


def menu(*answers: str):
    return runner.invoke(app, [], input="\n".join(("5", *answers, "0")) + "\n")


def test_option_5_is_in_the_menu() -> None:
    assert "Paquetes permitidos" in menu("").output


def test_with_nothing_added_it_says_so() -> None:
    result = menu("")

    flat = " ".join(result.output.split())
    assert "Oficiales: 42 paquetes" in flat
    assert "Todavía no agregaste ningún paquete" in flat


def test_it_lists_what_was_added_here() -> None:
    add_addition(stored())

    result = menu("")

    flat = " ".join(result.output.split())  # the 80-column console wraps long lines
    assert "Agregados en este equipo: 1" in flat
    assert "nodejs" in flat and "universe" in flat
    assert "un número para quitarlo" in flat


def test_a_package_can_be_added_from_the_menu() -> None:
    result = menu("agregar", "nodejs", "", "y")  # name, default base, approve

    assert result.exit_code == 0, result.output
    assert "Paquetes reales de Ubuntu" in result.output
    assert "Agregado:" in result.output
    assert [(a.package, a.base_version) for a in list_additions()] == [("nodejs", "24.04")]


def test_declining_adds_nothing() -> None:
    menu("agregar", "nodejs", "", "n")

    assert list_additions() == []


def test_a_refused_package_is_explained_and_not_added() -> None:
    result = menu("agregar", "nonfree", "")

    assert "No se puede agregar" in result.output and "multiverse" in result.output
    assert list_additions() == []


def test_one_the_archive_does_not_publish_is_refused() -> None:
    result = menu("agregar", "opencode", "")

    assert "does not publish a package named opencode" in result.output
    assert list_additions() == []


def test_an_unreachable_archive_is_reported_without_crashing(monkeypatch) -> None:
    def down(package, version, fetch=None):
        raise ArchiveUnavailable("could not ask the Ubuntu archive (Launchpad): offline")

    monkeypatch.setattr("gira_iso.archive.lookup", down)

    result = menu("agregar", "nodejs", "")

    assert result.exit_code == 0 and "could not ask the Ubuntu archive" in result.output
    assert list_additions() == []


def test_an_empty_name_returns_to_the_menu() -> None:
    result = menu("agregar", "")

    assert result.exit_code == 0 and list_additions() == []


def test_a_package_can_be_removed_by_its_number() -> None:
    add_addition(stored())

    result = menu("1", "y")

    assert "Quitado:" in result.output and list_additions() == []


def test_removing_asks_first_and_defaults_to_no() -> None:
    add_addition(stored())

    menu("1", "")

    assert len(list_additions()) == 1


def test_a_number_out_of_range_is_not_a_removal() -> None:
    add_addition(stored())

    result = menu("7")

    assert "No entendí esa opción" in result.output and len(list_additions()) == 1


def test_in_strict_mode_it_only_shows(monkeypatch) -> None:
    add_addition(stored())
    monkeypatch.setenv(STRICT_ENV, "1")

    result = menu()

    assert "Modo estricto" in result.output
    assert "nodejs" in result.output  # visible, so a person can see what is being ignored
    assert "«agregar»" not in result.output  # nothing can be changed
    assert len(list_additions(include_inactive=True)) == 1
