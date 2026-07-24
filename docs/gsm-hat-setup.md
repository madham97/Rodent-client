# GSM HAT Setup — GPIO UART vs USB

## Hardware

**Board**: Waveshare GSM/GPRS/GNSS HAT  
**Module**: SIM868 (compatible with SIM800C AT command set)  
**USB-to-UART chip**: Silicon Labs CP2102  
**Amazon listing**: B07PRRR4F7

The HAT supports two communication modes between the Raspberry Pi and the SIM868 module, selected via a jumper block on the board.

---

## Jumper Block

The jumper block has **4 rows × 2 columns** (8 pins total). The two columns carry TX and RX. Three positions are labelled on the PCB:

```
Row 1: [A] [A]
Row 2: [A/B] [A/B]   ← A and B overlap here
Row 3: [B/C] [B/C]   ← B and C overlap here
Row 4: [C] [C]
```

A vertical jumper cap bridges two adjacent rows within one column:

| Position | Rows bridged | What it connects |
|----------|-------------|-----------------|
| A | 1–2 | CP2102 (USB-to-UART chip) → SIM868 |
| B | 2–3 | Raspberry Pi GPIO UART → SIM868 |
| C | 3–4 | CP2102 → Raspberry Pi GPIO UART |

---

## Mode 1: GPIO UART (original setup)

The Pi's hardware UART (`/dev/serial0`, GPIO pins 8/10) talks directly to the SIM868.

**Jumper setting**: Both caps on **B** (rows 2–3 on both columns)

**Serial device**: `/dev/serial0`

**Config** (`config/client.json`):
```json
"gsm_device": "/dev/serial0"
```

**Additional setup required**:
- Disable the serial console so the kernel doesn't hold the port:
  ```bash
  sudo systemctl disable serial-getty@ttyS0.service
  sudo systemctl stop serial-getty@ttyS0.service
  ```
- Remove `console=serial0,...` from `/boot/firmware/cmdline.txt`
- `install.sh` handles both of these automatically

**Serial open flags**: `rtscts=True` works in this mode (CTS is wired through the GPIO header).

---

## Mode 2: USB (current setup)

The Pi connects to the CP2102 via USB. The CP2102 converts USB to UART and forwards to the SIM868.

**Jumper setting**: Both caps on **A** (rows 1–2 on both columns)

**USB port**: Must use a **USB 3.0 (blue) port** on the Raspberry Pi 4B. The 2.0 (black) ports do not enumerate the CP2102 on this board.

**USB cable**: Must be a data cable (not charge-only). The HAT's power LED will light up from a charge-only cable but the device will not appear in `lsusb`.

**Serial device**: `/dev/ttyUSB0`

**Config** (`config/client.json`):
```json
"gsm_device": "/dev/ttyUSB0"
```

**Serial open flags**: `rtscts=False` is required. `rtscts=True` blocks communication because the CTS line is not reliably wired through the CP2102 on this board. The modem will open successfully but return empty responses to every AT command.

**No serial console changes needed**: Unlike GPIO mode, the USB path doesn't conflict with the kernel serial console. The `install.sh` serial freeing steps are harmless but unnecessary.

---

## What We Tried and What Didn't Work

This section records the full debugging history to avoid repeating dead ends.

### Cable issues

- The HAT powers on (PWR LED lights) from a charge-only USB cable because 5V is present, but the CP2102 will not enumerate — nothing appears in `lsusb` and no USB event in `dmesg`.
- Changing to a "data" cable that was still charge-only had the same result.
- The fix was a verified data cable **and** a USB 3.0 port.

### USB port on the Pi matters

- Plugging into a USB 2.0 (black) port: CP2102 did not enumerate.
- Plugging into a USB 3.0 (blue) port: CP2102 enumerated immediately. This is documented in the Waveshare FAQ: *"the Raspberry Pi 4B needs to be plugged into the 3.0 interface."*

### Jumper configurations tried before finding the correct one

| Jumpers | Result |
|---------|--------|
| Both on B (GPIO mode) | Worked on GPIO UART, nothing via USB |
| Left A + Right C | No response at any baud rate |
| Both on C | No response |
| Both on A + USB 2.0 port | CP2102 not detected |
| Both on A + USB 3.0 port + `rtscts=True` | CP2102 detected, port opened, but modem returned empty responses to all AT commands |
| **Both on A + USB 3.0 port + `rtscts=False`** | **Working — full modem init, network registration, GPRS up** |

### rtscts=True blocking communication

Even after getting the hardware right (correct port, correct jumpers), the uploader was failing with "Modem did not respond within timeout". Manual testing with `rtscts=False` produced correct AT responses immediately, confirming the issue was hardware flow control, not the modem or jumpers.

The fix was changing `rtscts=True` → `rtscts=False` in `SIM800.open()` in `pi-client/uploader.py:192`.

---

## Verifying the Setup

After plugging in, confirm the CP2102 is detected:

```bash
ls /dev/ttyUSB*          # should show /dev/ttyUSB0
dmesg | tail -5          # should show "cp210x converter now attached to ttyUSB0"
```

Quick AT command test (stop the uploader service first to avoid port contention):

```bash
sudo systemctl stop monitoring-pipeline-uploader
/opt/Rodent-client/venv/bin/python3 -c "
import serial, time
s = serial.Serial('/dev/ttyUSB0', 115200, timeout=3, rtscts=False)
s.write(b'AT\r\n')
time.sleep(1)
print(repr(s.read(s.in_waiting or 200)))
s.close()
"
# Expected: b'AT\r\r\nOK\r\n'
sudo systemctl start monitoring-pipeline-uploader
```

---

## Switching Back to GPIO UART

If USB stops working or you need to move back to GPIO:

1. Move both jumper caps to **B** (rows 2–3)
2. Update `config/client.json`: `"gsm_device": "/dev/serial0"`
3. Ensure the serial console is disabled (run `install.sh` or do it manually)
4. Restart the uploader: `sudo systemctl restart monitoring-pipeline-uploader`
5. `rtscts` can be set back to `True` if desired (it worked on GPIO), but `False` also works fine

---

## Notes on the gsm-pin Service

`scripts/gsm-pin-unlock.py` and its systemd unit (`monitoring-pipeline-gsm-pin.service`) were written for pppd mode, where a separate process needed to unlock the SIM PIN before pppd claimed the serial port. The uploader now handles PIN unlock itself as part of its `initialize()` sequence (`AT+CPIN`), so the gsm-pin service is not needed in normal operation. It is harmless to leave installed.
