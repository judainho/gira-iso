"""Canonical form: two specs that mean the same must hash the same."""

from __future__ import annotations

from conftest import build_spec

from vmforge.spec.canonical import canonical_json, spec_hash
from vmforge.spec.models import Spec


def test_hash_is_stable_for_the_same_spec() -> None:
    assert spec_hash(build_spec()) == spec_hash(build_spec())


def test_defaults_are_applied_before_hashing() -> None:
    """Omitting a field and writing its default value must not change the hash."""
    implicit = build_spec()
    explicit = Spec.model_validate(
        {
            **build_spec().model_dump(),
            "spec_version": "0",
            "files": [
                {
                    "path": "/etc/nginx/conf.d/site.conf",
                    "content": "server { listen 8080; }\n",
                    "mode": "0644",
                    "owner": None,
                }
            ],
        }
    )
    assert spec_hash(implicit) == spec_hash(explicit)


def test_different_content_changes_the_hash() -> None:
    left = build_spec(files=[{"path": "/etc/nginx/a.conf", "content": "one"}])
    right = build_spec(files=[{"path": "/etc/nginx/a.conf", "content": "two"}])
    assert spec_hash(left) != spec_hash(right)


def test_different_path_changes_the_hash() -> None:
    left = build_spec(files=[{"path": "/etc/nginx/a.conf", "content": "x"}])
    right = build_spec(files=[{"path": "/etc/nginx/b.conf", "content": "x"}])
    assert spec_hash(left) != spec_hash(right)


def test_canonical_json_is_compact_and_key_sorted() -> None:
    text = canonical_json(build_spec())
    assert '": ' not in text and '", "' not in text
    assert text.startswith('{"base"')
    assert '"spec_version":"0"' in text
