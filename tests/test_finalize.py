"""C1: the build user must not survive into the image, and a failed removal must fail the build."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from gira_iso.builder.build import _generate_key
from gira_iso.builder.render import (
    BUILD_KEY_MARKER,
    BUILD_USER,
    finalize_context,
    render,
    render_text,
)
from gira_iso.policy.evaluate import RESERVED_USERS, evaluate, load_allowlist
from gira_iso.spec.models import Spec
from gira_iso.verify.goss import build_goss
from gira_iso.verify.vm import VERIFY_USER


def valid_spec(**extra) -> Spec:
    return Spec.model_validate({"packages": ["nginx"], **extra})


# --- what gets rendered ----------------------------------------------------------------


def test_the_build_renders_a_finalize_script_and_uses_it_to_shut_down(tmp_path: Path) -> None:
    rendered = render(valid_spec(), tmp_path, frozenset({"openssh-server"}), "ssh-ed25519 AAAA k")

    template = (rendered.packer_dir / "image.pkr.hcl").read_text(encoding="utf-8")
    assert (rendered.provision_dir / "finalize.sh").is_file()
    assert 'shutdown_command = "sudo bash /tmp/gira-finalize.sh"' in template
    assert 'destination = "/tmp/gira-finalize.sh"' in template
    assert "sudo shutdown -P now" not in template  # a plain shutdown would keep the user


def test_the_finalize_script_is_valid_bash() -> None:
    script = render_text("scripts/finalize.sh.j2", finalize_context())

    subprocess.run(["bash", "-n"], input=script, text=True, check=True)
    assert "userdel --remove --force 'packer'" not in script  # the name goes through a variable
    assert "BUILD_USER='packer'" in script


def test_the_key_carries_the_marker_the_script_searches_for(tmp_path: Path) -> None:
    public = _generate_key(tmp_path / "id_ed25519")

    assert public.endswith(BUILD_KEY_MARKER)


def test_the_names_reserved_by_the_policy_are_the_ones_the_tool_really_uses() -> None:
    assert {"root", BUILD_USER, VERIFY_USER} == RESERVED_USERS


# --- running the script against a fake system ---------------------------------------------

SSH_KEY = f"ssh-ed25519 AAAAC3Nz {BUILD_KEY_MARKER}"


@pytest.fixture
def machine(tmp_path: Path):
    """A fake root: passwd/group files, a home, a sudoers file, and shims for the system tools."""
    root = tmp_path
    (root / "etc/sudoers.d").mkdir(parents=True)
    (root / "home" / BUILD_USER / ".ssh").mkdir(parents=True)
    (root / "home" / BUILD_USER / ".ssh/authorized_keys").write_text(SSH_KEY + "\n")
    (root / "home/deploy/.ssh").mkdir(parents=True)
    (root / "home/deploy/.ssh/authorized_keys").write_text("ssh-ed25519 AAAAC3Nz deploy@laptop\n")
    (root / "etc/sudoers.d/90-cloud-init-users").write_text(
        f"{BUILD_USER} ALL=(ALL) NOPASSWD:ALL\ndeploy ALL=(ALL) NOPASSWD:ALL\n"
    )
    (root / "passwd").write_text(f"{BUILD_USER}\ndeploy\n")
    (root / "group").write_text(f"{BUILD_USER}\ndeploy\n")

    bin_dir = root / "bin"
    bin_dir.mkdir()
    (bin_dir / "getent").write_text(
        '#!/usr/bin/env bash\ngrep -qx "$2" "$FAKE_ROOT/$1" 2>/dev/null\n'
    )
    # userdel removes the account only when FAKE_USERDEL=ok, and the home only when =ok too.
    (bin_dir / "userdel").write_text(
        "#!/usr/bin/env bash\n"
        'user="${@: -1}"\n'
        'case "$FAKE_USERDEL" in\n'
        '  ok) sed -i "/^$user$/d" "$FAKE_ROOT/passwd" "$FAKE_ROOT/group"\n'
        '      rm -rf "$FAKE_ROOT/home/$user" ;;\n'
        '  keeps_home) sed -i "/^$user$/d" "$FAKE_ROOT/passwd" "$FAKE_ROOT/group" ;;\n'
        "esac\n"
    )
    (bin_dir / "shutdown").write_text('#!/usr/bin/env bash\ntouch "$FAKE_ROOT/shutdown-called"\n')
    for shim in bin_dir.iterdir():
        shim.chmod(0o755)

    script = root / "finalize.sh"
    script.write_text(
        render_text(
            "scripts/finalize.sh.j2",
            finalize_context(
                sudoers_dir=str(root / "etc/sudoers.d"),
                sudoers_file=str(root / "etc/sudoers.d/90-cloud-init-users"),
                home_root=str(root / "home"),
            ),
        )
    )

    def run(userdel: str) -> subprocess.CompletedProcess:
        env = dict(
            os.environ,
            PATH=f"{bin_dir}:{os.environ['PATH']}",
            FAKE_ROOT=str(root),
            FAKE_USERDEL=userdel,
        )
        return subprocess.run(
            ["bash", str(script)], env=env, capture_output=True, text=True, check=False
        )

    run.root = root
    run.script = script
    return run


def test_a_successful_removal_powers_off_and_keeps_the_other_users(machine) -> None:
    result = machine("ok")

    assert result.returncode == 0, result.stderr
    assert (machine.root / "shutdown-called").exists()
    sudoers = (machine.root / "etc/sudoers.d/90-cloud-init-users").read_text()
    assert BUILD_USER not in sudoers
    assert "deploy ALL=(ALL) NOPASSWD:ALL" in sudoers  # the spec's own user keeps its rule
    assert (machine.root / "home/deploy/.ssh/authorized_keys").exists()
    assert not machine.script.exists()  # the script deletes itself


def test_if_the_user_is_still_there_the_machine_does_not_power_off(machine) -> None:
    result = machine("nothing")

    assert result.returncode == 1
    assert "FINALIZE FAILED: user packer still exists" in result.stderr
    assert not (machine.root / "shutdown-called").exists()


def test_a_home_directory_that_survives_fails_the_build(machine) -> None:
    result = machine("keeps_home")

    assert result.returncode == 1
    assert "still exists" in result.stderr
    assert not (machine.root / "shutdown-called").exists()


def test_a_sudo_rule_left_in_another_file_fails_the_build(machine) -> None:
    (machine.root / "etc/sudoers.d/elsewhere").write_text(f"{BUILD_USER} ALL=(ALL) ALL\n")

    result = machine("ok")

    assert result.returncode == 1
    assert "a sudo rule for packer remains" in result.stderr
    assert not (machine.root / "shutdown-called").exists()


def test_the_build_key_found_anywhere_in_the_image_fails_the_build(machine) -> None:
    (machine.root / "home/deploy/.ssh/authorized_keys").write_text(SSH_KEY + "\n")

    result = machine("ok")

    assert result.returncode == 1
    assert "the build key is still present" in result.stderr
    assert not (machine.root / "shutdown-called").exists()


# --- the verifier and the policy ---------------------------------------------------------


def test_the_verification_proves_the_build_user_is_gone() -> None:
    document = build_goss(valid_spec(), ["nginx"])

    assert document["user"][BUILD_USER] == {"exists": False}
    assert document["file"][f"/home/{BUILD_USER}"] == {"exists": False}
    assert "no-build-user-sudo" in document["command"]
    assert "sudoers" in document["command"]["no-build-user-sudo"]["exec"]


@pytest.mark.parametrize("name", sorted(RESERVED_USERS))
def test_a_spec_cannot_use_a_name_the_tool_reserves(name: str) -> None:
    spec = valid_spec(
        users=[{"name": name, "sudo": False, "ssh_authorized_keys": [_ed25519_key()]}]
    )

    verdict = evaluate(spec, load_allowlist())

    assert "user.reserved" in verdict.codes()


def test_an_ordinary_user_is_not_reserved() -> None:
    spec = valid_spec(
        users=[{"name": "deploy", "sudo": True, "ssh_authorized_keys": [_ed25519_key()]}]
    )

    assert "user.reserved" not in evaluate(spec, load_allowlist()).codes()


def _ed25519_key() -> str:
    # A well-formed key body: the schema checks the shape, not whose key it is.
    import base64
    import struct

    kind = b"ssh-ed25519"
    blob = struct.pack(">I", len(kind)) + kind + struct.pack(">I", 32) + bytes(range(32))
    return f"ssh-ed25519 {base64.b64encode(blob).decode()}"
