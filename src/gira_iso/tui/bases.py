"""Menu options 2 and 3: register your own base image and see or download the official ones."""

from __future__ import annotations

from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text

from gira_iso.bases import UserBaseError, list_user_bases, register_base
from gira_iso.builder.base import BaseImageError, cache_dir, ensure_base_image
from gira_iso.policy.evaluate import load_allowlist
from gira_iso.tui.common import ACCENT, DownloadBar, section

PENDING_STATE = "se descarga al usarla"


def _base_rows() -> list[tuple[str, str, str, str, str]]:
    """(origin, os, version, state, sha256) for every base the policy would accept."""
    allowlist = load_allowlist()
    user = {(b.os, b.version) for b in list_user_bases()}
    rows = []
    for os_name, versions in sorted(allowlist.base_images.items()):
        for version, reference in sorted(versions.items()):
            if (os_name, version) in user:
                state, origin = "en disco", "subida"
            else:
                cached = cache_dir() / reference["url"].rsplit("/", 1)[-1]
                state = "en caché" if cached.exists() else PENDING_STATE
                origin = "oficial"
            rows.append((origin, os_name, version, state, reference["sha256"]))
    return rows


def list_bases(console: Console) -> None:
    section(console, "Imágenes base")
    rows = _base_rows()
    table = Table(border_style="dim", header_style=f"bold {ACCENT}")
    for column in ("#", "Origen", "SO", "Versión", "Estado", "sha256"):
        table.add_column(column)
    for index, (origin, os_name, version, state, sha) in enumerate(rows, start=1):
        table.add_row(str(index), origin, os_name, version, state, f"{sha[:16]}…")
    console.print(table)

    pending = [(index, row) for index, row in enumerate(rows, start=1) if row[3] == PENDING_STATE]
    if not pending:
        return
    choice = Prompt.ask(
        "Número de una base para descargarla ahora (Enter para volver)",
        default="",
        show_default=False,
        console=console,
    ).strip()
    if not choice:
        return
    match = next((row for index, row in pending if str(index) == choice), None)
    if match is None:
        console.print("[yellow]Esa base no necesita descarga.[/]")
        return
    _download_official(console, match[1], match[2])


def _download_official(console: Console, os_name: str, version: str) -> None:
    bar = DownloadBar(console)
    try:
        path, sha = ensure_base_image(os_name, version, load_allowlist(), on_progress=bar)
    except KeyboardInterrupt:
        console.print("\n[yellow]Descarga cancelada.[/]")
        return
    except (BaseImageError, OSError) as exc:
        console.print(f"[red]✘ {exc}[/]")
        return
    finally:
        bar.stop()
    console.print(f"[green]✔ {os_name} {version} descargada y verificada[/]")
    console.print(Text.assemble(("sha256  ", "dim"), sha), soft_wrap=True)
    console.print(Text.assemble(("archivo ", "dim"), str(path)), soft_wrap=True)


def upload_base(console: Console) -> None:
    section(console, "Subir imagen base")
    console.print(
        Panel(
            "Una base debe ser una [bold]cloud image qcow2[/] de Ubuntu "
            "([i]ubuntu-XX.XX-server-cloudimg-amd64.img[/]).\n"
            "Un instalador [bold]ISO[/] (live-server, desktop) [red]no sirve[/]: "
            "no trae cloud-init ni un disco listo para arrancar.\n"
            "Se fija por el sha256 calculado ahora; la política sigue validando el spec.",
            border_style="dim",
        )
    )
    raw = Prompt.ask("Ruta del archivo", console=console).strip().strip("'\"")
    version = Prompt.ask("Versión de Ubuntu (por ejemplo 22.04)", console=console).strip()
    name = Prompt.ask("Nombre descriptivo", default=Path(raw).name, console=console)
    try:
        with console.status("Verificando formato y calculando sha256…", spinner="dots"):
            base = register_base(
                Path(raw), version, builtin=load_allowlist().base_images, name=name
            )
    except UserBaseError as exc:
        console.print(f"[red]✘ {exc}[/]")
        return
    console.print(f"[green]✔ Base registrada:[/] ubuntu {base.version}")
    console.print(f"  [dim]sha256[/] {base.sha256}")
    console.print(f"  [dim]copia [/] {base.path}")


def choose_base(console: Console) -> tuple[str, str]:
    rows = _base_rows()
    if len(rows) == 1:
        return rows[0][1], rows[0][2]
    table = Table(border_style="dim", header_style=f"bold {ACCENT}")
    for column in ("#", "Origen", "SO", "Versión", "Estado"):
        table.add_column(column)
    for index, (origin, os_name, version, state, _) in enumerate(rows, start=1):
        table.add_row(str(index), origin, os_name, version, state)
    console.print(table)
    # Default to the version the pipeline was developed and tested against.
    tested = next((i for i, row in enumerate(rows, 1) if row[1:3] == ("ubuntu", "24.04")), 1)
    choice = Prompt.ask(
        "Imagen base",
        choices=[str(i) for i in range(1, len(rows) + 1)],
        default=str(tested),
        console=console,
    )
    _, os_name, version, _, _ = rows[int(choice) - 1]
    return os_name, version
