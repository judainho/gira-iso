"""Command line entry point.

Commands are added phase by phase. Nothing here executes model output: the CLI
takes a spec, and the spec must pass the policy layer before anything else happens.

Exit codes:
    0  accepted
    1  rejected by policy
    2  invalid spec (schema) or unreadable input
"""

from __future__ import annotations

import functools
import json
import os
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from typing import Any

import typer
import yaml
from pydantic import ValidationError

from gira_iso import __version__
from gira_iso.bases import builds_dir
from gira_iso.builder.catalog import display_name, resolve_image, scan_builds
from gira_iso.builder.export import delete_build, describe, export_image
from gira_iso.builder.manifest import Manifest
from gira_iso.errors import (
    EXIT_INTERRUPTED,
    EXIT_INVALID,
    EXIT_OK,
    EXIT_REJECTED,
    GiraError,
)
from gira_iso.eval.dataset import load_cases
from gira_iso.eval.runner import replay, run_eval
from gira_iso.naming import checked_name
from gira_iso.nl.client import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    InterpretError,
    LlmConfig,
    OpenAiCompatible,
    config_from_env,
)
from gira_iso.nl.explain import Explanation
from gira_iso.policy.evaluate import ENV_VAR, Verdict, allowlist_digest, load_allowlist
from gira_iso.policy.evaluate import evaluate as evaluate_policy
from gira_iso.push.openstack import upload_image
from gira_iso.spec.canonical import spec_hash, to_canonical_dict
from gira_iso.spec.models import Spec
from gira_iso.verify.verify import VerifyResult, verify_build
from gira_iso.workflow.create import CreateOutcome, CreateRequest, Status, run_create
from gira_iso.workflow.events import Event, LogLine, Step
from gira_iso.workflow.ports import Decision, Review

WORK_ROOT_HELP = "Where builds are written (default: <gira-iso home>/builds)"

app = typer.Typer(
    add_completion=False,
    help="Build reproducible cloud-ready images from a declarative spec.",
    epilog=(
        "Advanced commands (hidden from this list): check, plan, build, verify, eval, policy, push."
    ),
)


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    """With no command, open the interactive menu."""
    if ctx.invoked_subcommand is not None:
        return
    from gira_iso.tui import run

    run()


@app.command()
def version() -> None:
    """Print the gira-iso version."""
    typer.echo(f"gira-iso {__version__}")


def _load_spec(path: Path) -> Spec:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        typer.echo(f"error: no such file: {path}", err=True)
        raise typer.Exit(EXIT_INVALID) from None
    except yaml.YAMLError as exc:
        typer.echo(f"error: invalid YAML in {path}: {exc}", err=True)
        raise typer.Exit(EXIT_INVALID) from None

    if not isinstance(raw, dict):
        typer.echo("error: a spec must be a YAML mapping", err=True)
        raise typer.Exit(EXIT_INVALID)

    try:
        return Spec.model_validate(raw)
    except ValidationError as exc:
        typer.echo(f"error: spec does not match the schema ({path}):", err=True)
        for error in exc.errors():
            location = ".".join(str(part) for part in error["loc"]) or "<root>"
            typer.echo(f"  {location}: {error['msg']}", err=True)
        raise typer.Exit(EXIT_INVALID) from None


def _summarise(spec: Spec, err: bool = False) -> None:
    typer.echo(f"  base      {spec.base.os} {spec.base.version}, disk {spec.disk_size}", err=err)
    typer.echo(
        f"  content   {len(spec.packages)} package(s), "
        f"{len(spec.services.enable)} service(s) enabled, "
        f"{len(spec.users)} user(s), {len(spec.files)} file(s)",
        err=err,
    )


def _report(verdict: Verdict, err: bool = False) -> None:
    if verdict.passed:
        typer.echo("  verdict   PASS", err=err)
        return
    typer.echo(f"  verdict   FAIL ({len(verdict.violations)} violation(s))", err=err)
    for violation in verdict.violations:
        typer.echo(f"    {violation.location}: [{violation.code}] {violation.message}", err=err)


def _guarded(func: Callable[..., Any]) -> Callable[..., Any]:
    """Turn any expected failure into a one-line error and its exit code, never a traceback."""

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except typer.Exit:
            raise
        except GiraError as exc:
            typer.echo(f"error: {exc.user_message}", err=True)
            raise typer.Exit(exc.exit_code) from None
        except KeyboardInterrupt:
            typer.echo("interrupted", err=True)
            raise typer.Exit(EXIT_INTERRUPTED) from None

    return wrapper


def _print_checks(result: VerifyResult, err: bool = False) -> None:
    for check in result.checks:
        status = "PASS" if check.passed else "FAIL"
        typer.echo(f"  {status}  {check.name:<42} {check.detail}", err=err)


@app.command("list")
@_guarded
def list_images(
    work_root: Path = typer.Option(
        None,
        "--work-root",
        help=WORK_ROOT_HELP,
    ),
    json_output: bool = typer.Option(False, "--json", help="Print the list as JSON"),
) -> None:
    """List the images that have been created."""
    root = work_root or builds_dir()
    builds = scan_builds(root)
    if json_output:
        typer.echo(
            json.dumps(
                [
                    {
                        "name": display_name(manifest, path.parent),
                        "created_at": manifest.created_at.isoformat(),
                        "base": f"{manifest.base.os} {manifest.base.version}",
                        "virtual_size_bytes": manifest.artifact.virtual_size_bytes,
                        "sha256": manifest.artifact.sha256,
                        "build": path.parent.name,
                        "manifest": str(path),
                    }
                    for manifest, path in builds
                ],
                indent=2,
            )
        )
        return
    if not builds:
        typer.echo(f"no images found in {root}")
        return

    typer.echo(f"{'NAME':<24}{'CREATED':<22}{'SIZE':<8}BUILD")
    for manifest, manifest_path in builds:
        build_dir = manifest_path.parent
        size = f"{manifest.artifact.virtual_size_bytes / 1024**3:.1f}G"
        created = manifest.created_at.strftime("%Y-%m-%d %H:%MZ")
        typer.echo(f"{display_name(manifest, build_dir):<24}{created:<22}{size:<8}{build_dir.name}")


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
        _summarise(review.spec, err=self._err)
        if review.from_model and self._preview:
            self.say(yaml.safe_dump(to_canonical_dict(review.spec), sort_keys=False))
            if not typer.confirm("build this image?", default=True, err=self._err):
                return Decision(proceed=False)
        return Decision(proceed=True, name=review.name, verify=review.verify)

    def destination(self, name: str) -> Path | None:
        return None


def _conclude(outcome: CreateOutcome, *, err: bool, json_output: bool) -> None:
    """Turn the workflow's outcome into output and an exit code."""

    def say(message: str) -> None:
        typer.echo(message, err=err)

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
            _report(outcome.verdict, err=True)
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
        _print_checks(outcome.verification, err=err)

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
@_guarded
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
        parsed = _load_spec(Path(source))
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


@app.command()
@_guarded
def download(
    image: str = typer.Argument(
        ..., help="Build directory, manifest, image name or build id prefix"
    ),
    to: Path = typer.Option(Path("."), "--to", help="Destination directory"),
    work_root: Path = typer.Option(
        None,
        "--work-root",
        help=WORK_ROOT_HELP,
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing file"),
) -> None:
    """Copy a built image and its manifest to another directory."""
    manifest, manifest_path = resolve_image(image, work_root or builds_dir())
    build_dir = manifest_path.parent
    label = checked_name(display_name(manifest, build_dir))
    destination, manifest_destination = export_image(
        manifest, manifest_path, to.expanduser(), label, force=force
    )

    typer.echo(f"  image     {destination}")
    typer.echo(f"  manifest  {manifest_destination}")
    typer.echo(f"  sha256    {manifest.artifact.sha256}")


@app.command()
@_guarded
def show(
    image: str = typer.Argument(..., help="Image name, build id prefix, directory or manifest"),
    work_root: Path = typer.Option(None, "--work-root", help=WORK_ROOT_HELP),
    json_output: bool = typer.Option(False, "--json", help="Print the manifest as JSON"),
) -> None:
    """Show the provenance recorded for one image."""
    manifest, manifest_path = resolve_image(image, work_root or builds_dir())
    if json_output:
        payload = json.loads(manifest.model_dump_json())
        payload["build_dir"] = str(manifest_path.parent)
        typer.echo(json.dumps(payload, indent=2))
        return
    for label, value in describe(manifest, manifest_path.parent):
        typer.echo(f"  {label:<14}{value}")


@app.command()
@_guarded
def delete(
    image: str = typer.Argument(..., help="Image name, build id prefix, directory or manifest"),
    work_root: Path = typer.Option(
        None,
        "--work-root",
        help=WORK_ROOT_HELP,
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask for confirmation"),
) -> None:
    """Delete one image and its build directory."""
    root = work_root or builds_dir()
    manifest, manifest_path = resolve_image(image, root)
    label = display_name(manifest, manifest_path.parent)
    if not yes and not typer.confirm(f"delete {label} ({manifest_path.parent.name})?"):
        raise typer.Exit(EXIT_OK)
    removed = delete_build(manifest_path, root)
    typer.echo(f"  deleted   {removed}")


@app.command(hidden=True)
@_guarded
def check(spec: Path = typer.Argument(..., help="Path to a spec YAML file")) -> None:
    """Validate a spec against the schema and the policy allowlist."""
    parsed = _load_spec(spec)
    typer.echo(f"spec: {spec}")
    typer.echo(f"  hash      {spec_hash(parsed)}")
    _summarise(parsed)
    verdict = evaluate_policy(parsed, load_allowlist())
    _report(verdict)
    raise typer.Exit(EXIT_OK if verdict.passed else EXIT_REJECTED)


@app.command(hidden=True)
@_guarded
def plan(spec: Path = typer.Argument(..., help="Path to a spec YAML file")) -> None:
    """Show the normalised spec, its hash and the policy verdict."""
    parsed = _load_spec(spec)
    verdict = evaluate_policy(parsed, load_allowlist())
    rendered = yaml.safe_dump(to_canonical_dict(parsed), sort_keys=True, allow_unicode=True)
    typer.echo(rendered, nl=False)
    typer.echo(f"hash: {spec_hash(parsed)}")
    _report(verdict)
    raise typer.Exit(EXIT_OK if verdict.passed else EXIT_REJECTED)


@app.command(hidden=True)
@_guarded
def build(
    spec: Path = typer.Argument(..., help="Path to a spec YAML file"),
    work_root: Path = typer.Option(
        None,
        "--work-root",
        help=WORK_ROOT_HELP,
    ),
) -> None:
    """Reject, or build the image and write its manifest."""
    parsed = _load_spec(spec)
    typer.echo(f"spec: {spec}")
    typer.echo(f"  hash      {spec_hash(parsed)}")
    _summarise(parsed)

    request = CreateRequest(spec=parsed, work_root=work_root, review=False)
    outcome = run_create(request, _CliIo(interactive=False, err=False, preview=False))
    _conclude(outcome, err=False, json_output=False)


@app.command(hidden=True)
@_guarded
def verify(
    build: Path = typer.Argument(..., help="Build directory or manifest.json to verify"),
) -> None:
    """Boot the built image and prove it satisfies its manifest."""
    result = verify_build(build)
    _print_checks(result)
    raise typer.Exit(EXIT_OK if result.passed else EXIT_REJECTED)


@app.command("eval", hidden=True)
@_guarded
def evaluate(
    category: str = typer.Option(None, "--category", help="Only run one category"),
    limit: int = typer.Option(0, "--limit", help="Run at most N cases (0 = all)"),
    replay_path: Path = typer.Option(
        None, "--replay", help="Re-score a recorded run instead of calling a model"
    ),
    record_path: Path = typer.Option(
        None, "--record", help="Write the run (raw responses included) to this file"
    ),
) -> None:
    """Run the golden dataset and print the metrics."""
    cases = load_cases()
    if category:
        cases = [case for case in cases if case.category == category]
    if limit:
        cases = cases[:limit]
    if not cases:
        typer.echo("no cases selected", err=True)
        raise typer.Exit(EXIT_INVALID)

    allowlist = load_allowlist()
    try:
        if replay_path is not None:
            report = replay(replay_path, cases=cases, allowlist=allowlist)
        else:
            config = config_from_env()
            report = run_eval(
                cases,
                completer=OpenAiCompatible(config),
                model=config.model,
                allowlist=allowlist,
                record_path=record_path,
            )
    except InterpretError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(EXIT_INVALID) from None

    typer.echo(f"model: {report.model}  cases: {len(report.results)}")
    for result in report.results:
        mark = "PASS" if result.passed else "FAIL"
        typer.echo(f"  {mark}  {result.case_id:<16} {result.actual_kind:<10} {result.detail}")
    typer.echo("")
    for key, value in report.metrics.items():
        if key == "by_category":
            for name, bucket in value.items():
                typer.echo(f"  {name:<16} {bucket['passed']}/{bucket['total']}")
            continue
        typer.echo(f"  {key:<16} {value}")
    raise typer.Exit(EXIT_OK if report.passed else EXIT_REJECTED)


@app.command(hidden=True)
@_guarded
def policy() -> None:
    """Show which allowlist is active and what it permits."""
    source, digest = allowlist_digest()
    active = load_allowlist()
    images = [
        f"{name}:{version}" for name, versions in active.base_images.items() for version in versions
    ]
    typer.echo(f"  source    {source}")
    typer.echo(f"  sha256    {digest}")
    typer.echo(f"  override  set {ENV_VAR}=/path/to/allowlist.yaml")
    typer.echo(f"  packages  {len(active.packages)}")
    typer.echo(f"  services  {len(active.services)}")
    typer.echo(f"  paths     {len(active.file_prefixes)} prefix(es)")
    typer.echo(f"  base      {', '.join(images)}")


@app.command(hidden=True)
@_guarded
def push(
    build: Path = typer.Argument(..., help="Build directory or manifest.json"),
    cloud: str = typer.Option(None, "--cloud", help="Cloud name from clouds.yaml"),
    disk_format: str = typer.Option("qcow2", "--disk-format", help="qcow2 or raw"),
    name: str = typer.Option(None, "--name", help="Glance image name"),
) -> None:
    """Upload the built image to OpenStack Glance."""
    manifest_path = build / "manifest.json" if build.is_dir() else build
    result = upload_image(manifest_path, cloud=cloud, disk_format=disk_format, name=name)
    typer.echo(f"  uploaded  {result.name} ({result.disk_format}) id={result.image_id}")
    for key, value in sorted(result.properties.items()):
        typer.echo(f"    {key}={value}")


if __name__ == "__main__":
    app()
