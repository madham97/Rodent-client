#!/usr/bin/env python3
"""
Pi Monitoring Pipeline - Recorder

Modes:
  image_interval  JPEG captured at a fixed time interval.
  image_motion    JPEG captured when motion is detected.
"""

import json
import logging
import os
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('/var/log/monitoring-pipeline.log'),
        logging.StreamHandler(),
    ]
)
logger = logging.getLogger(__name__)

STOP_FLAG = False


def _signal_handler(signum, frame):
    global STOP_FLAG
    logger.info(f"Received signal {signum}, stopping after current capture...")
    STOP_FLAG = True


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


class Recorder:
    def __init__(self, config):
        self.outbox_dir          = Path(config.get('outbox_dir', '/outbox'))
        self.mode                = config.get('mode', 'image_motion')
        self._device_id          = config.get('device_id', socket.gethostname())

        # Camera
        self.camera_id           = int(config.get('camera_id', 0))
        self.width               = int(config.get('width', 1920))
        self.height              = int(config.get('height', 1080))
        self.min_size_bytes      = int(config.get('min_size_bytes', 1024))
        self.rpicam_still_path   = config.get('rpicam_still_path', 'rpicam-still')

        # Motion detection
        self.motion_threshold    = float(config.get('motion_threshold', 0.02))
        self.detection_interval  = float(config.get('detection_interval', 1.0))
        self.motion_cooldown     = float(config.get('motion_cooldown', 0.0))
        self.detection_width     = int(config.get('detection_width', 320))
        self.detection_height    = int(config.get('detection_height', 240))
        self.temporal_alpha      = float(config.get('temporal_alpha', 0.2))
        self.motion_debug        = bool(config.get('motion_debug', False))

        # Image capture
        self.image_interval  = float(config.get('image_interval', 5.0))
        self.image_quality   = int(config.get('image_quality', 85))

        self.outbox_dir.mkdir(parents=True, exist_ok=True)

        logger.info(f"Recorder initialized. outbox: {self.outbox_dir}")
        logger.info(
            f"Mode: {self.mode} | Device: {self._device_id} | "
            f"Resolution: {self.width}x{self.height}"
        )

    def start(self):
        runners = {
            'image_interval': self._run_image_interval_mode,
            'image_motion':   self._run_image_motion_mode,
        }
        runner = runners.get(self.mode)
        if runner is None:
            logger.critical(
                f"Unknown mode: {self.mode!r}. "
                f"Valid options: {list(runners)}"
            )
            return
        runner()

    # ── Utilities ─────────────────────────────────────────────────────────────

    def _make_filename(self) -> str:
        ts = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        return f"image_{ts}.jpg"

    def _write_sidecar(self, image_path: Path, motion_score: float = 0.0):
        meta = {
            'device_id':    self._device_id,
            'mode':         self.mode,
            'timestamp':    datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'motion_score': motion_score,
        }
        image_path.with_suffix('.json').write_text(json.dumps(meta))

    def _capture_detection_frame(self):
        """Capture a low-res grayscale PIL image for motion comparison. Returns None on failure."""
        tmp = self.outbox_dir / '.detect.jpg.tmp'
        cmd = [
            self.rpicam_still_path,
            '--camera',  str(self.camera_id),
            '--width',   str(self.detection_width),
            '--height',  str(self.detection_height),
            '--timeout', '500',
            '--output',  str(tmp),
            '--nopreview', '--immediate',
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=10)
            if r.returncode == 0 and tmp.exists():
                from PIL import Image
                return Image.open(tmp).convert('L')
            logger.debug(
                f"Detection frame failed (code {r.returncode}): "
                f"{r.stderr.decode(errors='ignore').strip()}"
            )
        except FileNotFoundError:
            logger.warning(f"rpicam-still not found at '{self.rpicam_still_path}'")
        except Exception as e:
            logger.debug(f"Detection frame error: {e}")
        finally:
            tmp.unlink(missing_ok=True)
        return None

    def _capture_image_tmp(self, path: Path) -> 'Path | None':
        """Capture a full-resolution JPEG to a .tmp file. Returns the tmp path on success, None on failure.
        The caller writes the sidecar then renames tmp → path so the image never appears without its sidecar."""
        tmp = path.with_suffix(path.suffix + '.tmp')
        cmd = [
            self.rpicam_still_path,
            '--camera',  str(self.camera_id),
            '--width',   str(self.width),
            '--height',  str(self.height),
            '--quality', str(self.image_quality),
            '--timeout', '1000',
            '--output',  str(tmp),
            '--nopreview', '--immediate',
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=15)
            if r.returncode == 0 and tmp.exists() and tmp.stat().st_size >= self.min_size_bytes:
                logger.info(f"Captured image: {path.name}")
                return tmp
            logger.warning(f"Image capture failed (code {r.returncode})")
        except FileNotFoundError:
            logger.critical(f"rpicam-still not found at '{self.rpicam_still_path}'")
        except Exception as e:
            logger.error(f"Image capture error: {e}")
        tmp.unlink(missing_ok=True)
        return None

    def _motion_ratio(self, avg, frame) -> float:
        """Return fraction of pixels that changed significantly vs. running average."""
        from PIL import ImageChops
        diff  = ImageChops.difference(avg, frame)
        ratio = sum(diff.histogram()[40:]) / (frame.width * frame.height)
        if self.motion_debug:
            logger.info(f"Motion ratio: {ratio:.4f} (threshold: {self.motion_threshold})")
        return ratio

    def _motion_loop(self, on_motion):
        """
        Run the motion detection loop. Calls on_motion(score) each time motion is detected.
        Uses temporal averaging to suppress false triggers from camera shake.
        """
        try:
            from PIL import Image, ImageChops  # noqa — validate import
        except ImportError:
            logger.critical("Pillow is required for motion detection: pip install pillow")
            return

        from PIL import Image

        def build_baseline(n=4):
            avg = None
            captured = 0
            while captured < n and not STOP_FLAG:
                frame = self._capture_detection_frame()
                if frame is None:
                    time.sleep(1)
                    continue
                avg = frame if avg is None else Image.blend(avg, frame, alpha=0.5)
                captured += 1
                time.sleep(self.detection_interval)
            return avg

        logger.info("Building initial baseline...")
        running_avg = build_baseline()

        while not STOP_FLAG:
            frame = self._capture_detection_frame()
            if frame is None:
                logger.warning("Detection frame failed, retrying in 2s...")
                time.sleep(2)
                continue

            ratio = self._motion_ratio(running_avg, frame)
            if ratio > self.motion_threshold:
                logger.info(f"Motion detected (score={ratio:.4f})")
                on_motion(ratio)
                if self.motion_cooldown > 0:
                    logger.info(f"Cooling down for {self.motion_cooldown}s...")
                    elapsed = 0.0
                    while elapsed < self.motion_cooldown and not STOP_FLAG:
                        time.sleep(0.5)
                        elapsed += 0.5
                    logger.info("Cooldown ended, rebuilding baseline...")
                running_avg = build_baseline()
            else:
                running_avg = Image.blend(running_avg, frame, alpha=self.temporal_alpha)
                time.sleep(self.detection_interval)

        logger.info("Motion detection stopping gracefully.")

    # ── Image: interval mode ───────────────────────────────────────────────────

    def _run_image_interval_mode(self):
        logger.info(f"Starting image interval recorder (every {self.image_interval}s)")

        while not STOP_FLAG:
            path = self.outbox_dir / self._make_filename()
            tmp = self._capture_image_tmp(path)
            if tmp:
                self._write_sidecar(path)
                tmp.rename(path)
            elapsed = 0.0
            while elapsed < self.image_interval and not STOP_FLAG:
                time.sleep(0.5)
                elapsed += 0.5

        logger.info("Image interval recorder stopping gracefully.")

    # ── Image: motion mode ─────────────────────────────────────────────────────

    def _run_image_motion_mode(self):
        logger.info("Starting motion-triggered image recorder")

        def on_motion(score: float):
            path = self.outbox_dir / self._make_filename()
            tmp = self._capture_image_tmp(path)
            if tmp:
                self._write_sidecar(path, motion_score=score)
                tmp.rename(path)

        self._motion_loop(on_motion)


# ── Config ────────────────────────────────────────────────────────────────────

def load_config(path: str = None) -> dict:
    if path is None:
        path = '/opt/Rodent-client/config/client.json'

    cfg = {}
    if os.path.exists(path):
        try:
            with open(path) as f:
                cfg = json.load(f)
        except Exception as e:
            logger.warning(f"Failed to load config {path}: {e}")

    rec = cfg.get('recording', {})
    defaults = {
        'mode':               'image_motion',
        'camera_id':          0,
        'width':              1920,
        'height':             1080,
        'rpicam_still_path':  'rpicam-still',
        'min_size_bytes':     1024,
        'motion_threshold':   0.02,
        'detection_interval': 1.0,
        'motion_cooldown':    0.0,
        'detection_width':    320,
        'detection_height':   240,
        'temporal_alpha':     0.2,
        'motion_debug':       False,
        'image_interval':     5.0,
        'image_quality':      85,
    }
    merged = {**defaults, **rec}
    merged['outbox_dir'] = cfg.get('outbox_dir', '/outbox')
    merged['device_id']  = cfg.get('device_id', '')
    return merged


if __name__ == '__main__':
    cfg_path = 'config/client.json'
    if len(sys.argv) > 1 and sys.argv[1] == '--config' and len(sys.argv) > 2:
        cfg_path = sys.argv[2]

    config = load_config(cfg_path)
    recorder = Recorder(config)
    try:
        recorder.start()
    except KeyboardInterrupt:
        logger.info('Recorder stopped by user')
        sys.exit(0)
