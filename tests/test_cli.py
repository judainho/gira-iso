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
from gira_iso.nl.client import CredentialCheck

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
    monkeypatch.setattr(tui, "check_credentials", lambda config: CredentialCheck("ok"))
    monkeypatch.delenv("GIRA_ISO_LLM_API_KEY", raising=False)
    return built


def test_menu_asks_for_the_key_first_then_creates_and_reports_the_path(
    monkeypatch, tmp_path: Path
) -> None:
    import json

    spec = yaml.safe_load(Path("examples/nginx-web.yaml").read_text(encoding="utf-8"))
    built = _tui_fixture(monkeypatch, tmp_path, json.dumps({"kind": "spec", "spec": spec}))
    destination = tmp_path / "out"

    answers = f"secret-key\n4\n\nun servidor nginx\nweb\ny\nn\n{destination}\n0\n"
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

    result = runner.invoke(app, [], input="k\n1\n\n0\n")

    assert result.exit_code == 0, result.output
    assert "web" in result.output


def test_subcommands_still_work_after_the_callback_change() -> None:
    result = runner.invoke(app, ["version"])

    assert result.exit_code == 0
    assert "gira-iso" in result.output


def _spec_reply() -> str:
    import json

    spec = yaml.safe_load(Path("examples/nginx-web.yaml").read_text(encoding="utf-8"))
    return json.dumps({"kind": "spec", "spec": spec})


def test_an_invalid_key_is_rejected_and_the_user_can_retry(monkeypatch, tmp_path: Path) -> None:
    import gira_iso.tui as tui

    _tui_fixture(monkeypatch, tmp_path)
    seen: list[str] = []

    def check(config):
        seen.append(config.api_key)
        if config.api_key == "bad":
            return CredentialCheck("invalid", "el proveedor rechazó la clave")
        return CredentialCheck("ok")

    monkeypatch.setattr(tui, "check_credentials", check)

    result = runner.invoke(app, [], input="bad\ngood\n0\n")

    assert result.exit_code == 0, result.output
    assert seen == ["bad", "good"]
    assert "rechazó la clave" in result.output
    assert "API key válida" in result.output


def test_three_invalid_keys_end_the_session(monkeypatch, tmp_path: Path) -> None:
    import gira_iso.tui as tui

    _tui_fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(
        tui, "check_credentials", lambda config: CredentialCheck("invalid", "rechazada")
    )

    result = runner.invoke(app, [], input="a\nb\nc\n")

    assert result.exit_code == 0
    assert "Demasiados intentos" in result.output
    assert "Menú" not in result.output


def test_an_unverifiable_key_does_not_block_the_user(monkeypatch, tmp_path: Path) -> None:
    import gira_iso.tui as tui

    _tui_fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(
        tui, "check_credentials", lambda config: CredentialCheck("unverified", "sin red")
    )

    result = runner.invoke(app, [], input="k\n0\n")

    assert "No pude verificar la clave" in result.output
    assert "Menú" in result.output


def test_examples_can_be_picked_by_number_and_tokens_are_shown(monkeypatch, tmp_path: Path) -> None:
    import gira_iso.tui as tui
    from gira_iso.nl.client import Completion

    _tui_fixture(monkeypatch, tmp_path, _spec_reply())

    class Counting(FakeCompleter):
        def complete(self, system: str, user: str):
            self.last_user = user
            return Completion(
                text=self._texts.pop(0),
                latency_ms=1200,
                usage={"prompt_tokens": 321, "completion_tokens": 45},
            )

    completer = Counting(_spec_reply())
    monkeypatch.setattr(tui, "OpenAiCompatible", lambda config: completer)

    result = runner.invoke(app, [], input=f"k\n4\n\n2\nweb\nn\nn\n{tmp_path / 'out'}\n0\n")

    assert "docker instalado y htop" in completer.last_user
    assert "321 entrada" in result.output
    assert "45 salida" in result.output


def test_name_and_destination_are_remembered_for_the_next_run(monkeypatch, tmp_path: Path) -> None:
    from gira_iso import settings

    _tui_fixture(monkeypatch, tmp_path, _spec_reply())
    destination = tmp_path / "out"

    runner.invoke(app, [], input=f"k\n4\n\nun nginx\nweb\ny\nn\n{destination}\n0\n")

    saved = settings.load()
    assert saved.last_name == "web"
    assert saved.last_destination == str(destination)


def test_ctrl_c_in_an_action_returns_to_the_menu(monkeypatch, tmp_path: Path) -> None:
    import gira_iso.tui as tui

    _tui_fixture(monkeypatch, tmp_path)

    def boom(console):
        raise KeyboardInterrupt

    monkeypatch.setattr(tui, "list_images", boom)

    result = runner.invoke(app, [], input="k\n1\n\n0\n")

    assert "Acción cancelada" in result.output
    assert result.output.count("Menú") == 2


# --- show / delete -------------------------------------------------------------------


def test_show_prints_the_recorded_provenance(tmp_path: Path) -> None:
    write_build(tmp_path)

    result = runner.invoke(app, ["show", "web", "--work-root", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "ubuntu 24.04" in result.output
    assert "1" * 64 in result.output
    assert "nginx" in result.output


def test_delete_removes_the_build_after_confirmation(tmp_path: Path) -> None:
    build_dir = write_build(tmp_path)

    result = runner.invoke(app, ["delete", "web", "--work-root", str(tmp_path)], input="y\n")

    assert result.exit_code == 0, result.output
    assert not build_dir.exists()


def test_delete_keeps_the_build_when_not_confirmed(tmp_path: Path) -> None:
    build_dir = write_build(tmp_path)

    result = runner.invoke(app, ["delete", "web", "--work-root", str(tmp_path)], input="n\n")

    assert result.exit_code == 0
    assert build_dir.exists()


def test_delete_with_yes_skips_the_question(tmp_path: Path) -> None:
    build_dir = write_build(tmp_path)

    result = runner.invoke(app, ["delete", "web", "--work-root", str(tmp_path), "--yes"])

    assert result.exit_code == 0, result.output
    assert not build_dir.exists()


def test_delete_refuses_a_build_outside_the_work_root(tmp_path: Path) -> None:
    elsewhere = write_build(tmp_path / "elsewhere")

    result = runner.invoke(
        app, ["delete", str(elsewhere), "--work-root", str(tmp_path / "builds"), "--yes"]
    )

    assert result.exit_code == 2
    assert "is not a build under" in result.output
    assert elsewhere.exists()


# --- menu: images and bases ----------------------------------------------------------


def test_menu_image_detail_export_and_delete(monkeypatch, tmp_path: Path) -> None:
    import gira_iso.tui as tui

    _tui_fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(tui, "builds_dir", lambda: tmp_path / "builds")
    out = tmp_path / "out"

    steps = [
        "k",
        "1",
        "1",  # open the first image
        "detalle",
        "exportar",
        str(out),
        "eliminar",
        "web",  # type the name to confirm
        "0",
    ]
    result = runner.invoke(app, [], input="\n".join(steps) + "\n")

    assert result.exit_code == 0, result.output
    assert "Imagen sha256" in result.output
    assert (out / "web.qcow2").is_file()
    assert "Eliminada" in result.output
    assert not (tmp_path / "builds" / "20260101T000000Z-abcdef123456").exists()


def test_menu_delete_needs_the_exact_name(monkeypatch, tmp_path: Path) -> None:
    import gira_iso.tui as tui

    _tui_fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(tui, "builds_dir", lambda: tmp_path / "builds")

    steps = ["k", "1", "1", "eliminar", "wrong", "volver", "0"]
    result = runner.invoke(app, [], input="\n".join(steps) + "\n")

    assert "No se eliminó nada" in result.output
    assert (tmp_path / "builds" / "20260101T000000Z-abcdef123456").exists()


def test_menu_downloads_an_official_base(monkeypatch, tmp_path: Path) -> None:
    import gira_iso.tui as tui

    _tui_fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(tui, "cache_dir", lambda: tmp_path / "empty-cache")
    calls: list[tuple[str, str]] = []

    def fake_ensure(os_name, version, allowlist, on_progress=None):
        calls.append((os_name, version))
        on_progress(5, 10)
        return tmp_path / "base.img", "a" * 64

    monkeypatch.setattr(tui, "ensure_base_image", fake_ensure)

    result = runner.invoke(app, [], input="k\n3\n1\n0\n")

    assert result.exit_code == 0, result.output
    assert calls == [("ubuntu", "22.04")]
    assert "descargada y verificada" in result.output


def test_menu_lists_both_official_bases(monkeypatch, tmp_path: Path) -> None:
    _tui_fixture(monkeypatch, tmp_path)

    result = runner.invoke(app, [], input="k\n3\n\n0\n")

    assert "22.04" in result.output
    assert "24.04" in result.output


def test_delete_build_refuses_symlinks_and_foreign_directories(tmp_path: Path) -> None:
    import pytest

    from gira_iso.builder.export import ExportError, delete_build

    real = write_build(tmp_path / "elsewhere")
    root = tmp_path / "builds"
    root.mkdir()
    link = root / "link"
    link.symlink_to(real, target_is_directory=True)

    with pytest.raises(ExportError, match="is not a build under"):
        delete_build(link / "manifest.json", root)
    with pytest.raises(ExportError, match="is not a build under"):
        delete_build(real / "manifest.json", root)
    assert real.exists()


def test_delete_build_refuses_a_directory_without_a_manifest(tmp_path: Path) -> None:
    import pytest

    from gira_iso.builder.export import ExportError, delete_build

    root = tmp_path / "builds"
    stray = root / "not-a-build"
    stray.mkdir(parents=True)

    with pytest.raises(ExportError, match="has no manifest"):
        delete_build(stray / "manifest.json", root)
    assert stray.exists()


def test_describe_lists_the_hashes_a_reader_needs(tmp_path: Path) -> None:
    from gira_iso.builder.export import describe, scan_builds

    write_build(tmp_path)
    manifest, manifest_path = scan_builds(tmp_path)[0]

    rows = dict(describe(manifest, manifest_path.parent))

    assert rows["Base sha256"] == "1" * 64
    assert rows["Imagen sha256"] == manifest.artifact.sha256
    assert rows["Paquetes"] == "nginx"


# --- automation: --json and --non-interactive ----------------------------------------


def _automation_fixture(monkeypatch, tmp_path: Path, *replies: str) -> list:
    import gira_iso.cli as cli

    build_dir = write_build(tmp_path / "builds", name="web")
    fake = BuildResult(
        build_dir=build_dir,
        image=build_dir / "image" / IMAGE,
        manifest=build_dir / "manifest.json",
    )
    built: list = []

    def fake_build(spec, **kwargs):
        built.append(spec)
        kwargs["progress"]("Validando el spec")
        return fake

    monkeypatch.setattr(cli, "build_image", fake_build)
    monkeypatch.setattr(cli, "OpenAiCompatible", lambda config: FakeCompleter(*replies))
    monkeypatch.delenv("GIRA_ISO_LLM_API_KEY", raising=False)
    return built


def test_json_output_is_the_only_thing_on_stdout(monkeypatch, tmp_path: Path) -> None:
    import json

    _automation_fixture(monkeypatch, tmp_path, _spec_reply())
    monkeypatch.setenv("GIRA_ISO_LLM_API_KEY", "from-env")

    result = runner.invoke(
        app, ["create", "un nginx", "--name", "web", "--yes", "--json", "--non-interactive"]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["name"] == "web"
    assert payload["base"] == "ubuntu 24.04"
    assert payload["image"].endswith(IMAGE)
    assert len(payload["sha256"]) == 64
    assert payload["verified"] is None
    assert "Validando el spec" in result.stderr
    assert "interpreted request as" in result.stderr
    assert "Validando" not in result.stdout


def test_non_interactive_without_a_key_fails_fast(monkeypatch, tmp_path: Path) -> None:
    built = _automation_fixture(monkeypatch, tmp_path)

    result = runner.invoke(app, ["create", "un nginx", "--name", "web", "--non-interactive"])

    assert result.exit_code == 2
    assert "GIRA_ISO_LLM_API_KEY" in result.stderr
    assert built == []


def test_non_interactive_never_asks_the_model_questions(monkeypatch, tmp_path: Path) -> None:
    import json

    built = _automation_fixture(
        monkeypatch,
        tmp_path,
        json.dumps({"kind": "questions", "questions": ["Which user?"]}),
    )
    monkeypatch.setenv("GIRA_ISO_LLM_API_KEY", "from-env")

    result = runner.invoke(app, ["create", "un nginx", "--name", "web", "--non-interactive"])

    assert result.exit_code == 1
    assert "Which user?" in result.stderr
    assert built == []


def test_non_interactive_skips_the_build_confirmation(monkeypatch, tmp_path: Path) -> None:
    built = _automation_fixture(monkeypatch, tmp_path, _spec_reply())
    monkeypatch.setenv("GIRA_ISO_LLM_API_KEY", "from-env")

    result = runner.invoke(app, ["create", "un nginx", "--name", "web", "--non-interactive"])

    assert result.exit_code == 0, result.output
    assert len(built) == 1
    assert "build this image?" not in result.output


def test_json_with_a_policy_rejection_leaves_stdout_empty(monkeypatch, tmp_path: Path) -> None:
    spec = tmp_path / "bad.yaml"
    spec.write_text("packages: [definitely-not-allowlisted-pkg]\n", encoding="utf-8")

    result = runner.invoke(
        app, ["create", str(spec), "--json", "--work-root", str(tmp_path / "builds")]
    )

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "not in the package allowlist" in result.stderr


def test_list_json(tmp_path: Path) -> None:
    import json

    write_build(tmp_path)

    result = runner.invoke(app, ["list", "--json", "--work-root", str(tmp_path)])

    assert result.exit_code == 0, result.output
    rows = json.loads(result.stdout)
    assert [row["name"] for row in rows] == ["web"]
    assert rows[0]["virtual_size_bytes"] == 10 * 1024**3


def test_list_json_with_no_images_is_an_empty_array(tmp_path: Path) -> None:
    import json

    result = runner.invoke(app, ["list", "--json", "--work-root", str(tmp_path / "none")])

    assert json.loads(result.stdout) == []


def test_show_json_is_the_manifest(tmp_path: Path) -> None:
    import json

    write_build(tmp_path)

    result = runner.invoke(app, ["show", "web", "--json", "--work-root", str(tmp_path)])

    payload = json.loads(result.stdout)
    assert payload["name"] == "web"
    assert payload["base"]["sha256"] == "1" * 64
    assert payload["build_dir"].endswith("20260101T000000Z-abcdef123456")
