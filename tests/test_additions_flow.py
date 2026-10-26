"""Phase C: the request needs a real Ubuntu package the allowlist lacks."""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager

import pytest

from gira_iso.additions import STRICT_ENV, list_additions
from gira_iso.archive import ArchivePackage, ArchiveUnavailable
from gira_iso.nl.client import Completion
from gira_iso.policy.evaluate import load_allowlist
from gira_iso.spec.models import Spec
from gira_iso.workflow.create import CreateRequest, Status, run_create
from gira_iso.workflow.events import Step
from gira_iso.workflow.ports import Decision

ARCHIVE = {
    "nodejs": ArchivePackage("nodejs", "18.19.1", "universe", "web", "Release"),
    "telnetd": ArchivePackage("telnetd", "0.17", "universe", "net", "Release"),
    "nonfree": ArchivePackage("nonfree", "1.0", "multiverse", "misc", "Release"),
}


def spec_reply(packages: list[str], unsupported: list[dict] | None = None, **extra) -> str:
    body: dict = {"kind": "spec", "spec": {"packages": packages}, **extra}
    if unsupported is not None:
        body["unsupported"] = unsupported
    return json.dumps(body)


def claim(item: str, kind: str = "package") -> dict:
    return {"item": item, "kind": kind, "reason": "no está en la lista"}


def says_node_is_missing() -> str:
    return spec_reply(["nginx"], [claim("node")])


def with_node() -> str:
    understood = [{"requested": "node", "resolved": "nodejs", "kind": "package"}]
    return spec_reply(["nginx", "nodejs"], understood=understood)


def suggestion(*names: str) -> str:
    return json.dumps({"candidates": list(names)})


class Model:
    """Answers in order and records every system/user pair."""

    def __init__(self, *replies: str) -> None:
        self._replies = list(replies)
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str) -> Completion:
        self.calls.append((system, user))
        return Completion(
            text=self._replies.pop(0),
            usage={"prompt_tokens": 100, "completion_tokens": 10},
        )


class Io:
    def __init__(self, *, interactive=True, approve=()) -> None:
        self.interactive = interactive
        self.approve = list(approve)
        self.proposed: list = []
        self.refused: list = []
        self.calls: list[str] = []
        self.events: list = []

    def emit(self, event):
        self.events.append(event)

    def answer(self, questions):
        return ["x"] * len(questions)

    @contextmanager
    def busy(self, message):
        self.calls.append(f"busy:{message.split()[0]}")
        yield

    def review(self, review):
        self.calls.append("review")
        return Decision(proceed=True, name="web")

    def propose(self, proposals, refusals):
        self.calls.append("propose")
        self.proposed = list(proposals)
        self.refused = list(refusals)
        return list(self.approve)

    def destination(self, name):
        return None


@pytest.fixture
def archive(monkeypatch):
    asked: list[tuple[str, str]] = []

    def lookup(package, base_version, fetch=None):
        asked.append((package, base_version))
        return ARCHIVE.get(package)

    monkeypatch.setattr("gira_iso.archive.lookup", lookup)
    return asked


@pytest.fixture
def build(monkeypatch, tmp_path, make_build):
    built: list[Spec] = []
    fake = make_build(tmp_path / "builds")

    def fake_build(spec, **kwargs):
        built.append(spec)
        return fake

    monkeypatch.setattr("gira_iso.workflow.create.build_image", fake_build)
    return built


def request(model, **extra) -> CreateRequest:
    return CreateRequest(text="nginx y node", completer=model, **extra)


# --- approving -------------------------------------------------------------------------------


def test_an_approved_package_is_stored_and_the_request_is_interpreted_again(archive, build) -> None:
    model = Model(says_node_is_missing(), suggestion("nodejs"), with_node())
    io = Io(approve=["nodejs"])

    outcome = run_create(request(model), io)

    assert outcome.status is Status.BUILT
    assert outcome.added == ("nodejs",)
    assert [p.package for p in io.proposed] == ["nodejs"]
    assert io.proposed[0].requested_as == "node"  # the person's own word is kept
    assert build[0].packages == ["nginx", "nodejs"]  # the second interpretation, now allowed


def test_what_is_stored_names_the_release_and_hashes_the_request(archive, build) -> None:
    model = Model(says_node_is_missing(), suggestion("nodejs"), with_node())

    run_create(request(model, base=("ubuntu", "24.04")), Io(approve=["nodejs"]))

    (stored,) = list_additions()
    assert (stored.package, stored.base_version, stored.component) == (
        "nodejs",
        "24.04",
        "universe",
    )
    assert stored.request_sha256 == hashlib.sha256(b"nginx y node").hexdigest()
    assert stored.note is None


def test_the_second_interpretation_sees_the_new_package_in_the_catalog(archive, build) -> None:
    model = Model(says_node_is_missing(), suggestion("nodejs"), with_node())

    run_create(request(model), Io(approve=["nodejs"]))

    first_system, second_system = model.calls[0][0], model.calls[2][0]
    assert "nodejs (added locally" not in first_system
    assert "nodejs (added locally, only for ubuntu 24.04)" in second_system


def test_the_cost_of_asking_twice_is_reported(archive, build) -> None:
    model = Model(says_node_is_missing(), suggestion("nodejs"), with_node())

    outcome = run_create(request(model), Io(approve=["nodejs"]))

    # Two interpretations; the suggestion call is separate from the spec's cost.
    assert outcome.usage["prompt_tokens"] == 200


def test_the_archive_is_asked_for_the_release_chosen_in_the_menu(archive, build) -> None:
    model = Model(says_node_is_missing(), suggestion("nodejs"), with_node())

    run_create(request(model, base=("ubuntu", "22.04")), Io(approve=["nodejs"]))

    assert ("nodejs", "22.04") in archive
    assert list_additions()[0].base_version == "22.04"


def test_only_what_the_person_approved_is_stored(archive, build) -> None:
    model = Model(says_node_is_missing(), suggestion("nodejs"))

    run_create(request(model), Io(approve=["telnetd", "not-offered"]))

    assert list_additions() == []  # names the workflow never proposed are ignored


# --- declining ---------------------------------------------------------------------------------


def test_declining_stores_nothing_and_the_flow_goes_on_without_the_package(archive, build) -> None:
    model = Model(says_node_is_missing(), suggestion("nodejs"))

    outcome = run_create(request(model), Io(approve=[]))

    assert outcome.status is Status.BUILT
    assert list_additions() == [] and outcome.added == ()
    assert build[0].packages == ["nginx"]
    assert len(model.calls) == 2  # no third call: nothing changed, nothing to ask again


# --- when it does not run ---------------------------------------------------------------------


def test_a_request_with_nothing_outside_the_list_never_touches_the_archive(archive, build) -> None:
    io = Io()

    run_create(request(Model(spec_reply(["nginx"]))), io)

    assert archive == [] and "propose" not in io.calls
    assert "busy:Consultando" not in io.calls  # no spinner for nothing


def test_a_program_the_archive_does_not_know_stays_unsupported_without_a_prompt(
    archive, build
) -> None:
    program = spec_reply(["nginx"], [claim("opencode", "program")])
    io = Io()

    outcome = run_create(request(Model(program, suggestion())), io)

    assert outcome.status is Status.BUILT
    assert ("opencode", "24.04") in archive  # looked up literally, and not found
    assert "propose" not in io.calls  # nothing to offer, so nobody is bothered
    assert [c.item for c in outcome.explanation.unsupported] == ["opencode"]


def test_services_are_never_offered_for_addition(archive, build) -> None:
    service = spec_reply(["nginx"], [claim("mosquitto", "service")])

    run_create(request(Model(service)), Io())

    assert archive == []


def test_strict_mode_does_not_even_ask_the_archive(monkeypatch, archive, build) -> None:
    monkeypatch.setenv(STRICT_ENV, "1")
    io = Io(approve=["nodejs"])

    outcome = run_create(request(Model(says_node_is_missing())), io)

    assert archive == [] and "propose" not in io.calls
    assert outcome.addable == () and list_additions(include_inactive=True) == []


def test_a_custom_allowlist_is_never_extended(archive, build) -> None:
    io = Io(approve=["nodejs"])

    run_create(request(Model(says_node_is_missing()), allowlist=load_allowlist()), io)

    assert archive == [] and "propose" not in io.calls


def test_additions_can_be_switched_off_per_request(archive, build) -> None:
    io = Io(approve=["nodejs"])

    run_create(request(Model(says_node_is_missing()), additions=False), io)

    assert archive == [] and "propose" not in io.calls


def test_a_ready_made_spec_is_never_extended(archive, build) -> None:
    spec = Spec.model_validate({"packages": ["nginx"]})

    run_create(CreateRequest(spec=spec, name="web", review=False), Io(approve=["nodejs"]))

    assert archive == []


# --- nobody to ask ------------------------------------------------------------------------------


def test_without_anyone_to_ask_the_outcome_lists_what_could_be_added(archive, build) -> None:
    io = Io(interactive=False, approve=["nodejs"])
    model = Model(spec_reply(["nginx"], [claim("nodejs")]))

    outcome = run_create(request(model), io)

    assert [p.package for p in outcome.addable] == ["nodejs"]
    assert outcome.added == () and list_additions() == []
    assert "propose" not in io.calls
    assert len(model.calls) == 1  # no suggestion call and no second interpretation


def test_without_anyone_to_ask_the_model_is_not_asked_for_suggestions(archive, build) -> None:
    model = Model(says_node_is_missing())

    run_create(request(model), Io(interactive=False))

    assert len(model.calls) == 1
    assert ("node", "24.04") in archive  # only the literal word was checked


# --- things that go wrong --------------------------------------------------------------------


def test_an_unreachable_archive_does_not_stop_the_request(monkeypatch, build) -> None:
    def down(package, base_version, fetch=None):
        raise ArchiveUnavailable("could not ask the Ubuntu archive (Launchpad): offline")

    monkeypatch.setattr("gira_iso.archive.lookup", down)
    io = Io(approve=["nodejs"])

    outcome = run_create(request(Model(says_node_is_missing(), suggestion("nodejs"))), io)

    assert outcome.status is Status.BUILT
    steps = [e.message for e in io.events if isinstance(e, Step)]
    assert any("No pude consultar el archivo de Ubuntu" in m for m in steps)
    assert list_additions() == []


def test_a_base_without_a_release_name_skips_the_offer(monkeypatch, build, tmp_path) -> None:
    from gira_iso.archive import codename_for
    from gira_iso.bases import register_base

    def like_the_real_lookup(package, base_version, fetch=None):
        codename_for(base_version)  # raises UnknownRelease for a release it has no name for
        return ARCHIVE.get(package)

    monkeypatch.setattr("gira_iso.archive.lookup", like_the_real_lookup)
    image = tmp_path / "focal.img"
    image.write_bytes(b"QFI\xfb" + b"\0" * 64)
    register_base(image, "20.04")  # allowed by the policy, but Launchpad has no name for it
    io = Io(approve=["nodejs"])
    model = Model(says_node_is_missing(), suggestion("nodejs"))

    outcome = run_create(request(model, base=("ubuntu", "20.04")), io)

    assert outcome.status is Status.BUILT
    assert "propose" not in io.calls and outcome.added == ()
    steps = [e.message for e in io.events if isinstance(e, Step)]
    assert any("no known release name" in m for m in steps)


def test_a_model_that_fails_to_suggest_still_lets_the_literal_name_through(archive, build) -> None:
    model = Model(spec_reply(["nginx"], [claim("nodejs")]), "this is not json", with_node())
    io = Io(approve=["nodejs"])

    outcome = run_create(request(model), io)

    assert outcome.added == ("nodejs",)


# --- held back on purpose ----------------------------------------------------------------------


def test_a_package_that_exists_but_is_never_offered_is_explained(archive, build) -> None:
    asks = spec_reply(["nginx"], [claim("telnetd")])
    io = Io()

    run_create(request(Model(asks, suggestion())), io)

    assert "propose" in io.calls
    assert io.proposed == []
    assert [(r.package, r.code) for r in io.refused] == [("telnetd", "never_add")]
    assert ("telnetd", "24.04") not in archive  # settled by the rules, no network spent


def test_non_free_components_are_explained_too(archive, build) -> None:
    asks = spec_reply(["nginx"], [claim("nonfree")])
    io = Io()

    run_create(request(Model(asks, suggestion())), io)

    assert [(r.package, r.code) for r in io.refused] == [("nonfree", "component")]


# --- the policy violation path ---------------------------------------------------------------


def test_a_package_the_model_put_in_the_spec_is_offered_too(archive, build) -> None:
    in_spec = spec_reply(["nginx", "nodejs"])
    model = Model(in_spec, in_spec, in_spec, in_spec)  # the policy keeps rejecting it at first
    io = Io(approve=["nodejs"])

    outcome = run_create(request(model), io)

    assert outcome.added == ("nodejs",)
    assert outcome.status is Status.BUILT
    assert build[0].packages == ["nginx", "nodejs"]


def test_the_cap_on_lookups_keeps_a_flood_of_requests_cheap(archive, build) -> None:
    many = spec_reply(["nginx"], [claim(f"tool{i}") for i in range(20)])

    run_create(request(Model(many)), Io(interactive=False))

    assert len(archive) <= 6
