"""The menu loop: banner, API key, then the four options."""

from __future__ import annotations

import os
import shutil

from rich.console import Console
from rich.prompt import Prompt

from gira_iso.nl.client import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    LlmConfig,
    check_credentials,
)
from gira_iso.tui.allowed import allowed_packages
from gira_iso.tui.bases import list_bases, upload_base
from gira_iso.tui.create import create_image
from gira_iso.tui.images import list_images
from gira_iso.tui.views import MENU, banner, show_environment, show_menu
from gira_iso.workflow.preflight import check_environment

MAX_KEY_ATTEMPTS = 3


MAX_WIDTH = 100


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


def run(console: Console | None = None) -> None:
    console = console or Console(width=min(shutil.get_terminal_size().columns, MAX_WIDTH))
    banner(console)
    config = ask_api_key(console)
    if config is None:
        return
    show_environment(console, check_environment())
    actions = {
        "1": lambda: list_images(console),
        "2": lambda: upload_base(console),
        "3": lambda: list_bases(console),
        "4": lambda: create_image(console, config),
        "5": lambda: allowed_packages(console),
    }
    first = True
    try:
        while True:
            show_menu(console, compact=not first)
            first = False
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
