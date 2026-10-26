"""Policy rules: a valid spec that must not build."""

from __future__ import annotations

import dataclasses

import pytest
from conftest import VALID_KEY, build_spec

from gira_iso.policy.evaluate import Allowlist, evaluate, load_allowlist


def codes(spec, allowlist: Allowlist) -> tuple[str, ...]:
    return evaluate(spec, allowlist).codes()


def test_known_good_spec_passes(valid_spec, allowlist) -> None:
    verdict = evaluate(valid_spec, allowlist)
    assert verdict.passed, verdict.violations


def test_every_violation_is_reported_not_just_the_first(allowlist) -> None:
    spec = build_spec(
        packages=["nginx", "netcat-openbsd", "telnet"],
        services={"enable": ["nginx", "telnetd"]},
    )
    codes_found = codes(spec, allowlist)
    assert codes_found.count("package.not_allowed") == 2
    assert codes_found.count("service.not_allowed") == 1


def test_package_outside_allowlist_is_rejected(allowlist) -> None:
    spec = build_spec(packages=["netcat-openbsd"])
    assert codes(spec, allowlist) == ("package.not_allowed",)


def test_service_outside_allowlist_is_rejected(allowlist) -> None:
    spec = build_spec(services={"enable": ["telnetd"]})
    assert codes(spec, allowlist) == ("service.not_allowed",)


@pytest.mark.parametrize("service", ["ssh", "qemu-guest-agent"])
def test_required_service_cannot_be_disabled(service: str, allowlist) -> None:
    spec = build_spec(services={"disable": [service]})
    verdict = evaluate(spec, allowlist)
    assert verdict.codes() == ("service.required",)
    assert service in verdict.violations[0].message


def test_disabling_an_unknown_service_is_rejected(allowlist) -> None:
    spec = build_spec(services={"disable": ["totally-not-a-unit"]})
    assert codes(spec, allowlist) == ("service.not_allowed",)


def test_file_outside_allowed_prefix_is_rejected(allowlist) -> None:
    spec = build_spec(files=[{"path": "/usr/local/bin/backdoor", "content": "#!/bin/sh\n"}])
    assert codes(spec, allowlist) == ("file.path.outside_allowlist",)


def test_file_under_forbidden_prefix_is_rejected(allowlist) -> None:
    spec = build_spec(
        files=[{"path": "/etc/sudoers.d/deploy", "content": "deploy ALL=(ALL) NOPASSWD:ALL\n"}]
    )
    assert codes(spec, allowlist) == ("file.path.forbidden",)


def test_exact_match_prefix_does_not_leak_to_siblings(allowlist) -> None:
    """`/etc/motd` is allowed; `/etc/motd.bak` is a different file and must not be."""
    allowed = build_spec(files=[{"path": "/etc/motd", "content": "hello\n"}])
    sibling = build_spec(files=[{"path": "/etc/motd.bak", "content": "hello\n"}])
    assert evaluate(allowed, allowlist).passed
    assert codes(sibling, allowlist) == ("file.path.outside_allowlist",)


def test_directory_prefix_does_not_match_partial_components(allowlist) -> None:
    spec = build_spec(files=[{"path": "/etc/nginx-backdoor/x.conf", "content": "x"}])
    assert codes(spec, allowlist) == ("file.path.outside_allowlist",)


def test_world_readable_root_file_is_flagged(allowlist) -> None:
    spec = build_spec(
        files=[
            {
                "path": "/etc/nginx/secret.conf",
                "content": "x",
                "owner": "root:root",
                "mode": "0644",
            }
        ]
    )
    assert codes(spec, allowlist) == ("file.permissions.world_readable_root",)


def test_unpinned_base_image_is_rejected(valid_spec) -> None:
    unpinned = dataclasses.replace(load_allowlist(), base_images={"ubuntu": {}})
    assert evaluate(valid_spec, unpinned).codes() == ("base.unknown",)


def test_mandatory_packages_are_also_allowed_packages() -> None:
    """A package the build always installs must be a legal spec value too."""
    allowlist = load_allowlist()
    assert allowlist.mandatory_packages <= allowlist.packages
    assert allowlist.required_services <= allowlist.services


def test_allowlist_ships_required_services_and_pinned_base() -> None:
    allowlist = load_allowlist()
    assert "ssh" in allowlist.required_services
    assert "qemu-guest-agent" in allowlist.required_services
    assert "qemu-guest-agent" in allowlist.mandatory_packages
    image = allowlist.base_image("ubuntu", "24.04")
    assert image is not None
    assert len(image["sha256"]) == 64
    assert image["url"].startswith("https://cloud-images.ubuntu.com/")


def test_allowlist_does_not_grant_authorisation_paths() -> None:
    allowlist = load_allowlist()
    for prefix in allowlist.file_prefixes:
        assert not prefix.startswith("/etc/sudoers")
        assert not prefix.startswith("/etc/ssh/")
        assert not prefix.startswith("/root/")
        assert not prefix.startswith("/etc/cron")


def test_example_spec_is_accepted() -> None:
    """The shipped example must stay valid; docs rot otherwise."""
    from pathlib import Path

    import yaml

    from gira_iso.spec.models import Spec

    path = Path(__file__).resolve().parents[1] / "examples" / "nginx-web.yaml"
    spec = Spec.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    assert evaluate(spec, load_allowlist()).passed


def test_valid_key_placeholder_in_example_parses() -> None:
    assert VALID_KEY.startswith("ssh-ed25519 ")


def test_allowlist_can_be_overridden_by_environment(tmp_path, monkeypatch) -> None:
    """An organisation points GIRA_ISO_ALLOWLIST at its own file; no code changes."""
    import yaml

    from gira_iso.policy.evaluate import ENV_VAR, allowlist_digest

    custom = tmp_path / "org-allowlist.yaml"
    data = {
        "base_images": {"ubuntu": {"24.04": {"url": "https://x/y.img", "sha256": "0" * 64}}},
        "packages": ["nginx"],
        "services": ["nginx"],
        "mandatory_packages": [],
        "required_services": [],
        "file_prefixes": ["/etc/nginx/"],
    }
    custom.write_text(yaml.safe_dump(data), encoding="utf-8")
    monkeypatch.setenv(ENV_VAR, str(custom))

    active = load_allowlist()
    assert active.packages == frozenset({"nginx"})
    source, digest = allowlist_digest()
    assert source == str(custom)
    assert len(digest) == 64

    spec = build_spec(packages=["curl"])
    assert evaluate(spec, active).codes() == ("package.not_allowed",)


def test_a_base_version_nobody_pinned_is_rejected_by_the_policy(valid_spec) -> None:
    other = valid_spec.model_copy(
        update={"base": valid_spec.base.model_copy(update={"version": "20.04"})}
    )
    assert evaluate(other, load_allowlist()).codes() == ("base.unknown",)
