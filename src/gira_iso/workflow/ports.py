"""What a front end must provide. The workflow depends on this, never on typer or rich."""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from gira_iso.nl.explain import Explanation
from gira_iso.spec.models import Spec
from gira_iso.workflow.events import Event


@dataclass(frozen=True)
class Review:
    """Everything a person needs to decide whether to build."""

    spec: Spec
    explanation: Explanation
    usage: dict[str, int]
    latency_ms: int
    name: str | None
    verify: bool
    from_model: bool  # False when the spec came straight from a file


@dataclass(frozen=True)
class Decision:
    proceed: bool
    name: str | None = None  # a name chosen during the review replaces the requested one
    verify: bool = False


class Io(Protocol):
    interactive: bool  # False: nobody can answer the model's questions

    def emit(self, event: Event) -> None: ...

    def answer(self, questions: list[str]) -> list[str]:
        """Replies to the model's questions, in order. Only called when `interactive`."""
        ...

    def busy(self, message: str) -> AbstractContextManager[object]:
        """Wrap a slow call (the model, the verification VM) in whatever shows progress."""
        ...

    def review(self, review: Review) -> Decision:
        """Show the interpreted spec and decide. Called once, just before building."""
        ...

    def destination(self, name: str) -> Path | None:
        """Where to copy the finished image, or None to leave it in the builds directory."""
        ...
