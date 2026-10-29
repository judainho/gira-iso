"""Snaps and curated vendor apt repositories.

Every test here pins one claim the feature makes: a snap the policy does not list never
reaches the image, a repository package is installed only behind a verified key, and the
manifest says exactly what came from where.
"""

from __future__ import annotations

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


def test_repository_script_verifies_the_key_before_using_it(tmp_path: Path) -> None:
    spec = build_spec(packages=["docker-ce"])
    render(spec, tmp_path, load_allowlist(), KEY)
    script = (tmp_path / "provision" / "repositories.sh").read_text(encoding="utf-8")

    key_check = script.index("sha256sum -c")
    source_write = script.index("Signed-By:")
    install = script.index("apt-get install")
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
