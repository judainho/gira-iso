"""The interpreter knows the catalog, classifies requests and is checked against the policy."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from gira_iso.nl.client import Completion
from gira_iso.nl.explain import explain
from gira_iso.nl.prompt import build_system_prompt, catalog_section
from gira_iso.nl.schema import Interpretation
from gira_iso.nl.session import converse, policy_checker
from gira_iso.policy.evaluate import load_allowlist
from gira_iso.spec.models import Spec

CATALOG = yaml.safe_load(Path("src/gira_iso/nl/catalog.yaml").read_text(encoding="utf-8"))


def interpretation(**extra) -> Interpretation:
    spec = {"packages": ["docker.io", "htop"], "services": {"enable": ["docker"]}}
    return Interpretation.model_validate({"kind": "spec", "spec": spec, **extra})


# --- the catalog the model is shown ---------------------------------------------------


def test_every_allowed_package_and_service_is_described() -> None:
    allowlist = load_allowlist()

    assert allowlist.packages <= set(CATALOG["packages"]), "undescribed package"
    assert allowlist.services <= set(CATALOG["services"]), "undescribed service"


def test_the_catalog_has_no_stale_entries() -> None:
    allowlist = load_allowlist()

    assert set(CATALOG["packages"]) <= allowlist.packages
    assert set(CATALOG["services"]) <= allowlist.services


def test_the_prompt_lists_the_allowed_software_with_aliases() -> None:
    prompt = build_system_prompt(load_allowlist())

    assert "docker.io - Docker engine" in prompt
    assert "also: docker" in prompt
    assert "opencode" in prompt  # named as a program outside the catalog
    assert "{{" not in prompt  # the old prompt shipped literal double braces


def test_the_prompt_follows_the_active_policy_not_the_packaged_one() -> None:
    custom = dataclasses.replace(
        load_allowlist(), packages=frozenset({"nginx"}), services=frozenset({"nginx"})
    )

    section = catalog_section(custom)

    assert "nginx" in section
    assert "docker.io" not in section.split("Programs that are NOT")[0]


def test_a_name_missing_from_the_catalog_file_is_still_listed() -> None:
    custom = dataclasses.replace(load_allowlist(), packages=frozenset({"brand-new-pkg"}))

    assert "  brand-new-pkg\n" in catalog_section(custom)


# --- the schema ------------------------------------------------------------------------


def test_a_spec_may_carry_understood_and_unsupported() -> None:
    result = interpretation(
        understood=[{"requested": "docker", "resolved": "docker.io", "kind": "package"}],
        unsupported=[
            {"item": "opencode", "kind": "program", "reason": "not an apt package"},
        ],
    )

    assert result.unsupported is not None and result.unsupported[0].alternatives == []


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "reject", "reason": "no", "unsupported": []},
        {"kind": "questions", "questions": ["?"], "understood": []},
    ],
)
def test_only_a_spec_may_carry_them(payload: dict) -> None:
    with pytest.raises(ValidationError):
        Interpretation.model_validate(payload)


def test_unknown_fields_in_a_claim_are_refused() -> None:
    with pytest.raises(ValidationError):
        interpretation(unsupported=[{"item": "x", "reason": "y", "command": "rm -rf /"}])


# --- verifying the model's claims -----------------------------------------------------


def test_claims_that_match_the_spec_are_kept() -> None:
    result = interpretation(
        understood=[
            {"requested": "docker", "resolved": "docker.io", "kind": "package"},
            {"requested": "docker daemon", "resolved": "docker", "kind": "service"},
        ]
    )

    explanation = explain(result, result.spec, load_allowlist())

    assert [c.resolved for c in explanation.understood] == ["docker.io", "docker"]
    assert explanation.discarded == 0


def test_a_claim_about_something_not_in_the_spec_is_discarded() -> None:
    result = interpretation(
        understood=[{"requested": "redis", "resolved": "redis-server", "kind": "package"}]
    )

    explanation = explain(result, result.spec, load_allowlist())

    assert explanation.understood == ()
    assert explanation.discarded == 1


def test_unsupported_items_that_are_in_the_spec_are_discarded() -> None:
    result = interpretation(
        unsupported=[{"item": "htop", "kind": "package", "reason": "unavailable"}]
    )

    explanation = explain(result, result.spec, load_allowlist())

    assert explanation.unsupported == ()
    assert explanation.discarded == 1


def test_invented_alternatives_are_dropped() -> None:
    result = interpretation(
        unsupported=[
            {
                "item": "opencode",
                "kind": "program",
                "reason": "not an apt package",
                "alternatives": ["tmux", "not-a-real-package", "nodejs"],
            }
        ]
    )

    explanation = explain(result, result.spec, load_allowlist())

    assert explanation.unsupported[0].alternatives == ["tmux"]


# --- the policy feeds back into the conversation --------------------------------------


class Scripted:
    def __init__(self, *replies: dict) -> None:
        self._replies = [json.dumps(r) for r in replies]
        self.prompts: list[str] = []

    def complete(self, system: str, user: str) -> Completion:
        self.prompts.append(user)
        return Completion(text=self._replies.pop(0))


def _spec(*packages: str, **extra) -> dict:
    return {"kind": "spec", "spec": {"packages": list(packages)}, **extra}


def test_a_policy_violation_is_sent_back_and_the_model_corrects_itself() -> None:
    completer = Scripted(_spec("opencode", "htop"), _spec("htop"))

    result = converse("htop and opencode", completer, check=policy_checker(load_allowlist()))

    assert result.spec is not None and result.spec.packages == ["htop"]
    assert result.violations == ()
    assert "rejected by the policy" in completer.prompts[1]
    assert "opencode" in completer.prompts[1]


def test_a_model_that_keeps_breaking_the_policy_returns_the_violations() -> None:
    completer = Scripted(_spec("opencode"), _spec("opencode"), _spec("opencode"))

    result = converse("opencode", completer, check=policy_checker(load_allowlist()))

    assert result.spec is not None
    assert any("opencode" in line for line in result.violations)


def test_without_a_checker_nothing_is_validated() -> None:
    result = converse("opencode", Scripted(_spec("opencode")))

    assert result.violations == ()


def test_the_checker_applies_the_base_chosen_in_the_interface() -> None:
    spec = Spec.model_validate({"base": {"os": "ubuntu", "version": "24.04"}})

    unknown = policy_checker(load_allowlist(), base=("ubuntu", "20.04"))(spec)
    known = policy_checker(load_allowlist(), base=("ubuntu", "22.04"))(spec)

    assert any("base.unknown" in line for line in unknown)
    assert known == []
