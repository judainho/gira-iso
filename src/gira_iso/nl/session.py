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
from gira_iso.nl.schema import Interpretation
from gira_iso.policy.evaluate import Allowlist, evaluate
from gira_iso.spec.models import Spec

MAX_ROUNDS = 3

Answerer = Callable[[list[str]], list[str]]
Checker = Callable[[Spec], list[str]]


def policy_checker(allowlist: Allowlist, base: tuple[str, str] | None = None) -> Checker:
    """Violations the policy would raise for a spec, as one line each.

    `base` is the (os, version) the user picked in the interface: it replaces whatever
    the model wrote, exactly as the caller does before building.
    """

    def check(spec: Spec) -> list[str]:
        if base is not None:
            spec = spec.model_copy(
                update={"base": spec.base.model_copy(update={"os": base[0], "version": base[1]})}
            )
        verdict = evaluate(spec, allowlist)
        return [f"{v.location}: [{v.code}] {v.message}" for v in verdict.violations]

    return check


@dataclass(frozen=True)
class SessionResult:
    """Exactly one of spec, reason, questions or error is set."""

    spec: Spec | None = None
    interpretation: Interpretation | None = None  # the full answer behind `spec`
    violations: tuple[str, ...] = ()  # the spec exists but the policy still rejects it
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
    context: str | None = None,
    check: Checker | None = None,
) -> SessionResult:
    """Interpret `request`; `answer` receives the model's questions and returns the replies.

    When `check` is given, a spec the policy rejects is sent back to the model with the
    violations so it can correct itself, using the same rounds as the questions.
    """
    text = request
    usage: dict[str, int] = {}
    latency = 0
    for round_number in range(1, rounds + 1):
        try:
            with (busy or nullcontext)():
                inference = interpret(text, completer=completer, context=context)
        except InterpretError as exc:
            return SessionResult(error=str(exc), usage=usage, latency_ms=latency)
        for key, value in inference.usage.items():
            usage[key] = usage.get(key, 0) + value
        latency += inference.latency_ms

        result = inference.interpretation
        if result.kind == "spec":
            assert result.spec is not None
            violations = check(result.spec) if check else []
            if violations and round_number < rounds:
                listed = "\n".join(f"- {line}" for line in violations)
                text = (
                    f"{text}\n\nYour previous spec was rejected by the policy:\n{listed}\n"
                    "Correct it using only the allowed catalog. Anything the catalog cannot "
                    'provide goes in "unsupported", not in the spec.'
                )
                continue
            return SessionResult(
                spec=result.spec,
                interpretation=result,
                violations=tuple(violations),
                usage=usage,
                latency_ms=latency,
            )
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
