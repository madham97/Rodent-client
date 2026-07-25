# Monitoring Pipeline Stakeholder Guide

## Purpose

This document is a newcomer-friendly guide to the current monitoring pipeline. It explains how the two active repositories fit together, what each part does, how the field device is configured, and how data moves from a Raspberry Pi in the field to server-side detection, annotation, and model training.

The current active repos are:

| Repo | Local path | Role |
|---|---|---|
| `Rodent-client` | `C:\Users\hamma\Python\Rodent-client` | Edge/client software that runs on the Raspberry Pi field device. |
| `Server` | `C:\Users\hamma\Python\Server` | Backend server that receives images, runs YOLO inference, supports annotation, exports datasets, and trains/promotes models. |

`Server2` is an older/alternate server repo and is intentionally not covered here.

## System At A Glance

The system is a remote image monitoring pipeline for rodent/human detection.

1. A Raspberry Pi captures images using its camera.
2. The Pi stores images locally in an outbox directory with a small metadata sidecar file.
3. A GSM modem uploads the images to the server over cellular data.
4. The server saves uploaded images.
5. Server-side processing can run YOLOv8 object detection on uploaded images.
6. A browser annotation UI lets a human label images as `rat` or `human`.
7. Annotated images can be exported to a YOLO dataset.
8. The server can train a new model and promote it to become the live model.

High-level flow:

```text
Raspberry Pi camera
  -> Rodent-client recorder
  -> /outbox image + metadata
  -> Rodent-client uploader
  -> GSM/SIM800 HTTP POST
  -> Server /upload
  -> uploads/
  -> YOLO inference, annotation, dataset export, training
```

## Repository Responsibilities

### Rodent-client

`Rodent-client` is the field-device application. It is designed to run directly from `/opt/Rodent-client` on a Raspberry Pi.

Primary capabilities:

- Capture images from a Raspberry Pi camera.
- Support two recording modes:
  - `image_motion`: capture when motion is detected.
  - `image_interval`: capture on a fixed timer.
- Write image metadata such as device id, recording mode, capture timestamp, and motion score.
- Upload images to the server using a SIM868/SIM800-compatible GSM modem.
- Optionally compress JPEGs to WebP before upload to reduce cellular transfer size.
- Expose a local web dashboard on port `8080`.
- Allow remote configuration changes by SMS.
- Use systemd services so the pipeline starts automatically after boot.
- Use Tailscale for remote SSH/dashboard access when available.

Important files:

| Path | Purpose |
|---|---|
| `pi-client/recorder.py` | Captures images and writes them to the outbox. |
| `pi-client/uploader.py` | Uploads images to the server through the GSM modem. |
| `pi-client/web_ui.py` | Flask dashboard for status, logs, config, and service actions. |
| `pi-client/test_record_upload.py` | Test script for camera capture and direct upload. |
| `pi-client/test_uploader_recovery.py` | Offline tests for the uploader's failure handling — no modem or network needed. |
| `config/client.json.template` | Starting template for device configuration. |
| `install.sh` | First-time Pi installer. |
| `systemd/` | systemd unit files for recorder, uploader, and web UI. |
| `docs/gsm-hat-setup.md` | Hardware notes for the Waveshare GSM HAT. |

### Server

`Server` is the backend application. It is a FastAPI server that receives field uploads and manages the model improvement loop.

Primary capabilities:

- Receive images from one or more Pi devices through `POST /upload`.
- Accept JPEG, PNG, or WebP uploads. WebP is converted to JPEG when received.
- Save all original received images to `uploads/`.
- Log every upload to `upload_log.txt`.
- Run background YOLOv8 inference when processing is enabled.
- Store inference outputs in `processed/`.
- Provide a browser annotation UI at `/annotate`.
- Store human annotation labels in YOLO format.
- Export annotated data into a YOLO training dataset.
- Start and monitor model training.
- Stage trained weights as `models/candidate.pt`.
- Promote a candidate model to `models/best.pt` only if it passes a minimum mAP50 gate.
- Archive previous live models before replacing them.

Important files:

| Path | Purpose |
|---|---|
| `app.py` | FastAPI entry point and background upload watcher. |
| `config.py` | Server paths, model settings, thresholds, and training defaults. |
| `inference.py` | YOLO inference wrapper. |
| `trainer.py` | Background YOLO training logic. |
| `state.py` | Runtime state for processing toggle, processed files, and failures. |
| `routers/upload.py` | `/upload` endpoint. |
| `routers/annotate.py` | Annotation UI/API endpoints. |
| `routers/export.py` | Dataset export endpoints. |
| `routers/train.py` | Training lifecycle endpoints. |
| `routers/infer.py` | Ad-hoc inference test endpoint. |
| `static/annotate.html` | Browser annotation UI. |
| `classes.json` | Detection classes, currently `rat` and `human`. |

## Field Device Details

The field device is a Raspberry Pi-based unit with:

- Raspberry Pi camera stack using `rpicam-still`.
- A Waveshare GSM/GPRS/GNSS HAT.
- SIM868 module, compatible with SIM800C AT commands.
- CP2102 USB-to-UART chip for USB serial mode.
- A SIM card with 2G/GPRS data service.

The current GSM setup is USB mode:

| Item | Current detail |
|---|---|
| Jumper setting | Both jumpers on position `A`. |
| USB port | Raspberry Pi 4B USB 3.0 blue port. |
| USB cable | Must be a data cable, not charge-only. |
| Serial device | `/dev/ttyUSB0`. |
| Flow control | `rtscts=False` is required. |

The alternative GPIO UART mode uses:

| Item | Detail |
|---|---|
| Jumper setting | Both jumpers on position `B`. |
| Serial device | `/dev/serial0`. |
| Extra setup | Disable serial console/getty so the kernel does not claim the port. |

The installer handles the GPIO serial-console changes, but USB mode does not need them. They are harmless if applied.

## Client Runtime Services

On the Pi, the active services are:

| Service | Role |
|---|---|
| `monitoring-pipeline-recorder` | Runs `recorder.py` and creates images in `/outbox`. |
| `monitoring-pipeline-uploader` | Runs `uploader.py`, manages the GSM modem, uploads images, and checks SMS commands. |
| `monitoring-pipeline-webui` | Runs `web_ui.py` and exposes the dashboard on port `8080`. |

Two legacy service files also exist:

| Service | Status |
|---|---|
| `monitoring-pipeline-gsm-pin` | Legacy one-shot SIM unlock for older PPP mode. Not needed in the current uploader path. |
| `monitoring-pipeline-gsm` | Legacy PPP data connection. Not needed because the uploader uses the modem HTTP stack directly. |

Useful operational commands on the Pi:

```bash
sudo systemctl status monitoring-pipeline-recorder
sudo systemctl status monitoring-pipeline-uploader
sudo systemctl status monitoring-pipeline-webui

journalctl -u monitoring-pipeline-recorder -f
journalctl -u monitoring-pipeline-uploader -f

sudo systemctl restart monitoring-pipeline-recorder
sudo systemctl restart monitoring-pipeline-uploader
```

## Client Configuration

The live client configuration is:

```text
/opt/Rodent-client/config/client.json
```

The installer creates this file from prompts, and the web dashboard can edit it later. It contains operational secrets such as the SIM PIN, so it should not be committed.

Key top-level settings:

| Setting | Meaning |
|---|---|
| `server_url` | Base server URL, for example `http://192.168.1.10:8000` or an ngrok URL. The uploader posts to `<server_url>/upload`. |
| `device_id` | Human-readable identifier for the Pi. Sent with every upload. |
| `outbox_dir` | Directory where the recorder places pending images. Default `/outbox`. |
| `uploaded_dir` | Directory where successfully uploaded images are moved. Default `/uploaded`. |
| `gsm_device` | Serial modem path, usually `/dev/ttyUSB0` for current USB mode or `/dev/serial0` for GPIO mode. |
| `gsm_pin` | SIM PIN, blank if not required. |
| `gsm_apn` | Carrier APN for GPRS data. |
| `gsm_number` | SIM phone number, useful for sending SMS config commands. |
| `poll_interval` | Seconds between uploader outbox checks. |
| `max_retries` | Upload retry count per image. |
| `retry_delay` | Delay between retries. |
| `webp_compress` | Whether JPEGs are converted to WebP before upload. |
| `webp_quality` | WebP quality if compression is enabled. |

Recording settings live under `recording`:

| Setting | Meaning |
|---|---|
| `mode` | `image_motion` or `image_interval`. |
| `camera_id` | Camera index passed to `rpicam-still`. |
| `width`, `height` | Full-resolution capture size. Current template uses `1280x720`. |
| `rpicam_still_path` | Path/command for `rpicam-still`. |
| `min_size_bytes` | Rejects captures smaller than this threshold. |
| `motion_threshold` | Fraction of changed pixels needed to trigger motion capture. |
| `detection_interval` | Seconds between low-resolution motion checks. |
| `motion_cooldown` | Cooldown after a motion-triggered capture. |
| `detection_width`, `detection_height` | Low-resolution frame size used for motion detection. |
| `temporal_alpha` | Running-average weight used to reduce false triggers. |
| `motion_debug` | Logs motion ratios for tuning. |
| `image_interval` | Seconds between interval-mode captures. |
| `image_quality` | JPEG capture quality. |

## Image Capture And Upload Flow

The recorder writes images atomically:

1. Generate a filename like `image_YYYYMMDDTHHMMSSZ.jpg`.
2. Capture the full-resolution image to a temporary file.
3. Write a `.json` sidecar with metadata.
4. Rename the temporary image into place.

The sidecar contains:

| Field | Meaning |
|---|---|
| `device_id` | Which Pi captured the image. |
| `mode` | Capture mode at the time of capture. |
| `timestamp` | UTC capture time. |
| `motion_score` | Motion ratio, mainly useful in motion mode. |

The uploader processes the oldest outbox image first. It builds a `multipart/form-data` request containing the image plus metadata fields and posts it to:

```text
<server_url>/upload
```

If the server returns HTTP `200`, the image is moved from the outbox to `uploaded/`, and the sidecar is deleted.

If the server returns an error, the image is retried and then parked in `failed/` — kept for a later re-send rather than deleted, and out of the queue so it cannot block the images behind it.

If the *modem* fails rather than the server, no verdict is available and the image stays queued. This matters because the two look similar in a log but need opposite handling: parking images on a transport fault would empty the outbox into `failed/` during any outage and make a network problem look like a pile of bad captures. Before re-sending, the uploader asks the server whether that image already arrived, since a lost reply does not mean a lost upload.

The SIM800/SIM868 modem has a practical HTTP body limit of roughly `300 KB`. The uploader re-encodes each image to WebP and, if it still overshoots, progressively lowers quality and then scale until it fits, so WebP compression is important for cellular reliability. An image that cannot be made to fit is parked in `failed/`.

## SMS Remote Configuration

The uploader checks for SMS messages between upload cycles. Supported commands:

| SMS | Effect |
|---|---|
| `STATUS` | Replies with current mode, threshold, interval, and quality. |
| `SET key=value` | Updates one setting. |
| `SET key=value key=value ...` | Updates multiple settings. |

Supported SMS keys:

| Key | Type | Notes |
|---|---|---|
| `motion_threshold` | float | Updates recorder config. |
| `motion_cooldown` | float | Updates recorder config. |
| `detection_interval` | float | Updates recorder config. |
| `image_interval` | float | Updates recorder config. |
| `image_quality` | int | Updates recorder config. |
| `mode` | string | `image_motion` or `image_interval`; triggers recorder restart. |
| `webp_compress` | bool | Updates uploader config. |
| `webp_quality` | int | Updates uploader config. |

Messages from any phone number are currently accepted. The Pi replies to the sender with either `OK: ...` or `ERR: ...`.

## Web Dashboard

The client dashboard runs at:

```text
http://<pi-ip>:8080
```

or, when Tailscale is available:

```text
http://<tailscale-ip>:8080
```

The dashboard uses HTTP Basic Auth. Credentials come from:

```text
/opt/Rodent-client/config/webui.env
```

Dashboard capabilities:

- Show recorder/uploader/web UI service status.
- Show GSM signal strength from logs.
- Show outbox and uploaded image counts/sizes.
- Show Tailscale SSH/dashboard address.
- Tail `/var/log/monitoring-pipeline.log`.
- View and edit `client.json`.
- Start, stop, or restart pipeline services.
- Clear the outbox.

## Server Runtime And Configuration

The server runs with FastAPI/Uvicorn:

```powershell
cd C:\Users\hamma\Python\Server
uvicorn app:app --host 0.0.0.0 --port 8000
```

For field access, the server URL can be exposed with a tunnel such as ngrok:

```powershell
ngrok http --scheme http 8000
```

Server configuration is in `config.py`.

Important server directories:

| Directory/File | Purpose |
|---|---|
| `uploads/` | Original received images. |
| `processed/` | YOLO inference outputs. |
| `annotated/` | Human annotation labels. |
| `failed/` | Images that repeatedly failed processing. |
| `dataset/` | Exported YOLO training dataset. Rebuilt on export. |
| `models/best.pt` | Live model used by inference. |
| `models/candidate.pt` | Most recent trained model, not live until promoted. |
| `models/runs/` | Training run outputs. |
| `models/archive/` | Previous live models archived during promotion. |
| `upload_log.txt` | Upload history. |
| `processed_log.txt` | Files already processed by background inference. |
| `annotation_log.txt` | Images that have been human-annotated. |

Important server settings:

| Setting | Default | Meaning |
|---|---:|---|
| `POLL_INTERVAL` | `5` | Background watcher cycle interval in seconds. |
| `MAX_PROCESS_ATTEMPTS` | `3` | Failures before an image is moved to `failed/`. |
| `MODEL_CONF` | `0.25` | YOLO confidence threshold. |
| `MODEL_IOU` | `0.45` | YOLO non-max suppression IOU threshold. |
| `MODEL_IMGSZ` | `640` | Inference image size. |
| `BASE_MODEL` | `yolov8n.pt` | Default model used for training. |
| `TRAIN_EPOCHS` | `100` | Default training epochs. |
| `TRAIN_IMGSZ` | `640` | Default training image size. |
| `MIN_MAP` | `0.3` | Minimum mAP50 required to promote a candidate model. |

## Server API Summary

Core endpoints:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Health check. |
| `GET` | `/processing` | Returns whether background inference is enabled. |
| `POST` | `/processing?enabled=true|false` | Enable or disable background inference. |
| `POST` | `/upload` | Receive an image from a device. |

Annotation endpoints:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/annotate` | Open browser annotation UI. |
| `GET` | `/annotate/next` | Get a pending image and class list. |
| `GET` | `/annotate/specific/{image_name}` | Load a specific uploaded image. |
| `GET` | `/annotate/image/{image_name}` | Serve the raw image to the UI. |
| `POST` | `/annotate/{image_name}` | Save YOLO-format labels. |
| `GET` | `/annotate/stats` | Show pending/annotated counts and class totals. |

Dataset/training endpoints:

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/dataset/export?val_split=0.2` | Export annotated images to YOLO dataset format. |
| `GET` | `/dataset/stats` | Show whether a dataset has been exported. |
| `POST` | `/train/start` | Start background YOLO training. |
| `GET` | `/train/status` | Poll training state and metrics. |
| `POST` | `/train/promote` | Promote `candidate.pt` to `best.pt` if it passes the metric gate. |
| `POST` | `/infer/test?n=5` | Run test inference on random uploaded images. |

The server starts with background processing disabled. To process uploads automatically, call:

```text
POST /processing?enabled=true
```

This lets the team collect images without immediately running inference, which is useful during field testing or when the model is not ready.

## Model And Annotation Workflow

The model loop is:

1. Collect images from field devices.
2. Open the annotation UI at `/annotate`.
3. Label images with bounding boxes for the current classes:
   - `rat`
   - `human`
4. Submit empty annotations for images that contain no target object. These are useful negative examples.
5. Check progress with `/annotate/stats`.
6. Export the dataset with `/dataset/export?val_split=0.2`.
7. Start training with `/train/start`.
8. Monitor with `/train/status`.
9. Promote with `/train/promote` if metrics are acceptable.

Promotion is intentionally separate from training. A completed training run does not automatically replace the live model. It produces `models/candidate.pt`; promotion copies it to `models/best.pt` and archives the previous model.

## Current Technical Choices

| Area | Choice | Reason |
|---|---|---|
| Client camera | `rpicam-still` | Uses Raspberry Pi camera stack. |
| Client upload | SIM800/SIM868 AT command HTTP stack | Avoids PPP and keeps modem control in the uploader process. |
| Client dashboard | Flask | Lightweight local management UI. |
| Client process manager | systemd | Autostart and operational control on boot. |
| Remote access | Tailscale | Convenient SSH/dashboard access when internet is available. |
| Server API | FastAPI | Simple async API and background task support. |
| Object detection | Ultralytics YOLOv8 | Standard detector with training/export support. |
| Server state | Append-only text logs | Simple, inspectable, low-setup persistence for current scale. |
| Model deployment | Candidate/promote/archive | Prevents accidental replacement of live model and allows rollback. |

## Setup Overview

### Client/Pi setup

Typical first install on the Pi:

```bash
cd /opt/Rodent-client
sudo bash install.sh
```

The installer prompts for:

- Server URL.
- Device ID.
- GSM APN.
- GSM serial device.
- SIM phone number.
- SIM PIN.
- Recording mode.
- Web UI password.
- Whether to start services immediately.

It then:

- Creates `/outbox`, `/uploaded`, and `/var/log/monitoring-pipeline.log`.
- Creates a Python virtual environment.
- Installs Flask, pyserial, Pillow, and related dependencies.
- Writes `config/client.json`.
- Writes `config/webui.env`.
- Installs Tailscale if possible.
- Installs systemd services.
- Optionally enables and starts the recorder, uploader, and web UI.

### Server setup

Typical server setup:

```powershell
cd C:\Users\hamma\Python\Server
py -3.13 -m venv venv
venv\Scripts\activate
pip install torch torchvision --index-url https://download.pytorch.org/whl/nightly/cu126
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```

A CUDA-capable GPU is recommended for training. CPU training is possible but can be very slow.

## Security And Operational Caveats

Important current caveats:

- The server endpoints are unauthenticated. Do not expose the server directly to the public internet without adding authentication or placing it behind a controlled tunnel/network.
- The client dashboard uses Basic Auth, but it should still be kept on a trusted local/Tailscale network.
- SMS configuration accepts messages from any phone number. This is useful during field testing but should be restricted if deployed in a less controlled environment.
- `client.json` contains sensitive values such as the SIM PIN and server URL.
- Upload filenames can overwrite existing files on the server if duplicated.
- The server uses text logs instead of a database. This is simple and appropriate for current low volume, but should be revisited if volume grows significantly.
- The server has one live model slot, `models/best.pt`; there is no per-device or per-location model routing yet.
- Dataset export rebuilds `dataset/` from scratch. Do not manually edit exported dataset contents and expect them to persist.
- The SIM800/SIM868 path depends on 2G/GPRS network availability.
- The modem serial port should be owned by one process at a time; the web UI avoids direct modem access for this reason.

## Common Troubleshooting

| Symptom | Likely check |
|---|---|
| Client captures images but server receives nothing | Check `server_url`, GSM signal, uploader logs, and server `/health`. |
| `/outbox` keeps growing | Uploader may be stopped, modem may not be registered, or cellular upload may be failing. |
| Images exceed upload limit | Reduce `image_quality`, enable `webp_compress`, or lower capture resolution. |
| Motion never triggers | Lower `motion_threshold` or enable `motion_debug` to inspect motion ratios. |
| Too many motion captures | Raise `motion_threshold` or increase `motion_cooldown`. |
| Modem returns no AT responses | Verify USB data cable, USB 3.0 port, jumper position `A`, `/dev/ttyUSB0`, and `rtscts=False`. |
| Server receives uploads but no inference output appears | Enable processing with `POST /processing?enabled=true`. |
| Training does not start | Export a dataset first with `/dataset/export`. |
| Candidate model will not promote | Check `mAP50`; promotion requires `mAP50 >= MIN_MAP`. |

## What A New Stakeholder Should Remember

- `Rodent-client` is the field edge device software.
- `Server` is the backend and model-management software.
- The Pi can run unattended: capture, queue, upload, and expose a local dashboard.
- The current communications path is GSM modem HTTP via AT commands, not PPP.
- The server stores all uploads first; inference is controlled separately with `/processing`.
- The project currently detects two classes: `rat` and `human`.
- The model improvement loop is built into the server: annotate, export, train, promote.
- The system is practical for field testing, but authentication and scaling hardening are future work before broad/public deployment.
