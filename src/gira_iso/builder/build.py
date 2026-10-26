"""Orchestrate one build: policy, base image, render, Packer, manifest.

The order is not negotiable. Policy runs before anything is downloaded or written,
so a rejected spec costs nothing but a few milliseconds.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from gira_iso.bases import builds_dir
from gira_iso.builder import packer
from gira_iso.builder.base import ensure_base_image
from gira_iso.builder.manifest import BaseRef, BuilderInfo, PolicyRef, build_manifest
from gira_iso.builder.render import (
    IMAGE_NAME,
    PLUGIN_VERSION,
    effective_packages,
    effective_services,
    render,
)
from gira_iso.policy.evaluate import (
    Allowlist,
    Verdict,
    allowlist_digest,
    evaluate,
    load_allowlist,
)
from gira_iso.spec.canonical import spec_hash
from gira_iso.spec.models import Spec


class BuildRejected(RuntimeError):
    def __init__(self, verdict: Verdict) -> None:
        super().__init__(f"spec rejected with {len(verdict.violations)} violation(s)")
        self.verdict = verdict


@dataclass(frozen=True)
class BuildResult:
    build_dir: Path
    image: Path
    manifest: Path


def _build_dir(work_root: Path, sha: str) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    directory = work_root / f"{stamp}-{sha[:12]}"
    if directory.exists():
        raise RuntimeError(f"build directory already exists: {directory}")
    directory.mkdir(parents=True)
    return directory


def _generate_key(path: Path) -> str:
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-C", "gira-iso-build", "-f", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return path.with_suffix(".pub").read_text(encoding="utf-8").strip()


def _policy_ref() -> PolicyRef:
    source, digest = allowlist_digest()
    return PolicyRef(source=source, sha256=digest)


def _virtual_size(path: Path) -> int:
    info = subprocess.run(
        ["qemu-img", "info", "--output=json", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return int(json.loads(info.stdout)["virtual-size"])


def build_image(
    spec: Spec,
    *,
    work_root: Path | None = None,
    allowlist: Allowlist | None = None,
    name: str | None = None,
    progress: Callable[[str], None] | None = None,
    on_line: Callable[[str], None] | None = None,
    on_download: Callable[[int, int], None] | None = None,
) -> BuildResult:
    def step(message: str) -> None:
        if progress is not None:
            progress(message)

    allowlist = allowlist or load_allowlist()

    step("Validando el spec contra el esquema y la política")
    verdict = evaluate(spec, allowlist)
    if not verdict.passed:
        raise BuildRejected(verdict)

    sha = spec_hash(spec)
    step(f"Preparando la imagen base ubuntu {spec.base.version}")
    base_path, base_sha = ensure_base_image(
        spec.base.os, spec.base.version, allowlist, on_progress=on_download
    )
    reference = allowlist.base_image(spec.base.os, spec.base.version)
    assert reference is not None

    build_dir = _build_dir(work_root or builds_dir(), sha)
    logs = build_dir / "logs"
    logs.mkdir()

    key_path = build_dir / "id_ed25519"
    public_key = _generate_key(key_path)

    step("Generando la plantilla de Packer y el seed de cloud-init")
    rendered = render(spec, build_dir, allowlist.mandatory_packages, public_key)

    template_dir = rendered.packer_dir
    step("Inicializando Packer (plugin qemu)")
    packer.init(template_dir, logs / "packer-init.log")
    step("Construyendo la imagen: arranca una VM, instala y limpia")
    packer.build(
        template_dir,
        {
            "base_image": str(base_path.resolve()),
            "base_sha256": base_sha,
            "seed_dir": str(rendered.seed_dir.resolve()),
            "ssh_private_key_file": str(key_path.resolve()),
            "build_dir": str(build_dir.resolve()),
        },
        logs / "packer-build.log",
        on_line=on_line,
    )

    image = build_dir / "image" / IMAGE_NAME
    if not image.is_file():
        raise RuntimeError(f"packer finished but produced no image at {image}")

    step("Registrando el manifest con hashes y procedencia")
    manifest = build_manifest(
        spec=spec,
        spec_sha256=sha,
        name=name,
        base=BaseRef(
            os=spec.base.os,
            version=spec.base.version,
            url=reference["url"],
            sha256=base_sha,
        ),
        policy=_policy_ref(),
        builder=BuilderInfo(
            packer_version=packer.version(),
            plugin_version=PLUGIN_VERSION,
            template_sha256=rendered.template_sha256,
        ),
        packages=effective_packages(spec, allowlist.mandatory_packages),
        services=effective_services(spec),
        artifact_path=image,
        virtual_size_bytes=_virtual_size(image),
    )
    manifest_path = manifest.write(build_dir)

    return BuildResult(build_dir=build_dir, image=image, manifest=manifest_path)
