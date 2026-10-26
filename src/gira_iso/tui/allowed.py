"""Menu option 5: see, add and remove the packages allowed on this machine."""

from __future__ import annotations

from datetime import UTC, datetime

from rich.console import Console
from rich.prompt import Confirm, Prompt
from rich.table import Table

from gira_iso import archive
from gira_iso.additions import (
    Addition,
    add_addition,
    list_additions,
    remove_addition,
    strict_mode,
)
from gira_iso.archive import ArchiveUnavailable, UnknownRelease
from gira_iso.policy.evaluate import load_allowlist
from gira_iso.proposals import Refusal
from gira_iso.tui.bases import choose_base
from gira_iso.tui.common import ACCENT, section
from gira_iso.tui.views import show_proposals
from gira_iso.workflow.additions import check_package


def allowed_packages(console: Console) -> None:
    section(console, "Paquetes permitidos")
    strict = strict_mode()
    entries = list_additions(include_inactive=True)
    official = len(load_allowlist().packages)

    console.print(
        f"[dim]Oficiales: {official} paquetes revisados e incluidos con la herramienta. "
        f"Agregados en este equipo: {len(entries)}.[/]"
    )
    if strict:
        console.print(
            "[yellow]Modo estricto:[/] las adiciones locales no se aplican ni se pueden cambiar "
            "(GIRA_ISO_STRICT, o una lista de la organización en GIRA_ISO_ALLOWLIST)."
        )
    if entries:
        table = Table(border_style="dim", header_style=f"bold {ACCENT}")
        for column in ("#", "Paquete", "Ubuntu", "Componente", "Versión", "Agregado"):
            table.add_column(column)
        for index, entry in enumerate(entries, start=1):
            table.add_row(
                str(index),
                entry.package,
                entry.base_version,
                entry.component,
                entry.version,
                entry.added_at[:10],
            )
        console.print(table)
    else:
        console.print("[dim]Todavía no agregaste ningún paquete.[/]")
    if strict:
        return

    hint = "un número para quitarlo, " if entries else ""
    choice = Prompt.ask(
        f"Escribe {hint}«agregar» para sumar uno (Enter para volver)",
        default="",
        show_default=False,
        console=console,
    ).strip()
    if choice == "agregar":
        _add_one(console)
    elif choice.isdigit() and 1 <= int(choice) <= len(entries):
        _remove_one(console, entries[int(choice) - 1])
    elif choice:
        console.print("[yellow]No entendí esa opción.[/]")


def _remove_one(console: Console, entry: Addition) -> None:
    if not Confirm.ask(
        f"¿Quitar [bold]{entry.package}[/] (Ubuntu {entry.base_version}) de la lista?",
        default=False,
        console=console,
    ):
        return
    remove_addition(entry.package, entry.base_version)
    console.print(f"[green]✔ Quitado:[/] {entry.package}")


def _add_one(console: Console) -> None:
    name = Prompt.ask("Nombre del paquete de Ubuntu", console=console).strip().lower()
    if not name:
        return
    _, version = choose_base(console)
    try:
        with console.status("Consultando el archivo de Ubuntu…", spinner="dots"):
            verdict = check_package(
                name,
                version,
                load_allowlist(),
                lookup=lambda package, release: archive.lookup(package, release),
            )
    except (ArchiveUnavailable, UnknownRelease) as exc:
        console.print(f"[red]✘ {exc}[/]")
        return
    if isinstance(verdict, Refusal):
        console.print(f"[red]✘ No se puede agregar:[/] {verdict.reason}")
        return
    show_proposals(console, [verdict], [])
    if not Confirm.ask(
        f"¿Agregar [bold]{verdict.package}[/] a la lista permitida (Ubuntu {version})?",
        default=False,
        console=console,
    ):
        return
    add_addition(
        Addition(
            package=verdict.package,
            base_version=verdict.base_version,
            component=verdict.component,
            version=verdict.version,
            added_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
    )
    console.print(f"[green]✔ Agregado:[/] {verdict.package}")
