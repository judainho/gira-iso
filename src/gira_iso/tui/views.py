"""Pure presentation: banner, tables and panels. Nothing here prompts or decides."""

from __future__ import annotations

import yaml
from rich.console import Console, Group
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from gira_iso import __version__
from gira_iso.nl.explain import Explanation
from gira_iso.policy.evaluate import Verdict
from gira_iso.proposals import WARNING, Proposal, Refusal
from gira_iso.spec.canonical import spec_hash, to_canonical_dict
from gira_iso.spec.models import Spec
from gira_iso.tui.common import ACCENT
from gira_iso.workflow.preflight import Problem

AUTHOR = "Manuel Judá Aliaga Aliaga"


EMAIL = "maliagaa@pucp.edu.pe"


ORG = "GIRA PUCP"


_KIND_LABELS = {
    "program": "programa externo",
    "package": "paquete no permitido",
    "service": "servicio no permitido",
    "other": "no expresable",
}


LOGO = r"""
  ██████╗ ██╗██████╗  █████╗       ██╗███████╗ ██████╗
 ██╔════╝ ██║██╔══██╗██╔══██╗      ██║██╔════╝██╔═══██╗
 ██║  ███╗██║██████╔╝███████║█████╗██║███████╗██║   ██║
 ██║   ██║██║██╔══██╗██╔══██║╚════╝██║╚════██║██║   ██║
 ╚██████╔╝██║██║  ██║██║  ██║      ██║███████║╚██████╔╝
  ╚═════╝ ╚═╝╚═╝  ╚═╝╚═╝  ╚═╝      ╚═╝╚══════╝ ╚═════╝
"""


def banner(console: Console) -> None:
    title = Text(LOGO.rstrip("\n"), style=f"bold {ACCENT}")
    credits = Text.assemble(
        ("Imágenes de máquina desde lenguaje natural\n", "italic"),
        (f"v{__version__}  ·  ", "dim"),
        (f"{AUTHOR}", "bold"),
        (f"  ·  {EMAIL}  ·  {ORG}", "dim"),
    )
    console.print(Panel(Group(title, credits), border_style=ACCENT, padding=(0, 2)))


def show_explanation(console: Console, explanation: Explanation) -> None:
    """What the model understood, and what it could not provide, in the user's terms."""
    if explanation.understood:
        table = Table(
            title="Lo que entendí",
            title_justify="left",
            border_style="dim",
            header_style=f"bold {ACCENT}",
        )
        for column in ("Pediste", "Se instala como", "Tipo"):
            table.add_column(column)
        for claim in explanation.understood:
            table.add_row(claim.requested, claim.resolved, claim.kind)
        console.print(table)
    if explanation.unsupported:
        table = Table(
            title="No puedo incluir",
            title_justify="left",
            border_style="yellow",
            header_style="bold yellow",
        )
        for column in ("Pediste", "Qué es", "Motivo", "Alternativas permitidas"):
            table.add_column(column)
        for claim in explanation.unsupported:
            table.add_row(
                claim.item,
                _KIND_LABELS[claim.kind],
                claim.reason,
                ", ".join(claim.alternatives) or "-",
            )
        console.print(table)


def show_violations(console: Console, violations: tuple[str, ...]) -> None:
    table = Table(title="La política rechaza este spec", border_style="red")
    table.add_column("Motivo")
    for line in violations:
        table.add_row(line)
    console.print(table)


def usage_line(usage: dict[str, int], latency_ms: int) -> str:
    parts = []
    if usage:
        parts.append(
            f"tokens: {usage.get('prompt_tokens', 0)} entrada · "
            f"{usage.get('completion_tokens', 0)} salida"
        )
    parts.append(f"{latency_ms / 1000:.1f} s")
    return "  ·  ".join(parts)


def show_spec(console: Console, spec: Spec, usage_line: str = "") -> None:
    body = yaml.safe_dump(to_canonical_dict(spec), sort_keys=False).rstrip()
    console.print(
        Panel(
            Syntax(body, "yaml", theme="ansi_dark", background_color="default"),
            title=f"Spec interpretado  ·  {spec_hash(spec)[:12]}",
            subtitle=usage_line or None,
            border_style=ACCENT,
        )
    )


def show_policy_table(console: Console, verdict: Verdict) -> None:
    table = Table(title="La política rechazó el spec", border_style="red")
    table.add_column("Dónde")
    table.add_column("Código")
    table.add_column("Motivo")
    for violation in verdict.violations:
        table.add_row(violation.location, violation.code, violation.message)
    console.print(table)


MENU = (
    ("1", "Listar imágenes existentes"),
    ("2", "Subir imagen base"),
    ("3", "Lista de imágenes base"),
    ("4", "Crear imagen"),
    ("0", "Salir"),
)


def show_menu(console: Console, compact: bool = False) -> None:
    """The full box once; afterwards a single line, so the screen is not repainted each time."""
    if compact:
        options = "  ".join(f"[bold {ACCENT}]{key}[/] {label}" for key, label in MENU)
        console.print(f"[dim]Menú ›[/] {options}")
        return
    table = Table.grid(padding=(0, 2))
    for key, label in MENU:
        table.add_row(f"[bold {ACCENT}]{key}[/]", label)
    console.print(Panel(table, title="Menú", border_style="dim", padding=(1, 3)))


def show_proposals(console: Console, proposals: list[Proposal], refusals: list[Refusal]) -> None:
    """Packages that could join the allowlist, and the ones held back on purpose."""
    if refusals:
        table = Table(
            title="No se pueden agregar",
            title_justify="left",
            border_style="red",
            header_style="bold red",
        )
        table.add_column("Paquete")
        table.add_column("Motivo")
        for refusal in refusals:
            table.add_row(refusal.package, refusal.reason)
        console.print(table)
    if proposals:
        table = Table(
            title="Paquetes reales de Ubuntu que no están en la lista permitida",
            title_justify="left",
            border_style="yellow",
            header_style="bold yellow",
        )
        for column in ("Paquete", "Pediste", "Componente", "Versión", "Sección"):
            table.add_column(column)
        for proposal in proposals:
            table.add_row(
                proposal.package,
                proposal.requested_as or "-",
                proposal.component,
                proposal.version,
                proposal.section,
            )
        console.print(table)
        console.print(f"[dim]{WARNING}[/]")


def show_environment(console: Console, problems: list[Problem]) -> None:
    """One line when the machine is ready; the problems and their fixes when it is not."""
    if not problems:
        console.print("[green]✔[/] Entorno listo para construir (QEMU, KVM, Packer)\n")
        return
    lines = Text()
    for problem in problems:
        lines.append(f"✘ {problem.what}\n", style="bold")
        lines.append(f"  arreglo: {problem.fix}\n", style="dim")
    console.print(Panel(lines, title="Este equipo aún no puede construir", border_style="yellow"))
    console.print("[dim]Puedes seguir usando el menú para ver imágenes y bases.[/]\n")
