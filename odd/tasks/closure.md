# Project closure

Objective: close everything the full-project audit found open, so `main` is green, the delivered
surface is documented as it is, and the remaining debt is either fixed or explicitly blocked on a
decision or an external resource.

Branch: `feat/closure`. Delivery: work-unit commits, one slice per reviewable unit; PR and merge are
the user's call. Test runner: `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`.

## Specs

- **S1** — `uv run pytest` is green on `main` (`586 passed, 1 deselected`) and `ruff check` plus
  `ruff format --check` are clean. Met by `056aa24` and `78f380d` on the snaps branch.
- **S2** — `gira-iso eval --replay <path>` with a missing file, invalid JSON, or a document without
  `entries` prints one `error:` line and exits `2`. No traceback, for any of the three.
- **S3** — `gira-iso push` without the optional `openstacksdk` exits `3` (environment), not `2`.
  Every other `PushError` keeps its current code, and the README's exit-code table says so.
- **S4** — `docs/flujo-de-control.md` describes snaps and vendor repositories: the trust table, the
  create flow with the repository provisioner, the manifest fields, the new controls, what is not
  done, and the decision that a repository key is root trust.
- **S5** — `docs/plan-v1.md` records the feature in its log and its phase state, pointing at
  `odd/tasks/snaps-and-repositories.md` for the detail.
- **S6** — `backlog.md` has exactly one entry about pinning snap revisions, and its blocker section
  no longer claims the LLM endpoint is unknown: the endpoint answers, `/v1/models` is public, and
  the credential is rejected by `/v1/chat/completions` with `401 invalid_api_key`.
- **S7** — `README.md` says the allowlist ships 42 archive packages plus 6 repository-backed ones
  (48 entries), documents exit `3` for a missing optional dependency and states that snaps are
  pinned to a revision.
- **S8** — `src/vmforge/` no longer exists; nothing imports it.
- **S9** — The workflows use GitHub Actions majors that run on Node 24, and the CI run on the merge
  commit is green.
- **S10** — `docker` and `certbot` are pinned to a concrete revision in the packaged allowlist; the
  build assertion fails when `snap list` reports a different revision; goss asserts the same; the
  manifest records the pinned revision instead of `null`.
- **S11** — `gira-iso clean --bases` lists cached base images with their size, asks once, deletes
  them, and refuses to delete a base that any manifest under the work root still references.
- **S12** — `toolchain.lock` pins the sha256 of the installed `packer-plugin-qemu` binary, a script
  verifies it against `packer plugins installed`, and CI exercises that check against a real
  install.
- **S13** — `eval/baselines/` holds a committed recording that CI replays offline, and the
  comparison table lives next to it. A recording cannot be produced until a credential that
  `/v1/chat/completions` accepts exists; until then the job is wired and the gap is recorded.
- **S14** — `docs/openstack.md` is a runbook for `clouds.yaml`, the chosen Glance name, and the
  manual boot check; an integration test skips when no credentials are configured.

## Tasks

- **T1** — Menu test and formatting (S1) — inline — done (`056aa24`, `78f380d`)
- **T2** — `eval --replay` refuses bad input instead of crashing (S2) — inline — done
- **T3** — `push` without the SDK is an environment failure (S3) — inline — done
- **T4** — Control-flow document covers the new surface (S4) — inline — done
- **T5** — Plan log and phase state (S5) — inline — done
- **T6** — Backlog dedup and corrected blocker (S6) — inline — done
- **T7** — README counts, exit code and pin note (S7) — inline — done
- **T8** — Remove the stale `src/vmforge/` tree (S8) — inline — done
- **T9** — Workflow action majors on Node 24 (S9) — inline — done
- **T10** — Pin snap revisions and assert them (S10) — inline — done
- **T11** — `clean --bases` for the base-image cache (S11) — inline — done
- **T12** — Pin the Packer plugin binary (S12) — inline — done
- **T13** — Replay job and baseline location (S13) — inline — done, recording blocked
- **T14** — OpenStack runbook and skippable integration test (S14) — inline — done

## Log

- **L1** — "Analiza al completo el proyecto e identifica qué es lo faltante. O si falta algo por
  cerrar."
- **L2** — "Diseña el plan para implementar y solucionar todos los problemas identificados."
- **L3** — "Si procede e implementalo todo. Avisame cuando se haya completado todo"
- **L4** — Audit evidence, before this branch: `main` (7dd37d2) had a red CI run
  (`40416476808`, test job, `assert 2 == 3`); `feat/snaps-and-repositories` held 7 unpushed commits
  and failed `ruff format --check` on five files; `eval --replay` raised an unhandled
  `FileNotFoundError`; `push` reported a missing optional dependency as invalid input; the
  control-flow document and the plan log had zero mentions of snaps or repositories; the backlog
  carried the snap-pin entry twice.
- **L5** — Correction to the audit: `/v1/models` answers `200` without any credential, so it proves
  nothing. `/v1/chat/completions` rejects the key in this environment with
  `401 invalid_api_key` for both `Authorization: Bearer` and `x-api-key`. F5 therefore stays
  blocked on a working credential, not on the endpoint.
- **L6** — Evidence for the fixes: `uv run pytest` 586 passed, `ruff check` clean,
  `ruff format --check` clean after T1; the menu test drives the base prompt with an empty line and
  expects one box plus one compact line.
