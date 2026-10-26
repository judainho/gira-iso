"""The three shapes the interpreter may return.

A closed union, not free text: either a spec that validates, a list of questions,
or a refusal. Anything else is a schema violation and gets retried once.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from gira_iso.spec.models import Spec

Short = Annotated[str, StringConstraints(min_length=1, max_length=200)]


class Understood(BaseModel):
    """How a word of the request was translated into a name in the spec."""

    model_config = ConfigDict(extra="forbid")

    requested: Short
    resolved: Short
    kind: Literal["package", "service"]


class Unsupported(BaseModel):
    """Something the user asked for that the spec cannot express, and why."""

    model_config = ConfigDict(extra="forbid")

    item: Short
    kind: Literal["program", "package", "service", "other"] = "other"
    reason: Short
    alternatives: Annotated[list[Short], Field(max_length=5)] = Field(default_factory=list)


class Interpretation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["spec", "questions", "reject"]
    spec: Spec | None = None
    questions: list[str] | None = None
    reason: str | None = None
    understood: Annotated[list[Understood], Field(max_length=50)] | None = None
    unsupported: Annotated[list[Unsupported], Field(max_length=50)] | None = None

    @model_validator(mode="after")
    def _exactly_one_payload(self) -> Interpretation:
        if self.kind != "spec" and (self.understood is not None or self.unsupported is not None):
            raise ValueError(f"kind {self.kind!r} must not carry understood or unsupported")
        if self.kind == "spec":
            if self.spec is None:
                raise ValueError("kind 'spec' requires a spec")
            if self.questions is not None or self.reason is not None:
                raise ValueError("kind 'spec' must not carry questions or reason")
        elif self.kind == "questions":
            if not self.questions:
                raise ValueError("kind 'questions' requires at least one question")
            if self.spec is not None or self.reason is not None:
                raise ValueError("kind 'questions' must not carry a spec or reason")
        else:
            if not self.reason:
                raise ValueError("kind 'reject' requires a reason")
            if self.spec is not None or self.questions is not None:
                raise ValueError("kind 'reject' must not carry a spec or questions")
        return self
