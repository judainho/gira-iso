"""Render a spec into the files Packer consumes.

Only the HCL and shell scripts go through Jinja. The cloud-init seed is built as a
Python dict and dumped as YAML: file contents are arbitrary text, and hand-rolled
template indentation silently changes them (trailing spaces inside a block scalar,
for example). Dumping a dict means the content is escaped by the YAML library, not
by string concatenation.
"""

from __future__ import annotations

import hashlib
import stat
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, StrictUndefined

from gira_iso.policy.evaluate import Allowlist
from gira_iso.releases import UnknownRelease, codename_for
from gira_iso.spec.canonical import spec_hash
from gira_iso.spec.models import Spec

PLUGIN_VERSION = "1.1.7"
IMAGE_NAME = "gira-iso-ubuntu-24.04.qcow2"
BUILD_HOSTNAME = "gira-iso-build"
BUILD_USER = "packer"
BUILD_INSTANCE_ID = "gira-iso-build-001"


@dataclass(frozen=True)
class Rendered:
    packer_dir: Path
    seed_dir: Path
    provision_dir: Path
    template_sha256: str


def _environment() -> Environment:
    return Environment(
        undefined=StrictUndefined,
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )


def _template_text(name: str) -> str:
    return resources.files("gira_iso.templates").joinpath(name).read_text("utf-8")


def render_text(name: str, context: dict[str, Any]) -> str:
    return _environment().from_string(_template_text(name)).render(**context)


# The comment ssh-keygen puts in the build key (see builder/build.py); the finalize script
# searches the image for it to prove the key did not survive.
BUILD_KEY_MARKER = "gira-iso-build"


def finalize_context(**overrides: str) -> dict[str, str]:
    """Values for scripts/finalize.sh.j2. The defaults are the real paths; tests override them."""
    context = {
        "build_user": BUILD_USER,
        "sudoers_dir": "/etc/sudoers.d",
        "sudoers_file": "/etc/sudoers.d/90-cloud-init-users",
        "home_root": "/home",
        "key_marker": BUILD_KEY_MARKER,
        "poweroff": "shutdown -P now",
    }
    context.update(overrides)
    return context


def disk_size_mb(disk_size: str) -> int:
    return int(disk_size.removesuffix("G")) * 1024


def archive_packages(spec: Spec, allowlist: Allowlist) -> list[str]:
    """Packages cloud-init may install: only the ones Ubuntu's own archive provides.

    cloud-init runs before any vendor repository exists, so asking it for `docker-ce` would
    fail the build with a confusing "unable to locate package".
    """
    return sorted(
        package for package in set(spec.packages) if allowlist.repository_of(package) is None
    )


def repository_packages(spec: Spec, allowlist: Allowlist) -> dict[str, list[str]]:
    """Packages grouped by the vendor repository that provides them."""
    grouped: dict[str, list[str]] = {}
    for package in sorted(set(spec.packages)):
        repository = allowlist.repository_of(package)
        if repository is not None:
            grouped.setdefault(repository, []).append(package)
    return grouped


def cloud_init_packages(spec: Spec, allowlist: Allowlist) -> list[str]:
    """Exactly what cloud-init installs: the archive packages plus the mandatory ones.

    One function owns this list. Everything else is derived from it, so there is a single place
    that answers "what does cloud-init receive?".
    """
    return sorted(set(archive_packages(spec, allowlist)) | set(allowlist.mandatory_packages))


def effective_packages(spec: Spec, allowlist: Allowlist) -> list[str]:
    """Every package that ends up installed, whatever source it came from.

    This is what the manifest records and what the verifier asserts. Derived from the two
    install paths, so adding a source cannot leave one of them stale.
    """
    from_repositories = {
        package
        for packages in repository_packages(spec, allowlist).values()
        for package in packages
    }
    return sorted(set(cloud_init_packages(spec, allowlist)) | from_repositories)


@dataclass(frozen=True)
class ResolvedRepository:
    """A policy repository with its suite resolved for the build's Ubuntu release."""

    id: str
    url: str
    suite: str
    components: tuple[str, ...]
    key_url: str
    key_sha256: str
    packages: tuple[str, ...]


def required_repositories(spec: Spec, allowlist: Allowlist) -> list[ResolvedRepository]:
    """The repositories this spec needs, with `{codename}` resolved. Never empty for a reason."""
    resolved: list[ResolvedRepository] = []
    for identifier, packages in sorted(repository_packages(spec, allowlist).items()):
        policy = allowlist.repositories[identifier]
        suite = policy.suite
        if "{codename}" in suite:
            suite = suite.replace("{codename}", codename_for(spec.base.version))
        resolved.append(
            ResolvedRepository(
                id=policy.id,
                url=policy.url,
                suite=suite,
                components=policy.components,
                key_url=policy.key_url,
                key_sha256=policy.key_sha256,
                packages=tuple(packages),
            )
        )
    return resolved


def repository_summary(spec: Spec, allowlist: Allowlist) -> str:
    """The repositories this spec needs, for a human, or "" when none can be resolved.

    Display only. An unsupported release must fall through to the policy verdict instead of
    raising here: the person needs to read "no pinned image for ubuntu 20.04", not a traceback
    about a codename.
    """
    try:
        repositories = required_repositories(spec, allowlist)
    except UnknownRelease:
        return ""
    return ", ".join(
        f"{repo.id} ({repo.suite}, {' '.join(repo.components)})" for repo in repositories
    )


def snap_install_commands(spec: Spec, allowlist: Allowlist) -> list[str]:
    """One `snap install` per snap, with channel/confinement/revision from the policy."""
    commands = []
    for name in spec.snaps:
        policy = allowlist.snaps[name]
        command = f"snap install {name}"
        if policy.revision is not None:
            command += f"={policy.revision}"
        else:
            command += f" --channel={policy.channel}"
        if policy.classic:
            command += " --classic"
        commands.append(command)
    return commands


def effective_services(spec: Spec) -> list[str]:
    return sorted(set(spec.services.enable))


def cloud_init(spec: Spec, build_authorized_key: str, allowlist: Allowlist) -> tuple[str, str]:
    """Return (user-data, meta-data) for the build VM's NoCloud seed."""
    users: list[dict[str, Any]] = [
        {
            "name": BUILD_USER,
            "shell": "/bin/bash",
            "sudo": "ALL=(ALL) NOPASSWD:ALL",
            "groups": ["sudo"],
            "ssh_authorized_keys": [build_authorized_key],
        }
    ]
    for user in spec.users:
        entry: dict[str, Any] = {
            "name": user.name,
            "shell": "/bin/bash",
            "ssh_authorized_keys": list(user.ssh_authorized_keys),
        }
        if user.sudo:
            entry["sudo"] = "ALL=(ALL) NOPASSWD:ALL"
            entry["groups"] = ["sudo"]
        users.append(entry)

    files: list[dict[str, Any]] = []
    for entry in spec.files:
        item: dict[str, Any] = {
            "path": entry.path,
            "content": entry.content,
            "permissions": entry.mode,
        }
        if entry.owner:
            item["owner"] = entry.owner
        files.append(item)

    # Snaps first: a service a snap provides cannot be enabled before the snap exists.
    runcmd = snap_install_commands(spec, allowlist)
    runcmd += [f"systemctl enable {service}" for service in effective_services(spec)]
    runcmd += [f"systemctl disable {service} || true" for service in spec.services.disable]

    packages = cloud_init_packages(spec, allowlist)

    user_data: dict[str, Any] = {
        "hostname": BUILD_HOSTNAME,
        "ssh_pwauth": False,
        "package_update": True,
        "package_upgrade": False,
        "users": users,
        "packages": packages,
        "runcmd": runcmd,
    }
    if files:
        user_data["write_files"] = files

    dumped = "#cloud-config\n" + yaml.safe_dump(
        user_data, sort_keys=True, default_flow_style=False, width=10_000
    )
    meta_data = yaml.safe_dump(
        {"instance-id": BUILD_INSTANCE_ID, "local-hostname": BUILD_HOSTNAME},
        sort_keys=True,
    )
    return dumped, meta_data


def render(
    spec: Spec,
    build_dir: Path,
    allowlist: Allowlist,
    build_authorized_key: str,
) -> Rendered:
    """Write every file Packer needs under `build_dir` and return their locations."""
    packer_dir = build_dir / "packer"
    seed_dir = build_dir / "seed"
    provision_dir = build_dir / "provision"
    for directory in (packer_dir, seed_dir, provision_dir):
        directory.mkdir(parents=True, exist_ok=True)

    packages = effective_packages(spec, allowlist)
    services = effective_services(spec)
    repositories = required_repositories(spec, allowlist)

    template = render_text(
        "packer/image.pkr.hcl.j2",
        {
            "spec_sha256": spec_hash(spec),
            "plugin_version": PLUGIN_VERSION,
            "image_name": IMAGE_NAME,
            "disk_size_mb": disk_size_mb(spec.disk_size),
            "cpus": 2,
            "memory": 2048,
            "packages": packages,
            "services": services,
            "has_repositories": bool(repositories),
        },
    )
    (packer_dir / "image.pkr.hcl").write_text(template, encoding="utf-8")

    user_data, meta_data = cloud_init(spec, build_authorized_key, allowlist)
    (seed_dir / "user-data").write_text(user_data, encoding="utf-8")
    (seed_dir / "meta-data").write_text(meta_data, encoding="utf-8")

    assert_script = render_text(
        "scripts/assert.sh.j2",
        {"packages": packages, "services": services, "snaps": list(spec.snaps)},
    )
    cleanup_script = render_text("scripts/cleanup.sh.j2", {})
    finalize_script = render_text("scripts/finalize.sh.j2", finalize_context())
    scripts = [
        ("assert.sh", assert_script),
        ("cleanup.sh", cleanup_script),
        ("finalize.sh", finalize_script),
    ]
    if repositories:
        scripts.append(
            (
                "repositories.sh",
                render_text(
                    "scripts/repositories.sh.j2",
                    {
                        "repositories": repositories,
                        "all_packages": sorted(
                            {package for repo in repositories for package in repo.packages}
                        ),
                    },
                ),
            )
        )
    for name, body in scripts:
        path = provision_dir / name
        path.write_text(body, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    return Rendered(
        packer_dir=packer_dir,
        seed_dir=seed_dir,
        provision_dir=provision_dir,
        template_sha256=hashlib.sha256(template.encode("utf-8")).hexdigest(),
    )
