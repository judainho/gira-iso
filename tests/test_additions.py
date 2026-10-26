"""Phase A: packages a person added locally, and what they do to the policy."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from importlib import resources

import pytest
import yaml

from gira_iso import additions
from gira_iso.additions import (
    ALLOW_LOCAL_ENV,
    ORG_ALLOWLIST_ENV,
    STRICT_ENV,
    Addition,
    AdditionsDisabled,
    add_addition,
    canonical_text,
    list_additions,
    local_packages_by_base,
    remove_addition,
    strict_mode,
)
from gira_iso.builder.build import _policy_ref
from gira_iso.builder.manifest import Manifest
from gira_iso.nl.explain import explain
from gira_iso.nl.prompt import catalog_section
from gira_iso.nl.schema import Interpretation
from gira_iso.policy.evaluate import ENV_VAR, allowlist_digest, evaluate, load_allowlist
from gira_iso.spec.models import Spec


def node(base: str = "24.04", **extra) -> Addition:
    values = dict(
        package="nodejs",
        base_version=base,
        component="universe",
        version="18.19.1+dfsg-6ubuntu5",
        added_at="2026-10-26T12:00:00Z",
        request_sha256="a" * 64,
        note="JavaScript runtime",
    )
    return Addition(**{**values, **extra})


def spec(*packages: str, base: str = "24.04") -> Spec:
    return Spec.model_validate(
        {"base": {"os": "ubuntu", "version": base}, "packages": list(packages)}
    )


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in (STRICT_ENV, ALLOW_LOCAL_ENV, ORG_ALLOWLIST_ENV):
        monkeypatch.delenv(name, raising=False)


# --- the store -------------------------------------------------------------------------------


def test_nothing_is_added_by_default() -> None:
    assert list_additions() == []
    assert local_packages_by_base() == {}
    assert canonical_text() == ""


def test_an_addition_is_stored_and_listed() -> None:
    add_addition(node())

    assert [(a.package, a.base_version, a.component) for a in list_additions()] == [
        ("nodejs", "24.04", "universe")
    ]
    assert local_packages_by_base() == {"24.04": frozenset({"nodejs"})}


def test_adding_the_same_package_twice_changes_nothing() -> None:
    add_addition(node())
    add_addition(node(added_at="2027-01-01T00:00:00Z"))

    assert len(list_additions()) == 1
    assert len(additions.log_path().read_text().splitlines()) == 1  # not logged twice


def test_the_same_package_for_another_release_is_a_separate_entry() -> None:
    add_addition(node("24.04"))
    add_addition(node("22.04"))

    assert local_packages_by_base() == {
        "24.04": frozenset({"nodejs"}),
        "22.04": frozenset({"nodejs"}),
    }


def test_removing_one_release_keeps_the_other() -> None:
    add_addition(node("24.04"))
    add_addition(node("22.04"))

    removed = remove_addition("nodejs", "22.04")

    assert [r.base_version for r in removed] == ["22.04"]
    assert local_packages_by_base() == {"24.04": frozenset({"nodejs"})}


def test_removing_without_a_release_removes_every_one() -> None:
    add_addition(node("24.04"))
    add_addition(node("22.04"))

    assert len(remove_addition("nodejs")) == 2
    assert list_additions() == []


def test_removing_something_that_is_not_there_is_not_an_error() -> None:
    assert remove_addition("nodejs") == []


def test_every_change_is_logged_without_the_request_text() -> None:
    add_addition(node())
    remove_addition("nodejs")

    events = [json.loads(line) for line in additions.log_path().read_text().splitlines()]

    assert [e["event"] for e in events] == ["add", "remove"]
    assert events[0]["package"] == "nodejs"
    assert events[0]["request_sha256"] == "a" * 64
    assert "note" not in events[0] and "request" not in events[0]


def test_a_damaged_file_is_ignored_rather_than_trusted() -> None:
    additions.additions_path().parent.mkdir(parents=True, exist_ok=True)
    additions.additions_path().write_text(
        "- package: nodejs\n- {nonsense: true}\n", encoding="utf-8"
    )

    assert list_additions() == []


def test_the_digest_input_ignores_timestamps_and_notes() -> None:
    add_addition(node(added_at="2026-01-01T00:00:00Z", note="one"))
    first = canonical_text()
    remove_addition("nodejs")
    add_addition(node(added_at="2027-06-06T00:00:00Z", note="completely different"))

    assert canonical_text() == first == "24.04:nodejs"


# --- strict mode -------------------------------------------------------------------------------


def test_it_is_not_strict_by_default() -> None:
    assert strict_mode() is False


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_the_explicit_variable_turns_it_on(monkeypatch, value: str) -> None:
    monkeypatch.setenv(STRICT_ENV, value)

    assert strict_mode() is True


@pytest.mark.parametrize("value", ["", "0", "no", "false"])
def test_other_values_leave_it_off(monkeypatch, value: str) -> None:
    monkeypatch.setenv(STRICT_ENV, value)

    assert strict_mode() is False


def test_an_organisation_allowlist_implies_strict(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(ORG_ALLOWLIST_ENV, str(tmp_path / "org.yaml"))

    assert strict_mode() is True


def test_allow_local_overrides_the_organisation_default(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(ORG_ALLOWLIST_ENV, str(tmp_path / "org.yaml"))
    monkeypatch.setenv(ALLOW_LOCAL_ENV, "1")

    assert strict_mode() is False


def test_allow_local_does_not_defeat_the_explicit_variable(monkeypatch) -> None:
    monkeypatch.setenv(STRICT_ENV, "1")
    monkeypatch.setenv(ALLOW_LOCAL_ENV, "1")

    assert strict_mode() is True


def test_in_strict_mode_additions_are_not_loaded_and_cannot_be_made(monkeypatch) -> None:
    add_addition(node())
    monkeypatch.setenv(STRICT_ENV, "1")

    assert list_additions() == []
    assert len(list_additions(include_inactive=True)) == 1  # visible, so a person can see them
    with pytest.raises(AdditionsDisabled, match="strict mode is on"):
        add_addition(node("22.04"))
    with pytest.raises(AdditionsDisabled):
        remove_addition("nodejs")
    assert load_allowlist().local_packages == {}


def test_the_environment_variable_name_matches_the_policy() -> None:
    assert ORG_ALLOWLIST_ENV == ENV_VAR


# --- the policy ----------------------------------------------------------------------------------


def test_a_package_outside_the_list_is_rejected_until_it_is_added() -> None:
    assert "package.not_allowed" in evaluate(spec("nodejs"), load_allowlist()).codes()

    add_addition(node())

    assert evaluate(spec("nodejs"), load_allowlist()).codes() == ()


def test_an_addition_only_counts_for_the_release_it_was_checked_on() -> None:
    add_addition(node("24.04"))

    assert evaluate(spec("nodejs", base="24.04"), load_allowlist()).codes() == ()
    assert "package.not_allowed" in evaluate(spec("nodejs", base="22.04"), load_allowlist()).codes()


def test_the_packaged_list_always_applies_to_every_release() -> None:
    assert evaluate(spec("nginx", base="22.04"), load_allowlist()).codes() == ()


def test_a_package_on_the_never_list_is_refused_even_if_it_was_added() -> None:
    add_addition(node(package="telnetd"))

    verdict = evaluate(spec("telnetd"), load_allowlist())

    assert "package.not_allowed" in verdict.codes()


def test_the_never_list_is_packaged_and_disjoint_from_the_allowed_list() -> None:
    allowlist = load_allowlist()

    assert {"telnetd", "ubuntu-desktop", "ubuntu-server"} <= allowlist.never_add
    assert allowlist.never_add.isdisjoint(allowlist.packages)


def test_an_explicit_allowlist_file_ignores_local_additions(tmp_path) -> None:
    add_addition(node())
    own = tmp_path / "own.yaml"
    packaged = resources.files("gira_iso.policy").joinpath("allowlist.yaml").read_text("utf-8")
    own.write_text(packaged, encoding="utf-8")

    assert load_allowlist(str(own)).local_packages == {}
    assert load_allowlist().local_packages == {"24.04": frozenset({"nodejs"})}


# --- provenance ---------------------------------------------------------------------


def packaged_hash() -> str:
    text = resources.files("gira_iso.policy").joinpath("allowlist.yaml").read_text("utf-8")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_without_additions_the_digest_is_exactly_the_hash_of_the_file() -> None:
    """Existing images must keep the policy hash they were built with."""
    assert allowlist_digest()[1] == packaged_hash()


def test_an_addition_changes_the_digest() -> None:
    before = allowlist_digest()[1]

    add_addition(node())

    assert allowlist_digest()[1] != before


def test_removing_the_addition_restores_the_original_digest() -> None:
    add_addition(node())
    remove_addition("nodejs")

    assert allowlist_digest()[1] == packaged_hash()


def test_strict_mode_gives_the_digest_of_the_file_alone(monkeypatch) -> None:
    add_addition(node())
    monkeypatch.setenv(STRICT_ENV, "1")

    assert allowlist_digest()[1] == packaged_hash()


def test_an_explicit_file_digest_ignores_additions(tmp_path) -> None:
    add_addition(node())
    own = tmp_path / "own.yaml"
    own.write_text("base_images: {}\npackages: []\nservices: []\n", encoding="utf-8")

    assert allowlist_digest(str(own))[1] == hashlib.sha256(own.read_bytes()).hexdigest()


def test_the_manifest_lists_only_the_additions_the_image_uses() -> None:
    add_addition(node())
    add_addition(node(package="unused-tool"))
    add_addition(node("22.04"))

    ref = _policy_ref(spec("nginx", "nodejs"))

    assert [(a.package, a.base_version) for a in ref.local_additions] == [("nodejs", "24.04")]


def test_a_manifest_written_before_additions_existed_still_validates() -> None:
    old = {
        "manifest_version": "1",
        "created_at": "2026-01-01T00:00:00Z",
        "name": "web",
        "spec_sha256": "0" * 64,
        "spec": {"spec_version": "0"},
        "base": {"os": "ubuntu", "version": "24.04", "url": "https://x/y.img", "sha256": "1" * 64},
        "policy": {"source": "packaged:allowlist.yaml", "sha256": "2" * 64},
        "builder": {
            "packer_version": "1.16.2",
            "plugin_version": "1.1.7",
            "template_sha256": "3" * 64,
        },
        "packages": ["nginx"],
        "services": ["nginx"],
        "artifact": {
            "file": "x.qcow2",
            "sha256": "4" * 64,
            "size_bytes": 1,
            "virtual_size_bytes": 1,
        },
    }

    assert Manifest.model_validate(old).policy.local_additions == []


# --- the model is told about them ---------------------------------------------------


def test_the_prompt_lists_local_additions_with_the_releases_they_apply_to() -> None:
    add_addition(node("24.04"))

    section = catalog_section(load_allowlist())

    assert "nodejs (added locally, only for ubuntu 24.04)" in section


def test_the_prompt_has_no_local_marker_without_additions() -> None:
    assert "added locally" not in catalog_section(load_allowlist())


def test_alternatives_may_name_a_locally_added_package() -> None:
    add_addition(node("24.04"))
    interpretation = Interpretation.model_validate(
        {
            "kind": "spec",
            "spec": {"packages": ["nginx"]},
            "unsupported": [
                {"item": "opencode", "kind": "program", "reason": "x", "alternatives": ["nodejs"]}
            ],
        }
    )

    result = explain(interpretation, spec("nginx"), load_allowlist())

    assert result.unsupported[0].alternatives == ["nodejs"]


def test_the_allowlist_dataclass_stays_constructible_without_the_new_fields() -> None:
    allowlist = load_allowlist()

    smaller = dataclasses.replace(allowlist, packages=frozenset({"nginx"}))

    assert smaller.local_packages == allowlist.local_packages
    assert yaml is not None
