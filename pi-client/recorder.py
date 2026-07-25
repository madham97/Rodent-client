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
from collections import deque
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

# Software rotation applied to buffered frames, correcting for how the CSI camera is physically
# mounted. Populated lazily so importing this module never requires cv2.
_CV_ROTATE = {}


def _valid_rotation(value, key):
    """Coerce a rotation config value to 0/90/180/270, warning rather than failing."""
    try:
        rotation = int(value)
    except (TypeError, ValueError):
        rotation = -1
    if rotation not in (0, 90, 180, 270):
        logger.warning(f"Invalid {key} {value!r}, must be 0/90/180/270 — using 0")
        return 0
    return rotation


def _cv_rotate_map():
    global _CV_ROTATE
    if not _CV_ROTATE:
        import cv2 as cv
        _CV_ROTATE = {90: cv.ROTATE_90_CLOCKWISE, 180: cv.ROTATE_180,
                       270: cv.ROTATE_90_COUNTERCLOCKWISE}
    return _CV_ROTATE


def _signal_handler(signum, frame):
    global STOP_FLAG
    logger.info(f"Received signal {signum}, stopping after current capture...")
    STOP_FLAG = True


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


class ThermalStream:
    """
    Keeps the MI48 thermal sensor (GPIO/SPI/I2C) streaming continuously in a background
    thread and buffers the most recent frames. Runs independently of the visible-camera capture
    loop so the two cameras are always both "live" — a capture selects from already-buffered
    thermal frames instead of triggering a fresh (slow) thermal acquisition.

    A short ring buffer is kept rather than just the newest frame so a capture can pick the frame
    *nearest in time* to the visible exposure, which may be the frame just before it as easily as
    the one just after. Taking simply the newest frame biases the pairing late by up to a full
    frame interval; nearest-neighbour selection halves the worst-case error to half an interval
    (~56ms at 9fps). That difference matters when the subject moves fast enough to change position
    measurably between thermal frames.

    Any failure to init or read from the sensor disables thermal fusion (`available` stays
    False) without affecting visible-camera capture.
    """

    def __init__(self, fps=9, filters=True, offset=0.0, out_width=480, out_height=372,
                 warmup_frames=5, spi_speed_hz=2_000_000, hflip=False, vflip=False,
                 rotation=0, stall_warn_s=5.0, buffer_s=2.0):
        self.fps           = fps
        self.filters       = filters
        self.offset        = offset
        self.out_size      = (out_width, out_height)
        self.warmup_frames = warmup_frames
        self.spi_speed_hz  = spi_speed_hz
        self.hflip         = hflip
        self.vflip         = vflip
        self.rotation      = rotation
        self.stall_warn_s  = stall_warn_s

        self.available   = False
        # Ring buffer of (capture_ts, gray8 ndarray, (temp_min_c, temp_max_c, temp_avg_c)).
        # Sized in seconds of history: enough to hold frames either side of a visible exposure,
        # small enough that stale frames can't accumulate (~178KB per frame at 480x372).
        self._frames     = deque(maxlen=max(2, int(fps * buffer_s)))
        # Condition (not a bare Lock) so a capture can block until the frame it needs has arrived
        # instead of polling — see `nearest()`.
        self._cond       = threading.Condition()
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

    def snapshot(self):
        """Return a list of the currently buffered (capture_ts, gray8, stats), newest last."""
        with self._cond:
            return list(self._frames)

    def nearest(self, target_ts, wait_timeout=None):
        """Return the buffered (capture_ts, gray8, stats) closest in time to `target_ts`, or None
        if nothing is buffered.

        Blocks (up to `wait_timeout`, default two frame intervals) until a frame captured at or
        after `target_ts` exists, so the frame *following* the exposure is considered too and the
        genuinely nearest of the two is returned. Without that wait this could only ever return a
        frame from before the exposure.

        The caller must still check the returned timestamp against its own: a sensor stall leaves
        the last good frame in the buffer, and fusing it would pair fresh visible pixels with old
        thermal data — an error invisible in the uploaded result.
        """
        if wait_timeout is None:
            wait_timeout = 2.0 / max(self.fps, 1)
        deadline = time.time() + wait_timeout
        with self._cond:
            while not (self._frames and self._frames[-1][0] >= target_ts):
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                self._cond.wait(remaining)
            if not self._frames:
                return None
            return min(self._frames, key=lambda f: abs(f[0] - target_ts))

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

        last_ok    = time.time()
        stalled    = False
        while not self._stop_event.is_set():
            try:
                data, _ = read_frame(self._mi48, cs_pin)
                gray8, stats = frame_to_gray8(data, self._mi48.fpa_shape, out_size=self.out_size,
                                               hflip=self.hflip, vflip=self.vflip,
                                               rotation=self.rotation)
                now = time.time()
                with self._cond:
                    self._frames.append((now, gray8, stats))
                    self._cond.notify_all()
                if stalled:
                    logger.info(f"Thermal stream recovered after {now - last_ok:.1f}s")
                    stalled = False
                last_ok = now
            except Exception as e:
                logger.debug(f"Thermal frame read error: {e}")
                # A stall is only visible here — downstream it looks like a normal frame. Escalate
                # to a warning once the gap is long enough that captures are being affected.
                if not stalled and time.time() - last_ok > self.stall_warn_s:
                    logger.warning(
                        f"Thermal stream stalled for {time.time() - last_ok:.1f}s "
                        f"(last error: {e}) — captures will fall back to visible-only"
                    )
                    stalled = True
                time.sleep(0.5)

        logger.info("Thermal stream stopping gracefully.")


class CameraStream:
    """
    Keeps the CSI visible camera streaming continuously via picamera2, buffering recent full-res
    frames so a capture can be served from history instead of triggered on demand.

    This exists because triggering a capture on demand is far too slow for a fast-moving subject.
    Each `rpicam-still` invocation spends ~0.97s opening and tuning the camera before it exposes
    (measured on this hardware), so by the time a motion-triggered frame is exposed the animal has
    moved for 1.5-3s and may have left the field of view. Here the camera is opened once (~0.02s)
    and frames are pulled continuously into a ring buffer, so the frame from the instant motion
    *began* is already in memory when the detector fires — the trigger latency becomes negative.

    Frames are buffered as YUV420 (the sensor's native output, 3.1MB at 1080p) rather than RGB
    (6.2MB), halving the memory cost of the buffer; conversion to RGB happens only for the one
    frame that gets saved. The lores stream is buffered alongside for motion detection, so
    detection costs no camera round-trip at all.
    """

    def __init__(self, width, height, fps=15, detection_width=320, detection_height=240,
                 buffer_s=1.5, stall_warn_s=5.0):
        self.width           = width
        self.height          = height
        self.fps             = fps
        self.detection_size  = (detection_width, detection_height)
        self.stall_warn_s    = stall_warn_s

        self.available   = False
        # (exposure_ts, main_yuv420 ndarray, lores_y ndarray)
        self._frames     = deque(maxlen=max(2, int(fps * buffer_s)))
        self._cond       = threading.Condition()
        self._stop_event = threading.Event()
        self._thread     = None
        self._picam      = None

    def start(self):
        """Open and start the camera. Returns True if streaming, False if picamera2 is unusable
        (caller then falls back to the rpicam-still path)."""
        try:
            from picamera2 import Picamera2
        except Exception as e:
            logger.warning(f"picamera2 unavailable, falling back to rpicam-still capture: {e}")
            return False

        try:
            self._picam = Picamera2()
            cfg = self._picam.create_video_configuration(
                main={'size': (self.width, self.height), 'format': 'YUV420'},
                lores={'size': self.detection_size, 'format': 'YUV420'},
                controls={'FrameRate': self.fps},
            )
            self._picam.configure(cfg)
            self._picam.start()
        except Exception as e:
            logger.warning(f"picamera2 failed to start, falling back to rpicam-still capture: {e}")
            self._close()
            return False

        self._thread = threading.Thread(target=self._run, name='camera-stream', daemon=True)
        self._thread.start()
        self.available = True
        buf_mb = self._frames.maxlen * self.width * self.height * 1.5 / 1e6
        logger.info(
            f"Camera stream started: {self.width}x{self.height} @ {self.fps}fps, "
            f"pre-trigger buffer {self._frames.maxlen} frames "
            f"({self._frames.maxlen / self.fps:.1f}s, ~{buf_mb:.0f}MB)"
        )
        return True

    def _close(self):
        if self._picam:
            try:
                self._picam.stop()
            except Exception:
                pass
            try:
                self._picam.close()
            except Exception:
                pass
            self._picam = None

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._close()
        logger.info("Camera stream stopping gracefully.")

    @staticmethod
    def _exposure_wall_ts(sensor_ts_ns):
        """Convert libcamera's SensorTimestamp (CLOCK_BOOTTIME nanoseconds) to a wall-clock time.

        This is the moment the sensor actually exposed the frame — measured at ~41ms before the
        frame is handed to us — so pairing thermal against it is exact rather than approximate.
        """
        if sensor_ts_ns is None:
            return time.time()
        return time.time() - (time.clock_gettime(time.CLOCK_BOOTTIME) - sensor_ts_ns / 1e9)

    def _run(self):
        last_ok = time.time()
        stalled = False
        while not self._stop_event.is_set():
            try:
                req = self._picam.capture_request()
                try:
                    md    = req.get_metadata()
                    main  = req.make_array('main')
                    lores = req.make_array('lores')
                finally:
                    req.release()

                ts = self._exposure_wall_ts(md.get('SensorTimestamp'))
                # Y plane only: the luma rows of the YUV420 lores frame are already the grayscale
                # image motion detection wants, so there is nothing to convert.
                lores_y = lores[:self.detection_size[1]].copy()

                with self._cond:
                    self._frames.append((ts, main, lores_y))
                    self._cond.notify_all()
                if stalled:
                    logger.info(f"Camera stream recovered after {time.time() - last_ok:.1f}s")
                    stalled = False
                last_ok = time.time()
            except Exception as e:
                if not stalled and time.time() - last_ok > self.stall_warn_s:
                    logger.warning(f"Camera stream stalled for {time.time() - last_ok:.1f}s: {e}")
                    stalled = True
                time.sleep(0.1)

    def latest(self, wait_timeout=2.0):
        """Block until at least one frame is buffered, then return the newest."""
        deadline = time.time() + wait_timeout
        with self._cond:
            while not self._frames:
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)
            return self._frames[-1]

    def snapshot(self):
        """Return a list of the currently buffered frames, newest last."""
        with self._cond:
            return list(self._frames)


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
        self.image_rotation = _valid_rotation(config.get('image_rotation', 0), 'image_rotation')
        # Mirroring for the visible feed, the counterpart to thermal_hflip/thermal_vflip.
        # image_rotation cannot express a mirror: a rotation preserves left/right handedness
        # and a flip reverses it, so a feed that comes off the sensor mirrored (or a lens or
        # mounting that reverses it) needs this instead. Correcting the two feeds separately
        # is what lets them be brought into agreement with each other.
        self.image_hflip = bool(config.get('image_hflip', False))
        self.image_vflip = bool(config.get('image_vflip', False))

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
        # Frames averaged into the motion baseline. Rebuilt after every trigger, and the recorder is
        # blind while it happens, so this trades shake-suppression against missed motion.
        self.baseline_frames     = max(1, int(config.get('baseline_frames', 3)))

        # Image capture
        self.image_interval  = float(config.get('image_interval', 5.0))
        self.image_quality   = int(config.get('image_quality', 85))

        # Thermal fusion (Waveshare MI48, GPIO/SPI/I2C)
        self.thermal_enabled = bool(config.get('thermal_enabled', False))
        thermal_fps = int(config.get('thermal_fps', 9))

        # Maximum tolerated gap between the visible exposure and the thermal frame fused with it.
        # A capture exceeding this is discarded outright (see `_capture_frame`) — for fast-moving
        # subjects a mistimed pair is not degraded data, it is wrong data, since the animal has
        # physically moved between the two frames.
        #
        # The floor is set by the sensor: with nearest-neighbour selection the worst case is half a
        # thermal frame interval (~56ms at 9fps), so the default is derived from `thermal_fps`
        # rather than fixed — 0.75/fps leaves a little headroom for timing jitter above that
        # half-interval bound. Raising `thermal_fps` tightens this automatically; demanding less
        # than half an interval just discards most captures. An explicit config value overrides.
        self.thermal_max_skew_s = float(
            config.get('thermal_max_skew_s') or 0.75 / max(thermal_fps, 1)
        )
        # Captures dropped for bad sync, so field failure rate is visible in the log.
        self._dropped_unsynced = 0

        # Visible-camera backend. 'picamera2' keeps the camera streaming with a pre-trigger buffer
        # (see CameraStream); 'rpicam' uses a fresh rpicam-still process per capture, which costs
        # ~0.97s of camera init before every exposure. The stream is the default, but the per-capture
        # path is kept as a fallback so a picamera2 problem in the field degrades rather than dies.
        self.capture_backend  = str(config.get('capture_backend', 'picamera2')).lower()
        self.camera_fps       = int(config.get('camera_fps', 15))
        self.camera_buffer_s  = float(config.get('camera_buffer_s', 1.5))
        self._camera = None

        self._thermal = None
        if self.thermal_enabled:
            self._thermal = ThermalStream(
                fps=thermal_fps,
                filters=bool(config.get('thermal_filters', True)),
                offset=float(config.get('thermal_offset', 0.0)),
                out_width=int(config.get('thermal_width', 480)),
                out_height=int(config.get('thermal_height', 372)),
                warmup_frames=int(config.get('thermal_warmup_frames', 5)),
                spi_speed_hz=int(config.get('thermal_spi_speed_hz', 2_000_000)),
                hflip=bool(config.get('thermal_hflip', False)),
                vflip=bool(config.get('thermal_vflip', False)),
                rotation=_valid_rotation(config.get('thermal_rotation', 0), 'thermal_rotation'),
                stall_warn_s=float(config.get('thermal_stall_warn_s', 5.0)),
            )
            self._thermal.start()

        if self.capture_backend == 'picamera2':
            cam = CameraStream(
                width=self._capture_width, height=self._capture_height,
                fps=self.camera_fps,
                detection_width=self.detection_width, detection_height=self.detection_height,
                buffer_s=self.camera_buffer_s,
                stall_warn_s=float(config.get('camera_stall_warn_s', 5.0)),
            )
            if cam.start():
                self._camera = cam
            else:
                logger.warning("Falling back to rpicam-still capture (per-capture camera init)")

        self.outbox_dir.mkdir(parents=True, exist_ok=True)

        logger.info(f"Recorder initialized. outbox: {self.outbox_dir}")
        logger.info(
            f"Mode: {self.mode} | Device: {self._device_id} | "
            f"Resolution: {self.width}x{self.height} | "
            f"Thermal fusion: {'enabled' if self.thermal_enabled else 'disabled'}"
        )
        if self.thermal_enabled:
            logger.info(
                f"Thermal sync tolerance: {self.thermal_max_skew_s * 1000:.0f}ms "
                f"(thermal_fps={thermal_fps}, half-interval floor "
                f"{500 / max(thermal_fps, 1):.0f}ms) | unsynced captures are discarded"
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
            if self._camera:
                self._camera.stop()

    # ── Utilities ─────────────────────────────────────────────────────────────

    def _make_filename_stem(self, at_ts: float = None) -> str:
        """Name the file after the frame's exposure time, not the time it was written.

        With a pre-trigger buffer the two differ: the frame may have been exposed a second before it
        was selected and saved, and it is the exposure instant that the data actually refers to.
        """
        when = datetime.now(timezone.utc) if at_ts is None else datetime.fromtimestamp(at_ts, timezone.utc)
        return f"image_{when.strftime('%Y%m%dT%H%M%SZ')}"

    def _write_sidecar(self, image_path: Path, motion_score: float = 0.0,
                        fmt: str = 'rgb', thermal_stats=None, thermal_skew_ms=None):
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
        if thermal_skew_ms is not None:
            # Recorded so sync is auditable from the uploaded data alone: how far apart in time the
            # two fused frames actually were, signed (+ = thermal newer than visible).
            meta['thermal_skew_ms'] = thermal_skew_ms
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

    def _capture_image_tmp(self, path: Path) -> 'tuple[Path, float] | None':
        """Capture a full-resolution JPEG to a .tmp file. Returns (tmp_path, exposure_ts) on success,
        None on failure. The caller writes the sidecar then renames tmp → path so the image never
        appears without its sidecar.

        `exposure_ts` is a wall-clock estimate of when the frame was actually exposed, needed to
        pair it with the right thermal frame. rpicam-still spends ~0.9s initialising the camera and
        then exits within ~10ms of writing the file (measured on this hardware), so the moment the
        subprocess returns is a good proxy for exposure — whereas the moment it was *launched* is
        almost a second too early.
        """
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
            exposure_ts = time.time()
            if r.returncode == 0 and tmp.exists() and tmp.stat().st_size >= self.min_size_bytes:
                # Rotation happens after the timestamp is taken — it re-encodes a 1920x1080 JPEG and
                # would otherwise inflate the apparent exposure time by its own duration.
                if self.image_rotation or self.image_hflip or self.image_vflip:
                    self._reorient_in_place(tmp)
                logger.info(f"Captured image: {path.name}")
                return tmp, exposure_ts
            logger.warning(f"Image capture failed (code {r.returncode})")
        except FileNotFoundError:
            logger.critical(f"rpicam-still not found at '{self.rpicam_still_path}'")
        except Exception as e:
            logger.error(f"Image capture error: {e}")
        tmp.unlink(missing_ok=True)
        return None

    def _reorient_in_place(self, jpg_path: Path):
        """Correct the camera's mounting rotation and any mirroring (see `image_rotation`,
        `image_hflip`, `image_vflip`).

        Rotation is applied first so the flip axes mean what they say in the final, upright
        image rather than in sensor space. PIL's rotate() angle convention is
        counter-clockwise for positive values, so a clockwise correction needs a negated
        angle."""
        from PIL import Image
        with Image.open(jpg_path) as img:
            out = img.rotate(-self.image_rotation, expand=True) if self.image_rotation else img
            if self.image_hflip:
                out = out.transpose(Image.FLIP_LEFT_RIGHT)
            if self.image_vflip:
                out = out.transpose(Image.FLIP_TOP_BOTTOM)
            # jpg_path is the ".jpg.tmp" working file, so PIL can't infer the encoder from the
            # extension — pass it explicitly.
            out.save(jpg_path, format='JPEG', quality=self.image_quality)

    def _fuse_thermal(self, visible_path: Path, exposure_ts: float):
        """
        Combine the visible JPEG with the latest buffered thermal frame into an RGBA image,
        with the normalized thermal frame stored as the alpha channel. Returns
        (PIL.Image, (temp_min_c, temp_max_c, temp_avg_c), skew_ms), or None if thermal isn't
        usable — not ready yet (still warming up, or the sensor failed to init), or the newest
        frame is too far from `exposure_ts` to honestly belong to the same moment.
        """
        nearest = self._thermal.nearest(exposure_ts)
        if nearest is None:
            logger.warning("No thermal frame available (sensor warming up or failed)")
            return None
        thermal_ts, gray8, stats = nearest

        skew = thermal_ts - exposure_ts
        if abs(skew) > self.thermal_max_skew_s:
            logger.warning(
                f"Thermal/visible skew {skew * 1000:+.0f}ms exceeds "
                f"{self.thermal_max_skew_s * 1000:.0f}ms limit — frames are not simultaneous"
            )
            return None

        from PIL import Image
        visible = Image.open(visible_path).convert('RGB')
        alpha = Image.fromarray(gray8, mode='L').resize(visible.size, Image.BICUBIC)
        fused = visible.copy()
        fused.putalpha(alpha)
        return fused, stats, round(skew * 1000)

    def _select_pair(self, around_ts: float, search_s: float = 0.5):
        """Pick the best-synchronised (visible, thermal) pair from the two ring buffers.

        Returns (visible_ts, main_yuv, thermal_entry, skew) or None if nothing is buffered.

        With both feeds buffered we are no longer forced to take one frame and hunt for a partner:
        we can consider every visible frame near `around_ts` and choose the one that happens to sit
        closest to a thermal frame. At 15fps visible / 9fps thermal the two grids drift in and out
        of alignment, so some visible frames are within a few ms of a thermal frame while others
        are ~50ms away — picking deliberately beats picking arbitrarily.

        Preference order: pairs within the sync tolerance first, and among those the one nearest
        `around_ts` (the moment of interest, e.g. when motion was seen). If nothing is within
        tolerance, the tightest-skew pair is returned so the caller can log a precise reason for
        rejecting the capture.
        """
        frames = self._camera.snapshot()
        if not frames:
            return None

        candidates = [f for f in frames if abs(f[0] - around_ts) <= search_s]
        if not candidates:
            candidates = [min(frames, key=lambda f: abs(f[0] - around_ts))]

        if not self._thermal:
            best = min(candidates, key=lambda f: abs(f[0] - around_ts))
            return best[0], best[1], None, 0.0

        # Ensure the thermal buffer covers the newest candidate before snapshotting it, so a pair
        # isn't rejected merely because the thermal frame that would match it hasn't arrived yet.
        self._thermal.nearest(max(f[0] for f in candidates))
        thermals = self._thermal.snapshot()
        if not thermals:
            return None

        pairs = []
        for vis_ts, main, _lores in candidates:
            th = min(thermals, key=lambda t: abs(t[0] - vis_ts))
            pairs.append((vis_ts, main, th, th[0] - vis_ts))

        in_tolerance = [p for p in pairs if abs(p[3]) <= self.thermal_max_skew_s]
        if in_tolerance:
            return min(in_tolerance, key=lambda p: abs(p[0] - around_ts))
        return min(pairs, key=lambda p: abs(p[3]))

    def _capture_frame_buffered(self, motion_score: float = 0.0, at_ts: float = None):
        """Save a capture from the pre-trigger buffer, with no camera round-trip.

        `at_ts` is the moment of interest — for motion mode, the exposure time of the frame the
        motion was seen in. Because that frame is already in the buffer, the saved image shows the
        scene as it was when motion occurred, not 1.5-3s later as the on-demand path did.
        """
        if at_ts is None:
            latest = self._camera.latest()
            if latest is None:
                logger.warning("No buffered camera frame available")
                return
            at_ts = latest[0]

        sel = self._select_pair(at_ts)
        if sel is None:
            # Distinguish the two causes: no visible frame at all, versus a visible frame with no
            # thermal counterpart yet (normal during thermal warmup). The latter is a real dropped
            # capture and must be counted, not just mentioned.
            if self.thermal_enabled and self._thermal and not self._thermal.snapshot():
                self._dropped_unsynced += 1
                logger.warning(
                    f"Discarded unsynced capture (no thermal frame buffered yet) — "
                    f"{self._dropped_unsynced} dropped so far"
                )
            else:
                logger.warning("No buffered camera frame available")
            return
        vis_ts, main_yuv, thermal, skew = sel

        if self.thermal_enabled:
            if thermal is None or abs(skew) > self.thermal_max_skew_s:
                self._dropped_unsynced += 1
                detail = (f"best available skew {skew * 1000:+.0f}ms exceeds "
                          f"{self.thermal_max_skew_s * 1000:.0f}ms limit"
                          if thermal is not None else "no thermal frame buffered")
                logger.warning(
                    f"Discarded unsynced capture ({detail}) — "
                    f"{self._dropped_unsynced} dropped so far"
                )
                return

        import cv2 as cv
        from PIL import Image

        rgb = cv.cvtColor(main_yuv, cv.COLOR_YUV420p2RGB)
        # Rotate first, then mirror, so the flip axes refer to the upright image — the same
        # order as the rpicam path in _reorient_in_place().
        if self.image_rotation:
            rgb = cv.rotate(rgb, _cv_rotate_map()[self.image_rotation])
        if self.image_hflip:
            rgb = cv.flip(rgb, 1)
        if self.image_vflip:
            rgb = cv.flip(rgb, 0)
        visible = Image.fromarray(rgb)

        stem = self.outbox_dir / self._make_filename_stem(vis_ts)
        if thermal is not None:
            _th_ts, gray8, stats = thermal
            alpha = Image.fromarray(gray8, mode='L').resize(visible.size, Image.BICUBIC)
            visible.putalpha(alpha)
            final_path = stem.with_suffix('.png')
            tmp = final_path.with_suffix('.png.tmp')
            visible.save(tmp, format='PNG', compress_level=1)
            self._write_sidecar(final_path, motion_score=motion_score,
                                 fmt='rgba_thermal_alpha', thermal_stats=stats,
                                 thermal_skew_ms=round(skew * 1000))
            tmp.rename(final_path)
            logger.info(
                f"Captured fused image: {final_path.name} (skew {round(skew * 1000):+d}ms, "
                f"trigger lag {(vis_ts - at_ts) * 1000:+.0f}ms)"
            )
        else:
            final_path = stem.with_suffix('.jpg')
            tmp = final_path.with_suffix('.jpg.tmp')
            visible.save(tmp, format='JPEG', quality=self.image_quality)
            self._write_sidecar(final_path, motion_score=motion_score, fmt='rgb')
            tmp.rename(final_path)
            logger.info(f"Captured image: {final_path.name}")

    def _capture_frame(self, motion_score: float = 0.0, at_ts: float = None):
        """
        Save one capture, from the pre-trigger buffer when the camera is streaming and via a
        fresh rpicam-still process otherwise.

        Capture a visible-light frame and, if thermal fusion is enabled, fuse it with the
        thermal frame nearest in time to its exposure into a single RGBA PNG (thermal in the
        alpha channel), then write the sidecar and move the result into the outbox atomically.

        With thermal fusion enabled a capture is all-or-nothing: if no thermal frame lands close
        enough in time, the visible frame is discarded rather than saved unpaired. With fusion
        disabled a plain JPEG is written as normal.
        """
        if self._camera:
            return self._capture_frame_buffered(motion_score, at_ts)

        stem = self.outbox_dir / self._make_filename_stem()
        captured = self._capture_image_tmp(stem.with_suffix('.jpg'))
        if not captured:
            return
        jpg_tmp, exposure_ts = captured

        fused = self._fuse_thermal(jpg_tmp, exposure_ts) if self._thermal else None

        # With thermal fusion on, a visible frame with no properly-timed thermal counterpart is
        # discarded rather than saved as a plain JPEG: an unpaired or mistimed frame is unusable
        # downstream, and writing one would spend upload bandwidth on data that gets thrown away.
        # The drop is logged (with a running count) so the failure is still visible in the field.
        if self.thermal_enabled and not fused:
            jpg_tmp.unlink(missing_ok=True)
            self._dropped_unsynced += 1
            logger.warning(
                f"Discarded unsynced capture (no usable thermal pair) — "
                f"{self._dropped_unsynced} dropped so far"
            )
            return

        if fused:
            image, stats, skew_ms = fused
            final_path = stem.with_suffix('.png')
            png_tmp = final_path.with_suffix('.png.tmp')
            # compress_level=1, not PIL's default 6: at 1920x1080 RGBA the default costs ~1.45s of
            # CPU per capture, during which the detection loop is blocked and motion is missed.
            # Level 1 is ~8x faster for ~17% more bytes on disk, and those bytes are transient —
            # the uploader re-encodes every file to WebP before transmission anyway, so the extra
            # size never reaches the network.
            image.save(png_tmp, format='PNG', compress_level=1)
            jpg_tmp.unlink(missing_ok=True)
            self._write_sidecar(final_path, motion_score=motion_score,
                                 fmt='rgba_thermal_alpha', thermal_stats=stats,
                                 thermal_skew_ms=skew_ms)
            png_tmp.rename(final_path)
            logger.info(f"Captured fused image: {final_path.name} (skew {skew_ms:+d}ms)")
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

    def _detection_frame(self):
        """Return (grayscale PIL image, exposure_ts) for motion comparison, or (None, None).

        When the camera is streaming this is free — the lores frame is already buffered and its Y
        plane is the grayscale image, so a detection cycle costs no camera round-trip (~59ms vs
        ~583ms for a fresh rpicam-still process). The returned timestamp is the frame's real
        exposure time, which is what a trigger passes to `_capture_frame` to identify the moment
        motion was seen.
        """
        from PIL import Image
        if self._camera:
            latest = self._camera.latest()
            if latest is None:
                return None, None
            ts, _main, lores_y = latest
            return Image.fromarray(lores_y, mode='L'), ts
        frame = self._capture_detection_frame()
        return frame, (time.time() if frame is not None else None)

    def _motion_loop(self, on_motion):
        """
        Run the motion detection loop. Calls on_motion(score, frame_ts) each time motion is
        detected, where frame_ts is the exposure time of the frame the motion was seen in.
        Uses temporal averaging to suppress false triggers from camera shake.
        """
        try:
            from PIL import Image, ImageChops  # noqa — validate import
        except ImportError:
            logger.critical("Pillow is required for motion detection: pip install pillow")
            return

        from PIL import Image

        def build_baseline(n=None):
            """Average a few detection frames into a motion baseline.

            Every frame spent here is a frame not watching for motion, and this runs after each
            trigger as well as at startup — so the frame count is configurable and the inter-frame
            wait is skipped after the last one. The wait between frames is what makes the average
            span a little time (suppressing shake); waiting after the final frame adds nothing but
            blindness.
            """
            n = self.baseline_frames if n is None else n
            avg = None
            captured = 0
            while captured < n and not STOP_FLAG:
                frame, _ts = self._detection_frame()
                if frame is None:
                    time.sleep(1)
                    continue
                avg = frame if avg is None else Image.blend(avg, frame, alpha=0.5)
                captured += 1
                if captured < n:
                    time.sleep(self.detection_interval)
            return avg

        logger.info("Building initial baseline...")
        running_avg = build_baseline()

        while not STOP_FLAG:
            frame, frame_ts = self._detection_frame()
            if frame is None:
                logger.warning("Detection frame failed, retrying in 2s...")
                time.sleep(2)
                continue

            ratio = self._motion_ratio(running_avg, frame)
            if ratio > self.motion_threshold:
                logger.info(f"Motion detected (score={ratio:.4f})")
                on_motion(ratio, frame_ts)
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

        def on_motion(score: float, frame_ts: float = None):
            # frame_ts is when motion was actually seen; with a pre-trigger buffer this selects the
            # frame from that instant rather than whatever the camera can produce now.
            self._capture_frame(motion_score=score, at_ts=frame_ts)

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
        'baseline_frames':    3,
        'image_interval':     5.0,
        'image_quality':      85,
        'image_rotation':     0,
        'image_hflip':        False,
        'image_vflip':        False,
        'thermal_enabled':       False,
        'thermal_fps':           9,
        'thermal_filters':       True,
        'thermal_offset':        0.0,
        'thermal_warmup_frames': 5,
        'thermal_width':         480,
        'thermal_height':        372,
        'thermal_spi_speed_hz':  2_000_000,
        'capture_backend':       'picamera2',
        'camera_fps':            15,
        'camera_buffer_s':       1.5,
        'camera_stall_warn_s':   5.0,
        'thermal_hflip':         False,
        'thermal_vflip':         False,
        'thermal_rotation':      0,
        # None → derived from thermal_fps at runtime (see Recorder.__init__)
        'thermal_max_skew_s':    None,
        'thermal_stall_warn_s':  5.0,
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
