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

---

## Linux TODO (next session on Ubuntu)

The goal is to detect all 16+ buttons including CC buttons via BLE on Linux/BlueZ.

### Setup
```bash
sudo apt install python3-pip libglib2.0-dev
pip install bleak
# Bluetooth stack: BlueZ (built-in on Ubuntu)
```

### Approach
BlueZ does NOT intercept the HID service — bleak on Linux can enumerate and subscribe
to `0x2A4D` (HID Report) characteristics directly.

```python
# On Linux, the full HID service IS visible via bleak
# Subscribe to CCCD at handle 0x0048 to get CC button events on handle 0x0047
# OR iterate client.services, find 0x1812, subscribe to all 0x2A4D notify chars
```

### Tasks
1. Pair gamepad with Ubuntu: `bluetoothctl` → `pair F4:22:7A:4A:AA:E0` → `trust` → `connect`
2. Run `gamepad_tester.py ble` — should now see HID service in service tree
3. Verify CC button data arrives on handle `0x0047` as 2-byte LE uint16
4. Update `gamepad_tester.py` to use Linux BLE path for CC buttons
5. Implement combined mode: hidapi for regular buttons + BLE for CC buttons

### Expected wire format for CC buttons on Linux
```python
# on_notification(sender, data):
usage = int.from_bytes(data[:2], "little")
CC_MAP = {0x0223: "Home", 0x0221: "SalutLogo", 0x0063: "Play", 0x0224: "Back"}
button = CC_MAP.get(usage)  # None on release (usage == 0)
```

---

## Audio Protocol (gamepad_mic_tester.py)

Works on Windows. BLE audio via `ab5e` service:
- Write `0x0C 0x00` to `ab5e0002` → GET_CAPS response on `ab5e0004`
- Audio data streams on `ab5e0003` as IMA ADPCM frames
- Conversion: `sox input.ima -r 16000 -e signed -b 16 -c 1 output.wav`
- Playback: PowerShell `SoundPlayer.PlaySync()` (avoids COM/WinRT conflict with bleak)

---

## Communication
- Language: Russian
- Code comments and docs: English
