# vmforge

Turn a natural-language description into a reproducible, cloud-ready `qcow2` image.

**Design principle:** the LLM never generates scripts or images. It only produces a declarative
spec that is validated against a schema and a security policy. A deterministic engine builds the
image from that spec.

```
natural language -> spec (spec.yaml) -> build template -> qcow2 image -> verification
        |                  ^
        v                  |
   interpreter (LLM) -> schema + policy
```

Status: initial design (v0). Development is incremental: every stage is built by hand and
understood before it is automated.

## Documents

- [`docs/plan-v1.md`](docs/plan-v1.md) — development plan, phases, acceptance gates and closed decisions.

## From zero

```bash
# 1. Host packages (the only ones this needs from the distribution)
sudo apt-get install -y qemu-system-x86 qemu-utils cloud-image-utils

# 2. Pinned toolchain: packer 1.16.2 and goss 0.4.10, verified by checksum
#    (see toolchain.lock for the exact downloads)
./scripts/check-toolchain.sh          # tells you exactly what is missing or different

# 3. Python
uv sync

# 4. Spec -> image
uv run vmforge check  examples/nginx-web.yaml   # schema + policy verdict
uv run vmforge build  examples/nginx-web.yaml   # ~45s, writes image + manifest
uv run vmforge verify builds/<timestamp>-<hash> # boots it and runs goss

# 5. Optionally describe it in natural language instead
export VMFORGE_LLM_BASE_URL=... VMFORGE_LLM_API_KEY=... VMFORGE_LLM_MODEL=...
uv run vmforge plan  spec.yaml   # what would be built, and what the policy says
```

Every command exits `0` on success, `1` when the policy or the verification rejects
something, and `2` on invalid input.

## Requirements

- Linux with KVM (`/dev/kvm`).
- `qemu-system-x86`, `qemu-utils`, `cloud-image-utils`.
- Packer (official binary, pinned by version and checksum).
- Python 3.12 with `uv`.

## Development

```bash
uv sync
uv run vmforge version
uv run pytest
uv run ruff check .
```

## Building an image

```bash
uv run vmforge build examples/nginx-web.yaml
```

That renders the spec into a Packer template and a cloud-init seed, runs Packer as a subprocess, and
writes `builds/<timestamp>-<spec-hash>/manifest.json` next to the image. The manifest records the
spec, the pinned base image, the Packer and plugin versions, the template hash and the artifact
hash, so an image is never an anonymous blob.

Policy runs before anything is downloaded: a rejected spec costs no bandwidth and no writes.

## Verifying an image

```bash
uv run vmforge verify builds/<timestamp>-<spec-hash>
```

Reads the manifest, checks the artifact checksum before booting anything, derives a `goss.yaml`
from the spec and runs it inside a fresh boot with a seed the image has never seen. Exit code is
`0` only when every assertion passes.

Only what the spec claims is asserted: packages installed, services enabled and running, users
present with their keys, files present with the declared mode and exact content hash. The spec does
not declare ports, so no port assertion is invented.

`./scripts/boot-vanilla.sh` remains as a debugging tool for booting an image that has no manifest.

## Interpreting natural language

The interpreter is model-agnostic: it speaks to any OpenAI-compatible endpoint.

```bash
export VMFORGE_LLM_BASE_URL=https://api.example.com/v1
export VMFORGE_LLM_API_KEY=...
export VMFORGE_LLM_MODEL=deepseek-v4.1-flash   # optional
uv run vmforge eval
```

The model may answer with exactly three shapes: a spec, a list of clarifying questions, or a
refusal. Anything else fails validation and is retried once with the error attached. The user's
text is delimited as data, and the policy layer runs independently of whatever the model produced.

### Measuring

```bash
uv run vmforge eval --record eval/runs/2026-10-26.json   # call the model
uv run vmforge eval --replay eval/runs/2026-10-26.json   # re-score, no network
```

The dataset lives in `src/vmforge/eval/cases.yaml`: 25 cases across simple, composite, ambiguous,
out-of-scope, adversarial and bilingual categories. It was written before the client, and a test
enforces that every expected spec is valid under both the schema and the policy.

## Uploading to OpenStack

Out of the MVP, and optional: `pip install 'vmforge[openstack]'`.

```bash
uv run vmforge push builds/<timestamp>-<hash> --cloud mycloud
uv run vmforge push builds/<timestamp>-<hash> --disk-format raw   # Ceph/RBD backends
```

Glance properties are derived from the manifest (`os_distro`, `os_version`,
`hw_qemu_guest_agent`, `hw_disk_bus`, plus the spec, policy and base hashes), and the
artifact checksum is verified before a byte is sent. A tampered or mismatched image is
refused.

## Changing the policy

The allowlist is the security boundary and it is data, not code:

```bash
export VMFORGE_ALLOWLIST=/etc/vmforge/allowlist.yaml
uv run vmforge policy      # which file is active, its hash, and what it permits
```

The hash of the active policy is recorded in every build manifest, so an image can be
traced back to the rules that allowed it.

### What reproducibility means here

Two builds of the same spec produce manifests that are identical field by field except for
`created_at` and the artifact hash. **The `.qcow2` bytes differ**, because the filesystem records
install timestamps and ext4 metadata. vmforge promises behavioural reproducibility — same spec,
same packages, same users, same configuration — not bit-identical images.

## Debugging a raw image

`./scripts/boot-vanilla.sh --image path/to/image.qcow2` boots an image that has no
manifest and reports whether cloud-init applied a seed. It exists for phase 0 work and
for debugging; the supported path is `vmforge verify`.

A spec that tries to disable `ssh`, install a package outside the allowlist or write
`/etc/sudoers.d/*` is rejected with a reason, and nothing reaches a builder.
