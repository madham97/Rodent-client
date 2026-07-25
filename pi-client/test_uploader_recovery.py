#!/usr/bin/env python3
"""
Offline tests for the uploader's failure handling. No modem, no network, no server —
pyserial is stubbed and the modem is scripted, so this runs anywhere.

Each case pins down a failure mode that was observed in the field (see
docs/gsm-hat-setup.md). The behaviour they protect is easy to regress because all these
faults look alike in a log: a healthy modem, a delivered image and a dead serial link can
all present as "no response".

Usage:
    python3 test_uploader_recovery.py
"""

import json
import shutil
import sys
import tempfile
import types
from pathlib import Path

# Stub pyserial before importing the uploader so this runs off-device.
if 'serial' not in sys.modules:
    _fake = types.ModuleType('serial')

    class _FakeSerial:
        def __init__(self, *a, **k):
            self.is_open = True

        def close(self):
            self.is_open = False

    _fake.Serial = lambda *a, **k: _FakeSerial()
    sys.modules['serial'] = _fake

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PIL import Image as PILImage

from uploader import SIM800, Uploader, ModemLinkError

_results = []


def check(name, cond):
    _results.append((name, bool(cond)))
    print(f"{'PASS' if cond else 'FAIL'}  {name}")


class StubModem(SIM800):
    """Modem with scripted AT replies.

    `deaf=True` reproduces a stalled CP2102 read endpoint: every command returns '' until
    the port is re-opened, while writes appear to succeed."""

    def __init__(self, script=None, deaf=False):
        super().__init__(device='/dev/null')
        self.script = script or {}
        self.deaf = deaf
        self.sent = []
        self.reopens = 0

    def open(self):
        pass

    def close(self):
        pass

    def reopen(self, reason=""):
        self.reopens += 1
        self.deaf = False          # a re-open restores reads, as on real hardware

    def _send_once(self, cmd, wait):
        self.sent.append(cmd)
        if self.deaf:
            return ''
        for key, resp in self.script.items():
            if cmd.startswith(key):
                return resp
        return 'OK'

    def _wait_for(self, token, timeout=30):
        return self.script.get(f'_wait:{token}', '')


class ScriptedPort:
    """Serial port that yields one queued chunk per command written to it.

    Modelling the reply as something the modem sends *when polled* is what makes the
    progress-tracking testable: a read window with no preceding command stays quiet, exactly
    as it would on the wire."""

    def __init__(self, chunks=()):
        self.chunks = list(chunks)
        self.written = []
        self._armed = False        # nothing has been asked yet, so nothing is coming

    @property
    def in_waiting(self):
        return len(self.chunks[0]) if (self._armed and self.chunks) else 0

    def read(self, n=1):
        if not (self._armed and self.chunks):
            return b''
        self._armed = False
        return self.chunks.pop(0)

    def write(self, data):
        self.written.append(data)
        self._armed = True         # a command went out, so a reply is due

    def flush(self):
        pass


def _fake_serial_attr(modem, chunks=()):
    """Give the modem a scripted port; http_post reads the verdict directly off it.
    The poll window is shortened so the tests do not sleep through real timeouts."""
    modem._ser = ScriptedPort(chunks)
    modem.action_poll_window = 0.05
    modem.action_timeout = 5
    return modem


def _uploader(tmp, **overrides):
    cfg = {
        'outbox_dir': str(tmp / 'outbox'),
        'uploaded_dir': str(tmp / 'uploaded'),
        'failed_dir': str(tmp / 'failed'),
        'server_url': 'http://x',
        'max_retries': 3,
        'retry_delay': 0,
        'webp_compress': False,
        '_config_path': str(tmp / 'client.json'),
    }
    cfg.update(overrides)
    return Uploader(cfg)


def _queue_image(tmp):
    img = tmp / 'outbox' / 'image_20260725T120000Z.png'
    PILImage.new('RGBA', (8, 8), (10, 20, 30, 40)).save(img, format='PNG')
    img.with_suffix('.json').write_text(json.dumps({'device_id': 'd'}))
    return img


def link_fault(*a, **k):
    raise ModemLinkError("no +HTTPACTION result after 60s (+HTTPSTATUS: POST,0,0,0)")


# ── Serial link recovery ──────────────────────────────────────────────────────

m = StubModem({'AT': 'OK'}, deaf=True)
check('deaf port: _send re-opens and retries', m._send('AT') == 'OK' and m.reopens == 1)

m = StubModem({'AT': 'OK'}, deaf=True)
check('heal=False never re-sends (no duplicate POST/SMS)',
      m._send('AT+HTTPACTION=1', heal=False) == '' and m.reopens == 0)

# ── Bearer state must be verified, not assumed ────────────────────────────────

m = StubModem()
m._send_once = lambda cmd, wait: ''          # permanently silent
check('bearer_open is False on silence', m.bearer_open() is False)

m = StubModem({'AT+SAPBR=2,1': '+SAPBR: 1,1,"100.64.1.2"\nOK'})
check('bearer_open is True only when connected', m.bearer_open() is True)

m = StubModem({'AT+SAPBR=2,1': '+SAPBR: 1,3,"0.0.0.0"\nOK'})
check('bearer_open is False when the bearer reports disconnected',
      m.bearer_open() is False)

# ── http_post: verdict vs no verdict ──────────────────────────────────────────

m = _fake_serial_attr(StubModem({
    'AT+HTTPINIT': 'OK', 'AT+HTTPPARA': 'OK', 'AT+HTTPDATA': 'DOWNLOAD',
    'AT+HTTPSTATUS?': '+HTTPSTATUS: POST,0,0,0\nOK',
}), chunks=[b'\r\n+HTTPSTATUS: POST,0,0,0\r\nOK\r\n'] * 6)
try:
    m.http_post('http://x/upload', b'x' * 20, 'multipart/form-data')
    check('missing +HTTPACTION raises ModemLinkError', False)
except ModemLinkError as e:
    check('missing +HTTPACTION raises ModemLinkError, carrying HTTPSTATUS',
          'POST,0,0,0' in str(e))

m = _fake_serial_attr(StubModem({
    'AT+HTTPINIT': 'OK', 'AT+HTTPPARA': 'OK', 'AT+HTTPDATA': 'DOWNLOAD',
}), chunks=[b'\r\n+HTTPACTION: 1,200,15\r\n'])
check('a real HTTP status is still returned', m.http_post('http://x/upload', b'x' * 20, 'ct') == 200)

# ── Waiting for the verdict: progress must extend the wait, stalls must not ───

def _await_with(chunks, timeout=30, idle_polls=3):
    m = _fake_serial_attr(StubModem(), chunks)
    m.action_timeout = timeout
    m.action_idle_polls = idle_polls
    return m, m._await_action_result()


# A slow but healthy upload: remain keeps falling, then the verdict lands. Must not give up
# even though it takes many polls — this is the 290 KB-on-2G case.
progressing = [b'\r\n+HTTPSTATUS: POST,2,1000,%d\r\n\r\nOK\r\n' % r
               for r in (200000, 150000, 100000, 50000, 10000)]
progressing.append(b'\r\n+HTTPACTION: 1,200,15\r\n')
m, out = _await_with(progressing)
check('a progressing transfer is waited out to its verdict', '+HTTPACTION: 1,200,15' in out)

# A stalled transfer: remain never moves. Must bail out early rather than burn the ceiling.
m, out = _await_with([b'\r\n+HTTPSTATUS: POST,2,1000,50000\r\n\r\nOK\r\n'] * 12)
check('a transfer stuck at the same byte count gives up early',
      '+HTTPACTION:' not in out and len(m._ser.chunks) > 6)

# A dead engine (the POST,0,0,0 case) must also bail out early.
m, out = _await_with([b'\r\n+HTTPSTATUS: POST,0,0,0\r\n\r\nOK\r\n'] * 12)
check('an idle engine gives up early', '+HTTPACTION:' not in out and len(m._ser.chunks) > 6)

# The verdict must be recognised even when it shares a read with a status reply, since
# polling must never purge the port.
m, out = _await_with([b'\r\n+HTTPSTATUS: POST,2,1,9\r\n\r\nOK\r\n\r\n+HTTPACTION: 1,200,15\r\n'])
check('a verdict arriving alongside a status poll is not lost',
      '+HTTPACTION: 1,200,15' in out)

check('_parse_httpstatus reads the latest sample',
      SIM800._parse_httpstatus('+HTTPSTATUS: POST,2,10,900\nx\n+HTTPSTATUS: POST,2,20,800\n')
      == (2, 800))
check('_parse_httpstatus ignores junk', SIM800._parse_httpstatus('no status here') is None)


# ── Queue behaviour ───────────────────────────────────────────────────────────

tmp = Path(tempfile.mkdtemp())
for d in ('outbox', 'uploaded', 'failed'):
    (tmp / d).mkdir()
img = _queue_image(tmp)

# A link fault must not park the image: the transport is down, not the image.
up = _uploader(tmp)
up.modem = StubModem()
up.modem.http_post = link_fault
up.modem.http_get = link_fault               # confirmation also unreachable
up._process_oldest()
check('link fault keeps the image in the outbox', img.exists())
check('link fault does not park the image in failed/',
      not (tmp / 'failed' / img.name).exists())
check('link fault is counted for link recovery', up._link_failures == 1)
check('link fault records the image as unconfirmed', img.name in up._unconfirmed)

# Next cycle: ask the server BEFORE spending another upload on it.
order = []
up.modem.http_post = lambda *a, **k: order.append('post') or link_fault()
up.modem.http_get = lambda url: order.append('get') or 200
up._process_oldest()
check('pre-flight confirmation runs before any re-POST', order == ['get'])
check('confirmed image is moved to uploaded/', (tmp / 'uploaded' / img.name).exists())
check('resolved image leaves the unconfirmed set', img.name not in up._unconfirmed)

# The confirmation must ask for the name the SERVER files it under: it re-encodes an
# uploaded WebP to <stem>.jpg, so asking for the local .png name would always 404.
shutil.move(str(tmp / 'uploaded' / img.name), str(img))
img.with_suffix('.json').write_text(json.dumps({'device_id': 'd'}))
asked = {}
up_w = _uploader(tmp, webp_compress=True)
up_w.modem = StubModem()
up_w.modem.http_post = link_fault
up_w.modem.http_get = lambda url: asked.setdefault('url', url) and 200 or 200
up_w._process_oldest()                        # cycle 1: fault, nothing asked yet
check('no server query on the link that just stalled', 'url' not in asked)
up_w._process_oldest()                        # cycle 2: pre-flight settles it
check('confirmation uses the .jpg name the server stores',
      asked.get('url') == 'http://x/annotate/specific/image_20260725T120000Z.jpg')
check('confirmed delivery resets the link-fault counter', up_w._link_failures == 0)

# Server says it is genuinely absent -> stays queued, still not parked.
shutil.move(str(tmp / 'uploaded' / img.name), str(img))
img.with_suffix('.json').write_text(json.dumps({'device_id': 'd'}))
up_n = _uploader(tmp, webp_compress=True)
up_n.modem = StubModem()
up_n.modem.http_post = link_fault
up_n.modem.http_get = lambda url: 404
up_n._process_oldest()
up_n._process_oldest()
check('image the server does not have stays queued', img.exists())
check('image the server does not have is not parked',
      not (tmp / 'failed' / img.name).exists())

# A genuine server rejection still parks after the retries are used up.
up_e = _uploader(tmp, max_retries=2)
up_e.modem = StubModem()
up_e.modem.http_post = lambda *a, **k: 500
up_e._process_oldest()
check('server error still parks the image in failed/',
      (tmp / 'failed' / img.name).exists())
check('server error clears the link-fault counter', up_e._link_failures == 0)

shutil.rmtree(tmp)

print()
failed = [n for n, ok in _results if not ok]
print(f"{len(_results) - len(failed)}/{len(_results)} passed"
      + (f"   FAILED: {failed}" if failed else ""))
sys.exit(1 if failed else 0)
