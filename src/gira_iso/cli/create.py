"""`create` and the hidden `build`: the command-line face of the create workflow."""

from __future__ import annotations

import json
import os
from contextlib import AbstractContextManager, nullcontext
from dataclasses import asdict
from pathlib import Path

import typer
import yaml

from gira_iso.builder.manifest import Manifest
from gira_iso.cli._app import WORK_ROOT_HELP, app
from gira_iso.cli._shared import guarded, load_spec, print_checks, report, summarise
from gira_iso.cli.allow import confirm_proposal
from gira_iso.errors import (
    EXIT_INVALID,
    EXIT_OK,
    EXIT_REJECTED,
)
from gira_iso.naming import checked_name
from gira_iso.nl.client import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    LlmConfig,
    OpenAiCompatible,
)
from gira_iso.nl.explain import Explanation
from gira_iso.proposals import Proposal, Refusal
from gira_iso.spec.canonical import spec_hash, to_canonical_dict
from gira_iso.workflow.create import CreateOutcome, CreateRequest, Status, run_create
from gira_iso.workflow.events import Event, LogLine, Step
from gira_iso.workflow.ports import Decision, Review


def _is_spec_file(value: str) -> bool:
    return value.endswith((".yaml", ".yml")) or Path(value).is_file()


def _llm_config(model: str | None, interactive: bool) -> LlmConfig:
    """Environment first; otherwise ask for the key (never stored), unless nobody can answer."""
    base_url = os.environ.get("GIRA_ISO_LLM_BASE_URL", DEFAULT_BASE_URL)
    chosen_model = model or os.environ.get("GIRA_ISO_LLM_MODEL", DEFAULT_MODEL)
    api_key = os.environ.get("GIRA_ISO_LLM_API_KEY")
    if not api_key:
        if not interactive:
            typer.echo("error: --non-interactive needs the key in GIRA_ISO_LLM_API_KEY", err=True)
            raise typer.Exit(EXIT_INVALID)
        typer.echo(f"endpoint: {base_url}  model: {chosen_model}", err=True)
        api_key = typer.prompt("API key", hide_input=True).strip()
    if not api_key:
        typer.echo("error: an API key is required to interpret a request", err=True)
        raise typer.Exit(EXIT_INVALID)
    return LlmConfig(base_url=base_url, api_key=api_key, model=chosen_model)


class _CliIo:
    """Presents the workflow on the command line. It decides nothing about the order."""

    def __init__(self, *, interactive: bool, err: bool, preview: bool) -> None:
        self.interactive = interactive
        self._err = err
        self._preview = preview  # show the spec and ask before building

    def say(self, message: str) -> None:
        typer.echo(message, err=self._err)

    def emit(self, event: Event) -> None:
        if isinstance(event, Step):
            self.say(f"> {event.message}")
        elif isinstance(event, LogLine):
            typer.echo(event.text, err=True)

    def answer(self, questions: list[str]) -> list[str]:
        typer.echo("the model needs more detail:", err=True)
        return [typer.prompt(f"  {question}") for question in questions]

    def busy(self, message: str) -> AbstractContextManager[object]:
        return nullcontext()

    def review(self, review: Review) -> Decision:
        if review.from_model:
            self.say("interpreted request as:")
            _print_explanation(review.explanation)
        self.say(f"  name      {review.name}")
        self.say(f"  hash      {spec_hash(review.spec)}")
        summarise(review.spec, err=self._err)
        if review.from_model and self._preview:
            self.say(yaml.safe_dump(to_canonical_dict(review.spec), sort_keys=False))
            if not typer.confirm("build this image?", default=True, err=self._err):
                return Decision(proceed=False)
        return Decision(proceed=True, name=review.name, verify=review.verify)

    def propose(self, proposals: list[Proposal], refusals: list[Refusal]) -> list[str]:
        """Ask one package at a time. `--yes` never answers this; it only skips the build prompt."""
        for refusal in refusals:
            self.say(f"  refused   {refusal.package}: {refusal.reason}")
        if proposals:
            self.say("the request needs packages that are not on the allowlist:")
        return [p.package for p in proposals if confirm_proposal(p, yes=False, err=self._err)]

    def destination(self, name: str) -> Path | None:
        return None


def _conclude(outcome: CreateOutcome, *, err: bool, json_output: bool) -> None:
    """Turn the workflow's outcome into output and an exit code."""

    def say(message: str) -> None:
        typer.echo(message, err=err)

    for proposal in outcome.addable:  # nobody could be asked: say what would unblock the request
        typer.echo(
            f"  addable   {proposal.package} ({proposal.component} {proposal.version}): "
            f"gira-iso allow add {proposal.package} --base {proposal.base_version}",
            err=True,
        )
    if outcome.status is Status.MODEL_REJECTED:
        typer.echo(f"rejected: {outcome.reason}", err=True)
        raise typer.Exit(EXIT_REJECTED)
    if outcome.status is Status.NEEDS_DETAIL:
        typer.echo("the request needs more detail (--non-interactive cannot ask):", err=True)
        for question in outcome.questions:
            typer.echo(f"  - {question}", err=True)
        raise typer.Exit(EXIT_REJECTED)
    if outcome.status is Status.INTERPRET_ERROR:
        typer.echo(f"error: {outcome.error}", err=True)
        raise typer.Exit(EXIT_INVALID)
    if outcome.status is Status.POLICY_REJECTED:
        if outcome.verdict is not None:
            report(outcome.verdict, err=True)
            typer.echo("nothing was downloaded or built", err=True)
        else:
            _print_explanation(outcome.explanation)
            typer.echo("the policy rejects the interpreted spec:", err=True)
            for line in outcome.violations:
                typer.echo(f"  {line}", err=True)
        raise typer.Exit(EXIT_REJECTED)
    if outcome.status is Status.DECLINED:
        raise typer.Exit(EXIT_OK)

    build = outcome.build
    assert build is not None and outcome.spec is not None
    say(f"  build     {build.build_dir}")
    say(f"  image     {build.image} ({build.image.stat().st_size} bytes)")
    say(f"  manifest  {build.manifest}")

    if outcome.verification is not None:
        say("verify:")
        print_checks(outcome.verification, err=err)

    if json_output:
        manifest = Manifest.model_validate_json(build.manifest.read_text(encoding="utf-8"))
        payload = {
            "name": outcome.name,
            "image": str(build.image),
            "manifest": str(build.manifest),
            "build_dir": str(build.build_dir),
            "sha256": manifest.artifact.sha256,
            "base": f"{manifest.base.os} {manifest.base.version}",
            "spec_hash": spec_hash(outcome.spec),
            "verified": outcome.verified,
            "understood": [claim.model_dump() for claim in outcome.explanation.understood],
            "unsupported": [claim.model_dump() for claim in outcome.explanation.unsupported],
            "added": list(outcome.added),
            "addable": [asdict(proposal) for proposal in outcome.addable],
        }
        typer.echo(json.dumps(payload, indent=2))
    if outcome.verified is False:
        raise typer.Exit(EXIT_REJECTED)


def _print_explanation(explanation: Explanation) -> None:
    for claim in explanation.understood:
        typer.echo(f"  understood  {claim.requested} -> {claim.resolved} ({claim.kind})", err=True)
    for claim in explanation.unsupported:
        alternatives = (
            f"; allowed alternatives: {', '.join(claim.alternatives)}"
            if (claim.alternatives)
            else ""
        )
        typer.echo(
            f"  skipped     {claim.item} ({claim.kind}): {claim.reason}{alternatives}", err=True
        )


@app.command()
@guarded
def create(
    source: str = typer.Argument(
        ..., help="A spec YAML file, or a request in natural language (quote it)"
    ),
    name: str = typer.Option(None, "--name", help="Image name (default: the spec file name)"),
    work_root: Path = typer.Option(
        None,
        "--work-root",
        help=WORK_ROOT_HELP,
    ),
    verify: bool = typer.Option(
        False, "--verify", help="Boot the image and verify it after building"
    ),
    model: str = typer.Option(None, "--model", help="Model for natural-language requests"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation before building"),
    json_output: bool = typer.Option(
        False, "--json", help="Print one JSON object on stdout; all progress goes to stderr"
    ),
    non_interactive: bool = typer.Option(
        False,
        "--non-interactive",
        help="Never prompt: the key comes from GIRA_ISO_LLM_API_KEY and --yes is implied",
    ),
) -> None:
    """Create an image from a spec file or a natural-language request."""
    # With --json, stdout is reserved for the result so a script can pipe it.
    err = json_output
    interactive = not non_interactive

    if _is_spec_file(source):
        parsed = load_spec(Path(source))
        chosen = checked_name(name or Path(source).stem)
        typer.echo(f"spec: {source}", err=err)
        request = CreateRequest(
            spec=parsed, name=chosen, verify=verify, work_root=work_root, review=True
        )
    else:
        if name is None:
            typer.echo("error: --name is required with a natural-language request", err=True)
            raise typer.Exit(EXIT_INVALID)
        request = CreateRequest(
            text=source,
            completer=OpenAiCompatible(_llm_config(model, interactive)),
            name=checked_name(name),
            verify=verify,
            work_root=work_root,
        )

    io = _CliIo(interactive=interactive, err=err, preview=not yes and interactive)
    _conclude(run_create(request, io), err=err, json_output=json_output)


@app.command(hidden=True)
@guarded
def build(
    spec: Path = typer.Argument(..., help="Path to a spec YAML file"),
    work_root: Path = typer.Option(
        None,
        "--work-root",
        help=WORK_ROOT_HELP,
    ),
) -> None:
    """Reject, or build the image and write its manifest."""
    parsed = load_spec(spec)
    typer.echo(f"spec: {spec}")
    typer.echo(f"  hash      {spec_hash(parsed)}")
    summarise(parsed)

    request = CreateRequest(spec=parsed, work_root=work_root, review=False)
    outcome = run_create(request, _CliIo(interactive=False, err=False, preview=False))
    _conclude(outcome, err=False, json_output=False)
