# gira-iso

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

## Interactive menu

Run `gira-iso` with no arguments. It asks for the API key first (hidden, validated, never
written to disk) and then offers:

1. **List images**: pick one to see its provenance, export it, or delete it.
2. **Upload a base image**: register a local Ubuntu cloud image (`*-cloudimg-amd64.img`, qcow2).
   Installer ISOs are refused; they cannot be used as a base.
3. **Base images**: the pinned official ones (Ubuntu 22.04 and 24.04) and your own, with a
   download option for the official ones.
4. **Create an image**: choose the base, describe the image in Spanish or English (or pick one of
   the examples), review the interpreted spec and token usage, then watch the build.

The interpreter is told the allowed catalog (`policy/allowlist.yaml`, described in
`src/gira_iso/nl/catalog.yaml`), so it can tell a **package** (`docker` becomes `docker.io`) from
a **program** that apt does not provide (opencode, nvm, the official Docker CE). The menu shows
*what it understood* and *what it cannot include*, with the reason and allowed alternatives; those
claims are checked against the spec and the policy before you see them. If the policy rejects the
spec, the violations go back to the model so it can correct itself, and nothing is built unless
the final spec passes.

The last image name and destination are remembered. `Ctrl+C` returns to the menu.

## Commands

The scripted surface mirrors the menu:

```bash
gira-iso list                                          # images already created
gira-iso create examples/nginx-web.yaml --name web     # ~45s, writes image + manifest
gira-iso create "a web server with nginx" --name web   # natural language (asks for the key)
gira-iso show web                                      # provenance recorded for an image
gira-iso download web --to ~/Downloads                 # copies image + manifest, checksum verified
gira-iso delete web                                    # asks first; --yes to skip
gira-iso clean                                         # delete builds that never finished
```

`create` runs the schema and policy checks before downloading or building anything, so a
rejected spec costs nothing. Add `--verify` to boot the image and run the generated goss
assertions immediately after the build. `download` accepts an image name, a build id prefix,
a build directory or a `manifest.json` path. Builds live in `~/.local/share/gira-iso/builds`
(override the root with `GIRA_ISO_HOME`).

The older commands (`check`, `plan`, `build`, `verify`, `policy`, `push`, `eval`) are still
available but hidden from the main help: they are the advanced surface.

## Automation

For scripts and CI, `--json` keeps stdout for one JSON object (progress goes to stderr) and
`--non-interactive` never prompts: the key must be in `GIRA_ISO_LLM_API_KEY`, `--yes` is implied,
and a request that needs more detail exits `1` listing the questions.

```bash
export GIRA_ISO_LLM_API_KEY=...
gira-iso create "a web server with nginx" --name web --non-interactive --json | jq -r .image
gira-iso list --json | jq '.[].name'
gira-iso show web --json | jq .base
```

Exit codes: `0` success, `1` the policy or the verification rejected something (or the model
needs more detail), `2` invalid input, `3` the environment cannot do it (Packer, QEMU, the base
image download), `130` interrupted with `Ctrl+C`.

## Documents

- [`docs/plan-v1.md`](docs/plan-v1.md) — development plan, phases, acceptance gates and closed decisions.
- [`docs/flujo-de-control.md`](docs/flujo-de-control.md) — control flow: who decides what, where it can stop, how errors propagate, which controls exist and what is still open (with diagrams).

## Quick install

```bash
./install.sh                                              # Linux or WSL2
powershell -ExecutionPolicy Bypass -File .\install.ps1    # Windows: sets up WSL2 and runs install.sh inside it
```

Then run `gira-iso` (on Windows: `wsl -d Ubuntu -- gira-iso`) to open the menu. It asks for your
API key unless `GIRA_ISO_LLM_API_KEY` is set (endpoint `https://api.llmapi.ai/v1`).

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
uv run gira-iso create examples/nginx-web.yaml --name web   # ~45s, writes image + manifest
uv run gira-iso list
uv run gira-iso download web --to ~/Downloads               # copies image + manifest

# 5. Optional: boot the image and prove it satisfies its manifest
uv run gira-iso create examples/nginx-web.yaml --name web --verify

# 6. Natural language: the interactive menu, or measure the interpreter with the harness
uv run gira-iso
export GIRA_ISO_LLM_BASE_URL=... GIRA_ISO_LLM_API_KEY=... GIRA_ISO_LLM_MODEL=...
uv run gira-iso eval
```

Every command exits `0` on success, `1` when the policy or the verification rejects
something, `2` on invalid input and `3` when the environment fails; none ends in a traceback.

## Requirements

- Linux with KVM (`/dev/kvm`).
- `qemu-system-x86`, `qemu-utils`, `cloud-image-utils`.
- Packer (official binary, pinned by version and checksum).
- Python 3.12 with `uv`.

## Development

```bash
uv sync
uv run gira-iso version
uv run pytest
uv run ruff check .
```

## Building an image

```bash
uv run gira-iso build examples/nginx-web.yaml
```

That renders the spec into a Packer template and a cloud-init seed, runs Packer as a subprocess, and
writes `builds/<timestamp>-<spec-hash>/manifest.json` next to the image. The manifest records the
spec, the pinned base image, the Packer and plugin versions, the template hash and the artifact
hash, so an image is never an anonymous blob.

Policy runs before anything is downloaded: a rejected spec costs no bandwidth and no writes.

## Verifying an image

```bash
uv run gira-iso verify builds/<timestamp>-<spec-hash>
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
export GIRA_ISO_LLM_BASE_URL=https://api.example.com/v1
export GIRA_ISO_LLM_API_KEY=...
export GIRA_ISO_LLM_MODEL=deepseek-v4.1-flash   # optional
uv run gira-iso eval
```

The model may answer with exactly three shapes: a spec, a list of clarifying questions, or a
refusal. Anything else fails validation and is retried once with the error attached. The user's
text is delimited as data, and the policy layer runs independently of whatever the model produced.

### Measuring

```bash
uv run gira-iso eval --record eval/runs/2026-10-26.json   # call the model
uv run gira-iso eval --replay eval/runs/2026-10-26.json   # re-score, no network
```

The dataset lives in `src/gira_iso/eval/cases.yaml`: 25 cases across simple, composite, ambiguous,
out-of-scope, adversarial and bilingual categories. It was written before the client, and a test
enforces that every expected spec is valid under both the schema and the policy.

## Uploading to OpenStack

Out of the MVP, and optional: `pip install 'gira-iso[openstack]'`.

```bash
uv run gira-iso push builds/<timestamp>-<hash> --cloud mycloud
uv run gira-iso push builds/<timestamp>-<hash> --disk-format raw   # Ceph/RBD backends
```

Glance properties are derived from the manifest (`os_distro`, `os_version`,
`hw_qemu_guest_agent`, `hw_disk_bus`, plus the spec, policy and base hashes), and the
artifact checksum is verified before a byte is sent. A tampered or mismatched image is
refused.

## Changing the policy

The allowlist is the security boundary and it is data, not code:

```bash
export GIRA_ISO_ALLOWLIST=/etc/gira-iso/allowlist.yaml
uv run gira-iso policy      # which file is active, its hash, and what it permits
```

The hash of the active policy is recorded in every build manifest, so an image can be
traced back to the rules that allowed it.

### What reproducibility means here

Two builds of the same spec produce manifests that are identical field by field except for
`created_at` and the artifact hash. **The `.qcow2` bytes differ**, because the filesystem records
install timestamps and ext4 metadata. gira-iso promises behavioural reproducibility — same spec,
same packages, same users, same configuration — not bit-identical images.

## Debugging a raw image

`./scripts/boot-vanilla.sh --image path/to/image.qcow2` boots an image that has no
manifest and reports whether cloud-init applied a seed. It exists for phase 0 work and
for debugging; the supported path is `gira-iso verify`.

A spec that tries to disable `ssh`, install a package outside the allowlist or write
`/etc/sudoers.d/*` is rejected with a reason, and nothing reaches a builder.
