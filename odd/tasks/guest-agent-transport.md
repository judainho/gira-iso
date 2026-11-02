# Verification over the guest agent

Objective: stop verifying images over SSH. The verification seed creates a user with
`sudo NOPASSWD`, so no verification could ever claim "this image has no passwordless sudo accounts"
and the transport itself depended on a credential the guest had to accept. The QEMU guest agent is
already on the virtio-serial bus (it was added so the agent starts at all); now it carries the
commands, the uploads and the shutdown.

Branch: `feat/guest-agent-transport`. Test runner: `uv run pytest`, `uv run ruff check .`,
`uv run ruff format --check .`. Real proof: `uv run gira-iso verify builds/<id>` on this machine.

## Specs

- **S1** — The verification seed declares no user and no sudo. Its user-data sets the hostname, keeps
  `ssh_pwauth: false`, disables package update/upgrade, and nothing else. No SSH key is generated,
  stored or deleted.
- **S2** — The verifier speaks to the guest through the QEMU guest agent on the virtio-serial socket.
  `ssh` and `ssh-keygen` are no longer required tools, and no TCP port is forwarded.
- **S3** — The client implements the QGA JSON protocol over the unix socket: `guest-ping` to wait for
  boot, `guest-exec` plus `guest-exec-status` to run a command and collect stdout, stderr and exit
  code, `guest-file-open/write/close` to upload, `guest-shutdown` to power off. An agent error
  becomes one `VmError`-family message with the agent's description, never a traceback.
- **S4** — Uploads are chunked and base64-encoded (`buf-b64`), so the 16 MB goss binary is uploaded
  over the channel. A file handle is closed even when a write fails.
- **S5** — Every existing check keeps its meaning: artifact checksum before boot, cloud-init applied
  the seed, the image boots unconfigured (hostname), goss result. A boot failure still prints the
  console tail.
- **S6** — goss asserts that no account with a real shell has passwordless sudo except the users the
  spec declares with `sudo`, plus root. The verification seed no longer adds one, so the assertion
  means what it says.
- **S7** — Tests cover the QGA client against a scripted unix-socket agent (ping, exec success and
  failure, chunked upload, agent error), the seed user-data (no `users:`, no key), and the rendered
  sudo command. The suite stays green without KVM.
- **S8** — Real proof on this machine: `gira-iso verify` on the existing build passes, the goss output
  includes the new assertion, and no QEMU process or sock survives.
- **S9** — `docs/flujo-de-control.md`, `README.md` and `backlog.md` describe the new transport, and
  the seed-sudo and goss-upload items leave the backlog. D1 stays: a spec still cannot disable `ssh`.

## Tasks

- **T1** — `verify/qga.py`: the protocol client (ping, exec, upload, shutdown) — inline — done
- **T2** — `verify/vm.py`: seed without users, agent wait, `run`, `upload`, power off; no ssh — inline — done
- **T3** — `verify/verify.py`: use the agent, upload goss and the spec, one command — inline — done
- **T4** — `verify/goss.py`: the passwordless-sudo assertion — inline — done
- **T5** — Tests: QGA fake agent, seed text, sudo command — inline
- **T6** — Docs and backlog — inline — done
- **T7** — Real verification of a fresh build — inline — done

## Log

- **L1** — "Podemos cerrar algo más hasta tener la API Key del modelo? Sigo esperando la instrucción
  del cliente."
- **L2** — "Procede a implementarlo" (the guest-agent transport recommended in the previous turn).
- **L3** — Decision: the agent runs as root, so no user and no sudo are needed in the seed. `ssh`
  stays mandatory in the policy (D1) because the image must remain usable, but the verifier stops
  needing it. This is an addition to the trust story, not a removal of a control.
- **L4** — Decision: goss travels over `guest-file-write` in chunks instead of the cloud-init seed.
  A 16 MB binary in user-data would be 6 MB gzipped plus base64 and would slow every boot; the file
  channel is local, faster, and already needed for the assertion document.

- **L5** — T1–T5 evidence: eight tests against a scripted agent on a real unix socket (ping down and up,
  exec with stdout and with a non-zero exit, an agent error, three-chunk upload, close-on-failure,
  shutdown that tolerates a dead channel), the seed text (`users: []`, no sudo, no key) and the
  rendered audit (`allowed='root'`, `allowed='deploy root'`). `uv run pytest` 611 passed, ruff clean.
- **L6** — First fix found by the protocol itself: `guest-file-open` answers with a bare integer
  handle, not `{"handle": n}`, and handle `0` must not be treated as missing. The client now takes
  the return value as-is; the fake agent mimics the real shape.
- **L7** — T7 evidence: a fresh build (`20261102T031605Z-a039e09cb2da`, 24 assertions) verifies with
  `PASS` on all four checks and `24 assertions passed`, including
  `no-passwordless-sudo-beyond-the-spec`. No QEMU process and no `id_ed25519` survive in the run
  directory.
- **L8** — Real finding on the way: the first run used the artifact from 2026-10-26T05:25, which
  predates the C1 fix (16:38 the same day). It failed `User/packer`, `File//home/packer`,
  `no-build-user-sudo` and the new audit, which is the correct verdict about an obsolete image — the
  new assertion caught a passwordless sudo account left by an old build. A fresh build passes; the
  old artifact stays out of the evidence.
- **L9** — Independent verifier (read-only, scratch copy): **no blockers**; S1–S7 and S9 met by execution.
  Seven advisories, five fixed here: the audit skipped system accounts with a shell (now it scans every
  account with a real shell, root and the spec's sudo users excepted), it missed `Defaults !authenticate`
  (now matches `NOPASSWD|!authenticate`), a short `guest-file-write` was silently accepted (now an error),
  a non-object agent reply escaped as `AttributeError` through `stop()` (now `GuestAgentError`), and the
  dead `qga.sock` stayed next to the evidence (now removed on stop). Left as they are: `ping` inherits
  the 120 s agent timeout, and the *build* transport still uses `ssh`/`ssh-keygen` (A7) — the spec says
  "the verifier", and that is what changed.
- **L10** — The verifier's last point was that the tests asserted the audit's text, not its behaviour.
  Six tests now execute the rendered command against a synthetic `/etc/passwd` and a `sudo` shim, plus
  three for the client (handle `0`, short write, timeout, non-object reply) and one for the console tail.
  Mutation check: neutering the audit (`:;`) makes three of them fail; restoring it makes them pass.
- **L11** — Final evidence on the same fresh build (`20261102T031605Z-a039e09cb2da`): 623 tests, ruff
  clean, `24 assertions passed` and no surviving QEMU process or socket.
