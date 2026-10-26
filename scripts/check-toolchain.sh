#!/usr/bin/env bash
# Verify the installed toolchain matches toolchain.lock.
#
# Without this, two machines can produce different images from the same manifest and
# nothing will say so. Missing binaries are reported, not installed.
set -Eeuo pipefail

SCRIPT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCK="$SCRIPT_ROOT/toolchain.lock"
[ -f "$LOCK" ] || { echo "error: no toolchain.lock at $LOCK" >&2; exit 1; }

value() { grep -E "^  $2:" "$1" | head -1 | sed -E 's/.*"([^"]*)".*/\1/'; }
section() { awk "/^$2:/{f=1;next} /^[a-z]/{f=0} f" "$1"; }

failures=0
check_binary() {
  local name="$1" version="$2" sha="$3" path
  path="$(command -v "$name" 2>/dev/null || true)"
  if [ -z "$path" ]; then
    printf '  MISSING  %-8s not in PATH\n' "$name"
    failures=$((failures + 1))
    return
  fi

  local actual_version actual_sha
  case "$name" in
    packer) actual_version="$("$path" version | sed -E 's/Packer v//')" ;;
    goss) actual_version="$("$path" --version | head -1 | sed -E 's/goss version //')" ;;
    *) actual_version="unknown" ;;
  esac
  actual_sha="$(sha256sum "$path" | awk '{print $1}')"

  if [ "$actual_version" = "$version" ] && [ "$actual_sha" = "$sha" ]; then
    printf '  OK       %-8s %s (%s)\n' "$name" "$version" "${actual_sha:0:12}…"
  else
    printf '  MISMATCH %-8s expected %s %s, got %s %s\n' \
      "$name" "$version" "${sha:0:12}…" "$actual_version" "${actual_sha:0:12}…"
    failures=$((failures + 1))
  fi
}

echo "toolchain:"
check_binary packer "$(value "$LOCK" version)" "$(value "$LOCK" sha256)"
check_binary goss \
  "$(awk '/^goss:/{f=1;next} /^[a-z]/{f=0} f' "$LOCK" | grep -E '^  version:' | sed -E 's/.*"([^"]*)".*/\1/')" \
  "$(awk '/^goss:/{f=1;next} /^[a-z]/{f=0} f' "$LOCK" | grep -E '^  sha256:' | sed -E 's/.*"([^"]*)".*/\1/')"

qemu_version="$(qemu-system-x86_64 --version | head -1 | sed -E 's/QEMU emulator version ([^ ]+).*/\1/')"
expected_qemu="$(awk '/^qemu:/{f=1;next} /^[a-z]/{f=0} f' "$LOCK" | grep -E '^  minimum_version:' | sed -E 's/.*"([^"]*)".*/\1/')"
expected_major="${expected_qemu%%.*}"
if [ "${qemu_version%%.*}" -ge "$expected_major" ]; then
  printf '  OK       %-8s %s\n' qemu "$qemu_version"
else
  printf '  MISMATCH %-8s expected %s.x, got %s\n' qemu "$expected_major" "$qemu_version"
  failures=$((failures + 1))
fi

for package in cloud-image-utils qemu-utils; do
  if dpkg -s "$package" >/dev/null 2>&1; then
    printf '  OK       %-8s installed\n' "$package"
  else
    printf '  MISSING  %-8s not installed (sudo apt-get install -y %s)\n' "$package" "$package"
    failures=$((failures + 1))
  fi
done

if [ "$failures" -ne 0 ]; then
  echo "toolchain does not match toolchain.lock ($failures problem(s))" >&2
  exit 1
fi
echo "toolchain matches toolchain.lock"
