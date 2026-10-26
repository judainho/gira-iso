"""Canonical form of a spec.

Two specs that mean the same thing must hash the same thing. Hashing feeds the
build manifest and the "same spec twice" reproducibility check, so the encoding is
fixed here and never depends on dict insertion order.

Note: key order inside `ssh_authorized_keys`, `packages` and `files` is meaningful
only in that duplicates are rejected; the canonical form keeps list order because a
reordering is a different spec as far as the build log is concerned.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from vmforge.spec.models import Spec


def to_canonical_dict(spec: Spec) -> dict[str, Any]:
    """Return the spec as plain JSON-compatible data with model defaults applied."""
    return spec.model_dump(mode="json")


def canonical_json(spec: Spec) -> str:
    return json.dumps(
        to_canonical_dict(spec),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def spec_hash(spec: Spec) -> str:
    """SHA-256 of the canonical encoding, hex encoded."""
    return hashlib.sha256(canonical_json(spec).encode("utf-8")).hexdigest()
