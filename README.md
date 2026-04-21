# Monitoring Pipeline — Edge Client

Runs on a Raspberry Pi. Captures images (on motion or on a timer), uploads them to the server over a SIM800 GSM modem, and exposes a local web dashboard for management.

## First-time setup

Clone or copy this repo onto the Pi, then run the installer as root from the project directory:

```bash
cd /opt/Rodent-client
sudo bash install.sh
```

The installer will prompt for:

| Prompt | Default | Notes |
|--------|---------|-------|
| Server URL | — | Required. Use your server IP, hostname, or ngrok URL |
| Device ID | hostname | Identifies this Pi in upload metadata |
| GSM APN | `web.vodafone.de` | Data APN for your SIM card |
| GSM serial device | `/dev/serial0` | Serial port the SIM800 modem is on |
| SIM PIN | *(blank)* | Entered silently with confirmation. Stored in `client.json` (mode 640) |
| Recording mode | `image_motion` | `1` = JPEG on motion, `2` = JPEG on a timer |
| Web UI password | `monitoring` | Password for the dashboard at port 8080 |
| Start services now | yes | Enables and starts all systemd services |

The installer:
- Installs Python dependencies into a venv at `/opt/monitoring-pipeline/venv`
- Writes `/opt/monitoring-pipeline/config/client.json` and `webui.env`
- Configures PPP with the entered APN for the GSM data connection
- Installs Tailscale for remote access
- Copies source files to `/opt/monitoring-pipeline/`
- Installs and optionally starts the systemd services

Re-running `install.sh` is safe — it loads existing values as defaults.

## How it works

```
rpicam-still → /outbox/image_YYYYMMDDThhmmssZ.jpg
                          + .json sidecar (device_id, mode, timestamp, motion_score)
                    ↓
              uploader reads oldest image + sidecar
              → multipart POST to server /upload
              → moves image to /uploaded, deletes sidecar
```

## Services

Five systemd services are installed under the name prefix `monitoring-pipeline-`:

| Service | Role |
|---------|------|
| `recorder` | Captures images to `/outbox` |
| `uploader` | Uploads images from `/outbox` to the server via GSM |
| `webui` | Local dashboard at `http://<pi-ip>:8080` |
| `gsm-pin` | One-shot: unlocks SIM PIN before the GSM connection starts |
| `gsm` | Establishes the PPP data connection |

```bash
# Check status
sudo systemctl status monitoring-pipeline-recorder
sudo systemctl status monitoring-pipeline-uploader

# Follow logs
journalctl -u monitoring-pipeline-uploader -f
journalctl -u monitoring-pipeline-recorder -f

# Restart after a config change
sudo systemctl restart monitoring-pipeline-recorder monitoring-pipeline-uploader
```

## Configuration

Config lives at `/opt/monitoring-pipeline/config/client.json`. It can be edited directly or via the web dashboard (Config tab).

Key settings:

**Upload**
- `server_url` — server address (e.g. `http://192.168.1.10:8000` or an ngrok URL)
- `webp_compress` / `webp_quality` — re-encode JPEGs as WebP before upload to reduce transfer size
- `poll_interval` — seconds between outbox checks (default 10)
- `max_retries` / `retry_delay` — upload retry behaviour

**GSM**
- `gsm_device` — serial port (default `/dev/serial0`)
- `gsm_pin` — SIM PIN (blank if not required)
- `gsm_apn` — data APN for your carrier
- `gsm_number` — phone number of the SIM (e.g. `+447700900123`), shown in the dashboard — useful to note here so you know what number to send SMS config commands to

**Recording**
- `recording.mode` — `image_motion` or `image_interval`
- `recording.motion_threshold` — fraction of pixels that must change to trigger (0–1, default 0.015)
- `recording.motion_cooldown` — seconds to wait after a capture before checking for motion again
- `recording.image_interval` — seconds between captures in interval mode
- `recording.image_quality` — JPEG quality 1–100 (default 75)
- `recording.width` / `recording.height` — capture resolution (default 1280×720)
- `recording.motion_debug` — set `true` to log the motion ratio on every check, useful for tuning `motion_threshold`

Restart the relevant service after any change:
```bash
sudo systemctl restart monitoring-pipeline-recorder  # after recording changes
sudo systemctl restart monitoring-pipeline-uploader  # after upload/GSM changes
```

## Remote config via SMS

When the Pi has no internet connection, settings can be updated by sending an SMS to the SIM card in the modem. The uploader checks for messages between uploads and replies automatically.

**Commands**

| SMS text | Effect |
|----------|--------|
| `SET key=value` | Update one or more settings |
| `SET key=value key=value ...` | Update multiple settings at once |
| `STATUS` | Reply with current mode, threshold, interval, and quality |

**Updatable keys**

| Key | Type | Recorder restart? |
|-----|------|-------------------|
| `motion_threshold` | float (0–1) | No |
| `motion_cooldown` | float (seconds) | No |
| `detection_interval` | float (seconds) | No |
| `image_interval` | float (seconds) | No |
| `image_quality` | int (1–100) | No |
| `mode` | `image_motion` or `image_interval` | Yes — automatic |
| `webp_compress` | `true` or `false` | No |
| `webp_quality` | int (1–100) | No |

**Examples**

```
SET motion_threshold=0.05
SET mode=image_interval image_interval=30
SET image_quality=70 webp_compress=true webp_quality=65
STATUS
```

The Pi replies with `OK: key=value ...` on success, or `ERR: ...` describing what went wrong. Changes are written to `client.json` immediately. Keys that require a recorder restart (`mode`) trigger one automatically.

Messages from any phone number are accepted. The reply is sent back to the sender.

## Web dashboard

Accessible at `http://<tailscale-ip>:8080` (or local IP) with the credentials from `webui.env`.

- **Dashboard** — service status, GSM signal strength, outbox/uploaded file counts, Tailscale SSH address
- **Logs** — live tail of `/var/log/monitoring-pipeline.log`
- **Config** — edit and save `client.json` in-browser

## Testing

Test image capture and upload (no GSM modem needed — posts directly over HTTP):
```bash
cd /opt/monitoring-pipeline
venv/bin/python3 pi-client/test_record_upload.py
```

This captures a test image with `rpicam-still` (or falls back to a 1×1 dummy JPEG if no camera is attached), POSTs it to the configured server URL, and reports success or failure.

## Tailscale

```bash
sudo tailscale up        # authenticate (follow the printed URL)
tailscale ip -4          # get the Pi's Tailscale IP
```

Once connected, the Pi is reachable from any device on your Tailnet:
```bash
ssh root@<tailscale-ip>
# or open http://<tailscale-ip>:8080 for the dashboard
```

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Uploader exits immediately | Check modem is on `/dev/serial0`; verify SIM PIN in `client.json` |
| Images not appearing on server | Check `server_url` in config; verify server is reachable (`curl <url>/health`) |
| `/outbox` growing, nothing uploaded | Check GSM signal in dashboard; check uploader logs |
| Motion not triggering | Lower `motion_threshold` or enable `motion_debug` to see live ratios |
| Dashboard not accessible | Check webui service is running; confirm Tailscale is authenticated |
| SMS commands not working | Confirm modem is registered (`AT+CREG?`); check uploader log for `SMS from` lines; ensure SIM can receive SMS |
