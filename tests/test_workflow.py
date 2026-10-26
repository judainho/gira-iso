"""The workflow decides the order; a fake front end records what it was asked."""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

import pytest
import yaml

from gira_iso.builder.build import BuildRejected
from gira_iso.builder.export import ExportError
from gira_iso.errors import GiraError
from gira_iso.nl.client import Completion
from gira_iso.policy.evaluate import Verdict, Violation
from gira_iso.spec.models import Spec
from gira_iso.verify.verify import CheckResult, VerifyResult
from gira_iso.workflow.create import CreateRequest, Status, run_create
from gira_iso.workflow.events import Built, Download, LogLine, Step, Verified
from gira_iso.workflow.ports import Decision, Review

SPEC = yaml.safe_load(Path("examples/nginx-web.yaml").read_text(encoding="utf-8"))


class FakeIo:
    def __init__(
        self, *, interactive=True, proceed=True, verify=False, destination=None, name=None
    ):
        self.interactive = interactive
        self.calls: list[str] = []
        self.events: list = []
        self.reviews: list[Review] = []
        self.questions: list[list[str]] = []
        self._decision = Decision(proceed=proceed, name=name, verify=verify)
        self._destination = destination

    def emit(self, event):
        self.events.append(event)
        self.calls.append(f"emit:{type(event).__name__}")

    def answer(self, questions):
        self.calls.append("answer")
        self.questions.append(questions)
        return ["deploy" for _ in questions]

    @contextmanager
    def busy(self, message):
        self.calls.append(f"busy:{message.split()[0]}")
        yield

    def review(self, review):
        self.calls.append("review")
        self.reviews.append(review)
        return self._decision

    def propose(self, proposals, refusals):
        self.calls.append("propose")
        return []

    def destination(self, name):
        self.calls.append("destination")
        return self._destination


class Scripted:
    def __init__(self, *replies: dict) -> None:
        self._replies = [json.dumps(r) for r in replies]
        self.prompts: list[str] = []

    def complete(self, system: str, user: str) -> Completion:
        self.prompts.append(user)
        return Completion(text=self._replies.pop(0))


def good() -> dict:
    return {"kind": "spec", "spec": SPEC}


@pytest.fixture
def build(monkeypatch, tmp_path, make_build):
    """Replace build_image/verify_build; record what the workflow asked of them."""
    record: dict = {"specs": [], "names": [], "verified": 0}
    fake = make_build(tmp_path / "builds")

    def fake_build(spec, **kwargs):
        record["specs"].append(spec)
        record["names"].append(kwargs["name"])
        kwargs["progress"]("Validando")
        kwargs["on_line"]("==> qemu: line")
        kwargs["on_download"](5, 10)
        return fake

    def fake_verify(build_dir):
        record["verified"] += 1
        return VerifyResult(checks=(CheckResult("goss assertions", True, "20/20"),), goss_output="")

    monkeypatch.setattr("gira_iso.workflow.create.build_image", fake_build)
    monkeypatch.setattr("gira_iso.workflow.create.verify_build", fake_verify)
    record["fake"] = fake
    return record


# --- the order ------------------------------------------------------------------------


def test_a_natural_language_request_runs_every_step_in_order(build, tmp_path) -> None:
    io = FakeIo(verify=True, destination=tmp_path / "out", name="web")

    outcome = run_create(CreateRequest(text="un nginx", completer=Scripted(good())), io)

    assert outcome.status is Status.BUILT
    assert io.calls == [
        "busy:Interpretando",
        "review",
        "emit:Step",
        "emit:LogLine",
        "emit:Download",
        "emit:Built",
        "emit:Step",  # verification announced
        "busy:Arrancando",
        "emit:Verified",
        "destination",
    ]
    assert outcome.verified is True
    assert outcome.exported is not None
    assert (tmp_path / "out" / "web.qcow2").is_file()


def test_build_events_reach_the_front_end_typed(build) -> None:
    io = FakeIo()

    run_create(CreateRequest(text="x", completer=Scripted(good())), io)

    assert Step("Validando") in io.events
    assert LogLine("==> qemu: line") in io.events
    assert Download(5, 10) in io.events
    assert any(isinstance(event, Built) for event in io.events)


def test_the_review_sees_what_the_model_understood(build) -> None:
    reply = good() | {
        "understood": [{"requested": "web server", "resolved": "nginx", "kind": "package"}]
    }
    io = FakeIo()

    run_create(CreateRequest(text="x", completer=Scripted(reply), name="web", verify=True), io)

    review = io.reviews[0]
    assert review.from_model is True
    assert review.name == "web"
    assert review.verify is True
    assert [c.resolved for c in review.explanation.understood] == ["nginx"]


def test_a_spec_file_skips_interpretation(build) -> None:
    io = FakeIo()

    outcome = run_create(CreateRequest(spec=Spec.model_validate(SPEC), name="web"), io)

    assert outcome.status is Status.BUILT
    assert "busy:Interpretando" not in io.calls
    assert io.reviews[0].from_model is False


def test_review_can_be_skipped(build) -> None:
    io = FakeIo()

    run_create(CreateRequest(spec=Spec.model_validate(SPEC), review=False), io)

    assert "review" not in io.calls
    assert len(build["specs"]) == 1


# --- the decisions belong to the person, not the model ---------------------------------


def test_the_base_chosen_by_the_person_replaces_the_models(build) -> None:
    model = Scripted(good())
    io = FakeIo()

    run_create(CreateRequest(text="x", completer=model, base=("ubuntu", "22.04")), io)

    assert build["specs"][0].base.version == "22.04"
    assert "Base image: ubuntu 22.04" in model.prompts[0]


def test_a_name_chosen_in_the_review_replaces_the_requested_one(build) -> None:
    io = FakeIo(name="from-review")

    outcome = run_create(CreateRequest(text="x", completer=Scripted(good()), name="asked"), io)

    assert build["names"] == ["from-review"]
    assert outcome.name == "from-review"


def test_verification_runs_only_when_the_person_asks(build) -> None:
    run_create(CreateRequest(text="x", completer=Scripted(good())), FakeIo(verify=False))
    assert build["verified"] == 0

    run_create(CreateRequest(text="x", completer=Scripted(good())), FakeIo(verify=True))
    assert build["verified"] == 1


# --- the stops -------------------------------------------------------------------------


def test_declining_the_review_builds_nothing(build) -> None:
    outcome = run_create(CreateRequest(text="x", completer=Scripted(good())), FakeIo(proceed=False))

    assert outcome.status is Status.DECLINED
    assert build["specs"] == []


def test_a_refusal_by_the_model_stops_before_the_review(build) -> None:
    io = FakeIo()
    refusal = {"kind": "reject", "reason": "not an image"}

    outcome = run_create(CreateRequest(text="coffee", completer=Scripted(refusal)), io)

    assert outcome.status is Status.MODEL_REJECTED
    assert outcome.reason == "not an image"
    assert "review" not in io.calls and build["specs"] == []


def test_questions_without_anyone_to_answer_stop_the_workflow(build) -> None:
    ask = {"kind": "questions", "questions": ["Which user?"]}
    io = FakeIo(interactive=False)

    outcome = run_create(CreateRequest(text="x", completer=Scripted(ask)), io)

    assert outcome.status is Status.NEEDS_DETAIL
    assert outcome.questions == ("Which user?",)
    assert "answer" not in io.calls


def test_questions_are_put_to_an_interactive_front_end(build) -> None:
    ask = {"kind": "questions", "questions": ["Which user?"]}
    io = FakeIo()

    outcome = run_create(CreateRequest(text="x", completer=Scripted(ask, good())), io)

    assert outcome.status is Status.BUILT
    assert io.questions == [["Which user?"]]


def test_a_model_that_never_answers_validly_is_an_error(build) -> None:
    class Garbage:
        def complete(self, system, user):
            return Completion(text="not json")

    outcome = run_create(CreateRequest(text="x", completer=Garbage()), FakeIo())

    assert outcome.status is Status.INTERPRET_ERROR
    assert outcome.error and build["specs"] == []


def test_a_spec_the_policy_rejects_never_reaches_the_review(build) -> None:
    bad = {"kind": "spec", "spec": {"packages": ["opencode"]}}
    io = FakeIo()

    outcome = run_create(CreateRequest(text="x", completer=Scripted(bad, bad, bad)), io)

    assert outcome.status is Status.POLICY_REJECTED
    assert any("opencode" in line for line in outcome.violations)
    assert outcome.verdict is None  # found during the conversation, not by build_image
    assert "review" not in io.calls and build["specs"] == []


def test_the_final_gate_still_rejects_what_build_image_finds(monkeypatch, build) -> None:
    verdict = Verdict(violations=(Violation("package.not_allowed", "no", "packages[0]"),))

    def reject(spec, **kwargs):
        raise BuildRejected(verdict)

    monkeypatch.setattr("gira_iso.workflow.create.build_image", reject)

    outcome = run_create(CreateRequest(text="x", completer=Scripted(good())), FakeIo())

    assert outcome.status is Status.POLICY_REJECTED
    assert outcome.verdict is verdict
    assert outcome.violations == ("packages[0]: [package.not_allowed] no",)


# --- failures and cancellation propagate ------------------------------------------------


def test_an_expected_failure_propagates_to_the_front_end(monkeypatch, build) -> None:
    class Boom(GiraError):
        exit_code = 3

    def explode(spec, **kwargs):
        raise Boom("packer exited with 1")

    monkeypatch.setattr("gira_iso.workflow.create.build_image", explode)

    with pytest.raises(Boom):
        run_create(CreateRequest(text="x", completer=Scripted(good())), FakeIo())


def test_ctrl_c_propagates_and_nothing_after_it_runs(monkeypatch, build) -> None:
    def interrupted(spec, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr("gira_iso.workflow.create.build_image", interrupted)
    io = FakeIo(verify=True)

    with pytest.raises(KeyboardInterrupt):
        run_create(CreateRequest(text="x", completer=Scripted(good())), io)

    assert "destination" not in io.calls and build["verified"] == 0


# --- export ----------------------------------------------------------------------------


def test_no_destination_leaves_the_image_in_the_builds_directory(build) -> None:
    outcome = run_create(CreateRequest(text="x", completer=Scripted(good())), FakeIo())

    assert outcome.exported is None and outcome.export_error is None


def test_an_export_failure_is_reported_not_raised(monkeypatch, build, tmp_path) -> None:
    def refuse(*args, **kwargs):
        raise ExportError("already exists (use --force)")

    monkeypatch.setattr("gira_iso.workflow.create.export_image", refuse)

    outcome = run_create(
        CreateRequest(text="x", completer=Scripted(good())), FakeIo(destination=tmp_path)
    )

    assert outcome.status is Status.BUILT
    assert outcome.export_error == "already exists (use --force)"


# --- the contract ----------------------------------------------------------------------


def test_a_request_needs_exactly_one_source() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        CreateRequest()
    with pytest.raises(ValueError, match="exactly one"):
        CreateRequest(text="x", spec=Spec.model_validate(SPEC), completer=Scripted())


def test_a_natural_language_request_needs_a_completer() -> None:
    with pytest.raises(ValueError, match="completer"):
        CreateRequest(text="x")


def test_the_workflow_knows_nothing_about_any_front_end() -> None:
    for module in Path("src/gira_iso/workflow").glob("*.py"):
        source = module.read_text(encoding="utf-8")
        for forbidden in (
            "import typer",
            "import rich",
            "from rich",
            "gira_iso.cli",
            "gira_iso.tui",
        ):
            assert forbidden not in source, f"{module.name} imports {forbidden}"


def test_verified_event_carries_the_result(build) -> None:
    io = FakeIo(verify=True)

    run_create(CreateRequest(text="x", completer=Scripted(good())), io)

    verified = [event for event in io.events if isinstance(event, Verified)]
    assert len(verified) == 1 and verified[0].result.passed
