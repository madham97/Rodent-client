#!/bin/bash
# Quick local test — verifies uploader can connect to the server
# Usage: bash test-setup.sh [server_url]
#   server_url defaults to http://localhost:8000

set -e

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
SERVER_URL="${1:-http://localhost:8000}"
TEST_DIR=/tmp/monitoring-test

echo "Setting up local test environment..."

mkdir -p "$TEST_DIR/outbox" "$TEST_DIR/uploaded"

# Create a small test image (1x1 white JPEG)
python3 - << 'PYEOF'
import struct, zlib, os

TEST_DIR = "/tmp/monitoring-test"

# Minimal valid JPEG (1x1 white pixel)
jpeg = (
    b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00'
    b'\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t'
    b'\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a'
    b'\x1f\x1e\x1d\x1a\x1c\x1c $.\' ",#\x1c\x1c(7),\x01\x02\x03'
    b'\x04\x05\x06\x07\x08\x09\x0a\x0b\xff\xc0\x00\x0b\x08\x00\x01'
    b'\x00\x01\x01\x01\x11\x00\xff\xc4\x00\x1f\x00\x00\x01\x05\x01'
    b'\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02'
    b'\x03\x04\x05\x06\x07\x08\x09\x0a\x0b\xff\xda\x00\x08\x01\x01'
    b'\x00\x00?\x00\xf5\x0f\xff\xd9'
)

path = os.path.join(TEST_DIR, "outbox", "image_20260101T000000Z.jpg")
with open(path, "wb") as f:
    f.write(jpeg)

# Companion sidecar
import json
sidecar = os.path.join(TEST_DIR, "outbox", "image_20260101T000000Z.json")
with open(sidecar, "w") as f:
    json.dump({"device_id": "test-device", "mode": "image_motion",
               "timestamp": "2026-01-01T00:00:00Z", "motion_score": 0.0}, f)

print("Test image + sidecar created in /tmp/monitoring-test/outbox/")
PYEOF

cat > "$TEST_DIR/test-config.json" << EOF
{
  "outbox_dir":   "$TEST_DIR/outbox",
  "uploaded_dir": "$TEST_DIR/uploaded",
  "server_url":   "$SERVER_URL",
  "max_retries":  1,
  "retry_delay":  2,
  "poll_interval": 3,
  "webp_compress": false
}
EOF

echo "Test config written to $TEST_DIR/test-config.json"
echo ""
echo "To run the test:"
echo ""
echo "  Terminal 1 — start the server (from /opt/Server):"
echo "    cd /opt/Server && uvicorn app:app --reload"
echo ""
echo "  Terminal 2 — check server health:"
echo "    curl $SERVER_URL/health"
echo ""
echo "  Terminal 3 — manually upload the test image:"
echo "    curl -X POST $SERVER_URL/upload \\"
echo "      -F 'image=@$TEST_DIR/outbox/image_20260101T000000Z.jpg' \\"
echo "      -F 'device_id=test-device' -F 'mode=image_motion' \\"
echo "      -F 'motion_score=0.0' -F 'timestamp=2026-01-01T00:00:00Z'"
echo ""
echo "  Or run the uploader directly (requires no GSM modem — edit uploader.py to use requests instead):"
echo "    python3 $SCRIPT_DIR/pi-client/uploader.py --config $TEST_DIR/test-config.json"
echo ""
