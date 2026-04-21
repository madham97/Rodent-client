#!/usr/bin/env python3
"""
Pi Monitoring Pipeline - Uploader

Monitors the outbox directory and uploads files to the remote server using
the SIM800's built-in AT+HTTP stack. No pppd required.
"""

import json
import logging
import shutil
import subprocess
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
IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.webp'}

# Keys settable via SMS. (json_section, type, requires_recorder_restart)
# section=None means top-level in client.json; 'recording' means the recording sub-dict.
_SMS_KEY_SPEC = {
    'motion_threshold':   ('recording', float, False),
    'motion_cooldown':    ('recording', float, False),
    'detection_interval': ('recording', float, False),
    'image_interval':     ('recording', float, False),
    'image_quality':      ('recording', int,   False),
    'mode':               ('recording', str,   True),
    'webp_compress':      (None,        bool,  False),
    'webp_quality':       (None,        int,   False),
}


class SMSConfigHandler:
    """Parse and apply config changes delivered via SMS."""

    def __init__(self, config_path: str):
        self._path = config_path

    def handle(self, text: str) -> str:
        cmd = text.strip()
        if cmd.upper() == 'STATUS':
            return self._status()
        if cmd.upper().startswith('SET '):
            return self._apply_set(cmd[4:].strip())
        return 'ERR: unknown command. Use: SET key=value [key=value ...] or STATUS'

    def _load(self):
        with open(self._path) as f:
            return json.load(f)

    def _save(self, cfg):
        with open(self._path, 'w') as f:
            json.dump(cfg, f, indent=2)
            f.write('\n')

    def _status(self) -> str:
        try:
            cfg = self._load()
            rec = cfg.get('recording', {})
            return (
                f"mode={rec.get('mode', '?')} "
                f"threshold={rec.get('motion_threshold', '?')} "
                f"interval={rec.get('image_interval', '?')} "
                f"quality={rec.get('image_quality', '?')}"
            )
        except Exception as e:
            return f'ERR: {e}'

    def _apply_set(self, args: str) -> str:
        pairs = {}
        for token in args.split():
            if '=' not in token:
                return f'ERR: bad syntax "{token}" — use key=value'
            k, _, v = token.partition('=')
            k = k.strip().lower()
            if k not in _SMS_KEY_SPEC:
                return f'ERR: unknown key "{k}"'
            pairs[k] = v.strip()

        if not pairs:
            return 'ERR: no key=value pairs found'

        try:
            cfg = self._load()
        except Exception as e:
            return f'ERR: cannot read config: {e}'

        applied = []
        needs_restart = False

        for k, v in pairs.items():
            section, cast, restarts = _SMS_KEY_SPEC[k]
            try:
                coerced = v.lower() in ('1', 'true', 'yes') if cast is bool else cast(v)
            except (ValueError, TypeError):
                return f'ERR: bad value "{v}" for {k}'
            target = cfg.setdefault(section, {}) if section else cfg
            target[k] = coerced
            applied.append(f'{k}={coerced}')
            if restarts:
                needs_restart = True

        try:
            self._save(cfg)
        except Exception as e:
            return f'ERR: cannot write config: {e}'

        reply = 'OK: ' + ' '.join(applied)
        if needs_restart:
            try:
                subprocess.run(
                    ['sudo', 'systemctl', 'restart', 'monitoring-pipeline-recorder'],
                    capture_output=True, timeout=10,
                )
                reply += ' (recorder restarted)'
            except Exception as e:
                reply += f' (restart failed: {e})'
        return reply


def _to_webp(file_path: Path, quality: int) -> tuple[bytes, str]:
    """Re-encode a JPEG as WebP in memory. Returns (webp_bytes, webp_filename)."""
    from PIL import Image
    import io
    with Image.open(file_path) as img:
        buf = io.BytesIO()
        img.save(buf, format='WEBP', quality=quality)
        return buf.getvalue(), file_path.stem + '.webp'


def build_multipart(file_path: Path, metadata: dict = None, webp_compress: bool = False, webp_quality: int = 80) -> bytes:
    """Wrap image file in a multipart/form-data body with metadata text fields."""
    if metadata is None:
        metadata = {}

    is_jpeg = file_path.suffix.lower() in ('.jpg', '.jpeg')
    if is_jpeg and webp_compress:
        data, filename = _to_webp(file_path, webp_quality)
        content_type = 'image/webp'
    else:
        data, filename = file_path.read_bytes(), file_path.name
        content_type = 'image/jpeg' if is_jpeg else 'image/png'

    body = b''
    for key in ('device_id', 'mode', 'motion_score', 'timestamp'):
        value = str(metadata.get(key, ''))
        body += (
            f'--{BOUNDARY}\r\n'
            f'Content-Disposition: form-data; name="{key}"\r\n'
            f'\r\n'
            f'{value}\r\n'
        ).encode()

    body += (
        f'--{BOUNDARY}\r\n'
        f'Content-Disposition: form-data; name="image"; filename="{filename}"\r\n'
        f'Content-Type: {content_type}\r\n'
        f'\r\n'
    ).encode()
    body += data
    body += f'\r\n--{BOUNDARY}--\r\n'.encode()
    return body


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
        self._send('ATZ', wait=3)   # factory reset — modem needs time to reboot
        self._send('ATE0')          # disable echo
        if not self.unlock_sim():
            return False
        if not self.wait_registered():
            return False
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

    def get_own_number(self) -> str:
        """Query the SIM for its own phone number via AT+CNUM.
        Returns the number string, or '' if the SIM doesn't have one stored."""
        resp = self._send('AT+CNUM', wait=1)
        for line in resp.splitlines():
            if '+CNUM:' in line:
                parts = line.split(',')
                if len(parts) >= 2:
                    number = parts[1].strip().strip('"')
                    if number:
                        return number
        return ''

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

    # ── SMS ────────────────────────────────────────────────────────────────────

    def read_pending_sms(self) -> list:
        """Return list of (index, sender, text) for all stored SMS messages."""
        self._send('AT+CMGF=1')  # text mode
        resp = self._send('AT+CMGL="ALL"', wait=2)
        messages = []
        lines = [l for l in resp.splitlines() if l.strip()]
        i = 0
        while i < len(lines):
            if lines[i].startswith('+CMGL:'):
                parts = lines[i].split(',')
                try:
                    index  = int(parts[0].split(':')[1].strip())
                    sender = parts[2].strip().strip('"')
                except (IndexError, ValueError):
                    i += 1
                    continue
                text = lines[i + 1] if i + 1 < len(lines) else ''
                messages.append((index, sender, text))
                i += 2
            else:
                i += 1
        return messages

    def delete_sms(self, index: int):
        self._send(f'AT+CMGD={index}', wait=0.5)

    def send_sms(self, number: str, text: str) -> bool:
        """Send an SMS to number. Returns True on success."""
        self._send('AT+CMGF=1')
        resp = self._send(f'AT+CMGS="{number}"', wait=1)
        if '>' not in resp:
            resp += self._wait_for('>', timeout=5)
        if '>' not in resp:
            logger.error("send_sms: modem did not prompt for message text")
            return False
        self._ser.write(text.encode('ascii', errors='replace') + b'\x1a')
        self._ser.flush()
        result = self._wait_for('+CMGS:', timeout=30)
        return '+CMGS:' in result

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


class Uploader:
    def __init__(self, config):
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

        self.modem = SIM800(
            device=config.get('gsm_device', '/dev/serial0'),
            pin=config.get('gsm_pin', ''),
            apn=config.get('gsm_apn', 'web.vodafone.de'),
        )

        self._sms_handler = SMSConfigHandler(
            config.get('_config_path', '/opt/monitoring-pipeline/config/client.json')
        )

        logger.info(f"Uploader initialized. Upload URL: {self.upload_url}")
        if self.webp_compress:
            logger.info(f"WebP compression enabled (quality {self.webp_quality})")

    def _get_oldest_file(self):
        try:
            items = [
                p for p in self.outbox_dir.glob('*')
                if p.is_file()
                and not p.name.endswith('.tmp')
                and p.suffix.lower() in IMAGE_EXTS
                and p.stat().st_size > 0
            ]
            return min(items, key=lambda p: p.stat().st_mtime) if items else None
        except Exception as e:
            logger.error(f"Error reading outbox: {e}")
            return None

    def _upload(self, file_path: Path, metadata: dict, attempt=0) -> bool:
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

        body         = build_multipart(file_path, metadata, self.webp_compress, self.webp_quality)
        content_type = f'multipart/form-data; boundary={BOUNDARY}'

        status = self.modem.http_post(self.upload_url, body, content_type)
        if status == 200:
            elapsed = time.time() - t0
            logger.info(f"Upload successful: {file_path.name} ({elapsed:.1f}s, {size / elapsed / 1024:.1f} KB/s)")
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

        sidecar = oldest.with_suffix('.json')
        metadata = {}
        if sidecar.exists():
            try:
                metadata = json.loads(sidecar.read_text())
            except Exception:
                logger.warning(f"Could not read sidecar: {sidecar.name}")

        for attempt in range(self.max_retries):
            if self._upload(oldest, metadata, attempt):
                try:
                    shutil.move(str(oldest), str(self.uploaded_dir / oldest.name))
                    logger.info(f"Moved to uploaded: {oldest.name}")
                    sidecar.unlink(missing_ok=True)
                except Exception as e:
                    logger.error(f"Error moving {oldest.name}: {e}")
                return True
            if attempt < self.max_retries - 1:
                logger.info(f"Retrying in {self.retry_delay}s...")
                time.sleep(self.retry_delay)

        logger.error(f"Failed to upload {oldest.name} after {self.max_retries} attempts")
        return False

    def _save_sim_number(self):
        """Query the SIM for its own number and write it to client.json if found."""
        number = self.modem.get_own_number()
        if not number:
            logger.info("SIM own number not available (not stored on this SIM)")
            return
        try:
            cfg_path = self._sms_handler._path
            with open(cfg_path) as f:
                cfg = json.load(f)
            if cfg.get('gsm_number') == number:
                return
            cfg['gsm_number'] = number
            with open(cfg_path, 'w') as f:
                json.dump(cfg, f, indent=2)
                f.write('\n')
            logger.info(f"SIM number saved to config: {number}")
        except Exception as e:
            logger.warning(f"Could not save SIM number to config: {e}")

    def _check_sms(self):
        try:
            messages = self.modem.read_pending_sms()
            for index, sender, text in messages:
                logger.info(f"SMS from {sender}: {text!r}")
                reply = self._sms_handler.handle(text)
                logger.info(f"SMS reply to {sender}: {reply!r}")
                self.modem.send_sms(sender, reply)
                self.modem.delete_sms(index)
        except Exception as e:
            logger.error(f"SMS check error: {e}")

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
                processed = self._process_oldest()
                self._check_sms()
                time.sleep(2 if processed else self.poll_interval)

        except KeyboardInterrupt:
            logger.info("Uploader stopped by user")
        except Exception as e:
            logger.critical(f"Fatal error: {e}")
            sys.exit(1)
        finally:
            try:
                self.modem.bearer_close()
            except Exception:
                pass
            self.modem.close()


def load_config(path=None):
    if path is None:
        path = '/opt/monitoring-pipeline/config/client.json'
    try:
        with open(path) as f:
            cfg = json.load(f)
            cfg['_config_path'] = path
            return cfg
    except Exception as e:
        logger.warning(f"Failed to load config {path}: {e} — using defaults")
    return {
        'outbox_dir':    '/outbox',
        'uploaded_dir':  '/uploaded',
        'server_url':    'http://localhost:8000',
        'max_retries':   3,
        'retry_delay':   60,
        'poll_interval': 10,
        '_config_path':  path,
    }


if __name__ == '__main__':
    cfg_path = 'config/client.json'
    if len(sys.argv) > 2 and sys.argv[1] == '--config':
        cfg_path = sys.argv[2]

    Uploader(load_config(cfg_path)).run()
