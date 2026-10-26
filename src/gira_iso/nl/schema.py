"""The three shapes the interpreter may return.

A closed union, not free text: either a spec that validates, a list of questions,
or a refusal. Anything else is a schema violation and gets retried once.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from gira_iso.spec.models import Spec


class Interpretation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["spec", "questions", "reject"]
    spec: Spec | None = None
    questions: list[str] | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def _exactly_one_payload(self) -> Interpretation:
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
