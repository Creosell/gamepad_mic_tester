# Project: Gamepad Mic Tester + Button Tester

## Overview

Two tools for testing a BLE gamepad (Realtek BT G100-4722):
1. **`gamepad_mic_tester.py`** — tests the built-in microphone via BLE audio
2. **`gamepad_tester.py`** — tests physical buttons via raw HID (Windows) or BLE GATT (Linux)

## Target Device

| Field       | Value                          |
|-------------|-------------------------------|
| Name        | GAME                          |
| MAC         | `F4:22:7A:4A:AA:E0`           |
| Model       | G100-4722 (Realtek BT)        |
| Firmware    | V1.4.0                        |
| BLE HWID    | `BTHLE\Dev_f4227a4aa7e6`      |
| USB         | Charge only, no data          |

## Button Layout

Physical buttons: A / B / X / Y · L1 / R1 / L2 / R2 · L3 / R3 · Share / Options · D-pad · 2 sticks · Back / Home / SalutLogo / Play

## Files

| File                    | Purpose                                              |
|-------------------------|------------------------------------------------------|
| `gamepad_mic_tester.py` | BLE mic tester — scan, record, convert, play, log    |
| `gamepad_tester.py`     | Button tester — HID probe/test + BLE CC buttons      |
| `dev_tools.py`          | Debug tools — sniff/probe/record/analyze BLE audio   |
| `hid_analyzer.py`       | HID report analyzer                                  |
| `raw_input_probe.py`    | Windows Raw Input probe (dead end, kept for reference)|

## Dependencies

```bash
pip install bleak hid
# Linux also needs:
sudo apt install python3-bluez sox  # or pipx install bleak
```

---

## HID Report Format (gamepad_tester.py, via hidapi)

Device: VID `0x1D5A` PID `0x1802`, hidapi device index 14, Usage Page `0x01` Usage `0x05`.

11-byte report layout:

```
byte[0]  = 0x04  constant
byte[1]  = 0x67  constant
byte[2]  = D-pad hat switch (0xFF = neutral)
byte[3]  = face buttons + bumpers (bitmask)
byte[4]  = triggers + stick clicks + center buttons (bitmask)
byte[5]  = Left Stick X  (0x80 = center)
byte[6]  = Left Stick Y  (0x80 = center)
byte[7]  = Right Stick X (0x80 = center)
byte[8]  = Right Stick Y (0x80 = center)
byte[9]  = R2 analog     (0x00 = rest)
byte[10] = L2 analog     (0x00 = rest)
```

Button bit map:

```python
G100_BUTTONS = {
    (3, 0): "A",   (3, 1): "B",   (3, 3): "X",   (3, 4): "Y",
    (3, 6): "L1",  (3, 7): "R1",
    (4, 0): "L2",  (4, 1): "R2",
    (4, 2): "Share", (4, 3): "Options",
    (4, 5): "L3",  (4, 6): "R3",
}
G100_DPAD = {0x00:"Up", 0x01:"Up+Right", 0x02:"Right", 0x03:"Down+Right",
             0x04:"Down", 0x05:"Down+Left", 0x06:"Left", 0x07:"Up+Left", 0xFF:None}
```

---

## Consumer Controls Buttons — Confirmed via btsnoop

**Back / Home / SalutLogo / Play** use standard HID Consumer Controls (Usage Page 0x000C).

Confirmed via Android TV btsnoop_hci.log capture:

| Button     | GATT handle | Wire format (bytes) | HID usage |
|------------|-------------|---------------------|-----------|
| Home       | `0x0047`    | `23 02` / `00 00`   | 0x0223    |
| SalutLogo  | `0x0047`    | `21 02` / `00 00`   | 0x0221    |
| Play       | `0x0047`    | `63 00` / `00 00`   | 0x0063    |
| Back       | `0x0047`    | `24 02` / `00 00`   | 0x0224    |

Format: **2-byte little-endian uint16** HID CC usage code on press, `00 00` on release.

CCCD subscription handle: `0x0048` (TV writes `01 00` here to enable notifications).

The device uses **standard HOGP** (HID over GATT Profile, service `0x1812`). No proprietary
activation command needed — just subscribe to CCCD at handle `0x0048`.

### Firmware bug (FW 1.3.7, may be fixed in 1.4.0)
Every Consumer Controls button press also fires `BTN_TR2 + ABS_GAS` on the gamepad
HID collection (byte[4] bit1 + byte[9]). Not a reliable detection mechanism.

---

## GATT Service Map (full)

```
SVC 00001801  Generic Attribute
    00002a05  [indicate]
SVC 00001800  Generic Access
    00002a00/01/04/aa6  [read]
SVC 0000180f  Battery
    00002a19  [read/notify]
SVC 0000180a  Device Information
    00002a29/24/25/27/26/28/23/2a/50  [read]
SVC 0000d1ff  Vendor HID-like
    0000a001  [write-without-response/notify]  — 16B state on subscribe
    0000a002  [write-without-response/write]   — command channel (no known cmds)
SVC 0000d0ff  Vendor config/identity
    0000ffd1  [write-without-response]  ⚠ WRITING CAUSES DISCONNECT
    0000ffd2  [read]  → device MAC address
    0000ffd3  [read]  → 01 90 10 08
    0000ffd4  [read]  → 21 20 00 00
    0000ffd5  [read]  → (empty)
    0000ffd8  [write-without-response]
    0000fff1  [read]  → 05 01 00 07 00 08 00 00 f0 0f 00 00
    0000fff2  [write]  → requires specific payload length (unknown)
    0000ffe0  [read]  → 00 00 00 01 01 00 00 00 01 90 10 08 21 20 00 00
SVC 00006287  ATV / command-response service
    00006387  [write-without-response]  — command channel
    00006487  [write/notify]            — bidirectional: 0x0a → GET_CAPS response
SVC ab5e0001  Realtek audio
    ab5e0002  [write-without-response/write]  — audio command channel
    ab5e0003  [read/notify]                   — audio data
    ab5e0004  [read/notify]                   — audio response/control
```

### 6487 protocol (partial)
- Write `0x0a` → response `10 0a 08 00 00 00 00` (GET_CAPS)
- No START command found (tried 35+ single/multi-byte variants)

### HID service (0x1812) — NOT visible via WinRT/bleak on Windows
Handle map confirmed from btsnoop:
- `0x0047` — Consumer Controls HID Report value
- `0x0048` — CCCD for Consumer Controls (subscribe here)
- `0x002a`, `0x003a`, `0x0041` — other HID Report CCCDs

---

## Windows Limitations (CONFIRMED DEAD ENDS)

| Approach               | Result                                      |
|------------------------|---------------------------------------------|
| hidapi device 12 (CC)  | Windows exclusive lock, no data             |
| `keyboard` library     | CC buttons bypass keyboard hooks            |
| Win32 Raw Input        | Empty for Usage Page 0x000C                 |
| WM_APPCOMMAND          | Window never receives events                |
| bleak + WinRT          | WinRT hides HID service (0x1812) entirely   |
| GATT cmd probe (35+)   | No activation command found for CC buttons  |

**Root cause:** Windows BLE HID driver intercepts the entire HID service (0x1812) at
the kernel level, before any user-space API can access it.

### Windows — Untested Approaches (low probability, worth trying)

The following were NOT tested and remain open:

| Approach | Estimated chance | Notes |
|----------|-----------------|-------|
| `WH_KEYBOARD_LL` hook for `VK_BROWSER_BACK` (0xA6), `VK_BROWSER_HOME` (0xAC) | ~20% | `keyboard` lib test may have been incomplete — didn't isolate specific VK codes |
| `pynput` keyboard listener | ~15% | Different internal mechanism than `keyboard` lib; may catch VK codes the other missed |
| `Windows.Gaming.Input` WinRT API | ~10% | Separate gaming path, untested against this device |

If any of these work, CC buttons would map to VK codes:
- Back → `VK_BROWSER_BACK` (0xA6)
- Home → `VK_BROWSER_HOME` (0xAC)
- Play → `VK_MEDIA_PLAY_PAUSE` (0xB3) — usage 0xCD, not 0x63 (Play ≠ Play/Pause)
- SalutLogo → no standard VK, likely inaccessible regardless

---

## Linux — DONE ✓

All 16+ buttons working via `python3 gamepad_tester.py evdev`.

### How it works

BlueZ acts as HOGP host — subscribes to all HID reports and exposes two `/dev/input/` devices:

| Device | Name | Contents |
|--------|------|----------|
| `event13` / `js0` | `GAME` | A/B/X/Y, L1/R1/L2/R2, L3/R3, Share/Options, sticks, D-pad |
| `event12` | `GAME Consumer Control` | Home, Back, Play, SalutLogo |

### Setup (Ubuntu)
```bash
sudo apt install python3-bleak python3-evdev python3-hid
sudo usermod -aG input $USER   # allow reading /dev/input/event* without sudo
# re-login or: newgrp input
bluetoothctl
  pairable on
  scan on
  pair F4:22:7A:4A:AA:E0
  trust F4:22:7A:4A:AA:E0
  connect F4:22:7A:4A:AA:E0
  quit
```

### Usage
```bash
python3 gamepad_tester.py evdev
# optional: evdev <gamepad_path> <cc_path>
# defaults: /dev/input/event13  /dev/input/event12
```

### evdev key → button mapping (confirmed)
```python
# Regular buttons (EV_KEY on event13)
KEY_304=A  KEY_305=B  KEY_307=X  KEY_308=Y
KEY_310=L1 KEY_311=R1 KEY_312=L2 KEY_313=R2
KEY_314=Share  KEY_315=Options
KEY_317=L3  KEY_318=R3

# Axes (EV_ABS on event13)
ABS_X=LX  ABS_Y=LY  ABS_Z=RX  ABS_RZ=RY
ABS_GAS=R2_analog  ABS_BRAKE=L2_analog
ABS_HAT0X=D-pad_X  ABS_HAT0Y=D-pad_Y

# CC buttons (EV_KEY on event12)
KEY_HOMEPAGE=Home  KEY_BACK=Back
KEY_VCR=Play  KEY_SEARCH=SalutLogo
```

### Notes
- HID service (0x1812) is NOT visible via bleak on Linux either — BlueZ input plugin
  takes it over, same as Windows. The evdev approach reads BlueZ's translated output.
- `event12`/`event13` device numbers may change after reconnect — check with
  `grep -A5 "GAME" /proc/bus/input/devices`

---

## Audio Protocol (gamepad_mic_tester.py)

Works on Windows and Linux. BLE audio via `ab5e` Realtek service.

### GATT characteristics

| UUID       | Direction       | Purpose                          |
|------------|-----------------|----------------------------------|
| `ab5e0002` | write           | Command channel                  |
| `ab5e0003` | notify          | Audio data stream (IMA ADPCM)    |
| `ab5e0004` | notify          | Command responses / control      |

### Commands

| Bytes        | Name      | Description                              |
|--------------|-----------|------------------------------------------|
| `0x0C 0x00`  | GET_CAPS  | Request capabilities; device responds on `ab5e0004` (first byte `0x0C`) |
| `0x0A 0x00`  | START     | Begin audio streaming on `ab5e0003`      |
| `0x0B 0x00`  | STOP      | Stop streaming                           |
| `0x00 0x00`  | STOP alt  | Secondary stop (sent after `0x0B 0x00`)  |

### Capture flow

1. Subscribe to notifications on `ab5e0003` (audio) and `ab5e0004` (responses)
2. Send `GET_CAPS` (`0x0C 0x00`) — wait for response on `ab5e0004` (up to 5 s)
3. Send `START` (`0x0A 0x00`) — device begins streaming frames
4. Collect frames from `ab5e0003` for the desired duration
5. Send `STOP` (`0x0B 0x00`), then `0x00 0x00` (50 ms apart)
6. Unsubscribe

### Warm-up (critical)

**After the very first BLE pairing, the first START cycle produces no audio frames.**
The device acknowledges GET_CAPS and START normally, but `ab5e0003` stays silent.

Fix: run one dummy `GET_CAPS → START → STOP` cycle immediately after connect/pair,
before showing the user any UI. Implemented in `_warmup()`. The next cycle works correctly.

This is a device firmware behaviour, not a timing or subscription issue.

### Audio packet format

Each notification on `ab5e0003` is one IMA ADPCM packet:

```
bytes[0:1]  — packet type / sequence (header, discard)
bytes[2:3]  — predictor (ADPCM state, discard)
bytes[4:5]  — step index  (ADPCM state, discard)
bytes[6:]   — raw ADPCM nibbles (keep these for sox)
```

Only `bytes[6:]` of each packet are written to the `.ima` file.

### Conversion & playback

```bash
sox -t ima -e ima-adpcm -r 16000 input.ima -e signed-integer output.wav norm -12
```

Parameters: 16 000 Hz, mono, IMA ADPCM. `norm -12` normalises headroom to −12 dB.

**Playback:**
- Windows: `PowerShell (New-Object Media.SoundPlayer 'file.wav').PlaySync()`  
  (avoids COM/WinRT conflict with bleak — do NOT use `winsound` or `pygame` here)
- Linux: `aplay` → `paplay` → `sox file.wav -d` (tried in order, first available wins)

---

## Communication
- Language: Russian
- Code comments and docs: English
