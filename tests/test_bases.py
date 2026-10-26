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
    base = register_base(qcow2(tmp_path / "jammy.img"), "20.04")

    assert base.version == "20.04"
    assert [b.sha256 for b in list_user_bases()] == [base.sha256]


def test_an_installer_iso_is_refused_with_a_useful_message(tmp_path: Path) -> None:
    with pytest.raises(UserBaseError, match="installer ISO"):
        register_base(iso(tmp_path / "live.iso"), "26.10")
    assert list_user_bases() == []


def test_a_non_qcow2_file_is_refused(tmp_path: Path) -> None:
    junk = tmp_path / "junk.img"
    junk.write_bytes(b"not an image")
    with pytest.raises(UserBaseError, match="not a qcow2"):
        register_base(junk, "20.04")


def test_a_malformed_version_is_refused(tmp_path: Path) -> None:
    with pytest.raises(UserBaseError, match="invalid version"):
        register_base(qcow2(tmp_path / "a.img"), "latest")


def test_a_pinned_version_cannot_be_shadowed(tmp_path: Path) -> None:
    builtin = load_allowlist().base_images
    with pytest.raises(UserBaseError, match="already registered"):
        register_base(qcow2(tmp_path / "a.img"), "24.04", builtin=builtin)


def test_a_version_cannot_be_registered_twice(tmp_path: Path) -> None:
    register_base(qcow2(tmp_path / "a.img"), "20.04")
    with pytest.raises(UserBaseError, match="already registered"):
        register_base(qcow2(tmp_path / "b.img"), "20.04")


def test_the_allowlist_includes_registered_bases_and_keeps_the_pinned_one(tmp_path: Path) -> None:
    base = register_base(qcow2(tmp_path / "a.img"), "20.04")

    allowlist = load_allowlist()

    assert allowlist.base_image("ubuntu", "20.04") == {"url": base.url, "sha256": base.sha256}
    assert allowlist.base_image("ubuntu", "24.04") is not None


def test_a_registered_base_is_verified_in_place(tmp_path: Path) -> None:
    base = register_base(qcow2(tmp_path / "a.img"), "20.04")

    path, sha = ensure_base_image("ubuntu", "20.04", load_allowlist())

    assert path == base.path
    assert sha == base.sha256


def test_a_tampered_registered_base_is_refused(tmp_path: Path) -> None:
    base = register_base(qcow2(tmp_path / "a.img"), "20.04")
    base.path.write_bytes(b"QFI\xfb tampered")

    with pytest.raises(BaseImageError, match="does not match its checksum"):
        ensure_base_image("ubuntu", "20.04", load_allowlist())


# --- official downloads ---------------------------------------------------------------


class FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self.headers = {"Content-Length": str(len(payload))}
        self._offset = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, size: int) -> bytes:
        chunk = self._payload[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk


def _official(sha: str):
    from gira_iso.policy.evaluate import Allowlist

    return Allowlist.from_mapping(
        {
            "base_images": {"ubuntu": {"99.04": {"url": "https://x/base.img", "sha256": sha}}},
            "packages": [],
            "services": [],
        }
    )


def test_download_verifies_the_checksum_and_reports_progress(monkeypatch, tmp_path: Path) -> None:
    import hashlib
    import urllib.request

    import gira_iso.builder.base as base

    payload = b"x" * (2 * 1024 * 1024 + 5)
    monkeypatch.setattr(base, "cache_dir", lambda: tmp_path / "cache")
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=None: FakeResponse(payload))
    seen: list[tuple[int, int]] = []

    path, sha = ensure_base_image(
        "ubuntu",
        "99.04",
        _official(hashlib.sha256(payload).hexdigest()),
        on_progress=lambda done, total: seen.append((done, total)),
    )

    assert path.read_bytes() == payload
    assert seen[-1] == (len(payload), len(payload))
    assert not path.with_suffix(path.suffix + ".part").exists()


def test_download_with_a_wrong_checksum_leaves_nothing_behind(monkeypatch, tmp_path: Path) -> None:
    import urllib.request

    import gira_iso.builder.base as base

    monkeypatch.setattr(base, "cache_dir", lambda: tmp_path / "cache")
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda url, timeout=None: FakeResponse(b"tampered")
    )

    with pytest.raises(BaseImageError, match="checksum"):
        ensure_base_image("ubuntu", "99.04", _official("0" * 64))

    assert list((tmp_path / "cache").iterdir()) == []


def test_both_official_bases_are_pinned_by_immutable_urls() -> None:
    allowlist = load_allowlist()

    jammy = allowlist.base_image("ubuntu", "22.04")
    assert jammy is not None
    assert "/release-" in jammy["url"]  # a dated directory, not the moving `release/` alias
    assert len(jammy["sha256"]) == 64
    assert allowlist.base_image("ubuntu", "24.04") is not None
