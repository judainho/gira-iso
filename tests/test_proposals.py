"""The rules that decide whether a package may be offered to a person."""

from __future__ import annotations

import dataclasses

import pytest

from gira_iso.archive import ArchivePackage
from gira_iso.policy.evaluate import load_allowlist
from gira_iso.proposals import MAX_PROPOSALS, Proposal, Refusal, assess, precheck


def found(name: str = "nodejs", component: str = "universe") -> ArchivePackage:
    return ArchivePackage(name, "18.19.1", component, "web", "Release")


def test_a_published_universe_package_becomes_a_proposal() -> None:
    result = assess("nodejs", found(), "24.04", load_allowlist())

    assert isinstance(result, Proposal)
    assert (result.package, result.component, result.base_version, result.version) == (
        "nodejs",
        "universe",
        "24.04",
        "18.19.1",
    )


def test_main_is_offered_too() -> None:
    assert isinstance(assess("jq2", found("jq2", "main"), "24.04", load_allowlist()), Proposal)


@pytest.mark.parametrize("component", ["multiverse", "restricted"])
def test_non_free_components_are_refused(component: str) -> None:
    result = assess("something", found("something", component), "24.04", load_allowlist())

    assert isinstance(result, Refusal) and result.code == "component"
    assert component in result.reason and "main and universe" in result.reason


def test_a_package_the_release_does_not_publish_is_refused() -> None:
    result = assess("opencode", None, "24.04", load_allowlist())

    assert isinstance(result, Refusal) and result.code == "not_found"
    assert "does not publish a package named opencode" in result.reason


def test_a_package_on_the_never_list_is_refused_even_though_it_exists() -> None:
    result = assess("telnetd", found("telnetd", "universe"), "24.04", load_allowlist())

    assert isinstance(result, Refusal) and result.code == "never_add"


def test_a_package_that_is_already_allowed_is_not_offered_again() -> None:
    result = assess("nginx", found("nginx", "main"), "24.04", load_allowlist())

    assert isinstance(result, Refusal) and result.code == "already_allowed"


def test_a_package_added_for_another_release_is_still_offered_for_this_one() -> None:
    allowlist = dataclasses.replace(
        load_allowlist(), local_packages={"22.04": frozenset({"nodejs"})}
    )

    assert isinstance(assess("nodejs", found(), "24.04", allowlist), Proposal)
    assert isinstance(assess("nodejs", found(), "22.04", allowlist), Refusal)


@pytest.mark.parametrize("name", ["", "Bad Name", "-x", "a;rm", "A", "x" * 65, "pkg/other"])
def test_invalid_names_are_refused_before_anything_else(name: str) -> None:
    result = precheck(name, "24.04", load_allowlist())

    assert result is not None and result.code == "invalid_name"


def test_precheck_lets_a_new_valid_name_through_to_the_archive() -> None:
    assert precheck("nodejs", "24.04", load_allowlist()) is None


def test_the_proposal_remembers_the_word_the_person_used() -> None:
    result = assess("nodejs", found(), "24.04", load_allowlist(), requested_as="node")

    assert isinstance(result, Proposal) and result.requested_as == "node"


def test_a_word_equal_to_the_package_is_not_repeated() -> None:
    result = assess("nodejs", found(), "24.04", load_allowlist(), requested_as="nodejs")

    assert isinstance(result, Proposal) and result.requested_as is None


def test_the_cap_per_request_is_five() -> None:
    assert MAX_PROPOSALS == 5
