"""Ask the Ubuntu archive whether a package exists for a release.

The answer comes from Launchpad over HTTPS, not from the model: a model that says "this package
exists" is a guess, the archive's publishing record is a fact. Nothing here decides whether a
package is acceptable; that is `proposals.py`. This only reports what the archive publishes.

A failure to reach the archive is never reported as "does not exist": that would turn a flaky
network into a refusal a person could believe.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from gira_iso.errors import EXIT_ENVIRONMENT, EXIT_INVALID, GiraError

LAUNCHPAD = "https://api.launchpad.net/devel/ubuntu"
# Launchpad answered in 0.6 s to 7.8 s for equivalent queries on the same day, so the limit is
# generous; one retry covers a connection that merely stalled.
TIMEOUT = 30  # seconds per attempt
ATTEMPTS = 2
# What apt installs by default. "Proposed" is the staging area and "Backports" is opt-in.
ACCEPTED_POCKETS = frozenset({"Release", "Updates", "Security"})
# Release names for the bases the tool offers. A base the user uploaded has no entry here, so
# nothing can be checked against it and nothing can be added for it.
CODENAMES = {"22.04": "jammy", "24.04": "noble"}

Fetch = Callable[[str], dict[str, Any]]


class ArchiveUnavailable(GiraError):
    exit_code = EXIT_ENVIRONMENT


class UnknownRelease(GiraError):
    exit_code = EXIT_INVALID


@dataclass(frozen=True)
class ArchivePackage:
    name: str
    version: str
    component: str  # main, universe, multiverse or restricted
    section: str
    pocket: str


def fetch_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": "gira-iso"}
    )
    last: Exception | None = None
    for _ in range(ATTEMPTS):
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                return json.load(response)
        except json.JSONDecodeError as exc:
            last = exc
            break  # the server answered; asking again will not change what it said
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc  # a stalled or dropped connection: worth one more try
    raise ArchiveUnavailable(
        f"could not ask the Ubuntu archive (Launchpad): {last}. "
        "This is not an answer about the package; try again."
    ) from last


def codename_for(base_version: str) -> str:
    try:
        return CODENAMES[base_version]
    except KeyError:
        raise UnknownRelease(
            f"ubuntu {base_version} has no known release name, so packages cannot be checked "
            f"against its archive (known: {', '.join(sorted(CODENAMES))})"
        ) from None


def query_url(package: str, codename: str) -> str:
    params = {
        "ws.op": "getPublishedBinaries",
        "binary_name": package,
        "exact_match": "true",
        "status": "Published",
        "distro_arch_series": f"{LAUNCHPAD}/{codename}/amd64",
    }
    return f"{LAUNCHPAD}/+archive/primary?{urllib.parse.urlencode(params)}"


def lookup(package: str, base_version: str, fetch: Fetch | None = None) -> ArchivePackage | None:
    """The newest publication apt would install, or None if the release does not publish it."""
    codename = codename_for(base_version)
    document = (fetch or fetch_json)(query_url(package, codename))
    candidates = [
        entry
        for entry in document.get("entries", [])
        if entry.get("pocket") in ACCEPTED_POCKETS
        and entry.get("status") == "Published"
        and not entry.get("is_debug")
        and entry.get("binary_package_name") == package
    ]
    if not candidates:
        return None
    newest = max(candidates, key=lambda entry: entry.get("date_published") or "")
    return ArchivePackage(
        name=newest["binary_package_name"],
        version=newest["binary_package_version"],
        component=newest["component_name"],
        section=newest.get("section_name") or "",
        pocket=newest["pocket"],
    )
