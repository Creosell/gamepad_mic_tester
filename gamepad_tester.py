 #!/usr/bin/env python3
"""
BLE HID Gamepad Button Tester — Windows

Reads raw HID reports from a paired BLE gamepad and displays button/axis states.

Usage:
    python gamepad_tester.py              # auto-detect + test mode
    python gamepad_tester.py list         # enumerate all HID devices
    python gamepad_tester.py probe [N]    # dump raw reports from device N
    python gamepad_tester.py test  [N]    # live button/axis dashboard (device N)
    python gamepad_tester.py learn [N]    # name buttons interactively

Dependencies:
    pip install hid
"""

import asyncio
import os
import sys
import time

# ─── G100 button map ──────────────────────────────────────────────────────────
# Key: (byte_index, bit_index)  Value: button name
# Populated incrementally as groups are probed.
# Report layout (VID=1D5A PID=1802), indices into hid.read() payload:
#   byte[0]   = 0x04  (report ID / constant)
#   byte[1]   = 0x67  (constant)
#   byte[2]   = 0xFF  (constant)
#   byte[3]   = face buttons + bumpers
#   byte[4]   = more buttons
#   byte[5]   = Left stick X   (0x80 = center)
#   byte[6]   = Left stick Y   (0x80 = center)
#   byte[7]   = Right stick X  (0x80 = center)
#   byte[8]   = Right stick Y  (0x80 = center)
#   byte[9]   = triggers / D-pad / misc
#   byte[10]  = misc

G100_BUTTONS: dict[tuple[int, int], str] = {
    # byte[3] — face buttons + bumpers
    (3, 0): "A",
    (3, 1): "B",
    (3, 3): "X",
    (3, 4): "Y",
    (3, 6): "L1",
    (3, 7): "R1",
    # byte[4] — triggers + stick clicks + center buttons
    (4, 0): "L2",
    (4, 1): "R2",
    (4, 2): "Share",
    (4, 3): "Options",
    (4, 5): "L3",
    (4, 6): "R3",
}

# D-pad hat switch — byte[2], value 0xFF = neutral
G100_DPAD_BYTE = 2
G100_DPAD: dict[int, str | None] = {
    0x00: "Up",
    0x01: "Up+Right",
    0x02: "Right",
    0x03: "Down+Right",
    0x04: "Down",
    0x05: "Down+Left",
    0x06: "Left",
    0x07: "Up+Left",
    0xFF: None,
}

# Axis definitions: (byte_index, label, center_value)
# Triggers use 0x00 as rest, sticks use 0x80 as center.
G100_AXES: list[tuple[int, str, int]] = [
    (5,  "LX",     0x80),
    (6,  "LY",     0x80),
    (7,  "RX",     0x80),
    (8,  "RY",     0x80),
    (9,  "R2 analog", 0x00),
    (10, "L2 analog", 0x00),
]

G100_VID = 0x1D5A
G100_PID = 0x1802

# ─── HID constants ────────────────────────────────────────────────────────────
_USAGE_PAGE_GENERIC  = 0x01
_USAGE_JOYSTICK      = 0x04
_USAGE_GAMEPAD       = 0x05
_USAGE_PAGE_CONSUMER = 0x0C

_GAMEPAD_USAGES = {(_USAGE_PAGE_GENERIC, _USAGE_JOYSTICK),
                   (_USAGE_PAGE_GENERIC, _USAGE_GAMEPAD)}


def _hid():
    try:
        import hid
        return hid
    except ImportError:
        print("Missing dependency:  pip install hid")
        sys.exit(1)


# ─── Device discovery ─────────────────────────────────────────────────────────

def _enumerate():
    return list(_hid().enumerate())


def _device_label(d: dict) -> str:
    name = f"{d['manufacturer_string']} {d['product_string']}".strip()
    return name or "(no name)"


def _find_gamepad(devices: list) -> dict | None:
    """Return first device with gamepad/joystick HID usage, or None."""
    for usage in ((_USAGE_PAGE_GENERIC, _USAGE_GAMEPAD),
                  (_USAGE_PAGE_GENERIC, _USAGE_JOYSTICK)):
        for d in devices:
            if (d["usage_page"], d["usage"]) == usage:
                return d
    return None


def _open(d: dict):
    """Open a HID device in non-blocking mode."""
    h = _hid().device()
    h.open_path(d["path"])
    h.set_nonblocking(True)
    return h


def _select(index: int | None) -> dict:
    devices = _enumerate()
    if index is not None:
        if index >= len(devices):
            print(f"Device index {index} out of range (max {len(devices)-1}).")
            sys.exit(1)
        return devices[index]
    d = _find_gamepad(devices)
    if d is None:
        print("No gamepad / joystick found.  Try:  python gamepad_tester.py list")
        sys.exit(1)
    return d


# ─── Modes ────────────────────────────────────────────────────────────────────

def mode_list():
    """Print all HID devices."""
    devices = _enumerate()
    if not devices:
        print("No HID devices found.")
        return

    print(f"\n  {'#':>3}  {'VID:PID':<10}  {'UP:U':<10}  Name")
    print("  " + "─" * 72)
    for i, d in enumerate(devices):
        gp_tag = " ← gamepad" if (d["usage_page"], d["usage"]) in _GAMEPAD_USAGES else ""
        print(f"  {i:3d}  {d['vendor_id']:04X}:{d['product_id']:04X}  "
              f"{d['usage_page']:04X}:{d['usage']:04X}  "
              f"{_device_label(d)}{gp_tag}")
    print()


def mode_probe(index: int | None = None):
    """Dump raw HID reports with hex + binary, highlighting changed bytes."""
    d   = _select(index)
    dev = _open(d)
    print(f"\n  Device : {_device_label(d)}")
    print(f"  VID:PID: {d['vendor_id']:04X}:{d['product_id']:04X}\n")
    print("  Press buttons (Ctrl+C to stop)\n")
    print("  Hex bytes")
    print("  " + "─" * 52)

    prev  = None
    count = 0
    try:
        while True:
            data = dev.read(64)
            if data:
                b = bytes(data)
                if b != prev:
                    changed_idx = set() if prev is None else {
                        i for i, (a, c) in enumerate(zip(prev, b)) if a != c
                    }
                    hex_parts = []
                    for i, byte in enumerate(b):
                        s = f"{byte:02x}"
                        hex_parts.append(f"[{s}]" if i in changed_idx else f" {s} ")
                    count += 1
                    print(f"  {''.join(hex_parts)}")
                    prev = b
            time.sleep(0.005)
    except KeyboardInterrupt:
        print(f"\n  {count} unique reports captured.")
    finally:
        dev.close()


def mode_test(index: int | None = None):
    """Live button/axis dashboard — tracks changes from idle baseline."""
    d   = _select(index)
    dev = _open(d)
    print(f"\n  Device : {_device_label(d)}")
    print(f"  VID:PID: {d['vendor_id']:04X}:{d['product_id']:04X}\n")
    print("  Release all buttons, then press Enter to capture baseline...")
    input()

    # Drain buffer, then take baseline
    time.sleep(0.1)
    for _ in range(20):
        dev.read(64)
    time.sleep(0.05)
    baseline_raw = None
    for _ in range(30):
        data = dev.read(64)
        if data:
            baseline_raw = bytes(data)
            break
        time.sleep(0.02)

    if baseline_raw is None:
        print("  No data from device. Make sure it is connected and active.")
        dev.close()
        return

    print(f"  Baseline: {baseline_raw.hex(' ')}\n")
    print("  Press buttons. Ctrl+C to stop.\n")

    is_g100   = (d["vendor_id"] == G100_VID and d["product_id"] == G100_PID)
    btn_map   = G100_BUTTONS if is_g100 else {}
    axis_defs = {ax[0]: (ax[1], ax[2]) for ax in G100_AXES} if is_g100 else {}

    # Fallback heuristic axis detection for unknown devices
    n = len(baseline_raw)
    heuristic_axes = (set() if is_g100
                      else {i for i, v in enumerate(baseline_raw) if 60 < v < 200})

    pressed: dict[tuple[int, int], bool] = {}

    prev = baseline_raw
    try:
        while True:
            data = dev.read(64)
            if not data:
                time.sleep(0.005)
                continue

            b = bytes(data)
            if b == prev:
                continue

            events: list[str] = []

            for byte_i in range(min(n, len(b))):
                if b[byte_i] == prev[byte_i]:
                    continue

                if is_g100 and byte_i == G100_DPAD_BYTE:
                    direction = G100_DPAD.get(b[byte_i])
                    if direction:
                        events.append(f"PRESS   D-pad {direction}")
                    else:
                        events.append(f"release D-pad")
                elif byte_i in axis_defs:
                    label, center = axis_defs[byte_i]
                    delta = b[byte_i] - center
                    events.append(f"axis  {label}={b[byte_i]:3d} (Δ{delta:+d})")
                elif byte_i in heuristic_axes:
                    base_val = baseline_raw[byte_i]
                    delta    = b[byte_i] - base_val
                    events.append(f"axis  [{byte_i}]={b[byte_i]:3d} (Δ{delta:+d})")
                else:
                    old, new = prev[byte_i], b[byte_i]
                    diff     = old ^ new
                    for bit_i in range(8):
                        if diff & (1 << bit_i):
                            key      = (byte_i, bit_i)
                            is_press = bool(new & (1 << bit_i))
                            pressed[key] = is_press
                            label    = btn_map.get(key, f"btn[{byte_i}.{bit_i}]")
                            events.append(f"{'PRESS  ' if is_press else 'release'} {label}")

            for ev in events:
                print(f"  {ev}")

            prev = b
    except KeyboardInterrupt:
        total = sum(1 for v in pressed.values() if v)
        print(f"\n  Stopped. Currently held: {total} button(s).")
    finally:
        dev.close()


def mode_learn(index: int | None = None):
    """Guided mode: press each button to assign a name, then show named states."""
    d   = _select(index)
    dev = _open(d)
    print(f"\n  Device : {_device_label(d)}")
    print(f"  VID:PID: {d['vendor_id']:04X}:{d['product_id']:04X}\n")
    print("  Release all buttons, then press Enter to capture baseline...")
    input()

    time.sleep(0.1)
    for _ in range(20):
        dev.read(64)
    time.sleep(0.05)
    baseline_raw = None
    for _ in range(30):
        data = dev.read(64)
        if data:
            baseline_raw = bytes(data)
            break
        time.sleep(0.02)

    if baseline_raw is None:
        print("  No data from device.")
        dev.close()
        return

    n         = len(baseline_raw)
    axis_bytes = {i for i, v in enumerate(baseline_raw) if 60 < v < 200}
    labels: dict[tuple[int, int], str] = {}

    print(f"  Baseline: {baseline_raw.hex(' ')}\n")
    print("  Now name your buttons.  For each button: press it and enter a name.")
    print("  Type 'done' when finished.\n")

    prev = baseline_raw
    pending_bits: set[tuple[int, int]] = set()

    try:
        while True:
            # Collect pressed bits
            for _ in range(10):
                data = dev.read(64)
                if data:
                    b = bytes(data)
                    if b != prev:
                        for byte_i in range(min(n, len(b))):
                            if byte_i in axis_bytes:
                                continue
                            diff = prev[byte_i] ^ b[byte_i]
                            for bit_i in range(8):
                                if diff & (1 << bit_i):
                                    key = (byte_i, bit_i)
                                    if b[byte_i] & (1 << bit_i):
                                        pending_bits.add(key)
                        prev = b
                time.sleep(0.01)

            if pending_bits:
                for key in list(pending_bits):
                    if key not in labels:
                        default = f"btn[{key[0]}.{key[1]}]"
                        raw     = input(f"  Detected {default} — name (or Enter to keep): ").strip()
                        labels[key] = raw or default
                pending_bits.clear()

            cmd = input("  Press next button, or type 'done': ").strip().lower()
            if cmd == "done":
                break

    except KeyboardInterrupt:
        pass

    if not labels:
        print("  No buttons named.")
        dev.close()
        return

    print(f"\n  Mapped {len(labels)} button(s). Starting live display (Ctrl+C to stop).\n")

    # Live display with named buttons
    prev      = baseline_raw
    btn_state = {k: False for k in labels}

    def _render():
        os.system("cls" if os.name == "nt" else "clear")
        print(f"  Device: {_device_label(d)}\n")
        for key, name in labels.items():
            state = "[ PRESSED ]" if btn_state[key] else "[         ]"
            print(f"  {state}  {name}")
        print("\n  Ctrl+C to stop.")

    _render()
    try:
        while True:
            data = dev.read(64)
            if not data:
                time.sleep(0.005)
                continue
            b = bytes(data)
            if b == prev:
                continue

            changed = False
            for byte_i in range(min(n, len(b))):
                diff = prev[byte_i] ^ b[byte_i]
                for bit_i in range(8):
                    if diff & (1 << bit_i):
                        key = (byte_i, bit_i)
                        if key in btn_state:
                            btn_state[key] = bool(b[byte_i] & (1 << bit_i))
                            changed = True
            prev = b
            if changed:
                _render()
    except KeyboardInterrupt:
        print("\n  Done.")
    finally:
        dev.close()


# ─── BLE mode (Consumer Controls buttons) ────────────────────────────────────

# Consumer Controls HID usages → button name
_CC_USAGES: dict[int, str] = {
    0x0063: "Play",
    0x0221: "SalutLogo",
    0x0223: "Home",
    0x0224: "Back",
}

_HID_REPORT_UUID = "00002a4d-0000-1000-8000-00805f9b34fb"
_DEVICE_NAME_BLE = "GAME"


def _decode_cc_report(data: bytes) -> str | None:
    """Decode a Consumer Controls HID report to button name, None if release/unknown."""
    if not data or all(b == 0 for b in data):
        return None
    # Usage code is LE uint16 (or uint8 for single-byte usages)
    usage = int.from_bytes(data[:2], "little") if len(data) >= 2 else data[0]
    if usage == 0:
        return None
    return _CC_USAGES.get(usage, f"CC_0x{usage:04X}")


async def _ble_run(mac: str | None, seconds: int) -> None:
    try:
        from bleak import BleakClient, BleakScanner
    except ImportError:
        print("Install bleak:  pip install bleak")
        return

    # On Linux: device may already be connected as HID (not advertising).
    # Build BLEDevice directly from BlueZ D-Bus objects — no scan needed.
    # On Windows: scan first (WinRT hides HID service so device must be un-paired).
    _TARGET_MAC = "F4:22:7A:4A:AA:E0"
    if sys.platform == "linux":
        import dbus
        from bleak.backends.bluezdbus.scanner import BleakScannerBlueZDBus
        addr = (mac or _TARGET_MAC).upper()
        print(f"  Linux: looking up {addr} in BlueZ D-Bus objects...")
        bus = dbus.SystemBus()
        manager = dbus.Interface(
            bus.get_object("org.bluez", "/"),
            "org.freedesktop.DBus.ObjectManager",
        )
        objects = manager.GetManagedObjects()
        ble_dev = None
        for path, ifaces in objects.items():
            dev_iface = ifaces.get("org.bluez.Device1", {})
            if str(dev_iface.get("Address", "")).upper() == addr:
                from bleak.backends.device import BLEDevice
                details = {"path": path, "props": dev_iface}
                ble_dev = BLEDevice(addr, str(dev_iface.get("Name", addr)), details, -1)
                print(f"  Found in D-Bus: {path}")
                break
        if ble_dev is None:
            print(f"  Not found in D-Bus — aborting.")
            return
        client = BleakClient(ble_dev, timeout=15.0)
    else:
        print(f"  Scanning for '{_DEVICE_NAME_BLE}'...")
        dev = await BleakScanner.find_device_by_name(_DEVICE_NAME_BLE, timeout=10.0)
        if dev is None:
            if mac:
                print(f"  Not in scan — WinRT bypass for {mac}")
                addr_int = int(mac.replace(":", ""), 16)
                client   = BleakClient(mac, timeout=15.0)
                client._backend._device_info = addr_int
            else:
                print("  Device not found. Make sure it is removed from Windows Bluetooth and is advertising.")
                return
        else:
            print(f"  Found: {dev.address}")
            client = BleakClient(dev, timeout=15.0)

    def on_report(sender, data: bytearray) -> None:
        b    = bytes(data)
        name = _decode_cc_report(b)
        tag  = f"← {name}" if name else ""
        print(f"  [{sender.uuid[:8]}]  {b.hex(' ')}  {tag}")

    await client.connect()
    print(f"  Connected: {client.address}")

    # On Linux the device is already bonded — skip pair()/bonding wait.
    if sys.platform != "linux":
        loop = asyncio.get_running_loop()
        try:
            await client.pair()
        except Exception as e:
            print(f"  pair() note: {e}")

        print("  Bonding", end="", flush=True)
        deadline = loop.time() + 30.0
        _CHAR_MODEL_UUID = "00002a24-0000-1000-8000-00805f9b34fb"
        while loop.time() < deadline:
            try:
                await client.read_gatt_char(_CHAR_MODEL_UUID)
                break
            except Exception:
                print(".", end="", flush=True)
                await asyncio.sleep(1.0)
    print(" done.\n")

    try:
        # Dump service/char tree
        for svc in client.services:
            print(f"  SVC {svc.uuid[:8]}")
            for ch in svc.characteristics:
                print(f"      {ch.uuid[:8]}  [{'/'.join(ch.properties)}]")

        # Subscribe to ALL notify/indicate characteristics
        subscribed = []
        for svc in client.services:
            for ch in svc.characteristics:
                if set(ch.properties) & {"notify", "indicate"}:
                    try:
                        await client.start_notify(ch.uuid, on_report)
                        subscribed.append(ch.uuid[:8])
                    except Exception as e:
                        print(f"  Could not subscribe {ch.uuid[:8]}: {e}")

        print(f"\n  Subscribed: {subscribed}")
        print(f"  Press ANY button (Ctrl+C or {seconds}s timeout)\n")

        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            pass
    finally:
        try:
            await client.unpair()
            print("\n  Unpaired.")
        except Exception:
            pass
        await client.disconnect()


def mode_ble(mac: str | None = None, seconds: int = 60) -> None:
    """Intercept Consumer Controls buttons (Back/Home/SalutLogo/Play) via BLE GATT."""
    import threading

    exc: BaseException | None = None

    def _run():
        nonlocal exc
        try:
            asyncio.run(_ble_run(mac, seconds))
        except KeyboardInterrupt:
            pass
        except BaseException as e:
            exc = e

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    try:
        t.join()
    except KeyboardInterrupt:
        print("\n  Stopped.")

    if exc:
        raise exc


# ─── BLE command probe ────────────────────────────────────────────────────────

_PROBE_COMMANDS: list[tuple[str, bytes]] = [
    ("enable_1b",     b"\x01"),
    ("enable_03",     b"\x03"),
    ("get_caps_0a",   b"\x0a"),
    ("enable_0100",   b"\x01\x00"),
    ("enable_0300",   b"\x03\x00"),
    ("enable_01x4",   b"\x01\x00\x00\x00"),
    ("enable_03x4",   b"\x03\x00\x00\x00"),
    ("get_caps_0ax4", b"\x0a\x00\x00\x00"),
    ("start_0200",    b"\x02\x00"),
    ("start_0201",    b"\x02\x01"),
    ("magic_ff01",    b"\xff\x01"),
    ("zeros_16",      bytes(16)),
]

# Commands to try on 6487 AFTER confirming GET_CAPS (0x0a) works.
# Goal: find which command activates button reporting on a001 or 6487.
_PROBE_6487_START: list[tuple[str, bytes]] = [
    # single-byte sweep
    ("0x00",        b"\x00"),
    ("0x01",        b"\x01"),
    ("0x02",        b"\x02"),
    ("0x03",        b"\x03"),
    ("0x04",        b"\x04"),
    ("0x05",        b"\x05"),
    ("0x06",        b"\x06"),
    ("0x07",        b"\x07"),
    ("0x08",        b"\x08"),
    ("0x09",        b"\x09"),
    ("0x0b",        b"\x0b"),
    ("0x0c",        b"\x0c"),
    ("0x0d",        b"\x0d"),
    ("0x0e",        b"\x0e"),
    ("0x0f",        b"\x0f"),
    ("0x10",        b"\x10"),
    ("0x11",        b"\x11"),
    ("0x12",        b"\x12"),
    ("0x20",        b"\x20"),
    ("0x40",        b"\x40"),
    ("0x80",        b"\x80"),
    ("0xff",        b"\xff"),
    # two-byte variants
    ("0a_01",       b"\x0a\x01"),
    ("0a_08",       b"\x0a\x08"),
    ("01_01",       b"\x01\x01"),
    ("01_08",       b"\x01\x08"),
    ("03_01",       b"\x03\x01"),
    ("10_01",       b"\x10\x01"),
    ("10_08",       b"\x10\x08"),
    ("11_01",       b"\x11\x01"),
    # response-echo style: send what device said back
    ("echo_caps",   b"\x10\x0a\x08\x00\x00\x00\x00"),
    # three-byte
    ("0a_01_00",    b"\x0a\x01\x00"),
    ("01_00_01",    b"\x01\x00\x01"),
]


async def _ble_connect_bond(dev):
    """Connect and wait for bonding. Returns connected client."""
    from bleak import BleakClient
    client = BleakClient(dev, timeout=15.0)
    await client.connect()
    print(f"  Connected: {client.address}")
    try:
        await client.pair()
    except Exception as e:
        print(f"  pair() note: {e}")
    _CHAR_MODEL_UUID = "00002a24-0000-1000-8000-00805f9b34fb"
    loop = asyncio.get_running_loop()
    print("  Bonding", end="", flush=True)
    deadline = loop.time() + 30.0
    while loop.time() < deadline:
        try:
            await client.read_gatt_char(_CHAR_MODEL_UUID)
            break
        except Exception:
            print(".", end="", flush=True)
            await asyncio.sleep(1.0)
    print(" done.")
    return client


async def _ble_probe_run(mac: str | None) -> None:
    """Connect, subscribe to all notify chars, then probe all write channels for activation command."""
    try:
        from bleak import BleakClient, BleakScanner
    except ImportError:
        print("Install bleak:  pip install bleak")
        return

    # Chars known to cause problems — skip writing to them
    # ffd1 causes immediate disconnect when written to
    _SKIP_WRITE_PREFIXES = {"0000ffd1", "0000ffd8"}

    print(f"  Scanning for '{_DEVICE_NAME_BLE}'...")
    dev = await BleakScanner.find_device_by_name(_DEVICE_NAME_BLE, timeout=10.0)
    if dev is None:
        print("  Device not found.")
        return
    print(f"  Found: {dev.address}")

    client = await _ble_connect_bond(dev)
    hits: list[str] = []

    def on_report(sender, data: bytearray) -> None:
        b = bytes(data)
        if all(x == 0 for x in b):
            return  # skip zero (initial state)
        name = _decode_cc_report(b)
        tag  = f" ← {name}" if name else ""
        print(f"\n  *** DATA [{sender.uuid[:8]}]  {b.hex(' ')}{tag}  ***", flush=True)
        hits.append(b.hex(' '))

    try:
        # Read all readable characteristics in vendor services
        print()
        vendor_svcs = {"0000d0ff", "0000d1ff", "00006287"}
        for svc in client.services:
            if svc.uuid[:8] not in vendor_svcs:
                continue
            for ch in svc.characteristics:
                if "read" in ch.properties:
                    try:
                        val = await client.read_gatt_char(ch.uuid)
                        print(f"  READ {ch.uuid[:8]}: {bytes(val).hex(' ')}")
                    except Exception as e:
                        print(f"  READ {ch.uuid[:8]}: {e}")

        # Collect all writable characteristics dynamically (skip sensitive ones)
        write_targets = []
        for svc in client.services:
            for ch in svc.characteristics:
                if ch.uuid[:8] in _SKIP_WRITE_PREFIXES:
                    continue
                if set(ch.properties) & {"write", "write-without-response"}:
                    write_targets.append(ch)

        print(f"\n  Write targets: {[ch.uuid[:8] for ch in write_targets]}")

        # Subscribe to all notify chars
        subscribed = []
        for svc in client.services:
            for ch in svc.characteristics:
                if set(ch.properties) & {"notify", "indicate"}:
                    try:
                        await client.start_notify(ch.uuid, on_report)
                        subscribed.append(ch.uuid[:8])
                    except Exception as e:
                        print(f"  skip {ch.uuid[:8]}: {e}")
        print(f"  Subscribed:    {subscribed}\n")

        # ── Baseline listen: does a001 send data without any command? ─────────
        hits.clear()  # discard anything received during connection/subscription
        print("  ── Baseline (10s): press buttons NOW, no command sent ──")
        await asyncio.sleep(10.0)
        if hits:
            print(f"  → a001 sends data NATIVELY ({len(hits)} packets). No START command needed!")
            print("  → Proceeding to live listen mode (60s). Keep pressing buttons.\n")
            await asyncio.sleep(60.0)
            return  # done — no need to probe commands
        else:
            print("  → No data without command. Probing activation commands...\n")
        hits.clear()

        # ── Phase 1: find which write target responds at all ──────────────────
        ch6487: object | None = None
        for ch in write_targets:
            if ch.uuid[:8] == "00006487":
                ch6487 = ch
                break

        phase1_hit_cmd: str | None = None
        for target_ch in write_targets:
            if not client.is_connected:
                print("  Device disconnected — reconnecting...")
                client = await _ble_connect_bond(dev)
                for svc in client.services:
                    for ch in svc.characteristics:
                        if set(ch.properties) & {"notify", "indicate"}:
                            try:
                                await client.start_notify(ch.uuid, on_report)
                            except Exception:
                                pass

            use_response = "write" in target_ch.properties
            print(f"\n  ═══ Target: {target_ch.uuid[:8]} ({'/'.join(target_ch.properties)}) ═══")

            for label, cmd in _PROBE_COMMANDS:
                if not client.is_connected:
                    print(f"  Disconnected — skipping rest of {target_ch.uuid[:8]}")
                    break
                print(f"  → [{label:16s}] {cmd.hex(' ')}", end="  ", flush=True)
                try:
                    await client.write_gatt_char(target_ch.uuid, cmd, response=use_response)
                    print("sent", flush=True)
                except Exception as e:
                    print(f"err: {e}", flush=True)
                    continue
                await asyncio.sleep(2.5)
                if hits:
                    phase1_hit_cmd = label
                    print(f"\n  ✓ Got response after [{label}] on {target_ch.uuid[:8]}!")
                    break
            if hits:
                break

        # ── Phase 2: if 6487/GET_CAPS confirmed, probe for START command ──────
        if phase1_hit_cmd == "get_caps_0a" and ch6487 is not None:
            hits.clear()
            print(f"\n{'═'*60}")
            print(f"  Phase 2: probing 6487 for START command (press buttons!)")
            print(f"{'═'*60}\n")

            for label, cmd in _PROBE_6487_START:
                if not client.is_connected:
                    print("  Device disconnected — stopping phase 2.")
                    break
                print(f"  → [{label:16s}] {cmd.hex(' ')}", end="  ", flush=True)
                try:
                    await client.write_gatt_char(ch6487.uuid, cmd, response=False)
                    print("sent", flush=True)
                except Exception as e:
                    print(f"err: {e}", flush=True)
                    continue
                await asyncio.sleep(2.5)
                if hits:
                    print(f"\n  ✓ BUTTON DATA after [{label}]! Command = {cmd.hex(' ')}")
                    break

            if not hits:
                print("\n  Phase 2: no START command found in this sweep.")
        elif not hits:
            print("\n  No activation command found.")

    finally:
        try:
            await client.unpair()
            print("\n  Unpaired.")
        except Exception:
            pass
        if client.is_connected:
            await client.disconnect()


def mode_ble_probe(mac: str | None = None) -> None:
    """Probe BLE write channels to find command that activates button reporting."""
    import threading

    exc: BaseException | None = None

    def _run():
        nonlocal exc
        try:
            asyncio.run(_ble_probe_run(mac))
        except KeyboardInterrupt:
            pass
        except BaseException as e:
            exc = e

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    try:
        t.join()
    except KeyboardInterrupt:
        print("\n  Stopped.")

    if exc:
        raise exc


# ─── evdev mode (Linux: reads BlueZ input devices directly) ──────────────────

# ecodes for Consumer Control keys that BlueZ maps from CC HID reports
_CC_KEY_NAMES: dict[int, str] = {
    # filled dynamically from evdev.ecodes at runtime
}

def _build_cc_key_names() -> dict[int, str]:
    """Map evdev key codes to friendly button names for the G100 CC buttons."""
    try:
        from evdev import ecodes
    except ImportError:
        return {}
    mapping = {
        ecodes.KEY_HOMEPAGE:   "Home",
        ecodes.KEY_BACK:       "Back",
        ecodes.KEY_PLAYPAUSE:  "Play",
    }
    # Play/SalutLogo may come as KEY_VCR / KEY_SEARCH depending on BlueZ version
    for attr, name in [("KEY_VCR", "Play"), ("KEY_SEARCH", "SalutLogo"),
                       ("KEY_MEDIA", "Play"), ("KEY_PROPS", "SalutLogo")]:
        code = getattr(ecodes, attr, None)
        if code is not None:
            mapping[code] = name
    return mapping


_G100_BTN_NAMES: dict[int, str] = {
    # BTN_SOUTH/EAST/NORTH/WEST → face buttons
    0x130: "A",        # BTN_SOUTH
    0x131: "B",        # BTN_EAST
    0x133: "X",        # BTN_NORTH
    0x134: "Y",        # BTN_WEST
    # Bumpers / triggers
    0x136: "L1",       # BTN_TL
    0x137: "R1",       # BTN_TR
    0x138: "L2",       # BTN_TL2
    0x139: "R2",       # BTN_TR2
    # Center buttons
    0x13a: "Share",    # BTN_SELECT
    0x13b: "Options",  # BTN_START
    # Stick clicks
    0x13d: "L3",       # BTN_THUMBL
    0x13e: "R3",       # BTN_THUMBR
}

_G100_AXIS_NAMES: dict[int, str] = {
    0x00: "LX",        # ABS_X
    0x01: "LY",        # ABS_Y
    0x02: "RX",        # ABS_Z
    0x05: "RY",        # ABS_RZ
    0x0a: "L2",        # ABS_BRAKE
    0x09: "R2",        # ABS_GAS
    0x10: "D-pad X",   # ABS_HAT0X
    0x11: "D-pad Y",   # ABS_HAT0Y
}


def mode_evdev(gamepad_path: str = "/dev/input/event13",
               cc_path: str     = "/dev/input/event12") -> None:
    """Read all buttons via BlueZ evdev devices (Linux only).

    event13 / js0  — regular buttons (A/B/X/Y, sticks, D-pad, L1/R1/L2/R2…)
    event12        — Consumer Controls (Home, Back, Play, SalutLogo)
    """
    try:
        import evdev
        from evdev import categorize, ecodes
    except ImportError:
        print("Install python3-evdev:  sudo apt install python3-evdev")
        return

    import selectors

    cc_key_names = _build_cc_key_names()

    devices = {}
    axis_zones: dict[int, int] = {}  # axis_code → last zone (-1, 0, 1)

    def _axis_zone(value: int, mn: int, mx: int) -> int:
        third = (mx - mn) // 3
        if value < mn + third:
            return -1
        if value > mx - third:
            return 1
        return 0

    for path, label in [(gamepad_path, "GAMEPAD"), (cc_path, "CC")]:
        try:
            d = evdev.InputDevice(path)
            devices[d.fd] = (d, label)
            print(f"  {label}: {d.name} ({path})")
        except PermissionError:
            print(f"  {label}: Permission denied — run: sudo usermod -aG input $USER")
        except FileNotFoundError:
            print(f"  {label}: {path} not found — is the gamepad connected?")

    if not devices:
        return

    print("  Press buttons (Ctrl+C to stop)\n")

    sel = selectors.DefaultSelector()
    for fd, (dev, _) in devices.items():
        sel.register(fd, selectors.EVENT_READ)

    try:
        while True:
            for key, _ in sel.select(timeout=1.0):
                dev, label = devices[key.fd]
                for event in dev.read():
                    if event.type == ecodes.EV_KEY:
                        key_event = categorize(event)
                        state     = key_event.keystate  # 0=up, 1=down, 2=hold
                        if state != 1:
                            continue  # skip release and auto-repeat
                        code = key_event.scancode
                        name = (cc_key_names.get(code)
                                or _G100_BTN_NAMES.get(code)
                                or ecodes.KEY.get(code, f"KEY_{code:#05x}"))
                        print(f"  [{label}] {name}")
                    elif event.type == ecodes.EV_ABS:
                        info  = dev.absinfo(event.code)
                        center = (info.max + info.min) // 2
                        dead   = max(8, (info.max - info.min) // 20)
                        delta  = event.value - center
                        # D-pad hat: show direction, skip release
                        if event.code in (0x10, 0x11):
                            if event.value != 0:
                                direction = ("Right" if event.value > 0 else "Left") if event.code == 0x10 else ("Down" if event.value > 0 else "Up")
                                print(f"  [{label}] D-pad {direction}")
                        elif event.code in (0x09, 0x0a):
                            # L2/R2 analog triggers: report half/full threshold crossings only
                            axis_name = _G100_AXIS_NAMES.get(event.code, "L2" if event.code == 0x0a else "R2")
                            val = event.value
                            prev_val = getattr(dev, f"_prev_{event.code}", 0)
                            setattr(dev, f"_prev_{event.code}", val)
                            half, full = info.max // 2, info.max
                            if prev_val < half <= val:
                                print(f"  [{label}] {axis_name} ~half")
                            elif prev_val < full <= val:
                                print(f"  [{label}] {axis_name} full")
                        else:
                            axis_name = _G100_AXIS_NAMES.get(event.code) or ecodes.ABS.get(event.code, f"ABS_{event.code}")
                            zone = _axis_zone(event.value, info.min, info.max)
                            if axis_zones.get(event.code) != zone:
                                axis_zones[event.code] = zone
                                direction = {-1: "-", 0: "·", 1: "+"}[zone]
                                print(f"  [{label}] {axis_name} {direction}")
    except KeyboardInterrupt:
        print("\n  Stopped.")
    finally:
        for dev, _ in devices.values():
            dev.close()
        sel.close()


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main():
    args  = sys.argv[1:]
    cmd   = args[0] if args else "test"
    index = int(args[1]) if len(args) > 1 and args[1].isdigit() else None

    if cmd == "list":
        mode_list()
    elif cmd == "probe":
        mode_probe(index)
    elif cmd == "test":
        mode_test(index)
    elif cmd == "learn":
        mode_learn(index)
    elif cmd == "ble":
        # optional: ble <MAC>  e.g.  ble F4:22:7A:4A:A7:E6
        mac = args[1] if len(args) > 1 and ":" in args[1] else None
        mode_ble(mac)
    elif cmd == "bleprobe":
        mac = args[1] if len(args) > 1 and ":" in args[1] else None
        mode_ble_probe(mac)
    elif cmd == "evdev":
        # optional: evdev <gamepad_path> <cc_path>
        gp = args[1] if len(args) > 1 and args[1].startswith("/") else "/dev/input/event13"
        cc = args[2] if len(args) > 2 and args[2].startswith("/") else "/dev/input/event12"
        mode_evdev(gp, cc)
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
