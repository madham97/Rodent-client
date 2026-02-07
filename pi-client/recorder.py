#!/usr/bin/env python3
"""
Pi Monitoring Pipeline - Audio Recorder
Records audio from a microphone in fixed-duration MP4 chunks and writes them to the outbox.

Design:
- Uses `ffmpeg` (external dependency) to record ALSA device directly to MP4 with AAC audio.
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


class AudioRecorder:
    def __init__(self, config):
        self.outbox_dir = Path(config.get('outbox_dir', '/outbox'))
        self.device = config.get('device', 'plughw:1,0')
        self.chunk_duration = int(config.get('chunk_duration', 60))  # seconds
        self.sample_rate = int(config.get('sample_rate', 44100))
        self.channels = int(config.get('channels', 1))
        self.bitrate_kbps = int(config.get('bitrate_kbps', 128))
        self.ffmpeg_path = config.get('ffmpeg_path', 'ffmpeg')
        self.min_size_bytes = int(config.get('min_size_bytes', 1024))
        self.mode = config.get('mode', 'segment')

        # Internal helpers for segment mode
        self._renamer_stop = threading.Event()
        self._renamer_thread = None

        self.outbox_dir.mkdir(parents=True, exist_ok=True)

        logger.info(f"Recorder initialized. outbox: {self.outbox_dir}")
        logger.info(
            f"Mode: {self.mode}; Device: {self.device}, chunk: {self.chunk_duration}s, sr: {self.sample_rate}, ch: {self.channels}, bitrate: {self.bitrate_kbps}kbps"
        )

    def _make_filename(self):
        now = datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')
        return f"audio_{now}.mp4"

    def _run_ffmpeg(self, tmp_path: Path) -> int:
        cmd = [
            self.ffmpeg_path,
            '-f', 'alsa',
            '-ac', str(self.channels),
            '-ar', str(self.sample_rate),
            '-i', self.device,
            '-map', '0:a',
            '-t', str(self.chunk_duration),
            '-c:a', 'aac',
            '-b:a', f"{self.bitrate_kbps}k",
            '-y',  # overwrite if exists
            str(tmp_path)
        ]

        logger.info(f"Starting ffmpeg: {' '.join(cmd)}")
        try:
            # Run ffmpeg and wait until chunk is recorded
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            out, err = proc.communicate()
            if proc.returncode != 0:
                logger.error(f"ffmpeg failed (code {proc.returncode}): {err.decode(errors='ignore')}" )
            return proc.returncode
        except FileNotFoundError:
            logger.critical(f"ffmpeg not found at '{self.ffmpeg_path}'. Please install ffmpeg or set ffmpeg_path.")
            return 127
        except Exception as e:
            logger.error(f"Error running ffmpeg: {e}")
            return 1

    def start(self):
        self._start_segment_mode()

    def _start_segment_mode(self):
        """Run a persistent ffmpeg process that writes segmented `.mp4.tmp` files, and
        finalize them by renaming to `.mp4` once they are stable."""
        pattern = str(self.outbox_dir / "audio_%Y%m%dT%H%M%SZ.mp4.tmp")
        cmd = [
            self.ffmpeg_path,
            '-f', 'alsa',
            '-ac', str(self.channels),
            '-ar', str(self.sample_rate),
            '-i', self.device,  # already set to plughw
            '-map', '0:a',  # ensure audio stream is selected for segment outputs
            '-c:a', 'aac',
            '-b:a', f"{self.bitrate_kbps}k",
            '-f', 'segment',
            '-segment_time', str(self.chunk_duration),
            '-segment_format', 'mp4',
            '-reset_timestamps', '1',
            '-movflags', '+frag_keyframe+empty_moov+default_base_moof',
            '-strftime', '1',
            pattern
        ]

        logger.info(f"Starting ffmpeg segmenter: {' '.join(cmd)}")
        restart_delay = 5

        while not STOP_FLAG:
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            except FileNotFoundError:
                logger.critical(f"ffmpeg not found at '{self.ffmpeg_path}'. Please install ffmpeg or set ffmpeg_path.")
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
                    # Capture any remaining output from ffmpeg so we can see the real error
                    try:
                        out, err = proc.communicate(timeout=5)
                        if out:
                            logger.debug(f"ffmpeg stdout: {out.decode(errors='ignore')}")
                        if err:
                            logger.error(f"ffmpeg stderr: {err.decode(errors='ignore')}")
                    except Exception:
                        # If communicate fails for any reason, continue - we still restart
                        pass

                    logger.error(f"ffmpeg segmenter exited with code {rc}; restarting in {restart_delay}s")
                    break
                time.sleep(0.5)

            # If stopping, ask ffmpeg to finish gracefully
            if STOP_FLAG:
                try:
                    proc.send_signal(signal.SIGINT)
                    logger.info("Sent SIGINT to ffmpeg to finalize current segment")
                    proc.wait(timeout=10 + self.chunk_duration)
                except Exception:
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                break
            else:
                # Not stopping: kill and restart after a small backoff
                try:
                    proc.terminate()
                except Exception:
                    pass
                time.sleep(restart_delay)

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
        'device': 'hw:0',
        'chunk_duration': 60,
        'sample_rate': 44100,
        'channels': 1,
        'bitrate_kbps': 128,
        'ffmpeg_path': 'ffmpeg',
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
    recorder = AudioRecorder(config)
    try:
        recorder.start()
    except KeyboardInterrupt:
        logger.info('Recorder stopped by user')
        sys.exit(0)
