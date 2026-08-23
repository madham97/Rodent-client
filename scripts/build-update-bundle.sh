#!/bin/bash
# Build a client update bundle for the fleet to pull.
#
# Paths inside the bundle are relative to the install directory (/opt/Rodent-client), which is
# what the updater extracts against. Only source is included — never config/client.json, the
# venv, or the image queues: those are per-device state, and an update that overwrote them would
# take the SIM PIN and server URL with it.
#
# Usage:  scripts/build-update-bundle.sh [output-dir] [version]
#         scripts/build-update-bundle.sh /home/haamer/Server/client_updates

set -euo pipefail

REPO_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )/.." && pwd )"
OUT_DIR="${1:-$REPO_DIR/dist}"
VERSION="${2:-$(date -u +%Y%m%dT%H%M%SZ)}"

cd "$REPO_DIR"

# Refuse to ship code that does not import. The device compile-checks too, but failing here
# costs seconds where failing there costs a probation window and a rollback.
echo "Compile-checking sources..."
python3 -m py_compile pi-client/*.py scripts/*.py

echo "Running offline tests..."
TEST_LOG="$(mktemp)"
if ! ( cd pi-client && python3 test_updater.py && python3 test_thermal_geometry.py ) >"$TEST_LOG" 2>&1; then
    echo "offline tests FAILED — not building:"
    cat "$TEST_LOG"
    rm -f "$TEST_LOG"
    exit 1
fi
rm -f "$TEST_LOG"

mkdir -p "$OUT_DIR"
BUNDLE="$OUT_DIR/$VERSION.tgz"

tar czf "$BUNDLE" \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    pi-client/*.py \
    scripts/update-watchdog.py

SIZE=$(stat -c %s "$BUNDLE")
echo ""
echo "Built $BUNDLE"
# The 1.33 is base64 expansion on the wire; 1.8 KB/s is this deployment's measured GPRS throughput.
awk -v s="$SIZE" 'BEGIN { printf "  %d bytes (%.1f KB), ~%.0fs to transfer at the ~1.8KB/s this link delivers\n", s, s/1024, s/1024/1.8*1.33 }'
echo "  sha256: $(sha256sum "$BUNDLE" | cut -d' ' -f1)"
echo ""
echo "Devices with update_enabled will pick this up on their next check."
echo "Newest .tgz in the directory wins — remove older ones to roll the fleet back."
