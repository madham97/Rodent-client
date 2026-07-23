"""
Capture one thermal frame and save as a false-colour PNG.

Usage:
    python3 pi-client/thermal/snapshot.py [output.png]

Must be run from a writable directory (e.g. /home/madham), not /opt,
because gpiozero's lgpio backend creates a socket file in the CWD.
"""
import sys
import logging
from thermal_common import make_mi48, read_frame, frame_to_image

logging.disable(logging.WARNING)

import cv2 as cv
from senxor.utils import data_to_frame

OUT = sys.argv[1] if len(sys.argv) > 1 else "/opt/Rodent-client/thermal.png"
WARMUP_FRAMES = 5

mi48, cs = make_mi48(fps=9, spi_speed_hz=2_000_000)
mi48.start(stream=True, with_header=True)

print(f"Warming up ({WARMUP_FRAMES} frames)...")
for _ in range(WARMUP_FRAMES):
    read_frame(mi48, cs)

data, _ = read_frame(mi48, cs)
mi48.stop(stop_timeout=0.5)

frame = data_to_frame(data, mi48.fpa_shape)
print(f"Temp range: {frame.min():.1f}°C – {frame.max():.1f}°C  (avg {frame.mean():.1f}°C)")

img = frame_to_image(data, mi48.fpa_shape)
cv.imwrite(OUT, img)
print(f"Saved: {OUT}")
