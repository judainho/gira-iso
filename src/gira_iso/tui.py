"""Interactive front end: banner, API key, and a four-option menu.

Everything here is presentation. The work is done by the same functions the scripted
commands use, so the menu cannot build anything the policy would reject.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml
from rich.console import Console, Group
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TaskID,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.prompt import Confirm, Prompt
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from gira_iso import __version__, settings
from gira_iso.bases import UserBaseError, builds_dir, list_user_bases, register_base
from gira_iso.builder import packer
from gira_iso.builder.base import BaseImageError, cache_dir, ensure_base_image
from gira_iso.builder.build import BuildRejected, build_image
from gira_iso.builder.export import (
    ExportError,
    ExportExists,
    delete_build,
    describe,
    export_image,
    scan_builds,
)
from gira_iso.builder.manifest import Manifest
from gira_iso.nl.client import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    LlmConfig,
    OpenAiCompatible,
    check_credentials,
)
from gira_iso.nl.session import converse
from gira_iso.policy.evaluate import load_allowlist
from gira_iso.spec.canonical import spec_hash, to_canonical_dict
from gira_iso.spec.models import BaseImage, Spec
from gira_iso.verify.verify import VerificationError, verify_build

AUTHOR = "Manuel Judá Aliaga Aliaga"
EMAIL = "maliagaa@pucp.edu.pe"
ORG = "GIRA PUCP"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
MAX_QUESTION_ROUNDS = 3

MAX_KEY_ATTEMPTS = 3
PENDING_STATE = "se descarga al usarla"
EXAMPLES = (
    ("Servidor web", "Un servidor web con nginx y un usuario deploy con sudo"),
    ("Docker", "Una imagen con docker instalado y htop"),
    (
        "Servidor con configuración",
        "Un servidor nginx con una página de health en /etc/nginx/conf.d/health.conf "
        "que responda ok en el puerto 8080",
    ),
)

ACCENT = "cyan"
LOGO = r"""
  ██████╗ ██╗██████╗  █████╗       ██╗███████╗ ██████╗
 ██╔════╝ ██║██╔══██╗██╔══██╗      ██║██╔════╝██╔═══██╗
 ██║  ███╗██║██████╔╝███████║█████╗██║███████╗██║   ██║
 ██║   ██║██║██╔══██╗██╔══██║╚════╝██║╚════██║██║   ██║
 ╚██████╔╝██║██║  ██║██║  ██║      ██║███████║╚██████╔╝
  ╚═════╝ ╚═╝╚═╝  ╚═╝╚═╝  ╚═╝      ╚═╝╚══════╝ ╚═════╝
"""


def default_downloads() -> Path:
    return Path.home() / "gira-iso-images"


def banner(console: Console) -> None:
    title = Text(LOGO.rstrip("\n"), style=f"bold {ACCENT}")
    credits = Text.assemble(
        ("Imágenes de máquina desde lenguaje natural\n", "italic"),
        (f"v{__version__}  ·  ", "dim"),
        (f"{AUTHOR}", "bold"),
        (f"  ·  {EMAIL}  ·  {ORG}", "dim"),
    )
    console.print(Panel(Group(title, credits), border_style=ACCENT, padding=(0, 2)))


def section(console: Console, title: str) -> None:
    console.print()
    console.rule(f"[bold {ACCENT}]{title}[/]", style=ACCENT)


def ask_api_key(console: Console) -> LlmConfig | None:
    """The key is the first thing asked for, hidden, validated, and never written anywhere."""
    base_url = os.environ.get("GIRA_ISO_LLM_BASE_URL", DEFAULT_BASE_URL)
    model = os.environ.get("GIRA_ISO_LLM_MODEL", DEFAULT_MODEL)
    console.print(f"[dim]Endpoint[/] {base_url}   [dim]Modelo[/] {model}")
    key = os.environ.get("GIRA_ISO_LLM_API_KEY", "").strip()
    if key:
        console.print("[dim]Usando la API key de GIRA_ISO_LLM_API_KEY[/]")

    for _ in range(MAX_KEY_ATTEMPTS):
        while not key:
            key = Prompt.ask("[bold]API Key[/]", password=True, console=console).strip()
        config = LlmConfig(base_url=base_url, api_key=key, model=model)
        with console.status("Validando la clave…", spinner="dots"):
            outcome = check_credentials(config)
        if outcome.status == "ok":
            console.print("[green]✔[/] API key válida (solo vive en memoria)\n")
            return config
        if outcome.status == "unverified":
            console.print(f"[yellow]![/] No pude verificar la clave: {outcome.detail}. Continúo.\n")
            return config
        console.print(f"[red]✘ {outcome.detail}.[/] Prueba con otra.")
        key = ""
    console.print("[red]Demasiados intentos con una clave inválida.[/]")
    return None


def _fmt_size(num: int) -> str:
    return f"{num / 1024**3:.1f} GB" if num >= 1024**3 else f"{num / 1024**2:.0f} MB"


def list_images(console: Console) -> None:
    section(console, "Imágenes existentes")
    builds = scan_builds(builds_dir())
    if not builds:
        console.print(f"[yellow]Todavía no hay imágenes[/] en {builds_dir()}")
        return
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
    console.print(table)
    console.print(f"[dim]Ubicación: {builds_dir()}[/]")

    choice = Prompt.ask(
        "Número de imagen para ver, exportar o eliminar (Enter para volver)",
        default="",
        show_default=False,
        console=console,
    ).strip()
    if not choice:
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
    destination = Path(
        Prompt.ask(
            "¿Dónde la copio?",
            default=remembered.last_destination or str(default_downloads()),
            console=console,
        )
    ).expanduser()
    safe_label = label if NAME_RE.match(label) else manifest_path.parent.name
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


def _choose_base(console: Console) -> tuple[str, str]:
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


def _interpret(
    console: Console, config: LlmConfig, request: str
) -> tuple[Spec, dict[str, int], int] | None:
    """Return (spec, token usage, latency ms) summed over every round, or None."""

    def answer(questions: list[str]) -> list[str]:
        console.print("[yellow]El modelo necesita más detalle:[/]")
        return [Prompt.ask(f"  {question}", console=console) for question in questions]

    result = converse(
        request,
        OpenAiCompatible(config),
        answer=answer,
        busy=lambda: console.status("Interpretando tu petición con el modelo…", spinner="dots"),
    )
    if result.spec is not None:
        return result.spec, result.usage, result.latency_ms
    if result.reason is not None:
        console.print(Panel(result.reason, title="Petición rechazada", border_style="red"))
    else:
        console.print(f"[red]✘ {result.error}[/]")
    return None


def _usage_line(usage: dict[str, int], latency_ms: int) -> str:
    parts = []
    if usage:
        parts.append(
            f"tokens: {usage.get('prompt_tokens', 0)} entrada · "
            f"{usage.get('completion_tokens', 0)} salida"
        )
    parts.append(f"{latency_ms / 1000:.1f} s")
    return "  ·  ".join(parts)


def _show_spec(console: Console, spec: Spec, usage_line: str = "") -> None:
    body = yaml.safe_dump(to_canonical_dict(spec), sort_keys=False).rstrip()
    console.print(
        Panel(
            Syntax(body, "yaml", theme="ansi_dark", background_color="default"),
            title=f"Spec interpretado  ·  {spec_hash(spec)[:12]}",
            subtitle=usage_line or None,
            border_style=ACCENT,
        )
    )


def _ask_request(console: Console) -> str:
    table = Table.grid(padding=(0, 2))
    for index, (title, text) in enumerate(EXAMPLES, start=1):
        table.add_row(f"[bold {ACCENT}]{index}[/]", f"[bold]{title}[/]  [dim]{text}[/]")
    console.print(
        Panel(table, title="Ejemplos (escribe el número para usarlos)", border_style="dim")
    )
    request = Prompt.ask("›", console=console).strip()
    if request.isdigit() and 1 <= int(request) <= len(EXAMPLES):
        request = EXAMPLES[int(request) - 1][1]
        console.print(f"[dim]Usando el ejemplo:[/] {request}")
    return request


class DownloadBar:
    """A transient progress bar fed by (done, total) callbacks; starts on the first one."""

    def __init__(self, console: Console) -> None:
        self._progress = Progress(
            TextColumn("  [dim]descarga[/]"),
            BarColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
            console=console,
            transient=True,
        )
        self._task: TaskID | None = None

    def __call__(self, done: int, total: int) -> None:
        if self._task is None:
            self._task = self._progress.add_task("download", total=total or None)
            self._progress.start()
        self._progress.update(self._task, completed=done, total=total or None)

    def stop(self) -> None:
        self._progress.stop()


def create_image(console: Console, config: LlmConfig) -> None:
    section(console, "Crear imagen")
    os_name, version = _choose_base(console)
    console.print("\n[bold]Describe la imagen que necesitas[/] [dim](español o inglés)[/]")
    request = _ask_request(console)
    if not request:
        console.print("[yellow]No escribiste nada.[/]")
        return

    interpreted = _interpret(console, config, request)
    if interpreted is None:
        return
    spec, usage, latency = interpreted
    # The base is the user's choice from the menu, not something the model decides.
    spec = spec.model_copy(update={"base": BaseImage(os=os_name, version=version)})
    _show_spec(console, spec, _usage_line(usage, latency))

    remembered = settings.load()
    name = Prompt.ask(
        "Nombre de la imagen", default=remembered.last_name or "mi-imagen", console=console
    )
    if not NAME_RE.match(name):
        console.print("[red]✘ Nombre inválido: usa letras, dígitos, '.', '_' o '-'.[/]")
        return
    if not Confirm.ask("¿Construir esta imagen?", default=True, console=console):
        return
    verify_after = Confirm.ask(
        "¿Arrancarla y verificarla al terminar? (más lento)", default=False, console=console
    )

    section(console, "Construcción")
    bar = DownloadBar(console)

    def on_progress(message: str) -> None:
        console.print(f"[bold {ACCENT}]▸[/] {message}")

    def on_line(line: str) -> None:
        # A Text, not markup: packer output is full of square brackets.
        console.print(Text(f"  │ {line}", style="dim"), soft_wrap=True)

    try:
        result = build_image(
            spec,
            work_root=builds_dir(),
            name=name,
            progress=on_progress,
            on_line=on_line,
            on_download=bar,
        )
    except BuildRejected as rejected:
        bar.stop()
        table = Table(title="La política rechazó el spec", border_style="red")
        table.add_column("Dónde")
        table.add_column("Código")
        table.add_column("Motivo")
        for violation in rejected.verdict.violations:
            table.add_row(violation.location, violation.code, violation.message)
        console.print(table)
        console.print("[dim]No se descargó ni construyó nada.[/]")
        return
    except KeyboardInterrupt:
        bar.stop()
        console.print("\n[yellow]Construcción interrumpida; la imagen quedó incompleta.[/]")
        return
    except (BaseImageError, packer.PackerError, RuntimeError) as exc:
        bar.stop()
        console.print(Panel(str(exc), title="Falló la construcción", border_style="red"))
        return
    finally:
        bar.stop()

    console.print("[green]✔ Imagen construida[/]")

    if verify_after:
        section(console, "Verificación")
        try:
            with console.status("Arrancando la VM y ejecutando goss…", spinner="dots"):
                verification = verify_build(result.build_dir)
        except VerificationError as exc:
            console.print(f"[red]✘ {exc}[/]")
        else:
            for check in verification.checks:
                mark = "[green]✔[/]" if check.passed else "[red]✘[/]"
                console.print(f"  {mark} {check.name}  [dim]{check.detail}[/]")
            console.print(
                "[green]Verificación superada[/]"
                if verification.passed
                else "[red]La verificación falló[/]"
            )

    destination = Path(
        Prompt.ask(
            "¿Dónde guardo la imagen?",
            default=remembered.last_destination or str(default_downloads()),
            console=console,
        )
    ).expanduser()
    manifest_path = result.manifest
    manifest = Manifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    try:
        image_path, manifest_copy = export_image(
            manifest, manifest_path, destination, name, force=False
        )
    except ExportError as exc:
        console.print(f"[red]✘ {exc}[/]")
        console.print(f"La imagen sigue en: {result.image}")
        return

    settings.save(settings.Settings(last_name=name, last_destination=str(destination)))

    # Plain lines, not a box: a long path inside a border gets cut in the middle.
    console.print()
    console.rule("[bold green]Imagen lista y descargada[/]", style="green")
    for label, value, style in (
        ("Imagen  ", image_path, "bold green"),
        ("Manifest", manifest_copy, ""),
        ("sha256  ", manifest.artifact.sha256, "dim"),
        ("Tamaño  ", _fmt_size(image_path.stat().st_size), ""),
    ):
        console.print(Text.assemble((f"{label}  ", "dim"), (str(value), style)), soft_wrap=True)


MENU = (
    ("1", "Listar imágenes existentes"),
    ("2", "Subir imagen base"),
    ("3", "Lista de imágenes base"),
    ("4", "Crear imagen"),
    ("0", "Salir"),
)


def show_menu(console: Console) -> None:
    table = Table.grid(padding=(0, 2))
    for key, label in MENU:
        table.add_row(f"[bold {ACCENT}]{key}[/]", label)
    console.print(Panel(table, title="Menú", border_style="dim", padding=(1, 3)))


def run(console: Console | None = None) -> None:
    console = console or Console()
    banner(console)
    config = ask_api_key(console)
    if config is None:
        return
    actions = {
        "1": lambda: list_images(console),
        "2": lambda: upload_base(console),
        "3": lambda: list_bases(console),
        "4": lambda: create_image(console, config),
    }
    try:
        while True:
            show_menu(console)
            choice = Prompt.ask(
                "Elige una opción",
                choices=[key for key, _ in MENU],
                default="4",
                console=console,
            )
            if choice == "0":
                console.print("[dim]Hasta luego.[/]")
                return
            try:
                actions[choice]()
            except KeyboardInterrupt:
                console.print("\n[yellow]Acción cancelada.[/]")
            console.print()
    except (KeyboardInterrupt, EOFError):
        console.print("\n[dim]Hasta luego.[/]")
