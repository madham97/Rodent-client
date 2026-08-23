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
python3 -m py_compile pi-client/*.py pi-client/thermal/*.py scripts/*.py

echo "Running offline tests..."
TEST_LOG="$(mktemp)"
SKIPPED=""
for t in test_updater.py test_thermal_geometry.py test_uploader_recovery.py; do
    # `&& rc=0 || rc=$?` rather than a bare call: `set -e` aborts the script on the first
    # non-zero exit, so a plain invocation never reaches the line that inspects the code —
    # which silently turned "skipped" into "build stops here".
    ( cd pi-client && python3 "$t" ) >"$TEST_LOG" 2>&1 && rc=0 || rc=$?
    case $rc in
        0)  echo "  $t: passed" ;;
        # 77 means the test could not run here (optional deps absent), not that it failed.
        # Reported loudly rather than swallowed: the gate genuinely did not cover this build.
        77) echo "  $t: SKIPPED — $(tail -1 "$TEST_LOG")"; SKIPPED="$SKIPPED $t" ;;
        *)  echo "  $t FAILED — not building:"; cat "$TEST_LOG"; rm -f "$TEST_LOG"; exit 1 ;;
    esac
done
rm -f "$TEST_LOG"
if [ -n "$SKIPPED" ]; then
    echo ""
    echo "WARNING: not all checks ran ($SKIPPED ). Install their deps to gate on them."
fi

mkdir -p "$OUT_DIR"
BUNDLE="$OUT_DIR/$VERSION.tgz"

# pi-client/thermal/ is a runtime dependency, not an optional extra: recorder.py puts it on
# sys.path and imports thermal_common at startup. Omitting it shipped a bundle whose recorder
# called a frame_to_gray8 that did not exist in the version on the device — which fails at
# runtime, not at the compile check, and takes the thermal stream down with it.
tar czf "$BUNDLE" \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    pi-client/*.py \
    pi-client/thermal/*.py \
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
