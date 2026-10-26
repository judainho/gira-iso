"""Preferences remembered between runs: last image name and destination.

Convenience only. The API key is never stored here, and a missing or damaged file
silently falls back to the defaults.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

import yaml

from gira_iso.bases import home


@dataclass(frozen=True)
class Settings:
    last_name: str | None = None
    last_destination: str | None = None


def path() -> Path:
    return home() / "settings.yaml"


def load() -> Settings:
    try:
        data = yaml.safe_load(path().read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return Settings()
    if not isinstance(data, dict):
        return Settings()
    return Settings(
        last_name=data.get("last_name") if isinstance(data.get("last_name"), str) else None,
        last_destination=(
            data.get("last_destination") if isinstance(data.get("last_destination"), str) else None
        ),
    )


def save(settings: Settings) -> None:
    target = path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(yaml.safe_dump(asdict(settings), sort_keys=False), encoding="utf-8")
    except OSError:
        pass  # remembering is a nicety; never fail an image over it
