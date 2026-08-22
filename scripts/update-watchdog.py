#!/usr/bin/env python3
"""
Rolls back a client update that never proved itself.

Run from a systemd timer. Deliberately **self-contained**: standard library only, and it imports
nothing from pi-client. Its whole job is to work when the code it is watching does not, and
importing `updater` from the very tree an update just replaced would put the rescue path inside
the blast radius. (Staging compile-checks every bundle, so a syntax error can't get that far —
but a module that imports fine and misbehaves at runtime can, and this still has to work then.)

The duplication with `updater.Updater.rollback` is the price of that independence. The shared
contract is the on-disk state file, not the code:

    {"state": "probation", "version": ..., "started_at": ..., "probation_s": ...,
     "previous_version": ...}   plus the snapshot tree at .update_snapshot/

An update leaves probation only when the uploader completes a real upload. If that never happens,
this restores the snapshot.

One refinement: an empty outbox means there was nothing to upload, so the absence of an upload is
no evidence against the new code — this deployment is near-silent overnight and would otherwise
roll back perfectly good updates. Deferral is capped, because a broken *recorder* would also keep
the outbox empty forever.

Usage:  update-watchdog.py [--install-dir /opt/Rodent-client] [--dry-run]
Exit:   0 nothing to do / rolled back   1 (--dry-run) would roll back   2 rollback failed
"""

import argparse
import json
import logging
import shutil
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_MAX_PROBATION_S = 24 * 60 * 60
UNITS = ('monitoring-pipeline-recorder', 'monitoring-pipeline-uploader')


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {'state': 'idle'}


def save_state(path: Path, state: dict) -> None:
    tmp = path.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(state, indent=2) + '\n')
    tmp.replace(path)


def outbox_pending(install_dir: Path) -> int | None:
    """How many captures are queued, or None if that can't be determined.

    Read from client.json, which is never part of a bundle and so is unaffected by whatever the
    update did."""
    try:
        cfg = json.loads((install_dir / 'config' / 'client.json').read_text())
        outbox = Path(cfg.get('outbox_dir', '/outbox'))
        return sum(1 for p in outbox.glob('*')
                   if p.is_file() and not p.name.endswith(('.tmp', '.json')))
    except Exception:
        return None


def rollback(install_dir: Path, state_path: Path, state: dict, reason: str) -> int:
    snapshot = install_dir / '.update_snapshot'
    if not snapshot.exists():
        logging.error(f"Rollback wanted ({reason}) but no snapshot exists")
        save_state(state_path, {**state, 'state': 'rolled_back',
                                'rollback_reason': f'{reason}; no snapshot'})
        return 2

    restored = 0
    for src in sorted(snapshot.rglob('*')):
        if src.is_dir():
            continue
        live = install_dir / src.relative_to(snapshot)
        live.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, live)
        restored += 1

    logging.error(f"Rolled back {state.get('version')} -> "
                  f"{state.get('previous_version') or 'pre-update'} ({restored} files): {reason}")
    save_state(state_path, {**state, 'state': 'rolled_back',
                            'version': state.get('previous_version'),
                            'rolled_back_from': state.get('version'),
                            'rollback_reason': reason, 'rolled_back_at': time.time()})
    shutil.rmtree(snapshot, ignore_errors=True)

    for unit in UNITS:
        try:
            subprocess.run(['systemctl', 'restart', unit], check=False, timeout=30)
        except Exception as e:
            logging.error(f'Could not restart {unit}: {e}')
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--install-dir', default='/opt/Rodent-client')
    parser.add_argument('--dry-run', action='store_true',
                        help='report what would happen without touching anything')
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

    install_dir = Path(args.install_dir)
    state_path = install_dir / 'update_state.json'
    state = load_state(state_path)

    if state.get('state') != 'probation':
        logging.info(f"Nothing on probation (state={state.get('state', 'idle')})")
        return 0

    elapsed = time.time() - state.get('started_at', 0)
    window = state.get('probation_s', 6 * 60 * 60)
    if elapsed < window:
        logging.info(f"Update {state.get('version')} has {(window - elapsed) / 3600:.1f}h "
                     f"of its probation window left")
        return 0

    max_window = state.get('max_probation_s', DEFAULT_MAX_PROBATION_S)
    pending = outbox_pending(install_dir)
    if pending == 0 and elapsed < max_window:
        logging.info(
            f"Update {state.get('version')} past its window but the outbox is empty — nothing "
            f"has needed uploading, so there is no evidence against it. Deferring "
            f"({elapsed / 3600:.1f}h of {max_window / 3600:.0f}h cap)"
        )
        return 0

    if pending is None:
        logging.warning("Could not read the outbox — judging on elapsed time alone")

    reason = (f'no successful upload in {elapsed / 3600:.1f}h'
              + (f' with {pending} capture(s) queued' if pending else ''))
    if args.dry_run:
        logging.warning(f"[dry-run] would roll back {state.get('version')}: {reason}")
        return 1
    return rollback(install_dir, state_path, state, reason)


if __name__ == '__main__':
    sys.exit(main())
