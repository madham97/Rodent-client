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

### USB read endpoint stalls during GPRS data (2026-07-25)

**Symptom**: uploads stop; the log fills with `Modem did not enter DOWNLOAD state:` (note the
empty response) interleaved with `GPRS bearer opened` every ~12 s, and every image ends up
parked in `failed/`.

**What is actually happening**: a GPRS data attempt (`AT+HTTPACTION`, `AT+SAPBR=1,1`,
`AT+SAPBR=0,1`) halts the CP2102's bulk-IN endpoint. The kernel logs

```
cp210x ttyUSB0: usb_serial_generic_read_bulk_callback - urb stopped: -32
```

`-32` is `-EPIPE`. The usb-serial driver does not resubmit the read URB after that status, so
**the file descriptor never delivers another byte, while writes keep succeeding**. Every
subsequent AT command looks like it returned an empty response, forever. Only re-opening the
port recovers it — `ATZ`, `AT+CFUN=1,1` and bearer cycling cannot, because their replies are
unreadable too. Counting the messages is the quickest confirmation:

```bash
dmesg | grep -c 'urb stopped'     # increments by 2 on every stall
```

This is *not* power-related: `AT+CBC` stays at 3994–3999 mV throughout, and the Pi reports
`vcgencmd get_throttled` = `0x0`. It is also not an idle timeout — an open port left untouched
for 3 minutes stays healthy. Only radio/data operations trigger it.

`uploader.py` now re-opens the port automatically when a response comes back empty
(`SIM800.reopen()`), so a stall costs one retry instead of the whole service.

**Ruled out as causes** (each tested directly, so don't re-test these):

| Suspected cause | Test | Result |
|---|---|---|
| Power / brownout on TX | `AT+CBC` sampled every second across a data attempt | Steady 3994–3999 mV; `vcgencmd get_throttled` = `0x0`. Not power |
| USB autosuspend | `/sys/bus/usb/devices/1-1.2/power/control` | `on`, `runtime_suspended_time` 0. Not autosuspend |
| Idle timeout | Port held open 180 s with no traffic | No stall. Only radio/data activity triggers it |
| `reset_input_buffer()` (cp210x PURGE) racing incoming data | Full upload with zero purge calls | Stalled anyway. Not the purge |
| UART speed | 5 × `GET /health` at each of 115200 / 19200 / 9600 baud (`AT+IPR`) | **0/5 verdicts and 5/5 stalled at every speed**, exactly 2 `urb stopped` per attempt. Baud is irrelevant |

**Actual cause: the USB port.** The HAT was on port `1-1.2` of the external USB2.0 hub
(VIA Labs `2109:3431`). Moving it to `1-1.1` on the same hub fixed it outright:

| | Verdicts received | Trials stalled |
|---|---|---|
| Port `1-1.2` (115200 / 19200 / 9600) | 0/15 | 15/15 |
| Port `1-1.1` | 4/4 | 0/4 |

Uploads resumed immediately after the move (`Upload successful ... 62.2s, 2.0 KB/s`), and the
reported signal rose from 16–17/31 to 21/31. So the stall was specific to that port/connector
path, not to the module, the SIM, the carrier, or the software.

**If it comes back**, work the physical layer first — that is where this fault lives:

- Move the HAT to a different USB port, and prefer **directly into the Pi's USB 3.0 (blue)
  port rather than through the hub** — the hub is an extra failure point this rig does not need.
- Reseat or replace the USB cable; try a shorter or shielded one, or add a ferrite. A 2G
  transmit burst coupling into the cable is a classic source of endpoint stalls.
- Check `dmesg | grep -c 'urb stopped'` before and after a data attempt: 2 new lines per
  attempt is the signature.

Note that GPIO UART mode is **not** available as a fallback on this unit — the thermal camera
occupies the GPIO header (see `docs/thermal-camera-setup.md`), so the USB path is the only
option and its physical health matters.

### Telling a dead data path apart from a dead modem

A stalled port makes a healthy modem look dead, so check the module *before* suspecting the
hardware. Stop the uploader first, then (see `## Verifying the Setup` below for the harness):

| Command | Healthy answer | Meaning |
|---------|----------------|---------|
| `AT+CPIN?` | `+CPIN: READY` | SIM unlocked |
| `AT+CREG?` | `+CREG: 0,1` or `0,5` | registered |
| `AT+CGATT?` | `+CGATT: 1` | attached to GPRS |
| `AT+CSQ` | 10–31 | signal present |
| `AT+SAPBR=2,1` | `+SAPBR: 1,1,"<ip>"` | bearer up, IP assigned |
| `AT+CBC` | ~4000 mV | module supply is fine |
| `AT+HTTPSTATUS?` | `POST,0,0,0` | HTTP engine **idle** — see the warning below |

**`POST,0,0,0` does not mean the upload failed.** `<mode>,<status>,<finish>,<remain>` with
status `0` means *idle*, and an engine that has already finished reads exactly the same as one
that never started. During this investigation that reading was initially taken as proof that
the carrier was moving no data — it was wrong. The uploads were in fact arriving and being
answered `200`; only the modem's reply was being lost to the USB stall. Do not diagnose a dead
data path from this value.

**Ask the server instead — it is the only reliable witness.** The modem cannot tell you whether
a POST landed once its reply is lost:

```bash
# Did this specific image arrive? The server re-encodes an uploaded .webp to <stem>.jpg,
# so ask for the .jpg name, not the local .png/.webp name — the wrong extension always 404s.
curl -s -o /dev/null -w '%{http_code}\n' <server_url>/annotate/specific/<stem>.jpg

curl -s <server_url>/health          # over WiFi: proves the server itself is fine
```

If the server has the image, the upload worked and the verdict was lost in transit — a local
serial/USB fault, not a network or carrier one. `uploader.py` performs exactly this check
automatically (`confirm_path`) before re-sending anything.

Note that `AT+CDNSGIP` and `AT+CIPPING` are **not** valid tests here — they belong to the CIP
stack, not the SAPBR bearer that HTTP uses, and return `ERROR` regardless of data health.

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
