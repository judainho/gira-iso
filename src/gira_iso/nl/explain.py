"""Check what the model says about its own spec before showing it to anyone.

The model reports how it translated the request ("docker" -> docker.io) and what it
could not provide. Both are claims, and a claim that contradicts the spec or the
policy would mislead the person reading it. Only claims that survive are kept.
"""

from __future__ import annotations

from dataclasses import dataclass

from gira_iso.nl.schema import Interpretation, Understood, Unsupported
from gira_iso.policy.evaluate import Allowlist
from gira_iso.spec.models import Spec


@dataclass(frozen=True)
class Explanation:
    understood: tuple[Understood, ...] = ()
    unsupported: tuple[Unsupported, ...] = ()
    discarded: int = 0  # claims dropped because the spec or the policy contradicts them


def explain(interpretation: Interpretation, spec: Spec, allowlist: Allowlist) -> Explanation:
    in_spec = {
        "package": set(spec.packages),
        "service": set(spec.services.enable),
    }
    allowed = allowlist.packages | allowlist.services

    understood = []
    discarded = 0
    for claim in interpretation.understood or []:
        if claim.resolved in in_spec[claim.kind]:
            understood.append(claim)
        else:
            discarded += 1

    included = in_spec["package"] | in_spec["service"]
    unsupported = []
    for claim in interpretation.unsupported or []:
        if claim.item in included:
            discarded += 1  # it says "cannot provide X" while X is in the spec
            continue
        # Alternatives are advice: keep only names the policy would really accept.
        real = [name for name in claim.alternatives if name in allowed]
        unsupported.append(claim.model_copy(update={"alternatives": real}))

    return Explanation(tuple(understood), tuple(unsupported), discarded)
