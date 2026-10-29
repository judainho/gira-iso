"""Snaps and curated vendor apt repositories.

Every test here pins one claim the feature makes: a snap the policy does not list never
reaches the image, a repository package is installed only behind a verified key, and the
manifest says exactly what came from where.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml
from conftest import build_spec
from pydantic import ValidationError

from gira_iso.builder.manifest import (
    BaseRef,
    BuilderInfo,
    Manifest,
    PolicyRef,
    RepositoryRef,
    SnapRef,
    build_manifest,
)
from gira_iso.builder.render import (
    archive_packages,
    cloud_init,
    effective_packages,
    render,
    repository_packages,
    required_repositories,
)
from gira_iso.policy.evaluate import Allowlist, evaluate, load_allowlist
from gira_iso.spec.models import Spec
from gira_iso.verify.goss import build_goss

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAABAgMEBQYHCAkKCwwNDg8QERITFBUWFxgZGhscHR4f"

BASE_POLICY: dict = {
    "base_images": {"ubuntu": {"24.04": {"url": "https://x/y.img", "sha256": "1" * 64}}},
    "packages": ["nginx"],
    "services": ["nginx"],
    "file_prefixes": ["/etc/nginx/"],
}


def policy(**overrides) -> Allowlist:
    return Allowlist.from_mapping({**BASE_POLICY, **overrides})


# --- schema ------------------------------------------------------------------


def test_snaps_inside_the_policy_pass_the_schema() -> None:
    spec = build_spec(snaps=["docker", "certbot"])
    assert spec.snaps == ["docker", "certbot"]


def test_default_spec_version_is_one() -> None:
    assert Spec.model_validate({"base": {"os": "ubuntu", "version": "24.04"}}).spec_version == "1"


def test_version_zero_still_validates() -> None:
    """Files and manifests written before this change must keep working."""
    assert build_spec(spec_version="0").spec_version == "0"


def test_version_zero_rejects_snaps() -> None:
    with pytest.raises(ValidationError, match="spec_version"):
        build_spec(spec_version="0", snaps=["docker"])


@pytest.mark.parametrize("name", ["Docker", "docker_ce", "-docker", "a" * 41])
def test_invalid_snap_names_are_rejected(name: str) -> None:
    with pytest.raises(ValidationError):
        build_spec(snaps=[name])


def test_duplicate_snaps_are_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate snaps"):
        build_spec(snaps=["docker", "docker"])


# --- policy ------------------------------------------------------------------


def test_snap_outside_the_allowlist_is_rejected() -> None:
    verdict = evaluate(build_spec(snaps=["lxd"]), policy())
    assert verdict.codes() == ("snap.not_allowed",)


def test_snap_inside_the_allowlist_passes() -> None:
    allowlist = policy(snaps={"docker": {"channel": "stable", "classic": True}})
    assert evaluate(build_spec(snaps=["docker"]), allowlist).passed


def test_a_snap_service_cannot_also_be_enabled_through_systemd() -> None:
    """The snap owns its unit; asking systemd for `docker` would fail at the last step."""
    allowlist = policy(
        snaps={"docker": {"channel": "stable", "classic": True}},
        services=["nginx", "docker"],
    )
    verdict = evaluate(build_spec(snaps=["docker"], services={"enable": ["docker"]}), allowlist)
    assert verdict.codes() == ("service.snap_managed",)


def test_package_may_name_a_repository() -> None:
    allowlist = policy(
        packages=["nginx", {"name": "docker-ce", "repository": "docker-ce"}],
        repositories={
            "docker-ce": {
                "url": "https://download.docker.com/linux/ubuntu",
                "suite": "{codename}",
                "components": ["stable"],
                "key_url": "https://download.docker.com/linux/ubuntu/gpg",
                "key_sha256": "a" * 64,
            }
        },
    )
    assert allowlist.allows_package("docker-ce", "24.04")
    assert allowlist.repository_of("docker-ce") == "docker-ce"
    assert allowlist.repository_of("nginx") is None


def test_a_package_pointing_at_an_unknown_repository_is_a_policy_error() -> None:
    with pytest.raises(ValueError, match="unknown repository"):
        policy(packages=[{"name": "docker-ce", "repository": "nope"}])


def test_a_repository_without_a_valid_key_hash_is_a_policy_error() -> None:
    with pytest.raises(ValueError, match="key_sha256"):
        policy(
            packages=[{"name": "docker-ce", "repository": "docker-ce"}],
            repositories={
                "docker-ce": {
                    "url": "https://download.docker.com/linux/ubuntu",
                    "suite": "{codename}",
                    "components": ["stable"],
                    "key_url": "https://download.docker.com/linux/ubuntu/gpg",
                    "key_sha256": "not-a-hash",
                }
            },
        )


def test_an_unknown_snap_channel_is_a_policy_error() -> None:
    with pytest.raises(ValueError, match="channel"):
        policy(snaps={"docker": {"channel": "nightly", "classic": True}})


def test_the_packaged_allowlist_ships_the_reviewed_set() -> None:
    active = load_allowlist()
    assert {"docker", "certbot"} <= set(active.snaps)
    assert {"docker-ce", "nodesource"} <= set(active.repositories)
    for repository in active.repositories.values():
        assert len(repository.key_sha256) == 64
    assert active.repository_of("docker-ce") == "docker-ce"
    assert active.repository_of("nodejs") == "nodesource"


# --- rendering ---------------------------------------------------------------


def test_repository_packages_never_reach_cloud_init() -> None:
    """cloud-init runs before the repository exists, so asking it for docker-ce would fail."""
    allowlist = load_allowlist()
    spec = build_spec(packages=["nginx", "docker-ce"])
    user_data, _ = cloud_init(spec, KEY, allowlist)
    parsed = yaml.safe_load(user_data.split("\n", 1)[1])
    assert "nginx" in parsed["packages"]
    assert "docker-ce" not in parsed["packages"]
    assert "qemu-guest-agent" in parsed["packages"]


def test_archive_and_repository_packages_are_split() -> None:
    allowlist = load_allowlist()
    spec = build_spec(packages=["nginx", "docker-ce", "nodejs"])
    assert "nginx" in archive_packages(spec, allowlist)
    assert "docker-ce" not in archive_packages(spec, allowlist)
    assert "nodejs" not in archive_packages(spec, allowlist)
    assert repository_packages(spec, allowlist) == {
        "docker-ce": ["docker-ce"],
        "nodesource": ["nodejs"],
    }
    assert set(effective_packages(spec, allowlist)) >= {"nginx", "docker-ce", "nodejs"}


def test_repository_suite_resolves_the_release_codename() -> None:
    spec = build_spec(packages=["docker-ce"])
    resolved = required_repositories(spec, load_allowlist())
    assert resolved[0].id == "docker-ce"
    assert resolved[0].suite == "noble"
    assert resolved[0].components == ("stable",)


def test_no_repository_is_resolved_when_no_package_needs_one() -> None:
    assert required_repositories(build_spec(packages=["nginx"]), load_allowlist()) == []


def test_repository_script_waits_for_cloud_init_before_using_apt(tmp_path: Path) -> None:
    """cloud-init installs the archive packages and starts the snaps; apt must not run alongside."""
    render(build_spec(packages=["docker-ce"]), tmp_path, load_allowlist(), KEY)
    script = (tmp_path / "provision" / "repositories.sh").read_text(encoding="utf-8")
    assert script.index("cloud-init status --wait") < script.index("apt-get")


def test_repository_script_verifies_the_key_before_using_it(tmp_path: Path) -> None:
    spec = build_spec(packages=["docker-ce"])
    render(spec, tmp_path, load_allowlist(), KEY)
    script = (tmp_path / "provision" / "repositories.sh").read_text(encoding="utf-8")

    key_check = script.index("sha256sum -c")
    source_write = script.index("Signed-By:")
    install = script.index("--no-install-recommends")
    assert key_check < source_write < install
    for expected in (
        "1500c1f56fa9e26b9b8f42452a553675796ade0807cdce11975eb98170b3a570",
        "download.docker.com/linux/ubuntu/gpg",
        "Signed-By: /usr/share/keyrings/docker-ce.asc",
        "Suites: noble",
        "Components: stable",
    ):
        assert expected in script


def test_no_repository_script_when_nothing_comes_from_a_repository(tmp_path: Path) -> None:
    render(build_spec(packages=["nginx"]), tmp_path, load_allowlist(), KEY)
    assert not (tmp_path / "provision" / "repositories.sh").exists()


def test_packer_runs_the_repository_script_before_asserting(tmp_path: Path) -> None:
    render(build_spec(packages=["docker-ce"]), tmp_path, load_allowlist(), KEY)
    template = (tmp_path / "packer" / "image.pkr.hcl").read_text(encoding="utf-8")
    assert template.index("repositories.sh") < template.index("assert.sh")


def test_assert_script_checks_snaps(tmp_path: Path) -> None:
    render(build_spec(snaps=["docker"]), tmp_path, load_allowlist(), KEY)
    script = (tmp_path / "provision" / "assert.sh").read_text(encoding="utf-8")
    assert "snap list" in script
    assert "'docker'" in script


# --- manifest ----------------------------------------------------------------


def _artifact(tmp_path: Path) -> Path:
    image = tmp_path / "image.qcow2"
    image.write_bytes(b"qcow2-bytes")
    return image


def test_manifest_records_snaps_and_repositories(tmp_path: Path) -> None:
    manifest = build_manifest(
        spec=build_spec(),
        spec_sha256="0" * 64,
        base=BaseRef(os="ubuntu", version="24.04", url="https://x/y.img", sha256="1" * 64),
        policy=PolicyRef(source="test", sha256="2" * 64),
        builder=BuilderInfo(packer_version="1", plugin_version="1", template_sha256="3" * 64),
        packages=["docker-ce"],
        services=[],
        snaps=[SnapRef(name="docker", channel="stable", classic=True, revision=2932)],
        repositories=[
            RepositoryRef(
                id="docker-ce",
                url="https://download.docker.com/linux/ubuntu",
                suite="noble",
                components=["stable"],
                key_sha256="a" * 64,
            )
        ],
        artifact_path=_artifact(tmp_path),
        virtual_size_bytes=10,
    )
    assert manifest.snaps[0].revision == 2932
    assert manifest.repositories[0].id == "docker-ce"


def test_a_manifest_without_the_new_fields_still_validates(tmp_path: Path) -> None:
    manifest = build_manifest(
        spec=build_spec(),
        spec_sha256="0" * 64,
        base=BaseRef(os="ubuntu", version="24.04", url="https://x/y.img", sha256="1" * 64),
        policy=PolicyRef(source="test", sha256="2" * 64),
        builder=BuilderInfo(packer_version="1", plugin_version="1", template_sha256="3" * 64),
        packages=["nginx"],
        services=[],
        artifact_path=_artifact(tmp_path),
        virtual_size_bytes=10,
    )
    assert manifest.snaps == [] and manifest.repositories == []
    assert Manifest.model_validate(manifest.model_dump(mode="json")).snaps == []


# --- verification ------------------------------------------------------------


def test_goss_asserts_each_snap_is_installed() -> None:
    document = build_goss(build_spec(snaps=["docker"]), ["nginx"])
    assert document["command"]["snap-docker"]["exit-status"] == 0
    assert "snap list docker" in document["command"]["snap-docker"]["exec"]


# --- hardening raised by the review ------------------------------------------


def repo(**overrides) -> dict:
    data = {
        "url": "https://download.docker.com/linux/ubuntu",
        "suite": "{codename}",
        "components": ["stable"],
        "key_url": "https://download.docker.com/linux/ubuntu/gpg",
        "key_sha256": "a" * 64,
    }
    data.update(overrides)
    return data


def repo_policy(identifier: str = "docker-ce", **overrides) -> Allowlist:
    return policy(
        packages=[{"name": "docker-ce", "repository": identifier}],
        repositories={identifier: repo(**overrides)},
    )


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("suite", "noble\nTypes: deb"),
        ("suite", "noble; rm -rf /"),
        ("suite", "no ble"),
        ("url", "https://x/'; touch /tmp/pwned; '"),
        ("url", "http://insecure.example"),
        ("url", "https://x/`id`"),
        ("key_url", "https://x/\n"),
        ("components", ["sta ble"]),
        ("components", ["stable", "main;evil"]),
        ("id", "../keyrings/evil"),
        ("id", "Docker"),
    ],
)
def test_repository_fields_are_data_not_shell(field_name: str, value) -> None:
    """These values are interpolated into a script that runs as root. Pattern-check them."""
    if field_name == "id":
        with pytest.raises(ValueError, match="repository"):
            repo_policy(value)
        return
    with pytest.raises(ValueError, match=field_name):
        repo_policy("docker-ce", **{field_name: value})


def test_a_well_formed_repository_still_loads() -> None:
    active = repo_policy()
    assert "docker-ce" in active.repositories


def test_seed_installs_snaps_before_it_enables_services() -> None:
    spec = build_spec(packages=["nginx"], snaps=["docker"], services={"enable": ["nginx"]})
    user_data, _ = cloud_init(spec, KEY, load_allowlist())
    runcmd = yaml.safe_load(user_data.split("\n", 1)[1])["runcmd"]
    install = next(i for i, line in enumerate(runcmd) if line.startswith("snap install docker"))
    enable = next(i for i, line in enumerate(runcmd) if line == "systemctl enable nginx")
    assert install < enable


def test_the_provisioner_retries_a_transient_network_failure(tmp_path: Path) -> None:
    render(build_spec(packages=["docker-ce"]), tmp_path, load_allowlist(), KEY)
    script = (tmp_path / "provision" / "repositories.sh").read_text(encoding="utf-8")
    assert "--tries" in script and "--timeout" in script  # the wget fallback
    assert "Acquire::Retries" in script


def test_a_wrong_key_hash_actually_fails_the_script(tmp_path: Path) -> None:
    """The README promises no warning path. Prove it by running the script, not by reading it."""
    import subprocess

    render(build_spec(packages=["docker-ce"]), tmp_path, load_allowlist(), KEY)
    body = (tmp_path / "provision" / "repositories.sh").read_text(encoding="utf-8")
    body = body.replace("/usr/share/keyrings", str(tmp_path / "keyrings"))
    body = body.replace("/etc/apt/sources.list.d", str(tmp_path / "sources"))
    script = tmp_path / "run.sh"
    script.write_text(body, encoding="utf-8")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "curl").write_text(
        "#!/bin/sh\n"
        'out=""\n'
        'while [ $# -gt 0 ]; do case "$1" in -o) out="$2"; shift 2;; *) shift;; esac; done\n'
        "printf 'not-the-key\\n' > \"$out\"\n"
    )
    for name in ("cloud-init", "apt-get"):
        (bin_dir / name).write_text("#!/bin/sh\nexit 0\n")
    for stub in bin_dir.iterdir():
        stub.chmod(0o755)

    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    result = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True)

    assert result.returncode != 0, result.stdout
    assert not (tmp_path / "keyrings" / "docker-ce.asc").exists()
    assert not (tmp_path / "sources" / "docker-ce.sources").exists()


def test_the_prompt_lists_the_allowed_snaps() -> None:
    from gira_iso.nl.prompt import catalog_section

    section = catalog_section(load_allowlist())
    snaps = section.split("Snaps:")[1]
    assert "docker" in snaps
    assert "certbot" in snaps


def test_catalog_lists_a_snap_it_cannot_describe_by_name_only() -> None:
    """A drift between allowlist and catalog must be visible, not papered over."""
    from gira_iso.nl.prompt import catalog_section

    allowlist = policy(snaps={"mystery": {"channel": "edge"}})
    section = catalog_section(allowlist)
    assert "  mystery" in section.split("Snaps:")[1]
    assert "channel edge" not in section


def test_unknown_release_message_mentions_repository_suites() -> None:
    from gira_iso.releases import UnknownRelease, codename_for

    with pytest.raises(UnknownRelease) as raised:
        codename_for("20.04")
    assert "repositor" in str(raised.value)
    assert "20.04" in str(raised.value)


def test_a_build_with_snaps_and_a_repository_records_its_provenance(tmp_path, monkeypatch) -> None:
    """S6 end to end: the wiring in builder/build.py, not a hand-built manifest."""
    import json

    from gira_iso.builder import build as build_module
    from gira_iso.builder import packer
    from gira_iso.builder.build import build_image

    base = tmp_path / "base.img"
    base.write_bytes(b"QFI\xfb")
    monkeypatch.setattr(
        build_module,
        "ensure_base_image",
        lambda os_name, version, allowlist, on_progress=None: (base, "1" * 64),
    )
    monkeypatch.setattr(build_module, "_virtual_size", lambda path: 10)

    def fake_build(template_dir, variables, log, on_line=None):
        image = Path(variables["build_dir"]) / "image" / build_module.IMAGE_NAME
        image.parent.mkdir(parents=True, exist_ok=True)
        image.write_bytes(b"qcow2-bytes")

    monkeypatch.setattr(packer, "init", lambda *args, **kwargs: None)
    monkeypatch.setattr(packer, "build", fake_build)
    monkeypatch.setattr(packer, "version", lambda: "1.16.2")

    spec = build_spec(packages=["nginx", "docker-ce"], snaps=["docker"])
    result = build_image(spec, work_root=tmp_path / "builds")

    manifest = json.loads(result.manifest.read_text(encoding="utf-8"))
    assert manifest["snaps"] == [
        {"name": "docker", "channel": "stable", "classic": True, "revision": None}
    ]
    assert manifest["repositories"] == [
        {
            "id": "docker-ce",
            "url": "https://download.docker.com/linux/ubuntu",
            "suite": "noble",
            "components": ["stable"],
            "key_sha256": "1500c1f56fa9e26b9b8f42452a553675796ade0807cdce11975eb98170b3a570",
        }
    ]


def test_check_names_the_repository_that_provides_a_package(tmp_path: Path) -> None:
    """S9: a repository is not in the spec, so `check` must say which one a package pulls in."""
    from typer.testing import CliRunner

    from gira_iso.cli import app

    spec = tmp_path / "mixed.yaml"
    spec.write_text(yaml.safe_dump({"spec_version": "1", "packages": ["nodejs"]}), encoding="utf-8")

    result = CliRunner().invoke(app, ["check", str(spec)])

    assert result.exit_code == 0, result.output
    assert "repos     nodesource (nodistro, main)" in result.output


def test_the_review_panel_names_the_repository_that_provides_a_package() -> None:
    from io import StringIO

    from rich.console import Console

    from gira_iso.tui.views import show_spec

    buffer = StringIO()
    show_spec(Console(file=buffer, width=120), build_spec(packages=["nodejs"]))

    assert "Repositorios: nodesource (nodistro, main)" in buffer.getvalue()
