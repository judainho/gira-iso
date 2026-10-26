"""Builder: rendering determinism, seed fidelity and the policy gate."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from conftest import build_spec

from gira_iso.builder.build import BuildRejected, build_image
from gira_iso.builder.render import (
    PLUGIN_VERSION,
    cloud_init,
    disk_size_mb,
    effective_packages,
    effective_services,
    render,
)
from gira_iso.policy.evaluate import load_allowlist
from gira_iso.spec.models import Spec

MANDATORY = load_allowlist().mandatory_packages
KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAABAgMEBQYHCAkKCwwNDg8QERITFBUWFxgZGhscHR4f"


def test_rendering_the_same_spec_twice_is_byte_identical(tmp_path: Path) -> None:
    spec = build_spec(packages=["nginx", "curl"], services={"enable": ["nginx"]})
    first = render(spec, tmp_path / "a", MANDATORY, KEY)
    second = render(spec, tmp_path / "b", MANDATORY, KEY)

    for relative in ("packer/image.pkr.hcl", "provision/assert.sh", "provision/cleanup.sh"):
        assert (first.packer_dir.parent / relative).read_bytes() == (
            second.packer_dir.parent / relative
        ).read_bytes(), relative
    assert first.template_sha256 == second.template_sha256


def test_rendered_hcl_keeps_packer_templating_intact(tmp_path: Path) -> None:
    """Jinja and Packer both use {{ }}; the raw blocks must survive rendering."""
    render(build_spec(), tmp_path, MANDATORY, KEY)
    text = (tmp_path / "packer" / "image.pkr.hcl").read_text(encoding="utf-8")
    assert "{{ .Vars }}" in text
    assert "{{ .Path }}" in text
    assert f'version = "= {PLUGIN_VERSION}"' in text
    assert "disk_size        = 8192" in text


def test_disk_size_is_converted_to_megabytes(tmp_path: Path) -> None:
    assert disk_size_mb("8G") == 8192
    assert disk_size_mb("256G") == 262144


def test_mandatory_packages_are_always_installed() -> None:
    spec = build_spec(packages=["nginx"])
    packages = effective_packages(spec, MANDATORY)
    assert "nginx" in packages
    assert "qemu-guest-agent" in packages
    assert packages == sorted(packages)


def test_services_are_deduplicated_and_sorted() -> None:
    spec = build_spec(services={"enable": ["nginx", "nginx"]})
    assert effective_services(spec) == ["nginx"]


def test_file_content_survives_the_seed_exactly() -> None:
    """Trailing spaces and blank lines inside a config file must not be reflowed."""
    content = "server {\n    listen 8080;  \n\n    # trailing spaces above\n}\n"
    spec = build_spec(files=[{"path": "/etc/nginx/conf.d/x.conf", "content": content}])
    user_data, _ = cloud_init(spec, KEY, MANDATORY)
    parsed = yaml.safe_load(user_data.split("\n", 1)[1])
    assert parsed["write_files"][0]["content"] == content
    assert parsed["write_files"][0]["permissions"] == "0644"


def test_seed_contains_build_user_and_spec_users() -> None:
    spec = build_spec(
        users=[
            {"name": "deploy", "sudo": True, "ssh_authorized_keys": [KEY]},
            {"name": "readonly", "ssh_authorized_keys": [KEY]},
        ]
    )
    user_data, _ = cloud_init(spec, KEY, MANDATORY)
    parsed = yaml.safe_load(user_data.split("\n", 1)[1])
    names = [user["name"] for user in parsed["users"]]
    assert names == ["packer", "deploy", "readonly"]
    deploy = parsed["users"][1]
    assert deploy["sudo"] == "ALL=(ALL) NOPASSWD:ALL"
    readonly = parsed["users"][2]
    assert "sudo" not in readonly


def test_seed_enables_declared_services() -> None:
    spec = build_spec(services={"enable": ["nginx"]})
    user_data, _ = cloud_init(spec, KEY, MANDATORY)
    parsed = yaml.safe_load(user_data.split("\n", 1)[1])
    assert "systemctl enable nginx" in parsed["runcmd"]


def test_seed_never_contains_a_password() -> None:
    user_data, _ = cloud_init(build_spec(), KEY, MANDATORY)
    assert "password:" not in user_data
    assert "ssh_pwauth: false" in user_data


def test_assertions_script_names_every_package(tmp_path: Path) -> None:
    render(build_spec(packages=["nginx", "curl"]), tmp_path, MANDATORY, KEY)
    script = (tmp_path / "provision" / "assert.sh").read_text(encoding="utf-8")
    for package in ("nginx", "curl", "qemu-guest-agent", "cloud-init"):
        assert f"dpkg -l '{package}'" in script


def test_build_rejects_a_hostile_spec_before_touching_disk(tmp_path: Path) -> None:
    spec = build_spec(files=[{"path": "/etc/sudoers.d/deploy", "content": "x"}])
    with pytest.raises(BuildRejected) as raised:
        build_image(spec, work_root=tmp_path)
    assert raised.value.verdict.codes() == ("file.path.forbidden",)
    assert list(tmp_path.iterdir()) == []


def test_rejected_spec_does_not_download_the_base(tmp_path: Path) -> None:
    """Policy runs before ensure_base_image, so a rejection costs nothing."""
    spec = Spec.model_validate({"packages": ["netcat-openbsd"]})
    with pytest.raises(BuildRejected):
        build_image(spec, work_root=tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_ctrl_c_terminates_packer_and_reraises(monkeypatch, tmp_path) -> None:
    import subprocess

    from gira_iso.builder import packer

    class Stdout:
        def __iter__(self):
            raise KeyboardInterrupt

    class FakeProcess:
        stdout = Stdout()
        terminated = False

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            return -15

        def kill(self):
            raise AssertionError("must not kill a process that exited in time")

    process = FakeProcess()
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)

    try:
        packer._run(["packer", "build"], tmp_path / "log", stream=True)
    except KeyboardInterrupt:
        pass
    else:
        raise AssertionError("KeyboardInterrupt must propagate")

    assert process.terminated
