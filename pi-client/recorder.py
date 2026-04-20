#!/usr/bin/env python3
"""
Pi Monitoring Pipeline - Recorder

Modes:
  segment        Continuous fixed-duration video chunks.
  motion         Video clip recorded when motion is detected.
  image_interval JPEG captured at a fixed time interval.
  image_motion   JPEG captured when motion is detected.
"""

import os
import sys
import time
import json
import signal
import logging
import socket
import subprocess
import threading
from pathlib import Path
from datetime import datetime

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
    logger.info(f"Received signal {signum}, stopping after current chunk...")
    STOP_FLAG = True


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


class Recorder:
    def __init__(self, config):
        self.outbox_dir       = Path(config.get('outbox_dir', '/outbox'))
        self.mode             = config.get('mode', 'segment')

        # Camera / video
        self.camera_id        = int(config.get('camera_id', 0))
        self.width            = int(config.get('width', 1920))
        self.height           = int(config.get('height', 1080))
        self.framerate        = int(config.get('framerate', 30))
        self.bitrate          = config.get('bitrate', '5Mbps')
        self.chunk_duration   = int(config.get('chunk_duration', 60))
        self.min_size_bytes   = int(config.get('min_size_bytes', 1024))
        self.rpicam_vid_path  = config.get('rpicam_vid_path', 'rpicam-vid')
        self.rpicam_still_path = config.get('rpicam_still_path', 'rpicam-still')

        # Motion detection
        self.motion_threshold   = float(config.get('motion_threshold', 0.02))
        self.detection_interval = float(config.get('detection_interval', 1.0))
        self.motion_cooldown    = float(config.get('motion_cooldown', 0.0))
        self.detection_width    = int(config.get('detection_width', 320))
        self.detection_height   = int(config.get('detection_height', 240))
        self.temporal_alpha     = float(config.get('temporal_alpha', 0.2))
        self.motion_debug       = bool(config.get('motion_debug', False))

        # Image capture
        self.image_interval = float(config.get('image_interval', 5.0))
        self.image_quality  = int(config.get('image_quality', 85))

        # Internal: segment-mode renamer thread
        self._renamer_stop   = threading.Event()
        self._renamer_thread = None

        self.outbox_dir.mkdir(parents=True, exist_ok=True)

        logger.info(f"Recorder initialized. outbox: {self.outbox_dir}")
        logger.info(
            f"Mode: {self.mode} | Camera: {self.camera_id} | "
            f"Resolution: {self.width}x{self.height} | "
            f"Framerate: {self.framerate} | Bitrate: {self.bitrate}"
        )

    def start(self):
        runners = {
            'segment':        self._run_segment_mode,
            'motion':         self._run_motion_video_mode,
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

    def _make_filename(self, ext='.mp4') -> str:
        ts     = datetime.now(datetime.UTC).strftime('%Y%m%dT%H%M%SZ')
        prefix = 'image' if ext.startswith('.jp') else 'video'
        return f"{prefix}_{ts}{ext}"

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

    def _capture_image(self, path: Path) -> bool:
        """Capture a full-resolution JPEG to path. Returns True on success."""
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
                tmp.rename(path)
                logger.info(f"Captured image: {path.name}")
                return True
            logger.warning(f"Image capture failed (code {r.returncode})")
        except FileNotFoundError:
            logger.critical(f"rpicam-still not found at '{self.rpicam_still_path}'")
        except Exception as e:
            logger.error(f"Image capture error: {e}")
        finally:
            tmp.unlink(missing_ok=True)
        return False

    def _motion_score(self, avg, frame) -> float:
        """Return the fraction of pixels that changed significantly between avg and frame."""
        from PIL import ImageChops
        diff = ImageChops.difference(avg, frame)
        # Only count pixels that changed by more than 40/255 — filters sensor noise
        # and minor lighting shifts, focuses on meaningful movement
        ratio = sum(diff.histogram()[40:]) / (frame.width * frame.height)
        if self.motion_debug:
            logger.info(f"Motion ratio: {ratio:.4f} (threshold: {self.motion_threshold})")
        return ratio

    def _write_sidecar(self, file_path: Path, motion_score: float = None):
        """Write a JSON sidecar with capture metadata alongside file_path."""
        meta = {
            "timestamp":    datetime.now(datetime.UTC).strftime('%Y-%m-%dT%H:%M:%SZ'),
            "mode":         self.mode,
            "device_id":    socket.gethostname(),
            "motion_score": round(motion_score, 6) if motion_score is not None else None,
        }
        sidecar = file_path.with_suffix('.json')
        with open(sidecar, 'w') as f:
            json.dump(meta, f)

    def _finalize_tmp(self, tmp_path: Path):
        """Wait for a .tmp file to stop growing, then rename it to its final name."""
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                if not tmp_path.exists():
                    return
                size = tmp_path.stat().st_size
                time.sleep(0.5)
                if tmp_path.stat().st_size == size >= self.min_size_bytes:
                    final = tmp_path.with_suffix('')  # strip .tmp
                    tmp_path.rename(final)
                    logger.info(f"Finalized: {final.name}")
                    return
            except FileNotFoundError:
                return
            except Exception as e:
                logger.error(f"Finalize error for {tmp_path}: {e}")
                return
        logger.warning(f"Timed out finalizing {tmp_path.name}")

    # ── Motion detection loop (shared by motion video and image_motion) ────────

    def _motion_loop(self, on_motion):
        """
        Run the motion detection loop. Calls on_motion() each time motion is detected.
        Uses temporal averaging to suppress false triggers from camera shake.
        """
        try:
            from PIL import Image, ImageChops  # noqa — validate import
        except ImportError:
            logger.critical("Pillow is required for motion detection: pip install pillow")
            return

        from PIL import Image

        def build_baseline(n=4):
            """Capture n frames and blend them into a stable baseline."""
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

            score = self._motion_score(running_avg, frame)
            if score > self.motion_threshold:
                logger.info("Motion detected")
                on_motion(score)
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

    # ── Video: segment mode ────────────────────────────────────────────────────

    def _run_segment_mode(self):
        """Record continuous fixed-duration MP4 chunks."""
        logger.info(f"Starting segment recorder ({self.chunk_duration}s chunks)")

        while not STOP_FLAG:
            tmp_path = self.outbox_dir / self._make_filename('.mp4.tmp')
            cmd = [
                self.rpicam_vid_path,
                '--camera',    str(self.camera_id),
                '--width',     str(self.width),
                '--height',    str(self.height),
                '--framerate', str(self.framerate),
                '--bitrate',   self.bitrate,
                '--timeout',   str(self.chunk_duration * 1000),
                '-o',          str(tmp_path),
            ]
            logger.info(f"Recording chunk: {tmp_path.name}")
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            except FileNotFoundError:
                logger.critical(f"rpicam-vid not found at '{self.rpicam_vid_path}'")
                return

            if not (self._renamer_thread and self._renamer_thread.is_alive()):
                self._renamer_stop.clear()
                self._renamer_thread = threading.Thread(target=self._renamer_loop, daemon=True)
                self._renamer_thread.start()

            while True:
                if STOP_FLAG:
                    break
                if proc.poll() is not None:
                    try:
                        _, err = proc.communicate(timeout=5)
                        if err:
                            logger.debug(f"rpicam-vid: {err.decode(errors='ignore').strip()}")
                    except Exception:
                        pass
                    if proc.returncode != 0:
                        logger.warning(f"rpicam-vid exited {proc.returncode}")
                    break
                time.sleep(0.5)

            if STOP_FLAG:
                try:
                    proc.send_signal(signal.SIGINT)
                    logger.info("Sent SIGINT to rpicam-vid to finalize chunk")
                    proc.wait(timeout=10 + self.chunk_duration)
                except Exception:
                    proc.terminate()
                break
            else:
                proc.terminate()

        self._renamer_stop.set()
        if self._renamer_thread:
            self._renamer_thread.join(timeout=5)
        self._do_renamer_pass()
        logger.info("Segment recorder stopping gracefully.")

    def _renamer_loop(self):
        while not self._renamer_stop.is_set():
            self._do_renamer_pass()
            self._renamer_stop.wait(1.0)
        self._do_renamer_pass()

    def _do_renamer_pass(self):
        for tmp in self.outbox_dir.glob('*.mp4.tmp'):
            try:
                size = tmp.stat().st_size
                time.sleep(0.5)
                if tmp.stat().st_size == size >= self.min_size_bytes:
                    final = tmp.with_name(tmp.name[:-4])  # strip .tmp
                    tmp.rename(final)
                    logger.info(f"Finalized segment: {final.name}")
            except FileNotFoundError:
                continue
            except Exception as e:
                logger.error(f"Rename error {tmp}: {e}")

    # ── Video: motion mode ─────────────────────────────────────────────────────

    def _record_clip(self, tmp_path: Path):
        """Record a single video clip via rpicam-vid."""
        cmd = [
            self.rpicam_vid_path,
            '--camera',    str(self.camera_id),
            '--width',     str(self.width),
            '--height',    str(self.height),
            '--framerate', str(self.framerate),
            '--bitrate',   self.bitrate,
            '--timeout',   str(self.chunk_duration * 1000),
            '-o',          str(tmp_path),
        ]
        logger.info(f"Recording clip: {tmp_path.name}")
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            while True:
                if STOP_FLAG:
                    proc.send_signal(signal.SIGINT)
                    break
                if proc.poll() is not None:
                    if proc.returncode != 0:
                        logger.warning(f"rpicam-vid exited {proc.returncode}")
                    break
                time.sleep(0.5)
            proc.communicate(timeout=10 + self.chunk_duration)
        except FileNotFoundError:
            logger.critical(f"rpicam-vid not found at '{self.rpicam_vid_path}'")
        except Exception as e:
            logger.error(f"Clip recording error: {e}")

    def _run_motion_video_mode(self):
        """Record a video clip each time motion is detected."""
        logger.info(f"Starting motion-triggered video recorder ({self.chunk_duration}s clips)")

        def on_motion(motion_score):
            tmp_path = self.outbox_dir / self._make_filename('.mp4.tmp')
            self._record_clip(tmp_path)
            if not STOP_FLAG:
                final = tmp_path.with_suffix('')
                self._finalize_tmp(tmp_path)
                if final.exists():
                    self._write_sidecar(final, motion_score)
                logger.info("Clip saved, resuming detection")

        self._motion_loop(on_motion)

    # ── Image: interval mode ───────────────────────────────────────────────────

    def _run_image_interval_mode(self):
        """Capture a JPEG at a fixed time interval."""
        logger.info(f"Starting image interval recorder (every {self.image_interval}s)")

        while not STOP_FLAG:
            path = self.outbox_dir / self._make_filename('.jpg')
            if self._capture_image(path):
                self._write_sidecar(path)
            elapsed = 0.0
            while elapsed < self.image_interval and not STOP_FLAG:
                time.sleep(0.5)
                elapsed += 0.5

        logger.info("Image interval recorder stopping gracefully.")

    # ── Image: motion mode ─────────────────────────────────────────────────────

    def _run_image_motion_mode(self):
        """Capture a JPEG each time motion is detected."""
        logger.info("Starting motion-triggered image recorder")

        def on_motion(motion_score):
            path = self.outbox_dir / self._make_filename('.jpg')
            if self._capture_image(path):
                self._write_sidecar(path, motion_score)

        self._motion_loop(on_motion)


# ── Config ────────────────────────────────────────────────────────────────────

def load_config(path: str = None) -> dict:
    if path is None:
        path = '/opt/monitoring-pipeline/config/client.json'

    cfg = {}
    if os.path.exists(path):
        try:
            with open(path) as f:
                cfg = json.load(f)
        except Exception as e:
            logger.warning(f"Failed to load config {path}: {e}")

    rec = cfg.get('recording', {})
    defaults = {
        'camera_id':          0,
        'chunk_duration':     60,
        'width':              1920,
        'height':             1080,
        'framerate':          30,
        'bitrate':            '5Mbps',
        'rpicam_vid_path':    'rpicam-vid',
        'rpicam_still_path':  'rpicam-still',
        'min_size_bytes':     1024,
        'mode':               'segment',
        'motion_threshold':   0.02,
        'detection_interval': 1.0,
        'detection_width':    320,
        'detection_height':   240,
        'temporal_alpha':     0.2,
        'image_interval':     5.0,
        'image_quality':      85,
    }
    merged = {**defaults, **rec}
    merged['outbox_dir'] = cfg.get('outbox_dir', '/outbox')
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
