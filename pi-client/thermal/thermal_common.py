"""
Shared setup for the Waveshare MI48 80×62 thermal camera.

Wiring (BCM numbering):
  SDA  → GPIO2  (I2C, pin 3)
  SCL  → GPIO3  (I2C, pin 5)
  MOSI → GPIO10 (SPI, pin 19)
  MISO → GPIO9  (SPI, pin 21)
  CLK  → GPIO11 (SPI, pin 23)
  SS   → GPIO7  (SPI CE1, pin 26)  ← managed manually by pysenxor
  READY→ GPIO24 (pin 18)
  RESET→ GPIO23 (pin 16)
  VCC  → 5V     (pin 2 or 4)
  GND  → GND    (pin 6, 9, 14, …)

SPI speed: 2 MHz by default. 31 MHz (the sensor maximum) causes CRC errors over
jumper-wire connections; even 4 MHz can still produce frequent CRC errors on longer
or looser jumper runs. Drop lower (e.g. 1 MHz) if `read_frame()` keeps exhausting its
retries, or use a direct PCB mount / shorter, twisted SPI wiring to allow a higher rate.
"""
import time
from smbus import SMBus
from spidev import SpiDev
from gpiozero import DigitalInputDevice, DigitalOutputDevice
from senxor.mi48 import MI48
from senxor.interfaces import SPI_Interface, I2C_Interface

I2C_BUS      = 1
I2C_ADDR     = 0x40   # default; 0x41 selectable via 0R resistor on PCB
SPI_BUS      = 0
SPI_DEVICE   = 0      # /dev/spidev0.0 (hardware CE ignored; CS driven via GPIO7)
SPI_SPEED_HZ = 2_000_000
SPI_XFER     = 160    # bytes per transfer (1 row of 80 pixels × 2 bytes)

BCM_CS    = 7
BCM_DRDY  = 24
BCM_RESET = 23


class _Reset:
    def __init__(self, pin):
        self.pin = pin

    def __call__(self):
        self.pin.on()
        time.sleep(35e-6)
        self.pin.off()
        time.sleep(0.05)


def make_mi48(fps=9, filters=True, offset=0.0, spi_speed_hz=SPI_SPEED_HZ):
    """
    Initialise and return a ready-to-use MI48 instance.

    Parameters
    ----------
    fps : int
        Desired frame rate (1–25).
    filters : bool
        Enable the on-chip spatial filters (f1, f2) for cleaner images.
    offset : float
        Global temperature offset correction in °C (factory calibration is
        usually sufficient; leave at 0.0).
    spi_speed_hz : int
        SPI clock speed. Lower this (e.g. to 1 MHz) if CRC errors are frequent —
        see the module docstring.

    Returns
    -------
    tuple (mi48, cs_pin)
        mi48   — MI48 instance, already started
        cs_pin — DigitalOutputDevice for the SPI CS line; caller must
                 assert (on) before read() and deassert (off) after.
    """
    i2c = I2C_Interface(SMBus(I2C_BUS), I2C_ADDR)

    spi_dev = SpiDev(SPI_BUS, SPI_DEVICE)
    spi = SPI_Interface(spi_dev, xfer_size=SPI_XFER)
    spi.device.mode          = 0b00
    spi.device.max_speed_hz  = spi_speed_hz
    spi.device.bits_per_word = 8
    spi.device.lsbfirst      = False
    spi.cshigh  = True
    spi.no_cs   = True

    cs_pin    = DigitalOutputDevice(f"BCM{BCM_CS}",    active_high=False, initial_value=False)
    drdy_pin  = DigitalInputDevice(f"BCM{BCM_DRDY}",  pull_up=False)
    reset_pin = DigitalOutputDevice(f"BCM{BCM_RESET}", active_high=False, initial_value=True)

    mi48 = MI48([i2c, spi], data_ready=drdy_pin, reset_handler=_Reset(reset_pin))
    mi48.set_fps(fps)
    if filters and int(mi48.fw_version[0]) >= 2:
        mi48.enable_filter(f1=True, f2=True, f3=False)
        mi48.set_offset_corr(offset)

    return mi48, cs_pin


CS_DELAY = 1e-4   # seconds to hold CS before/after SPI transfer


def read_frame(mi48, cs_pin, timeout=3, retries=2):
    """
    Wait for DATA_READY, read one frame, return (data, header).

    The SPI link to this sensor is CRC-checked in hardware and jumper-wire connections
    routinely corrupt a transfer here and there (see module docstring) — `mi48.read()`
    logs a CRC error and returns None in that case rather than raising. Retry a few times
    before giving up, since a fresh DATA_READY wait + re-read usually succeeds.
    """
    for attempt in range(retries + 1):
        mi48.data_ready.wait_for_active(timeout=timeout)
        cs_pin.on()
        time.sleep(CS_DELAY)
        data, header = mi48.read()
        time.sleep(CS_DELAY)
        cs_pin.off()
        if data is not None:
            return data, header
        if attempt < retries:
            time.sleep(0.02)
    return data, header


def rotate_gray8(img8, rotation):
    """Rotate an 8-bit frame clockwise by 0/90/180/270 degrees.

    A 90/270 rotation transposes the frame, so the caller gets back an image whose width and
    height are swapped relative to the input."""
    if not rotation:
        return img8
    import cv2 as cv
    codes = {90: cv.ROTATE_90_CLOCKWISE, 180: cv.ROTATE_180, 270: cv.ROTATE_90_COUNTERCLOCKWISE}
    if rotation not in codes:
        return img8
    return cv.rotate(img8, codes[rotation])


def frame_to_gray8(data, fpa_shape, out_size=(480, 372), hflip=False, vflip=False, rotation=0):
    """
    Convert raw MI48 frame data to an 8-bit single-channel image plus temperature stats.

    Parameters
    ----------
    data       : raw data from MI48.read()
    fpa_shape  : mi48.fpa_shape  (rows, cols)
    out_size   : (width, height) for the output image
    hflip, vflip : bool
        The MI48's row/column readout order doesn't necessarily match the CSI camera's —
        on at least one unit tested the thermal frame comes out horizontally mirrored
        relative to the (correctly oriented) visible-camera frame. Set hflip/vflip so the
        two feeds agree on left/right and up/down before pairing them side by side or
        fusing them.
    rotation : int
        Clockwise rotation in degrees (0/90/180/270), the thermal counterpart to the visible
        feed's `image_rotation` — for a sensor mounted at a different angle to the camera.
        Applied before the flips, so hflip/vflip refer to the upright thermal image. Note a
        90/270 rotation swaps the frame's aspect ratio; the result is resized to the visible
        frame when fused, so a mismatched aspect will stretch rather than crop.

    Returns
    -------
    (img8, (temp_min_c, temp_max_c, temp_avg_c))
        img8 — normalized 8-bit numpy array, shape (out_size[1], out_size[0])
        temps — the actual °C range the 0-255 normalization was stretched from,
                needed to reconstruct real temperatures from a pixel value later:
                temp_c = temp_min_c + (pixel / 255) * (temp_max_c - temp_min_c)
    """
    import cv2 as cv
    from senxor.utils import data_to_frame, cv_filter

    frame = data_to_frame(data, fpa_shape)
    stats = (float(frame.min()), float(frame.max()), float(frame.mean()))

    img8 = cv.normalize(frame.astype('float32'), None, 0, 255,
                         norm_type=cv.NORM_MINMAX, dtype=cv.CV_8U)
    img8 = cv_filter(img8, parameters={'blur_ks': 3},
                      use_median=False, use_bilat=True, use_nlm=False)
    img8 = cv.resize(img8, out_size, interpolation=cv.INTER_CUBIC)
    # Rotate first, then mirror, so hflip/vflip mean left/right and up/down in the upright
    # image — the same order the visible feed uses.
    img8 = rotate_gray8(img8, rotation)
    if hflip or vflip:
        flip_code = -1 if (hflip and vflip) else (1 if hflip else 0)
        img8 = cv.flip(img8, flip_code)
    return img8, stats


def frame_to_image(data, fpa_shape, out_size=(480, 372), colormap=None, hflip=False,
                   vflip=False, rotation=0):
    """
    Convert raw MI48 frame data to an 8-bit colour image (numpy array, BGR).

    Parameters
    ----------
    data       : raw data from MI48.read()
    fpa_shape  : mi48.fpa_shape  (rows, cols)
    out_size   : (width, height) for the output image
    colormap   : cv2 colormap constant (default: cv2.COLORMAP_INFERNO)
    hflip, vflip, rotation : see frame_to_gray8
    """
    import cv2 as cv

    if colormap is None:
        colormap = cv.COLORMAP_INFERNO

    img8, _ = frame_to_gray8(data, fpa_shape, out_size=out_size, hflip=hflip, vflip=vflip,
                             rotation=rotation)
    return cv.applyColorMap(img8, colormap)
