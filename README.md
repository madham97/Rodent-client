# Monitoring Pipeline — Edge Client

Runs on a Raspberry Pi. Captures images (on motion or on a timer), uploads them to the server over a SIM868/SIM800C GSM modem, and exposes a local web dashboard for management.

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
| GSM serial device | `/dev/serial0` | GPIO UART. Use `/dev/ttyUSB0` for USB mode — see `docs/gsm-hat-setup.md` |
| SIM PIN | *(blank)* | Entered silently with confirmation. Stored in `client.json` (mode 640) |
| Recording mode | `image_motion` | `1` = capture on motion, `2` = capture on a timer (PNG when thermal fusion is on, JPEG otherwise) |
| Web UI password | `monitoring` | Password for the dashboard at port 8080 |
| Start services now | yes | Enables and starts all systemd services |

The installer:
- Installs Python dependencies into a venv at `/opt/Rodent-client/venv`
- Writes `/opt/Rodent-client/config/client.json` and `webui.env`
- Frees the GPIO serial port (disables serial getty, removes kernel console — harmless if using USB mode)
- Installs Tailscale for remote access
- Installs and optionally starts the three core systemd services (recorder, uploader, webui)

Re-running `install.sh` is safe — it loads existing values as defaults.

## How it works

```
picamera2 stream (continuous, 1.5s pre-trigger ring buffer)
  + MI48 thermal stream (continuous)
                    ↓
      motion seen at time T → both buffers queried for the
      best-synchronised pair of frames captured at T
                    ↓
   /outbox/image_YYYYMMDDThhmmssZ.png   (RGBA: visible + thermal in alpha)
             + .json sidecar (device_id, mode, timestamp, motion_score,
                              thermal_min_c/max_c/avg_c, thermal_skew_ms)
                    ↓
              uploader reads oldest image + sidecar
              → re-encodes to WebP (alpha preserved) to fit the modem's 300KB limit
              → multipart POST to server /upload
              → moves image to /uploaded, deletes sidecar
```

Both cameras stream continuously and captures are served from their buffers, rather than a
capture being triggered on demand. This is what makes the two frames simultaneous *and* makes
the triggered frame the one from the moment motion occurred: a fresh `rpicam-still` process
spends ~1s initialising the camera before it exposes, so an on-demand capture shows the scene
1.5–3s after the animal moved. The filename timestamp is the frame's **exposure** time, not the
time it was written.

Captures with no properly-timed thermal counterpart are **discarded**, not saved unpaired — see
`thermal_max_skew_s` under [Thermal Camera](#thermal-camera).

## Services

Three core systemd services are installed under the name prefix `monitoring-pipeline-`:

| Service | Role |
|---------|------|
| `recorder` | Captures images to `/outbox` |
| `uploader` | Uploads images from `/outbox` to the server via GSM; manages its own GPRS bearer via AT commands |
| `webui` | Local dashboard at `http://<pi-ip>:8080` |

Two additional service unit files exist in `systemd/` but are not installed by `install.sh`:

| Service | Role |
|---------|------|
| `gsm-pin` | Legacy: one-shot SIM PIN unlock before pppd. Not needed — uploader handles PIN unlock itself. |
| `gsm` | Legacy: establishes a pppd PPP data connection. Not needed — uploader uses AT+HTTPACTION directly. |

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

Config lives at `/opt/Rodent-client/config/client.json`. It can be edited directly or via the web dashboard (Config tab).

Key settings:

**Upload**
- `server_url` — server address (e.g. `http://192.168.1.10:8000` or an ngrok URL)
- `webp_compress` / `webp_quality` — re-encode JPEGs as WebP before upload to reduce transfer size
- `poll_interval` — seconds between outbox checks (default 10)
- `max_retries` / `retry_delay` — upload retry behaviour
- `http_action_timeout` — **ceiling** on waiting for the modem's upload verdict (default 180), not a fixed wait. The uploader polls `AT+HTTPSTATUS` and keeps waiting only while bytes are still moving, so a slow-but-healthy transfer runs to completion while a stuck one is abandoned in ~25 s. Raise it only if a legitimate upload is being cut off — a 290 KB payload at 2 KB/s needs over two minutes
- `http_action_idle_polls` — consecutive checks showing no progress before an upload is declared stuck (default 3, ~8 s apart)
- `confirm_path` — path used to ask the server whether an image already arrived, after a stall swallowed the modem's reply (default `/annotate/specific/{name}`, where `{name}` is the name the server files it under — `<stem>.jpg` when `webp_compress` is on). Prevents re-sending an image that was in fact delivered; set to `""` to disable

**GSM**
- `gsm_device` — serial port: `/dev/serial0` for GPIO UART mode, `/dev/ttyUSB0` for USB mode (see `docs/gsm-hat-setup.md`)
- `gsm_baud` — modem serial speed (default 115200); must match the modem's `AT+IPR` setting
- `gsm_pin` — SIM PIN (blank if not required)
- `gsm_apn` — data APN for your carrier
- `gsm_number` — phone number of the SIM (e.g. `+447700900123`), shown in the dashboard — useful to note here so you know what number to send SMS config commands to

**Recording**
- `recording.mode` — `image_motion` or `image_interval`
- `recording.capture_backend` — `picamera2` (default) streams the camera continuously with a pre-trigger buffer; `rpicam` runs a fresh `rpicam-still` per capture, costing ~1s of camera init before every exposure. Falls back to `rpicam` automatically if picamera2 can't start
- `recording.camera_fps` — stream frame rate for the `picamera2` backend (default 15)
- `recording.camera_buffer_s` — seconds of full-res frames kept in the pre-trigger buffer (default 1.5, ~3MB per frame). Must be longer than the delay between motion onset and detection firing, or the onset frame is already gone
- `recording.motion_threshold` — fraction of pixels that must change to trigger (0–1, default 0.015)
- `recording.motion_cooldown` — seconds to wait after a capture before checking for motion again
- `recording.detection_interval` — seconds between motion checks. On the `picamera2` backend a check costs ~0.06s instead of ~0.6s, so this can be much shorter than it used to be; keep it below `camera_buffer_s`
- `recording.baseline_frames` — frames averaged into the motion baseline (default 3). Rebuilt after every trigger, and the recorder is blind while it happens
- `recording.image_interval` — seconds between captures in interval mode
- `recording.image_quality` — JPEG quality 1–100 (default 75)
- `recording.image_rotation` — software rotation in degrees clockwise (`0`/`90`/`180`/`270`) to correct how the CSI camera is physically mounted. `rpicam-still` only supports 0/180 in hardware, so 90/270 mounts need this. Unlike `--hflip`/`--vflip`, a rotation preserves left/right handedness — important once this feed is paired with the thermal feed (see [Thermal Camera](#thermal-camera))
- `recording.width` / `recording.height` — capture resolution (default 1280×720)
- `recording.motion_debug` — set `true` to log the motion ratio on every check, useful for tuning `motion_threshold`
- `recording.thermal_enabled` — fuse the GPIO thermal camera into every capture (default `false`, see [Thermal Camera](#thermal-camera))
- `recording.thermal_fps` / `recording.thermal_filters` / `recording.thermal_offset` — thermal sensor stream settings
- `recording.thermal_width` / `recording.thermal_height` — thermal frame size before it's resized to match the visible capture
- `recording.thermal_spi_speed_hz` — SPI clock speed to the thermal sensor (default 2,000,000 = 2 MHz); lower it if the log shows frequent CRC errors (see [Thermal Camera](#thermal-camera))
- `recording.thermal_max_skew_s` — maximum time gap allowed between the visible exposure and the thermal frame fused with it; captures that can't be paired this closely are **discarded**. Defaults to `0.75 / thermal_fps` (83ms at 9fps). The floor is half a thermal frame interval (56ms at 9fps) — raise `thermal_fps` to tighten it (see [Thermal Camera](#thermal-camera))
- `recording.thermal_stall_warn_s` / `recording.camera_stall_warn_s` — log a warning if either stream produces no frames for this long (default 5.0 each)
- `recording.thermal_hflip` / `recording.thermal_vflip` — flip the thermal frame so it agrees with the visible frame's left/right and up/down. The MI48's readout orientation is independent of the CSI camera's, so this needs to be set from an actual test (see [Thermal Camera](#thermal-camera)), not assumed

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

- **Dashboard** — service status, GSM signal strength, image counts for the outbox, uploaded and failed queues, Tailscale SSH address. Each queue has a **Clear**; the failed queue also has a **Requeue** that moves its images back to the outbox to be uploaded again — the normal way to retry images parked during an outage, once the cause is fixed
- **Logs** — live tail of `/var/log/monitoring-pipeline.log`
- **Config** — edit and save `client.json` in-browser

## Testing

Test image capture and upload (no GSM modem needed — posts directly over HTTP):
```bash
cd /opt/Rodent-client
venv/bin/python3 pi-client/test_record_upload.py
```

This captures a test image with `rpicam-still` (or falls back to a 1×1 dummy JPEG if no camera is attached), POSTs it to the configured server URL, and reports success or failure.

Test the uploader's failure handling (no modem, no network, no server — runs anywhere):
```bash
venv/bin/python3 pi-client/test_uploader_recovery.py
```

This pins down behaviour that is easy to regress because the faults all look alike in a log: a stalled serial link, a delivered image whose reply was lost, and a genuinely rejected image must be handled differently, and only the last one may be parked in `failed/`.

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

## Thermal Camera

An optional Waveshare MI48 80×62 thermal camera can be attached via GPIO (I2C + SPI), alongside the CSI ribbon IR camera the recorder already uses. The two cameras are independent hardware (different buses — CSI vs I2C/SPI) so they can run at the same time with no conflict.

**Combined feed (recorder integration)**

Set `recording.thermal_enabled: true` in `client.json` and restart the recorder service. Both the MI48 thermal sensor and the visible camera then stream continuously in background threads inside `recorder.py`, each buffering recent frames. A capture picks the best-synchronised **pair** from the two buffers — considering every visible frame near the moment of interest and choosing the one that sits closest to a thermal frame, rather than taking one frame and accepting whatever thermal frame is newest:

- Output changes from `image_*.jpg` to `image_*.png` — an RGBA image where R/G/B is the visible frame and **the normalized thermal frame is stored as the alpha channel**.
- The sidecar JSON gets `format: "rgba_thermal_alpha"` plus `thermal_min_c` / `thermal_max_c` / `thermal_avg_c` — the actual °C range the alpha byte was normalized from, needed to reconstruct real temperatures server-side: `temp_c = thermal_min_c + (alpha / 255) * (thermal_max_c - thermal_min_c)`.
- The sidecar also carries `thermal_skew_ms` — the signed time gap between the two fused frames (+ = thermal newer). Sync is therefore auditable from the uploaded data alone; filter on it server-side if a study needs tighter simultaneity than the recorder enforced.
- **Captures that can't be paired within `thermal_max_skew_s` are discarded, not saved unpaired.** For a moving subject a mistimed pair is wrong data rather than degraded data, so no image and no sidecar is written and nothing is uploaded. Each drop is logged with a running count (`Discarded unsynced capture ... N dropped so far`) — watch that count in the field, since a persistent thermal fault shows up as silence in the outbox rather than an error.
- Expect a few dropped captures at startup while the thermal sensor warms up. That is normal.
- The uploader's WebP compression (`webp_compress`) applies to fused PNGs too and preserves the alpha channel, keeping combined captures well under the SIM800's 300KB upload limit.

**How close is "simultaneous"?** The limit is the thermal sensor's frame rate: at `thermal_fps: 9` frames are 111ms apart, so nearest-neighbour pairing can only ever land within half an interval (~56ms) — there is no thermal data in between to pair with. Measured on real captures: typically ±10–60ms. Raising `thermal_fps` tightens this (25fps → ~20ms floor) but stresses the SPI link; see CRC errors below.

**Aligning the two feeds (rotation and mirroring)**

The CSI ribbon camera and the GPIO thermal sensor are independent hardware with independent, arbitrary mounting/readout orientations — there's no default that's correct for every unit. Two separate corrections may be needed, and they're not interchangeable:

- **`recording.image_rotation`** (0/90/180/270, applies to the visible feed) — corrects the CSI camera's physical mounting angle, in software, on both capture backends. A rotation *preserves* left/right handedness.
- **`recording.thermal_hflip`** / **`thermal_vflip`** (applies to the thermal feed) — corrects the MI48's readout orientation, which is independent of the camera's mounting. A flip *reverses* left/right handedness — that's a fundamentally different kind of correction than a rotation, and one doesn't substitute for the other.

Figure out both empirically, don't guess:
1. Set `image_rotation` first: capture a frame and check the visible image is upright. Test all 4 values if needed.
2. Once the visible frame is upright, run `record_combined.py` and raise **one specific hand** (note which). Check the RGB pane: for a camera facing you (not a mirror/selfie view), your right hand should appear on the image's *left* side — that's the correct, unmirrored convention for a monitoring camera. If it's backwards, that means the visible feed itself needs a hardware/software hflip too (not covered by `image_rotation`).
3. Compare the thermal pane to the now-correct RGB pane in the same frame: does the warm blob (your raised hand) line up on the same side as the RGB hand? If not, toggle `thermal_hflip` (or `thermal_vflip` if it's an up/down mismatch instead) and re-test until they agree.

**CRC errors / dropped thermal frames**

The MI48's SPI link is CRC-checked in hardware, and jumper-wire connections routinely corrupt a transfer here and there — you'll see `MI48 Frame CRC error` in the log. `read_frame()` (`pi-client/thermal/thermal_common.py`) now retries a corrupted read a couple of times before giving up, and the default SPI clock was dropped from 4 MHz to 2 MHz, which should clear up occasional errors. If they're still frequent:
- Lower `recording.thermal_spi_speed_hz` further (e.g. `1000000` for 1 MHz) — slower but more reliable over jumper wires.
- Shorten the SPI wiring (MOSI/MISO/CLK) or use twisted-pair/ribbon jumpers instead of loose single-strand wires.
- Move to a direct PCB mount if you need to go back up to higher speeds.

**Standalone thermal-only tools** (unaffected by the above, useful for testing the sensor in isolation):

| Script | Purpose |
|--------|---------|
| `pi-client/thermal/snapshot.py` | Capture one frame → PNG |
| `pi-client/thermal/record_video.py` | Record N seconds → MP4 (thermal only) |
| `pi-client/thermal/record_combined.py` | Record both cameras simultaneously → single side-by-side MP4 (RGB left, thermal right) |

Quick test (run from a writable directory):

```bash
cd ~
python3 /opt/Rodent-client/pi-client/thermal/snapshot.py
# opens /opt/Rodent-client/thermal.png

python3 /opt/Rodent-client/pi-client/thermal/record_combined.py --duration 10
# opens /opt/Rodent-client/combined.mp4 — RGB feed and thermal feed recorded at the same
# time (rpicam-vid + thermal thread in parallel) and muxed side by side
```

Full wiring, config, and installation instructions: [`docs/thermal-camera-setup.md`](docs/thermal-camera-setup.md)

## Further Reading

- [`docs/project-overview.md`](docs/project-overview.md) — detailed architecture, data flow, component internals, and known constraints
- [`docs/gsm-hat-setup.md`](docs/gsm-hat-setup.md) — full GPIO vs USB setup guide for the Waveshare GSM HAT, including debugging history
- [`docs/thermal-camera-setup.md`](docs/thermal-camera-setup.md) — wiring, config.txt changes, pysenxor installation, and troubleshooting for the MI48 thermal camera

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Uploader exits immediately | Check `gsm_device` in config (`/dev/serial0` for GPIO, `/dev/ttyUSB0` for USB); verify SIM PIN |
| Images not appearing on server | Check `server_url` in config; verify server is reachable (`curl <url>/health`) |
| `/outbox` growing, nothing uploaded | Check GSM signal in dashboard; check uploader logs |
| Log repeats `Modem link fault ... (+HTTPSTATUS: POST,0,0,0)` | The modem gave no verdict for the upload. `POST,0,0,0` only means the HTTP engine is **idle** — it does *not* mean the upload failed, and the POST has often already been delivered. Usually a USB endpoint stall (`dmesg \| grep -c 'urb stopped'`); work the physical layer first. Images stay queued and delivery is confirmed against the server before any re-send. See [`docs/gsm-hat-setup.md`](docs/gsm-hat-setup.md#telling-a-dead-data-path-apart-from-a-dead-modem) |
| Log repeats `Serial read path is dead (USB endpoint stalled)` | A GPRS data attempt halted the CP2102 bulk-IN endpoint (`dmesg \| grep 'urb stopped'`); the uploader re-opens the port itself. Frequent stalls with uploads otherwise working are worth reporting, not fatal |
| Motion not triggering | Lower `motion_threshold` or enable `motion_debug` to see live ratios |
| Dashboard not accessible | Check webui service is running; confirm Tailscale is authenticated |
| SMS commands not working | Confirm modem is registered (`AT+CREG?`); check uploader log for `SMS from` lines; ensure SIM can receive SMS |
| Nothing appearing in `/outbox` with thermal on | Look for `Discarded unsynced capture` in the log — captures are dropped when visible and thermal frames can't be paired within `thermal_max_skew_s`. A stalled sensor (`Thermal stream stalled`) silences the outbox rather than erroring |
| Log shows `Falling back to rpicam-still capture` | picamera2 failed to start, so the recorder is running the slow per-capture path (~1s camera init before every exposure, no pre-trigger buffer). See the numpy/simplejpeg note below |
| `picamera2` import fails with `numpy.dtype size changed` | Debian's `python3-simplejpeg` is compiled against the numpy 1.x ABI, but numpy 2.x is installed in `/usr/local/lib/python3.11/dist-packages` and shadows Debian's. Do **not** downgrade numpy — opencv and the thermal code need 2.x. Instead install a numpy-2-compatible simplejpeg into the venv, where it shadows the system package: `sudo venv/bin/pip install --upgrade --force-reinstall --only-binary :all: simplejpeg`. Re-check with `venv/bin/python3 -c "import picamera2"`. This is worth re-testing after any rebuild of the venv or system packages |
