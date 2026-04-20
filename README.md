# Rodent Client — Edge Device

Raspberry Pi client for the rodent monitoring pipeline. Captures motion-triggered images via camera, compresses them, and uploads over GSM (SIM800 modem) to the server. Config can be updated remotely by sending a JSON SMS to the device's SIM number.

## Hardware requirements

- Raspberry Pi (tested on Pi 4 Model B)
- Camera module (compatible with `rpicam-still` / `rpicam-vid`)
- SIM800 GSM HAT connected to GPIO UART (`/dev/serial0`)
- SIM card with a data plan (GPRS)

## Installation

Clone the repo onto the Pi, then run the install script from the repo root:

```bash
cd /opt/Rodent-client
sudo bash install.sh
```

The script will:
- Create a Python virtualenv at `/opt/monitoring-pipeline/venv`
- Install Python dependencies
- Copy source files and config to `/opt/monitoring-pipeline/`
- Install and enable Tailscale
- Register the systemd services (uploader, recorder, web UI)

## First-time configuration

Edit `/opt/monitoring-pipeline/config/client.json` before starting the services:

```json
{
  "server_url": "http://<your-server>:8000",
  "gsm_pin":    "1234",
  "gsm_apn":    "web.vodafone.de",
  "outbox_dir":   "/outbox",
  "uploaded_dir": "/uploaded",
  "poll_interval": 10,
  "max_retries":   3,
  "retry_delay":   10,
  "webp_compress": true,
  "webp_quality":  80,
  "recording": {
    "enabled":           true,
    "mode":              "image_motion",
    "width":             1280,
    "height":            720,
    "framerate":         15,
    "image_quality":     75,
    "motion_threshold":  0.015,
    "motion_cooldown":   60,
    "detection_interval": 1
  }
}
```

Then authenticate Tailscale for remote web UI access:

```bash
sudo tailscale up
# open the printed URL in a browser to authorise the device
tailscale ip -4   # note this IP for web UI access
```

**Ensure the GSM HAT is powered on** before starting the services.

## Starting the services

```bash
sudo systemctl start monitoring-pipeline-recorder.service \
                     monitoring-pipeline-uploader.service \
                     monitoring-pipeline-webui.service
```

All three services are enabled at boot after install.

## Services

| Service | Description |
|---|---|
| `monitoring-pipeline-recorder` | Captures images/video from the camera into `/outbox` |
| `monitoring-pipeline-uploader` | Uploads files from `/outbox` to the server via GSM AT+HTTP |
| `monitoring-pipeline-webui` | Local management dashboard on port 8080 |

## Recording modes

Set via `recording.mode` in `client.json`:

| Mode | Description |
|---|---|
| `image_motion` | JPEG captured when motion is detected (default) |
| `image_interval` | JPEG captured at a fixed interval |
| `motion` | MP4 clip recorded when motion is detected |
| `segment` | Continuous fixed-duration MP4 chunks |

## Remote config via SMS

Send a JSON patch as a plain SMS to the device's SIM number. Changes are applied within 60 seconds.

Examples:
```
{"recording":{"mode":"segment"}}
{"recording":{"motion_threshold":0.03}}
{"webp_quality":60,"poll_interval":30}
{"recording":{"enabled":false}}
```

Keys under `recording` require the recorder service to restart automatically. Uploader keys (`webp_quality`, `webp_compress`, `poll_interval`, `max_retries`, `retry_delay`) take effect immediately.

Use the server's `/config-help` page to build and copy SMS patches interactively.

## Upload metadata

Each upload includes a JSON sidecar with:
- `device_id` — hostname of the Pi
- `mode` — recording mode that produced the file
- `motion_score` — fraction of pixels that changed (motion modes only)
- `timestamp` — UTC capture time

The server logs these to `upload_log.txt`.

## Web UI

Accessible at `http://<tailscale-ip>:8080`. Default credentials: `admin` / `monitoring` — change in `/opt/monitoring-pipeline/config/webui.env`.

## Logs

```bash
journalctl -u monitoring-pipeline-uploader.service -f
journalctl -u monitoring-pipeline-recorder.service -f
```

## Integration test

Verify the full pipeline (camera → GSM → server) after installation:

```bash
cd /opt/monitoring-pipeline
venv/bin/python3 pi-client/test_record_upload.py
```

## Troubleshooting

| Symptom | Check |
|---|---|
| Modem not responding | GSM HAT powered on? Correct device in `gsm_device`? |
| Uploads not reaching server | `server_url` correct? Signal strength in uploader logs? |
| No motion captures | `motion_threshold` too high? Check `motion_debug: true` |
| Web UI unreachable | `tailscale up` authenticated? Check port 8080 is not firewalled |
