"""Menu option 4: the menu's face of the create workflow."""

from __future__ import annotations

from contextlib import AbstractContextManager
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text

from gira_iso import settings
from gira_iso.builder.catalog import load_manifest
from gira_iso.errors import GiraError
from gira_iso.naming import is_valid_image_name
from gira_iso.nl.client import (
    LlmConfig,
    OpenAiCompatible,
)
from gira_iso.tui.bases import choose_base
from gira_iso.tui.common import (
    ACCENT,
    DownloadBar,
    ask_directory,
    fmt_size,
    remembered_directory,
    section,
)
from gira_iso.tui.views import (
    show_explanation,
    show_policy_table,
    show_spec,
    show_violations,
    usage_line,
)
from gira_iso.workflow.create import CreateOutcome, CreateRequest, Status, run_create
from gira_iso.workflow.events import Built, Download, Event, LogLine, Step, Verified
from gira_iso.workflow.ports import Decision, Review

EXAMPLES = (
    ("Servidor web", "Un servidor web con nginx y un usuario deploy con sudo"),
    ("Docker", "Una imagen con docker instalado y htop"),
    (
        "Servidor con configuración",
        "Un servidor nginx con una página de health en /etc/nginx/conf.d/health.conf "
        "que responda ok en el puerto 8080",
    ),
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


class _TuiIo:
    """Presents the workflow in the menu. It decides nothing about the order."""

    interactive = True

    def __init__(self, console: Console) -> None:
        self._console = console
        self._bar = DownloadBar(console)
        self._remembered = settings.load()
        self.building = False  # True once the person confirmed; a Ctrl+C then interrupts a build

    def close(self) -> None:
        self._bar.stop()

    def emit(self, event: Event) -> None:
        console = self._console
        if isinstance(event, Step):
            console.print(f"[bold {ACCENT}]▸[/] {event.message}")
        elif isinstance(event, LogLine):
            # A Text, not markup: packer output is full of square brackets.
            # One line each: Packer's xtrace output has lines of thousands of characters. The full
            # text is in <build>/logs/packer-build.log.
            console.print(Text(f"  │ {event.text}", style="dim"), no_wrap=True, overflow="ellipsis")
        elif isinstance(event, Download):
            self._bar(event.done, event.total)
        elif isinstance(event, Built):
            self._bar.stop()
            console.print("[green]✔ Imagen construida[/]")
        elif isinstance(event, Verified):
            for check in event.result.checks:
                mark = "[green]✔[/]" if check.passed else "[red]✘[/]"
                console.print(f"  {mark} {check.name}  [dim]{check.detail}[/]")
            console.print(
                "[green]Verificación superada[/]"
                if event.result.passed
                else "[red]La verificación falló[/]"
            )

    def answer(self, questions: list[str]) -> list[str]:
        self._console.print("[yellow]El modelo necesita más detalle:[/]")
        return [Prompt.ask(f"  {question}", console=self._console) for question in questions]

    def busy(self, message: str) -> AbstractContextManager[object]:
        return self._console.status(message, spinner="dots")

    def review(self, review: Review) -> Decision:
        console = self._console
        show_explanation(console, review.explanation)
        show_spec(console, review.spec, usage_line(review.usage, review.latency_ms))

        name = Prompt.ask(
            "Nombre de la imagen",
            default=self._remembered.last_name or "mi-imagen",
            console=console,
        )
        if not is_valid_image_name(name):
            console.print("[red]✘ Nombre inválido: usa letras, dígitos, '.', '_' o '-'.[/]")
            return Decision(proceed=False)
        question = (
            "¿Construir la imagen sin lo que no puedo incluir?"
            if review.explanation.unsupported
            else "¿Construir esta imagen?"
        )
        if not Confirm.ask(question, default=True, console=console):
            return Decision(proceed=False)
        verify = Confirm.ask(
            "¿Arrancarla y verificarla al terminar? (más lento)", default=False, console=console
        )
        self.building = True
        section(console, "Construcción")
        return Decision(proceed=True, name=name, verify=verify)

    def destination(self, name: str) -> Path | None:
        default = remembered_directory(self._remembered.last_destination)
        return ask_directory(self._console, "¿En qué carpeta guardo la imagen?", default)


def _show_outcome(console: Console, outcome: CreateOutcome) -> None:
    """Everything that happens after the workflow returns."""
    if outcome.status is Status.MODEL_REJECTED:
        console.print(Panel(outcome.reason or "", title="Petición rechazada", border_style="red"))
    elif outcome.status in (Status.INTERPRET_ERROR, Status.NEEDS_DETAIL):
        console.print(f"[red]✘ {outcome.error or 'El modelo no pudo interpretar la petición.'}[/]")
    elif outcome.status is Status.POLICY_REJECTED and outcome.verdict is not None:
        show_policy_table(console, outcome.verdict)
        console.print("[dim]No se descargó ni construyó nada.[/]")
    elif outcome.status is Status.POLICY_REJECTED:
        assert outcome.spec is not None
        show_explanation(console, outcome.explanation)
        show_spec(console, outcome.spec, usage_line(outcome.usage, outcome.latency_ms))
        show_violations(console, outcome.violations)
        console.print("[dim]No se construyó nada. Reformula la petición con lo permitido.[/]")
    elif outcome.status is Status.BUILT:
        _show_built(console, outcome)


def _show_built(console: Console, outcome: CreateOutcome) -> None:
    assert outcome.build is not None and outcome.name is not None
    if outcome.exported is None:
        console.print(f"[red]✘ {outcome.export_error}[/]")
        console.print(f"La imagen sigue en: {outcome.build.image}")
        return

    image_path, manifest_copy = outcome.exported
    manifest, _ = load_manifest(outcome.build.manifest)
    settings.save(
        settings.Settings(last_name=outcome.name, last_destination=str(image_path.parent))
    )

    # Plain lines, not a box: a long path inside a border gets cut in the middle.
    console.print()
    console.rule("[bold green]Imagen lista y descargada[/]", style="green")
    for label, value, style in (
        ("Imagen  ", image_path, "bold green"),
        ("Manifest", manifest_copy, ""),
        ("sha256  ", manifest.artifact.sha256, "dim"),
        ("Tamaño  ", fmt_size(image_path.stat().st_size), ""),
    ):
        console.print(Text.assemble((f"{label}  ", "dim"), (str(value), style)), soft_wrap=True)


def create_image(console: Console, config: LlmConfig) -> None:
    section(console, "Crear imagen")
    os_name, version = choose_base(console)
    console.print("\n[bold]Describe la imagen que necesitas[/] [dim](español o inglés)[/]")
    request = _ask_request(console)
    if not request:
        console.print("[yellow]No escribiste nada.[/]")
        return

    io = _TuiIo(console)
    try:
        outcome = run_create(
            CreateRequest(
                text=request, completer=OpenAiCompatible(config), base=(os_name, version)
            ),
            io,
        )
    except KeyboardInterrupt:
        if not io.building:
            raise  # still choosing or interpreting: the menu just cancels the action
        console.print("\n[yellow]Construcción interrumpida; la imagen quedó incompleta.[/]")
        return
    except (GiraError, OSError) as exc:
        console.print(Panel(str(exc), title="Falló la construcción", border_style="red"))
        return
    finally:
        io.close()

    _show_outcome(console, outcome)
