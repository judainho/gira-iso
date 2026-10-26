"""`gira-iso allow`: extend the allowlist, one checked and confirmed package at a time.

This is the manual path: a person names a package, the archive says whether Ubuntu publishes it,
the rules decide whether it may be offered, and the person says yes or no. The model is not
involved. The interactive create flow reaches the same decision through `Io.propose`.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime

import typer

from gira_iso.additions import (
    Addition,
    add_addition,
    ensure_enabled,
    list_additions,
    remove_addition,
    strict_mode,
)
from gira_iso.archive import lookup
from gira_iso.cli._app import app
from gira_iso.cli._shared import guarded
from gira_iso.errors import EXIT_INVALID, EXIT_OK, EXIT_REJECTED
from gira_iso.policy.evaluate import load_allowlist
from gira_iso.proposals import MAX_PROPOSALS, WARNING, Proposal, Refusal, assess, precheck

allow_app = typer.Typer(
    add_completion=False,
    help="Extend the allowlist with packages Ubuntu publishes (you approve each one).",
)
app.add_typer(allow_app, name="allow")

BASE_HELP = "Ubuntu release the package is checked against and allowed for"


@allow_app.command("add")
@guarded
def add(
    packages: list[str] = typer.Argument(..., help="Debian package names"),
    base: str = typer.Option("24.04", "--base", help=BASE_HELP),
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask for confirmation"),
) -> None:
    """Check packages against the Ubuntu archive and, if you agree, allow them."""
    ensure_enabled()  # strict mode: refuse before spending any network
    if len(packages) > MAX_PROPOSALS:
        typer.echo(f"error: at most {MAX_PROPOSALS} packages at a time", err=True)
        raise typer.Exit(EXIT_INVALID)

    allowlist = load_allowlist()
    refused = False
    for name in dict.fromkeys(packages):  # in order, without repeats
        settled = precheck(name, base, allowlist)  # no network for what is already settled
        if settled is not None:
            typer.echo(f"  refused   {name}: {settled.reason}")
            refused = True
            continue
        found = lookup(name, base)
        verdict = assess(name, found, base, allowlist)
        if isinstance(verdict, Refusal):
            typer.echo(f"  refused   {name}: {verdict.reason}")
            refused = True
            continue
        if not _approved(verdict, yes):
            typer.echo(f"  skipped   {name}")
            continue
        add_addition(_addition(verdict))
        typer.echo(
            f"  allowed   {verdict.package} ({verdict.component} {verdict.version}, "
            f"ubuntu {verdict.base_version})"
        )
    if refused:
        raise typer.Exit(EXIT_REJECTED)


def _approved(proposal: Proposal, yes: bool) -> bool:
    typer.echo(
        f"  found     {proposal.package} {proposal.version} "
        f"({proposal.component}, {proposal.section}) in ubuntu {proposal.base_version}"
    )
    typer.echo(f"            {WARNING}")
    return yes or typer.confirm(f"allow {proposal.package}?", default=False)


def _addition(proposal: Proposal) -> Addition:
    return Addition(
        package=proposal.package,
        base_version=proposal.base_version,
        component=proposal.component,
        version=proposal.version,
        added_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


@allow_app.command("list")
@guarded
def list_(
    json_output: bool = typer.Option(False, "--json", help="Print the additions as JSON"),
) -> None:
    """Show the packages allowed on this machine in addition to the packaged list."""
    entries = list_additions(include_inactive=True)
    if json_output:
        typer.echo(json.dumps([asdict(entry) for entry in entries], indent=2))
        return
    packaged = len(load_allowlist().packages)
    state = "inactive (strict mode)" if strict_mode() else "active"
    typer.echo(f"  packaged  {packaged} package(s)")
    typer.echo(f"  local     {len(entries)} package(s), {state}")
    for entry in entries:
        typer.echo(
            f"    {entry.package:<28} ubuntu {entry.base_version}  "
            f"{entry.component} {entry.version}  ({entry.added_at[:10]})"
        )


@allow_app.command("remove")
@guarded
def remove(
    package: str = typer.Argument(..., help="Package to stop allowing"),
    base: str = typer.Option(None, "--base", help="Only for this Ubuntu release (default: all)"),
) -> None:
    """Stop allowing a package you added."""
    removed = remove_addition(package, base)
    if not removed:
        typer.echo(f"error: {package} is not among the packages you added", err=True)
        raise typer.Exit(EXIT_INVALID)
    for entry in removed:
        typer.echo(f"  removed   {entry.package} (ubuntu {entry.base_version})")
    raise typer.Exit(EXIT_OK)
