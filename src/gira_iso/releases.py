"""Ubuntu release data shared by the policy, the renderer and the archive client.

Codename lookups are pure data, so they live apart from the network module that happens to
need them. A release the tool has never heard of cannot be resolved: nothing is guessed,
because a guessed codename would silently ask the wrong archive or write the wrong suite.
"""

from __future__ import annotations

from gira_iso.errors import EXIT_INVALID, GiraError

# The releases the tool offers. A base uploaded by the user has no entry here, so nothing can
# be resolved against it: repository suites and archive lookups both refuse.
CODENAMES = {"22.04": "jammy", "24.04": "noble"}


class UnknownRelease(GiraError):
    exit_code = EXIT_INVALID


def codename_for(base_version: str) -> str:
    try:
        return CODENAMES[base_version]
    except KeyError:
        raise UnknownRelease(
            f"ubuntu {base_version} has no known release name, so packages cannot be checked "
            f"against its archive (known: {', '.join(sorted(CODENAMES))})"
        ) from None
