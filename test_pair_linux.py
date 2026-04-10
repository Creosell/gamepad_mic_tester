#!/usr/bin/env python3
"""
Linux BLE pairing/unpairing test — Realtek G100

Registers a BlueZ D-Bus pairing agent (NoInputNoOutput) that auto-accepts
RequestAuthorization, then waits for the device to initiate reverse pairing.

Usage:
    python test_pair_linux.py
    python test_pair_linux.py --no-unpair   # keep bonded after test
"""

import argparse
import asyncio
import glob
import io
import os
import subprocess
import sys
from pathlib import Path

from bleak import BleakClient, BleakScanner

DEVICE_NAME   = "GAME"
KNOWN_ADDRESS = "F4:22:7A:4A:AA:E0"

_CHAR_MODEL   = "00002a24-0000-1000-8000-00805f9b34fb"
_CHAR_BATTERY = "00002a19-0000-1000-8000-00805f9b34fb"

AB5E_CMD   = "ab5e0002-5a21-4f05-bc7d-af01f617b664"
AB5E_AUDIO = "ab5e0003-5a21-4f05-bc7d-af01f617b664"
AB5E_RESP  = "ab5e0004-5a21-4f05-bc7d-af01f617b664"

LOG_DIR      = Path("logs")
RECORD_SECS  = 5

# VID:PID of the gamepad — used to locate its hid-generic sysfs node
_HID_VID = "1D5A"
_HID_PID = "1802"
_HOG_UNBIND = "/sys/bus/hid/drivers/hid-generic/unbind"


# ── HOG unbind ────────────────────────────────────────────────────────────────

def _wait_and_unbind_hog(timeout: float = 8.0, interval: float = 0.3) -> None:
    """Poll until hid-generic node appears for our gamepad, then unbind it."""
    import time
    pattern  = f"/sys/bus/hid/drivers/hid-generic/0005:{_HID_VID}:{_HID_PID}.*"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if glob.glob(pattern):
            _unbind_hog()
            return
        time.sleep(interval)
    print("    [hog] hid-generic did not appear within timeout — skipping")


def _unbind_hog() -> None:
    """Unbind hid-generic for every connected instance of our gamepad.

    BlueZ's input/HOG plugin races with Bleak for GATT handles and causes
    bluetoothd to segfault.  Unbinding the kernel HID driver immediately after
    connect hands full GATT control back to our process.

    The node disappears automatically when the device disconnects, so no
    rebind is needed at unpair time.

    Writes to /sys require root; falls back to sudo if needed.
    """
    pattern = f"/sys/bus/hid/drivers/hid-generic/0005:{_HID_VID}:{_HID_PID}.*"
    nodes   = glob.glob(pattern)

    if not nodes:
        print("    [hog] no hid-generic binding found — nothing to unbind")
        return

    for node in nodes:
        dev_id = os.path.basename(node)
        print(f"    [hog] unbinding {dev_id}...")
        try:
            with open(_HOG_UNBIND, "w") as f:
                f.write(dev_id)
            print("    [hog] unbound OK")
        except PermissionError:
            try:
                subprocess.run(
                    ["sudo", "sh", "-c", f"echo {dev_id} > {_HOG_UNBIND}"],
                    check=True,
                )
                print("    [hog] unbound OK (via sudo)")
            except subprocess.CalledProcessError as e:
                print(f"    [hog] unbind failed: {e}")

# ── Audio ─────────────────────────────────────────────────────────────────────

async def _sniff_all(client: BleakClient, seconds: int = 8) -> None:
    """Subscribe to every notifiable characteristic, send START, dump everything received."""
    received: dict[str, list[bytes]] = {}

    def make_cb(uuid: str):
        received[uuid] = []
        def cb(sender, data: bytearray):
            b = bytes(data)
            received[uuid].append(b)
            print(f"    [{uuid[:8]}] #{len(received[uuid]):3d}  {len(b):3d}B  {b.hex()[:48]}")
        return cb

    subs = []
    for svc in client.services:
        for ch in svc.characteristics:
            if "notify" in ch.properties or "indicate" in ch.properties:
                try:
                    await client.start_notify(ch.uuid, make_cb(ch.uuid))
                    subs.append(ch.uuid)
                    print(f"    subscribed: {ch.uuid}")
                except Exception as e:
                    print(f"    subscribe failed {ch.uuid}: {e}")

    print(f"    Sending GET_CAPS + START, listening {seconds}s...")
    try:
        await client.write_gatt_char(AB5E_CMD, bytes([0x0C, 0x00]), response=False)
        await asyncio.sleep(0.5)
        await client.write_gatt_char(AB5E_CMD, bytes([0x0A, 0x00]), response=False)
        await asyncio.sleep(seconds)
        await client.write_gatt_char(AB5E_CMD, bytes([0x0B, 0x00]), response=False)
    except Exception as e:
        print(f"    write error: {e}")

    for uuid in subs:
        try:
            await client.stop_notify(uuid)
        except Exception:
            pass

    print("\n    ── Sniff summary ──")
    for uuid, frames in received.items():
        if frames:
            sizes = sorted(set(len(f) for f in frames))
            print(f"    {uuid[:8]}  {len(frames):4d} frames  sizes={sizes}")


async def _warmup(client: BleakClient) -> None:
    """GET_CAPS → START → STOP burn-in cycle.
    First START after pairing produces no audio; this primes the pipeline.
    """
    ready = asyncio.Event()

    def on_resp(sender, data: bytearray):
        if data and data[0] == 0x0C:
            ready.set()

    await client.start_notify(AB5E_RESP, on_resp)
    await client.write_gatt_char(AB5E_CMD, bytes([0x0C, 0x00]), response=False)
    try:
        await asyncio.wait_for(ready.wait(), timeout=5.0)
        print("    GET_CAPS acknowledged")
    except asyncio.TimeoutError:
        print("    GET_CAPS timeout — continuing anyway")
    await client.write_gatt_char(AB5E_CMD, bytes([0x0A, 0x00]), response=False)
    await asyncio.sleep(0.2)
    await client.write_gatt_char(AB5E_CMD, bytes([0x0B, 0x00]), response=False)
    await asyncio.sleep(0.3)
    await client.stop_notify(AB5E_RESP)
    print("    Warm-up done.")


async def _capture_audio(client: BleakClient, seconds: int) -> list[bytes]:
    """GET_CAPS + START, collect AB5E_AUDIO frames, STOP. Returns raw frames."""
    frames: list[bytes] = []
    recording      = False
    caps_received  = asyncio.Event()

    def on_audio(sender, data: bytearray):
        if recording:
            frames.append(bytes(data))

    def on_resp(sender, data: bytearray):
        if data:
            caps_received.set()

    await client.start_notify(AB5E_AUDIO, on_audio)
    await client.start_notify(AB5E_RESP,  on_resp)

    await client.write_gatt_char(AB5E_CMD, bytes([0x0C, 0x00]), response=False)
    try:
        await asyncio.wait_for(caps_received.wait(), timeout=5.0)
    except asyncio.TimeoutError:
        print("    GET_CAPS timeout — starting anyway")

    recording = True
    await client.write_gatt_char(AB5E_CMD, bytes([0x0A, 0x00]), response=False)

    for remaining in range(seconds, 0, -1):
        print(f"\r    Recording: {remaining}s  ", end="", flush=True)
        await asyncio.sleep(1)
    print("\r    Recording done.      ")

    recording = False
    for stop in (bytes([0x0B, 0x00]), bytes([0x00, 0x00])):
        try:
            await client.write_gatt_char(AB5E_CMD, stop, response=False)
            await asyncio.sleep(0.05)
        except Exception:
            pass

    await client.stop_notify(AB5E_AUDIO)
    await client.stop_notify(AB5E_RESP)
    return frames


def _convert_and_play(frames: list[bytes], mac: str) -> None:
    """Strip 6-byte packet headers → IMA ADPCM → WAV via sox → play."""
    LOG_DIR.mkdir(exist_ok=True)
    safe_mac = mac.replace(":", "")
    ima_path = LOG_DIR / f"test_{safe_mac}.ima"
    wav_path = LOG_DIR / f"test_{safe_mac}.wav"

    ima_path.write_bytes(b"".join(f[6:] for f in frames if len(f) > 6))
    print(f"    IMA: {ima_path}  ({len(frames)} frames)")

    try:
        subprocess.run(
            ["sox", "-t", "ima", "-e", "ima-adpcm", "-r", "16000", str(ima_path),
             "-e", "signed-integer", str(wav_path), "norm", "-12"],
            check=True, capture_output=True,
        )
    except subprocess.CalledProcessError as e:
        print(f"    sox error: {e.stderr.decode(errors='replace')}")
        return
    except FileNotFoundError:
        print("    sox not found — install sox")
        return

    print(f"    WAV: {wav_path}")
    for cmd in (["aplay", str(wav_path)], ["paplay", str(wav_path)],
                ["sox", str(wav_path), "-d"]):
        try:
            subprocess.run(cmd, check=True, capture_output=True)
            return
        except (subprocess.CalledProcessError, FileNotFoundError):
            continue
    print("    No audio player found (install aplay or paplay)")


# ── D-Bus agent ───────────────────────────────────────────────────────────────

AGENT_PATH    = "/test/agent/gamepad"
AGENT_IFACE   = "org.bluez.Agent1"
MANAGER_IFACE = "org.bluez.AgentManager1"
MANAGER_PATH  = "/org/bluez"
CAPABILITY    = "NoInputNoOutput"


async def _register_agent(auth_event: asyncio.Event) -> tuple:
    """Register a NoInputNoOutput BlueZ agent via dbus-next.

    Returns (bus, agent_object) — keep both alive for the duration of the session.
    Sets auth_event when RequestAuthorization arrives so the caller can detect
    that the device is actually in pairing mode (not just advertising).
    """
    from dbus_next.aio import MessageBus
    from dbus_next.service import ServiceInterface, method
    from dbus_next import BusType

    class BluezAgent(ServiceInterface):
        def __init__(self):
            super().__init__(AGENT_IFACE)

        @method()
        def Release(self):
            print("    [agent] Release")

        @method()
        def RequestAuthorization(self, device: "o"):  # noqa: F821
            print(f"    [agent] RequestAuthorization from {device} → accepted")
            auth_event.set()
            # Returning without raising = accepted

        @method()
        def AuthorizeService(self, device: "o", uuid: "s"):  # noqa: F821
            print(f"    [agent] AuthorizeService {uuid} → accepted")

        @method()
        def Cancel(self):
            print("    [agent] Cancel")

    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()

    agent = BluezAgent()
    bus.export(AGENT_PATH, agent)

    manager = bus.get_proxy_object(
        "org.bluez", MANAGER_PATH,
        await bus.introspect("org.bluez", MANAGER_PATH),
    ).get_interface(MANAGER_IFACE)

    await manager.call_register_agent(AGENT_PATH, CAPABILITY)
    await manager.call_request_default_agent(AGENT_PATH)
    print(f"    [agent] Registered (NoInputNoOutput) at {AGENT_PATH}")

    return bus, agent


async def _unpair_bluez(address: str) -> None:
    """Remove device from BlueZ via D-Bus (adapter.RemoveDevice)."""
    from dbus_next.aio import MessageBus
    from dbus_next import BusType

    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()

    bluez = bus.get_proxy_object(
        "org.bluez", "/",
        await bus.introspect("org.bluez", "/"),
    )

    # Find adapter path (usually /org/bluez/hci0)
    om = bluez.get_interface("org.freedesktop.DBus.ObjectManager")
    objects = await om.call_get_managed_objects()

    adapter_path = None
    device_path  = None
    norm_addr    = address.upper()

    for path, ifaces in objects.items():
        if "org.bluez.Adapter1" in ifaces and adapter_path is None:
            adapter_path = path
        if "org.bluez.Device1" in ifaces:
            dev_addr = ifaces["org.bluez.Device1"].get("Address")
            if dev_addr and dev_addr.value.upper() == norm_addr:
                device_path = path

    if not adapter_path or not device_path:
        print(f"    [unpair] device {address} not found in BlueZ objects")
        return

    adapter = bus.get_proxy_object(
        "org.bluez", adapter_path,
        await bus.introspect("org.bluez", adapter_path),
    ).get_interface("org.bluez.Adapter1")

    # Get adapter MAC address to build the cache file path.
    # We can't glob /var/lib/bluetooth/ as a regular user (root-owned, drwx------).
    adapter_iface = bus.get_proxy_object(
        "org.bluez", adapter_path,
        await bus.introspect("org.bluez", adapter_path),
    ).get_interface("org.bluez.Adapter1")
    adapter_props = bus.get_proxy_object(
        "org.bluez", adapter_path,
        await bus.introspect("org.bluez", adapter_path),
    ).get_interface("org.freedesktop.DBus.Properties")
    adapter_mac = (await adapter_props.call_get("org.bluez.Adapter1", "Address")).value

    await adapter.call_remove_device(device_path)
    print(f"    [unpair] RemoveDevice({device_path}) OK")
    bus.disconnect()

    # BlueZ keeps a name cache at /var/lib/bluetooth/<adapter>/cache/<device>
    # even after RemoveDevice. This lets it recognise the device from directed
    # (reconnection) advertising in future scans, breaking our pairing-mode filter.
    # Delete the cache entry so BlueZ truly forgets the device.
    # The directory is root-owned so we can't glob it — build the path directly.
    cache_file = f"/var/lib/bluetooth/{adapter_mac}/cache/{address.upper()}"
    print(f"    [unpair] Removing cache: {cache_file}")
    try:
        os.remove(cache_file)
        print("    [unpair] Cache removed OK")
    except FileNotFoundError:
        print("    [unpair] No cache file found — already clean")
    except PermissionError:
        try:
            subprocess.run(["sudo", "rm", cache_file], check=True)
            print("    [unpair] Cache removed OK (via sudo)")
        except subprocess.CalledProcessError as e:
            print(f"    [unpair] Cache remove failed: {e}")


# ── Main flow ─────────────────────────────────────────────────────────────────

async def run(do_unpair: bool) -> None:

    # ── Step 1: register D-Bus agent ──────────────────────────────────────────
    print("[1] Registering BlueZ pairing agent...")
    auth_event = asyncio.Event()
    try:
        _bus, _agent = await _register_agent(auth_event)
    except Exception as e:
        print(f"    FAILED: {e}")
        print("    Install dbus-next:  pip install dbus-next")
        return

    # ── Step 2: scan for device ───────────────────────────────────────────────
    # After RemoveDevice/unpair, BlueZ ignores directed (reconnection) advertising
    # from this device and only reports undirected advertising — which is pairing mode.
    # So find_device_by_name is sufficient: if it fires, the device is in pairing mode.
    print(f"[2] Waiting for '{DEVICE_NAME}' in pairing mode...")
    print("    Hold the pairing button on the gamepad.")

    dev = await BleakScanner.find_device_by_name(DEVICE_NAME, timeout=60.0)
    if dev is None:
        print("    Device not found within 60 s.")
        _bus.disconnect()
        return

    print(f"    Found: {dev.address}")

    auth_event.clear()
    client = BleakClient(dev, timeout=15.0)
    try:
        await client.connect()
    except Exception as e:
        print(f"    Connect failed: {e}")
        _bus.disconnect()
        return
    print(f"    Connected: {client.is_connected}")

    try:
        # ── Step 3: unbind hid-generic ────────────────────────────────────────
        # Poll until the kernel loads hid-generic (usually 1-4 s after connect).
        print("[3] Waiting for hid-generic and unbinding...")
        await asyncio.get_running_loop().run_in_executor(None, _wait_and_unbind_hog)

        # ── Step 4: device info ───────────────────────────────────────────────
        print("[4] Reading device info...")
        try:
            val = await client.read_gatt_char(_CHAR_MODEL)
            print(f"    Model:   {val.decode('utf-8', errors='replace').strip()}")
        except Exception as e:
            print(f"    Model read failed: {e}")
        try:
            val = await client.read_gatt_char(_CHAR_BATTERY)
            print(f"    Battery: {val[0]}%")
        except Exception as e:
            print(f"    Battery read failed: {e}")

        # ── Step 5: sniff all notify characteristics ──────────────────────────
        print("[5] Sniffing all notify characteristics (8s)...")
        print("    Speak into the mic during this time.")
        await _sniff_all(client, seconds=8)

        # ── Step 7: unpair ────────────────────────────────────────────────────
        if do_unpair:
            print("[7] Unpairing...")
            await client.disconnect()
            await _unpair_bluez(dev.address)
        else:
            print("[7] Skipping unpair (--no-unpair)")

    finally:
        if client.is_connected:
            print("[8] Disconnecting...")
            await client.disconnect()
            print("    Disconnected.")

    _bus.disconnect()
    print("\nDone.")


class _Tee(io.TextIOBase):
    """Write to both the real stdout and a log file simultaneously."""
    def __init__(self, real, log_file):
        self._real = real
        self._log  = log_file

    def write(self, s):
        self._real.write(s)
        self._log.write(s)
        return len(s)

    def flush(self):
        self._real.flush()
        self._log.flush()


def main() -> None:
    p = argparse.ArgumentParser(description="Linux BLE pair/unpair test — G100")
    p.add_argument("--no-unpair", action="store_true",
                   help="Keep device bonded after the test")
    args = p.parse_args()

    log_path = Path("last_run_test_pair.log")
    with log_path.open("w", encoding="utf-8") as log_file:
        sys.stdout = _Tee(sys.__stdout__, log_file)
        try:
            asyncio.run(run(do_unpair=not args.no_unpair))
        except KeyboardInterrupt:
            print("\nInterrupted.")
        finally:
            sys.stdout = sys.__stdout__


if __name__ == "__main__":
    main()
