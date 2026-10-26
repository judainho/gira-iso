"""Create an image: interpret, check, review, build, verify, export.

The order is fixed here and nowhere else:

    0. preflight  (tools, KVM and disk: fail before spending tokens, downloads or minutes)
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

import dataclasses
import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

from gira_iso import archive
from gira_iso.additions import Addition, add_addition, strict_mode
from gira_iso.archive import ArchiveUnavailable, UnknownRelease
from gira_iso.bases import builds_dir
from gira_iso.builder.build import BuildRejected, BuildResult, build_image
from gira_iso.builder.catalog import load_manifest
from gira_iso.builder.export import ExportError, export_image
from gira_iso.nl.client import Completer
from gira_iso.nl.explain import Explanation, explain
from gira_iso.nl.session import SessionResult, converse, policy_checker
from gira_iso.nl.suggest import suggest_packages
from gira_iso.policy.evaluate import Allowlist, Verdict, load_allowlist
from gira_iso.proposals import Proposal
from gira_iso.spec.models import BaseImage, Spec
from gira_iso.verify.verify import VerifyResult, verify_build
from gira_iso.workflow.additions import Offer, gather, worth_asking
from gira_iso.workflow.events import Built, Download, LogLine, Step, Verified
from gira_iso.workflow.ports import Decision, Io, Review
from gira_iso.workflow.preflight import ensure_environment, ensure_space


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
    preflight: bool = True  # check tools, KVM and disk before spending anything
    # Offer to extend the allowlist with real Ubuntu packages the request needs. Never applies to
    # a custom allowlist, in strict mode, or to a spec that arrives ready-made.
    additions: bool = True

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
    # Packages that could be added to the allowlist but were not asked about (nobody could answer).
    addable: tuple[Proposal, ...] = ()
    # Packages added to the allowlist during this run, because a person approved them.
    added: tuple[str, ...] = ()

    @property
    def verified(self) -> bool | None:
        return None if self.verification is None else self.verification.passed


def _violation_lines(verdict: Verdict) -> tuple[str, ...]:
    return tuple(f"{v.location}: [{v.code}] {v.message}" for v in verdict.violations)


def _interpret(request: CreateRequest, io: Io, allowlist: Allowlist) -> SessionResult:
    assert request.text is not None and request.completer is not None
    context = None
    if request.base is not None:
        context = f"Base image: {request.base[0]} {request.base[1]} (already chosen in the menu)"
    return converse(
        request.text,
        request.completer,
        answer=io.answer if io.interactive else None,
        context=context,
        check=policy_checker(allowlist, request.base),
        busy=lambda: io.busy("Interpretando tu petición con el modelo…"),
    )


def _keep_cost(first: SessionResult, second: SessionResult) -> SessionResult:
    """Asking twice costs twice; report both."""
    usage = dict(first.usage)
    for key, value in second.usage.items():
        usage[key] = usage.get(key, 0) + value
    return dataclasses.replace(second, usage=usage, latency_ms=first.latency_ms + second.latency_ms)


def _additions_apply(request: CreateRequest, result: SessionResult) -> bool:
    return (
        request.additions
        and request.allowlist is None  # a custom list is a reviewed list: nothing widens it
        and not strict_mode()
        and result.spec is not None
        and result.interpretation is not None
    )


def _gather_offer(
    request: CreateRequest, io: Io, allowlist: Allowlist, result: SessionResult
) -> Offer | None:
    """What could be added, or None if the archive cannot be asked (the flow goes on without)."""
    assert result.spec is not None and result.interpretation is not None
    base_version = request.base[1] if request.base else result.spec.base.version
    if not worth_asking(result.spec, result.interpretation, allowlist, base_version):
        return None
    completer = request.completer
    # Asking the model what a word means costs a call; scripts that cannot answer skip it and
    # only look up names that are already package-shaped.
    suggest = (
        (lambda item: suggest_packages(item, completer, base_version))
        if io.interactive and completer is not None
        else None
    )
    try:
        with io.busy("Consultando el archivo de Ubuntu…"):
            return gather(
                result.spec,
                result.interpretation,
                allowlist,
                base_version,
                lookup=lambda name, version: archive.lookup(name, version),
                suggest=suggest,
            )
    except (ArchiveUnavailable, UnknownRelease) as exc:
        io.emit(Step(f"No pude consultar el archivo de Ubuntu ({exc}); sigo sin ampliar la lista"))
        return None


def _store(proposals: tuple[Proposal, ...], approved: set[str], text: str) -> list[str]:
    """Save what the person approved. Nothing the front end did not name is ever saved."""
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    saved = []
    for proposal in proposals:
        if proposal.package in approved:
            add_addition(
                Addition(
                    package=proposal.package,
                    base_version=proposal.base_version,
                    component=proposal.component,
                    version=proposal.version,
                    added_at=stamp,
                    request_sha256=digest,
                )
            )
            saved.append(proposal.package)
    return saved


def run_create(request: CreateRequest, io: Io) -> CreateOutcome:
    # 0. preflight: the machine can build, before the model or the network is touched
    if request.preflight:
        ensure_environment()

    allowlist = request.allowlist or load_allowlist()
    explanation = Explanation()
    usage: dict[str, int] = {}
    latency = 0

    # 1-3. interpret, offer additions, explain, refuse
    addable: tuple[Proposal, ...] = ()
    added: list[str] = []
    if request.text is not None:
        assert request.completer is not None
        result = _interpret(request, io, allowlist)

        # 1b. the request may need real Ubuntu packages the allowlist lacks: offer to add them
        if _additions_apply(request, result):
            offer = _gather_offer(request, io, allowlist, result)
            if offer is not None and (offer.proposals or offer.refusals):
                if io.interactive:
                    approved = set(io.propose(list(offer.proposals), list(offer.refusals)))
                    added = _store(offer.proposals, approved, request.text)
                    if added:  # the catalog the model sees now includes them: ask once more
                        allowlist = load_allowlist()
                        first = result
                        result = _interpret(request, io, allowlist)
                        result = _keep_cost(first, result)
                else:
                    addable = offer.proposals

        def finish(status: Status, **kwargs) -> CreateOutcome:
            return CreateOutcome(status, addable=addable, added=tuple(added), **kwargs)

        usage, latency = result.usage, result.latency_ms
        if result.spec is None or result.interpretation is None:
            if result.reason is not None:
                return finish(Status.MODEL_REJECTED, reason=result.reason, usage=usage)
            if result.questions:
                return finish(Status.NEEDS_DETAIL, questions=result.questions, usage=usage)
            return finish(Status.INTERPRET_ERROR, error=result.error, usage=usage)
        spec = result.spec
        explanation = explain(result.interpretation, spec, allowlist)
        if result.violations:
            return finish(
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
                Status.DECLINED,
                spec=spec,
                explanation=explanation,
                usage=usage,
                latency_ms=latency,
                addable=addable,
                added=tuple(added),
            )
        name, verify = decision.name or name, decision.verify

    # 5. build
    if request.preflight:
        ensure_space(request.work_root or builds_dir(), int(spec.disk_size.removesuffix("G")))
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
            addable=addable,
            added=tuple(added),
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
        addable=addable,
        added=tuple(added),
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
