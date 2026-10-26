"""The one rule for image names.

The name becomes a file name (`<name>.qcow2`, `<name>.manifest.json`) and a lookup key, so it
stays within a conservative alphabet. The command line and the menu used to disagree on the
maximum length (64 vs 63); this module is now the only definition.
"""

from __future__ import annotations

import re

from gira_iso.errors import EXIT_INVALID, GiraError

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
NAME_HINT = "use letters, digits, '.', '_' or '-'"


class InvalidImageName(GiraError):
    exit_code = EXIT_INVALID


def is_valid_image_name(value: str) -> bool:
    return bool(NAME_RE.match(value))


def checked_name(value: str) -> str:
    """Return `value` unchanged, or raise with a message that says what is allowed."""
    if not is_valid_image_name(value):
        raise InvalidImageName(f"invalid image name {value!r}: {NAME_HINT}")
    return value
