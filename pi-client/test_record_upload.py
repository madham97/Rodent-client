#!/usr/bin/env python3
"""
Integration test — records a short clip then uploads it via the GSM modem.
Run on the Pi after installation to verify the full pipeline end-to-end.

  python3 test_record_upload.py [--config /path/to/client.json]
"""

import subprocess
import sys
import time
from pathlib import Path

from uploader import load_config, Uploader


def record_temp(cfg, duration_sec=5) -> Path:
    """Record a short clip using rpicam-vid, or write a dummy file on failure."""
    outbox = Path(cfg.get('outbox_dir', '/outbox'))
    outbox.mkdir(parents=True, exist_ok=True)

    path = outbox / f"test_{int(time.time())}.mp4"
    cmd = [
        cfg.get('rpicam_vid_path', 'rpicam-vid'),
        '--camera',    str(cfg.get('camera_id', 0)),
        '--width',     str(cfg.get('width', 1280)),
        '--height',    str(cfg.get('height', 720)),
        '--framerate', str(cfg.get('framerate', 15)),
        '--bitrate',   str(cfg.get('bitrate', '200000')),
        '--timeout',   str(duration_sec * 1000),
        '-o',          str(path),
    ]
    print(f"Recording {duration_sec}s clip to {path}...")
    try:
        subprocess.run(cmd, check=True, timeout=duration_sec + 10)
        print("Recording finished")
    except Exception as e:
        print(f"rpicam-vid failed ({e}); writing dummy file instead")
        path.write_bytes(b"\0" * 1024)
    return path


def main():
    cfg_path = '/opt/monitoring-pipeline/config/client.json'
    if len(sys.argv) > 2 and sys.argv[1] == '--config':
        cfg_path = sys.argv[2]

    config = load_config(cfg_path)
    file_path = record_temp(config)

    uploader = Uploader(config, config_path=cfg_path)
    uploader.modem.open()
    try:
        print("Initialising modem...")
        if not uploader.modem.initialize():
            print("Modem initialisation failed")
            sys.exit(1)
        if not uploader.modem.bearer_open():
            print("Could not open GPRS bearer")
            sys.exit(1)
        print(f"Uploading {file_path}...")
        success = uploader._upload(file_path)
        print("Upload succeeded" if success else "Upload failed")
    finally:
        uploader.modem.bearer_close()
        uploader.modem.close()


if __name__ == '__main__':
    main()
