"""The spec is the only contract between the LLM and the build engine.

Shape rules live here: types, bounds, formats. Anything that depends on what the
organisation allows (which packages, which paths, which base image) belongs to the
policy layer, not to this module.

Everything is `extra="forbid"`: a model that invents a field gets rejected instead
of silently having it ignored.
"""

from __future__ import annotations

import base64
import binascii
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

# --- bounded primitives ---------------------------------------------------

PackageName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9+.-]{0,63}$")]
ServiceName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9@._-]{0,63}$")]
UserName = Annotated[str, StringConstraints(pattern=r"^[a-z_][a-z0-9_-]{0,31}$")]
FileMode = Annotated[str, StringConstraints(pattern=r"^0[0-7]{3}$")]
Owner = Annotated[
    str, StringConstraints(pattern=r"^[a-z_][a-z0-9_-]{0,31}(:[a-z_][a-z0-9_-]{0,31})?$")
]

MAX_FILE_BYTES = 32 * 1024
MAX_TOTAL_FILE_BYTES = 128 * 1024


def _ed25519_error(key: str) -> str | None:
    """Return a reason when `key` is not a bare, well-formed ed25519 public key."""
    parts = key.split()
    if len(parts) != 2:
        # Three or more parts means the line carries options or a comment; both are
        # the classic way to smuggle `command="..."` into authorized_keys.
        return "must be exactly '<type> <base64>' with no options and no comment"
    if parts[0] != "ssh-ed25519":
        return "only ssh-ed25519 keys are accepted in v0"
    try:
        raw = base64.b64decode(parts[1], validate=True)
    except (binascii.Error, ValueError):
        return "public key body is not valid base64"
    # An authorized_keys body is the SSH wire blob, not the raw 32 key bytes:
    # uint32 len("ssh-ed25519") + "ssh-ed25519" + uint32 32 + 32 bytes, total 51.
    expected = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20"
    if len(raw) != 51 or not raw.startswith(expected):
        return "public key body is not a well-formed ed25519 blob"
    return None


class FileEntry(BaseModel):
    """A configuration file written into the image.

    Content is arbitrary, so the path is the security boundary. The policy layer
    decides which prefixes are writable; this model only enforces that the path is
    absolute, normalised, and non-empty of traversal.
    """

    model_config = ConfigDict(extra="forbid")

    path: Annotated[str, StringConstraints(min_length=2, max_length=255)]
    content: Annotated[str, StringConstraints(max_length=MAX_FILE_BYTES)]
    mode: FileMode = "0644"
    owner: Owner | None = None

    @field_validator("path")
    @classmethod
    def _absolute_and_normalised(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("path must be absolute")
        if value.endswith("/"):
            raise ValueError("path must name a file, not a directory")
        if "//" in value or "\x00" in value:
            raise ValueError("path must not contain empty segments or NUL bytes")
        if any(segment in {".", ".."} for segment in value.split("/")):
            raise ValueError("path must not contain '.' or '..' segments")
        return value

    @field_validator("content")
    @classmethod
    def _no_nul(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("content must not contain NUL bytes")
        return value


class User(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: UserName
    sudo: bool = False
    ssh_authorized_keys: Annotated[list[str], Field(min_length=1, max_length=5)]

    @field_validator("ssh_authorized_keys")
    @classmethod
    def _keys_are_bare_ed25519(cls, value: list[str]) -> list[str]:
        for key in value:
            reason = _ed25519_error(key.strip())
            if reason is not None:
                raise ValueError(f"invalid ssh key: {reason}")
        return [key.strip() for key in value]


class Services(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enable: Annotated[list[ServiceName], Field(max_length=20)] = Field(default_factory=list)
    disable: Annotated[list[ServiceName], Field(max_length=20)] = Field(default_factory=list)


class BaseImage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    os: Literal["ubuntu"] = "ubuntu"
    version: Literal["24.04"] = "24.04"


class Spec(BaseModel):
    """A complete, declarative image description."""

    model_config = ConfigDict(extra="forbid")

    spec_version: Literal["0"] = "0"
    base: BaseImage = Field(default_factory=BaseImage)
    disk_size: Annotated[str, StringConstraints(pattern=r"^[0-9]{1,3}G$")] = "8G"
    packages: Annotated[list[PackageName], Field(max_length=40)] = Field(default_factory=list)
    services: Services = Field(default_factory=Services)
    users: Annotated[list[User], Field(max_length=10)] = Field(default_factory=list)
    files: Annotated[list[FileEntry], Field(max_length=20)] = Field(default_factory=list)

    @field_validator("disk_size")
    @classmethod
    def _disk_in_range(cls, value: str) -> str:
        size = int(value.removesuffix("G"))
        if size < 6:
            raise ValueError(
                "disk_size must be at least 6G: the cloud image does not fit below that"
            )
        if size > 256:
            raise ValueError("disk_size must not exceed 256G")
        return value

    @model_validator(mode="after")
    def _no_duplicates_and_bounded_content(self) -> Spec:
        duplicates = sorted({item for item in self.packages if self.packages.count(item) > 1})
        if duplicates:
            raise ValueError(f"duplicate packages: {', '.join(duplicates)}")

        names = [user.name for user in self.users]
        repeated_users = sorted({name for name in names if names.count(name) > 1})
        if repeated_users:
            raise ValueError(f"duplicate users: {', '.join(repeated_users)}")

        paths = [entry.path for entry in self.files]
        repeated_paths = sorted({path for path in paths if paths.count(path) > 1})
        if repeated_paths:
            raise ValueError(f"duplicate file paths: {', '.join(repeated_paths)}")

        total = sum(len(entry.content.encode()) for entry in self.files)
        if total > MAX_TOTAL_FILE_BYTES:
            raise ValueError(
                f"total file content is {total} bytes; limit is {MAX_TOTAL_FILE_BYTES}"
            )

        both = sorted(set(self.services.enable) & set(self.services.disable))
        if both:
            raise ValueError(f"services both enabled and disabled: {', '.join(both)}")

        return self
