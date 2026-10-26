"""The Ubuntu archive lookup, with Launchpad's real response shape as test data."""

from __future__ import annotations

import urllib.error
import urllib.parse
from typing import Any

import pytest

from gira_iso import archive
from gira_iso.archive import (
    ArchiveUnavailable,
    UnknownRelease,
    codename_for,
    lookup,
    query_url,
)
from gira_iso.errors import EXIT_ENVIRONMENT, EXIT_INVALID


def entry(name: str = "nodejs", **extra: Any) -> dict[str, Any]:
    """One publication, with the fields Launchpad really returns."""
    values: dict[str, Any] = {
        "binary_package_name": name,
        "binary_package_version": "18.19.1+dfsg-6ubuntu5",
        "component_name": "universe",
        "section_name": "web",
        "pocket": "Release",
        "status": "Published",
        "is_debug": False,
        "date_published": "2024-04-01T10:00:00+00:00",
    }
    return {**values, **extra}


def fetching(*entries: dict[str, Any]):
    seen: list[str] = []

    def fetch(url: str) -> dict[str, Any]:
        seen.append(url)
        return {"total_size": len(entries), "entries": list(entries)}

    fetch.seen = seen  # type: ignore[attr-defined]
    return fetch


def test_a_published_package_is_found_with_its_component_and_version() -> None:
    found = lookup("nodejs", "24.04", fetching(entry()))

    assert found is not None
    assert (found.name, found.component, found.version, found.section) == (
        "nodejs",
        "universe",
        "18.19.1+dfsg-6ubuntu5",
        "web",
    )


def test_a_package_the_release_does_not_publish_is_none_not_an_error() -> None:
    assert lookup("opencode", "24.04", fetching()) is None


def test_the_query_names_the_release_the_exact_package_and_the_architecture() -> None:
    fetch = fetching(entry())

    lookup("nodejs", "22.04", fetch)

    query = urllib.parse.parse_qs(urllib.parse.urlparse(fetch.seen[0]).query)
    assert query["ws.op"] == ["getPublishedBinaries"]
    assert query["binary_name"] == ["nodejs"]
    assert query["exact_match"] == ["true"]
    assert query["status"] == ["Published"]
    assert query["distro_arch_series"] == ["https://api.launchpad.net/devel/ubuntu/jammy/amd64"]
    assert fetch.seen[0].startswith("https://api.launchpad.net/")


def test_a_hostile_name_cannot_reshape_the_query() -> None:
    url = query_url("x&ws.op=other#frag", "noble")

    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert query["ws.op"] == ["getPublishedBinaries"]
    assert query["binary_name"] == ["x&ws.op=other#frag"]


@pytest.mark.parametrize("pocket", ["Proposed", "Backports"])
def test_packages_only_in_staging_or_backports_do_not_count(pocket: str) -> None:
    assert lookup("nodejs", "24.04", fetching(entry(pocket=pocket))) is None


@pytest.mark.parametrize("pocket", ["Release", "Updates", "Security"])
def test_the_pockets_apt_installs_from_do_count(pocket: str) -> None:
    assert lookup("nodejs", "24.04", fetching(entry(pocket=pocket))) is not None


def test_the_newest_accepted_publication_wins() -> None:
    older = entry(binary_package_version="1.0", date_published="2024-01-01T00:00:00+00:00")
    newer = entry(
        binary_package_version="1.1",
        pocket="Security",
        date_published="2025-01-01T00:00:00+00:00",
    )
    staging = entry(
        binary_package_version="9.9",
        pocket="Proposed",
        date_published="2026-01-01T00:00:00+00:00",
    )

    found = lookup("nodejs", "24.04", fetching(older, staging, newer))

    assert found is not None and found.version == "1.1"


def test_debug_symbol_packages_and_other_names_are_ignored() -> None:
    entries = (entry(is_debug=True), entry(name="nodejs-doc"))

    assert lookup("nodejs", "24.04", fetching(*entries)) is None


def test_unpublished_entries_are_ignored() -> None:
    assert lookup("nodejs", "24.04", fetching(entry(status="Superseded"))) is None


# --- releases -------------------------------------------------------------------------


def test_the_two_offered_bases_have_release_names() -> None:
    assert codename_for("22.04") == "jammy"
    assert codename_for("24.04") == "noble"


def test_a_base_without_a_release_name_cannot_be_checked() -> None:
    with pytest.raises(UnknownRelease, match="no known release name") as caught:
        codename_for("26.10")

    assert caught.value.exit_code == EXIT_INVALID


def test_an_unknown_release_never_reaches_the_network() -> None:
    fetch = fetching()

    with pytest.raises(UnknownRelease):
        lookup("nodejs", "26.10", fetch)

    assert fetch.seen == []


# --- failures are not answers ---------------------------------------------------------


def test_a_network_failure_is_unavailable_not_absent(monkeypatch) -> None:
    def broken(request, timeout=None):
        raise urllib.error.URLError("name resolution failed")

    monkeypatch.setattr(archive.urllib.request, "urlopen", broken)

    with pytest.raises(ArchiveUnavailable, match="not an answer about the package") as caught:
        lookup("nodejs", "24.04")

    assert caught.value.exit_code == EXIT_ENVIRONMENT


def test_a_timeout_is_unavailable(monkeypatch) -> None:
    def slow(request, timeout=None):
        raise TimeoutError("timed out")

    monkeypatch.setattr(archive.urllib.request, "urlopen", slow)

    with pytest.raises(ArchiveUnavailable):
        lookup("nodejs", "24.04")


def test_a_reply_that_is_not_json_is_unavailable(monkeypatch) -> None:
    import io

    monkeypatch.setattr(
        archive.urllib.request,
        "urlopen",
        lambda request, timeout=None: io.BytesIO(b"<html>maintenance</html>"),
    )

    with pytest.raises(ArchiveUnavailable):
        lookup("nodejs", "24.04")


def test_the_request_has_a_timeout_and_asks_for_json(monkeypatch) -> None:
    import io

    seen = {}

    def spy(request, timeout=None):
        seen["timeout"] = timeout
        seen["accept"] = request.get_header("Accept")
        return io.BytesIO(b'{"entries": []}')

    monkeypatch.setattr(archive.urllib.request, "urlopen", spy)

    assert lookup("nodejs", "24.04") is None
    assert seen == {"timeout": archive.TIMEOUT, "accept": "application/json"}


# --- the real archive, opt-in ---------------------------------------------------------


@pytest.mark.network
def test_launchpad_agrees_with_what_the_plan_observed() -> None:
    """Run with `pytest -m network`. These are the facts the design rests on."""
    assert lookup("jq", "24.04").component == "main"
    assert lookup("nodejs", "24.04").component == "universe"
    assert lookup("ca-certificates", "24.04") is not None  # an architecture-independent package
    assert lookup("opencode", "24.04") is None


def test_one_dropped_connection_is_retried(monkeypatch) -> None:
    import io

    attempts = []

    def flaky(request, timeout=None):
        attempts.append(1)
        if len(attempts) == 1:
            raise TimeoutError("stalled")
        return io.BytesIO(b'{"entries": []}')

    monkeypatch.setattr(archive.urllib.request, "urlopen", flaky)

    assert lookup("nodejs", "24.04") is None
    assert len(attempts) == 2


def test_it_gives_up_after_the_second_failure(monkeypatch) -> None:
    attempts = []

    def down(request, timeout=None):
        attempts.append(1)
        raise TimeoutError("stalled")

    monkeypatch.setattr(archive.urllib.request, "urlopen", down)

    with pytest.raises(ArchiveUnavailable):
        lookup("nodejs", "24.04")
    assert len(attempts) == archive.ATTEMPTS == 2


def test_an_answer_that_is_not_json_is_not_retried(monkeypatch) -> None:
    import io

    attempts = []

    def html(request, timeout=None):
        attempts.append(1)
        return io.BytesIO(b"<html>maintenance</html>")

    monkeypatch.setattr(archive.urllib.request, "urlopen", html)

    with pytest.raises(ArchiveUnavailable):
        lookup("nodejs", "24.04")
    assert len(attempts) == 1
