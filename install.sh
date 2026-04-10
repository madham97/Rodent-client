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

# Install ppp for GSM data connection
echo "Installing ppp..."
apt-get install -y ppp

# PPP chat script for Vodafone Germany
mkdir -p /etc/ppp/chatscripts
cat > /etc/ppp/chatscripts/vodafone-de << 'EOF'
ABORT 'BUSY'
ABORT 'NO CARRIER'
ABORT 'NO DIALTONE'
ABORT 'NO ANSWER'
ABORT 'ERROR'
TIMEOUT 30
'' ATZ
OK ATE0
OK AT+CGDCONT=1,"IP","web.vodafone.de"
OK ATD*99#
CONNECT ''
EOF
chmod 640 /etc/ppp/chatscripts/vodafone-de

# PPP peer config for GSM modem
cat > /etc/ppp/peers/gsm << 'EOF'
/dev/serial0
115200
noauth
nodefaultroute
usepeerdns
persist
maxfail 0
holdoff 10
crtscts
lock
connect "/usr/sbin/chat -v -f /etc/ppp/chatscripts/vodafone-de"
EOF

# ip-up script: add GSM as a lower-priority default route (metric 700)
# so WiFi (metric 600) is preferred when both are up
mkdir -p /etc/ppp/ip-up.d
cat > /etc/ppp/ip-up.d/01-gsm-route << 'EOF'
#!/bin/bash
ip route add default dev ppp0 metric 700 2>/dev/null || true
EOF
chmod +x /etc/ppp/ip-up.d/01-gsm-route

# Copy service files
echo "Installing systemd services..."
cp systemd/monitoring-pipeline-uploader.service   /etc/systemd/system/
cp systemd/monitoring-pipeline-recorder.service   /etc/systemd/system/
cp systemd/monitoring-pipeline-webui.service      /etc/systemd/system/
cp systemd/monitoring-pipeline-gsm-pin.service    /etc/systemd/system/
cp systemd/monitoring-pipeline-gsm.service        /etc/systemd/system/
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
echo "  2. Edit config/webui.env to set a strong password"
echo "  3. Connect the Pi to your Tailscale network:"
echo "       sudo tailscale up"
echo "     Follow the URL it prints to authorise the device."
echo "     Then get its Tailscale IP:  tailscale ip -4"
echo "  4. sudo systemctl daemon-reload"
echo "  5. sudo systemctl enable monitoring-pipeline-recorder.service monitoring-pipeline-uploader.service monitoring-pipeline-webui.service"
echo "  6. sudo systemctl start monitoring-pipeline-recorder.service monitoring-pipeline-uploader.service monitoring-pipeline-webui.service"
echo "  7. Open http://<tailscale-ip>:8080 from any device on your Tailnet"
echo "     (login: admin / monitoring  — change this in config/webui.env)"
