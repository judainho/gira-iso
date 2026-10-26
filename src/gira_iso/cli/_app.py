"""The Typer app and the two things every command module shares: the app and its help text."""

from __future__ import annotations

import typer

from gira_iso import __version__

WORK_ROOT_HELP = "Where builds are written (default: <gira-iso home>/builds)"


app = typer.Typer(
    add_completion=False,
    help="Build reproducible cloud-ready images from a declarative spec.",
    epilog=(
        "Advanced commands (hidden from this list): check, plan, build, verify, eval, policy, push."
    ),
)


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    """With no command, open the interactive menu."""
    if ctx.invoked_subcommand is not None:
        return
    from gira_iso.tui import run

    run()


@app.command()
def version() -> None:
    """Print the gira-iso version."""
    typer.echo(f"gira-iso {__version__}")
