"""What every menu screen shares: the accent colour, section rules and the download bar."""

from __future__ import annotations

from pathlib import Path

from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TaskID,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)

ACCENT = "cyan"


def default_downloads() -> Path:
    return Path.home() / "gira-iso-images"


def section(console: Console, title: str) -> None:
    console.print()
    console.rule(f"[bold {ACCENT}]{title}[/]", style=ACCENT)


def fmt_size(num: int) -> str:
    return f"{num / 1024**3:.1f} GB" if num >= 1024**3 else f"{num / 1024**2:.0f} MB"


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
