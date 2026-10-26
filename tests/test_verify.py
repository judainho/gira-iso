"""Verification: derived assertions, manifest integrity and honest scope."""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from conftest import VALID_KEY, build_spec

from gira_iso.builder.manifest import MANIFEST_NAME
from gira_iso.verify.goss import build_goss, file_sha256, to_yaml
from gira_iso.verify.verify import VerificationError, verify_build


def test_packages_become_installed_assertions() -> None:
    document = build_goss(build_spec(packages=["nginx"]), ["curl", "nginx", "qemu-guest-agent"])
    assert document["package"] == {
        "curl": {"installed": True},
        "nginx": {"installed": True},
        "qemu-guest-agent": {"installed": True},
    }


def test_enabled_services_must_be_enabled_and_running() -> None:
    spec = build_spec(services={"enable": ["nginx"]})
    document = build_goss(spec, [])
    assert document["service"]["nginx"] == {"enabled": True, "running": True}


def test_disabled_services_are_not_asserted_running() -> None:
    spec = build_spec(services={"disable": ["redis-server"]})
    assert "service" not in build_goss(spec, [])


def test_users_and_their_keys_are_asserted() -> None:
    spec = build_spec(users=[{"name": "deploy", "sudo": True, "ssh_authorized_keys": [VALID_KEY]}])
    document = build_goss(spec, [])
    assert document["user"]["deploy"] == {"exists": True}
    keys = document["file"]["/home/deploy/.ssh/authorized_keys"]
    assert keys["exists"] is True
    assert keys["owner"] == "deploy"


def test_sudo_membership_is_asserted_by_command() -> None:
    spec = build_spec(users=[{"name": "deploy", "sudo": True, "ssh_authorized_keys": [VALID_KEY]}])
    document = build_goss(spec, [])
    assert document["command"]["sudo-deploy"]["stdout"] == ["sudo"]


def test_non_sudo_user_has_no_sudo_assertion() -> None:
    spec = build_spec(users=[{"name": "readonly", "ssh_authorized_keys": [VALID_KEY]}])
    assert "sudo-readonly" not in build_goss(spec, [])["command"]


def test_file_content_is_asserted_by_hash_not_by_regex() -> None:
    content = "server {\n  listen 8080;\n}\n"
    spec = build_spec(files=[{"path": "/etc/nginx/conf.d/x.conf", "content": content}])
    entry = build_goss(spec, [])["file"]["/etc/nginx/conf.d/x.conf"]
    assert entry["sha256"] == file_sha256(content)
    assert entry["mode"] == "0644"
    assert "contents" not in entry


def test_no_port_assertions_are_invented() -> None:
    """The spec never declares ports, so verification must not guess one."""
    document = build_goss(build_spec(packages=["nginx"], services={"enable": ["nginx"]}), ["nginx"])
    assert "port" not in document


def test_goss_yaml_round_trips() -> None:
    document = yaml.safe_load(to_yaml(build_spec(packages=["nginx"]), ["nginx"]))
    assert document["package"]["nginx"]["installed"] is True


def _write_build(tmp_path: Path, manifest_dump: dict) -> Path:
    build = tmp_path / "build"
    (build / "image").mkdir(parents=True)
    (build / "image" / "gira-iso-ubuntu-24.04.qcow2").write_bytes(b"not really an image")
    (build / MANIFEST_NAME).write_text(json.dumps(manifest_dump), encoding="utf-8")
    return build


def test_checksum_mismatch_stops_before_booting(tmp_path: Path) -> None:
    """A changed artifact must fail without spending 2 minutes booting it."""
    build = _write_build(
        tmp_path,
        {
            "spec_sha256": "0" * 64,
            "spec": {"packages": ["nginx"]},
            "base": {
                "os": "ubuntu",
                "version": "24.04",
                "url": "https://x/y.img",
                "sha256": "1" * 64,
            },
            "policy": {"source": "packaged:allowlist.yaml", "sha256": "4" * 64},
            "builder": {
                "packer_version": "1.16.2",
                "plugin_version": "1.1.7",
                "template_sha256": "2" * 64,
            },
            "packages": ["nginx"],
            "services": [],
            "artifact": {
                "file": "gira-iso-ubuntu-24.04.qcow2",
                "sha256": "3" * 64,
                "size_bytes": 4,
                "virtual_size_bytes": 8,
            },
        },
    )
    result = verify_build(build)
    assert not result.passed
    assert result.checks[0].name == "artifact checksum"
    assert len(result.checks) == 1


def test_missing_manifest_is_a_clear_error(tmp_path: Path) -> None:
    try:
        verify_build(tmp_path)
    except VerificationError as exc:
        assert "no manifest" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected VerificationError")


def test_goss_summary_parser_uses_the_real_key_names() -> None:
    """goss reports `test-count`, not `tested-count`; a typo here reports 0 tests."""
    from gira_iso.verify.verify import _goss_summary

    report = json.dumps(
        {
            "results": [
                {
                    "resource-type": "Package",
                    "resource-id": "nginx",
                    "property": "installed",
                    "successful": False,
                }
            ],
            "summary": {"failed-count": 1, "skipped-count": 0, "test-count": 20},
        }
    )
    passed, detail = _goss_summary(report)
    assert passed is False
    assert "1 of 20 assertions failed" in detail
    assert "Package/nginx" in detail


def test_goss_summary_parser_reports_success() -> None:
    from gira_iso.verify.verify import _goss_summary

    passed, detail = _goss_summary(
        json.dumps({"results": [], "summary": {"failed-count": 0, "test-count": 7}})
    )
    assert passed is True
    assert detail == "7 assertions passed"
