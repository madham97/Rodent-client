#!/usr/bin/env python3
"""
On-device integration test.
Captures a test image (or uses a dummy JPEG) and POSTs it directly to the server
via HTTP — no GSM modem required.

Usage:
    python3 test_record_upload.py
    python3 test_record_upload.py --config /path/to/client.json
"""

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from uploader import load_config, build_multipart, BOUNDARY


def capture_test_image(cfg) -> tuple[Path, bool]:
    """Capture a low-res test image with rpicam-still. Falls back to a 1×1 dummy JPEG."""
    outbox = Path(cfg.get('outbox_dir', '/outbox'))
    outbox.mkdir(parents=True, exist_ok=True)
    path = outbox / f"test_{int(time.time())}.jpg"

    cmd = [
        cfg.get('rpicam_still_path', 'rpicam-still'),
        '--camera', str(cfg.get('camera_id', 0)),
        '--width', '640', '--height', '480',
        '--quality', '75', '--timeout', '1000',
        '--output', str(path), '--nopreview', '--immediate',
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=10)
        if r.returncode == 0 and path.exists() and path.stat().st_size > 0:
            return path, True
    except Exception:
        pass

    # Minimal valid JPEG (1×1 white pixel, no camera needed)
    path.write_bytes(
        b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00'
        b'\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t\x08\n'
        b'\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a\x1f\x1e\x1d'
        b'\x1a\x1c\x1c $.\' ",#\x1c\x1c(7),\x01\x02\x03\x04\x05\x06\x07\x08'
        b'\x09\x0a\x0b\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00'
        b'\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00'
        b'\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a'
        b'\x0b\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xf5\x0f\xff\xd9'
    )
    return path, False


def main():
    cfg_path = '/opt/monitoring-pipeline/config/client.json'
    if len(sys.argv) > 2 and sys.argv[1] == '--config':
        cfg_path = sys.argv[2]

    cfg = load_config(cfg_path)
    upload_url = cfg.get('server_url', 'http://localhost:8000') + '/upload'

    print(f"Server : {upload_url}")

    path, is_real = capture_test_image(cfg)
    print(f"Image  : {path.name}  ({'camera capture' if is_real else 'dummy JPEG'})")

    metadata = {
        'device_id':    cfg.get('device_id', 'test-device'),
        'mode':         'test',
        'timestamp':    time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'motion_score': '0',
    }
    body = build_multipart(path, metadata)

    req = urllib.request.Request(
        upload_url,
        data=body,
        headers={'Content-Type': f'multipart/form-data; boundary={BOUNDARY}'},
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
        if result.get('status') == 'ok':
            print("Upload : OK")
        else:
            print(f"Upload : unexpected response: {result}")
            sys.exit(1)
    except urllib.error.HTTPError as e:
        print(f"Upload : FAILED  HTTP {e.code} {e.reason}")
        sys.exit(1)
    except Exception as e:
        print(f"Upload : FAILED  {e}")
        sys.exit(1)
    finally:
        path.unlink(missing_ok=True)

    print("Test passed.")


if __name__ == '__main__':
    main()
