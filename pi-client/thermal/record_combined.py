"""
Record from both cameras simultaneously — the CSI ribbon RGB/IR camera and the GPIO
thermal camera — and write a single side-by-side MP4 (RGB left, thermal right).

The two cameras run on independent hardware (CSI vs I2C/SPI), so they're recorded in
parallel threads for the same time window: rpicam-vid streams the RGB feed to an MJPEG
file while a thread continuously reads timestamped thermal frames. Once both finish, each
RGB frame is paired with its nearest-in-time thermal frame and the two are concatenated
side by side into the output video.

Usage:
    python3 pi-client/thermal/record_combined.py [--duration 10] [--fps 9] [output.mp4]

Must be run from a writable directory (e.g. /home/madham), not /opt, because
gpiozero's lgpio backend creates a socket file in the CWD.
"""
import argparse
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import logging
from pathlib import Path

logging.disable(logging.WARNING)

import cv2 as cv
from thermal_common import make_mi48, read_frame, frame_to_image


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument('output', nargs='?', default='/opt/Rodent-client/combined.mp4')
    p.add_argument('--duration', '-d', type=float, default=10, help='seconds to record')
    p.add_argument('--fps', '-f', type=int, default=9, help='thermal sensor + output frame rate (1-25)')
    p.add_argument('--camera-id', type=int, default=0, help='CSI camera index')
    p.add_argument('--width', type=int, default=640, help='RGB capture width')
    p.add_argument('--height', type=int, default=480, help='RGB capture height')
    p.add_argument('--thermal-width', type=int, default=None,
                    help='thermal pane width in the output video (defaults to --height, i.e. a square pane)')
    p.add_argument('--rpicam-vid-path', default='rpicam-vid')
    p.add_argument('--spi-speed', type=int, default=2_000_000,
                    help='thermal SPI clock speed in Hz — lower if you see CRC errors (default 2 MHz)')
    p.add_argument('--rgb-rotate', type=int, default=180, choices=(0, 90, 180, 270),
                    help='clockwise rotation to apply to the RGB frame in software, correcting for how the '
                         'CSI ribbon camera is physically mounted (default 180 — rpicam-vid/still only support '
                         '0/180 in hardware, so anything else needs a software rotation here). A rotation, '
                         'unlike a flip, preserves left/right handedness, so getting this right is what keeps '
                         'a raised hand reading as the same hand in both panes.')
    p.add_argument('--thermal-hflip', action=argparse.BooleanOptionalAction, default=True,
                    help='flip the thermal frame horizontally — the MI48 sensor readout on this unit comes out '
                         'mirrored relative to the (correctly rotated) RGB frame; default on. Use '
                         '--no-thermal-hflip to disable if your unit doesn\'t need it.')
    p.add_argument('--thermal-vflip', action=argparse.BooleanOptionalAction, default=False,
                    help='flip the thermal frame vertically, if needed')
    return p.parse_args()


_CV_ROTATE = {90: cv.ROTATE_90_CLOCKWISE, 180: cv.ROTATE_180, 270: cv.ROTATE_90_COUNTERCLOCKWISE}


def _record_rgb(rpicam_vid_path, camera_id, width, height, fps, duration, out_path, pts_path,
                 result, errors):
    """Record the RGB feed with rpicam-vid to an MJPEG file. Runs in a helper thread.

    Writes a --save-pts timestamp file alongside the video and records the wall-clock instant
    rpicam-vid exited into `result['exit_ts']`. Together those give every RGB frame a real
    wall-clock time, which is what keeps the two feeds in sync — see `_rgb_frame_timestamps`.
    """
    cmd = [
        rpicam_vid_path,
        '--camera',    str(camera_id),
        '--width',     str(width),
        '--height',    str(height),
        '--framerate', str(fps),
        '--timeout',   str(int(duration * 1000)),
        '--codec',     'mjpeg',
        '--nopreview',
        '--save-pts',  str(pts_path),
        '--output',    str(out_path),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=duration + 30)
        result['exit_ts'] = time.time()
        if r.returncode != 0:
            errors.append(f"rpicam-vid failed (code {r.returncode}): {r.stderr.decode(errors='ignore').strip()}")
    except FileNotFoundError:
        errors.append(f"rpicam-vid not found at '{rpicam_vid_path}'")
    except Exception as e:
        result.setdefault('exit_ts', time.time())
        errors.append(f"rpicam-vid error: {e}")


def _record_thermal(mi48, cs, stop_event, max_duration, frames_out, errors):
    """Read thermal frames until `stop_event` is set, tagging each with a wall-clock timestamp.
    Runs in a helper thread.

    Deliberately driven by the event rather than a fixed duration: rpicam-vid spends a second
    or more initialising the camera before its first frame, so the RGB feed's real time window
    ends well after `start + duration`. A fixed-duration thermal capture would leave that tail
    with no thermal frames to pair against, and every RGB frame there would fall back to the
    same stale final thermal frame. `max_duration` is only a runaway backstop.
    """
    end = time.time() + max_duration
    while not stop_event.is_set() and time.time() < end:
        try:
            data, _ = read_frame(mi48, cs, timeout=1)
            if data is not None:
                frames_out.append((time.time(), data))
        except Exception as e:
            errors.append(f"thermal read error: {e}")


def _rgb_frame_timestamps(pts_path, n_frames, exit_ts, fallback_fps):
    """Return a wall-clock timestamp for each of the `n_frames` decoded RGB frames.

    rpicam-vid's --save-pts file gives per-frame presentation timestamps in milliseconds, which
    are accurate *relative* to each other but carry no absolute epoch. The anchor comes from the
    other end of the recording: measured on this hardware, rpicam-vid writes its final frame and
    exits in the same instant (last file growth and process exit coincide to within 10ms), while
    startup latency before the first frame is large and variable (1.6s idle, ~5s when another
    process just released the camera). So anchoring the LAST frame to process exit is reliable
    where anchoring the first frame to launch time is not.

    Falls back to evenly spaced timestamps ending at `exit_ts` if the pts file is missing or
    unusable, which keeps the tail aligned even without per-frame timing.
    """
    pts = []
    try:
        for line in Path(pts_path).read_text().splitlines():
            line = line.strip()
            if line and not line.startswith('#'):
                pts.append(float(line) / 1000.0)
    except Exception:
        pts = []

    if len(pts) < n_frames:
        # Fewer timestamps than decoded frames (truncated pts file, or no pts at all): space the
        # frames evenly at the nominal rate, still ending at exit.
        return [exit_ts - (n_frames - 1 - i) / fallback_fps for i in range(n_frames)]

    pts = pts[:n_frames]
    last = pts[-1]
    return [exit_ts - (last - p) for p in pts]


def main():
    args = parse_args()
    thermal_w = args.thermal_width or args.height

    # If we're correcting a 90/270 mounting rotation in software, ask the sensor to capture
    # at swapped dimensions so the rotated result comes out at the requested --width/--height
    # without any extra resize/distortion.
    if args.rgb_rotate in (90, 270):
        capture_width, capture_height = args.height, args.width
    else:
        capture_width, capture_height = args.width, args.height

    print("Initializing thermal sensor...")
    mi48, cs = make_mi48(fps=args.fps, spi_speed_hz=args.spi_speed)
    mi48.start(stream=True, with_header=True)

    print("Warming up thermal sensor...")
    for _ in range(5):
        read_frame(mi48, cs)

    tmp_dir = tempfile.mkdtemp(prefix='combined_rec_')
    rgb_path = Path(tmp_dir) / 'rgb.mjpeg'
    pts_path = Path(tmp_dir) / 'rgb.pts'

    thermal_frames = []
    errors = []
    rgb_result = {}
    rgb_done = threading.Event()
    t_rgb = threading.Thread(
        target=_record_rgb,
        args=(args.rpicam_vid_path, args.camera_id, capture_width, capture_height,
              args.fps, args.duration, rgb_path, pts_path, rgb_result, errors),
    )
    t_thermal = threading.Thread(
        target=_record_thermal,
        args=(mi48, cs, rgb_done, args.duration + 60, thermal_frames, errors),
    )

    print(f"Recording {args.duration}s from both cameras simultaneously...")
    start = time.time()
    t_rgb.start()
    t_thermal.start()
    t_rgb.join()
    rgb_done.set()      # stop thermal only once the RGB feed is actually finished
    t_thermal.join()
    elapsed = time.time() - start
    rgb_exit_ts = rgb_result.get('exit_ts', time.time())
    print(f"Capture done in {elapsed:.1f}s — {len(thermal_frames)} thermal frames")

    mi48.stop(stop_timeout=0.5)

    for e in errors:
        print(f"WARNING: {e}", file=sys.stderr)

    if not rgb_path.exists() or rgb_path.stat().st_size == 0:
        print("ERROR: no RGB video was captured — check the CSI ribbon camera / rpicam-vid", file=sys.stderr)
        shutil.rmtree(tmp_dir, ignore_errors=True)
        sys.exit(1)
    if not thermal_frames:
        print("ERROR: no thermal frames were captured — check the GPIO thermal camera wiring", file=sys.stderr)
        shutil.rmtree(tmp_dir, ignore_errors=True)
        sys.exit(1)

    # Read every RGB frame up front so we know the real frame count. rpicam-vid's raw MJPEG
    # output has no embedded frame-rate metadata, so cv.CAP_PROP_FPS falls back to a bogus
    # default (25 fps) rather than reflecting the true ~8-9 fps capture rate. The per-frame
    # timing comes from the --save-pts sidecar instead (see `_rgb_frame_timestamps`) — note
    # that the total wall-clock duration is NOT a usable substitute, because it includes
    # rpicam-vid's variable multi-second camera-init latency during which no frames exist.
    cap = cv.VideoCapture(str(rgb_path))
    rgb_frames = []
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        rgb_frames.append(frame)
    cap.release()

    if not rgb_frames:
        print("ERROR: RGB video contained no decodable frames", file=sys.stderr)
        shutil.rmtree(tmp_dir, ignore_errors=True)
        sys.exit(1)

    rgb_ts = _rgb_frame_timestamps(pts_path, len(rgb_frames), rgb_exit_ts, args.fps)
    rgb_span = rgb_ts[-1] - rgb_ts[0]
    rgb_fps = (len(rgb_frames) - 1) / rgb_span if rgb_span > 0 else args.fps
    print(f"RGB: {len(rgb_frames)} frames over {rgb_span:.2f}s -> {rgb_fps:.2f} fps "
          f"(from --save-pts; {rgb_ts[0] - start:.2f}s camera-init latency before first frame)")

    writer = cv.VideoWriter(
        args.output,
        cv.VideoWriter_fourcc(*'mp4v'),
        rgb_fps,
        (args.width + thermal_w, args.height),
    )

    fpa_shape = mi48.fpa_shape
    count = 0
    worst_skew = 0.0
    for frame_idx, rgb_frame in enumerate(rgb_frames):
        if args.rgb_rotate:
            rgb_frame = cv.rotate(rgb_frame, _CV_ROTATE[args.rgb_rotate])
        if rgb_frame.shape[1::-1] != (args.width, args.height):
            rgb_frame = cv.resize(rgb_frame, (args.width, args.height))

        # Pair this RGB frame with the nearest-in-time thermal frame, both on the same wall-clock
        # timeline: the RGB side from --save-pts anchored to rpicam-vid's exit, the thermal side
        # stamped as each frame was read.
        target_ts = rgb_ts[frame_idx]
        ts, data = min(thermal_frames, key=lambda tf: abs(tf[0] - target_ts))
        worst_skew = max(worst_skew, abs(ts - target_ts))
        thermal_img = frame_to_image(data, fpa_shape, out_size=(thermal_w, args.height),
                                      hflip=args.thermal_hflip, vflip=args.thermal_vflip)

        writer.write(cv.hconcat([rgb_frame, thermal_img]))
        count += 1

    writer.release()
    shutil.rmtree(tmp_dir, ignore_errors=True)
    print(f"Wrote {count} combined frames ({args.width + thermal_w}x{args.height}) -> {args.output}")
    print(f"Worst RGB/thermal pairing skew: {worst_skew * 1000:.0f}ms")
    if worst_skew > 1.0:
        print("WARNING: some frames are paired more than 1s apart — the thermal feed may have "
              "stalled mid-recording (check for CRC errors above)", file=sys.stderr)


if __name__ == '__main__':
    main()
