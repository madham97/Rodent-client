# Project Overview — Monitoring Pipeline Edge Client

## Purpose

A Raspberry Pi-based surveillance client that captures images (on motion or on a fixed timer), uploads them to a remote server over a 2G GSM modem, and exposes a local web dashboard for management and monitoring. Designed for remote/unattended deployments where no fixed internet connection is available — the GSM modem is the only data link.

## Repository Layout

```
Rodent-client/
├── pi-client/
│   ├── recorder.py          # Image capture service
│   ├── uploader.py          # GSM upload service
│   ├── web_ui.py            # Flask management dashboard
│   └── test_record_upload.py
├── scripts/
│   └── gsm-pin-unlock.py    # One-shot SIM PIN unlock at boot
├── systemd/                 # Service unit files (copied to /etc/systemd/system/)
├── config/
│   ├── client.json          # Live instance config (gitignored)
│   └── client.json.template # Template for new deployments
├── docs/
├── install.sh               # Setup script (run once per Pi)
└── .gitignore
```

The repo lives at `/opt/Rodent-client` and is the live deployment — services run directly from this directory. There is no separate install target; `install.sh` sets up the venv, writes config, and installs the systemd units.

## Services

Five systemd services, all prefixed `monitoring-pipeline-`:

| Service | Script | Role |
|---------|--------|------|
| `recorder` | `pi-client/recorder.py` | Captures images to `/outbox` |
| `uploader` | `pi-client/uploader.py` | Uploads images from `/outbox` via GSM modem |
| `webui` | `pi-client/web_ui.py` | Flask dashboard on port 8080 |
| `gsm-pin` | `scripts/gsm-pin-unlock.py` | One-shot: unlocks SIM PIN at boot before pppd starts |
| `gsm` | pppd | Establishes PPP data connection (not currently used — uploader uses AT+HTTP directly) |

The `gsm` and `gsm-pin` services are legacy from when pppd was used for data. The uploader now manages its own GPRS bearer via AT commands (`AT+SAPBR`, `AT+HTTPACTION`) and does not require pppd.

## Data Flow

```
rpicam-still
    │
    ▼
/outbox/image_YYYYMMDDTHHMMSSz.jpg   ← written atomically (sidecar then rename)
/outbox/image_YYYYMMDDTHHMMSSz.json  ← sidecar: device_id, mode, timestamp, motion_score
    │
    ▼ uploader polls outbox (oldest-first)
    │
    ├─ optionally re-encodes JPEG → WebP (reduces transfer size)
    │
    ▼
multipart/form-data POST → server /upload  (via SIM800 AT+HTTP stack)
    │
    ▼
/uploaded/image_YYYYMMDDTHHMMSSz.jpg  ← moved on success, sidecar deleted
```

Images are written atomically: the sidecar `.json` is always written before the `.jpg` is renamed into place, so the uploader never sees a `.jpg` without its metadata.

Files larger than 300 KB are skipped (SIM800 internal HTTP buffer limit) and moved directly to `/uploaded` with a log warning.

## Key Components

### recorder.py

Two capture modes, selected by `recording.mode` in config:

- **`image_motion`** — runs a continuous motion detection loop. Captures a low-resolution (320×240) grayscale frame every `detection_interval` seconds, computes a pixel diff against a running temporal average, and triggers a full-res capture when `motion_threshold` is exceeded. After a capture, a `motion_cooldown` pause prevents flooding the outbox. The running average rebuilds after each cooldown.
- **`image_interval`** — captures a full-res JPEG every `image_interval` seconds regardless of motion.

All captures go through `rpicam-still` (Raspberry Pi camera stack). The recorder handles SIGTERM gracefully and stops between captures.

### uploader.py

Manages the full GSM upload pipeline:

1. Opens the serial port to the modem (`/dev/ttyUSB0` for USB mode, `/dev/serial0` for GPIO mode)
2. Initialises the modem: polls `AT` until ready, resets with `ATZ`, unlocks SIM PIN if configured, waits for network registration (`AT+CREG`)
3. Opens a GPRS bearer (`AT+SAPBR`) with the configured APN
4. Polls `/outbox` for the oldest image, builds a multipart body, sends it via `AT+HTTP`
5. Between uploads, checks for incoming SMS messages — operators can update config or query status remotely via SMS commands (`SET key=value`, `STATUS`)

The modem class is named `SIM800` but works with SIM868/SIM800C as well (same AT command set).

**Serial port config**: `rtscts=False` is required when using USB-to-UART (CP2102). Hardware flow control (`rtscts=True`) blocks communication because the CTS line is not reliably wired through the CP2102 in the Waveshare HAT.

### web_ui.py

Flask app on port 8080, protected by HTTP Basic Auth (credentials from `config/webui.env`). Provides:

- **Dashboard** — service status (recorder/uploader active/inactive), last known GSM signal strength (read from log — avoids serial port contention), outbox/uploaded file counts and sizes, Tailscale SSH address
- **Logs** — live tail of `/var/log/monitoring-pipeline.log`
- **Config** — in-browser editor for `client.json`; saves and optionally restarts affected services

Signal strength is read from the log file rather than querying the modem directly to avoid serial port conflicts between web_ui and uploader.

### gsm-pin-unlock.py

One-shot script that runs as a systemd service before the GSM data connection. Sends `AT+CPIN=<pin>` over the serial port to unlock the SIM before pppd claims the port. Only relevant if using pppd (not currently in use).

### SMS Remote Config

The uploader reads all pending SMS messages between upload cycles. Supported commands:

| Command | Effect |
|---------|--------|
| `SET key=value` | Update one or more config keys |
| `STATUS` | Reply with current mode, threshold, interval, quality |

Keys settable via SMS: `motion_threshold`, `motion_cooldown`, `detection_interval`, `image_interval`, `image_quality`, `mode`, `webp_compress`, `webp_quality`. Changing `mode` triggers an automatic recorder restart. Changes are written to `client.json` immediately. The Pi replies to the sender's number.

## Config File (`config/client.json`)

```json
{
  "server_url":    "http://...",
  "device_id":     "rodent2",
  "outbox_dir":    "/outbox",
  "uploaded_dir":  "/uploaded",
  "gsm_device":    "/dev/ttyUSB0",
  "gsm_pin":       "...",
  "gsm_apn":       "...",
  "gsm_number":    "+...",
  "poll_interval": 10,
  "max_retries":   3,
  "retry_delay":   10,
  "webp_compress": true,
  "webp_quality":  80,
  "recording": {
    "mode":               "image_motion",
    "camera_id":          0,
    "width":              1280,
    "height":             720,
    "motion_threshold":   0.015,
    "detection_interval": 1,
    "motion_cooldown":    60,
    "motion_debug":       false,
    "image_interval":     30,
    "image_quality":      75
  }
}
```

`client.json` is gitignored — never commit it (contains SIM PIN and server URL). Use `config/client.json.template` as the starting point for new deployments.

## Tech Stack

| Component | Choice |
|-----------|--------|
| Language | Python 3 |
| Camera | `rpicam-still` (Raspberry Pi camera stack) |
| Motion detection | Pillow (`ImageChops.difference`, pixel histogram) |
| GSM modem | AT commands over `pyserial` (`serial.Serial`) |
| Web dashboard | Flask |
| Process management | systemd |
| Remote access | Tailscale |
| Log output | `/var/log/monitoring-pipeline.log` + journald |

## Known Constraints and Gotchas

- **SIM800 HTTP buffer**: Max upload size is ~300 KB. WebP compression is recommended to stay under this limit for 1280×720 captures.
- **Serial port exclusivity**: Only one process should hold the serial port open at a time. The web_ui reads signal strength from the log file rather than the modem for this reason.
- **USB device naming**: `/dev/ttyUSB0` is assigned at boot and should be stable, but if multiple USB serial devices are present the number can change. Check with `ls /dev/ttyUSB*` if the modem stops responding after adding hardware.
- **2G only**: The SIM868/SIM800C is a 2G module. Requires a SIM card and carrier that still supports 2G (GPRS). Not compatible with 3G/4G-only carriers.
- **GPIO UART vs USB**: See `docs/gsm-hat-setup.md` for the full history and configuration differences.
- **rtscts must be False for USB**: Hardware flow control (`rtscts=True`) blocks communication when using the CP2102 USB-to-UART path. It was previously `True` and worked on GPIO (where CTS was wired), but must be `False` for USB.
- **Atomic writes**: The recorder always writes the `.json` sidecar before renaming the `.jpg` into place. If you ever see `.jpg` files in `/outbox` without a matching `.json`, the recorder was interrupted mid-write.
