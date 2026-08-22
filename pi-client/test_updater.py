#!/usr/bin/env python3
"""
Offline tests for self-update. No modem, no network, no server.

These cover the paths that matter when nobody can reach the device: a corrupted download, a
bundle that doesn't compile, an update that installs but never manages an upload, and an archive
trying to write outside the install directory. The happy path is the least interesting case here
— it's the failures that decide whether someone has to drive to the site.

Usage:
    python3 test_updater.py
"""

import base64
import hashlib
import io
import json
import shutil
import sys
import tarfile
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from updater import Updater, STATE_PROBATION, STATE_CONFIRMED, STATE_ROLLED_BACK

_results = []


def check(name, cond):
    _results.append((name, bool(cond)))
    print(f"{'PASS' if cond else 'FAIL'}  {name}")


def make_bundle(files: dict) -> bytes:
    """A .tgz of {relative path: text}."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w:gz') as tar:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class StubModem:
    """Serves a manifest and a bundle the way the real modem would: base64 text, status code."""

    def __init__(self, bundle: bytes | None, version='v2', corrupt=False, status=200):
        self.bundle = bundle
        self.version = version
        self.corrupt = corrupt
        self.status = status
        self.requests = []

    def http_get_body(self, url, max_bytes=512_000, chunk=1024):
        self.requests.append(url)
        if '/manifest' in url:
            if self.bundle is None:
                return 200, json.dumps({'version': None})
            return 200, json.dumps({
                'version': self.version,
                'sha256': hashlib.sha256(self.bundle).hexdigest(),
                'size': len(self.bundle),
                'encoded_size': len(base64.b64encode(self.bundle)),
            })
        if self.status != 200:
            return self.status, ''
        payload = self.bundle
        if self.corrupt:
            payload = payload[:-5] + b'xxxxx'   # digest no longer matches
        return 200, base64.b64encode(payload).decode()


def new_updater(tmp: Path, **kw) -> Updater:
    install = tmp / 'install'
    (install / 'pi-client').mkdir(parents=True, exist_ok=True)
    (install / 'pi-client' / 'recorder.py').write_text('VERSION = "v1"\n')
    up = Updater(install, server_url='http://server', device_id='test', **kw)
    up.save_state(state='idle', version='v1')
    return up


def run():
    tmp = Path(tempfile.mkdtemp())

    # ── a good update installs, enters probation, and is confirmed by an upload ──
    up = new_updater(tmp / 'a')
    good = make_bundle({'pi-client/recorder.py': 'VERSION = "v2"\n'})
    installed = up.poll(StubModem(good))
    check('good bundle installs', installed)
    check('installed file is the new one',
          (up.install_dir / 'pi-client/recorder.py').read_text().strip() == 'VERSION = "v2"')
    check('enters probation', up.load_state()['state'] == STATE_PROBATION)
    check('snapshot kept while on probation', up.snapshot_dir.exists())

    up.note_successful_upload()
    check('successful upload confirms the update', up.load_state()['state'] == STATE_CONFIRMED)
    check('snapshot discarded once confirmed', not up.snapshot_dir.exists())

    # ── an update nobody could confirm rolls back to the previous version ──
    up = new_updater(tmp / 'b', probation_s=1)
    up.poll(StubModem(make_bundle({'pi-client/recorder.py': 'VERSION = "broken"\n'})))
    check('probation expiry is not immediate', not up.probation_expired())
    time.sleep(1.1)
    check('probation expires with no upload', up.probation_expired())
    up.rollback('no successful upload within the probation window', restart=False)
    check('rollback restores the previous file',
          (up.install_dir / 'pi-client/recorder.py').read_text().strip() == 'VERSION = "v1"')
    check('rollback is recorded', up.load_state()['state'] == STATE_ROLLED_BACK)
    check('rollback restores the previous version label', up.load_state()['version'] == 'v1')

    # ── a corrupted download never reaches the disk ──
    up = new_updater(tmp / 'c')
    installed = up.poll(StubModem(make_bundle({'pi-client/recorder.py': 'VERSION = "v2"\n'}), corrupt=True))
    check('digest mismatch is not installed', not installed)
    check('corrupt download leaves the live file alone',
          (up.install_dir / 'pi-client/recorder.py').read_text().strip() == 'VERSION = "v1"')
    check('corrupt download leaves state idle', up.load_state()['state'] == 'idle')

    # ── a bundle that does not compile is rejected before it is installed ──
    up = new_updater(tmp / 'd')
    installed = up.poll(StubModem(make_bundle({'pi-client/recorder.py': 'def broken(:\n'})))
    check('syntax error is not installed', not installed)
    check('syntax error leaves the live file alone',
          (up.install_dir / 'pi-client/recorder.py').read_text().strip() == 'VERSION = "v1"')

    # ── an archive cannot write outside the install directory ──
    up = new_updater(tmp / 'e')
    evil = io.BytesIO()
    with tarfile.open(fileobj=evil, mode='w:gz') as tar:
        data = b'pwned\n'
        info = tarfile.TarInfo('../../../../tmp/escaped.py')
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    installed = up.poll(StubModem(evil.getvalue()))
    check('path traversal is refused', not installed)
    check('traversal target was never written', not Path('/tmp/escaped.py').exists())

    # ── nothing published, and an already-current version, are both no-ops ──
    up = new_updater(tmp / 'f')
    check('no published bundle is a no-op', not up.poll(StubModem(None)))
    up2 = new_updater(tmp / 'g')
    up2.save_state(state='confirmed', version='v2')
    check('already on the published version is a no-op',
          not up2.poll(StubModem(make_bundle({'pi-client/recorder.py': 'x = 1\n'}), version='v2')))

    # ── a device already on probation does not stack a second update ──
    up = new_updater(tmp / 'h')
    up.poll(StubModem(make_bundle({'pi-client/recorder.py': 'VERSION = "v2"\n'})))
    check('no second update while on probation',
          not up.poll(StubModem(make_bundle({'pi-client/recorder.py': 'VERSION = "v3"\n'}), version='v3')))

    # ── a server error during download changes nothing ──
    up = new_updater(tmp / 'i')
    check('HTTP error during fetch is a no-op',
          not up.poll(StubModem(make_bundle({'pi-client/recorder.py': 'x = 1\n'}), status=500)))

    # ── a rolled-back version is not immediately re-fetched ──
    # Without this the device reinstalls exactly what it just rejected on the next check and
    # loops, spending a bundle's worth of a metered 2G link each time.
    up = new_updater(tmp / 'j', probation_s=0, retry_after_rollback_s=3600)
    bad = make_bundle({'pi-client/recorder.py': 'VERSION = "bad"\n'})
    up.poll(StubModem(bad, version='bad'))
    up.rollback('probation expired', restart=False)
    check('rolled-back version is skipped on the next check',
          not up.poll(StubModem(bad, version='bad')))
    check('the skip is time-limited, not permanent',
          not up.recently_rolled_back('bad', now=time.time() + 3601))
    # A fix must never be held up by the failure that preceded it.
    check('a different version is still accepted after a rollback',
          up.poll(StubModem(make_bundle({'pi-client/recorder.py': 'VERSION = "fixed"\n'}), version='fixed')))

    # ── probation carries the deferral cap the watchdog reads ──
    up = new_updater(tmp / 'k', max_probation_s=12345)
    up.poll(StubModem(make_bundle({'pi-client/recorder.py': 'x = 1\n'})))
    check('probation state records the deferral cap',
          up.load_state().get('max_probation_s') == 12345)

    shutil.rmtree(tmp, ignore_errors=True)

    failed = [n for n, ok in _results if not ok]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} passed")
    if failed:
        print("FAILED: " + ", ".join(failed))
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(run())
