#!/usr/bin/env python3
"""
Unlock SIM PIN before pppd takes ownership of the serial port.
Run as a oneshot systemd service before monitoring-pipeline-gsm.service.
"""

import json
import sys
import time
import serial

CONFIG_PATH = '/opt/Rodent-client/config/client.json'
READY_TIMEOUT = 30  # seconds to wait for modem to respond to AT


def send_at(ser, cmd, timeout=2):
    ser.write(f'{cmd}\r'.encode())
    time.sleep(0.3)
    return ser.read(ser.in_waiting or 500).decode('ascii', errors='ignore').strip()


def wait_for_modem(ser, timeout=READY_TIMEOUT):
    """Poll AT until the modem responds OK. Returns True if ready, False if timed out."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        ser.reset_input_buffer()
        resp = send_at(ser, 'AT', timeout=1)
        if 'OK' in resp:
            return True
        print(f"Modem not ready yet, retrying... ({resp!r})")
        time.sleep(1)
    return False


def main():
    with open(CONFIG_PATH) as f:
        config = json.load(f)

    device = config.get('gsm_device', '/dev/serial0')
    pin    = config.get('gsm_pin', '')

    try:
        with serial.Serial(device, 115200, timeout=2) as ser:
            print(f"Waiting for modem on {device}...")
            if not wait_for_modem(ser):
                print(f"ERROR: Modem did not respond within {READY_TIMEOUT}s.")
                sys.exit(1)

            print("Modem ready.")

            # Reset modem cleanly
            send_at(ser, 'ATZ')
            time.sleep(0.5)

            resp = send_at(ser, 'AT+CPIN?', timeout=5)
            print(f"SIM status: {resp}")

            if '+CPIN: SIM PIN' in resp:
                if not pin:
                    print("ERROR: SIM requires a PIN but gsm_pin is not set in config.")
                    sys.exit(1)
                resp = send_at(ser, f'AT+CPIN={pin}', timeout=5)
                print(f"PIN unlock response: {resp}")
                if 'ERROR' in resp:
                    print("ERROR: Failed to unlock SIM — wrong PIN?")
                    sys.exit(1)
                time.sleep(3)  # Give SIM time to register after unlock
            elif '+CPIN: READY' in resp:
                print("SIM already unlocked.")
            else:
                print(f"Unexpected CPIN response: {resp}")

    except serial.SerialException as e:
        print(f"ERROR: Could not open {device}: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()
