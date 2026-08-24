"""
Self-update for a device that cannot be reached inbound.

The uplink is a 2G modem driven by AT commands, so there is no IP stack, no SSH and no VPN —
the device can only ever *pull*. Everything here is built around one consequence of that: if an
update breaks the uploader, nothing can tell us and nothing can fix it. A person has to drive to
the site.

So the update is staged, not applied in place, and it is on probation until the device proves it
still works. "Works" is defined as *a capture was successfully POSTed to the server* — not that a
process started, not that a file parsed. The uplink is the only thing whose loss is unrecoverable,
so the uplink is what has to be demonstrated.

    fetch ─→ verify digest ─→ compile-check ─→ snapshot current ─→ swap in ─→ PROBATION
                                                                                 │
                                    upload succeeds ──→ CONFIRMED ───────────────┤
                                    window expires  ──→ ROLL BACK to snapshot ───┘

The rollback path deliberately depends on nothing from the new bundle: the watchdog reads this
state file and restores a directory. See scripts/update-watchdog.py, which runs from a systemd
timer so it survives the updated code crashing on startup.
"""

import hashlib
import json
import logging
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

logger = logging.getLogger(__name__)

STATE_IDLE = 'idle'
STATE_PROBATION = 'probation'
STATE_CONFIRMED = 'confirmed'
STATE_ROLLED_BACK = 'rolled_back'

# How long a freshly-installed version has to prove itself by completing one upload. Generous on
# purpose: the device may be mid-backoff, out of coverage, or simply waiting for motion, and
# rolling back a *working* update because nothing happened to photograph would be its own bug.
# Sized against this deployment's actual capture pattern, which is near-silent overnight — an
# update landing at 19:00 may legitimately see no upload until captures resume the next morning.
DEFAULT_PROBATION_S = 6 * 60 * 60

# Hard ceiling on deferral. The watchdog holds off rolling back while the outbox is empty (no
# upload attempt means no evidence either way), but a recorder that has itself broken would keep
# the outbox empty forever, so deferral cannot be unbounded.
DEFAULT_MAX_PROBATION_S = 24 * 60 * 60

# How long a rolled-back version is left alone before it will be tried again. Without this a
# device that rolls back re-downloads the same bundle on its next check and loops, spending
# ~52KB of a ~1.8KB/s link every time. Retried eventually rather than never, since a rollback
# may have been caused by a quiet period rather than by the code.
RETRY_AFTER_ROLLBACK_S = 24 * 60 * 60


class Updater:
    """Fetches, stages and confirms client updates. Owns no modem — the caller passes one in,
    since the uploader already has the serial port open and two openers would contend."""

    def __init__(self, install_dir: Path, server_url: str, device_id: str = '',
                 probation_s: int = DEFAULT_PROBATION_S, enabled: bool = True,
                 max_probation_s: int = DEFAULT_MAX_PROBATION_S,
                 retry_after_rollback_s: int = RETRY_AFTER_ROLLBACK_S):
        self.install_dir = Path(install_dir)
        self.server_url = server_url.rstrip('/')
        self.device_id = device_id
        self.probation_s = probation_s
        self.max_probation_s = max_probation_s
        self.retry_after_rollback_s = retry_after_rollback_s
        self.enabled = enabled
        self.state_path = self.install_dir / 'update_state.json'
        self.snapshot_dir = self.install_dir / '.update_snapshot'
        self.staging_dir = self.install_dir / '.update_staging'

    # ── state ────────────────────────────────────────────────────────────────

    def load_state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text())
        except Exception:
            return {'state': STATE_IDLE, 'version': None}

    def save_state(self, **fields) -> dict:
        state = {**self.load_state(), **fields}
        tmp = self.state_path.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(state, indent=2) + '\n')
        tmp.replace(self.state_path)   # atomic: the watchdog may read this at any moment
        return state

    # ── the probation contract ───────────────────────────────────────────────

    def note_successful_upload(self) -> None:
        """Called by the uploader after a capture is accepted by the server. This is the single
        piece of evidence that promotes an update out of probation, because it is the only event
        that proves the whole chain — camera, disk, modem, bearer, server — still works end to
        end on the new code."""
        state = self.load_state()
        if state.get('state') != STATE_PROBATION:
            return
        logger.info(f"Update {state.get('version')} confirmed by a successful upload")
        self.save_state(state=STATE_CONFIRMED, confirmed_at=time.time())
        shutil.rmtree(self.snapshot_dir, ignore_errors=True)

    def probation_expired(self, now: float | None = None) -> bool:
        state = self.load_state()
        if state.get('state') != STATE_PROBATION:
            return False
        started = state.get('started_at', 0)
        return (now or time.time()) - started > state.get('probation_s', self.probation_s)

    # ── fetching ─────────────────────────────────────────────────────────────

    def check(self, modem) -> dict | None:
        """The published manifest, or None if there's nothing new or nothing published. Cheap
        enough to call on a normal upload cycle: the manifest is a few dozen bytes."""
        if not self.enabled:
            return None
        status, body = modem.http_get_body(
            f'{self.server_url}/client-update/manifest?device_id={self.device_id}', max_bytes=4096)
        if status != 200 or not body:
            return None
        try:
            manifest = json.loads(body)
        except ValueError:
            logger.warning(f"Update manifest was not JSON ({body[:80]!r})")
            return None
        version = manifest.get('version')
        if not version or version == self.current_version():
            return None
        if self.recently_rolled_back(version):
            return None
        return manifest

    def recently_rolled_back(self, version: str, now: float | None = None) -> bool:
        """Whether this exact version was rolled back too recently to be worth retrying.

        Without this the device re-downloads the version it just rejected on the very next check
        and loops forever. Publishing a *different* version clears the block immediately, since
        the match is on the version string — so a fix is never held up by a previous failure."""
        state = self.load_state()
        if state.get('rolled_back_from') != version:
            return False
        since = (now or time.time()) - state.get('rolled_back_at', 0)
        if since >= self.retry_after_rollback_s:
            return False
        logger.info(
            f"Skipping {version}: rolled back {since / 3600:.1f}h ago, "
            f"retrying after {self.retry_after_rollback_s / 3600:.0f}h"
        )
        return True

    def current_version(self) -> str | None:
        return self.load_state().get('version')

    def fetch(self, modem, manifest: dict) -> Path | None:
        """Download and verify a bundle, returning the path to the verified tarball.

        Refuses to return anything whose digest doesn't match. A truncated or corrupted transfer
        over this link is a normal event, not an exceptional one — a half-written update that
        installs is exactly the failure this whole module exists to prevent."""
        url = f'{self.server_url}/client-update/bundle?device_id={self.device_id}'
        encoded_size = manifest.get('encoded_size', 0)
        status, body = modem.http_get_body(url, max_bytes=max(encoded_size, 1) + 1024)
        if status != 200:
            logger.warning(f"Update bundle fetch returned HTTP {status}")
            return None

        import base64
        try:
            raw = base64.b64decode(body, validate=True)
        except Exception as e:
            logger.warning(f"Update bundle was not valid base64 ({e}) — discarding")
            return None

        digest = hashlib.sha256(raw).hexdigest()
        if digest != manifest.get('sha256'):
            logger.warning(
                f"Update bundle digest mismatch (got {digest[:12]}, "
                f"expected {str(manifest.get('sha256'))[:12]}) — discarding"
            )
            return None

        out = Path(tempfile.mkdtemp(prefix='update_')) / f"{manifest['version']}.tgz"
        out.write_bytes(raw)
        logger.info(f"Fetched update {manifest['version']} ({len(raw)} bytes, digest verified)")
        return out

    # ── staging and activation ───────────────────────────────────────────────

    @staticmethod
    def _safe_members(tar: tarfile.TarFile, dest: Path):
        """Members that stay inside `dest`. A bundle is trusted-ish — it came from our own server
        over a verified digest — but an archive that can write outside the install directory is
        the kind of thing that should be impossible by construction, not by trust."""
        for member in tar.getmembers():
            target = (dest / member.name).resolve()
            if not target.is_relative_to(dest.resolve()):
                raise ValueError(f'Refusing path outside install dir: {member.name}')
            if member.issym() or member.islnk():
                raise ValueError(f'Refusing link member: {member.name}')
            yield member

    def stage(self, bundle: Path) -> Path:
        """Unpack a verified bundle and check it compiles. Returns the staging directory.

        The compile check is the cheapest possible smoke test and catches the most likely way a
        bundle is broken — a syntax error reaches every module at import and would take the
        uploader down before it could ever report anything."""
        shutil.rmtree(self.staging_dir, ignore_errors=True)
        self.staging_dir.mkdir(parents=True)
        with tarfile.open(bundle, 'r:gz') as tar:
            tar.extractall(self.staging_dir, members=self._safe_members(tar, self.staging_dir))

        sources = sorted(self.staging_dir.rglob('*.py'))
        if not sources:
            raise ValueError('Bundle contains no Python sources — refusing to install')
        result = subprocess.run([sys.executable, '-m', 'py_compile', *map(str, sources)],
                                capture_output=True, text=True)
        if result.returncode != 0:
            raise ValueError(f'Bundle failed to compile: {result.stderr.strip()[:300]}')
        return self.staging_dir

    def activate(self, version: str, restart: bool = True) -> None:
        """Snapshot what's installed, copy the staged tree over it, and enter probation.

        The snapshot is taken of only the paths the bundle actually replaces. Copying the whole
        install directory would sweep in the venv, the outbox and the config, which are both large
        and the things you least want a rollback to revert."""
        shutil.rmtree(self.snapshot_dir, ignore_errors=True)
        self.snapshot_dir.mkdir(parents=True)

        replaced = []
        for src in sorted(self.staging_dir.rglob('*')):
            if src.is_dir():
                continue
            rel = src.relative_to(self.staging_dir)
            live = self.install_dir / rel
            if live.exists():
                backup = self.snapshot_dir / rel
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(live, backup)
            replaced.append(str(rel))

        self.save_state(state=STATE_PROBATION, version=version, started_at=time.time(),
                        probation_s=self.probation_s, max_probation_s=self.max_probation_s,
                        replaced=replaced, previous_version=self.load_state().get('version'))

        for src in sorted(self.staging_dir.rglob('*')):
            if src.is_dir():
                continue
            live = self.install_dir / src.relative_to(self.staging_dir)
            live.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, live)
        shutil.rmtree(self.staging_dir, ignore_errors=True)

        logger.warning(
            f"Installed update {version} — on probation for {self.probation_s / 3600:.1f}h, "
            f"rolls back automatically unless an upload succeeds"
        )
        if restart:
            self.restart_services()

    def rollback(self, reason: str, restart: bool = True) -> bool:
        """Restore the snapshot taken at activate(). Safe to call twice."""
        state = self.load_state()
        if not self.snapshot_dir.exists():
            logger.error(f"Rollback wanted ({reason}) but no snapshot exists")
            self.save_state(state=STATE_ROLLED_BACK, rollback_reason=f'{reason}; no snapshot')
            return False

        for src in sorted(self.snapshot_dir.rglob('*')):
            if src.is_dir():
                continue
            live = self.install_dir / src.relative_to(self.snapshot_dir)
            live.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, live)

        logger.error(f"Rolled back update {state.get('version')} to "
                     f"{state.get('previous_version') or 'the pre-update version'}: {reason}")
        self.save_state(state=STATE_ROLLED_BACK, version=state.get('previous_version'),
                        rolled_back_from=state.get('version'), rollback_reason=reason,
                        rolled_back_at=time.time())
        shutil.rmtree(self.snapshot_dir, ignore_errors=True)
        if restart:
            self.restart_services()
        return True

    # The uploader is deliberately absent: this runs *inside* it, and `systemctl restart` on
    # your own unit is a SIGTERM to yourself. The uploader has no chance to close the GPRS
    # bearer or the serial port, and the modem can be left in a state the restarted process
    # cannot recover from — with no SMS either, because it dies before reaching the loop that
    # polls for it. `poll()` returns True instead, and the caller exits cleanly; Restart=always
    # brings it back on the new code just the same. The watchdog, a separate process, still
    # restarts both units directly.
    RESTART_UNITS = ('monitoring-pipeline-recorder',)

    @classmethod
    def restart_services(cls) -> None:
        """Restart the *other* services so the swapped-in code takes effect."""
        for unit in cls.RESTART_UNITS:
            try:
                subprocess.run(['systemctl', 'restart', unit], check=False, timeout=30)
            except Exception as e:
                logger.error(f'Could not restart {unit}: {e}')

    # ── the one call the uploader makes ──────────────────────────────────────

    def poll(self, modem) -> bool:
        """Check for, fetch and install an update. Returns True if one was installed, which
        obliges the caller to shut down cleanly so systemd restarts it on the new code — this
        function will not do that for it, see RESTART_UNITS.

        Never raises into the upload loop: a broken update channel must not be able to stop the
        device doing its actual job."""
        if not self.enabled or self.load_state().get('state') == STATE_PROBATION:
            return False
        try:
            manifest = self.check(modem)
            if not manifest:
                return False
            bundle = self.fetch(modem, manifest)
            if not bundle:
                return False
            self.stage(bundle)
            self.activate(manifest['version'])
            return True
        except Exception:
            logger.exception('Update attempt failed — staying on the current version')
            shutil.rmtree(self.staging_dir, ignore_errors=True)
            return False
