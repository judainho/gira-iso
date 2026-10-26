"""What the workflow tells the front end while it runs. Typed, so a front end can match on them."""

from __future__ import annotations

from dataclasses import dataclass

from gira_iso.builder.build import BuildResult
from gira_iso.verify.verify import VerifyResult


@dataclass(frozen=True)
class Step:
    """A phase started: validating, preparing the base, rendering, building, ..."""

    message: str


@dataclass(frozen=True)
class Download:
    """Progress of the base image download; `total` is 0 when the size is unknown."""

    done: int
    total: int


@dataclass(frozen=True)
class LogLine:
    """One line of Packer output."""

    text: str


@dataclass(frozen=True)
class Built:
    """The image exists and its manifest is written."""

    result: BuildResult


@dataclass(frozen=True)
class Verified:
    """The verification finished; `result.passed` says whether the image kept its promises."""

    result: VerifyResult


Event = Step | Download | LogLine | Built | Verified
