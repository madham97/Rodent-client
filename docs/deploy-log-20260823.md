# Deployment record — self-update release on `rodent`

**Host:** `rodent` (Raspberry Pi) · **Repo:** `/opt/Rodent-client` · **Branch:** `fix/thermal-fixed-range-normalization`
**Date:** 23 August 2026, 14:13–14:44 CEST (times below are CEST, from journald)

Pulled three commits, installed the update watchdog, enabled native thermal transport and
self-update, and verified both against the server. One real bug surfaced and was fixed: the new
modem read path failed every manifest check. **The fix is live on this device only — the published
bundle still carries the broken version.**

No credentials are reproduced in this document.

---

## What was pulled

Fast-forward, `0eb369b` → `65870f6`.

| Commit | Change |
|---|---|
| `5b14f11` | Recorder stores the thermal normalization range in metadata, making frames self-describing |
| `74550b5` | Native transport for thermal frames — separate upload part, smaller payload |
| `65870f6` | Self-update mechanism: `updater.py`, watchdog timer, bundle build script |

A local edit to `thermal.png` was present throughout and was never touched — it isn't in the
incoming diff.

---

## Sequence

| Time | What |
|---|---|
| 14:13 | Fast-forward pull, then `test_updater.py` 26/26. Ancestry verified first so the pull could not merge. |
| 14:16 | Watchdog `.service` + `.timer` installed to `/etc/systemd/system`, enabled. Dry run: `Nothing on probation (state=idle)`. Fires every 15 min. |
| 14:17 | Config backed up to `config/client.json.bak-20260823T141650`, ten missing keys added. |
| 14:18 | Uploader restarted, **then** recorder — deliberate ordering, see below. |
| 14:27 | 17 legacy captures (56 MB) parked in `data/deferred_legacy_rgba/` to drain the queue. |
| 14:29 | First native-format capture uploaded: `image_20260823T121856Z.jpg`, 89,078 bytes in 48.1 s. |
| 14:30 | First self-update check failed — `ModemLinkError: HTTPREAD returned nothing at offset 0`. |
| 14:32 | Server repo shallow-cloned to `/opt/Server` (a full clone stalled at 2.5 MB). |
| 14:38 | Fix applied, `update_state.json` seeded, uploader restarted. No update error since. |

**On the restart order.** Services had been running since 9 Aug, so they were still executing
pre-pull code until 14:18. The uploader went first because the old one had no `_thermal.png`
exclusion and would have uploaded thermal halves as standalone photos. Confirmed zero went out.

**On parking the backlog.** Captures were *moved*, not deleted, and the in-flight upload was
identified from the log and left alone.

---

## Config keys added

The live config predated all three commits. Eight keys match the code's own defaults and change
nothing; two were decisions.

| Key | Value | Note |
|---|---|---|
| `thermal_native_transport` | `true` | **Decision** — changes the wire format |
| `update_enabled` | `true` | **Decision** — device pulls and installs bundles |
| `update_check_interval` | `21600` | 6 h, matches default |
| `update_probation_s` | `21600` | 6 h to prove one upload |
| `update_max_probation_s` | `86400` | Cap on empty-outbox deferral |
| `install_dir` | `"/opt/Rodent-client"` | Tree the bundle extracts over |
| `confirm_path` | `"/annotate/specific/{name}"` | Matches default |
| `gsm_baud` | `115200` | Matches default |
| `gsm_number` | `""` | Matches default |
| `thermal_rotation` | `0` | Matches default |

Five keys in the live config have no counterpart in the template (`bitrate`, `chunk_duration`,
`enabled`, `framerate`, `rpicam_vid_path`) — leftovers from video mode, left in place.

---

## Native thermal transport — verified end to end

This was the one change that could have broken the live pipeline, and it could not be verified
before the server repo was available. The server is reachable over GSM but not from this Pi's LAN,
so an HTTP probe from here proved nothing either way.

The cloned server settles it — `routers/upload.py` declares the part explicitly:

```python
image:   UploadFile = File(None)
thermal: UploadFile = File(None)     # accepts the new part
...
thermal_norm_min_c: str = Form("")
thermal_norm_max_c: str = Form("")   # and the 5b14f11 metadata
```

So the HTTP 200 was a genuine store rather than a silently-ignored part — the failure mode worth
worrying about. Recorder output changed as expected:

```
image_20260823T121856Z.jpg
image_20260823T121856Z_thermal.png
sidecar: format "rgb+thermal_native", thermal_norm_min_c 10.0, thermal_norm_max_c 45.0
```

Payload dropped from ~118 KB to 89 KB per capture, roughly 25% off a link that moves about
1.8 KB/s. The thermal half is unlinked after a successful upload — intended, since it was already
sent inside the same POST.

---

## The bug: `http_get_body` discarded the modem reply

Every manifest check failed, deterministically, at offset 0. Not a server problem — the update
endpoints exist and a bundle is published.

```python
self._send(f'AT+HTTPREAD={offset},{want}', wait=0.3, heal=False)   # reply thrown away
raw = self._wait_for('OK', timeout=30)
```

`_send_once` sleeps for `wait`, then drains the port and returns what it read. The manifest is
about 120 bytes, so at 115200 baud the entire reply — echo, `+HTTPREAD: 120`, payload, `OK` —
lands well inside that 300 ms and was consumed and dropped. `_wait_for` then sat on an empty
buffer for its full 30 s and returned an empty string.

Two pieces of evidence match exactly: the failure was at **offset 0**, the first read; and the gap
between the upload finishing at 14:29:55 and the error at 14:30:40 is 45 s — HTTP setup plus that
30 s timeout.

### Fix — `pi-client/uploader.py`

```python
raw = self._send(f'AT+HTTPREAD={offset},{want}', wait=0.3, heal=False)
if 'OK' not in raw:
    raw += self._wait_for('OK', timeout=30)
piece = self._extract_httpread(raw)
```

The conditional matters: large payloads still need the follow-up read, so this repairs the
small-payload case without breaking the chunked bundle download.

### How it was verified

- A reproduction test drives `http_get_body` against a simulated modem where `_send` captures the
  whole small reply. It raises the identical `ModemLinkError` at the same line against the unfixed
  copy inside the published bundle, and passes against the fix.
- `test_updater.py` 26/26, `test_uploader_recovery.py` 35/35.
- On the device: no update error in the 150 s watched after the uploader came up at 14:38:58 with
  an empty outbox. Worth stating plainly — a check that finds a matching version logs nothing, so
  the on-device evidence is the *absence* of an error that previously fired within 45 s of every
  drain, not a positive confirmation line.

The suite passes without catching this because `test_updater.py` mocks the modem entirely; nothing
exercises `http_get_body`. That coverage gap is why the bug reached the device.

---

## Why `update_state.json` was seeded

**This is a guard, not a resolution.** The published bundle `20260822T125031Z.tgz` contains the
*unfixed* `uploader.py`. Once the read path worked, the device would have fetched that bundle,
reverted the fix, and been unable to update again.

Seeding `version: 20260822T125031Z` with `state: confirmed` makes `check()` treat it as already
installed and skip it. The watchdog reads the file correctly
(`Nothing on probation (state=confirmed)`).

The claim is close to true rather than exactly true: the bundle was byte-identical to the tree when
checked, and now differs by the one-hunk fix. A note recording that is stored in the file itself.

---

## Outstanding

- [ ] **Rebuild and republish the bundle from the fixed tree.** Run
      `scripts/build-update-bundle.sh` against the **real** server at the deployment address, not
      the clone at `/opt/Server`. Until then the fix exists only on this Pi, and any other device
      pulling the current bundle gets the bug. A new version string supersedes the seeded one
      automatically.
- [ ] **Return the 17 parked captures** in `data/deferred_legacy_rgba/` (56 MB) to the outbox. The
      new uploader handles the legacy RGBA format correctly — it uploaded several before they were
      parked — so there is no technical reason to hold them.
- [ ] **Commit the fix.** Nothing was committed. The working tree carries the `uploader.py` hunk,
      the config backup, and `update_state.json`.
- [ ] **Consider covering `http_get_body` in the test suite.** The reproduction written during this
      session is a starting point; it currently lives outside the repo.

---

## Rollback pointers

- **Config** — `config/client.json.bak-20260823T141650` is the pre-session file.
- **Thermal format** — set `thermal_native_transport: false` and restart the recorder to return to
  alpha-channel fusion.
- **Self-update** — set `update_enabled: false` and restart the uploader. The watchdog timer stays
  installed and armed either way, which is what keeps re-enabling it a config change rather than a
  site visit.
- **Parked captures** — `data/deferred_legacy_rgba/`, nothing deleted.
