"""Advanced commands, hidden from the main help: check, plan, verify, eval, policy, push."""

from __future__ import annotations

from pathlib import Path

import typer
import yaml

from gira_iso.cli._app import app
from gira_iso.cli._shared import guarded, load_spec, print_checks, report, summarise
from gira_iso.errors import (
    EXIT_INVALID,
    EXIT_OK,
    EXIT_REJECTED,
)
from gira_iso.eval.dataset import load_cases
from gira_iso.eval.runner import replay, run_eval
from gira_iso.nl.client import (
    InterpretError,
    OpenAiCompatible,
    config_from_env,
)
from gira_iso.policy.evaluate import ENV_VAR, allowlist_digest, load_allowlist
from gira_iso.policy.evaluate import evaluate as evaluate_policy
from gira_iso.push.openstack import upload_image
from gira_iso.spec.canonical import spec_hash, to_canonical_dict
from gira_iso.verify.verify import verify_build


@app.command(hidden=True)
@guarded
def check(spec: Path = typer.Argument(..., help="Path to a spec YAML file")) -> None:
    """Validate a spec against the schema and the policy allowlist."""
    parsed = load_spec(spec)
    typer.echo(f"spec: {spec}")
    typer.echo(f"  hash      {spec_hash(parsed)}")
    summarise(parsed)
    verdict = evaluate_policy(parsed, load_allowlist())
    report(verdict)
    raise typer.Exit(EXIT_OK if verdict.passed else EXIT_REJECTED)


@app.command(hidden=True)
@guarded
def plan(spec: Path = typer.Argument(..., help="Path to a spec YAML file")) -> None:
    """Show the normalised spec, its hash and the policy verdict."""
    parsed = load_spec(spec)
    verdict = evaluate_policy(parsed, load_allowlist())
    rendered = yaml.safe_dump(to_canonical_dict(parsed), sort_keys=True, allow_unicode=True)
    typer.echo(rendered, nl=False)
    typer.echo(f"hash: {spec_hash(parsed)}")
    report(verdict)
    raise typer.Exit(EXIT_OK if verdict.passed else EXIT_REJECTED)


@app.command(hidden=True)
@guarded
def verify(
    build: Path = typer.Argument(..., help="Build directory or manifest.json to verify"),
) -> None:
    """Boot the built image and prove it satisfies its manifest."""
    result = verify_build(build)
    print_checks(result)
    raise typer.Exit(EXIT_OK if result.passed else EXIT_REJECTED)


@app.command("eval", hidden=True)
@guarded
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
@guarded
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
@guarded
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
