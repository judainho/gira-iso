"""Commands over images that already exist: list, download, show, delete, clean."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from gira_iso.bases import builds_dir
from gira_iso.builder.catalog import (
    directory_size,
    display_name,
    find_incomplete,
    resolve_image,
    scan_builds,
)
from gira_iso.builder.export import delete_build, describe, export_image, remove_incomplete
from gira_iso.cli._app import WORK_ROOT_HELP, app
from gira_iso.cli._shared import guarded
from gira_iso.errors import (
    EXIT_OK,
)
from gira_iso.naming import checked_name


@app.command("list")
@guarded
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


@app.command()
@guarded
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
@guarded
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
@guarded
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


@app.command()
@guarded
def clean(
    work_root: Path = typer.Option(None, "--work-root", help=WORK_ROOT_HELP),
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask for confirmation"),
) -> None:
    """Delete builds that never finished: failed, cancelled or killed."""
    root = work_root or builds_dir()
    incomplete = find_incomplete(root)
    if not incomplete:
        typer.echo("nothing to clean")
        return
    total = 0
    for directory in incomplete:
        size = directory_size(directory)
        total += size
        typer.echo(f"  {directory.name}  {size / 1024**2:.0f} MB")
    if not yes and not typer.confirm(
        f"delete {len(incomplete)} incomplete build(s) ({total / 1024**2:.0f} MB)?"
    ):
        raise typer.Exit(EXIT_OK)
    removed = remove_incomplete(root)
    freed = sum(size for _, size in removed)
    typer.echo(f"  removed   {len(removed)} build(s), {freed / 1024**2:.0f} MB freed")
