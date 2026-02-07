#!/bin/bash
# Quick deployment script for testing
# This sets up the monitoring pipeline locally for development/testing

set -e

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

echo "Setting up monitoring pipeline for local testing..."

# Create test directories
mkdir -p /tmp/monitoring-test/outbox
mkdir -p /tmp/monitoring-test/uploaded
mkdir -p /tmp/monitoring-test/uploads

echo "✓ Created test directories"

# Create a test video file (100MB)
echo "Creating test video file (10MB)..."
dd if=/dev/zero of=/tmp/monitoring-test/outbox/test-video.mp4 bs=1M count=10 2>/dev/null
echo "✓ Test video created"

# Create test config
cat > /tmp/test-config.json << 'EOF'
{
  "outbox_dir": "/tmp/monitoring-test/outbox",
  "uploaded_dir": "/tmp/monitoring-test/uploaded",
  "server_url": "http://localhost:5000",
  "max_retries": 2,
  "retry_delay": 5,
  "poll_interval": 2
}
EOF
echo "✓ Created test config"

echo ""
echo "Setup complete! To test:"
echo ""
echo "Terminal 1 - Start the server:"
echo "  export UPLOAD_DIR=/tmp/monitoring-test/uploads"
echo "  python3 $SCRIPT_DIR/server/server.py"
echo ""
echo "Terminal 2 - Start the client:"
echo "  python3 $SCRIPT_DIR/pi-client/uploader.py --config /tmp/test-config.json"
echo ""
echo "Or monitor directories:"
echo "  watch 'ls -la /tmp/monitoring-test/outbox /tmp/monitoring-test/uploaded /tmp/monitoring-test/uploads'"
