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
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / 'thermal'))

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


class ThermalStream:
    """
    Keeps the MI48 thermal sensor (GPIO/SPI/I2C) streaming continuously in a background
    thread and exposes the latest frame. Runs independently of the visible-camera capture
    loop so the two cameras are always both "live" — a capture just reads whatever thermal
    frame is currently buffered instead of triggering a fresh (slow) thermal acquisition.

    Any failure to init or read from the sensor disables thermal fusion (`available` stays
    False) without affecting visible-camera capture.
    """

    def __init__(self, fps=9, filters=True, offset=0.0, out_width=480, out_height=372,
                 warmup_frames=5, spi_speed_hz=2_000_000, hflip=False, vflip=False):
        self.fps           = fps
        self.filters       = filters
        self.offset        = offset
        self.out_size      = (out_width, out_height)
        self.warmup_frames = warmup_frames
        self.spi_speed_hz  = spi_speed_hz
        self.hflip         = hflip
        self.vflip         = vflip

        self.available   = False
        self._lock       = threading.Lock()
        self._latest      = None  # (gray8 ndarray, (temp_min_c, temp_max_c, temp_avg_c))
        self._stop_event = threading.Event()
        self._thread      = None
        self._mi48        = None

    def start(self):
        self._thread = threading.Thread(target=self._run, name='thermal-stream', daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        if self._mi48:
            try:
                self._mi48.stop(stop_timeout=0.5)
            except Exception:
                pass

    def latest(self):
        with self._lock:
            return self._latest

    def _run(self):
        try:
            from thermal_common import make_mi48, read_frame, frame_to_gray8
        except ImportError as e:
            logger.warning(f"Thermal sensor libraries not available, disabling thermal fusion: {e}")
            return

        try:
            self._mi48, cs_pin = make_mi48(fps=self.fps, filters=self.filters, offset=self.offset,
                                            spi_speed_hz=self.spi_speed_hz)
            self._mi48.start(stream=True, with_header=True)
        except Exception as e:
            logger.warning(f"Thermal sensor init failed, disabling thermal fusion: {e}")
            return

        for _ in range(self.warmup_frames):
            if self._stop_event.is_set():
                return
            try:
                read_frame(self._mi48, cs_pin)
            except Exception:
                pass

        self.available = True
        logger.info("Thermal stream started")

        while not self._stop_event.is_set():
            try:
                data, _ = read_frame(self._mi48, cs_pin)
                gray8, stats = frame_to_gray8(data, self._mi48.fpa_shape, out_size=self.out_size,
                                               hflip=self.hflip, vflip=self.vflip)
                with self._lock:
                    self._latest = (gray8, stats)
            except Exception as e:
                logger.debug(f"Thermal frame read error: {e}")
                time.sleep(0.5)

        logger.info("Thermal stream stopping gracefully.")


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

        # rpicam-still only supports 0/180 rotation in hardware. If the camera is physically
        # mounted at 90/270 (common with CSI ribbon cameras in compact enclosures), correct it
        # here in software instead — a rotation (unlike a flip) preserves left/right handedness,
        # which matters when this feed is later paired side by side with the thermal feed.
        self.image_rotation = int(config.get('image_rotation', 0))
        if self.image_rotation not in (0, 90, 180, 270):
            logger.warning(f"Invalid image_rotation {self.image_rotation}, must be 0/90/180/270 — using 0")
            self.image_rotation = 0
        # When rotating 90/270 in software, request the swapped dimensions from the sensor so
        # the rotated result comes out at the configured width x height without distortion.
        if self.image_rotation in (90, 270):
            self._capture_width, self._capture_height = self.height, self.width
        else:
            self._capture_width, self._capture_height = self.width, self.height

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

        # Thermal fusion (Waveshare MI48, GPIO/SPI/I2C)
        self.thermal_enabled = bool(config.get('thermal_enabled', False))
        self._thermal = None
        if self.thermal_enabled:
            self._thermal = ThermalStream(
                fps=int(config.get('thermal_fps', 9)),
                filters=bool(config.get('thermal_filters', True)),
                offset=float(config.get('thermal_offset', 0.0)),
                out_width=int(config.get('thermal_width', 480)),
                out_height=int(config.get('thermal_height', 372)),
                warmup_frames=int(config.get('thermal_warmup_frames', 5)),
                spi_speed_hz=int(config.get('thermal_spi_speed_hz', 2_000_000)),
                hflip=bool(config.get('thermal_hflip', False)),
                vflip=bool(config.get('thermal_vflip', False)),
            )
            self._thermal.start()

        self.outbox_dir.mkdir(parents=True, exist_ok=True)

        logger.info(f"Recorder initialized. outbox: {self.outbox_dir}")
        logger.info(
            f"Mode: {self.mode} | Device: {self._device_id} | "
            f"Resolution: {self.width}x{self.height} | "
            f"Thermal fusion: {'enabled' if self.thermal_enabled else 'disabled'}"
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
        try:
            runner()
        finally:
            if self._thermal:
                self._thermal.stop()

    # ── Utilities ─────────────────────────────────────────────────────────────

    def _make_filename_stem(self) -> str:
        ts = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        return f"image_{ts}"

    def _write_sidecar(self, image_path: Path, motion_score: float = 0.0,
                        fmt: str = 'rgb', thermal_stats=None):
        meta = {
            'device_id':    self._device_id,
            'mode':         self.mode,
            'timestamp':    datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'motion_score': motion_score,
            'format':       fmt,
        }
        if thermal_stats is not None:
            tmin, tmax, tavg = thermal_stats
            meta['thermal_min_c'] = round(tmin, 1)
            meta['thermal_max_c'] = round(tmax, 1)
            meta['thermal_avg_c'] = round(tavg, 1)
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
            '--width',   str(self._capture_width),
            '--height',  str(self._capture_height),
            '--quality', str(self.image_quality),
            '--timeout', '1000',
            '--output',  str(tmp),
            '--nopreview', '--immediate',
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=15)
            if r.returncode == 0 and tmp.exists() and tmp.stat().st_size >= self.min_size_bytes:
                if self.image_rotation:
                    self._rotate_in_place(tmp)
                logger.info(f"Captured image: {path.name}")
                return tmp
            logger.warning(f"Image capture failed (code {r.returncode})")
        except FileNotFoundError:
            logger.critical(f"rpicam-still not found at '{self.rpicam_still_path}'")
        except Exception as e:
            logger.error(f"Image capture error: {e}")
        tmp.unlink(missing_ok=True)
        return None

    def _rotate_in_place(self, jpg_path: Path):
        """Correct the physical camera mounting rotation (see `image_rotation`). PIL's rotate()
        angle convention is counter-clockwise for positive values, so a clockwise correction
        needs a negated angle."""
        from PIL import Image
        with Image.open(jpg_path) as img:
            rotated = img.rotate(-self.image_rotation, expand=True)
            # jpg_path is the ".jpg.tmp" working file, so PIL can't infer the encoder from the
            # extension — pass it explicitly.
            rotated.save(jpg_path, format='JPEG', quality=self.image_quality)

    def _fuse_thermal(self, visible_path: Path):
        """
        Combine the visible JPEG with the latest buffered thermal frame into an RGBA image,
        with the normalized thermal frame stored as the alpha channel. Returns
        (PIL.Image, (temp_min_c, temp_max_c, temp_avg_c)), or None if thermal isn't ready yet
        (still warming up, or the sensor failed to init).
        """
        latest = self._thermal.latest()
        if latest is None:
            return None
        gray8, stats = latest

        from PIL import Image
        visible = Image.open(visible_path).convert('RGB')
        alpha = Image.fromarray(gray8, mode='L').resize(visible.size, Image.BICUBIC)
        fused = visible.copy()
        fused.putalpha(alpha)
        return fused, stats

    def _capture_frame(self, motion_score: float = 0.0):
        """
        Capture a visible-light frame and, if thermal fusion is enabled and a thermal frame
        is available, fuse them into a single RGBA PNG (thermal in the alpha channel) before
        writing the sidecar and moving the result into the outbox atomically. Falls back to a
        plain JPEG if thermal isn't available so a sensor hiccup never blocks visible capture.
        """
        stem = self.outbox_dir / self._make_filename_stem()
        jpg_tmp = self._capture_image_tmp(stem.with_suffix('.jpg'))
        if not jpg_tmp:
            return

        fused = self._fuse_thermal(jpg_tmp) if self._thermal else None
        if fused:
            image, stats = fused
            final_path = stem.with_suffix('.png')
            png_tmp = final_path.with_suffix('.png.tmp')
            image.save(png_tmp, format='PNG')
            jpg_tmp.unlink(missing_ok=True)
            self._write_sidecar(final_path, motion_score=motion_score,
                                 fmt='rgba_thermal_alpha', thermal_stats=stats)
            png_tmp.rename(final_path)
            logger.info(f"Captured fused image: {final_path.name}")
        else:
            final_path = stem.with_suffix('.jpg')
            self._write_sidecar(final_path, motion_score=motion_score, fmt='rgb')
            jpg_tmp.rename(final_path)

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
            self._capture_frame()
            elapsed = 0.0
            while elapsed < self.image_interval and not STOP_FLAG:
                time.sleep(0.5)
                elapsed += 0.5

        logger.info("Image interval recorder stopping gracefully.")

    # ── Image: motion mode ─────────────────────────────────────────────────────

    def _run_image_motion_mode(self):
        logger.info("Starting motion-triggered image recorder")

        def on_motion(score: float):
            self._capture_frame(motion_score=score)

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
        'image_rotation':     0,
        'thermal_enabled':       False,
        'thermal_fps':           9,
        'thermal_filters':       True,
        'thermal_offset':        0.0,
        'thermal_warmup_frames': 5,
        'thermal_width':         480,
        'thermal_height':        372,
        'thermal_spi_speed_hz':  2_000_000,
        'thermal_hflip':         False,
        'thermal_vflip':         False,
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
