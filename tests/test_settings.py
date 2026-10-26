from __future__ import annotations

from gira_iso import settings
from gira_iso.settings import Settings


def test_defaults_when_nothing_was_saved() -> None:
    assert settings.load() == Settings()


def test_roundtrip() -> None:
    settings.save(Settings(last_name="web", last_destination="/tmp/out"))

    assert settings.load() == Settings(last_name="web", last_destination="/tmp/out")


def test_a_damaged_file_falls_back_to_defaults() -> None:
    settings.path().parent.mkdir(parents=True, exist_ok=True)
    settings.path().write_text("{ not: [valid", encoding="utf-8")

    assert settings.load() == Settings()


def test_wrong_types_are_ignored() -> None:
    settings.path().parent.mkdir(parents=True, exist_ok=True)
    settings.path().write_text("last_name: 5\nlast_destination: [a]\n", encoding="utf-8")

    assert settings.load() == Settings()


def test_the_api_key_is_never_part_of_the_saved_fields() -> None:
    assert set(Settings.__dataclass_fields__) == {"last_name", "last_destination"}
