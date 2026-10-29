# Snaps and official apt repositories

Objective: let a spec install software from the official Ubuntu snap store and from curated
vendor apt repositories, without weakening the "spec names identifiers, policy holds URLs" rule.
Branch: `feat/snaps-and-repositories`. Delivery: work-unit commits on the branch; no PR without the user.
Test runner: `uv run pytest` (network tests deselected) and `uv run ruff check .`.

## Specs

- **S1** — A spec may carry `snaps`, a list of lowercase snap names. Unsupported names are rejected
  by the policy with code `snap.not_allowed`. Duplicates are rejected by the schema.
- **S2** — `spec_version` accepts `"0"` and `"1"` and defaults to `"1"`. A `"0"` spec that carries
  `snaps` is rejected: version 0 does not define them. Every existing `"0"` spec stays valid.
- **S3** — The allowlist gains a `snaps` mapping (`channel`, `classic`, optional `revision`) and a
  `repositories` mapping (`url`, `suite`, `components`, `key_url`, `key_sha256`). A `packages`
  entry may be either a bare name (Ubuntu archive) or `{name, repository}`.
- **S4** — The packaged allowlist ships snaps `docker` and `certbot` (both `classic`, channel
  `stable`) and repositories `docker-ce` and `nodesource`, each with its GPG key pinned by sha256:
  `1500c1f56fa9e26b9b8f42452a553675796ade0807cdce11975eb98170b3a570` (Docker CE) and
  `b42e0321dabdc24e892115da705cf061167eac12a317f23d329862d0aa0a271d` (NodeSource).
- **S5** — A package backed by a repository is never handed to cloud-init's `packages:`. The build
  installs it only after the key is downloaded, its sha256 verified, the source file written with
  `Signed-By`, and `apt-get update` has run. Ubuntu-archive packages keep going through cloud-init.
- **S6** — The manifest records what was installed: for each snap `name`, `channel`, `classic`,
  `revision`; for each repository `id`, `url`, `suite`, `components`, `key_sha256`. Manifests written
  before this change still validate.
- **S7** — The build's own assertion script checks snaps with `snap list` and packages with
  `dpkg -l`. Verification (goss) asserts each snap is installed.
- **S8** — The interpreter is told the `snaps` field and the allowed snap catalog; it may translate a
  user's word into an allowed snap name, and nothing else.
- **S9** — `gira-iso policy`, `gira-iso check`, the review summary and `gira-iso allow list` show
  snaps and repositories. `gira-iso allow add` keeps working for packages only: snaps and
  repositories are curated in the packaged allowlist and are not addable from a laptop.
- **S10** — README and backlog describe the new surface and what remains out of scope (GitHub
  release assets).

## Tasks

- **T1** — Schema + policy + tests (S1, S2, S3, S4) — inline — done
- **T2** — Repo resolution, render, templates, packer provisioner (S5) — inline — done
- **T3** — Manifest provenance (S6) — inline — done
- **T4** — Build assertions and goss (S7) — inline — done
- **T5** — Interpreter catalog and eval scoring (S8) — inline — done
- **T6** — CLI/TUI surfacing and docs (S9, S10) — inline — done

## Log

- **L1** — "No, seguimos manteniendo con Canonical, pero me gustaria repos oficiales, snap, repos
  de github publico (pero con permiso del mismo usuario, por aho solo instalarlos? Es posible?"
- **L2** — "Empecemos con snap y repos oficiales. Dejemos el proyecto listo con eso."
- **L3** — Decision (orchestrator, confirmed by the user's choice of the recommended set): snaps and
  repositories are curated in the packaged allowlist. They are not locally addable, because no
  archive check exists for them and a third-party GPG key is permanent root trust. GitHub release
  assets are out of this scope.
- **L4** — Decision: `snaps` is the only new spec field. Repositories are not named in the spec: a
  repository is an implementation detail of a package, so the policy decides which repository a
  package comes from. This keeps the model's surface at one new field.
- **L5** — Decision: repository keys are ASCII-armored, installed as `/usr/share/keyrings/<id>.asc`
  and referenced by `Signed-By`, so no `gpg --dearmor` step and no extra package is needed.
- **L6** — Evidence: `spec_hash` changes for every spec because `snaps` is part of the canonical
  dump. Manifests keep the hash they recorded, so no stored build is invalidated; a rebuild of an
  unchanged spec gets a new build directory name. Accepted as a schema-version consequence.
- **L7** — Evidence: `uv run pytest` passes except
  `test_cli.py::test_the_full_menu_is_drawn_once_then_a_single_line`, which fails identically on
  `main` in this environment (Rich wraps the label at the captured width). Pre-existing, not
  caused by this change.
- **L8** — Evidence: rendered `repositories.sh`, `assert.sh`, `user-data` and the Packer template
  inspected by hand for a spec with `docker-ce`, `nodejs`, and snaps `docker` + `certbot`; all
  four scripts pass `bash -n`.
- **L9** — Review (four lenses, high risk): the refuter corroborated one CRITICAL finding, `R4-1`
  (the repository provisioner ran apt before cloud-init finished; dpkg lock contention). Fixed in
  `cb0dcb8` by waiting for cloud-init in `repositories.sh`, pinned with a test. Targeted validator
  then approved; authority acknowledged and burned. Fourteen advisory findings recorded in
  `backlog.md` as separate later work.
