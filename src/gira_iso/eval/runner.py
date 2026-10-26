"""Run the dataset, score it, and aggregate the metrics.

A run can be recorded and replayed. That is what lets CI re-score a previous run
without calling a model, and what keeps the numbers auditable after the fact.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from gira_iso.eval.dataset import Case
from gira_iso.eval.scoring import CaseResult, score_case
from gira_iso.nl.client import Completer, Inference, interpret
from gira_iso.nl.schema import Interpretation
from gira_iso.policy.evaluate import Allowlist
from gira_iso.spec.models import Spec


@dataclass(frozen=True)
class EvalReport:
    model: str
    results: tuple[CaseResult, ...]
    metrics: dict[str, Any]

    @property
    def passed(self) -> bool:
        return all(result.passed for result in self.results)


def _interpret_one(
    case: Case, completer: Completer
) -> tuple[str, Spec | None, Inference | None, str]:
    try:
        inference = interpret(case.prompt, completer=completer)
    except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
        return "error", None, None, str(exc)
    interpretation = inference.interpretation
    return interpretation.kind, interpretation.spec, inference, ""


def run_eval(
    cases: list[Case],
    *,
    completer: Completer,
    model: str,
    allowlist: Allowlist,
    record_path: Path | None = None,
) -> EvalReport:
    results: list[CaseResult] = []
    entries: list[dict[str, Any]] = []

    for case in cases:
        kind, spec, inference, error = _interpret_one(case, completer)
        if inference is None:
            result = CaseResult(
                case_id=case.id,
                category=case.category,
                language=case.language,
                pair=case.pair,
                passed=False,
                expected_kind=case.expect.kind,
                actual_kind="error",
                detail=error,
            )
        else:
            result = replace(
                score_case(case, kind, spec, allowlist),
                attempts=inference.attempts,
                latency_ms=inference.latency_ms,
            )
        results.append(result)
        entries.append(
            {
                "case_id": case.id,
                "raw": inference.raw if inference else "",
                "attempts": inference.attempts if inference else 0,
                "latency_ms": inference.latency_ms if inference else 0,
                "usage": inference.usage if inference else {},
                "error": error or None,
            }
        )

    metrics = aggregate(results, cases)
    report = EvalReport(model=model, results=tuple(results), metrics=metrics)

    if record_path is not None:
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text(
            json.dumps(
                {
                    "model": model,
                    "created_at": datetime.now(UTC).isoformat(),
                    "metrics": metrics,
                    "entries": entries,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    return report


def replay(record_path: Path, *, cases: list[Case], allowlist: Allowlist) -> EvalReport:
    """Re-score a recorded run without touching the network."""
    payload = json.loads(record_path.read_text(encoding="utf-8"))
    by_id = {entry["case_id"]: entry for entry in payload["entries"]}
    results: list[CaseResult] = []

    for case in cases:
        entry = by_id.get(case.id)
        if entry is None:
            results.append(
                CaseResult(
                    case_id=case.id,
                    category=case.category,
                    language=case.language,
                    pair=case.pair,
                    passed=False,
                    expected_kind=case.expect.kind,
                    actual_kind="missing",
                    detail="case absent from the record",
                )
            )
            continue
        if entry.get("error"):
            results.append(
                CaseResult(
                    case_id=case.id,
                    category=case.category,
                    language=case.language,
                    pair=case.pair,
                    passed=False,
                    expected_kind=case.expect.kind,
                    actual_kind="error",
                    detail=entry["error"],
                )
            )
            continue
        try:
            interpretation = Interpretation.model_validate(json.loads(entry["raw"]))
        except (json.JSONDecodeError, ValidationError) as exc:
            results.append(
                CaseResult(
                    case_id=case.id,
                    category=case.category,
                    language=case.language,
                    pair=case.pair,
                    passed=False,
                    expected_kind=case.expect.kind,
                    actual_kind="invalid",
                    detail=str(exc)[:200],
                )
            )
            continue
        results.append(
            replace(
                score_case(case, interpretation.kind, interpretation.spec, allowlist),
                attempts=entry.get("attempts", 1),
                latency_ms=entry.get("latency_ms", 0),
            )
        )

    return EvalReport(
        model=payload.get("model", "unknown"),
        results=tuple(results),
        metrics=aggregate(results, cases),
    )


def aggregate(results: list[CaseResult], cases: list[Case]) -> dict[str, Any]:
    total = len(results)
    passed = sum(1 for result in results if result.passed)

    by_category: dict[str, dict[str, int]] = {}
    for result in results:
        bucket = by_category.setdefault(result.category, {"passed": 0, "total": 0})
        bucket["total"] += 1
        bucket["passed"] += int(result.passed)

    tp = fp = fn = 0
    for result in results:
        if result.expected_kind != "spec":
            continue
        for _, score in result.fields:
            tp += score.tp
            fp += score.fp
            fn += score.fn

    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None

    kind_counts: dict[str, int] = {}
    for result in results:
        kind_counts[result.category] = kind_counts.get(result.category, 0)
    for category in {case.category for case in cases}:
        kind_counts.setdefault(category, 0)

    def rate(category: str) -> float | None:
        bucket = by_category.get(category)
        return round(bucket["passed"] / bucket["total"], 3) if bucket else None

    pairs: dict[str, list[CaseResult]] = {}
    for result in results:
        if result.pair:
            pairs.setdefault(result.pair, []).append(result)
    consistent = 0
    for members in pairs.values():
        if len(members) < 2:
            continue
        if len({member.actual_kind for member in members}) == 1:
            consistent += 1

    attempt_values = [result.attempts for result in results if result.actual_kind not in {"error"}]
    latencies = [result.latency_ms for result in results if result.latency_ms]

    return {
        "cases": total,
        "passed": passed,
        "pass_rate": round(passed / total, 3) if total else None,
        "by_category": by_category,
        "field_precision": round(precision, 3) if precision is not None else None,
        "field_recall": round(recall, 3) if recall is not None else None,
        "valid_without_retry": (
            round(sum(1 for value in attempt_values if value == 1) / len(attempt_values), 3)
            if attempt_values
            else None
        ),
        "ambiguous_asked": rate("ambiguous"),
        "adversarial_blocked": rate("adversarial"),
        "out_of_scope_declined": rate("out_of_scope"),
        "language_consistency": (round(consistent / len(pairs), 3) if pairs else None),
        "mean_latency_ms": round(sum(latencies) / len(latencies), 1) if latencies else None,
    }
