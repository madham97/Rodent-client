#!/usr/bin/env python3
"""
Pi Monitoring Pipeline - Uploader

Monitors the outbox directory and uploads files to the remote server using
the SIM800's built-in AT+HTTP stack. No pppd required.
"""

import json
import logging
import shutil
import sys
import time
from pathlib import Path

import serial

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('/var/log/monitoring-pipeline.log'),
        logging.StreamHandler(),
    ]
)
logger = logging.getLogger(__name__)

BOUNDARY = 'PiPipeline'
MAX_FILE_BYTES = 300_000  # SIM800 internal HTTP buffer limit


def _to_webp(file_path: Path, quality: int) -> tuple[bytes, str]:
    """Re-encode a JPEG as WebP in memory. Returns (webp_bytes, webp_filename)."""
    from PIL import Image
    import io
    with Image.open(file_path) as img:
        buf = io.BytesIO()
        img.save(buf, format='WEBP', quality=quality)
        return buf.getvalue(), file_path.stem + '.webp'


def build_multipart(file_path: Path, webp_compress: bool = False, webp_quality: int = 80,
                    metadata: dict = None) -> bytes:
    """Wrap file in a multipart/form-data body. Optionally re-encodes JPEGs as WebP for smaller transfer."""
    is_image = file_path.suffix.lower() in ('.jpg', '.jpeg')
    if is_image and webp_compress:
        data, filename = _to_webp(file_path, webp_quality)
        field        = 'image'
        content_type = 'image/webp'
    else:
        data, filename = file_path.read_bytes(), file_path.name
        field        = 'image' if is_image else 'video'
        content_type = 'image/jpeg' if is_image else 'video/mp4'
    parts = [(
        f'--{BOUNDARY}\r\n'
        f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
        f'Content-Type: {content_type}\r\n'
        f'\r\n'
    ).encode() + data]
    for key, value in (metadata or {}).items():
        if value is None:
            continue
        parts.append((
            f'\r\n--{BOUNDARY}\r\n'
            f'Content-Disposition: form-data; name="{key}"\r\n'
            f'\r\n'
            f'{value}'
        ).encode())
    parts.append(f'\r\n--{BOUNDARY}--\r\n'.encode())
    return b''.join(parts)


class SIM800:
    """SIM800 modem controlled entirely via AT commands over a serial port."""

    def __init__(self, device='/dev/serial0', baud=115200, pin='', apn='web.vodafone.de'):
        self.device = device
        self.baud   = baud
        self.pin    = pin
        self.apn    = apn
        self._ser   = None

    # ── Serial helpers ─────────────────────────────────────────────────────────

    def open(self):
        self._ser = serial.Serial(self.device, self.baud, timeout=2, rtscts=True)
        logger.info(f"Serial port {self.device} opened")

    def close(self):
        if self._ser and self._ser.is_open:
            self._ser.close()

    def _send(self, cmd, wait=0.4):
        """Send one AT command, return the raw text response."""
        self._ser.reset_input_buffer()
        self._ser.write(f'{cmd}\r'.encode())
        time.sleep(wait)
        raw = self._ser.read(self._ser.in_waiting or 512)
        return raw.decode('ascii', errors='ignore').strip()

    def _wait_for(self, token, timeout=30):
        """Read from serial until token appears or timeout. Returns full buffer."""
        deadline = time.time() + timeout
        buf = ''
        while time.time() < deadline:
            chunk = self._ser.read(self._ser.in_waiting or 1)
            if chunk:
                buf += chunk.decode('ascii', errors='ignore')
                if token in buf:
                    return buf
            else:
                time.sleep(0.05)
        return buf

    # ── Initialization ─────────────────────────────────────────────────────────

    def wait_ready(self, timeout=30):
        """Poll AT until the modem replies OK."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            self._ser.reset_input_buffer()
            if 'OK' in self._send('AT', wait=0.5):
                return True
            time.sleep(1)
        return False

    def unlock_sim(self):
        """Unlock SIM PIN if required. Returns True when SIM is ready."""
        resp = self._send('AT+CPIN?', wait=1)
        if '+CPIN: READY' in resp:
            logger.info("SIM ready")
            return True
        if '+CPIN: SIM PIN' in resp:
            if not self.pin:
                logger.error("SIM requires PIN but gsm_pin is not configured")
                return False
            resp = self._send(f'AT+CPIN={self.pin}', wait=3)
            if 'OK' in resp or 'READY' in resp:
                logger.info("SIM PIN unlocked")
                time.sleep(3)
                return True
            logger.error(f"PIN unlock failed: {resp}")
            return False
        logger.error(f"Unexpected CPIN response: {resp}")
        return False

    def wait_registered(self, timeout=60):
        """Wait until the modem registers on the GSM network."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            resp = self._send('AT+CREG?', wait=0.5)
            if '+CREG: 0,1' in resp or '+CREG: 0,5' in resp:
                logger.info("Registered on network")
                return True
            time.sleep(2)
        logger.error("Timed out waiting for network registration")
        return False

    def initialize(self):
        """Full startup: wait for modem, unlock SIM, wait for registration."""
        logger.info("Waiting for modem to respond...")
        if not self.wait_ready():
            logger.error("Modem did not respond within timeout")
            return False
        self._send('ATZ', wait=1)   # factory reset
        self._send('ATE0')          # disable echo
        if not self.unlock_sim():
            return False
        if not self.wait_registered():
            return False
        self.enable_sms_text_mode()
        logger.info(f"Modem ready. Signal: {self.get_signal()}/31")
        return True

    def get_signal(self):
        try:
            resp = self._send('AT+CSQ')
            if '+CSQ:' in resp:
                return int(resp.split('+CSQ: ')[1].split(',')[0])
        except Exception:
            pass
        return 0

    # ── SMS ────────────────────────────────────────────────────────────────────

    def enable_sms_text_mode(self):
        """Switch to text mode so SMS bodies are human-readable strings."""
        resp = self._send('AT+CMGF=1', wait=0.5)
        if 'OK' in resp:
            logger.info("SMS text mode enabled")
        else:
            logger.warning(f"SMS text mode failed: {resp!r}")

    def read_sms(self) -> list[tuple[int, str, str]]:
        """Read all unread SMS messages. Returns list of (index, sender, body)."""
        resp = self._send('AT+CMGL="REC UNREAD"', wait=2)
        messages = []
        lines = resp.splitlines()
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if line.startswith('+CMGL:'):
                try:
                    # +CMGL: <index>,"REC UNREAD","<sender>","","<timestamp>"
                    parts = line[len('+CMGL:'):].split(',')
                    index = int(parts[0].strip())
                    sender = parts[2].strip().strip('"')
                    # next non-empty line is the message body
                    i += 1
                    while i < len(lines) and not lines[i].strip():
                        i += 1
                    body = lines[i].strip() if i < len(lines) else ''
                    messages.append((index, sender, body))
                except (IndexError, ValueError):
                    pass
            i += 1
        return messages

    def delete_sms(self, index: int):
        """Delete a message by its SIM slot index."""
        self._send(f'AT+CMGD={index}', wait=0.5)

    # ── Bearer (GPRS data connection) ──────────────────────────────────────────

    def bearer_open(self):
        """Open the GPRS bearer used by the HTTP stack."""
        self._send(f'AT+SAPBR=3,1,"Contype","GPRS"')
        self._send(f'AT+SAPBR=3,1,"APN","{self.apn}"')
        resp = self._send('AT+SAPBR=1,1', wait=5)
        if 'ERROR' in resp:
            # Check if it's already open
            status = self._send('AT+SAPBR=2,1', wait=1)
            if ',1,' in status:
                return True
            logger.error(f"Bearer open failed: {resp}")
            return False
        logger.info("GPRS bearer opened")
        return True

    def bearer_close(self):
        self._send('AT+SAPBR=0,1', wait=2)
        logger.info("GPRS bearer closed")

    # ── HTTP ───────────────────────────────────────────────────────────────────

    def http_post(self, url: str, body: bytes, content_type: str) -> int:
        """
        POST body to url using the modem's internal HTTP stack.
        Returns the HTTP status code, or 0 on failure.
        """
        self._send('AT+HTTPTERM', wait=1)   # clean up any previous session

        resp = self._send('AT+HTTPINIT', wait=1)
        if 'ERROR' in resp:
            # HTTP stack stuck — full bearer cycle to reset it
            logger.info("HTTPINIT failed, cycling bearer...")
            self.bearer_close()
            time.sleep(3)
            self.bearer_open()
            self._send('AT+HTTPTERM', wait=1)
            resp = self._send('AT+HTTPINIT', wait=2)
        if 'ERROR' in resp:
            logger.error("HTTPINIT failed after bearer reset")
            return 0

        self._send('AT+HTTPPARA="CID",1')
        self._send(f'AT+HTTPPARA="URL","{url}"')
        self._send(f'AT+HTTPPARA="CONTENT","{content_type}"')

        # Tell the modem how many bytes we'll send (give 60 s to receive them)
        resp = self._send(f'AT+HTTPDATA={len(body)},60000', wait=1)
        if 'DOWNLOAD' not in resp:
            resp += self._wait_for('DOWNLOAD', timeout=5)
        if 'DOWNLOAD' not in resp:
            logger.error(f"Modem did not enter DOWNLOAD state: {resp}")
            self._send('AT+HTTPTERM')
            return 0

        # Write body in small chunks to avoid overwhelming the SIM800 receive buffer
        chunk_size = 512
        for i in range(0, len(body), chunk_size):
            self._ser.write(body[i:i + chunk_size])
            self._ser.flush()
            time.sleep(0.02)
        self._wait_for('OK', timeout=10)

        # Fire the POST — GPRS round-trips can take up to 2 minutes
        self._send('AT+HTTPACTION=1', wait=0.5)
        result = self._wait_for('+HTTPACTION:', timeout=180)
        # Read a bit more to capture the full line after the token arrives
        time.sleep(0.5)
        tail = self._ser.read(self._ser.in_waiting or 0)
        result += tail.decode('ascii', errors='ignore')
        self._send('AT+HTTPTERM', wait=0.5)

        # Parse: +HTTPACTION: 1,<status_code>,<response_bytes>
        if '+HTTPACTION:' in result:
            try:
                parts = result.split('+HTTPACTION:')[1].strip().split(',')
                return int(parts[1])
            except Exception:
                pass

        logger.error(f"Could not parse HTTPACTION response: {result!r}")
        return 0


SMS_CHECK_INTERVAL = 60  # seconds between SMS inbox polls


class Uploader:
    def __init__(self, config, config_path=None):
        self.config      = config
        self.config_path = config_path

        self.outbox_dir    = Path(config.get('outbox_dir', '/outbox'))
        self.uploaded_dir  = Path(config.get('uploaded_dir', '/uploaded'))
        self.upload_url    = config.get('server_url', 'http://localhost:8000') + '/upload'
        self.max_retries   = int(config.get('max_retries', 3))
        self.retry_delay   = int(config.get('retry_delay', 60))
        self.poll_interval = int(config.get('poll_interval', 10))

        self.outbox_dir.mkdir(parents=True, exist_ok=True)
        self.uploaded_dir.mkdir(parents=True, exist_ok=True)

        self.webp_compress = bool(config.get('webp_compress', False))
        self.webp_quality  = int(config.get('webp_quality', 80))

        self._last_sms_check = 0.0

        self.modem = SIM800(
            device=config.get('gsm_device', '/dev/serial0'),
            pin=config.get('gsm_pin', ''),
            apn=config.get('gsm_apn', 'web.vodafone.de'),
        )

        logger.info(f"Uploader initialized. Upload URL: {self.upload_url}")
        if self.webp_compress:
            logger.info(f"WebP compression enabled (quality {self.webp_quality})")

    def _get_oldest_file(self):
        try:
            items = [
                p for p in self.outbox_dir.glob('*')
                if not p.name.endswith('.tmp')
                and not p.name.endswith('.json')
                and p.exists()
                and (not p.is_file() or p.stat().st_size > 0)
            ]
            return min(items, key=lambda p: p.stat().st_mtime) if items else None
        except Exception as e:
            logger.error(f"Error reading outbox: {e}")
            return None

    def _upload(self, file_path: Path, attempt=0) -> bool:
        size = file_path.stat().st_size
        logger.info(f"Uploading {file_path.name} ({size} bytes, attempt {attempt + 1})")
        t0 = time.time()

        if size > MAX_FILE_BYTES:
            logger.error(
                f"{file_path.name} is {size} bytes — exceeds SIM800 HTTP buffer "
                f"({MAX_FILE_BYTES} bytes). Moving aside and skipping."
            )
            try:
                shutil.move(str(file_path), str(self.uploaded_dir / file_path.name))
            except FileNotFoundError:
                pass  # already moved externally
            return True

        sidecar = file_path.with_suffix('.json')
        metadata = None
        if sidecar.exists():
            try:
                with open(sidecar) as f:
                    metadata = json.load(f)
            except Exception as e:
                logger.warning(f"Could not read sidecar {sidecar.name}: {e}")

        body         = build_multipart(file_path, self.webp_compress, self.webp_quality, metadata)
        content_type = f'multipart/form-data; boundary={BOUNDARY}'

        status = self.modem.http_post(self.upload_url, body, content_type)
        if status == 200:
            elapsed = time.time() - t0
            logger.info(f"Upload successful: {file_path.name} ({elapsed:.1f}s, {size / elapsed / 1024:.1f} KB/s)")
            sidecar.unlink(missing_ok=True)
            return True

        logger.warning(f"Upload failed (HTTP {status}): {file_path.name}")

        # 0 = parse failure, 601 = SIM800 network error — re-open bearer
        if status in (0, 601):
            logger.info("Attempting to re-open GPRS bearer...")
            self.modem.bearer_open()

        return False

    def _process_oldest(self) -> bool:
        oldest = self._get_oldest_file()
        if not oldest:
            return False

        for attempt in range(self.max_retries):
            if self._upload(oldest, attempt):
                try:
                    shutil.move(str(oldest), str(self.uploaded_dir / oldest.name))
                    logger.info(f"Moved to uploaded: {oldest.name}")
                except Exception as e:
                    logger.error(f"Error moving {oldest.name}: {e}")
                return True
            if attempt < self.max_retries - 1:
                logger.info(f"Retrying in {self.retry_delay}s...")
                time.sleep(self.retry_delay)

        logger.error(f"Failed to upload {oldest.name} after {self.max_retries} attempts")
        return False

    def _deep_merge(self, base: dict, patch: dict) -> dict:
        """Recursively merge patch into base, returning a new dict."""
        result = base.copy()
        for key, value in patch.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = self._deep_merge(result[key], value)
            else:
                result[key] = value
        return result

    def _apply_config_patch(self, patch: dict):
        """Merge patch into the live config, update live settings, and persist to disk."""
        self.config = self._deep_merge(self.config, patch)

        # Apply uploader settings that can take effect without a restart
        if 'poll_interval' in patch:
            self.poll_interval = int(patch['poll_interval'])
        if 'max_retries' in patch:
            self.max_retries = int(patch['max_retries'])
        if 'retry_delay' in patch:
            self.retry_delay = int(patch['retry_delay'])
        if 'webp_compress' in patch:
            self.webp_compress = bool(patch['webp_compress'])
        if 'webp_quality' in patch:
            self.webp_quality = int(patch['webp_quality'])

        if self.config_path:
            try:
                with open(self.config_path, 'w') as f:
                    json.dump(self.config, f, indent=2)
                logger.info(f"Config saved to {self.config_path}")
            except Exception as e:
                logger.error(f"Failed to save config: {e}")

        if any(k in patch for k in ('recording', 'gsm_device', 'gsm_apn')):
            logger.info("Recording/GSM settings updated — restarting recorder service...")
            try:
                import subprocess
                subprocess.run(
                    ['systemctl', 'restart', 'monitoring-pipeline-recorder'],
                    check=True, timeout=15,
                )
                logger.info("Recorder service restarted successfully")
            except Exception as e:
                logger.error(f"Failed to restart recorder service: {e}")

    def _check_sms_config(self):
        """Poll the SMS inbox and apply any JSON config patches found."""
        messages = self.modem.read_sms()
        for index, sender, body in messages:
            logger.info(f"SMS from {sender}: {body!r}")
            try:
                patch = json.loads(body)
                if not isinstance(patch, dict):
                    raise ValueError("SMS body must be a JSON object")
                self._apply_config_patch(patch)
                logger.info(f"Config updated via SMS from {sender}: keys={list(patch.keys())}")
            except json.JSONDecodeError:
                logger.warning(f"SMS from {sender} is not valid JSON — ignoring")
            except Exception as e:
                logger.warning(f"Failed to apply SMS config from {sender}: {e}")
            finally:
                self.modem.delete_sms(index)

    def run(self):
        self.modem.open()
        try:
            while not self.modem.initialize():
                logger.info("Modem init failed, retrying in 15s...")
                time.sleep(15)

            if not self.modem.bearer_open():
                logger.error("Could not open GPRS bearer — check APN settings")
                sys.exit(1)

            logger.info("Uploader running.")
            while True:
                if time.time() - self._last_sms_check >= SMS_CHECK_INTERVAL:
                    self._last_sms_check = time.time()
                    self._check_sms_config()

                processed = self._process_oldest()
                time.sleep(2 if processed else self.poll_interval)

        except KeyboardInterrupt:
            logger.info("Uploader stopped by user")
        except Exception as e:
            logger.critical(f"Fatal error: {e}")
            sys.exit(1)
        finally:
            self.modem.bearer_close()
            self.modem.close()


def load_config(path=None):
    if path is None:
        path = '/opt/monitoring-pipeline/config/client.json'
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"Failed to load config {path}: {e} — using defaults")
    return {
        'outbox_dir':    '/outbox',
        'uploaded_dir':  '/uploaded',
        'server_url':    'http://localhost:8000',
        'max_retries':   3,
        'retry_delay':   60,
        'poll_interval': 10,
    }


if __name__ == '__main__':
    cfg_path = 'config/client.json'
    if len(sys.argv) > 2 and sys.argv[1] == '--config':
        cfg_path = sys.argv[2]

    Uploader(load_config(cfg_path), config_path=cfg_path).run()
