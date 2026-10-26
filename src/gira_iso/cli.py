"""Command line entry point.

Commands are added phase by phase. Nothing here executes model output: the CLI
takes a spec, and the spec must pass the policy layer before anything else happens.

Exit codes:
    0  accepted
    1  rejected by policy
    2  invalid spec (schema) or unreadable input
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import typer
import yaml
from pydantic import ValidationError

from gira_iso import __version__
from gira_iso.bases import builds_dir
from gira_iso.builder.build import BuildRejected, build_image
from gira_iso.builder.export import (
    ExportError,
    delete_build,
    describe,
    export_image,
    scan_builds,
)
from gira_iso.builder.manifest import MANIFEST_NAME, Manifest
from gira_iso.eval.dataset import load_cases
from gira_iso.eval.runner import replay, run_eval
from gira_iso.nl.client import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    InterpretError,
    LlmConfig,
    OpenAiCompatible,
    config_from_env,
    interpret,
)
from gira_iso.policy.evaluate import ENV_VAR, Verdict, allowlist_digest, load_allowlist
from gira_iso.policy.evaluate import evaluate as evaluate_policy
from gira_iso.push.openstack import PushError, upload_image
from gira_iso.spec.canonical import spec_hash, to_canonical_dict
from gira_iso.spec.models import Spec
from gira_iso.verify.verify import VerificationError, VerifyResult, verify_build

EXIT_OK = 0
EXIT_REJECTED = 1
EXIT_INVALID = 2

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


_IMAGE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _checked_name(value: str) -> str:
    if not _IMAGE_NAME.match(value):
        typer.echo(
            f"error: invalid image name {value!r}: use letters, digits, '.', '_' or '-'",
            err=True,
        )
        raise typer.Exit(EXIT_INVALID)
    return value


def _display_name(manifest: Manifest, build_dir: Path) -> str:
    return manifest.name or build_dir.name


def _load_manifest(path: Path) -> tuple[Manifest, Path]:
    manifest_path = path / MANIFEST_NAME if path.is_dir() else path
    if not manifest_path.is_file():
        typer.echo(f"error: no manifest at {manifest_path}", err=True)
        raise typer.Exit(EXIT_INVALID)
    try:
        manifest = Manifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
        return manifest, manifest_path
    except (ValidationError, OSError) as exc:
        typer.echo(f"error: manifest at {manifest_path} is not valid: {exc}", err=True)
        raise typer.Exit(EXIT_INVALID) from None


_scan_builds = scan_builds


def _resolve_image(reference: str, work_root: Path) -> tuple[Manifest, Path]:
    candidate = Path(reference).expanduser()
    if candidate.exists():
        return _load_manifest(candidate)

    matches: dict[Path, tuple[Manifest, Path]] = {}
    for manifest, manifest_path in _scan_builds(work_root):
        build_dir = manifest_path.parent
        name = _display_name(manifest, build_dir)
        if name == reference or build_dir.name.startswith(reference):
            matches[build_dir] = (manifest, manifest_path)

    if not matches:
        typer.echo(f"error: no image matches {reference!r} under {work_root}", err=True)
        raise typer.Exit(EXIT_INVALID)
    if len(matches) > 1:
        names = ", ".join(sorted(path.name for path in matches))
        typer.echo(f"error: {reference!r} is ambiguous: {names}", err=True)
        raise typer.Exit(EXIT_INVALID)
    return next(iter(matches.values()))


def _print_checks(result: VerifyResult) -> None:
    for check in result.checks:
        status = "PASS" if check.passed else "FAIL"
        typer.echo(f"  {status}  {check.name:<42} {check.detail}")


@app.command("list")
def list_images(
    work_root: Path = typer.Option(
        None,
        "--work-root",
        help=WORK_ROOT_HELP,
    ),
) -> None:
    """List the images that have been created."""
    root = work_root or builds_dir()
    builds = _scan_builds(root)
    if not builds:
        typer.echo(f"no images found in {root}")
        return

    typer.echo(f"{'NAME':<24}{'CREATED':<22}{'SIZE':<8}BUILD")
    for manifest, manifest_path in builds:
        build_dir = manifest_path.parent
        size = f"{manifest.artifact.virtual_size_bytes / 1024**3:.1f}G"
        created = manifest.created_at.strftime("%Y-%m-%d %H:%MZ")
        typer.echo(
            f"{_display_name(manifest, build_dir):<24}{created:<22}{size:<8}{build_dir.name}"
        )


def _is_spec_file(value: str) -> bool:
    return value.endswith((".yaml", ".yml")) or Path(value).is_file()


def _llm_config(model: str | None) -> LlmConfig:
    """Environment first; otherwise ask for the key, which is never stored."""
    base_url = os.environ.get("GIRA_ISO_LLM_BASE_URL", DEFAULT_BASE_URL)
    chosen_model = model or os.environ.get("GIRA_ISO_LLM_MODEL", DEFAULT_MODEL)
    api_key = os.environ.get("GIRA_ISO_LLM_API_KEY")
    if not api_key:
        typer.echo(f"endpoint: {base_url}  model: {chosen_model}")
        api_key = typer.prompt("API key", hide_input=True).strip()
    if not api_key:
        typer.echo("error: an API key is required to interpret a request", err=True)
        raise typer.Exit(EXIT_INVALID)
    return LlmConfig(base_url=base_url, api_key=api_key, model=chosen_model)


def _spec_from_request(request: str, model: str | None) -> Spec:
    """Ask the model; follow-up questions are answered at the prompt, up to three rounds."""
    completer = OpenAiCompatible(_llm_config(model))
    text = request
    for _ in range(3):
        try:
            inference = interpret(text, completer=completer)
        except InterpretError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(EXIT_INVALID) from None
        result = inference.interpretation
        if result.kind == "spec":
            assert result.spec is not None
            return result.spec
        if result.kind == "reject":
            typer.echo(f"rejected: {result.reason}", err=True)
            raise typer.Exit(EXIT_REJECTED)
        assert result.questions is not None
        typer.echo("the model needs more detail:")
        answers = [typer.prompt(f"  {question}") for question in result.questions]
        details = "\n".join(f"- {q} {a}" for q, a in zip(result.questions, answers, strict=True))
        text = f"{text}\n\nAdditional details:\n{details}"
    typer.echo("error: the request is still ambiguous after three rounds", err=True)
    raise typer.Exit(EXIT_INVALID)


@app.command()
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
) -> None:
    """Create an image from a spec file or a natural-language request."""
    from_file = _is_spec_file(source)
    if from_file:
        parsed = _load_spec(Path(source))
        chosen = _checked_name(name or Path(source).stem)
        typer.echo(f"spec: {source}")
    else:
        if name is None:
            typer.echo("error: --name is required with a natural-language request", err=True)
            raise typer.Exit(EXIT_INVALID)
        chosen = _checked_name(name)
        parsed = _spec_from_request(source, model)
        typer.echo("interpreted request as:")
    typer.echo(f"  name      {chosen}")
    typer.echo(f"  hash      {spec_hash(parsed)}")
    _summarise(parsed)

    if not from_file and not yes:
        typer.echo(yaml.safe_dump(to_canonical_dict(parsed), sort_keys=False))
        if not typer.confirm("build this image?", default=True):
            raise typer.Exit(EXIT_OK)

    try:
        result = build_image(parsed, work_root=work_root, name=chosen)
    except BuildRejected as rejected:
        _report(rejected.verdict)
        typer.echo("nothing was downloaded or built", err=True)
        raise typer.Exit(EXIT_REJECTED) from None

    typer.echo(f"  build     {result.build_dir}")
    typer.echo(f"  image     {result.image} ({result.image.stat().st_size} bytes)")
    typer.echo(f"  manifest  {result.manifest}")

    if not verify:
        return
    typer.echo("verify:")
    verification = verify_build(result.build_dir)
    _print_checks(verification)
    raise typer.Exit(EXIT_OK if verification.passed else EXIT_REJECTED)


@app.command()
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
    manifest, manifest_path = _resolve_image(image, work_root or builds_dir())
    build_dir = manifest_path.parent
    label = _checked_name(_display_name(manifest, build_dir))
    try:
        destination, manifest_destination = export_image(
            manifest, manifest_path, to.expanduser(), label, force=force
        )
    except ExportError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(EXIT_INVALID if exc.invalid else EXIT_REJECTED) from None

    typer.echo(f"  image     {destination}")
    typer.echo(f"  manifest  {manifest_destination}")
    typer.echo(f"  sha256    {manifest.artifact.sha256}")


@app.command()
def show(
    image: str = typer.Argument(..., help="Image name, build id prefix, directory or manifest"),
    work_root: Path = typer.Option(
        None,
        "--work-root",
        help=WORK_ROOT_HELP,
    ),
) -> None:
    """Show the provenance recorded for one image."""
    manifest, manifest_path = _resolve_image(image, work_root or builds_dir())
    for label, value in describe(manifest, manifest_path.parent):
        typer.echo(f"  {label:<14}{value}")


@app.command()
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
    manifest, manifest_path = _resolve_image(image, root)
    label = _display_name(manifest, manifest_path.parent)
    if not yes and not typer.confirm(f"delete {label} ({manifest_path.parent.name})?"):
        raise typer.Exit(EXIT_OK)
    try:
        removed = delete_build(manifest_path, root)
    except ExportError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(EXIT_INVALID) from None
    typer.echo(f"  deleted   {removed}")


@app.command(hidden=True)
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

    try:
        result = build_image(parsed, work_root=work_root)
    except BuildRejected as rejected:
        _report(rejected.verdict)
        typer.echo("nothing was downloaded or built", err=True)
        raise typer.Exit(EXIT_REJECTED) from None

    typer.echo(f"  build     {result.build_dir}")
    typer.echo(f"  image     {result.image} ({result.image.stat().st_size} bytes)")
    typer.echo(f"  manifest  {result.manifest}")


@app.command(hidden=True)
def verify(
    build: Path = typer.Argument(..., help="Build directory or manifest.json to verify"),
) -> None:
    """Boot the built image and prove it satisfies its manifest."""
    try:
        result = verify_build(build)
    except VerificationError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(EXIT_INVALID) from None

    _print_checks(result)
    raise typer.Exit(EXIT_OK if result.passed else EXIT_REJECTED)


@app.command("eval", hidden=True)
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
