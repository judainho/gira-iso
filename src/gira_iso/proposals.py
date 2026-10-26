"""Decide whether a package may even be offered to a person for addition.

Pure: no network, no model. The archive's answer comes in as an argument. A `Proposal` is not an
approval: it is what a person will be shown before saying yes. A `Refusal` is final; nobody is
asked, because the answer would be no.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from gira_iso.archive import ArchivePackage
from gira_iso.policy.evaluate import Allowlist

# Software from the Ubuntu components that are maintained and free. Multiverse and restricted
# hold non-free or unsupported software and are not offered.
ALLOWED_COMPONENTS = frozenset({"main", "universe"})
MAX_PROPOSALS = 5
PACKAGE_NAME = re.compile(r"^[a-z0-9][a-z0-9+.-]{0,63}$")

WARNING = (
    "Se instala como root, con los scripts de mantenimiento del paquete, y arrastra sus "
    "dependencias, que tampoco están en la lista."
)


@dataclass(frozen=True)
class Proposal:
    package: str
    base_version: str
    version: str
    component: str
    section: str
    requested_as: str | None = None  # the person's own word, when it differs from the package


@dataclass(frozen=True)
class Refusal:
    package: str
    code: str  # already_allowed, invalid_name, never_add, not_found, component
    reason: str


def precheck(package: str, base_version: str, allowlist: Allowlist) -> Refusal | None:
    """Refusals that need no network, so the archive is not asked about what is settled."""
    if not PACKAGE_NAME.match(package):
        return Refusal(package, "invalid_name", f"{package!r} is not a valid Debian package name")
    if allowlist.allows_package(package, base_version):
        return Refusal(package, "already_allowed", f"{package} is already allowed")
    if package in allowlist.never_add:
        return Refusal(
            package, "never_add", f"{package} is on the list of packages that are never offered"
        )
    return None


def assess(
    package: str,
    found: ArchivePackage | None,
    base_version: str,
    allowlist: Allowlist,
    requested_as: str | None = None,
) -> Proposal | Refusal:
    settled = precheck(package, base_version, allowlist)
    if settled is not None:
        return settled
    if found is None:
        return Refusal(
            package,
            "not_found",
            f"ubuntu {base_version} does not publish a package named {package}",
        )
    if found.component not in ALLOWED_COMPONENTS:
        return Refusal(
            package,
            "component",
            f"{package} is in {found.component}; only {' and '.join(sorted(ALLOWED_COMPONENTS))} "
            "are offered",
        )
    return Proposal(
        package=found.name,
        base_version=base_version,
        version=found.version,
        component=found.component,
        section=found.section,
        requested_as=requested_as if requested_as and requested_as != found.name else None,
    )
