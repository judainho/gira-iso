#!/usr/bin/env bash
# One-shot installer for gira-iso on Linux (and inside WSL2).
#
# Installs the host packages, the pinned packer and goss (verified against the sha256
# in toolchain.lock), uv, the Python environment, and puts `gira-iso` on the PATH.
# Safe to run again: anything already correct is skipped.
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCK="$ROOT/toolchain.lock"
BIN="$HOME/.local/bin"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

say() { printf '\n==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = "Linux" ] || die "this script is for Linux or WSL2; on Windows run install.ps1"
[ "$(uname -m)" = "x86_64" ] || die "only x86_64 is supported (the pinned binaries are amd64)"
[ -f "$LOCK" ] || die "no toolchain.lock at $LOCK"
mkdir -p "$BIN"
export PATH="$BIN:$PATH"

lock_value() { # lock_value <section> <key>
  awk -v s="$1:" '$0 ~ "^"s {f=1;next} /^[a-z]/{f=0} f' "$LOCK" \
    | grep -E "^  $2:" | head -1 | sed -E 's/.*"([^"]*)".*/\1/'
}

download_verified() { # download_verified <url> <sha256> <dest>
  curl -fsSL --retry 3 -o "$3" "$1"
  echo "$2  $3" | sha256sum -c --status - || die "checksum mismatch for $1"
}

# --- 1. host packages --------------------------------------------------------------
say "Host packages"
packages=(cloud-image-utils qemu-system-x86 qemu-utils curl unzip)
missing=()
for package in "${packages[@]}"; do
  dpkg -s "$package" >/dev/null 2>&1 || missing+=("$package")
done
if [ "${#missing[@]}" -eq 0 ]; then
  echo "  already installed"
elif command -v apt-get >/dev/null 2>&1; then
  sudo apt-get update
  sudo apt-get install -y "${missing[@]}"
else
  die "no apt-get found; install these with your package manager: ${missing[*]}"
fi

# --- 2. packer and goss, pinned ----------------------------------------------------
installed_sha() { command -v "$1" >/dev/null 2>&1 && sha256sum "$(command -v "$1")" | awk '{print $1}'; }

say "packer $(lock_value packer version)"
packer_sha="$(lock_value packer sha256)"
if [ "$(installed_sha packer || true)" = "$packer_sha" ]; then
  echo "  already installed"
else
  # The lock pins the extracted binary; the zip is checked after extraction.
  curl -fsSL --retry 3 -o "$TMP/packer.zip" "$(lock_value packer source)"
  unzip -oq "$TMP/packer.zip" packer -d "$TMP"
  echo "$packer_sha  $TMP/packer" | sha256sum -c --status - || die "packer checksum mismatch"
  install -m 0755 "$TMP/packer" "$BIN/packer"
fi

say "goss $(lock_value goss version)"
goss_sha="$(lock_value goss sha256)"
if [ "$(installed_sha goss || true)" = "$goss_sha" ]; then
  echo "  already installed"
else
  curl -fsSL --retry 3 -o "$TMP/goss.tgz" "$(lock_value goss source)"
  tar -xzf "$TMP/goss.tgz" -C "$TMP" goss
  echo "$goss_sha  $TMP/goss" | sha256sum -c --status - || die "goss checksum mismatch"
  install -m 0755 "$TMP/goss" "$BIN/goss"
fi

# --- 3. uv and the Python environment ---------------------------------------------
say "uv"
if command -v uv >/dev/null 2>&1; then
  echo "  already installed ($(uv --version))"
else
  base="https://github.com/astral-sh/uv/releases/latest/download"
  asset="uv-x86_64-unknown-linux-gnu.tar.gz"
  curl -fsSL --retry 3 -o "$TMP/$asset" "$base/$asset"
  curl -fsSL --retry 3 -o "$TMP/$asset.sha256" "$base/$asset.sha256"
  (cd "$TMP" && sha256sum -c --status "$asset.sha256") || die "uv checksum mismatch"
  tar -xzf "$TMP/$asset" -C "$TMP"
  install -m 0755 "$TMP"/uv-x86_64-unknown-linux-gnu/uv "$TMP"/uv-x86_64-unknown-linux-gnu/uvx "$BIN/"
fi

say "Python environment and the gira-iso command"
(cd "$ROOT" && uv sync && uv tool install --editable --force .)

# --- 4. checks ---------------------------------------------------------------------
say "Toolchain check"
"$ROOT/scripts/check-toolchain.sh"

say "KVM"
if [ -r /dev/kvm ] && [ -w /dev/kvm ]; then
  echo "  /dev/kvm is usable"
elif [ -e /dev/kvm ]; then
  echo "  /dev/kvm exists but you cannot use it. Try: sudo usermod -aG kvm \$USER (then log in again)"
else
  echo "  /dev/kvm not found. Builds need hardware virtualisation."
  echo "  On WSL2 it needs a recent kernel and nested virtualisation enabled."
fi

if ! grep -qs 'local/bin' "$HOME/.profile" "$HOME/.bashrc" 2>/dev/null; then
  echo
  echo "Add this to your shell profile so 'gira-iso' is found in new terminals:"
  echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
fi

printf '\nDone. Run:  gira-iso\n'
