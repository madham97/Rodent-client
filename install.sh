#!/bin/bash
# Monitoring pipeline client — installer
# Run as root from the Rodent-client directory: sudo bash install.sh

set -e

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
INSTALL_DIR=/opt/monitoring-pipeline

if [ "$EUID" -ne 0 ]; then
    echo "Error: run as root: sudo bash install.sh"
    exit 1
fi

echo ""
echo "=== Monitoring Pipeline Installer ==="
echo ""

# ── Load defaults from existing config ────────────────────────────────────────
EXISTING="$INSTALL_DIR/config/client.json"
_json() {
    python3 -c "import json; d=json.load(open('$EXISTING')); print(d.get('$1','$2'))" \
        2>/dev/null || echo "$2"
}

if [ -f "$EXISTING" ]; then
    echo "Existing config found at $EXISTING — press Enter to keep current values."
    echo ""
    DEF_URL=$(_json server_url "")
    DEF_APN=$(_json gsm_apn "web.vodafone.de")
    DEF_DEV=$(_json gsm_device "/dev/serial0")
    DEF_DID=$(_json device_id "$(hostname)")
    DEF_NUM=$(_json gsm_number "")
else
    DEF_URL=""
    DEF_APN="web.vodafone.de"
    DEF_DEV="/dev/serial0"
    DEF_DID="$(hostname)"
    DEF_NUM=""
fi

# ── Configuration prompts ──────────────────────────────────────────────────────
read -rp "Server URL [${DEF_URL:-required}]: " _IN
SERVER_URL="${_IN:-$DEF_URL}"
if [ -z "$SERVER_URL" ]; then echo "Error: server URL is required."; exit 1; fi

read -rp "Device ID [$DEF_DID]: " _IN
DEVICE_ID="${_IN:-$DEF_DID}"

read -rp "GSM APN [$DEF_APN]: " _IN
GSM_APN="${_IN:-$DEF_APN}"

read -rp "GSM serial device [$DEF_DEV]: " _IN
GSM_DEVICE="${_IN:-$DEF_DEV}"

read -rp "SIM phone number (e.g. +491741959602, leave blank if unknown) [${DEF_NUM}]: " _IN
GSM_NUMBER="${_IN:-$DEF_NUM}"

echo -n "SIM PIN (leave blank if none): "
read -rs GSM_PIN; echo
if [ -n "$GSM_PIN" ]; then
    echo -n "Confirm SIM PIN: "
    read -rs GSM_PIN2; echo
    if [ "$GSM_PIN" != "$GSM_PIN2" ]; then echo "Error: PINs do not match."; exit 1; fi
    echo "Note: PIN will be stored in plaintext in client.json (mode 640)."
fi

echo ""
echo "Recording mode:"
echo "  1) image_motion   - capture JPEG when motion detected (default)"
echo "  2) image_interval - capture JPEG on a fixed timer"
read -rp "Choice [1]: " _CHOICE
[[ "$_CHOICE" == "2" ]] && REC_MODE="image_interval" || REC_MODE="image_motion"

echo ""
WRITE_WEBUI=0
if [ -f "$INSTALL_DIR/config/webui.env" ]; then
    read -rp "Change Web UI password? [y/N]: " _CHG
fi
if [[ "$_CHG" =~ ^[Yy]$ ]] || [ ! -f "$INSTALL_DIR/config/webui.env" ]; then
    echo -n "Web UI password [monitoring]: "
    read -rs WEBUI_PASS; echo
    WEBUI_PASS="${WEBUI_PASS:-monitoring}"
    [ "$WEBUI_PASS" = "monitoring" ] && echo "Warning: using default password. Change it before exposing the dashboard."
    WRITE_WEBUI=1
fi

echo ""
echo "--- Installing ---"

# ── Directories ────────────────────────────────────────────────────────────────
mkdir -p "$INSTALL_DIR"/{config,pi-client,scripts}
mkdir -p /outbox /uploaded /var/log
touch /var/log/monitoring-pipeline.log
echo "Directories ready."

# ── Copy source files ──────────────────────────────────────────────────────────
cp -r "$SCRIPT_DIR/pi-client/"* "$INSTALL_DIR/pi-client/"
cp -r "$SCRIPT_DIR/scripts/"*   "$INSTALL_DIR/scripts/"
echo "Source files installed."

# ── Python virtual environment ─────────────────────────────────────────────────
if ! python3 -m venv --help >/dev/null 2>&1; then
    apt-get install -y python3-venv
fi
python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install --upgrade pip -q
"$INSTALL_DIR/venv/bin/pip" install flask requests pyserial pillow -q
echo "Python environment ready."

# ── Write client.json ──────────────────────────────────────────────────────────
cat > "$INSTALL_DIR/config/client.json" << EOF
{
  "server_url":    "$SERVER_URL",
  "device_id":     "$DEVICE_ID",
  "outbox_dir":    "/outbox",
  "uploaded_dir":  "/uploaded",
  "gsm_device":    "$GSM_DEVICE",
  "gsm_pin":       "$GSM_PIN",
  "gsm_apn":       "$GSM_APN",
  "gsm_number":    "$GSM_NUMBER",
  "poll_interval": 10,
  "max_retries":   3,
  "retry_delay":   10,
  "webp_compress": true,
  "webp_quality":  80,
  "recording": {
    "mode":               "$REC_MODE",
    "camera_id":          0,
    "width":              1280,
    "height":             720,
    "rpicam_still_path":  "rpicam-still",
    "min_size_bytes":     1024,
    "motion_threshold":   0.015,
    "detection_interval": 1,
    "motion_cooldown":    60,
    "detection_width":    320,
    "detection_height":   240,
    "temporal_alpha":     0.1,
    "motion_debug":       false,
    "image_interval":     30,
    "image_quality":      75
  }
}
EOF
chown root:sudo "$INSTALL_DIR/config/client.json"
chmod 660 "$INSTALL_DIR/config/client.json"
echo "client.json written."

# ── Write webui.env ────────────────────────────────────────────────────────────
if [ "$WRITE_WEBUI" -eq 1 ]; then
    printf 'WEBUI_USERNAME=admin\nWEBUI_PASSWORD=%s\nWEBUI_PORT=8080\n' \
        "$WEBUI_PASS" > "$INSTALL_DIR/config/webui.env"
    chown root:sudo "$INSTALL_DIR/config/webui.env"
    chmod 660 "$INSTALL_DIR/config/webui.env"
    echo "webui.env written."
fi

# ── Tailscale ──────────────────────────────────────────────────────────────────
if command -v tailscale &>/dev/null; then
    echo "Tailscale already installed: $(tailscale version | head -1)"
else
    echo "Installing Tailscale..."
    if curl -fsSL https://pkgs.tailscale.com/stable/debian/bookworm.noarmor.gpg \
            | tee /usr/share/keyrings/tailscale-archive-keyring.gpg >/dev/null \
    && curl -fsSL https://pkgs.tailscale.com/stable/debian/bookworm.tailscale-keyring.list \
            | tee /etc/apt/sources.list.d/tailscale.list >/dev/null; then
        apt-get update -qq && apt-get install -y tailscale -q
        echo "Tailscale installed."
    else
        echo "Warning: Tailscale download failed. Install manually: curl -fsSL https://tailscale.com/install.sh | sh"
    fi
fi
if command -v tailscale &>/dev/null; then
    systemctl enable --now tailscaled 2>/dev/null || true
fi

# ── Free serial port for GSM modem ────────────────────────────────────────────
# Disable the serial getty so it doesn't hold /dev/ttyS0 open
systemctl stop serial-getty@ttyS0.service 2>/dev/null || true
systemctl disable serial-getty@ttyS0.service 2>/dev/null || true
# Remove the serial console from the kernel cmdline so the kernel doesn't claim the port
CMDLINE=/boot/firmware/cmdline.txt
if [ -f "$CMDLINE" ] && grep -q 'console=serial0' "$CMDLINE"; then
    sed -i 's/console=serial0,[0-9]* //g' "$CMDLINE"
    echo "Removed serial console from $CMDLINE."
fi
echo "Serial port freed for GSM modem."

# ── Systemd services ───────────────────────────────────────────────────────────
for svc in monitoring-pipeline-uploader monitoring-pipeline-recorder \
           monitoring-pipeline-webui; do
    cp "$SCRIPT_DIR/systemd/$svc.service" /etc/systemd/system/
done
systemctl daemon-reload
echo "Systemd services installed."

# ── Enable and start ───────────────────────────────────────────────────────────
echo ""
read -rp "Enable and start services now? [Y/n]: " _START
if [[ ! "$_START" =~ ^[Nn]$ ]]; then
    for svc in monitoring-pipeline-recorder monitoring-pipeline-uploader monitoring-pipeline-webui; do
        if systemctl enable --now "$svc" 2>/dev/null; then
            echo "Started: $svc"
        else
            echo "Warning: $svc did not start. Check: journalctl -u $svc -n 20"
        fi
    done
fi

# ── Summary ────────────────────────────────────────────────────────────────────
echo ""
echo "=== Installation complete ==="
echo ""
echo "  Server URL : $SERVER_URL"
echo "  Device ID  : $DEVICE_ID"
echo "  GSM        : $GSM_APN on $GSM_DEVICE"
[ -n "$GSM_NUMBER" ] && echo "  SIM number : $GSM_NUMBER"
[ -n "$GSM_PIN" ] && echo "  SIM PIN    : stored in client.json (mode 640)"
echo "  Recording  : $REC_MODE"
echo ""
TS_IP="$(tailscale ip -4 2>/dev/null || true)"
if [ -n "$TS_IP" ]; then
    echo "  Tailscale IP : $TS_IP"
    echo "  Dashboard    : http://${TS_IP}:8080  (user: admin)"
else
    echo "  Next: run 'sudo tailscale up' to authenticate Tailscale."
    echo "  Then open: http://<tailscale-ip>:8080"
fi
echo ""
