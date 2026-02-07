# Monitoring Pipeline - Video Upload System

A distributed monitoring system that automatically detects, uploads, and archives video files from a Raspberry Pi to a remote server.

## Architecture

```
Pi (Uploader Client)          Server
┌─────────────────────┐    ┌──────────────────┐
│  /outbox/           │    │  HTTP Server     │
│  ├─ video1.mp4      │    │  on port 5000    │
│  └─ video2.mp4      │    │                  │
│         ↓           │    │  /upload endpoint│
│  Uploader Loop      │────┤→ Validates       │
│  (watches, retries) │    │→ Saves to disk   │
│         ↓           │    │→ Returns 200 OK  │
│  /uploaded/         │←───┤                  │
│  ├─ video1.mp4      │    │                  │
│  └─ video2.mp4      │    │ /srv/monitoring  │
└─────────────────────┘    │   /uploads/      │
                           └──────────────────┘
```

## Features

- **Automatic Detection**: Monitors `/outbox/` directory for new video files
- **Retry Logic**: Automatically retries failed uploads with configurable delays
- **File Organization**: Moves uploaded files to `/uploaded/` on success
- **Logging**: Comprehensive logging to syslog and file
- **Systemd Integration**: Starts automatically on device boot
- **Server Validation**: File type validation and size limits
- **Health Checks**: Built-in endpoints for monitoring

## Installation

### Prerequisites

- Python 3.7+
- pip3
- Root access (for systemd installation)

### Pi Client Setup

1. Copy the monitoring-pipeline directory to `/opt/monitoring-pipeline` on your Pi:
   ```bash
   sudo cp -r monitoring-pipeline /opt/monitoring-pipeline
   sudo chown -R monitoring:monitoring /opt/monitoring-pipeline
   ```

2. Install requirements:
   ```bash
   cd /opt/monitoring-pipeline
   sudo bash install.sh
   ```

3. Edit the configuration to point to your server:
   ```bash
   sudo nano /opt/monitoring-pipeline/config/client.json
   ```
   
   Update `server_url` to your server's IP and port:
   ```json
   {
     "server_url": "http://192.168.1.100:5000"
   }
   ```

4. Enable and start the services:
   ```bash
   sudo systemctl enable monitoring-pipeline-uploader.service monitoring-pipeline-recorder.service
   sudo systemctl start monitoring-pipeline-uploader.service monitoring-pipeline-recorder.service
   sudo systemctl status monitoring-pipeline-uploader.service monitoring-pipeline-recorder.service
   ```

5. Check logs:
   ```bash
   sudo journalctl -u monitoring-pipeline-uploader.service -u monitoring-pipeline-recorder.service -f
   ```

### Server Setup

1. Install dependencies:
   ```bash
   pip3 install flask
   ```

2. Create upload directory:
   ```bash
   sudo mkdir -p /srv/monitoring-pipeline/uploads
   sudo chown monitoring:monitoring /srv/monitoring-pipeline/uploads
   ```

3. Copy service file and start:
   ```bash
   sudo cp monitoring-pipeline/systemd/monitoring-pipeline-server.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable monitoring-pipeline-server.service
   sudo systemctl start monitoring-pipeline-server.service
   ```

4. Test the server:
   ```bash
   curl http://localhost:5000/health
   curl http://localhost:5000/stats
   ```

## Configuration

### Client Configuration (`/opt/monitoring-pipeline/config/client.json`) 

| Parameter | Default | Description |
|-----------|---------|-------------|
| `outbox_dir` | `/outbox` | Directory to monitor for videos |
| `uploaded_dir` | `/uploaded` | Directory to move uploaded files |
| `server_url` | `http://localhost:5000` | Server endpoint URL |
| `max_retries` | `3` | Number of upload retry attempts |
| `retry_delay` | `300` | Seconds to wait between retries |
| `poll_interval` | `10` | Seconds between checking for new files |

### Server Configuration

Environment variables (set in systemd service):

| Variable | Default | Description |
|----------|---------|-------------|
| `UPLOAD_DIR` | `/srv/monitoring-pipeline/uploads` | Where to save uploaded files |

## Directory Structure

On Pi:
```
/outbox/              # Monitors this directory
├─ video1.mp4
├─ video2.mp4
└─ video3.avi

/uploaded/            # Successfully uploaded files
├─ video1.mp4
└─ video2.mp4
```

On Server:
```
/srv/monitoring-pipeline/uploads/
├─ 20231215_143022_video1.mp4
├─ 20231215_150145_video2.mp4
└─ 20231216_082330_video3.avi
```

## API Endpoints

### Upload Video
```http
POST /upload
Content-Type: multipart/form-data

file: <binary video data>
```

**Success Response (200):**
```json
{
  "status": "OK",
  "message": "File uploaded successfully",
  "filename": "20231215_143022_video1.mp4",
  "size": 1048576
}
```

**Error Responses:**
- `400`: Missing file, empty filename, or unsupported format
- `413`: File too large (>500MB)
- `500`: Server error

### Health Check
```http
GET /health
```

Response:
```json
{
  "status": "OK",
  "service": "monitoring-pipeline-server"
}
```

### Statistics
```http
GET /stats
```

Response:
```json
{
  "status": "OK",
  "files_count": 42,
  "total_size_mb": 12345.67,
  "upload_dir": "/srv/monitoring-pipeline/uploads"
}
```

## Workflow Details

### Upload Loop (Client Side)

1. **Poll** - Check `/outbox/` every 10 seconds (configurable)
2. **Select** - Pick the oldest file/directory
3. **Upload** - Send HTTP POST to server
4. **Retry** - If failed, wait 5 minutes and retry (up to 3 times)
5. **Archive** - On success, move to `/uploaded/` directory
6. **Repeat** - Go back to step 1

### Server Processing

1. **Receive** - Accept multipart file upload
2. **Validate** - Check file extension and size
3. **Save** - Write file to disk with timestamp prefix
4. **Confirm** - Return 200 OK status
5. **Log** - Record transaction in logs

## Troubleshooting

### Client not uploading

1. Check service status:
   ```bash
   sudo systemctl status monitoring-pipeline-uploader.service
   ```

2. Check logs:
   ```bash
   sudo journalctl -u monitoring-pipeline-uploader.service -n 50
   ```

3. Verify network connectivity:
   ```bash
   curl http://[server-ip]:5000/health
   ```

4. Check file permissions:
   ```bash
   ls -la /outbox/
   ls -la /uploaded/
   ```

5. Test manual upload:
   ```bash
   curl -F "file=@/outbox/test.mp4" http://[server-ip]:5000/upload
   ```

### Server not receiving files

1. Check server status:
   ```bash
   sudo systemctl status monitoring-pipeline-server.service
   ```

2. Check logs:
   ```bash
   sudo journalctl -u monitoring-pipeline-server.service -n 50
   ```

3. Verify upload directory exists:
   ```bash
   ls -la /srv/monitoring-pipeline/uploads/
   ```

4. Check disk space:
   ```bash
   df -h /srv/monitoring-pipeline/
   ```

5. Test endpoint:
   ```bash
   curl http://localhost:5000/health
   ```

## Security Considerations

- Services run with limited permissions (non-root `monitoring` user)
- File extensions are validated (only video formats allowed)
- File size is limited to 500MB
- Systemd services use strict security settings (`ProtectSystem`, `ProtectHome`)
- All uploads are timestamped to prevent overwrites
- Logs are written to syslog for audit trails

## Logs

Logs are stored in:
- Client: `/var/log/monitoring-pipeline.log` and systemd journal
- Server: `/var/log/monitoring-pipeline-server.log` and systemd journal

View systemd logs:
```bash
# Real-time client logs
sudo journalctl -u monitoring-pipeline-uploader.service -f

# Real-time server logs
sudo journalctl -u monitoring-pipeline-server.service -f

# Last 100 lines of client logs
sudo journalctl -u monitoring-pipeline-uploader.service -n 100

# Logs from the last hour
sudo journalctl -u monitoring-pipeline-uploader.service --since "1 hour ago"
```

## Performance Tuning

Adjust these parameters in `/opt/monitoring-pipeline/config/client.json`:

- **`poll_interval`**: Smaller value = faster detection (but more CPU). Recommend 10-30 seconds.
- **`retry_delay`**: Shorter delay = faster retry, but more network traffic. Recommend 300-600 seconds.
- **`max_retries`**: More retries = better reliability but longer wait times. Recommend 3-5.

For high-volume scenarios:
```json
{
  "poll_interval": 5,
  "retry_delay": 120,
  "max_retries": 5
}
```

## Development & Testing

### Test Upload Manually

```bash
# Create a test file
dd if=/dev/zero of=/outbox/test.mp4 bs=1M count=10

# Watch the upload
sudo journalctl -u monitoring-pipeline-uploader.service -f

# Verify server received it
sudo ls -la /srv/monitoring-pipeline/uploads/
```

### Run Components Locally (for debugging)

```bash
# Terminal 1: Start server
python3 monitoring-pipeline/server/server.py

# Terminal 2: Start client
python3 monitoring-pipeline/pi-client/uploader.py

# Terminal 3: Test upload
dd if=/dev/zero of=/outbox/test.mp4 bs=1M count=10
```

## Maintenance

### Log Rotation

Add to `/etc/logrotate.d/monitoring-pipeline`:
```
/var/log/monitoring-pipeline.log
/var/log/monitoring-pipeline-server.log
{
    weekly
    rotate 4
    compress
    missingok
    notifempty
}
```

### Cleanup Old Uploads

```bash
# Find and delete files older than 30 days
find /srv/monitoring-pipeline/uploads -type f -mtime +30 -delete

# Or archive instead
find /srv/monitoring-pipeline/uploads -type f -mtime +30 | tar czf backup-$(date +%Y%m%d).tar.gz -T -
```

## Audio Recording (Pi)

The Pi client can record audio from a microphone into timestamped MP4 chunks and drop them into the `outbox` directory so the uploader will pick them up and send them to the server.

Configuration (add to `/opt/monitoring-pipeline/config/client.json` under the `recording` key):
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

Defaults:
- `chunk_duration`: 60 seconds (adjust to your needs)
- `device`: ALSA device (e.g., `hw:0`)
- `ffmpeg_path`: path to ffmpeg binary

Systemd service (copy `systemd/monitoring-pipeline-recorder.service` to `/etc/systemd/system/` then enable and start):
```bash
sudo cp monitoring-pipeline/systemd/monitoring-pipeline-recorder.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable monitoring-pipeline-recorder.service
sudo systemctl start monitoring-pipeline-recorder.service
sudo journalctl -u monitoring-pipeline-recorder.service -f
```

Notes:
- `ffmpeg` must be installed on the Pi (`sudo apt install ffmpeg`).
- The recorder writes temporary files with `.tmp` suffix and renames them to `.mp4` on successful completion.
- If you want to disable recording, set `recording.enabled` to `false`.

---

## License

This project is provided as-is.
