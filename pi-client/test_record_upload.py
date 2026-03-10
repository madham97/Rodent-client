#!/usr/bin/env python3
"""
Simple script for edge device testing.

It records a short video using the same tools the recorder uses (or falls back to a dummy file),
then uploads it using the existing VideoUploader class and the configuration file.

Run this on the Pi after installation; it will read `/opt/monitoring-pipeline/config/client.json`.
"""

import subprocess
import sys
import time
from pathlib import Path

# import from local modules
from uploader import load_config, VideoUploader


def record_temp(cfg, duration_sec=5):
    """Record a short clip and return the path to the file."""
    outbox = Path(cfg.get('outbox_dir', '/outbox'))
    outbox.mkdir(parents=True, exist_ok=True)

    timestamp = int(time.time())
    filename = f"test_{timestamp}.mp4"
    path = outbox / filename

    # Use same cmd as recorder for consistency
    cmd = [
        cfg.get('rpicam_vid_path', 'rpicam-vid'),
        '--camera', str(cfg.get('camera_id', 0)),
        '--width', str(cfg.get('width', 1920)),
        '--height', str(cfg.get('height', 1080)),
        '--framerate', str(cfg.get('framerate', 30)),
        '--bitrate', cfg.get('bitrate', '5Mbps'),
        '--timeout', str(duration_sec * 1000),
        '-o', str(path)
    ]

    print(f"Recording {duration_sec}s clip to {path}...")
    try:
        subprocess.run(cmd, check=True, timeout=duration_sec + 5)
        print("Recording finished")
    except Exception as e:
        print("rpicam-vid failed (", e, "); writing dummy file instead")
        # fallback: create a small zero-filled file
        path.write_bytes(b"\0" * 1024 * 1024)
    return path


def main():
    # load uploader config (defaults to /opt/monitoring-pipeline/config/client.json)
    config = load_config()

    uploader = VideoUploader(config)

    file_path = record_temp(config)

    print(f"Uploading {file_path}...")
    uploader.upload_file(file_path)
    print("Upload attempt finished")


if __name__ == '__main__':
    main()
