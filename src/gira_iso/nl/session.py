"""One interpretation conversation, independent of how the questions get answered.

The menu answers at a rich prompt, the scripted command at a typer prompt or not at
all. Both need the same loop: ask the model, feed answers back, stop after a few
rounds. Nothing here prints or prompts.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field

from gira_iso.nl.client import Completer, InterpretError, interpret
from gira_iso.spec.models import Spec

MAX_ROUNDS = 3

Answerer = Callable[[list[str]], list[str]]


@dataclass(frozen=True)
class SessionResult:
    """Exactly one of spec, reason, questions or error is set."""

    spec: Spec | None = None
    reason: str | None = None  # the model refused the request
    questions: tuple[str, ...] = ()  # unanswered, because nobody could answer
    error: str | None = None  # the model never produced a valid answer
    usage: dict[str, int] = field(default_factory=dict)
    latency_ms: int = 0


def converse(
    request: str,
    completer: Completer,
    *,
    answer: Answerer | None = None,
    rounds: int = MAX_ROUNDS,
    busy: Callable[[], AbstractContextManager[object]] | None = None,
) -> SessionResult:
    """Interpret `request`; `answer` receives the model's questions and returns the replies."""
    text = request
    usage: dict[str, int] = {}
    latency = 0
    for _ in range(rounds):
        try:
            with (busy or nullcontext)():
                inference = interpret(text, completer=completer)
        except InterpretError as exc:
            return SessionResult(error=str(exc), usage=usage, latency_ms=latency)
        for key, value in inference.usage.items():
            usage[key] = usage.get(key, 0) + value
        latency += inference.latency_ms

        result = inference.interpretation
        if result.kind == "spec":
            return SessionResult(spec=result.spec, usage=usage, latency_ms=latency)
        if result.kind == "reject":
            return SessionResult(reason=result.reason, usage=usage, latency_ms=latency)

        assert result.questions is not None
        if answer is None:
            return SessionResult(questions=tuple(result.questions), usage=usage, latency_ms=latency)
        replies = answer(list(result.questions))
        details = "\n".join(f"- {q} {a}" for q, a in zip(result.questions, replies, strict=True))
        text = f"{text}\n\nAdditional details:\n{details}"

    return SessionResult(
        error=f"the request is still ambiguous after {rounds} rounds",
        usage=usage,
        latency_ms=latency,
    )
