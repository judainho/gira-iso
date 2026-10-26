"""Helpers shared by the command modules: spec loading, summaries and the error guard."""

from __future__ import annotations

import functools
from collections.abc import Callable
from pathlib import Path
from typing import Any

import typer
import yaml
from pydantic import ValidationError

from gira_iso.errors import (
    EXIT_INTERRUPTED,
    EXIT_INVALID,
    GiraError,
)
from gira_iso.policy.evaluate import Verdict
from gira_iso.spec.models import Spec
from gira_iso.verify.verify import VerifyResult


def load_spec(path: Path) -> Spec:
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


def summarise(spec: Spec, err: bool = False) -> None:
    typer.echo(f"  base      {spec.base.os} {spec.base.version}, disk {spec.disk_size}", err=err)
    typer.echo(
        f"  content   {len(spec.packages)} package(s), "
        f"{len(spec.services.enable)} service(s) enabled, "
        f"{len(spec.users)} user(s), {len(spec.files)} file(s)",
        err=err,
    )


def report(verdict: Verdict, err: bool = False) -> None:
    if verdict.passed:
        typer.echo("  verdict   PASS", err=err)
        return
    typer.echo(f"  verdict   FAIL ({len(verdict.violations)} violation(s))", err=err)
    for violation in verdict.violations:
        typer.echo(f"    {violation.location}: [{violation.code}] {violation.message}", err=err)


def guarded(func: Callable[..., Any]) -> Callable[..., Any]:
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


def print_checks(result: VerifyResult, err: bool = False) -> None:
    for check in result.checks:
        status = "PASS" if check.passed else "FAIL"
        typer.echo(f"  {status}  {check.name:<42} {check.detail}", err=err)
