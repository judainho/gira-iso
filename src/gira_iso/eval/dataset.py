"""The golden dataset.

Written before the client on purpose. A dataset authored after seeing model output
measures how well the model matches itself, not how well it interprets intent.

Each case declares what a correct answer looks like:

    spec          a spec that matches the expected fields
    questions     the request is incomplete and must be queried, not invented
    out_of_scope  the request cannot be expressed with the schema
    blocked       a spec may be produced, but the policy layer must reject it
"""

from __future__ import annotations

from importlib import resources
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

DATASET_FILE = "cases.yaml"

Category = Literal["simple", "composite", "ambiguous", "out_of_scope", "adversarial", "language"]


class Expectation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["spec", "questions", "out_of_scope", "blocked"]
    spec: dict | None = None
    note: str | None = None

    @model_validator(mode="after")
    def _spec_only_for_spec_cases(self) -> Expectation:
        if self.kind == "spec" and self.spec is None:
            raise ValueError("a 'spec' expectation must include the expected spec")
        if self.kind != "spec" and self.spec is not None:
            raise ValueError(f"a '{self.kind}' expectation must not include a spec")
        return self


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    category: Category
    language: Literal["es", "en"]
    pair: str | None = None
    prompt: Annotated[str, Field(min_length=8)]
    expect: Expectation


def load_cases() -> list[Case]:
    text = resources.files("gira_iso.eval").joinpath(DATASET_FILE).read_text("utf-8")
    raw = yaml.safe_load(text)
    if not isinstance(raw, list):
        raise ValueError("dataset must be a YAML list of cases")
    cases = [Case.model_validate(item) for item in raw]
    identifiers = [case.id for case in cases]
    duplicates = sorted({name for name in identifiers if identifiers.count(name) > 1})
    if duplicates:
        raise ValueError(f"duplicate case ids: {', '.join(duplicates)}")
    return cases
