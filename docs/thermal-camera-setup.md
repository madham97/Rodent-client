# Thermal Camera Setup — Waveshare MI48 80×62

**Module**: Waveshare Long-Wave IR Thermal Imaging Camera Module (90° FOV variant)  
**Sensor chip**: Meridian Innovation MI48x3 (80×62 pixels, 8–14 µm, up to 25 FPS)  
**Interfaces**: I2C (register config) + SPI (frame data stream)  
**Datasheet**: [MI48x3 v3.1.3](https://files.waveshare.com/wiki/Thermal_Camera_Module/MI48x3--Datasheet--v3.1.3..pdf)  
**Waveshare wiki**: https://www.waveshare.com/wiki/Thermal-Camera-Module

---

## How it works

The board contains an MI48 sensor and an onboard MCU. Two interfaces are used simultaneously:

- **I2C** — used only to configure sensor registers (frame rate, filters, offset).
- **SPI** — used to stream full frame data. You must poll the **DATA_READY** pin and manually assert the SPI CS line via GPIO; the kernel's hardware CS cannot be used because the Linux SPI driver doesn't handle the CS timing this sensor requires.

The `pysenxor` library (from Waveshare / Meridian Innovation) handles all of this.

---

## Wiring

Connect to the Raspberry Pi 40-pin header:

| Module pin | Function   | BCM GPIO | Header pin |
|------------|------------|----------|------------|
| VCC        | 5V power   | —        | Pin 2 or 4 |
| GND        | Ground     | —        | Pin 6, 9, 14, … |
| SDA        | I2C data   | GPIO2    | Pin 3  |
| SCL        | I2C clock  | GPIO3    | Pin 5  |
| MOSI       | SPI data out | GPIO10 | Pin 19 |
| MISO       | SPI data in  | GPIO9  | Pin 21 |
| CLK        | SPI clock  | GPIO11   | Pin 23 |
| SS         | SPI chip select | **GPIO7** | **Pin 26** |
| READY      | Data ready signal | GPIO24 | Pin 18 |
| RESET      | Hardware reset    | GPIO23 | Pin 16 |

> **Important — SS on GPIO7 (pin 26), not GPIO8 (pin 24).**  
> The `pysenxor` library drives CS manually via GPIO7, so it must be wired there.
> GPIO8 (CE0) is used internally by the SPI bus but ignored (no_cs=True in software).
>
> **READY and RESET are mandatory.** The library waits on READY before every frame
> read and pulses RESET on startup. The sensor will not work without these connected.

---

## `/boot/firmware/config.txt` changes

Add or ensure these lines are present (order matters — `spi0-0cs` must not be paired with `dtparam=spi=on`):

```
dtparam=i2c_arm=on
dtparam=i2c_arm_baudrate=10000
dtoverlay=spi0-0cs
```

**Do not add `dtparam=spi=on`.** The `spi0-0cs` overlay enables SPI with no kernel-managed chip-select pins, which frees GPIO7 for `pysenxor` to manage CS itself. Adding `spi=on` alongside it causes the kernel to reclaim GPIO7 as CE1, making it busy and unavailable to user space.

**`i2c_arm_baudrate=10000`** (10 kHz) is required because the Raspberry Pi's BCM283x I2C controller has a short, fixed clock-stretch timeout. At the default 100 kHz the timeout expires before the MI48 can respond to register reads. Dropping to 10 kHz multiplies the timeout by 10× and makes I2C reliable.

Also ensure the `i2c-dev` kernel module loads on boot:

```bash
echo "i2c-dev" | sudo tee /etc/modules-load.d/i2c-dev.conf
```

Reboot after making config changes.

---

## Software installation

The `pysenxor` library is **not** on PyPI. Install it from the Waveshare zip:

```bash
cd ~
wget https://files.waveshare.com/wiki/Thermal_Camera_Module/Thermal_Camera_Hat.zip
unzip Thermal_Camera_Hat.zip

# Install system-level dependencies (cannot be compiled in the venv
# without Python headers; easier to use system packages)
sudo apt-get install -y \
    python3-smbus \
    python3-spidev \
    python3-gpiozero \
    python3-numpy \
    python3-opencv \
    python3-setuptools \
    python3-pip

# Install pysenxor and remaining deps system-wide
sudo pip3 install --break-system-packages \
    crcmod cmapy imutils pysenxor
```

Verify:

```bash
python3 -c "import senxor, smbus, spidev, gpiozero, cv2; print('all ok')"
```

---

## I2C address

The default I2C address is **0x40**. The board has a 0Ω resistor that can be moved to change it to 0x41. Confirm with:

```bash
i2cdetect -y 1
```

You should see a device at `0x40`.

---

## SPI speed

**Use 4 MHz.** The sensor supports up to ~31 MHz but at that speed jumper-wire connections produce CRC errors on every frame. 4 MHz is reliable over a 100 mm ribbon or jumper wires. If the board is mounted directly on the header (no wires), you may be able to increase to ~8–16 MHz.

---

## Running the tools

> **Always `cd` to a writable directory before running** (e.g. `cd ~`). The `gpiozero` / `lgpio` backend creates a socket file in the current working directory; running from `/opt` (not writable by your user) causes it to fail.

### Single snapshot

```bash
cd ~
python3 /opt/Rodent-client/pi-client/thermal/snapshot.py [output.png]
# default output: /opt/Rodent-client/thermal.png
```

### Record a video

```bash
cd ~
python3 /opt/Rodent-client/pi-client/thermal/record_video.py \
    --duration 10 --fps 9 [output.mp4]
# default output: /opt/Rodent-client/thermal.mp4
```

Options:

| Flag | Default | Description |
|------|---------|-------------|
| `--duration` / `-d` | `10` | Recording length in seconds |
| `--fps` / `-f` | `9` | Sensor frame rate (1–25) |
| `--width` | `480` | Output video width in pixels |
| `--height` | `372` | Output video height in pixels |
| positional | `thermal.mp4` | Output file path |

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `GPIO busy` error on BCM7 | `dtparam=spi=on` still present | Remove it from config.txt; keep only `dtoverlay=spi0-0cs`; reboot |
| I2C read timeout | Clock stretching; wrong baudrate | Confirm `dtparam=i2c_arm_baudrate=10000` is in config.txt |
| `No module named 'senxor'` | pysenxor not installed | Run the pip install step above |
| CRC errors on every frame | SPI speed too high | Ensure `SPI_SPEED_HZ = 4_000_000` in `thermal_common.py` |
| Wild temperatures (< −100°C or > 1000°C) | First frame after reset | Normal — discard a few warmup frames (the scripts do this automatically) |
| `lgpio … Operation not permitted` | Running from `/opt` | `cd ~` before running scripts |
| `i2cdetect` shows nothing on bus 1 | `i2c-dev` module not loaded | `sudo modprobe i2c-dev`; add to `/etc/modules-load.d/i2c-dev.conf` for persistence |
| DATA_READY never goes high | READY pin not connected, or wrong GPIO | Check wiring: READY → GPIO24 (pin 18) |
