#!/usr/bin/env python3
"""
Pi Monitoring Pipeline - Video Recorder
Records video from camera in fixed-duration MP4 chunks using libcamera and writes them to the outbox.

Design:
- Uses `rpicam-vid` (libcamera command-line tool) to record video directly to MP4.
- Writes to a temporary filename (".tmp") then renames atomically to `.mp4` when the chunk completes.
- Configurable via a JSON config file or CLI args.
"""

import os
import sys
import time
import json
import signal
import logging
import shutil
import subprocess
from pathlib import Path
from datetime import datetime
import threading

# Configure logging (similar to uploader)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('/var/log/monitoring-pipeline.log'),
        logging.StreamHandler()
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


class VideoRecorder:
    def __init__(self, config):
        self.outbox_dir = Path(config.get('outbox_dir', '/outbox'))
        self.chunk_duration = int(config.get('chunk_duration', 60))  # seconds
        self.bitrate = config.get('bitrate', '5Mbps')  # libcamera format: e.g. "5Mbps"
        self.width = int(config.get('width', 1920))
        self.height = int(config.get('height', 1080))
        self.framerate = int(config.get('framerate', 30))
        self.camera_id = int(config.get('camera_id', 0))  # Camera index
        self.rpicam_vid_path = config.get('rpicam_vid_path', 'rpicam-vid')
        self.min_size_bytes = int(config.get('min_size_bytes', 1024))
        self.mode = config.get('mode', 'segment')

        # Internal helpers for segment mode
        self._renamer_stop = threading.Event()
        self._renamer_thread = None

        self.outbox_dir.mkdir(parents=True, exist_ok=True)

        logger.info(f"Recorder initialized. outbox: {self.outbox_dir}")
        logger.info(
            f"Mode: {self.mode}; Camera: {self.camera_id}, chunk: {self.chunk_duration}s, resolution: {self.width}x{self.height}, fps: {self.framerate}, bitrate: {self.bitrate}"
        )

    def _make_filename(self):
        now = datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')
        return f"video_{now}.mp4"

    def _run_rpicam_vid(self, tmp_path: Path) -> int:
        cmd = [
            self.rpicam_vid_path,
            '--camera', str(self.camera_id),
            '--width', str(self.width),
            '--height', str(self.height),
            '--framerate', str(self.framerate),
            '--bitrate', self.bitrate,
            '--timeout', str(self.chunk_duration * 1000),  # rpicam-vid uses milliseconds
            '--codec', 'h264',
            '-o', str(tmp_path)
        ]

        logger.info(f"Starting rpicam-vid: {' '.join(cmd)}")
        try:
            # Run rpicam-vid and wait until chunk is recorded
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            out, err = proc.communicate()
            if proc.returncode != 0:
                logger.error(f"rpicam-vid failed (code {proc.returncode}): {err.decode(errors='ignore')}" )
            return proc.returncode
        except FileNotFoundError:
            logger.critical(f"rpicam-vid not found at '{self.rpicam_vid_path}'. Please install libcamera-apps or set rpicam_vid_path.")
            return 127
        except Exception as e:
            logger.error(f"Error running rpicam-vid: {e}")
            return 1

    def start(self):
        self._start_segment_mode()

    def _start_segment_mode(self):
        """Run rpicam-vid to record MP4 video chunks.
        Writes segmented `.mp4.tmp` files, and finalizes them by renaming to `.mp4` once stable."""
        logger.info(f"Starting video recorder with {self.chunk_duration}s chunks")
        restart_delay = 5

        while not STOP_FLAG:
            try:
                tmp_path = self.outbox_dir / self._make_filename().replace('.mp4', '.mp4.tmp')
                
                # Record directly to MP4
                rpicam_cmd = [
                    self.rpicam_vid_path,
                    '--camera', str(self.camera_id),
                    '--width', str(self.width),
                    '--height', str(self.height),
                    '--framerate', str(self.framerate),
                    '--bitrate', self.bitrate,
                    '--timeout', str(self.chunk_duration * 1000),  # milliseconds
                    '-o', str(tmp_path)
                ]
                
                logger.info(f"Recording chunk: {tmp_path.name}")
                # Record H.264 stream
                proc = subprocess.Popen(rpicam_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            except FileNotFoundError as e:
                logger.critical(f"rpicam-vid not found at '{self.rpicam_vid_path}'. Please install libcamera-apps.")
                return

            # Start renamer helper if needed
            if self._renamer_thread is None or not self._renamer_thread.is_alive():
                self._renamer_stop.clear()
                self._renamer_thread = threading.Thread(target=self._renamer_loop, daemon=True)
                self._renamer_thread.start()

            # Monitor the process; restart if it dies
            while True:
                if STOP_FLAG:
                    break
                rc = proc.poll()
                if rc is not None:
                    # Capture any remaining output from rpicam-vid
                    try:
                        out, err = proc.communicate(timeout=5)
                        if out:
                            logger.debug(f"rpicam-vid stdout: {out.decode(errors='ignore')}")
                        if err:
                            logger.debug(f"rpicam-vid stderr: {err.decode(errors='ignore')}")
                    except Exception:
                        pass

                    if rc != 0:
                        logger.warning(f"rpicam-vid exited with code {rc}")
                    # Continue to next chunk
                    break
                time.sleep(0.5)

            # If stopping, ask rpicam-vid to finish gracefully
            if STOP_FLAG:
                try:
                    proc.send_signal(signal.SIGINT)
                    logger.info("Sent SIGINT to rpicam-vid to finalize current chunk")
                    proc.wait(timeout=10 + self.chunk_duration)
                except Exception:
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                break
            else:
                # Not stopping: process exited normally or timed out, continue to next chunk
                try:
                    proc.terminate()
                except Exception:
                    pass

        # Stopping: stop renamer and run final pass
        self._renamer_stop.set()
        if self._renamer_thread:
            self._renamer_thread.join(timeout=5)
        self._do_renamer_pass()

        logger.info("Recorder stopping gracefully.")

    def _renamer_loop(self):
        while not self._renamer_stop.is_set():
            self._do_renamer_pass()
            self._renamer_stop.wait(1.0)
        # final pass
        self._do_renamer_pass()

    def _do_renamer_pass(self):
        for tmp in self.outbox_dir.glob('*.mp4.tmp'):
            try:
                size1 = tmp.stat().st_size
                time.sleep(0.5)
                size2 = tmp.stat().st_size
                if size1 == size2 and size2 >= self.min_size_bytes:
                    final_name = tmp.name[:-4]  # strip '.tmp'
                    final_path = tmp.with_name(final_name)
                    try:
                        tmp.rename(final_path)
                        logger.info(f"Finalized segment: {final_path.name}")
                    except Exception as e:
                        logger.error(f"Failed to rename segment {tmp} -> {final_path}: {e}")
            except FileNotFoundError:
                continue
            except Exception as e:
                logger.error(f"Error while finalizing segments: {e}")

def load_config(path: str = None):
    if path is None:
        # default to the repository install location (single-folder layout)
        path = '/opt/monitoring-pipeline/config/client.json' 

    cfg = {}
    if os.path.exists(path):
        try:
            with open(path) as f:
                cfg = json.load(f)
        except Exception as e:
            logger.warning(f"Failed to load config file {path}: {e}")

    # Merge recording defaults
    rec = cfg.get('recording', {})
    defaults = {
        'camera_id': 0,
        'chunk_duration': 60,
        'width': 1920,
        'height': 1080,
        'framerate': 30,
        'bitrate': '5Mbps',
        'rpicam_vid_path': 'rpicam-vid',
        'min_size_bytes': 1024,
        'mode': 'segment'
    }

    merged = defaults.copy()
    merged.update(rec)

    # Base config
    base = {
        'outbox_dir': cfg.get('outbox_dir', '/outbox')
    }
    base.update(merged)
    return base


if __name__ == '__main__':
    cfg_path = 'config/client.json'
    if len(sys.argv) > 1 and sys.argv[1] == '--config' and len(sys.argv) > 2:
        cfg_path = sys.argv[2]

    config = load_config(cfg_path)
    recorder = VideoRecorder(config)
    try:
        recorder.start()
    except KeyboardInterrupt:
        logger.info('Recorder stopped by user')
        sys.exit(0)
