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


def _record_rgb(rpicam_vid_path, camera_id, width, height, fps, duration, out_path, errors):
    """Record the RGB feed with rpicam-vid to an MJPEG file. Runs in a helper thread."""
    cmd = [
        rpicam_vid_path,
        '--camera',    str(camera_id),
        '--width',     str(width),
        '--height',    str(height),
        '--framerate', str(fps),
        '--timeout',   str(int(duration * 1000)),
        '--codec',     'mjpeg',
        '--nopreview',
        '--output',    str(out_path),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=duration + 15)
        if r.returncode != 0:
            errors.append(f"rpicam-vid failed (code {r.returncode}): {r.stderr.decode(errors='ignore').strip()}")
    except FileNotFoundError:
        errors.append(f"rpicam-vid not found at '{rpicam_vid_path}'")
    except Exception as e:
        errors.append(f"rpicam-vid error: {e}")


def _record_thermal(mi48, cs, duration, frames_out, errors):
    """Read thermal frames for `duration` seconds, tagging each with a wall-clock timestamp.
    Runs in a helper thread."""
    end = time.time() + duration
    while time.time() < end:
        try:
            data, _ = read_frame(mi48, cs, timeout=1)
            if data is not None:
                frames_out.append((time.time(), data))
        except Exception as e:
            errors.append(f"thermal read error: {e}")


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

    thermal_frames = []
    errors = []
    t_rgb = threading.Thread(
        target=_record_rgb,
        args=(args.rpicam_vid_path, args.camera_id, capture_width, capture_height,
              args.fps, args.duration, rgb_path, errors),
    )
    t_thermal = threading.Thread(
        target=_record_thermal,
        args=(mi48, cs, args.duration, thermal_frames, errors),
    )

    print(f"Recording {args.duration}s from both cameras simultaneously...")
    start = time.time()
    t_rgb.start()
    t_thermal.start()
    t_rgb.join()
    t_thermal.join()
    elapsed = time.time() - start
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
    # default (25 fps) rather than reflecting the true ~8-9 fps capture rate — trusting it
    # desyncs the two feeds (RGB reads as if it spans a few seconds when it actually spans
    # the full recording). Deriving fps from the measured wall-clock duration instead keeps
    # both feeds aligned to the same real timeline.
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

    rgb_fps = len(rgb_frames) / elapsed if elapsed > 0 else args.fps
    print(f"RGB: {len(rgb_frames)} frames over {elapsed:.2f}s -> {rgb_fps:.2f} fps (measured, not container metadata)")

    writer = cv.VideoWriter(
        args.output,
        cv.VideoWriter_fourcc(*'mp4v'),
        rgb_fps,
        (args.width + thermal_w, args.height),
    )

    fpa_shape = mi48.fpa_shape
    count = 0
    for frame_idx, rgb_frame in enumerate(rgb_frames):
        if args.rgb_rotate:
            rgb_frame = cv.rotate(rgb_frame, _CV_ROTATE[args.rgb_rotate])
        if rgb_frame.shape[1::-1] != (args.width, args.height):
            rgb_frame = cv.resize(rgb_frame, (args.width, args.height))

        # Pair this RGB frame with the nearest-in-time thermal frame captured during recording.
        target_ts = start + frame_idx / rgb_fps
        _, data = min(thermal_frames, key=lambda tf: abs(tf[0] - target_ts))
        thermal_img = frame_to_image(data, fpa_shape, out_size=(thermal_w, args.height),
                                      hflip=args.thermal_hflip, vflip=args.thermal_vflip)

        writer.write(cv.hconcat([rgb_frame, thermal_img]))
        count += 1

    writer.release()
    shutil.rmtree(tmp_dir, ignore_errors=True)
    print(f"Wrote {count} combined frames ({args.width + thermal_w}x{args.height}) -> {args.output}")


if __name__ == '__main__':
    main()
