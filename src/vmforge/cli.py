"""Command line entry point.

Commands are added phase by phase. Nothing here executes model output: the CLI
takes a spec, and the spec must pass the policy layer before anything else happens.

Exit codes:
    0  accepted
    1  rejected by policy
    2  invalid spec (schema) or unreadable input
"""

from __future__ import annotations

from pathlib import Path

import typer
import yaml
from pydantic import ValidationError

from vmforge import __version__
from vmforge.builder.build import BuildRejected, build_image
from vmforge.eval.dataset import load_cases
from vmforge.eval.runner import replay, run_eval
from vmforge.nl.client import InterpretError, OpenAiCompatible, config_from_env
from vmforge.policy.evaluate import ENV_VAR, Verdict, allowlist_digest, load_allowlist
from vmforge.policy.evaluate import evaluate as evaluate_policy
from vmforge.push.openstack import PushError, upload_image
from vmforge.spec.canonical import spec_hash, to_canonical_dict
from vmforge.spec.models import Spec
from vmforge.verify.verify import VerificationError, verify_build

EXIT_OK = 0
EXIT_REJECTED = 1
EXIT_INVALID = 2

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Build reproducible cloud-ready images from a declarative spec.",
)


@app.callback()
def main() -> None:
    """vmforge command line."""


@app.command()
def version() -> None:
    """Print the vmforge version."""
    typer.echo(f"vmforge {__version__}")


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


def _summarise(spec: Spec) -> None:
    typer.echo(f"  base      {spec.base.os} {spec.base.version}, disk {spec.disk_size}")
    typer.echo(
        f"  content   {len(spec.packages)} package(s), "
        f"{len(spec.services.enable)} service(s) enabled, "
        f"{len(spec.users)} user(s), {len(spec.files)} file(s)"
    )


def _report(verdict: Verdict) -> None:
    if verdict.passed:
        typer.echo("  verdict   PASS")
        return
    typer.echo(f"  verdict   FAIL ({len(verdict.violations)} violation(s))")
    for violation in verdict.violations:
        typer.echo(f"    {violation.location}: [{violation.code}] {violation.message}")


@app.command()
def check(spec: Path = typer.Argument(..., help="Path to a spec YAML file")) -> None:
    """Validate a spec against the schema and the policy allowlist."""
    parsed = _load_spec(spec)
    typer.echo(f"spec: {spec}")
    typer.echo(f"  hash      {spec_hash(parsed)}")
    _summarise(parsed)
    verdict = evaluate_policy(parsed, load_allowlist())
    _report(verdict)
    raise typer.Exit(EXIT_OK if verdict.passed else EXIT_REJECTED)


@app.command()
def plan(spec: Path = typer.Argument(..., help="Path to a spec YAML file")) -> None:
    """Show the normalised spec, its hash and the policy verdict."""
    parsed = _load_spec(spec)
    verdict = evaluate_policy(parsed, load_allowlist())
    rendered = yaml.safe_dump(to_canonical_dict(parsed), sort_keys=True, allow_unicode=True)
    typer.echo(rendered, nl=False)
    typer.echo(f"hash: {spec_hash(parsed)}")
    _report(verdict)
    raise typer.Exit(EXIT_OK if verdict.passed else EXIT_REJECTED)


@app.command()
def build(
    spec: Path = typer.Argument(..., help="Path to a spec YAML file"),
    work_root: Path = typer.Option(
        None, "--work-root", help="Where builds are written (default: ./builds)"
    ),
) -> None:
    """Reject, or build the image and write its manifest."""
    parsed = _load_spec(spec)
    typer.echo(f"spec: {spec}")
    typer.echo(f"  hash      {spec_hash(parsed)}")
    _summarise(parsed)

    try:
        result = build_image(parsed, work_root=work_root)
    except BuildRejected as rejected:
        _report(rejected.verdict)
        typer.echo("nothing was downloaded or built", err=True)
        raise typer.Exit(EXIT_REJECTED) from None

    typer.echo(f"  build     {result.build_dir}")
    typer.echo(f"  image     {result.image} ({result.image.stat().st_size} bytes)")
    typer.echo(f"  manifest  {result.manifest}")


@app.command()
def verify(
    build: Path = typer.Argument(..., help="Build directory or manifest.json to verify"),
) -> None:
    """Boot the built image and prove it satisfies its manifest."""
    try:
        result = verify_build(build)
    except VerificationError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(EXIT_INVALID) from None

    for check in result.checks:
        status = "PASS" if check.passed else "FAIL"
        typer.echo(f"  {status}  {check.name:<42} {check.detail}")
    raise typer.Exit(EXIT_OK if result.passed else EXIT_REJECTED)


@app.command("eval")
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


@app.command()
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


@app.command()
def push(
    build: Path = typer.Argument(..., help="Build directory or manifest.json"),
    cloud: str = typer.Option(None, "--cloud", help="Cloud name from clouds.yaml"),
    disk_format: str = typer.Option("qcow2", "--disk-format", help="qcow2 or raw"),
    name: str = typer.Option(None, "--name", help="Glance image name"),
) -> None:
    """Upload the built image to OpenStack Glance."""
    manifest_path = build / "manifest.json" if build.is_dir() else build
    try:
        result = upload_image(manifest_path, cloud=cloud, disk_format=disk_format, name=name)
    except PushError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(EXIT_INVALID) from None
    typer.echo(f"  uploaded  {result.name} ({result.disk_format}) id={result.image_id}")
    for key, value in sorted(result.properties.items()):
        typer.echo(f"    {key}={value}")


if __name__ == "__main__":
    app()
