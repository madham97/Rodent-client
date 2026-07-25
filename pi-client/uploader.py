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


class ModemLinkError(Exception):
    """The modem or the serial link to it failed, so we never learned what the server
    thought of the image. Distinct from an HTTP error: the image itself is fine and must
    stay queued, not be parked in failed/ as though the server had rejected it."""

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


def _to_webp(file_path: Path, quality: int, max_bytes: int = None) -> tuple[bytes, str]:
    """Re-encode a JPEG or PNG as WebP in memory. Alpha channels (e.g. thermal-fused RGBA
    PNGs) survive the conversion since WebP supports alpha. Returns (webp_bytes, webp_filename).

    If max_bytes is given, keep the result within it: encode at the requested quality, then, if
    that overshoots, progressively lower quality and finally downscale until it fits. Fused
    thermal frames vary in size with scene detail, so a fixed quality can't guarantee they clear
    the modem's HTTP buffer — this adapts per image. Best-effort: if even the smallest attempt is
    over, the smallest is returned and the caller enforces the hard cap."""
    from PIL import Image
    import io

    def encode(im, q):
        buf = io.BytesIO()
        im.save(buf, format='WEBP', quality=q)
        return buf.getvalue()

    with Image.open(file_path) as img:
        img.load()
        name = file_path.stem + '.webp'
        data = encode(img, quality)
        if max_bytes is None or len(data) <= max_bytes:
            return data, name

        # 1) Lower quality at full resolution.
        for q in (60, 45, 30):
            if q < quality:
                data = encode(img, q)
                if len(data) <= max_bytes:
                    logger.info(f"{file_path.name}: compressed to fit at webp q{q} ({len(data)} bytes)")
                    return data, name

        # 2) Still over — downscale (from the original each step) at the quality floor.
        for scale in (0.75, 0.6, 0.5, 0.4):
            w, h = max(1, int(img.width * scale)), max(1, int(img.height * scale))
            data = encode(img.resize((w, h)), 40)
            if len(data) <= max_bytes:
                logger.info(f"{file_path.name}: compressed to fit at {int(scale*100)}% scale ({len(data)} bytes)")
                return data, name

        logger.warning(f"{file_path.name}: could not compress under {max_bytes} bytes (smallest {len(data)})")
        return data, name


def build_multipart(file_path: Path, metadata: dict = None, webp_compress: bool = False,
                    webp_quality: int = 80, max_image_bytes: int = None) -> bytes:
    """Wrap image file in a multipart/form-data body with metadata text fields."""
    if metadata is None:
        metadata = {}

    is_jpeg = file_path.suffix.lower() in ('.jpg', '.jpeg')
    is_png  = file_path.suffix.lower() == '.png'
    if (is_jpeg or is_png) and webp_compress:
        data, filename = _to_webp(file_path, webp_quality, max_bytes=max_image_bytes)
        content_type = 'image/webp'
    else:
        data, filename = file_path.read_bytes(), file_path.name
        content_type = 'image/jpeg' if is_jpeg else 'image/png'

    body = b''
    always_keys    = ('device_id', 'mode', 'motion_score', 'timestamp')
    optional_keys  = ('format', 'thermal_min_c', 'thermal_max_c', 'thermal_avg_c')
    for key in always_keys + tuple(k for k in optional_keys if k in metadata):
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

    def __init__(self, device='/dev/serial0', baud=115200, pin='', apn='web.vodafone.de',
                 action_timeout=60):
        self.device = device
        self.baud   = baud
        self.pin    = pin
        self.apn    = apn
        self._ser   = None
        # How long to wait for +HTTPACTION. Measured round-trips on this link are 26–37 s,
        # so the old 180 s only ever added dead time: when the USB endpoint stalls the reply
        # is already lost and no amount of waiting recovers it. A shorter wait means a
        # stalled cycle costs ~1 minute instead of ~3.5, and the delivery check settles
        # whether the POST landed.
        self.action_timeout = action_timeout

    # ── Serial helpers ─────────────────────────────────────────────────────────

    def open(self):
        self._ser = serial.Serial(self.device, self.baud, timeout=2, rtscts=False)
        logger.info(f"Serial port {self.device} opened")

    def close(self):
        if self._ser and self._ser.is_open:
            self._ser.close()

    def reopen(self, reason="serial read path is dead (USB endpoint stalled)"):
        """Close and re-open the serial port to recover a one-way link.

        A GPRS data attempt can halt the CP2102's bulk-IN endpoint. The kernel logs
        `cp210x ttyUSB0: usb_serial_generic_read_bulk_callback - urb stopped: -32`
        (-EPIPE) and the usb-serial driver then stops resubmitting the read URB, so the
        file descriptor never delivers another byte — while writes keep succeeding. Every
        AT command from then on looks like it returned an empty response, forever, and the
        modem appears dead when it is actually healthy. Re-opening the port gets a fresh
        URB and restores reads immediately; nothing else does."""
        logger.warning(f"Re-opening serial port: {reason}")
        try:
            self.close()
        except Exception as e:
            logger.warning(f"Error closing port during recovery: {e}")
        time.sleep(0.5)
        self.open()

    def _send(self, cmd, wait=0.4, heal=True):
        """Send one AT command, return the raw text response.

        An empty response means the read path died rather than that the modem said
        nothing, so by default we re-open the port and ask once more. `heal=False` is for
        commands that must never be sent twice (firing an HTTP POST, sending an SMS) —
        a silent duplicate is worse there than a lost reply."""
        resp = self._send_once(cmd, wait)
        if resp or not heal:
            return resp
        self.reopen()
        return self._send_once(cmd, wait)

    def _send_once(self, cmd, wait):
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
        """Open the GPRS bearer used by the HTTP stack.

        Confirms the outcome by querying the bearer rather than trusting the reply to
        SAPBR=1,1: an already-open bearer answers ERROR, and a dead read path answers
        nothing at all. Treating either as success made the log claim the bearer was up
        every 12 seconds while nothing worked."""
        self._send(f'AT+SAPBR=3,1,"Contype","GPRS"')
        self._send(f'AT+SAPBR=3,1,"APN","{self.apn}"')
        self._send('AT+SAPBR=1,1', wait=5)

        status = self._send('AT+SAPBR=2,1', wait=1)
        if '+SAPBR: 1,1' in status:   # <cid>,<status=1 connected>,<ip>
            ip = status.split('"')[1] if '"' in status else '?'
            logger.info(f"GPRS bearer open (IP {ip})")
            return True
        if not status:
            logger.error("Bearer status unreadable — modem is not answering")
            return False
        logger.error(f"Bearer is not connected: {status.splitlines()[0] if status else status!r}")
        return False

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

    def http_get(self, url: str) -> int:
        """GET url, returning the HTTP status code. Raises ModemLinkError if the modem never
        reports a result. Cheap enough (no body) to use as a delivery check."""
        self._send('AT+HTTPTERM', wait=1)
        if 'OK' not in self._send('AT+HTTPINIT', wait=1):
            raise ModemLinkError("HTTPINIT failed for GET")
        for para in ('AT+HTTPPARA="CID",1', f'AT+HTTPPARA="URL","{url}"'):
            if 'OK' not in self._send(para):
                raise ModemLinkError(f"{para.split('=')[0]} rejected for GET")

        self._send('AT+HTTPACTION=0', wait=0.5, heal=False)
        # A bodyless GET returns far quicker than an upload, so it gets a tighter budget.
        result = self._wait_for('+HTTPACTION:', timeout=max(30, self.action_timeout // 2))
        time.sleep(0.5)
        result += self._ser.read(self._ser.in_waiting or 0).decode('ascii', errors='ignore')
        self._send('AT+HTTPTERM', wait=0.5)

        if '+HTTPACTION:' in result:
            try:
                return int(result.split('+HTTPACTION:')[1].strip().split(',')[1])
            except Exception:
                pass
        raise ModemLinkError("no +HTTPACTION result for confirmation GET")

    def http_post(self, url: str, body: bytes, content_type: str) -> int:
        """
        POST body to url using the modem's internal HTTP stack.
        Returns the HTTP status code. Raises ModemLinkError if the modem or the link to it
        failed, so the caller can tell "the server rejected this image" (park it) apart
        from "we never got to ask" (keep it queued).
        """
        self._send('AT+HTTPTERM', wait=1)   # clean up any previous session

        resp = self._send('AT+HTTPINIT', wait=1)
        if 'OK' not in resp:
            # HTTP stack stuck (or already initialised) — full bearer cycle to reset it
            logger.info(f"HTTPINIT did not confirm ({resp!r}), cycling bearer...")
            self.bearer_close()
            time.sleep(3)
            if not self.bearer_open():
                raise ModemLinkError("bearer would not open")
            self._send('AT+HTTPTERM', wait=1)
            resp = self._send('AT+HTTPINIT', wait=2)
        if 'OK' not in resp:
            raise ModemLinkError(f"HTTPINIT failed after bearer reset: {resp!r}")

        for para in (f'AT+HTTPPARA="CID",1',
                     f'AT+HTTPPARA="URL","{url}"',
                     f'AT+HTTPPARA="CONTENT","{content_type}"'):
            resp = self._send(para)
            if 'OK' not in resp:
                raise ModemLinkError(f"{para.split('=')[0]} rejected: {resp!r}")

        # Tell the modem how many bytes we'll send (give 60 s to receive them)
        resp = self._send(f'AT+HTTPDATA={len(body)},60000', wait=1)
        if 'DOWNLOAD' not in resp:
            resp += self._wait_for('DOWNLOAD', timeout=5)
        if 'DOWNLOAD' not in resp:
            self._send('AT+HTTPTERM')
            raise ModemLinkError(f"modem did not enter DOWNLOAD state (got {resp!r})")

        # Write body in small chunks to avoid overwhelming the SIM800 receive buffer
        chunk_size = 512
        for i in range(0, len(body), chunk_size):
            self._ser.write(body[i:i + chunk_size])
            self._ser.flush()
            time.sleep(0.02)
        self._wait_for('OK', timeout=10)

        # Fire the POST — GPRS round-trips can take up to 2 minutes.
        # heal=False: a re-sent HTTPACTION would POST the image a second time.
        self._send('AT+HTTPACTION=1', wait=0.5, heal=False)
        result = self._wait_for('+HTTPACTION:', timeout=self.action_timeout)
        # Read a bit more to capture the full line after the token arrives
        time.sleep(0.5)
        tail = self._ser.read(self._ser.in_waiting or 0)
        result += tail.decode('ascii', errors='ignore')

        # Parse: +HTTPACTION: 1,<status_code>,<response_bytes>
        if '+HTTPACTION:' in result:
            try:
                parts = result.split('+HTTPACTION:')[1].strip().split(',')
                status = int(parts[1])
                self._send('AT+HTTPTERM', wait=0.5)
                return status
            except Exception:
                pass

        # No result line. Record what the HTTP engine reports, for the log.
        #
        # +HTTPSTATUS: <mode>,<status>,<finish>,<remain>. Read it carefully: status 0 means
        # *idle*, which an engine that already finished reports identically to one that never
        # started. "POST,0,0,0" is therefore NOT evidence that the upload failed — during the
        # original investigation it was misread that way, while the POSTs were in fact landing
        # and being answered 200. Only the server can settle it, which is what
        # Uploader._confirm_delivered() is for.
        diag = self._send('AT+HTTPSTATUS?', wait=1)
        self._send('AT+HTTPTERM', wait=0.5)
        state = diag.splitlines()[0].strip() if diag else '+HTTPSTATUS unreadable'
        raise ModemLinkError(f"no +HTTPACTION result after {self.action_timeout}s ({state})")


class Uploader:
    def __init__(self, config):
        self.outbox_dir    = Path(config.get('outbox_dir', '/outbox'))
        self.uploaded_dir  = Path(config.get('uploaded_dir', '/uploaded'))
        # Images that exhaust their retries are parked here so a single un-acceptable image (e.g.
        # one the server 500s on) never blocks the oldest-first queue behind it. Kept, not
        # deleted, so they can be re-sent once the server accepts them.
        self.failed_dir    = Path(config.get('failed_dir', '/failed'))
        self.server_url    = config.get('server_url', 'http://localhost:8000').rstrip('/')
        self.upload_url    = self.server_url + '/upload'
        # Path used to ask the server whether an image already arrived, after a link fault
        # swallowed the modem's reply. '{name}' is the name the server files it under.
        # Set to "" in client.json to disable the check.
        self.confirm_path  = config.get('confirm_path', '/annotate/specific/{name}')
        self.max_retries   = int(config.get('max_retries', 3))
        self.retry_delay   = int(config.get('retry_delay', 60))
        self.poll_interval = int(config.get('poll_interval', 10))

        self.outbox_dir.mkdir(parents=True, exist_ok=True)
        self.uploaded_dir.mkdir(parents=True, exist_ok=True)
        self.failed_dir.mkdir(parents=True, exist_ok=True)

        self.webp_compress = bool(config.get('webp_compress', False))
        self.webp_quality  = int(config.get('webp_quality', 80))
        self._link_failures = 0   # consecutive cycles lost to a modem/link fault
        # Images whose POST ended without a verdict. Checked against the server on the next
        # cycle before being re-sent, so a delivered image is not uploaded twice.
        self._unconfirmed = set()

        self.modem = SIM800(
            device=config.get('gsm_device', '/dev/serial0'),
            pin=config.get('gsm_pin', ''),
            apn=config.get('gsm_apn', 'web.vodafone.de'),
            action_timeout=int(config.get('http_action_timeout', 60)),
            baud=int(config.get('gsm_baud', 115200)),
        )

        self._sms_handler = SMSConfigHandler(
            config.get('_config_path', '/opt/Rodent-client/config/client.json')
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

    def _upload(self, file_path: Path, metadata: dict, attempt=0) -> str:
        """Attempt one upload. Returns 'ok' (sent), 'toobig' (won't fit even after adaptive
        compression — don't retry), 'link' (modem/link fault: we never learned the server's
        verdict, so keep the image queued), or 'retry' (server failure, retry may help)."""
        # Build the body first so the size check sees the actual POST payload the modem must
        # buffer — i.e. the WebP-compressed image, not the raw file. Give the image a byte budget
        # under the HTTP buffer (leaving margin for the multipart boundaries and metadata fields)
        # so build_multipart can adaptively compress a bulky thermal frame to fit.
        budget       = MAX_FILE_BYTES - 8192
        body         = build_multipart(file_path, metadata, self.webp_compress,
                                       self.webp_quality, max_image_bytes=budget)
        content_type = f'multipart/form-data; boundary={BOUNDARY}'
        size = len(body)
        logger.info(f"Uploading {file_path.name} ({size} bytes payload, attempt {attempt + 1})")
        t0 = time.time()

        if size > MAX_FILE_BYTES:
            logger.error(
                f"{file_path.name} payload is {size} bytes — exceeds SIM800 HTTP buffer "
                f"({MAX_FILE_BYTES} bytes) even after adaptive compression."
            )
            return 'toobig'

        try:
            status = self.modem.http_post(self.upload_url, body, content_type)
        except ModemLinkError as e:
            logger.error(f"Modem link fault, {file_path.name} stays queued: {e}")
            return 'link'

        if status == 200:
            elapsed = time.time() - t0
            logger.info(f"Upload successful: {file_path.name} ({elapsed:.1f}s, {size / elapsed / 1024:.1f} KB/s)")
            return 'ok'

        logger.warning(f"Upload failed (HTTP {status}): {file_path.name}")

        # 601/603 = SIM800 network error — re-open bearer before the next attempt
        if status in (601, 603):
            logger.info("Attempting to re-open GPRS bearer...")
            self.modem.bearer_open()

        return 'retry'

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

        # Pre-flight: if a previous attempt on this image ended without a verdict, settle it
        # with a small GET before spending another ~120 KB of 2G on a POST the server may
        # already have. Asking here rather than straight after the fault matters — by now
        # _recover_link() has re-opened the port and rebuilt the bearer, so the check runs on
        # a working link instead of the one that just stalled.
        if oldest.name in self._unconfirmed:
            if self._confirm_delivered(oldest):
                self._unconfirmed.discard(oldest.name)
                self._link_failures = 0
                self._move_to_uploaded(oldest, sidecar)
                return True

        for attempt in range(self.max_retries):
            result = self._upload(oldest, metadata, attempt)
            if result != 'link':
                # Anything other than a link fault means the modem carried the request and
                # the server answered, so the transport is working again.
                self._link_failures = 0
            if result == 'ok':
                self._move_to_uploaded(oldest, sidecar)
                return True
            if result == 'toobig':
                # Never fits — park it (kept for re-send) instead of wasting retries or, worse,
                # dropping it into uploaded/ where it would look delivered.
                self._park_failed(oldest, sidecar, "too large to send even after compression")
                return False
            if result == 'link':
                # No verdict. Confirming right now would query the same link that just
                # stalled, so record the doubt and let the next cycle's pre-flight check ask
                # once the modem has been re-established.
                #
                # The image stays put: the transport is down, not this image. Parking it
                # would empty the outbox into failed/ one image per cycle during any outage
                # and make a network problem look like a pile of bad captures.
                self._unconfirmed.add(oldest.name)
                self._link_failures += 1
                return False
            if attempt < self.max_retries - 1:
                logger.info(f"Retrying in {self.retry_delay}s...")
                time.sleep(self.retry_delay)

        # Out of retries — park in failed/ so the oldest-first queue keeps flowing instead of
        # retrying this same file forever and starving everything behind it.
        self._park_failed(oldest, sidecar, f"failed after {self.max_retries} attempts")
        return False

    def _move_to_uploaded(self, image: Path, sidecar: Path):
        self._unconfirmed.discard(image.name)
        try:
            shutil.move(str(image), str(self.uploaded_dir / image.name))
            logger.info(f"Moved to uploaded: {image.name}")
            sidecar.unlink(missing_ok=True)
        except Exception as e:
            logger.error(f"Error moving {image.name}: {e}")

    def _park_failed(self, image: Path, sidecar: Path, reason: str):
        """Move an un-sendable image (and its sidecar) into failed/ — kept for later re-send,
        and out of the active queue so it never blocks the images behind it."""
        self._unconfirmed.discard(image.name)
        logger.error(f"Parking {image.name} in {self.failed_dir.name}/ ({reason})")
        try:
            shutil.move(str(image), str(self.failed_dir / image.name))
            if sidecar.exists():
                shutil.move(str(sidecar), str(self.failed_dir / sidecar.name))
        except FileNotFoundError:
            pass  # already moved externally — nothing to do
        except Exception as e:
            logger.error(f"Error parking {image.name} in {self.failed_dir.name}/: {e}")

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

    def _server_name(self, image: Path) -> str:
        """The name the server files this image under. It re-encodes an uploaded WebP to
        `<stem>.jpg` (splitting any thermal alpha into its own directory) and otherwise keeps
        the name the file arrived with."""
        return image.stem + '.jpg' if self.webp_compress else image.name

    def _confirm_delivered(self, image: Path) -> bool:
        """Ask the server whether the image already arrived.

        A stalled USB read endpoint can swallow the modem's `+HTTPACTION:` line *after* the
        POST has already been delivered and answered 200 — verified against the server's
        request log. Without this check the uploader treats a delivered image as failed,
        re-sends it every cycle (burning 2G data on duplicates the server just overwrites),
        and never advances past it, so the whole queue stalls behind one image."""
        if not self.confirm_path:
            return False
        url = self.server_url + self.confirm_path.format(name=self._server_name(image))
        try:
            status = self.modem.http_get(url)
        except ModemLinkError as e:
            logger.info(f"Delivery of {image.name} still unconfirmed ({e}) — staying queued")
            return False
        if status == 200:
            logger.info(f"{image.name} is already on the server — the POST succeeded and "
                        f"only the modem's reply was lost")
            return True
        logger.info(f"{image.name} not on the server yet (HTTP {status}) — will retry")
        return False

    def _recover_link(self):
        """Escalating recovery after a modem/link fault, cheapest step first.

        The image stays in the outbox throughout — this only rebuilds the transport. Steps
        escalate because the cheap fixes cover the common faults: a stalled USB endpoint
        needs only a port re-open, a stale PDP context needs a bearer rebuild, and a wedged
        module needs a reset. If none of it helps, the fault is upstream of this Pi (no GPRS
        data service) and the log should say so rather than churn silently."""
        n = self._link_failures
        logger.warning(f"Recovering modem link (consecutive link faults: {n})")

        # Step 1, every time: clears a halted CP2102 read endpoint.
        self.modem.reopen("recovering after a link fault")
        if 'OK' not in self.modem._send('AT', wait=0.5):
            logger.error("Modem not responding to AT after port re-open")

        if n >= 2:
            logger.info("Rebuilding the GPRS bearer from scratch...")
            self.modem.bearer_close()
            time.sleep(3)
            # Bearer teardown can stall the endpoint too, so clear it before reopening.
            self.modem.reopen("bearer teardown can stall the endpoint")
            self.modem.bearer_open()

        # Step 3, but only every 4th fault: a module reset costs ~45s of downtime, so it is
        # worth retrying periodically without doing it on every single cycle.
        if n >= 4 and n % 4 == 0:
            logger.warning("Resetting the modem module (AT+CFUN=1,1)...")
            self.modem._send('AT+CFUN=1,1', wait=2, heal=False)
            time.sleep(30)
            self.modem.reopen()
            if self.modem.initialize():
                self.modem.bearer_open()

        if n >= 6 and n % 6 == 0:
            logger.error(
                "Modem still not reporting upload verdicts after repeated recovery. Note the "
                "uploads may well be arriving — check the server before assuming otherwise. "
                "The usual cause is the USB read endpoint halting: check "
                "`dmesg | grep -c 'urb stopped'` and try a different USB port or cable "
                "(see docs/gsm-hat-setup.md). Images remain queued."
            )

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
                if self._link_failures:
                    self._recover_link()
                else:
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
        path = '/opt/Rodent-client/config/client.json'
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
        'failed_dir':    '/failed',
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
