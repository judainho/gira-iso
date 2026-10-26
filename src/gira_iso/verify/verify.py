"""Orchestrate verification from a manifest.

The manifest is the input, not the spec file: it is the record of what was actually
built. Verifying an artifact against a spec you happen to have lying around proves
nothing about that artifact.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from gira_iso.builder.manifest import MANIFEST_NAME, Manifest, file_sha256
from gira_iso.spec.models import Spec
from gira_iso.verify.goss import to_yaml
from gira_iso.verify.vm import VirtualMachine, VmError

GOSS_REMOTE = "/tmp/goss"
GOSS_SPEC_REMOTE = "/tmp/goss.yaml"


class VerificationError(RuntimeError):
    pass


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True)
class VerifyResult:
    checks: tuple[CheckResult, ...]
    goss_output: str

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)


def load_manifest(path: Path) -> tuple[Manifest, Path]:
    """Accept either a build directory or the manifest file itself."""
    manifest_path = path / MANIFEST_NAME if path.is_dir() else path
    if not manifest_path.is_file():
        raise VerificationError(f"no manifest at {manifest_path}")
    try:
        manifest = Manifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    except ValidationError as exc:
        raise VerificationError(f"manifest at {manifest_path} is not valid: {exc}") from exc
    return manifest, manifest_path


def _goss_binary() -> Path:
    from shutil import which

    found = which("goss")
    if found is None:
        raise VerificationError(
            "goss is not installed: install the pinned release into ~/.local/bin"
        )
    return Path(found)


def _goss_summary(output: str) -> tuple[bool, str]:
    """Parse goss JSON output into (passed, short summary)."""
    try:
        report = json.loads(output)
    except json.JSONDecodeError:
        return False, "goss produced no parseable JSON"
    failed = report.get("summary", {}).get("failed-count", 0)
    tested = report.get("summary", {}).get("test-count", 0)
    if failed:
        names = [
            f"{result['resource-type']}/{result['resource-id']}: {result.get('property', '')}"
            for result in report.get("results", [])
            if not result.get("successful", True)
        ]
        return False, f"{failed} of {tested} assertions failed: " + ", ".join(names)
    return True, f"{tested} assertions passed"


def verify_build(
    build_path: Path,
    *,
    workdir: Path | None = None,
) -> VerifyResult:
    manifest, manifest_path = load_manifest(build_path)
    build_dir = manifest_path.parent
    image = build_dir / "image" / manifest.artifact.file
    if not image.is_file():
        raise VerificationError(f"manifest points at {image}, which does not exist")

    checks: list[CheckResult] = []

    # Integrity first: nothing else means anything if the bytes changed.
    actual = file_sha256(image)
    checks.append(
        CheckResult(
            "artifact checksum",
            actual == manifest.artifact.sha256,
            f"{actual[:16]}… vs manifest {manifest.artifact.sha256[:16]}…",
        )
    )
    if not checks[-1].passed:
        return VerifyResult(tuple(checks), "")

    spec = Spec.model_validate(manifest.spec)
    goss_spec = build_dir / "verify" / "goss.yaml"
    goss_spec.parent.mkdir(parents=True, exist_ok=True)
    goss_spec.write_text(to_yaml(spec, manifest.packages), encoding="utf-8")

    run_dir = workdir or (build_dir / "verify")
    goss_output = ""

    with VirtualMachine(image=image, workdir=run_dir) as vm:
        status = vm.wait_for_cloud_init()
        checks.append(
            CheckResult(
                "cloud-init applied the verification seed",
                "done" in status,
                status.replace("\n", " ")[:160],
            )
        )

        hostname = vm.ssh("hostname", check=False).stdout.strip()
        checks.append(
            CheckResult(
                "image boots unconfigured",
                hostname == "gira-iso-verify",
                f"hostname is '{hostname}'",
            )
        )

        try:
            vm.upload(_goss_binary(), GOSS_REMOTE)
            vm.upload(goss_spec, GOSS_SPEC_REMOTE)
            vm.ssh(f"sudo chmod 0755 {GOSS_REMOTE}", check=True)
            result = vm.ssh(
                f"sudo {GOSS_REMOTE} --gossfile {GOSS_SPEC_REMOTE} validate --format json",
                check=False,
                timeout=300,
            )
            goss_output = result.stdout or result.stderr
            (goss_spec.parent / "goss.json").write_text(goss_output, encoding="utf-8")
        except (VmError, OSError) as exc:
            checks.append(CheckResult("goss", False, str(exc)))

        if goss_output:
            passed, summary = _goss_summary(goss_output)
            checks.append(CheckResult("goss assertions", passed, summary))

    return VerifyResult(tuple(checks), goss_output)
