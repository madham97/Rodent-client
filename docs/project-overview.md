# Project Overview — Monitoring Pipeline Edge Client

## Purpose

A Raspberry Pi-based surveillance client that captures images (on motion or on a fixed timer), uploads them to a remote server over a 2G GSM modem, and exposes a local web dashboard for management and monitoring. Designed for remote/unattended deployments where no fixed internet connection is available — the GSM modem is the only data link.

## Repository Layout

```
Rodent-client/
├── pi-client/
│   ├── recorder.py          # Image capture service (visible + thermal fusion)
│   ├── uploader.py          # GSM upload service
│   ├── web_ui.py            # Flask management dashboard
│   ├── thermal/             # MI48 thermal sensor: streaming, snapshot, combined recording
│   └── test_record_upload.py
├── data/                    # Runtime image queue (gitignored)
│   ├── outbox/              # Captured, awaiting upload
│   ├── uploaded/            # Confirmed delivered
│   └── failed/              # Un-sendable or server-rejected; kept for a later re-send
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
picamera2 (or rpicam-still) [+ MI48 thermal]
    │
    ▼
data/outbox/image_YYYYMMDDTHHMMSSz.png   ← RGBA fused frame (thermal in alpha); .jpg when
                                            thermal fusion is off. Written atomically.
data/outbox/image_YYYYMMDDTHHMMSSz.json  ← sidecar: device_id, mode, timestamp, motion_score,
                                            and when fused: format, thermal_min/max/avg_c
    │
    ▼ uploader polls outbox (oldest-first)
    │
    ├─ re-encodes to WebP, adaptively lowering quality/scale to fit the modem's buffer
    │
    ▼
multipart/form-data POST → server /upload  (via SIM800 AT+HTTP stack)
    │
    ├─ HTTP 200 ──────────────► data/uploaded/   (sidecar deleted)
    ├─ modem/link fault ──────► stays in outbox, delivery confirmed against the server
    │                           on the next cycle before any re-send
    └─ server error / too big ► data/failed/     (kept for a later re-send)
```

Images are written atomically: the sidecar `.json` is always written before the image is renamed into place, so the uploader never sees an image without its metadata.

An image whose payload cannot be squeezed under the modem's HTTP buffer even after adaptive compression is parked in `data/failed/` — never dropped into `uploaded/`, which would make it look delivered.

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
3. Opens a GPRS bearer (`AT+SAPBR`) with the configured APN, verifying the result with `AT+SAPBR=2,1` rather than trusting the reply
4. Polls the outbox for the oldest image, builds a multipart body, sends it via `AT+HTTP`
5. Between uploads, checks for incoming SMS messages — operators can update config or query status remotely via SMS commands (`SET key=value`, `STATUS`)

**Failure handling.** The uploader distinguishes three outcomes, because they need opposite responses:

| Outcome | Meaning | Response |
|---------|---------|----------|
| HTTP status returned | The server saw the image and judged it | 200 → `uploaded/`; other codes → retry, then park in `failed/` |
| `ModemLinkError` | The modem never reported a verdict — we don't know what happened | Keep the image queued, escalate link recovery, confirm delivery next cycle |
| `toobig` | Won't fit even after adaptive compression | Park in `failed/` immediately |

A link fault must never be treated as a bad image: doing so drains the outbox into `failed/` one image per cycle during any outage, and makes a transport problem look like a pile of bad captures.

**Recovering a one-way serial link.** A GPRS data attempt can halt the CP2102's USB bulk-IN endpoint, after which the port accepts writes but never delivers another byte — every AT command appears to return an empty response forever. The uploader detects the empty response, re-opens the port (the only thing that clears it), and retries once. Commands that must not be duplicated (`AT+HTTPACTION`, SMS send) opt out via `heal=False`. See `docs/gsm-hat-setup.md` for the full diagnosis; the root cause was a faulty USB port.

**Delivery confirmation.** If a stall swallows the modem's `+HTTPACTION` line, the POST may already have been delivered and answered 200. Before re-sending, the uploader asks the server whether the image is already there (`confirm_path`, a small bodyless GET). This runs at the *start* of the next cycle — after link recovery, on a working link — rather than immediately after the fault on the link that just failed.

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
  "device_id":     "rodent",
  "outbox_dir":    "/opt/Rodent-client/data/outbox",
  "uploaded_dir":  "/opt/Rodent-client/data/uploaded",
  "failed_dir":    "/opt/Rodent-client/data/failed",
  "gsm_device":    "/dev/ttyUSB0",
  "gsm_baud":      115200,
  "gsm_pin":       "...",
  "gsm_apn":       "...",
  "gsm_number":    "+...",
  "poll_interval": 10,
  "max_retries":   3,
  "retry_delay":   10,
  "http_action_timeout": 180,
  "confirm_path":  "/annotate/specific/{name}",
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
- **USB endpoint stalls are a physical fault, not a software one**: if the log shows repeated `Modem link fault ... (+HTTPSTATUS: POST,0,0,0)` while the modem is otherwise healthy (registered, bearer up, `AT+CBC` ~4000 mV), check `dmesg | grep -c 'urb stopped'` — two new lines per data attempt means the USB read endpoint is halting. Work the physical layer: a different USB port, straight into the Pi rather than through a hub, a different cable. This was diagnosed at length once already; `docs/gsm-hat-setup.md` records what was tested and ruled out so it need not be repeated.
- **GPIO UART is not an available fallback on this unit**: the thermal camera occupies the GPIO header, so the USB path is the only option.
- **USB device naming**: `/dev/ttyUSB0` is assigned at boot and should be stable, but if multiple USB serial devices are present the number can change. Check with `ls /dev/ttyUSB*` if the modem stops responding after adding hardware.
- **2G only**: The SIM868/SIM800C is a 2G module. Requires a SIM card and carrier that still supports 2G (GPRS). Not compatible with 3G/4G-only carriers.
- **GPIO UART vs USB**: See `docs/gsm-hat-setup.md` for the full history and configuration differences.
- **rtscts must be False for USB**: Hardware flow control (`rtscts=True`) blocks communication when using the CP2102 USB-to-UART path. It was previously `True` and worked on GPIO (where CTS was wired), but must be `False` for USB.
- **Atomic writes**: The recorder always writes the `.json` sidecar before renaming the `.jpg` into place. If you ever see `.jpg` files in `/outbox` without a matching `.json`, the recorder was interrupted mid-write.
