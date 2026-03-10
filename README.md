# Monitoring Pipeline (Edge Device)

This README is for the Raspberry Pi (or other edge device) that records and uploads video/audio. It does **not** cover the server side – only the client.

## First-time setup

1. **Copy the project** to the Pi and make it owned by a normal user:
   ```bash
   sudo cp -r monitoring-pipeline /opt/monitoring-pipeline
   sudo chown -R $(whoami) /opt/monitoring-pipeline
   ```

2. **Create the work directories** (these are the defaults, you can change them later):
   ```bash
   mkdir -p /outbox /uploaded
   ```

3. **Install system requirements**:
   ```bash
   cd /opt/monitoring-pipeline
   sudo bash install.sh          # installs python deps and sets up venv
   ```

4. **Edit the config file** at `/opt/monitoring-pipeline/config/client.json` and set at least the `server_url` entry to point at your server. You can also tune polling/retry settings.

5. **(Optional) enable recording** by adding a `recording` section in the same JSON. See the "Recorder" section below.


## Quick start – client side

With the prerequisites done you can enable and start the services:

```bash
sudo systemctl daemon-reload
sudo systemctl enable monitoring-pipeline-uploader.service monitoring-pipeline-recorder.service
sudo systemctl start monitoring-pipeline-uploader.service monitoring-pipeline-recorder.service
```

View the logs to see what the services are doing:

```bash
# follow uploader activity
journalctl -u monitoring-pipeline-uploader.service -f

# in another terminal follow recorder activity (if enabled)
journalctl -u monitoring-pipeline-recorder.service -f
```

Once the services are running, putting a video file into `/outbox` should trigger an upload within a few seconds; the file will then appear in `/uploaded`.

> **Tip:** if you ever reboot the Pi the services will start automatically, so following the first‑time setup above will leave the device running on its own.


## Recorder (audio/video capture)

The recorder service can create timestamped MP4 chunks from a camera or microphone and put them in `/outbox` automatically.

To enable it, add this block to `client.json`:

```json
"recording": {
  "enabled": true,
  "device": "hw:0",
  "chunk_duration": 60,
  "sample_rate": 44100,
  "channels": 1,
  "bitrate_kbps": 128,
  "ffmpeg_path": "ffmpeg",
  "min_size_bytes": 1024
}
```

Defaults are mostly sensible; change `chunk_duration` or `device` as needed. Make sure `ffmpeg` is installed (`sudo apt install ffmpeg`).

The recorder writes `.tmp` files while recording and renames them to `.mp4` when done; the uploader ignores files ending in `.tmp`.


## Configuration overview

The client uses a JSON file at `/opt/monitoring-pipeline/config/client.json`.

Example:
```json
{
  "outbox_dir": "/outbox",
  "uploaded_dir": "/uploaded",
  "server_url": "http://localhost:5000",
  "poll_interval": 10,
  "retry_delay": 300,
  "max_retries": 3
}
```

See the file itself for comments and other options (GSM modem settings, etc.).


## Testing and troubleshooting

- You can **stop**, **start** or **restart** the services while debugging:
  ```bash
  sudo systemctl stop monitoring-pipeline-uploader.service monitoring-pipeline-recorder.service
  sudo systemctl start monitoring-pipeline-uploader.service monitoring-pipeline-recorder.service
  sudo systemctl restart monitoring-pipeline-uploader.service monitoring-pipeline-recorder.service
  ```
  use `status` to check their state.

- Test a manual upload:
  ```bash
  curl -F "file=@/outbox/test.mp4" http://SERVER:5000/upload
  ```
- If uploads stop:
  * Ensure uploader service is active (`systemctl status`).
  * Check `/outbox` and `/uploaded` permissions (`ls -la`).
  * Verify the server address is correct (`curl /health`).
- Logs are available with `journalctl` for both services.


## Notes

- Services run as the owner of `/opt/monitoring-pipeline`; you do not need a special user.
- The recorder is optional; leave `recording.enabled` set to `false` to disable.
- **GSM modem**: at present the uploader expects a working GSM modem (configured via `gsm_device` and `gsm_pin` in the JSON). If no modem is attached the service will exit with a runtime error like
  ````
  RuntimeError: GSM modem initialization failed - cannot proceed without GSM
  ````
  Make sure the hardware is present and the device path is correct.
- This device should always be able to reach the server (via wifi, ethernet, or GSM).


## License

This project is provided as-is.
