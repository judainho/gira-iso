from __future__ import annotations

import json
from pathlib import Path

import yaml

from gira_iso.nl.client import Completion
from gira_iso.nl.session import converse

SPEC = yaml.safe_load(Path("examples/nginx-web.yaml").read_text(encoding="utf-8"))


class Scripted:
    def __init__(self, *replies: dict) -> None:
        self._replies = [json.dumps(r) for r in replies]
        self.prompts: list[str] = []

    def complete(self, system: str, user: str) -> Completion:
        self.prompts.append(user)
        return Completion(
            text=self._replies.pop(0),
            latency_ms=100,
            usage={"prompt_tokens": 10, "completion_tokens": 5},
        )


def test_a_direct_spec_is_returned_with_its_usage() -> None:
    result = converse("nginx", Scripted({"kind": "spec", "spec": SPEC}))

    assert result.spec is not None
    assert result.usage == {"prompt_tokens": 10, "completion_tokens": 5}
    assert isinstance(result.latency_ms, int)


def test_a_refusal_carries_the_reason() -> None:
    result = converse("make coffee", Scripted({"kind": "reject", "reason": "not an image"}))

    assert result.reason == "not an image"
    assert result.spec is None


def test_questions_are_answered_and_usage_is_summed() -> None:
    completer = Scripted(
        {"kind": "questions", "questions": ["Which user?"]},
        {"kind": "spec", "spec": SPEC},
    )

    result = converse("a server", completer, answer=lambda questions: ["deploy"])

    assert result.spec is not None
    assert "Which user? deploy" in completer.prompts[1]
    assert result.usage == {"prompt_tokens": 20, "completion_tokens": 10}


def test_without_an_answerer_the_questions_are_returned() -> None:
    result = converse(
        "a server", Scripted({"kind": "questions", "questions": ["Which user?", "How big?"]})
    )

    assert result.questions == ("Which user?", "How big?")
    assert result.spec is None


def test_endless_questions_stop_after_the_round_limit() -> None:
    ask = {"kind": "questions", "questions": ["more?"]}

    result = converse("a server", Scripted(ask, ask), answer=lambda q: ["x"], rounds=2)

    assert result.error is not None
    assert "still ambiguous after 2 rounds" in result.error


def test_a_model_that_never_returns_valid_json_is_an_error() -> None:
    class Garbage:
        def complete(self, system: str, user: str) -> Completion:
            return Completion(text="not json")

    result = converse("a server", Garbage())

    assert result.error is not None
    assert "failed validation" in result.error


def test_the_context_reaches_the_model_on_every_round() -> None:
    completer = Scripted(
        {"kind": "questions", "questions": ["Which user?"]},
        {"kind": "spec", "spec": SPEC},
    )

    converse("a server", completer, answer=lambda q: ["deploy"], context="Base image: ubuntu 22.04")

    assert all("Base image: ubuntu 22.04" in prompt for prompt in completer.prompts)
