from __future__ import annotations

from pathlib import Path

import pytest

from gira_iso.bases import UserBaseError, list_user_bases, register_base
from gira_iso.builder.base import BaseImageError, ensure_base_image
from gira_iso.policy.evaluate import load_allowlist


def qcow2(path: Path) -> Path:
    path.write_bytes(b"QFI\xfb" + b"\0" * 4096)
    return path


def iso(path: Path) -> Path:
    path.write_bytes(b"\0" * 0x8001 + b"CD001" + b"\0" * 64)
    return path


def test_registers_a_qcow2_and_lists_it(tmp_path: Path) -> None:
    base = register_base(qcow2(tmp_path / "jammy.img"), "22.04")

    assert base.version == "22.04"
    assert [b.sha256 for b in list_user_bases()] == [base.sha256]


def test_an_installer_iso_is_refused_with_a_useful_message(tmp_path: Path) -> None:
    with pytest.raises(UserBaseError, match="installer ISO"):
        register_base(iso(tmp_path / "live.iso"), "26.10")
    assert list_user_bases() == []


def test_a_non_qcow2_file_is_refused(tmp_path: Path) -> None:
    junk = tmp_path / "junk.img"
    junk.write_bytes(b"not an image")
    with pytest.raises(UserBaseError, match="not a qcow2"):
        register_base(junk, "22.04")


def test_a_malformed_version_is_refused(tmp_path: Path) -> None:
    with pytest.raises(UserBaseError, match="invalid version"):
        register_base(qcow2(tmp_path / "a.img"), "latest")


def test_a_pinned_version_cannot_be_shadowed(tmp_path: Path) -> None:
    builtin = load_allowlist().base_images
    with pytest.raises(UserBaseError, match="already registered"):
        register_base(qcow2(tmp_path / "a.img"), "24.04", builtin=builtin)


def test_a_version_cannot_be_registered_twice(tmp_path: Path) -> None:
    register_base(qcow2(tmp_path / "a.img"), "22.04")
    with pytest.raises(UserBaseError, match="already registered"):
        register_base(qcow2(tmp_path / "b.img"), "22.04")


def test_the_allowlist_includes_registered_bases_and_keeps_the_pinned_one(tmp_path: Path) -> None:
    base = register_base(qcow2(tmp_path / "a.img"), "22.04")

    allowlist = load_allowlist()

    assert allowlist.base_image("ubuntu", "22.04") == {"url": base.url, "sha256": base.sha256}
    assert allowlist.base_image("ubuntu", "24.04") is not None


def test_a_registered_base_is_verified_in_place(tmp_path: Path) -> None:
    base = register_base(qcow2(tmp_path / "a.img"), "22.04")

    path, sha = ensure_base_image("ubuntu", "22.04", load_allowlist())

    assert path == base.path
    assert sha == base.sha256


def test_a_tampered_registered_base_is_refused(tmp_path: Path) -> None:
    base = register_base(qcow2(tmp_path / "a.img"), "22.04")
    base.path.write_bytes(b"QFI\xfb tampered")

    with pytest.raises(BaseImageError, match="does not match its checksum"):
        ensure_base_image("ubuntu", "22.04", load_allowlist())
