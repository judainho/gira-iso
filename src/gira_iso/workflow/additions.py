"""Gather what could be added to the allowlist for a request, without adding anything.

Candidates come from two places: packages the model put in the spec that the policy does not
allow, and things the model said it could not provide. For the second kind the model is asked
what Ubuntu package the person's word means (`nl/suggest.py`). Every candidate is then checked
against the Ubuntu archive by code, so the model only ever supplies names.

Nothing is stored here and nobody is asked; the workflow does that with the result.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from gira_iso.archive import ArchivePackage
from gira_iso.nl.schema import Interpretation
from gira_iso.policy.evaluate import Allowlist
from gira_iso.proposals import MAX_PROPOSALS, PACKAGE_NAME, Proposal, Refusal, assess, precheck
from gira_iso.spec.models import Spec

# The archive answers in 0.6 to 8 seconds per name: more than this makes the wait noticeable.
MAX_LOOKUPS = 6
# Refusals worth telling the person about: the package exists but is held back on purpose. The
# others (it does not exist, it is already allowed) are visible elsewhere or are noise.
SURPRISING = frozenset({"never_add", "component"})

Lookup = Callable[[str, str], ArchivePackage | None]
Suggest = Callable[[str], list[str]]


@dataclass(frozen=True)
class Offer:
    proposals: tuple[Proposal, ...] = ()
    refusals: tuple[Refusal, ...] = ()


def candidates(
    spec: Spec,
    interpretation: Interpretation,
    allowlist: Allowlist,
    base_version: str,
    suggest: Suggest | None,
) -> list[tuple[str, str | None]]:
    """(package name, the person's own word) pairs, in order, without repeats."""
    found: list[tuple[str, str | None]] = []

    def add(name: str, word: str | None) -> None:
        if name not in {existing for existing, _ in found}:
            found.append((name, word))

    # The model put it in the spec and the policy would reject it.
    for package in spec.packages:
        if not allowlist.allows_package(package, base_version):
            add(package, None)

    # The model said it could not provide it.
    for claim in interpretation.unsupported or []:
        if claim.kind == "service":
            continue  # services stay curated; this feature only extends packages
        literal = claim.item.strip().lower()
        if PACKAGE_NAME.match(literal):
            add(literal, None)
        if suggest is not None:
            for name in suggest(claim.item):
                add(name, claim.item)
    return found[:MAX_LOOKUPS]


def check_package(
    name: str,
    base_version: str,
    allowlist: Allowlist,
    *,
    lookup: Lookup,
    requested_as: str | None = None,
) -> Proposal | Refusal:
    """Everything between a package name and a question to a person.

    Rules that need no network are applied first, so what is already settled costs no lookup.
    """
    settled = precheck(name, base_version, allowlist)
    if settled is not None:
        return settled
    return assess(name, lookup(name, base_version), base_version, allowlist, requested_as)


def worth_asking(
    spec: Spec, interpretation: Interpretation, allowlist: Allowlist, base_version: str
) -> bool:
    """Is there anything that could be added? Free to answer: no network, no model.

    The common request has nothing outside the allowlist, and then nothing here should run, show
    a spinner or cost a call.
    """
    if any(not allowlist.allows_package(p, base_version) for p in spec.packages):
        return True
    return any(claim.kind != "service" for claim in interpretation.unsupported or [])


def gather(
    spec: Spec,
    interpretation: Interpretation,
    allowlist: Allowlist,
    base_version: str,
    *,
    lookup: Lookup,
    suggest: Suggest | None,
) -> Offer:
    proposals: list[Proposal] = []
    refusals: list[Refusal] = []
    for name, word in candidates(spec, interpretation, allowlist, base_version, suggest):
        verdict = check_package(name, base_version, allowlist, lookup=lookup, requested_as=word)
        if isinstance(verdict, Proposal):
            proposals.append(verdict)
        elif verdict.code in SURPRISING:
            refusals.append(verdict)
    return Offer(tuple(proposals[:MAX_PROPOSALS]), tuple(refusals))
