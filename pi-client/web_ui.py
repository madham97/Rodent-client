#!/usr/bin/env python3
"""
Monitoring Pipeline - Web UI
Flask dashboard for managing the recording and upload pipeline.
Access at http://<pi-ip>:8080  (credentials in config/webui.env)
"""

import json
import os
import subprocess
from pathlib import Path
from functools import wraps

from flask import Flask, request, Response, jsonify

# ─── Config ───────────────────────────────────────────────────────────────────

CONFIG_PATH = Path(os.environ.get('CONFIG_PATH', '/opt/Rodent-client/config/client.json'))
LOG_PATH    = Path(os.environ.get('LOG_PATH',    '/var/log/monitoring-pipeline.log'))

SERVICES = {
    'recorder': 'monitoring-pipeline-recorder',
    'uploader': 'monitoring-pipeline-uploader',
}

WEBUI_USERNAME = os.environ.get('WEBUI_USERNAME', 'admin')
WEBUI_PASSWORD = os.environ.get('WEBUI_PASSWORD', 'monitoring')
WEBUI_PORT     = int(os.environ.get('WEBUI_PORT', 8080))

app = Flask(__name__)

# ─── Auth ─────────────────────────────────────────────────────────────────────

def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or auth.username != WEBUI_USERNAME or auth.password != WEBUI_PASSWORD:
            return Response(
                'Authentication required.',
                401,
                {'WWW-Authenticate': 'Basic realm="Monitoring Pipeline"'},
            )
        return f(*args, **kwargs)
    return decorated

# ─── Helpers ──────────────────────────────────────────────────────────────────

def get_service_status(service_name):
    try:
        r = subprocess.run(
            ['systemctl', 'is-active', service_name],
            capture_output=True, text=True, timeout=5,
        )
        return r.stdout.strip()
    except Exception:
        return 'unknown'


def get_gsm_signal():
    """Read the most recent signal strength from the log file (avoids serial port contention)."""
    try:
        result = subprocess.run(
            ['grep', '-a', 'Modem ready. Signal:', str(LOG_PATH)],
            capture_output=True, text=True, timeout=5,
        )
        lines = result.stdout.strip().splitlines()
        if lines:
            # Format: "Modem ready. Signal: 19/31"
            val = lines[-1].split('Signal:')[1].split('/')[0].strip()
            return int(val)
    except Exception:
        pass
    return None


def get_tailscale_ip():
    try:
        r = subprocess.run(
            ['tailscale', 'ip', '-4'],
            capture_output=True, text=True, timeout=5,
        )
        return r.stdout.strip() or None
    except Exception:
        return None


def dir_stats(path):
    """Return (file_count, total_bytes) for image files in path."""
    IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.webp'}
    try:
        files = [p for p in Path(path).iterdir()
                 if p.is_file() and p.suffix.lower() in IMAGE_EXTS]
        return len(files), sum(f.stat().st_size for f in files)
    except Exception:
        return 0, 0


def fmt_bytes(n):
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024:
            return f'{n:.1f} {unit}'
        n /= 1024
    return f'{n:.1f} TB'

# ─── API ──────────────────────────────────────────────────────────────────────

@app.route('/api/status')
@require_auth
def api_status():
    config = json.loads(CONFIG_PATH.read_text())

    services = {name: get_service_status(svc) for name, svc in SERVICES.items()}

    gsm_device  = config.get('gsm_device', '/dev/serial0')
    gsm_number  = config.get('gsm_number', '')
    signal      = get_gsm_signal()
    ts_ip       = get_tailscale_ip()

    outbox_count, outbox_size     = dir_stats(config.get('outbox_dir',   '/outbox'))
    uploaded_count, uploaded_size = dir_stats(config.get('uploaded_dir', '/uploaded'))

    return jsonify(
        services=services,
        gsm=dict(signal=signal, device=gsm_device, number=gsm_number),
        outbox=dict(count=outbox_count, size=fmt_bytes(outbox_size)),
        uploaded=dict(count=uploaded_count, size=fmt_bytes(uploaded_size)),
        ssh=dict(tailscale_ip=ts_ip),
    )


@app.route('/api/logs')
@require_auth
def api_logs():
    try:
        n = min(int(request.args.get('lines', 100)), 500)
        r = subprocess.run(
            ['tail', f'-n{n}', str(LOG_PATH)],
            capture_output=True, text=True, timeout=5,
        )
        return jsonify(lines=r.stdout.splitlines())
    except Exception as e:
        return jsonify(lines=[], error=str(e))


@app.route('/api/config', methods=['GET'])
@require_auth
def api_config_get():
    return jsonify(json.loads(CONFIG_PATH.read_text()))


@app.route('/api/config', methods=['POST'])
@require_auth
def api_config_save():
    try:
        data = request.get_json(force=True)
        if not isinstance(data, dict):
            return jsonify(error='Config must be a JSON object'), 400
        CONFIG_PATH.write_text(json.dumps(data, indent=2) + '\n')
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(error=str(e)), 500


@app.route('/api/outbox/clear', methods=['POST'])
@require_auth
def api_outbox_clear():
    try:
        config = json.loads(CONFIG_PATH.read_text())
        outbox = Path(config.get('outbox_dir', '/outbox'))
        deleted = 0
        for f in outbox.iterdir():
            if f.is_file():
                f.unlink()
                deleted += 1
        return jsonify(ok=True, deleted=deleted)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


@app.route('/api/service/<name>/<action>', methods=['POST'])
@require_auth
def api_service(name, action):
    if name not in SERVICES:
        return jsonify(error=f'Unknown service: {name}'), 400
    if action not in ('start', 'stop', 'restart'):
        return jsonify(error=f'Unknown action: {action}'), 400
    try:
        r = subprocess.run(
            ['sudo', 'systemctl', action, SERVICES[name]],
            capture_output=True, text=True, timeout=15,
        )
        ok = r.returncode == 0
        return jsonify(ok=ok, output=(r.stdout + r.stderr).strip())
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500

# ─── UI ───────────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Monitoring Pipeline</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css">
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css">
  <style>
    body { background: #f0f2f5; }
    .navbar { background: #1a1a2e !important; }
    .card { border: none; border-radius: 12px; box-shadow: 0 1px 6px rgba(0,0,0,0.07); }
    .status-badge { font-size: 0.72rem; padding: 0.25em 0.6em; border-radius: 20px; font-weight: 700; text-transform: uppercase; letter-spacing: .03em; }
    .s-active   { background: #d1e7dd; color: #0a5c36; }
    .s-inactive { background: #f8d7da; color: #842029; }
    .s-failed   { background: #f8d7da; color: #842029; }
    .s-unknown  { background: #e2e3e5; color: #41464b; }
    .bar { display: inline-block; width: 5px; border-radius: 2px; margin: 0 1px; vertical-align: bottom; background: #dee2e6; }
    .bar.on { background: #198754; }
    #log-out { font-family: monospace; font-size: 0.8rem; background: #1e1e1e; color: #d4d4d4; border-radius: 8px; padding: 1rem; height: 460px; overflow-y: auto; white-space: pre-wrap; word-break: break-all; }
    #cfg { font-family: monospace; font-size: 0.85rem; }
    .nav-tabs .nav-link { color: #6c757d; }
    .nav-tabs .nav-link.active { font-weight: 600; }
  </style>
</head>
<body>

<nav class="navbar navbar-dark mb-4">
  <div class="container-fluid px-4">
    <span class="navbar-brand mb-0 h1"><i class="bi bi-camera-fill"></i> Monitoring Pipeline</span>
    <span class="text-white-50 small" id="ts"></span>
  </div>
</nav>

<div class="container-fluid px-4" style="max-width:960px">
  <ul class="nav nav-tabs mb-4" id="tabs">
    <li class="nav-item"><a class="nav-link active" href="#" data-tab="dash"><i class="bi bi-speedometer2"></i> Dashboard</a></li>
    <li class="nav-item"><a class="nav-link" href="#" data-tab="logs"><i class="bi bi-journal-text"></i> Logs</a></li>
    <li class="nav-item"><a class="nav-link" href="#" data-tab="cfg"><i class="bi bi-sliders"></i> Config</a></li>
  </ul>

  <!-- Dashboard -->
  <div id="tab-dash">
    <div class="row g-3">
      <div class="col-sm-6">
        <div class="card p-3 h-100">
          <h6 class="text-muted mb-3"><i class="bi bi-gear-fill"></i> Services</h6>
          <div id="svcs"><div class="placeholder-glow"><span class="placeholder col-8 mb-2"></span><span class="placeholder col-7"></span></div></div>
        </div>
      </div>
      <div class="col-sm-6">
        <div class="card p-3 h-100">
          <h6 class="text-muted mb-3"><i class="bi bi-reception-4"></i> GSM Modem</h6>
          <div id="gsm"><div class="placeholder-glow"><span class="placeholder col-6"></span></div></div>
        </div>
      </div>
      <div class="col-sm-6">
        <div class="card p-3">
          <div class="d-flex justify-content-between align-items-center mb-3">
            <h6 class="text-muted mb-0"><i class="bi bi-folder2-open"></i> Outbox</h6>
            <button class="btn btn-outline-danger btn-sm" onclick="clearOutbox()"><i class="bi bi-trash"></i> Clear</button>
          </div>
          <div id="outbox"><div class="placeholder-glow"><span class="placeholder col-4"></span></div></div>
          <div id="outbox-clear-res" class="small mt-2 d-none"></div>
        </div>
      </div>
      <div class="col-sm-6">
        <div class="card p-3">
          <h6 class="text-muted mb-3"><i class="bi bi-check2-circle"></i> Uploaded</h6>
          <div id="uploaded"><div class="placeholder-glow"><span class="placeholder col-4"></span></div></div>
        </div>
      </div>
      <div class="col-12">
        <div class="card p-3">
          <h6 class="text-muted mb-3"><i class="bi bi-terminal-fill"></i> Remote Access</h6>
          <div id="ssh-info"><div class="placeholder-glow"><span class="placeholder col-5"></span></div></div>
        </div>
      </div>
    </div>
  </div>

  <!-- Logs -->
  <div id="tab-logs" style="display:none">
    <div class="card p-3">
      <div class="d-flex align-items-center flex-wrap gap-2 mb-3">
        <h6 class="text-muted mb-0">Log Output</h6>
        <div class="ms-auto d-flex align-items-center gap-2">
          <select class="form-select form-select-sm" id="log-n" style="width:auto">
            <option value="50">50 lines</option>
            <option value="100" selected>100 lines</option>
            <option value="250">250 lines</option>
            <option value="500">500 lines</option>
          </select>
          <button class="btn btn-sm btn-outline-secondary" onclick="fetchLogs()"><i class="bi bi-arrow-clockwise"></i></button>
          <div class="form-check form-switch mb-0 d-flex align-items-center gap-1">
            <input class="form-check-input" type="checkbox" id="log-auto" checked>
            <label class="form-check-label small" for="log-auto">Auto</label>
          </div>
        </div>
      </div>
      <div id="log-out">Loading…</div>
    </div>
  </div>

  <!-- Config -->
  <div id="tab-cfg" style="display:none">
    <div class="row g-3">
      <div class="col-lg-7">
        <div class="card p-3">
          <div class="d-flex align-items-center mb-3 gap-2">
            <h6 class="text-muted mb-0">client.json</h6>
            <div class="ms-auto d-flex gap-2">
              <button class="btn btn-sm btn-outline-secondary" onclick="fetchCfg()"><i class="bi bi-arrow-clockwise"></i> Reload</button>
              <button class="btn btn-sm btn-primary" onclick="saveCfg()"><i class="bi bi-save"></i> Save</button>
            </div>
          </div>
          <div id="cfg-alert" class="alert d-none" role="alert"></div>
          <textarea id="cfg" class="form-control" rows="28" spellcheck="false"></textarea>
        </div>
      </div>
      <div class="col-lg-5">
        <div class="card p-3 h-100">
          <h6 class="text-muted mb-3"><i class="bi bi-info-circle"></i> Key Reference</h6>
          <div style="font-size:0.82rem; overflow-y:auto; max-height:600px">

            <p class="fw-semibold mb-1 text-uppercase" style="font-size:0.7rem;letter-spacing:.05em">Upload</p>
            <table class="table table-sm table-borderless mb-3" style="font-size:0.82rem">
              <tbody>
                <tr><td class="text-nowrap pe-2"><code>server_url</code></td><td class="text-muted">Remote server to upload images to</td></tr>
                <tr><td class="text-nowrap pe-2"><code>webp_compress</code></td><td class="text-muted">Re-encode JPEGs as WebP before upload for smaller transfer (true/false). Original JPEG kept on disk.</td></tr>
                <tr><td class="text-nowrap pe-2"><code>webp_quality</code></td><td class="text-muted">WebP encode quality for upload (1–100, default 80)</td></tr>
                <tr><td class="text-nowrap pe-2"><code>outbox_dir</code></td><td class="text-muted">Images wait here before upload</td></tr>
                <tr><td class="text-nowrap pe-2"><code>uploaded_dir</code></td><td class="text-muted">Images moved here after successful upload</td></tr>
                <tr><td class="text-nowrap pe-2"><code>max_retries</code></td><td class="text-muted">Upload attempts before giving up on a file</td></tr>
                <tr><td class="text-nowrap pe-2"><code>retry_delay</code></td><td class="text-muted">Seconds between upload retries</td></tr>
                <tr><td class="text-nowrap pe-2"><code>poll_interval</code></td><td class="text-muted">Seconds between checks for new images</td></tr>
              </tbody>
            </table>

            <p class="fw-semibold mb-1 text-uppercase" style="font-size:0.7rem;letter-spacing:.05em">GSM</p>
            <table class="table table-sm table-borderless mb-3" style="font-size:0.82rem">
              <tbody>
                <tr><td class="text-nowrap pe-2"><code>gsm_device</code></td><td class="text-muted">Serial port for the GSM modem</td></tr>
                <tr><td class="text-nowrap pe-2"><code>gsm_pin</code></td><td class="text-muted">SIM PIN — omit if not required</td></tr>
                <tr><td class="text-nowrap pe-2"><code>gsm_number</code></td><td class="text-muted">Phone number of the SIM — for reference when sending SMS config commands</td></tr>
              </tbody>
            </table>

            <p class="fw-semibold mb-1 text-uppercase" style="font-size:0.7rem;letter-spacing:.05em">Recording</p>
            <table class="table table-sm table-borderless mb-3" style="font-size:0.82rem">
              <tbody>
                <tr><td class="text-nowrap pe-2"><code>mode</code></td><td class="text-muted"><code>image_motion</code> capture on movement · <code>image_interval</code> capture every N seconds. Output is a fused RGBA PNG when thermal fusion is on, a JPEG when it's off</td></tr>
                <tr><td class="text-nowrap pe-2"><code>camera_id</code></td><td class="text-muted">Camera index (0 = default) — only used by the <code>rpicam</code> backend</td></tr>
                <tr><td class="text-nowrap pe-2"><code>width</code> / <code>height</code></td><td class="text-muted">Capture resolution in pixels</td></tr>
                <tr><td class="text-nowrap pe-2"><code>min_size_bytes</code></td><td class="text-muted">Minimum file size to treat an image as valid — only used by the <code>rpicam</code> backend</td></tr>
                <tr><td class="text-nowrap pe-2"><code>capture_backend</code></td><td class="text-muted"><code>picamera2</code> (default) keeps the camera streaming with a pre-trigger buffer, so a triggered capture is served from the instant motion was seen · <code>rpicam</code> runs a fresh rpicam-still per capture, which costs ~1s of camera init before every exposure. Falls back to <code>rpicam</code> automatically if picamera2 can't start</td></tr>
                <tr><td class="text-nowrap pe-2"><code>camera_fps</code></td><td class="text-muted">Stream frame rate for the <code>picamera2</code> backend (default 15). Higher = finer choice of frame to pair with thermal, more CPU and memory</td></tr>
                <tr><td class="text-nowrap pe-2"><code>camera_buffer_s</code></td><td class="text-muted">Seconds of full-res frames held in the pre-trigger buffer (default 1.5). Must exceed how long after motion onset detection fires, or the onset frame is gone. ~3MB per frame, so 1.5s at 15fps ≈ 68MB</td></tr>
                <tr><td class="text-nowrap pe-2"><code>camera_stall_warn_s</code></td><td class="text-muted">Log a warning if the camera stream produces no frames for this long (default 5.0)</td></tr>
                <tr><td class="text-nowrap pe-2"><code>rpicam_still_path</code></td><td class="text-muted">Path to rpicam-still binary — only used by the <code>rpicam</code> backend</td></tr>
              </tbody>
            </table>

            <p class="fw-semibold mb-1 text-uppercase" style="font-size:0.7rem;letter-spacing:.05em">Motion Detection</p>
            <table class="table table-sm table-borderless" style="font-size:0.82rem">
              <tbody>
                <tr><td class="text-nowrap pe-2"><code>motion_threshold</code></td><td class="text-muted">Fraction of pixels that must change to trigger (0–1, default 0.02)</td></tr>
                <tr><td class="text-nowrap pe-2"><code>detection_interval</code></td><td class="text-muted">Seconds between motion checks. On the <code>picamera2</code> backend a check costs ~0.06s (the frame is already buffered) rather than ~0.6s, so this can be much shorter than it used to be — but lowering it changes sensitivity, since less of the scene changes between closer-spaced frames. Keep it below <code>camera_buffer_s</code></td></tr>
                <tr><td class="text-nowrap pe-2"><code>detection_width</code> / <code>detection_height</code></td><td class="text-muted">Resolution of detection frames — low-res for speed. On the <code>picamera2</code> backend this is the size of the lores stream</td></tr>
                <tr><td class="text-nowrap pe-2"><code>baseline_frames</code></td><td class="text-muted">Frames averaged into the motion baseline (default 3). Rebuilt after every trigger and the recorder is blind while it happens, so this trades shake-suppression against missed motion</td></tr>
                <tr><td class="text-nowrap pe-2"><code>temporal_alpha</code></td><td class="text-muted">How fast background average updates (0–1). Lower = more shake-resistant, higher = reacts faster</td></tr>
                <tr><td class="text-nowrap pe-2"><code>motion_cooldown</code></td><td class="text-muted">Seconds to wait after a motion capture before checking again (0 = no cooldown)</td></tr>
                <tr><td class="text-nowrap pe-2"><code>motion_debug</code></td><td class="text-muted">Log motion ratio on every detection check — useful for tuning motion_threshold (true/false)</td></tr>
              </tbody>
            </table>

            <p class="fw-semibold mb-1 text-uppercase" style="font-size:0.7rem;letter-spacing:.05em">Image Capture</p>
            <table class="table table-sm table-borderless" style="font-size:0.82rem">
              <tbody>
                <tr><td class="text-nowrap pe-2"><code>image_interval</code></td><td class="text-muted">Seconds between captures in <code>image_interval</code> mode</td></tr>
                <tr><td class="text-nowrap pe-2"><code>image_quality</code></td><td class="text-muted">JPEG quality (1–100, default 85)</td></tr>
                <tr><td class="text-nowrap pe-2"><code>image_rotation</code></td><td class="text-muted">Software rotation in degrees clockwise (0/90/180/270) to correct how the CSI camera is physically mounted — corrects orientation without mirroring left/right</td></tr>
              </tbody>
            </table>

            <p class="fw-semibold mb-1 text-uppercase" style="font-size:0.7rem;letter-spacing:.05em">Thermal Fusion</p>
            <table class="table table-sm table-borderless" style="font-size:0.82rem">
              <tbody>
                <tr><td class="text-nowrap pe-2"><code>thermal_enabled</code></td><td class="text-muted">Stream the GPIO thermal sensor and fuse it into captures as an alpha channel (true/false)</td></tr>
                <tr><td class="text-nowrap pe-2"><code>thermal_fps</code></td><td class="text-muted">Thermal sensor frame rate (1–25, default 9)</td></tr>
                <tr><td class="text-nowrap pe-2"><code>thermal_filters</code></td><td class="text-muted">Enable on-chip spatial filters for a cleaner thermal image (true/false)</td></tr>
                <tr><td class="text-nowrap pe-2"><code>thermal_offset</code></td><td class="text-muted">Global temperature offset correction in °C (default 0.0)</td></tr>
                <tr><td class="text-nowrap pe-2"><code>thermal_warmup_frames</code></td><td class="text-muted">Frames discarded after sensor start before it's considered ready</td></tr>
                <tr><td class="text-nowrap pe-2"><code>thermal_width</code> / <code>thermal_height</code></td><td class="text-muted">Thermal frame size before it's resized to match the visible capture</td></tr>
                <tr><td class="text-nowrap pe-2"><code>thermal_spi_speed_hz</code></td><td class="text-muted">SPI clock speed to the thermal sensor (default 2,000,000 = 2 MHz) — lower if the log shows frequent CRC errors</td></tr>
                <tr><td class="text-nowrap pe-2"><code>thermal_hflip</code> / <code>thermal_vflip</code></td><td class="text-muted">Flip the thermal frame so it agrees with the (correctly rotated) visible frame on left/right and up/down — set based on a real test, not guessed</td></tr>
              </tbody>
            </table>

          </div>
                <tr><td class="text-nowrap pe-2"><code>thermal_max_skew_s</code></td><td class="text-muted">Maximum time gap allowed between the visible exposure and the thermal frame fused with it. A capture that can't be paired this closely is <strong>discarded</strong>, not saved unpaired — a mistimed pair is wrong data, not degraded data, once the subject moves. Defaults to <code>0.75 / thermal_fps</code> (83ms at 9fps). The floor is half a thermal frame interval (56ms at 9fps), since no thermal data exists between frames — asking for less just discards captures. Raise <code>thermal_fps</code> to tighten it</td></tr>
                <tr><td class="text-nowrap pe-2"><code>thermal_stall_warn_s</code></td><td class="text-muted">Log a warning if the thermal sensor produces no frames for this long (default 5.0). Without this a stalled sensor is invisible — the last good frame would otherwise keep being fused into fresh captures</td></tr>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- Service modal -->
<div class="modal fade" id="svcModal" tabindex="-1">
  <div class="modal-dialog modal-sm">
    <div class="modal-content">
      <div class="modal-header py-2">
        <h6 class="modal-title" id="svcModalTitle"></h6>
        <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
      </div>
      <div class="modal-body">
        <div class="d-grid gap-2">
          <button class="btn btn-success" onclick="doService('start')"><i class="bi bi-play-fill"></i> Start</button>
          <button class="btn btn-warning" onclick="doService('restart')"><i class="bi bi-arrow-repeat"></i> Restart</button>
          <button class="btn btn-danger" onclick="doService('stop')"><i class="bi bi-stop-fill"></i> Stop</button>
        </div>
        <div id="svc-res" class="mt-3 small font-monospace d-none"></div>
      </div>
    </div>
  </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/js/bootstrap.bundle.min.js"></script>
<script>
// ── Tab switching ────────────────────────────────────────────────────────────
document.querySelectorAll('[data-tab]').forEach(el => {
  el.addEventListener('click', e => {
    e.preventDefault();
    document.querySelectorAll('[data-tab]').forEach(t => t.classList.remove('active'));
    el.classList.add('active');
    document.querySelectorAll('[id^="tab-"]').forEach(t => t.style.display = 'none');
    document.getElementById('tab-' + el.dataset.tab).style.display = '';
    if (el.dataset.tab === 'logs') fetchLogs();
    if (el.dataset.tab === 'cfg')  fetchCfg();
  });
});

// ── Status ───────────────────────────────────────────────────────────────────
function bars(sig) {
  if (sig === null || sig === undefined) return '<span class="text-muted">N/A</span>';
  const pct = sig / 31;
  const hs  = [8, 12, 16, 20, 24];
  const ts  = [0, .2, .4, .6, .8];
  let out = '';
  for (let i = 0; i < 5; i++)
    out += `<span class="bar ${pct > ts[i] ? 'on' : ''}" style="height:${hs[i]}px"></span>`;
  const lbl = sig === 99 ? 'Unknown' : `${sig}/31`;
  return `${out} <span class="text-muted small ms-1">${lbl}</span>`;
}

function badge(s) {
  return `<span class="status-badge s-${s}">${s}</span>`;
}

function fetchStatus() {
  fetch('/api/status').then(r => r.json()).then(d => {
    let html = '';
    for (const [name, status] of Object.entries(d.services)) {
      html += `<div class="d-flex align-items-center justify-content-between mb-2">
        <span class="fw-medium text-capitalize">${name}</span>
        <div class="d-flex align-items-center gap-2">
          ${badge(status)}
          <button class="btn btn-sm btn-outline-secondary py-0 px-2" onclick="openSvc('${name}')">
            <i class="bi bi-three-dots-vertical"></i>
          </button>
        </div>
      </div>`;
    }
    document.getElementById('svcs').innerHTML = html;

    document.getElementById('gsm').innerHTML =
      `<div class="mb-1">Signal &nbsp;${bars(d.gsm.signal)}</div>
       <div class="text-muted small mt-1">Device: <code>${d.gsm.device}</code></div>` +
      (d.gsm.number ? `<div class="text-muted small">SIM number: <code>${d.gsm.number}</code></div>` : '');

    document.getElementById('outbox').innerHTML =
      `<span class="fs-3 fw-bold">${d.outbox.count}</span>
       <span class="text-muted ms-1">files</span>
       <div class="text-muted small">${d.outbox.size}</div>`;

    document.getElementById('uploaded').innerHTML =
      `<span class="fs-3 fw-bold">${d.uploaded.count}</span>
       <span class="text-muted ms-1">files</span>
       <div class="text-muted small">${d.uploaded.size}</div>`;

    // SSH / remote access
    const ip = d.ssh && d.ssh.tailscale_ip;
    document.getElementById('ssh-info').innerHTML = ip
      ? `<p class="mb-1 small text-muted">SSH into the Pi from any device on your Tailnet:</p>
         <code>ssh root@${ip}</code>
         <p class="mt-2 mb-1 small text-muted">Or open this dashboard from another device:</p>
         <code>http://${ip}:8080</code>`
      : `<span class="text-muted small">Tailscale not connected — run <code>sudo tailscale up</code> on the Pi.</span>`;

    document.getElementById('ts').textContent = 'Updated ' + new Date().toLocaleTimeString();
  }).catch(() => {});
}

// ── Logs ─────────────────────────────────────────────────────────────────────
function fetchLogs() {
  const n = document.getElementById('log-n').value;
  fetch('/api/logs?lines=' + n).then(r => r.json()).then(d => {
    const el = document.getElementById('log-out');
    el.textContent = d.lines.join('\n') || '(no log entries)';
    el.scrollTop = el.scrollHeight;
  }).catch(() => {});
}
document.getElementById('log-n').addEventListener('change', fetchLogs);
setInterval(() => {
  if (document.getElementById('log-auto').checked &&
      document.getElementById('tab-logs').style.display !== 'none') fetchLogs();
}, 5000);

// ── Config ───────────────────────────────────────────────────────────────────
function fetchCfg() {
  fetch('/api/config').then(r => r.json()).then(d => {
    document.getElementById('cfg').value = JSON.stringify(d, null, 2);
  }).catch(() => {});
}
function saveCfg() {
  const alertEl = document.getElementById('cfg-alert');
  let data;
  try { data = JSON.parse(document.getElementById('cfg').value); }
  catch(e) {
    alertEl.className = 'alert alert-danger';
    alertEl.textContent = 'Invalid JSON: ' + e.message;
    alertEl.classList.remove('d-none');
    return;
  }
  fetch('/api/config', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(data),
  }).then(r => r.json()).then(d => {
    alertEl.className = 'alert ' + (d.ok ? 'alert-success' : 'alert-danger');
    alertEl.textContent = d.ok
      ? 'Saved. Restart services to apply changes.'
      : 'Error: ' + (d.error || 'unknown');
    alertEl.classList.remove('d-none');
    setTimeout(() => alertEl.classList.add('d-none'), 5000);
  });
}

// ── Service control ──────────────────────────────────────────────────────────
let curSvc = null, svcModal = null;
function openSvc(name) {
  curSvc = name;
  document.getElementById('svcModalTitle').textContent = name;
  document.getElementById('svc-res').classList.add('d-none');
  if (!svcModal) svcModal = new bootstrap.Modal(document.getElementById('svcModal'));
  svcModal.show();
}
function doService(action) {
  if (!curSvc) return;
  const el = document.getElementById('svc-res');
  el.className = 'mt-3 small font-monospace text-muted';
  el.textContent = action + '…';
  el.classList.remove('d-none');
  fetch('/api/service/' + curSvc + '/' + action, {method: 'POST'})
    .then(r => r.json())
    .then(d => {
      el.className = 'mt-3 small font-monospace ' + (d.ok ? 'text-success' : 'text-danger');
      el.textContent = d.ok ? '✓ ' + action + ' OK' : '✗ ' + (d.output || d.error || 'failed');
      setTimeout(fetchStatus, 1500);
    }).catch(e => {
      el.className = 'mt-3 small font-monospace text-danger';
      el.textContent = '✗ ' + e.message;
    });
}

function clearOutbox() {
  if (!confirm('Delete all files in the outbox queue?')) return;
  const el = document.getElementById('outbox-clear-res');
  el.className = 'small mt-2 text-muted';
  el.textContent = 'Clearing…';
  el.classList.remove('d-none');
  fetch('/api/outbox/clear', {method: 'POST'})
    .then(r => r.json())
    .then(d => {
      el.className = 'small mt-2 ' + (d.ok ? 'text-success' : 'text-danger');
      el.textContent = d.ok ? `✓ Deleted ${d.deleted} file(s)` : '✗ ' + (d.error || 'failed');
      setTimeout(fetchStatus, 1000);
    }).catch(e => {
      el.className = 'small mt-2 text-danger';
      el.textContent = '✗ ' + e.message;
    });
}

// ── Init ─────────────────────────────────────────────────────────────────────
fetchStatus();
setInterval(fetchStatus, 15000);
</script>
</body>
</html>
"""

@app.route('/')
@require_auth
def index():
    return HTML, 200, {'Content-Type': 'text/html; charset=utf-8'}


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=WEBUI_PORT, debug=False, threaded=True)
