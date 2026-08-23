#!/usr/bin/env python3
"""
Offline tests for thermal frame geometry. No sensor, no cv2 hardware path.

These exist because a frame that is the wrong shape still uploads, still aligns, and still looks
plausible in a viewer — the alignment homography rescales each axis independently, so an
anisotropic squash at capture time is cancelled by the warp and leaves the *aligned* output
correct while the stored native frame is wrong. Nothing downstream complains. The only place it
can be caught is here.

Usage:
    python3 test_thermal_geometry.py
"""

import sys
import types
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent / 'thermal'))

# The MI48 reports fpa_shape as (width, height) = (80, 62), while the decoded frame is a normal
# (rows, cols) = (62, 80) array. That mismatch is the trap; stub both faithfully.
SENSOR_W, SENSOR_H = 80, 62
FPA_SHAPE = (SENSOR_W, SENSOR_H)

# thermal_common imports the Pi's I2C/SPI/GPIO stack at module scope, so stub the hardware
# the same way test_uploader_recovery.py stubs pyserial — this must run on a dev machine.
for name, attrs in (('smbus', ('SMBus',)), ('spidev', ('SpiDev',)),
                    ('gpiozero', ('DigitalInputDevice', 'DigitalOutputDevice'))):
    if name not in sys.modules:
        mod = types.ModuleType(name)
        for a in attrs:
            setattr(mod, a, type(a, (), {'__init__': lambda self, *a, **k: None}))
        sys.modules[name] = mod

if 'senxor' not in sys.modules:
    senxor = types.ModuleType('senxor')
    utils = types.ModuleType('senxor.utils')
    mi48_mod = types.ModuleType('senxor.mi48')
    ifaces = types.ModuleType('senxor.interfaces')
    mi48_mod.MI48 = type('MI48', (), {'__init__': lambda self, *a, **k: None})
    ifaces.SPI_Interface = type('SPI_Interface', (), {'__init__': lambda self, *a, **k: None})
    ifaces.I2C_Interface = type('I2C_Interface', (), {'__init__': lambda self, *a, **k: None})
    senxor.mi48 = mi48_mod
    senxor.interfaces = ifaces
    sys.modules['senxor.mi48'] = mi48_mod
    sys.modules['senxor.interfaces'] = ifaces
    utils.data_to_frame = lambda data, fpa_shape: np.asarray(data, dtype='float32').reshape(SENSOR_H, SENSOR_W)
    utils.cv_filter = lambda img, parameters=None, **kw: img
    senxor.utils = utils
    sys.modules['senxor'] = senxor
    sys.modules['senxor.utils'] = utils

from thermal_common import frame_to_gray8

_results = []


def check(name, cond):
    _results.append((name, bool(cond)))
    print(f"{'PASS' if cond else 'FAIL'}  {name}")


# A frame with a distinct aspect signature: a bar across the full width, so a transpose or a
# squash changes its proportions measurably rather than just its numbers.
data = np.full(SENSOR_H * SENSOR_W, 20.0, dtype='float32')
frame = data.reshape(SENSOR_H, SENSOR_W)
frame[10:20, :] = 40.0
data = frame.ravel()

img, stats = frame_to_gray8(data, FPA_SHAPE, out_size=None)
h, w = img.shape
check('native output is the sensor grid, not its transpose', (w, h) == (SENSOR_W, SENSOR_H))
check('native output is landscape', w > h)

# The specific regression: deriving out_size by unpacking fpa_shape as (rows, cols) yields
# (62, 80) and squashes every frame into a portrait canvas. Alignment then cancels it, so this
# assertion is the only signal.
check('native output is not the 62x80 squash', (w, h) != (SENSOR_H, SENSOR_W))

# An explicit out_size must still be honoured — record_combined.py depends on it.
img2, _ = frame_to_gray8(data, FPA_SHAPE, out_size=(480, 372))
check('explicit out_size is still honoured', img2.shape == (372, 480))

# Temperature stats must come from the decoded frame, not the resized image.
check('stats report the real observed range', abs(stats[0] - 20.0) < 0.01 and abs(stats[1] - 40.0) < 0.01)

failed = [n for n, ok in _results if not ok]
print(f"\n{len(_results) - len(failed)}/{len(_results)} passed")
if failed:
    print("FAILED: " + ", ".join(failed))
sys.exit(1 if failed else 0)
