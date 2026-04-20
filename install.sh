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
pip install flask requests pyserial pillow

# Create necessary directories
mkdir -p /outbox /uploaded
mkdir -p /var/log
mkdir -p /opt/monitoring-pipeline/config
touch /var/log/monitoring-pipeline.log

# Create web UI credentials file if missing
if [ ! -f /opt/monitoring-pipeline/config/webui.env ]; then
    cat > /opt/monitoring-pipeline/config/webui.env << 'EOF'
WEBUI_USERNAME=admin
WEBUI_PASSWORD=monitoring
WEBUI_PORT=8080
EOF
    echo "Web UI credentials written to config/webui.env — change the password!"
fi

# Install Tailscale via apt (works on Debian Bookworm / Raspberry Pi OS)
if ! command -v tailscale &> /dev/null; then
    echo "Installing Tailscale..."
    curl -fsSL https://pkgs.tailscale.com/stable/debian/bookworm.noarmor.gpg \
        | tee /usr/share/keyrings/tailscale-archive-keyring.gpg >/dev/null
    curl -fsSL https://pkgs.tailscale.com/stable/debian/bookworm.tailscale-keyring.list \
        | tee /etc/apt/sources.list.d/tailscale.list
    apt-get update -qq
    apt-get install -y tailscale
else
    echo "Tailscale already installed: $(tailscale version | head -1)"
fi

# Enable and start tailscaled (the daemon)
systemctl enable --now tailscaled

# Copy source files
echo "Copying source files..."
cp -r pi-client /opt/monitoring-pipeline/pi-client
cp -r scripts   /opt/monitoring-pipeline/scripts

# Copy service files (uploader, recorder, web UI only — GSM pppd not needed)
echo "Installing systemd services..."
cp systemd/monitoring-pipeline-uploader.service /etc/systemd/system/
cp systemd/monitoring-pipeline-recorder.service /etc/systemd/system/
cp systemd/monitoring-pipeline-webui.service    /etc/systemd/system/
systemctl daemon-reload

# Enable services to start on boot
systemctl enable monitoring-pipeline-uploader.service \
                 monitoring-pipeline-recorder.service \
                 monitoring-pipeline-webui.service

# Copy config if not already present
if [ ! -f /opt/monitoring-pipeline/config/client.json ]; then
    cp config/client.json /opt/monitoring-pipeline/config/client.json
    echo "Config copied to /opt/monitoring-pipeline/config/client.json"
fi

echo ""
echo "================================================"
echo " Installation complete!"
echo "================================================"
echo ""
echo "IMPORTANT — before starting the services:"
echo ""
echo "  1. Ensure the GSM HAT is powered on and the SIM card is inserted"
echo ""
echo "  2. Edit /opt/monitoring-pipeline/config/client.json:"
echo "       - server_url  : URL of your server (e.g. http://192.168.1.100:8000)"
echo "       - gsm_pin     : SIM PIN if required"
echo "       - gsm_apn     : APN for your carrier (default: web.vodafone.de)"
echo ""
echo "  3. Edit /opt/monitoring-pipeline/config/webui.env — set a strong password"
echo ""
echo "  4. Authenticate Tailscale (required for remote web UI access):"
echo "       sudo tailscale up"
echo "     Open the URL it prints, then get your Tailscale IP:"
echo "       tailscale ip -4"
echo ""
echo "  5. Start the services:"
echo "       sudo systemctl start monitoring-pipeline-recorder.service \\"
echo "                            monitoring-pipeline-uploader.service \\"
echo "                            monitoring-pipeline-webui.service"
echo ""
echo "  6. Open http://<tailscale-ip>:8080 in a browser"
echo "     (login: admin / monitoring — change this in config/webui.env)"
echo ""
