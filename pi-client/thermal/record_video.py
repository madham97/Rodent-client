"""
Record a thermal video and save as MP4.

Usage:
    python3 pi-client/thermal/record_video.py [--duration 10] [--fps 9] [output.mp4]

Must be run from a writable directory (e.g. /home/madham), not /opt,
because gpiozero's lgpio backend creates a socket file in the CWD.
"""
import sys
import argparse
import logging
import time

logging.disable(logging.WARNING)

import cv2 as cv
from thermal_common import make_mi48, read_frame, frame_to_image

def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument('output',          nargs='?', default='/opt/Rodent-client/thermal.mp4')
    p.add_argument('--duration', '-d', type=float, default=10,  help='seconds to record')
    p.add_argument('--fps',      '-f', type=int,   default=9,   help='sensor frame rate (1-25)')
    p.add_argument('--width',         type=int,   default=480)
    p.add_argument('--height',        type=int,   default=372)
    p.add_argument('--spi-speed',     type=int,   default=2_000_000,
                    help='thermal SPI clock speed in Hz — lower if you see CRC errors (default 2 MHz)')
    return p.parse_args()

args = parse_args()
WARMUP_FRAMES = 5

mi48, cs = make_mi48(fps=args.fps, spi_speed_hz=args.spi_speed)
mi48.start(stream=True, with_header=True)

writer = cv.VideoWriter(
    args.output,
    cv.VideoWriter_fourcc(*'mp4v'),
    args.fps,
    (args.width, args.height),
)

print(f"Warming up ({WARMUP_FRAMES} frames)...")
for _ in range(WARMUP_FRAMES):
    read_frame(mi48, cs)

print(f"Recording {args.duration}s at {args.fps} FPS → {args.output}")
end   = time.time() + args.duration
count = 0
while time.time() < end:
    data, _ = read_frame(mi48, cs)
    if data is not None:
        writer.write(frame_to_image(data, mi48.fpa_shape,
                                    out_size=(args.width, args.height)))
        count += 1
        remaining = max(0, end - time.time())
        print(f"  {count} frames  ({remaining:.1f}s remaining)  ", end='\r')

writer.release()
mi48.stop(stop_timeout=0.5)
print(f"\nDone — {count} frames saved to {args.output}")
