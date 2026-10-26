"""The `gira-iso` command line. `app` is the entry point declared in pyproject.toml."""

# Importing the modules registers their commands on `app`.
from gira_iso.cli import advanced, allow, create, images  # noqa: F401, E402, I001
from gira_iso.cli._app import app

__all__ = ["app"]
