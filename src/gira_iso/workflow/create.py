"""Create an image: interpret, check, review, build, verify, export.

The order is fixed here and nowhere else:

    1. interpret  (only for natural language; the policy is applied inside the conversation
                   so the model can correct itself)
    2. explain    (check the model's claims against the spec and the policy)
    3. refuse     (the spec still violates the policy: stop, build nothing)
    4. review     (the front end shows everything and decides; may rename or toggle verify)
    5. build      (build_image evaluates the policy again: the gate that does not depend on
                   the model or on the front end)
    6. verify     (optional: boot the image and run the assertions)
    7. export     (optional: copy to a destination the front end chose)

Expected failures (a GiraError) and Ctrl+C propagate to the front end, which knows how to
show them. Everything that is a normal outcome, such as a refusal, is a `CreateOutcome`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from gira_iso.builder.build import BuildRejected, BuildResult, build_image
from gira_iso.builder.catalog import load_manifest
from gira_iso.builder.export import ExportError, export_image
from gira_iso.nl.client import Completer
from gira_iso.nl.explain import Explanation, explain
from gira_iso.nl.session import converse, policy_checker
from gira_iso.policy.evaluate import Allowlist, Verdict, load_allowlist
from gira_iso.spec.models import BaseImage, Spec
from gira_iso.verify.verify import VerifyResult, verify_build
from gira_iso.workflow.events import Built, Download, LogLine, Step, Verified
from gira_iso.workflow.ports import Decision, Io, Review


class Status(Enum):
    BUILT = "built"
    DECLINED = "declined"  # the person looked at the spec and said no
    MODEL_REJECTED = "model_rejected"  # the model refused the request
    NEEDS_DETAIL = "needs_detail"  # the model asked and nobody could answer
    INTERPRET_ERROR = "interpret_error"  # the model never produced a valid answer
    POLICY_REJECTED = "policy_rejected"  # the policy said no; nothing was built


@dataclass(frozen=True)
class CreateRequest:
    """Either `text` (natural language, needs `completer`) or a ready `spec`."""

    text: str | None = None
    spec: Spec | None = None
    completer: Completer | None = None
    # (os, version) chosen by the person; the model never decides the base.
    base: tuple[str, str] | None = None
    name: str | None = None
    verify: bool = False
    work_root: Path | None = None
    allowlist: Allowlist | None = None
    review: bool = True  # False skips Io.review, as a spec file always did on the command line

    def __post_init__(self) -> None:
        if (self.text is None) == (self.spec is None):
            raise ValueError("a request needs exactly one of text or spec")
        if self.text is not None and self.completer is None:
            raise ValueError("a natural-language request needs a completer")


@dataclass
class CreateOutcome:
    status: Status
    spec: Spec | None = None
    explanation: Explanation = field(default_factory=Explanation)
    usage: dict[str, int] = field(default_factory=dict)
    latency_ms: int = 0
    name: str | None = None
    reason: str | None = None  # MODEL_REJECTED
    questions: tuple[str, ...] = ()  # NEEDS_DETAIL
    error: str | None = None  # INTERPRET_ERROR
    violations: tuple[str, ...] = ()  # POLICY_REJECTED
    # Set when build_image, not the conversation, found the violations.
    verdict: Verdict | None = None
    build: BuildResult | None = None
    verification: VerifyResult | None = None
    exported: tuple[Path, Path] | None = None  # (image, manifest)
    export_error: str | None = None

    @property
    def verified(self) -> bool | None:
        return None if self.verification is None else self.verification.passed


def _violation_lines(verdict: Verdict) -> tuple[str, ...]:
    return tuple(f"{v.location}: [{v.code}] {v.message}" for v in verdict.violations)


def run_create(request: CreateRequest, io: Io) -> CreateOutcome:
    allowlist = request.allowlist or load_allowlist()
    explanation = Explanation()
    usage: dict[str, int] = {}
    latency = 0

    # 1-3. interpret, explain, refuse
    if request.text is not None:
        assert request.completer is not None
        context = None
        if request.base is not None:
            context = (
                f"Base image: {request.base[0]} {request.base[1]} (already chosen in the menu)"
            )
        result = converse(
            request.text,
            request.completer,
            answer=io.answer if io.interactive else None,
            context=context,
            check=policy_checker(allowlist, request.base),
            busy=lambda: io.busy("Interpretando tu petición con el modelo…"),
        )
        usage, latency = result.usage, result.latency_ms
        if result.spec is None or result.interpretation is None:
            if result.reason is not None:
                return CreateOutcome(Status.MODEL_REJECTED, reason=result.reason, usage=usage)
            if result.questions:
                return CreateOutcome(Status.NEEDS_DETAIL, questions=result.questions, usage=usage)
            return CreateOutcome(Status.INTERPRET_ERROR, error=result.error, usage=usage)
        spec = result.spec
        explanation = explain(result.interpretation, spec, allowlist)
        if result.violations:
            return CreateOutcome(
                Status.POLICY_REJECTED,
                spec=spec,
                explanation=explanation,
                usage=usage,
                latency_ms=latency,
                violations=result.violations,
            )
    else:
        assert request.spec is not None
        spec = request.spec

    # The base is the person's choice, not the model's.
    if request.base is not None:
        spec = spec.model_copy(
            update={"base": BaseImage(os=request.base[0], version=request.base[1])}
        )

    # 4. review
    name, verify = request.name, request.verify
    if request.review:
        decision: Decision = io.review(
            Review(
                spec=spec,
                explanation=explanation,
                usage=usage,
                latency_ms=latency,
                name=name,
                verify=verify,
                from_model=request.text is not None,
            )
        )
        if not decision.proceed:
            return CreateOutcome(
                Status.DECLINED, spec=spec, explanation=explanation, usage=usage, latency_ms=latency
            )
        name, verify = decision.name or name, decision.verify

    # 5. build
    try:
        build = build_image(
            spec,
            work_root=request.work_root,
            allowlist=allowlist,
            name=name,
            progress=lambda message: io.emit(Step(message)),
            on_line=lambda text: io.emit(LogLine(text)),
            on_download=lambda done, total: io.emit(Download(done, total)),
        )
    except BuildRejected as rejected:
        return CreateOutcome(
            Status.POLICY_REJECTED,
            spec=spec,
            explanation=explanation,
            usage=usage,
            latency_ms=latency,
            name=name,
            violations=_violation_lines(rejected.verdict),
            verdict=rejected.verdict,
        )

    io.emit(Built(build))
    outcome = CreateOutcome(
        Status.BUILT,
        spec=spec,
        explanation=explanation,
        usage=usage,
        latency_ms=latency,
        name=name,
        build=build,
    )

    # 6. verify
    if verify:
        io.emit(Step("Verificando la imagen: arranca una VM y ejecuta las aserciones"))
        with io.busy("Arrancando la VM y ejecutando goss…"):
            outcome.verification = verify_build(build.build_dir)
        io.emit(Verified(outcome.verification))

    # 7. export
    destination = io.destination(name or build.build_dir.name)
    if destination is not None:
        manifest, manifest_path = load_manifest(build.manifest)
        try:
            outcome.exported = export_image(
                manifest, manifest_path, destination, name or build.build_dir.name
            )
        except ExportError as exc:
            outcome.export_error = str(exc)
    return outcome
