#!/usr/bin/env bash
# Phase 0 baseline: boot the Ubuntu 24.04 cloud image by hand, no Packer involved.
#
# Overlay + NoCloud seed ISO + QEMU/KVM. The point is to prove three things before
# any automation exists:
#   1. KVM and QEMU user networking work on this host.
#   2. cloud-init reads a seed and provisions the machine (hostname + SSH key).
#   3. growpart expands the root filesystem to the requested disk size.
#
# The base image is never modified: everything happens on a qcow2 overlay.
set -Eeuo pipefail

# The script changes directory to the image cache, so remember where it was
# invoked from: relative work dirs must resolve against the caller, not the cache.
SCRIPT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ORIG_PWD="$PWD"

BASE_URL="https://cloud-images.ubuntu.com/releases/noble/release"
BASE_NAME="ubuntu-24.04-server-cloudimg-amd64.img"
CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/gira-iso/images"

DISK="8G"
MEMORY="2048"
CPUS="2"
SSH_PORT="2222"
SSH_USER="gira-iso"
HOSTNAME="gira-iso-f0"
KEEP_RUNNING="no"
WORK_DIR=""
IMAGE=""
REQUIRE_PKGS=()
REQUIRE_USERS=()

log() { printf '[f0] %s\n' "$*" >&2; }
die() { printf '[f0] error: %s\n' "$*" >&2; exit 1; }

need() {
  command -v "$1" >/dev/null 2>&1 || die "missing '$1'. $2"
}

usage() {
  cat <<EOF
Usage: $(basename "$0") [options]

Boots a qcow2 on an overlay, seeds it with cloud-init and checks that the seed
was applied. Defaults to the pinned official Ubuntu 24.04 cloud image.

Options:
  --disk SIZE         overlay size (default: $DISK)
  --memory MB         guest RAM in MB (default: $MEMORY)
  --cpus N            vCPUs (default: $CPUS)
  --ssh-port PORT     host port forwarded to guest ssh (default: $SSH_PORT)
  --image PATH        boot this qcow2 instead of the official cloud image
  --require-package N fail unless package N is installed (repeatable)
  --require-user N    fail unless N exists and has an authorized key (repeatable)
  --work-dir DIR      where to put overlay, seed and logs (default: builds/vanilla-<ts>)
  --keep-running      leave the VM running instead of powering it off
  -h, --help          show this help
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --disk) DISK="$2"; shift 2 ;;
    --memory) MEMORY="$2"; shift 2 ;;
    --cpus) CPUS="$2"; shift 2 ;;
    --ssh-port) SSH_PORT="$2"; shift 2 ;;
    --image) IMAGE="$2"; shift 2 ;;
    --require-package) REQUIRE_PKGS+=("$2"); shift 2 ;;
    --require-user) REQUIRE_USERS+=("$2"); shift 2 ;;
    --work-dir) WORK_DIR="$2"; shift 2 ;;
    --keep-running) KEEP_RUNNING="yes"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

# --- preflight ------------------------------------------------------------

need qemu-system-x86_64 "install qemu-system-x86 (apt install qemu-system-x86)."
need qemu-img "install qemu-utils (apt install qemu-utils)."
need ssh "install openssh-client (apt install openssh-client)."
need ssh-keygen "install openssh-client (apt install openssh-client)."
need curl "install curl (apt install curl)."
need sha256sum "install coreutils (apt install coreutils)."
need jq "install jq (apt install jq)."

if ! command -v cloud-localds >/dev/null 2>&1; then
  die "missing 'cloud-localds'. Install cloud-image-utils:
       sudo apt-get install -y cloud-image-utils"
fi

[ -r /dev/kvm ] && [ -w /dev/kvm ] || die "/dev/kvm is not accessible for this user"

# --- base image, pinned by version and verified by checksum ---------------

if [ -n "$IMAGE" ]; then
  # Booting an image we built: it has no published checksum, so trust the caller.
  [ -f "$IMAGE" ] || die "no such image: $IMAGE"
  BASE_IMAGE="$(cd "$(dirname "$IMAGE")" && pwd)/$(basename "$IMAGE")"
  log "using supplied image: $BASE_IMAGE"
else
  mkdir -p "$CACHE_DIR"
  cd "$CACHE_DIR"

  if [ ! -f SHA256SUMS ]; then
    log "fetching SHA256SUMS"
    curl -fsSL -O "$BASE_URL/SHA256SUMS"
  fi

  if [ ! -f "$BASE_NAME" ]; then
    log "downloading $BASE_NAME"
    curl -fsSL -o "$BASE_NAME" "$BASE_URL/$BASE_NAME"
  fi

  log "verifying base image checksum"
  grep " \*\?${BASE_NAME}\$" SHA256SUMS > ".check.$$" \
    || die "no checksum entry for $BASE_NAME in SHA256SUMS"
  sha256sum -c ".check.$$" || die "base image checksum mismatch"
  rm -f ".check.$$"

  BASE_IMAGE="$CACHE_DIR/$BASE_NAME"
fi

# --- work directory -------------------------------------------------------

if [ -z "$WORK_DIR" ]; then
  WORK_DIR="$SCRIPT_ROOT/builds/vanilla-$(date -u +%Y%m%dT%H%M%SZ)"
else
  case "$WORK_DIR" in
    /*) ;;
    *) WORK_DIR="$ORIG_PWD/$WORK_DIR" ;;
  esac
fi
mkdir -p "$WORK_DIR"
WORK_DIR="$(cd "$WORK_DIR" && pwd)"
log "work directory: $WORK_DIR"

OVERLAY="$WORK_DIR/overlay.qcow2"
SEED="$WORK_DIR/seed.iso"
CONSOLE="$WORK_DIR/console.log"
PIDFILE="$WORK_DIR/qemu.pid"
SSH_KEY="$WORK_DIR/id_ed25519"

# --- ephemeral ssh key ----------------------------------------------------

if [ ! -f "$SSH_KEY" ]; then
  log "generating throwaway ssh key"
  ssh-keygen -t ed25519 -N '' -C 'gira-iso-f0' -f "$SSH_KEY" >/dev/null
fi
PUBKEY="$(cat "${SSH_KEY}.pub")"

# --- cloud-init seed ------------------------------------------------------

cat > "$WORK_DIR/user-data" <<EOF
#cloud-config
hostname: $HOSTNAME
fqdn: $HOSTNAME.local
ssh_pwauth: false
package_update: false
package_upgrade: false
users:
  - name: $SSH_USER
    shell: /bin/bash
    sudo: ALL=(ALL) NOPASSWD:ALL
    groups: [sudo]
    ssh_authorized_keys:
      - $PUBKEY
EOF

cat > "$WORK_DIR/meta-data" <<EOF
instance-id: $HOSTNAME-001
local-hostname: $HOSTNAME
EOF

log "building NoCloud seed ISO"
cloud-localds "$SEED" "$WORK_DIR/user-data" "$WORK_DIR/meta-data"

# --- overlay and boot -----------------------------------------------------

# An overlay smaller than its backing file truncates the guest disk: the partition
# table points past the end and the kernel drops to initramfs. Fail loudly instead.
base_virtual="$(qemu-img info --output=json "$BASE_IMAGE" | jq -r '."virtual-size"')"
overlay_bytes="$(numfmt --from=iec "$DISK")"
if [ "$overlay_bytes" -lt "$base_virtual" ]; then
  die "overlay $DISK is smaller than $BASE_IMAGE ($(numfmt --to=iec "$base_virtual")); use --disk >= that"
fi

log "creating $DISK overlay on top of the base image"
rm -f "$OVERLAY"
qemu-img create -q -f qcow2 -F qcow2 -b "$BASE_IMAGE" "$OVERLAY" "$DISK"

cleanup() {
  if [ -f "$PIDFILE" ]; then
    local pid
    pid="$(cat "$PIDFILE" 2>/dev/null || true)"
    if [ -n "${pid:-}" ] && kill -0 "$pid" 2>/dev/null; then
      log "stopping leftover qemu pid $pid"
      kill "$pid" 2>/dev/null || true
      for _ in $(seq 1 15); do
        kill -0 "$pid" 2>/dev/null || break
        sleep 1
      done
      kill -9 "$pid" 2>/dev/null || true
    fi
  fi
}
trap cleanup EXIT

: > "$CONSOLE"
log "starting qemu (KVM, $CPUS vCPU, ${MEMORY}MB, ssh on 127.0.0.1:$SSH_PORT)"
qemu-system-x86_64 \
  -name "$HOSTNAME" \
  -machine q35,accel=kvm \
  -cpu host \
  -smp "$CPUS" \
  -m "$MEMORY" \
  -drive "file=$OVERLAY,if=virtio,format=qcow2" \
  -drive "file=$SEED,if=virtio,format=raw,readonly=on" \
  -device virtio-serial-pci \
  -chardev "socket,path=$WORK_DIR/qga.sock,server=on,wait=off,id=qga0" \
  -device "virtserialport,chardev=qga0,name=org.qemu.guest_agent.0" \
  -nic "user,model=virtio-net-pci,hostfwd=tcp:127.0.0.1:$SSH_PORT-:22" \
  -display none \
  -serial "file:$CONSOLE" \
  -pidfile "$PIDFILE" \
  -daemonize

SSH=(ssh -i "$SSH_KEY" -p "$SSH_PORT" -o BatchMode=yes -o ConnectTimeout=5
     -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR
     "$SSH_USER@127.0.0.1")

log "waiting for ssh to accept connections (max 180s)"
ready="no"
for _ in $(seq 1 90); do
  if "${SSH[@]}" true 2>/dev/null; then ready="yes"; break; fi
  if ! kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    log "qemu exited early; last console output:"
    tail -n 40 "$CONSOLE" >&2
    die "VM failed to boot"
  fi
  sleep 2
done
[ "$ready" = "yes" ] || { tail -n 40 "$CONSOLE" >&2; die "ssh never came up"; }

log "waiting for cloud-init to finish"
"${SSH[@]}" sudo cloud-init status --wait >/dev/null

# --- checks derived from phase 0 goals ------------------------------------

failures=0
check() {
  local label="$1" expected="$2" actual="$3"
  if [ "$expected" = "$actual" ]; then
    printf '  PASS  %-22s %s\n' "$label" "$actual"
  else
    printf '  FAIL  %-22s expected %s, got %s\n' "$label" "$expected" "$actual"
    failures=$((failures + 1))
  fi
}

log "verifying the seed was applied"
got_hostname="$("${SSH[@]}" hostname)"
got_status="$("${SSH[@]}" sudo cloud-init status | awk -F': ' '/status/{print $2; exit}')"
got_key="$("${SSH[@]}" 'grep -c ssh-ed25519 ~/.ssh/authorized_keys || true')"
got_pw="$("${SSH[@]}" 'sudo grep -E "^\s*PasswordAuthentication" /etc/ssh/sshd_config /etc/ssh/sshd_config.d/*.conf 2>/dev/null | grep -ci "yes" || true')"
got_guest_agent="$("${SSH[@]}" 'dpkg -l qemu-guest-agent 2>/dev/null | grep -c "^ii" || true')"

# Disk expansion: growpart runs during first boot, so /dev/vda must report the
# overlay size as bytes, not the base image size.
want_bytes="$(numfmt --from=iec "$DISK")"
got_bytes="$("${SSH[@]}" "lsblk -bno SIZE /dev/vda | head -1")"
grew="$([ "$got_bytes" -ge $((want_bytes * 95 / 100)) ] && echo yes || echo no)"

check "hostname" "$HOSTNAME" "$got_hostname"
check "cloud-init status" "done" "$got_status"
check "ssh key installed" "1" "$got_key"
check "password auth off" "0" "$got_pw"
check "disk expanded" "yes" "$grew"

for user in ${REQUIRE_USERS[@]+"${REQUIRE_USERS[@]}"}; do
  exists="$("${SSH[@]}" "id -u $user >/dev/null 2>&1 && echo 1 || echo 0")"
  keys="$("${SSH[@]}" "sudo grep -c ssh-ed25519 /home/$user/.ssh/authorized_keys 2>/dev/null || echo 0")"
  check "user $user" "1" "$exists"
  check "user $user keys" "1" "$keys"
done

for pkg in ${REQUIRE_PKGS[@]+"${REQUIRE_PKGS[@]}"}; do
  installed="$("${SSH[@]}" "dpkg -l $pkg 2>/dev/null | grep -c '^ii' || true")"
  check "package $pkg" "1" "$installed"
done

# Informational: the official image is expected to ship the guest agent already.
# It is not a phase 0 gate, it is a fact the build phase must rely on or fix.
printf '  INFO  %-22s %s\n' "qemu-guest-agent" \
  "$([ "$got_guest_agent" = 1 ] && echo present || echo 'absent (build must install it)')"

log "guest ram: $("${SSH[@]}" free -m | awk '/Mem:/{print $2}')MB, disk /dev/vda: $(numfmt --to=iec "$got_bytes")"

if [ "$KEEP_RUNNING" != "yes" ]; then
  log "powering off"
  "${SSH[@]}" sudo poweroff || true
  # Wait for qemu to actually exit, otherwise the trap reports it as a leftover.
  pid="$(cat "$PIDFILE" 2>/dev/null || true)"
  for _ in $(seq 1 30); do
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null || break
    sleep 1
  done
fi

if [ "$failures" -ne 0 ]; then
  die "$failures check(s) failed; console log: $CONSOLE"
fi

log "phase 0 baseline OK"
log "artifacts: overlay=$OVERLAY console=$CONSOLE"
