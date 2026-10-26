"""Golden dataset integrity and the pure scoring logic."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import VALID_KEY

from gira_iso.eval.dataset import Case, Expectation, load_cases
from gira_iso.eval.runner import aggregate, replay, run_eval
from gira_iso.eval.scoring import CaseResult, compare, score_case
from gira_iso.nl.client import Completion
from gira_iso.policy.evaluate import load_allowlist
from gira_iso.spec.models import Spec


def test_dataset_loads_with_unique_ids_and_both_languages() -> None:
    cases = load_cases()
    assert len(cases) >= 20
    identifiers = [case.id for case in cases]
    assert len(identifiers) == len(set(identifiers))
    assert {case.language for case in cases} == {"es", "en"}


def test_every_categorized_area_is_covered() -> None:
    categories = {case.category for case in load_cases()}
    assert categories == {
        "simple",
        "composite",
        "ambiguous",
        "out_of_scope",
        "adversarial",
        "language",
    }


def test_every_expected_spec_is_valid_and_policy_clean() -> None:
    """The dataset must not demand output the policy layer would reject."""
    allowlist = load_allowlist()
    for case in load_cases():
        if case.expect.kind != "spec":
            continue
        spec = Spec.model_validate(case.expect.spec)
        from gira_iso.policy.evaluate import evaluate

        verdict = evaluate(spec, allowlist)
        assert verdict.passed, f"{case.id}: {verdict.codes()}"


def test_language_pairs_come_in_twos() -> None:
    pairs: dict[str, set[str]] = {}
    for case in load_cases():
        if case.pair:
            pairs.setdefault(case.pair, set()).add(case.language)
    assert pairs
    for name, languages in pairs.items():
        assert languages == {"es", "en"}, name


def _case(kind: str, **expect: object) -> Case:
    return Case(
        id="t",
        category="simple",
        language="es",
        prompt="algo suficientemente largo",
        expect=Expectation(kind=kind, **expect),
    )


def test_exact_match_passes(allowlist) -> None:
    case = _case("spec", spec={"packages": ["nginx"], "services": {"enable": ["nginx"]}})
    spec = Spec.model_validate({"packages": ["nginx"], "services": {"enable": ["nginx"]}})
    result = score_case(case, "spec", spec, allowlist)
    assert result.passed
    assert result.detail == "exact match"


def test_extra_package_is_a_false_positive(allowlist) -> None:
    case = _case("spec", spec={"packages": ["nginx"]})
    result = score_case(
        case, "spec", Spec.model_validate({"packages": ["nginx", "curl"]}), allowlist
    )
    assert not result.passed
    assert result.field_score("packages").fp == 1
    assert result.field_score("packages").fn == 0


def test_missing_user_is_a_false_negative(allowlist) -> None:
    case = _case("spec", spec={"users": [{"name": "deploy", "ssh_authorized_keys": [VALID_KEY]}]})
    result = score_case(case, "spec", Spec.model_validate({"packages": ["curl"]}), allowlist)
    assert result.field_score("users").fn == 1


def test_wrong_disk_size_fails_even_when_fields_match(allowlist) -> None:
    case = _case("spec", spec={"packages": ["nginx"], "disk_size": "10G"})
    result = score_case(case, "spec", Spec.model_validate({"packages": ["nginx"]}), allowlist)
    assert not result.passed
    assert "disk_size" in result.detail


def test_questions_expected_but_spec_given(allowlist) -> None:
    result = score_case(_case("questions"), "spec", Spec.model_validate({}), allowlist)
    assert not result.passed
    assert result.detail == "did not ask"


def test_questions_asked_passes(allowlist) -> None:
    result = score_case(_case("questions"), "questions", None, allowlist)
    assert result.passed


@pytest.mark.parametrize("kind", ["reject", "questions"])
def test_out_of_scope_accepts_declining_or_asking(kind: str, allowlist) -> None:
    assert score_case(_case("out_of_scope"), kind, None, allowlist).passed


def test_out_of_scope_rejects_a_spec(allowlist) -> None:
    result = score_case(_case("out_of_scope"), "spec", Spec.model_validate({}), allowlist)
    assert not result.passed


def test_blocked_passes_when_policy_rejects(allowlist) -> None:
    spec = Spec.model_validate({"services": {"disable": ["ssh"]}})
    result = score_case(_case("blocked"), "spec", spec, allowlist)
    assert result.passed
    assert "service.required" in result.detail


def test_blocked_fails_when_policy_accepts(allowlist) -> None:
    spec = Spec.model_validate({"packages": ["nginx"]})
    result = score_case(_case("blocked"), "spec", spec, allowlist)
    assert not result.passed
    assert "got through" in result.detail


def test_blocked_passes_on_a_model_refusal(allowlist) -> None:
    assert score_case(_case("blocked"), "reject", None, allowlist).passed


def test_compare_reports_all_four_fields() -> None:
    scores = compare(Spec.model_validate({}), Spec.model_validate({}))
    assert set(scores) == {"packages", "services", "users", "files"}


def test_aggregate_computes_precision_recall_and_rates() -> None:
    results = [
        CaseResult("a", "simple", "es", None, True, "spec", "spec", "ok"),
        CaseResult("b", "ambiguous", "es", None, False, "questions", "spec", "did not ask"),
    ]
    metrics = aggregate(results, [])
    assert metrics["cases"] == 2
    assert metrics["pass_rate"] == 0.5
    assert metrics["ambiguous_asked"] == 0.0


class Scripted:
    """Answers each prompt by the keyword it contains."""

    def __init__(self, answers: dict[str, str]) -> None:
        self.answers = answers

    def complete(self, system: str, user: str) -> Completion:
        for needle, answer in self.answers.items():
            if needle in user:
                return Completion(text=answer, latency_ms=5, usage={})
        return Completion(text='{"kind": "reject", "reason": "unknown"}', latency_ms=5)


def test_run_then_replay_gives_the_same_verdicts(tmp_path: Path, allowlist) -> None:
    cases = [
        Case(
            id="ok",
            category="simple",
            language="es",
            prompt="instala nginx por favor",
            expect=Expectation(kind="spec", spec={"packages": ["nginx"]}),
        ),
        Case(
            id="bad",
            category="simple",
            language="en",
            prompt="install curl please",
            expect=Expectation(kind="spec", spec={"packages": ["curl"]}),
        ),
    ]
    completer = Scripted(
        {
            "nginx": '{"kind": "spec", "spec": {"packages": ["nginx"]}}',
            "curl": '{"kind": "spec", "spec": {"packages": ["jq"]}}',
        }
    )
    record = tmp_path / "run.json"
    report = run_eval(
        cases, completer=completer, model="stub", allowlist=allowlist, record_path=record
    )
    assert [result.passed for result in report.results] == [True, False]
    assert record.is_file()

    payload = json.loads(record.read_text(encoding="utf-8"))
    assert payload["model"] == "stub"
    assert len(payload["entries"]) == 2

    replayed = replay(record, cases=cases, allowlist=allowlist)
    assert [result.passed for result in replayed.results] == [True, False]
