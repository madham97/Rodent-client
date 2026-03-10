#!/bin/bash
# Requirements installation script
# Run this before deploying the monitoring pipeline

set -e

echo "Installing monitoring pipeline requirements..."

# Install python3-venv if not available
if ! python3 -m venv --help > /dev/null 2>&1; then
    echo "Installing python3-venv..."
    apt-get update
    apt-get install -y python3-venv
fi

# Create virtual environment
echo "Creating virtual environment..."
python3 -m venv /opt/monitoring-pipeline/venv

# Activate venv and install requirements
source /opt/monitoring-pipeline/venv/bin/activate
echo "Installing Python requirements..."
pip install --upgrade pip
pip install flask requests pyserial

# Create necessary directories
mkdir -p /outbox /uploaded
mkdir -p /var/log

# Set permissions (owned by current user)
chown -R $(whoami) /outbox /uploaded /opt/monitoring-pipeline/venv
chmod 755 /outbox /uploaded

# Copy service files
echo "Installing systemd services..."
cp systemd/monitoring-pipeline-uploader.service /etc/systemd/system/
cp systemd/monitoring-pipeline-recorder.service /etc/systemd/system/
systemctl daemon-reload

# Copy config (if not exists) into the single-folder layout under /opt
mkdir -p /opt/monitoring-pipeline/config
if [ ! -f /opt/monitoring-pipeline/config/client.json ]; then
    cp config/client.json /opt/monitoring-pipeline/config/client.json
    echo "Config file created at /opt/monitoring-pipeline/config/client.json"
    echo "Edit it to set the correct server URL!"
fi

echo "Installation complete!"
echo ""
echo "Next steps:"
echo "  1. Edit /opt/monitoring-pipeline/config/client.json to set:"
echo "     - server_url: your server IP and port (e.g., http://192.168.1.100:5000)"
echo "     - gsm_device: device path for GSM modem (default: /dev/serial0)"
echo "     - gsm_pin: SIM card PIN if needed"
echo "     - recording: enable audio/video recording settings"
echo "  2. sudo systemctl daemon-reload"
echo "  3. sudo systemctl enable monitoring-pipeline-uploader.service monitoring-pipeline-recorder.service"
echo "  4. sudo systemctl start monitoring-pipeline-uploader.service monitoring-pipeline-recorder.service"
echo "  5. journalctl -u monitoring-pipeline-uploader.service -f     # watch uploader logs"
echo "  6. journalctl -u monitoring-pipeline-recorder.service -f     # watch recorder logs"
