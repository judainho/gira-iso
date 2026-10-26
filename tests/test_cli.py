"""The simple CLI surface: list, create, download."""

from __future__ import annotations

from pathlib import Path

import yaml
from typer.testing import CliRunner

from gira_iso.builder.build import BuildResult
from gira_iso.builder.manifest import (
    Artifact,
    BaseRef,
    BuilderInfo,
    Manifest,
    PolicyRef,
    file_sha256,
)
from gira_iso.cli import app

runner = CliRunner()
IMAGE = "gira-iso-ubuntu-24.04.qcow2"


def write_build(
    root: Path,
    *,
    name: str = "web",
    content: bytes = b"qcow2-bytes",
    build_id: str = "20260101T000000Z-abcdef123456",
) -> Path:
    """Create a fake build directory with a valid manifest and artifact."""
    build_dir = root / build_id
    image = build_dir / "image" / IMAGE
    image.parent.mkdir(parents=True)
    image.write_bytes(content)
    manifest = Manifest(
        name=name,
        spec_sha256="0" * 64,
        spec={"spec_version": "0"},
        base=BaseRef(
            os="ubuntu",
            version="24.04",
            url="https://example.invalid/base.img",
            sha256="1" * 64,
        ),
        policy=PolicyRef(source="test", sha256="2" * 64),
        builder=BuilderInfo(
            packer_version="1.16.2", plugin_version="1.1.7", template_sha256="3" * 64
        ),
        packages=["nginx"],
        services=["nginx"],
        artifact=Artifact(
            file=IMAGE,
            sha256=file_sha256(image),
            size_bytes=len(content),
            virtual_size_bytes=10 * 1024**3,
        ),
    )
    manifest.write(build_dir)
    return build_dir


def test_list_without_builds(tmp_path: Path) -> None:
    result = runner.invoke(app, ["list", "--work-root", str(tmp_path / "builds")])
    assert result.exit_code == 0
    assert "no images found" in result.output


def test_list_shows_name_created_and_size(tmp_path: Path) -> None:
    write_build(tmp_path)
    result = runner.invoke(app, ["list", "--work-root", str(tmp_path)])

    assert result.exit_code == 0
    assert "web" in result.output
    assert "10.0" in result.output
    assert "20260101T000000Z-abcdef123456" in result.output


def test_download_copies_image_and_manifest(tmp_path: Path) -> None:
    write_build(tmp_path)
    destination = tmp_path / "out"

    result = runner.invoke(
        app, ["download", "web", "--work-root", str(tmp_path), "--to", str(destination)]
    )

    assert result.exit_code == 0, result.output
    assert (destination / "web.qcow2").read_bytes() == b"qcow2-bytes"
    assert (destination / "web.manifest.json").is_file()


def test_download_by_build_id_prefix(tmp_path: Path) -> None:
    write_build(tmp_path)
    destination = tmp_path / "out"

    result = runner.invoke(
        app,
        ["download", "20260101", "--work-root", str(tmp_path), "--to", str(destination)],
    )

    assert result.exit_code == 0, result.output
    assert (destination / "web.qcow2").is_file()


def test_download_refuses_a_tampered_artifact(tmp_path: Path) -> None:
    build_dir = write_build(tmp_path)
    (build_dir / "image" / IMAGE).write_bytes(b"tampered")
    destination = tmp_path / "out"

    result = runner.invoke(
        app, ["download", "web", "--work-root", str(tmp_path), "--to", str(destination)]
    )

    assert result.exit_code == 1
    assert "checksum" in result.output
    assert not (destination / "web.qcow2").exists()


def test_download_refuses_to_overwrite_without_force(tmp_path: Path) -> None:
    write_build(tmp_path)
    destination = tmp_path / "out"
    args = ["download", "web", "--work-root", str(tmp_path), "--to", str(destination)]

    assert runner.invoke(app, args).exit_code == 0
    again = runner.invoke(app, args)
    assert again.exit_code == 1
    assert "--force" in again.output

    forced = runner.invoke(app, [*args, "--force"])
    assert forced.exit_code == 0


def test_download_unknown_name_is_invalid_input(tmp_path: Path) -> None:
    write_build(tmp_path)
    result = runner.invoke(app, ["download", "nope", "--work-root", str(tmp_path)])
    assert result.exit_code == 2
    assert "no image matches" in result.output


def test_download_ambiguous_name_lists_the_builds(tmp_path: Path) -> None:
    write_build(tmp_path, build_id="20260101T000000Z-aaaaaaaaaaaa")
    write_build(tmp_path, build_id="20260102T000000Z-bbbbbbbbbbbb")

    result = runner.invoke(app, ["download", "web", "--work-root", str(tmp_path)])

    assert result.exit_code == 2
    assert "ambiguous" in result.output
    assert "20260101T000000Z-aaaaaaaaaaaa" in result.output


def test_create_rejects_a_policy_violation(tmp_path: Path) -> None:
    spec = tmp_path / "bad.yaml"
    spec.write_text(
        "spec_version: '0'\n"
        "base: {os: ubuntu, version: '24.04'}\n"
        "disk_size: 8G\n"
        "packages: [netcat-openbsd]\n"
        "services: {enable: [nginx]}\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["create", str(spec), "--work-root", str(tmp_path / "builds")])

    assert result.exit_code == 1
    assert "not in the package allowlist" in result.output
    assert not (tmp_path / "builds").exists()


def test_create_records_the_name(monkeypatch, tmp_path: Path) -> None:
    build_dir = write_build(tmp_path / "builds", name="web")
    fake = BuildResult(
        build_dir=build_dir,
        image=build_dir / "image" / IMAGE,
        manifest=build_dir / "manifest.json",
    )

    import gira_iso.cli as cli

    monkeypatch.setattr(cli, "build_image", lambda spec, **kwargs: fake)

    spec = tmp_path / "web.yaml"
    spec.write_text(Path("examples/nginx-web.yaml").read_text(encoding="utf-8"), encoding="utf-8")

    result = runner.invoke(app, ["create", str(spec), "--work-root", str(tmp_path / "builds")])

    assert result.exit_code == 0, result.output
    assert "name      web" in result.output
    assert "manifest" in result.output


def test_create_rejects_an_invalid_name(tmp_path: Path) -> None:
    spec = tmp_path / "web.yaml"
    spec.write_text(Path("examples/nginx-web.yaml").read_text(encoding="utf-8"), encoding="utf-8")

    result = runner.invoke(app, ["create", str(spec), "--name", "bad name"])

    assert result.exit_code == 2
    assert "invalid image name" in result.output


class FakeCompleter:
    def __init__(self, *texts: str) -> None:
        self._texts = list(texts)

    def complete(self, system: str, user: str):
        from gira_iso.nl.client import Completion

        return Completion(text=self._texts.pop(0))


def _nl_fixture(monkeypatch, tmp_path: Path, *replies: str):
    import gira_iso.cli as cli

    build_dir = write_build(tmp_path / "builds", name="web")
    fake = BuildResult(
        build_dir=build_dir,
        image=build_dir / "image" / IMAGE,
        manifest=build_dir / "manifest.json",
    )
    built: list = []
    monkeypatch.setattr(cli, "build_image", lambda spec, **kwargs: built.append(spec) or fake)
    monkeypatch.setattr(cli, "OpenAiCompatible", lambda config: FakeCompleter(*replies))
    monkeypatch.delenv("GIRA_ISO_LLM_API_KEY", raising=False)
    return built


def test_natural_language_prompts_for_the_key_and_builds(monkeypatch, tmp_path: Path) -> None:
    import json

    spec = yaml.safe_load(Path("examples/nginx-web.yaml").read_text(encoding="utf-8"))
    built = _nl_fixture(monkeypatch, tmp_path, json.dumps({"kind": "spec", "spec": spec}))

    result = runner.invoke(
        app, ["create", "un servidor nginx", "--name", "web", "-y"], input="secret-key\n"
    )

    assert result.exit_code == 0, result.output
    assert "https://api.llmapi.ai/v1" in result.output
    assert "secret-key" not in result.output
    assert len(built) == 1


def test_natural_language_uses_the_key_from_the_environment(monkeypatch, tmp_path: Path) -> None:
    import json

    spec = yaml.safe_load(Path("examples/nginx-web.yaml").read_text(encoding="utf-8"))
    _nl_fixture(monkeypatch, tmp_path, json.dumps({"kind": "spec", "spec": spec}))
    monkeypatch.setenv("GIRA_ISO_LLM_API_KEY", "from-env")

    result = runner.invoke(app, ["create", "un servidor nginx", "--name", "web", "-y"])

    assert result.exit_code == 0, result.output
    assert "API key" not in result.output


def test_natural_language_requires_a_name(monkeypatch, tmp_path: Path) -> None:
    _nl_fixture(monkeypatch, tmp_path)

    result = runner.invoke(app, ["create", "un servidor nginx"])

    assert result.exit_code == 2
    assert "--name is required" in result.output


def test_natural_language_refusal_builds_nothing(monkeypatch, tmp_path: Path) -> None:
    import json

    built = _nl_fixture(
        monkeypatch, tmp_path, json.dumps({"kind": "reject", "reason": "not an image request"})
    )

    result = runner.invoke(app, ["create", "hazme un café", "--name", "web", "-y"], input="k\n")

    assert result.exit_code == 1
    assert "not an image request" in result.output
    assert built == []


def _tui_fixture(monkeypatch, tmp_path: Path, *replies: str) -> list:
    import gira_iso.tui as tui

    build_dir = write_build(tmp_path / "builds", name="web")
    fake = BuildResult(
        build_dir=build_dir,
        image=build_dir / "image" / IMAGE,
        manifest=build_dir / "manifest.json",
    )
    built: list = []

    def fake_build(spec, **kwargs):
        built.append(spec)
        kwargs["progress"]("Validando")
        kwargs["on_line"]("==> qemu: [packer] line with [brackets]")
        return fake

    monkeypatch.setattr(tui, "build_image", fake_build)
    monkeypatch.setattr(tui, "OpenAiCompatible", lambda config: FakeCompleter(*replies))
    monkeypatch.delenv("GIRA_ISO_LLM_API_KEY", raising=False)
    return built


def test_menu_asks_for_the_key_first_then_creates_and_reports_the_path(
    monkeypatch, tmp_path: Path
) -> None:
    import json

    spec = yaml.safe_load(Path("examples/nginx-web.yaml").read_text(encoding="utf-8"))
    built = _tui_fixture(monkeypatch, tmp_path, json.dumps({"kind": "spec", "spec": spec}))
    destination = tmp_path / "out"

    answers = f"secret-key\n4\nun servidor nginx\nweb\ny\nn\n{destination}\n0\n"
    result = runner.invoke(app, [], input=answers)

    assert result.exit_code == 0, result.output
    assert result.output.index("API Key") < result.output.index("Menú")
    assert "secret-key" not in result.output
    assert "https://api.llmapi.ai/v1" in result.output
    assert "line with [brackets]" in result.output
    assert str(destination / "web.qcow2") in result.output
    assert (destination / "web.qcow2").is_file()
    assert len(built) == 1


def test_menu_lists_the_pinned_base_images(monkeypatch, tmp_path: Path) -> None:
    _tui_fixture(monkeypatch, tmp_path)

    result = runner.invoke(app, [], input="k\n3\n0\n")

    assert result.exit_code == 0, result.output
    assert "Imágenes base" in result.output
    assert "oficial" in result.output
    assert "24.04" in result.output


def test_menu_refuses_an_installer_iso_as_a_base(monkeypatch, tmp_path: Path) -> None:
    _tui_fixture(monkeypatch, tmp_path)
    iso = tmp_path / "live.iso"
    iso.write_bytes(b"\0" * 0x8001 + b"CD001" + b"\0" * 64)

    result = runner.invoke(app, [], input=f"k\n2\n{iso}\n26.10\nlive\n0\n")

    assert result.exit_code == 0, result.output
    assert "installer ISO" in result.output


def test_menu_lists_existing_images(monkeypatch, tmp_path: Path) -> None:
    import gira_iso.tui as tui

    _tui_fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(tui, "builds_dir", lambda: tmp_path / "builds")

    result = runner.invoke(app, [], input="k\n1\n0\n")

    assert result.exit_code == 0, result.output
    assert "web" in result.output


def test_subcommands_still_work_after_the_callback_change() -> None:
    result = runner.invoke(app, ["version"])

    assert result.exit_code == 0
    assert "gira-iso" in result.output
