"""Menu option 1: list the images, then show, export or delete one."""

from __future__ import annotations

from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text

from gira_iso import settings
from gira_iso.bases import builds_dir
from gira_iso.builder.catalog import directory_size, find_incomplete, scan_builds
from gira_iso.builder.export import (
    ExportError,
    ExportExists,
    delete_build,
    describe,
    export_image,
    remove_incomplete,
)
from gira_iso.builder.manifest import Manifest
from gira_iso.naming import is_valid_image_name
from gira_iso.tui.common import ACCENT, ask_directory, remembered_directory, section


def list_images(console: Console) -> None:
    section(console, "Imágenes existentes")
    builds = scan_builds(builds_dir())
    incomplete = find_incomplete(builds_dir())
    if not builds and not incomplete:
        console.print(f"[yellow]Todavía no hay imágenes[/] en {builds_dir()}")
        return
    if not builds:
        console.print(f"[yellow]Todavía no hay imágenes[/] en {builds_dir()}")
    table = Table(border_style="dim", header_style=f"bold {ACCENT}")
    for column in ("#", "Nombre", "Creada (UTC)", "Disco", "Base", "Build"):
        table.add_column(column)
    for index, (manifest, manifest_path) in enumerate(builds, start=1):
        build_dir = manifest_path.parent
        table.add_row(
            str(index),
            manifest.name or build_dir.name,
            manifest.created_at.strftime("%Y-%m-%d %H:%M"),
            f"{manifest.artifact.virtual_size_bytes / 1024**3:.1f} GB",
            f"{manifest.base.os} {manifest.base.version}",
            build_dir.name,
        )
    if builds:
        console.print(table)
    console.print(f"[dim]Ubicación: {builds_dir()}[/]")

    hint = ""
    if incomplete:
        total = sum(directory_size(directory) for directory in incomplete)
        console.print(
            f"[yellow]Hay {len(incomplete)} build(s) incompleto(s) "
            f"({total / 1024**2:.0f} MB).[/] Escribe [bold]limpiar[/] para borrarlos."
        )
        hint = " o limpiar"
    choice = Prompt.ask(
        f"Número de imagen para ver, exportar o eliminar{hint} (Enter para volver)",
        default="",
        show_default=False,
        console=console,
    ).strip()
    if not choice:
        return
    if choice == "limpiar" and incomplete:
        removed = remove_incomplete(builds_dir())
        freed = sum(size for _, size in removed)
        console.print(
            f"[green]✔ Borrados {len(removed)} build(s); {freed / 1024**2:.0f} MB libres[/]"
        )
        return
    if not (choice.isdigit() and 1 <= int(choice) <= len(builds)):
        console.print("[yellow]Número fuera de rango.[/]")
        return
    manifest, manifest_path = builds[int(choice) - 1]
    _image_actions(console, manifest, manifest_path)


def _image_actions(console: Console, manifest: Manifest, manifest_path: Path) -> None:
    build_dir = manifest_path.parent
    label = manifest.name or build_dir.name
    while True:
        action = Prompt.ask(
            f"¿Qué hago con [bold]{label}[/]?",
            choices=["detalle", "exportar", "eliminar", "volver"],
            default="detalle",
            console=console,
        )
        if action == "volver":
            return
        if action == "detalle":
            table = Table.grid(padding=(0, 2))
            for key, value in describe(manifest, build_dir):
                table.add_row(f"[dim]{key}[/]", Text(value))
            console.print(Panel(table, title=label, border_style=ACCENT))
        elif action == "exportar":
            _export_from_menu(console, manifest, manifest_path, label)
        elif _delete_from_menu(console, manifest_path, label):
            return


def _export_from_menu(
    console: Console, manifest: Manifest, manifest_path: Path, label: str
) -> None:
    remembered = settings.load()
    destination = ask_directory(
        console, "¿En qué carpeta la copio?", remembered_directory(remembered.last_destination)
    )
    safe_label = label if is_valid_image_name(label) else manifest_path.parent.name
    try:
        try:
            image, manifest_copy = export_image(manifest, manifest_path, destination, safe_label)
        except ExportExists:
            if not Confirm.ask("Ya existe. ¿Sobrescribir?", default=False, console=console):
                return
            image, manifest_copy = export_image(
                manifest, manifest_path, destination, safe_label, force=True
            )
    except ExportError as exc:
        console.print(f"[red]✘ {exc}[/]")
        return
    console.print(Text.assemble(("Imagen    ", "dim"), (str(image), "bold green")), soft_wrap=True)
    console.print(Text.assemble(("Manifest  ", "dim"), str(manifest_copy)), soft_wrap=True)


def _delete_from_menu(console: Console, manifest_path: Path, label: str) -> bool:
    """Deleting is irreversible: the user must type the name. Returns True when deleted."""
    typed = Prompt.ask(f"Escribe [bold]{label}[/] para confirmar (Enter cancela)", default="")
    if typed.strip() != label:
        console.print("[dim]No se eliminó nada.[/]")
        return False
    try:
        removed = delete_build(manifest_path, builds_dir())
    except ExportError as exc:
        console.print(f"[red]✘ {exc}[/]")
        return False
    console.print(f"[green]✔ Eliminada:[/] {removed}")
    return True
