# AGENTS.md

Guidance for AI coding agents working in this repository. Human-facing docs live in
[README.md](README.md) and [docs/](docs/); this file is the orientation layer on top of them.
The README is unusually complete — treat it as the reference, not as background.

## What this is

The Pi-side edge client for a wildlife/rodent monitoring deployment. It captures paired
visible + thermal frames on motion (or on a timer), queues them in an outbox, and uploads them
over a SIM868/SIM800C GSM modem driven entirely by AT commands. It also runs a local web
dashboard, accepts config changes over SMS, and can pull and install its own updates.

This is one half of a two-repo system. The other half is **`Server`** (FastAPI ingest,
YOLOv8 detection, annotation/training UIs) — checked out at `/home/haamer/Server` on
sentinel-compute, with its own `AGENTS.md`. Read that file before changing anything on the
wire.

**The one constraint behind every odd design decision here: the link is one-way.** The SIM800
is driven by AT commands, so the Pi has no IP stack on that link — no SSH, no Tailscale, no
inbound route of any kind, at ~1.8 KB/s. The device can only ever *pull*. That is why updates
are published as bundles the device fetches, why config changes arrive by SMS, why a 300 KB
upload ceiling matters, and why the probation/rollback contract in `updater.py` exists at all.
A change that assumes you can reach the device to fix it is wrong.

## Where you are

**Check `hostname` and whether `/opt/Rodent-client` exists before running anything.** This repo
lives in three kinds of place and they are not interchangeable:

| Location | What it is | What you can do |
|---|---|---|
| `/home/haamer/Rodent-client` on `sentinel-compute` | Build/publish checkout. No Pi hardware, no `monitoring-pipeline-*` services, no `/opt/Rodent-client` | Edit source, run the offline tests, build and publish bundles |
| `/opt/Rodent-client` on a field Pi | The live install | `systemctl` the services, read `journalctl` — reachable only by physical access or Tailscale over WiFi, never over GSM |
| A dev workstation | Source only | Offline tests only |

Almost every command in the README assumes the Pi. On sentinel-compute the only things that
run are the offline tests and `scripts/build-update-bundle.sh`. Nothing here talks to hardware,
so do not report a hardware-dependent change as verified from this host.

## Layout

| Path | Role |
|---|---|
| [pi-client/recorder.py](pi-client/recorder.py) | Both camera streams, motion detection, frame pairing, RGBA fusion, sidecar writing |
| [pi-client/uploader.py](pi-client/uploader.py) | Modem driver (AT commands, GPRS bearer, SMS), upload queue, failure classification, update polling |
| [pi-client/updater.py](pi-client/updater.py) | Fetch → verify digest → compile-check → snapshot → swap → probation |
| [pi-client/web_ui.py](pi-client/web_ui.py) | Local dashboard on :8080 |
| [pi-client/thermal/](pi-client/thermal/) | `thermal_common.py` (MI48 driver, CRC-retrying `read_frame`, `frame_to_gray8`) + standalone capture tools |
| [scripts/update-watchdog.py](scripts/update-watchdog.py) | Rollback timer. **Stdlib only, imports nothing from `pi-client`** — see invariants |
| [scripts/build-update-bundle.sh](scripts/build-update-bundle.sh) | The only supported way to publish an update |
| [config/client.json.template](config/client.json.template) | Every setting and its default. The live `client.json` is gitignored |
| [systemd/](systemd/) | Seven unit files; `install.sh` installs four services plus the watchdog timer. `gsm.service` and `gsm-pin.service` are legacy — the uploader does PIN unlock and HTTP itself |
| [docs/](docs/) | `project-overview.md`, `gsm-hat-setup.md`, `thermal-camera-setup.md`, `deploy-log-20260823.md`, enclosure CAD |

Runtime state (`data/`, `config/client.json`, `config/webui.env`, `update_state.json`, `venv/`)
is gitignored — it is per-device, and a bundle must never carry it.

## Testing

Three offline tests, runnable anywhere with no modem, no camera and no server:

```bash
cd pi-client && python3 test_uploader_recovery.py && python3 test_updater.py && python3 test_thermal_geometry.py
```

They exist because the failures they cover all look alike in a log. `test_uploader_recovery.py`
in particular pins down that a stalled serial link, a delivered image whose reply was lost, and
a genuinely rejected image must be handled *differently* — and that only the last may be parked
in `failed/`.

**Exit code 77 means "could not run" (optional deps absent), not "passed."** The build script
reports it separately and loudly. Do not treat a 77 as a green test.

`test_record_upload.py` needs a real server URL and is not part of the gate.

Anything touching the modem, the cameras, or the GSM link cannot be verified from a
workstation. Say what you actually ran, and say what still needs a device.

## Invariants — do not break these

- **Thermal alpha is normalized against the *fixed* `thermal_norm_min_c`/`thermal_norm_max_c`
  window (default 10–45 °C), never each frame's own min/max.** Per-frame normalization means the
  same real temperature maps to a different byte depending on what else is in view, which
  destroys any cross-frame analysis — that is exactly what broke background-subtraction
  calibration on the server. `thermal_min_c`/`thermal_max_c`/`thermal_avg_c` in the sidecar are
  observed-range *telemetry* and must never be used to decode.
- **Captures that cannot be paired within `thermal_max_skew_s` are discarded, not saved
  unpaired.** For a moving subject a mistimed pair is wrong data, not degraded data. The drop is
  logged with a running count; a persistent thermal fault therefore shows up as *silence* in the
  outbox, not an error.
- **Upload failures are three classes, not one** ([uploader.py:57-66](pi-client/uploader.py#L57-L66)):
  `BLOCKED_STATUSES` (401/402/403/407/429/451 — the endpoint refuses everything) pauses uploads
  with 60 s→1 h backoff and leaves the queue intact; `REJECT_STATUSES` (400/413/415/422 — this
  specific image) parks it in `failed/` immediately; everything else retries. Collapsing these
  costs a full ~120 KB POST per pointless retry on a metered 2G link.
- **Confirm before re-sending.** A stall can swallow the modem's reply after the POST was
  delivered; `confirm_path` asks the server whether the image already arrived. `+HTTPSTATUS:
  POST,0,0,0` means the HTTP engine is *idle*, not that the upload failed.
- **`MAX_FILE_BYTES = 300_000`** — the SIM800's internal HTTP buffer. This is a hardware limit,
  not a tuning knob.
- **The upload token goes in a multipart `token` field**, not only a header: SIM800 firmware
  handles `AT+HTTPPARA="USERDATA"` inconsistently. The server accepts both paths; keep the form
  field.
- **`scripts/update-watchdog.py` is stdlib-only and imports nothing from `pi-client`.** It is the
  rescue path for a tree an update just broke, so it must not live inside that tree's failure
  domain. The shared contract with `updater.py` is the on-disk `update_state.json`, not code.
  The duplication is deliberate.
- **A bundle contains only source — never `config/client.json`, the venv, or `data/`.** A
  rollback that took the SIM PIN and server URL with it would strand the device permanently.
- **`pi-client/thermal/*.py` must stay in the bundle.** `recorder.py` imports `thermal_common` at
  startup; a bundle omitting it once shipped a recorder calling a `frame_to_gray8` that did not
  exist on the device — which fails at runtime, past the compile check.
- **Probation ends on a completed *upload*, not on a process start.** Losing the uplink is the
  only failure that cannot be fixed remotely, so the uplink is what has to be proven.
- **Publish only via `scripts/build-update-bundle.sh`.** It compile-checks, runs the gate, and
  reports the digest. Hand-rolling a tarball skips all three.

## Conventions

- Comments here explain **why**, at length, and cite measured numbers and real incidents (the
  ~1.8 KB/s throughput, the 200-capture sample behind the 10–45 °C window, the `set -e` bug that
  turned "skipped" into "build stops here"). Match that register.
- Secrets discipline in `.gitignore` is deliberate and load-bearing: `config/client.json*` is
  globbed on the **prefix** because `install.sh` writes dated backups like
  `client.json.bak-20260823T141650` that `*.bak` does not match — that near-miss is how a live
  `gsm_pin` and `upload_token` once reached a public commit. The
  `!config/client.json.template` negation must stay too. Do not "tidy" these rules.
- New config keys go in `config/client.json.template` with their default, and in the README's
  Configuration section with the reasoning. A key that only exists in code is invisible to the
  field.
- Timestamps are UTC ISO 8601; capture stems carry `YYYYMMDDTHHMMSSZ` and the filename timestamp
  is the frame's **exposure** time, not its write time.

## The wire contract with `Server`

`/upload` is a multipart POST. Always sent: `device_id`, `mode`, `motion_score`, `timestamp`.
Sent when present: `format`, `thermal_min_c`, `thermal_max_c`, `thermal_avg_c`,
`thermal_norm_min_c`, `thermal_norm_max_c`, `token`, plus `image` and an optional native
`thermal` part. The server's endpoint signature (`routers/upload.py` in the `Server` repo)
matches this field-for-field.

**Changing either side alone breaks the deployment**, and the device cannot be reached to fix
it — the client is the half you cannot roll back remotely once it stops uploading. Change the
server first, keep it backward-compatible with the fleet's current bundle, then publish the
client change and let probation prove it.

## Known sharp edges

- **SMS config commands are accepted from any phone number.** Anyone who knows the SIM's number
  can reconfigure the device. There is no authentication.
- **Update bundles are unsigned.** The sha256 covers transit corruption, not a compromised
  server: anyone who can write `Server/client_updates/` runs code on every device.
- **The README's update settings table says `update_check_interval` defaults to 21600 (6 h);
  the real default is 7200 (2 h)** — `DEFAULT_UPDATE_CHECK_S` in
  [uploader.py:49](pi-client/uploader.py#L49) and the template both say 2 h. The table is stale
  after commit 8b5822b.
- **`update_state.json` is gitignored but present in this checkout**, seeded to `confirmed` for
  bundle `20260822T125031Z` with a note about a local `http_get_body` fix. Read that note before
  publishing — republishing without superseding it can revert the fix.
- Removing a `.tgz` from the server's `client_updates/` rolls the fleet **back**. The winning
  bundle is the one with the newest **mtime**, not the newest filename or version string
  (`routers/client_update.py` in the `Server` repo) — so copying bundles around without
  preserving timestamps, or touching an old file, silently re-elects it.
- A rolled-back version is not retried for 24 h. Publishing a *different* version clears the
  block immediately.
- Debian's `python3-simplejpeg` is built against the numpy 1.x ABI and breaks `picamera2` under
  numpy 2.x. Do not downgrade numpy — install a numpy-2 `simplejpeg` into the venv. See the
  README troubleshooting table.

## Git

Work on a branch. The current branch is `fix/thermal-fixed-range-normalization`; `main` is the
default. Do not commit `config/client.json`, any `client.json.bak*`, `webui.env`, captures, or
`data/`.

**`main` is not the record of what is deployed — the published bundle is.** The server's
`client_updates/` holds the tarballs the fleet actually pulled, and they are deliberately
untracked there; this repo is the source of record for their contents (bundle
`20260824T074110Z.tgz` matches commit `8b5822b` byte for byte). When you change client code,
say explicitly whether it has been published, because committing it does not ship it.
