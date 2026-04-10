#!/usr/bin/env python3
"""
Franken test: Linux BLE pairing + exact Windows audio sequence.

Pairing:  BlueZ D-Bus agent (NoInputNoOutput) + Device1.Pair() — from test_pair_linux.py
Audio:    Exact copy of Windows _warmup + _capture_audio — from gamepad_mic_tester.py

Goal: verify that the simplest possible approach (Windows audio logic verbatim)
fails or works on Linux, to isolate whether the problem is in our audio protocol
implementation or in the BLE stack / device interaction.

Usage:
    sudo /home/qa/Downloads/gamepad_mic_tester/.venv/bin/python franken_test.py
    sudo ... franken_test.py --no-unpair
    sudo ... franken_test.py --reconnect
"""

import argparse
import asyncio
import io
import subprocess
import sys
from pathlib import Path

from bleak import BleakClient, BleakScanner

DEVICE_NAME   = "GAME"
KNOWN_ADDRESS = "F4:22:7A:4A:AA:E0"

AB5E_CMD   = "ab5e0002-5a21-4f05-bc7d-af01f617b664"
AB5E_AUDIO = "ab5e0003-5a21-4f05-bc7d-af01f617b664"
AB5E_RESP  = "ab5e0004-5a21-4f05-bc7d-af01f617b664"

_CHAR_MODEL   = "00002a24-0000-1000-8000-00805f9b34fb"
_CHAR_BATTERY = "00002a19-0000-1000-8000-00805f9b34fb"

AGENT_PATH    = "/test/agent/franken"
AGENT_IFACE   = "org.bluez.Agent1"
MANAGER_IFACE = "org.bluez.AgentManager1"
MANAGER_PATH  = "/org/bluez"
CAPABILITY    = "NoInputNoOutput"

RECORD_SECS   = 8


# ── Linux pairing (from test_pair_linux.py) ───────────────────────────────────

async def _register_agent(auth_event: asyncio.Event) -> tuple:
    from dbus_next.aio import MessageBus
    from dbus_next.service import ServiceInterface, method
    from dbus_next import BusType

    class BluezAgent(ServiceInterface):
        def __init__(self):
            super().__init__(AGENT_IFACE)

        @method()
        def Release(self): pass

        @method()
        def RequestAuthorization(self, device: "o"):  # noqa: F821
            auth_event.set()

        @method()
        def AuthorizeService(self, device: "o", uuid: "s"): pass  # noqa: F821

        @method()
        def Cancel(self): pass

    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    agent = BluezAgent()
    bus.export(AGENT_PATH, agent)
    manager = bus.get_proxy_object(
        "org.bluez", MANAGER_PATH,
        await bus.introspect("org.bluez", MANAGER_PATH),
    ).get_interface(MANAGER_IFACE)
    await manager.call_register_agent(AGENT_PATH, CAPABILITY)
    await manager.call_request_default_agent(AGENT_PATH)
    print(f"    [agent] Registered at {AGENT_PATH}")
    return bus, agent


async def _pair_and_trust(dev_address: str) -> None:
    from dbus_next.aio import MessageBus
    from dbus_next import BusType, Variant

    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    dev_path = "/org/bluez/hci0/dev_" + dev_address.upper().replace(":", "_")
    dev_obj  = bus.get_proxy_object(
        "org.bluez", dev_path,
        await bus.introspect("org.bluez", dev_path),
    )
    dev_iface = dev_obj.get_interface("org.bluez.Device1")
    props     = dev_obj.get_interface("org.freedesktop.DBus.Properties")

    paired = (await props.call_get("org.bluez.Device1", "Paired")).value
    bonded = (await props.call_get("org.bluez.Device1", "Bonded")).value
    print(f"    Before pair: Paired={paired}, Bonded={bonded}")

    if not paired:
        print("    Calling Device1.Pair()...")
        await dev_iface.call_pair()
        paired = (await props.call_get("org.bluez.Device1", "Paired")).value
        bonded = (await props.call_get("org.bluez.Device1", "Bonded")).value
        print(f"    After  pair: Paired={paired}, Bonded={bonded}")

    await props.call_set("org.bluez.Device1", "Trusted", Variant("b", True))
    print("    Trusted=True")
    bus.disconnect()


async def _unpair(address: str) -> None:
    from dbus_next.aio import MessageBus
    from dbus_next import BusType

    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    om  = bus.get_proxy_object(
        "org.bluez", "/", await bus.introspect("org.bluez", "/"),
    ).get_interface("org.freedesktop.DBus.ObjectManager")
    objects = await om.call_get_managed_objects()

    adapter_path = device_path = None
    for path, ifaces in objects.items():
        if "org.bluez.Adapter1" in ifaces and not adapter_path:
            adapter_path = path
        if "org.bluez.Device1" in ifaces:
            addr = ifaces["org.bluez.Device1"].get("Address")
            if addr and addr.value.upper() == address.upper():
                device_path = path

    if not (adapter_path and device_path):
        print(f"    [unpair] device not found in BlueZ")
        bus.disconnect()
        return

    adapter = bus.get_proxy_object(
        "org.bluez", adapter_path,
        await bus.introspect("org.bluez", adapter_path),
    ).get_interface("org.bluez.Adapter1")

    adapter_props = bus.get_proxy_object(
        "org.bluez", adapter_path,
        await bus.introspect("org.bluez", adapter_path),
    ).get_interface("org.freedesktop.DBus.Properties")
    adapter_mac = (await adapter_props.call_get("org.bluez.Adapter1", "Address")).value

    await adapter.call_remove_device(device_path)
    print(f"    [unpair] RemoveDevice OK")
    bus.disconnect()

    # Clear BlueZ name cache — without this, BlueZ recognises the device from
    # directed advertising and connects without pairing mode (dangerous in inspection).
    # Requires sudoers rule: qa ALL=(ALL) NOPASSWD: /usr/bin/rm /var/lib/bluetooth/*/cache/*
    cache = f"/var/lib/bluetooth/{adapter_mac}/cache/{address.upper()}"
    try:
        import os
        os.remove(cache)
        print(f"    [unpair] Cache removed")
    except PermissionError:
        r = subprocess.run(["sudo", "rm", cache], capture_output=True)
        if r.returncode == 0:
            print(f"    [unpair] Cache removed (via sudo)")
        else:
            print(f"    [unpair] Cache removal failed — add sudoers rule (see SETUP.md)")
    except FileNotFoundError:
        pass


# ── Windows audio logic (verbatim from gamepad_mic_tester.py) ─────────────────

async def _warmup(client: BleakClient) -> None:
    """Exact Windows warmup: GET_CAPS → START → STOP."""
    ready = asyncio.Event()
    resp_log: list[str] = []

    def on_resp(sender, data: bytearray):
        b = bytes(data)
        resp_log.append(b.hex())
        print(f"    [ab5e0004] {b.hex()}")
        if data and data[0] == 0x0C:
            ready.set()

    await client.start_notify(AB5E_RESP, on_resp)
    await client.write_gatt_char(AB5E_CMD, bytes([0x0C, 0x00]), response=False)
    try:
        await asyncio.wait_for(ready.wait(), timeout=5.0)
        print("    GET_CAPS acknowledged")
    except asyncio.TimeoutError:
        print("    GET_CAPS timeout")
    await client.write_gatt_char(AB5E_CMD, bytes([0x0A, 0x00]), response=False)
    await asyncio.sleep(0.2)
    await client.write_gatt_char(AB5E_CMD, bytes([0x0B, 0x00]), response=False)
    await asyncio.sleep(0.3)
    await client.stop_notify(AB5E_RESP)
    print("    Warmup done.")


async def _capture_audio(client: BleakClient, seconds: int) -> list[bytes]:
    """Exact Windows capture: subscribe both → GET_CAPS → recording=True → START."""
    frames: list[bytes] = []
    recording     = False
    caps_received = asyncio.Event()

    def on_audio(sender, data: bytearray):
        if recording:
            frames.append(bytes(data))
            print(f"    [ab5e0003] #{len(frames):3d}  {len(data):3d}B")

    def on_resp(sender, data: bytearray):
        b = bytes(data)
        print(f"    [ab5e0004] {b.hex()}")
        if data:
            caps_received.set()

    # Windows sequence: subscribe AUDIO and RESP together, before GET_CAPS
    await client.start_notify(AB5E_AUDIO, on_audio)
    await client.start_notify(AB5E_RESP,  on_resp)

    await client.write_gatt_char(AB5E_CMD, bytes([0x0C, 0x00]), response=False)
    try:
        await asyncio.wait_for(caps_received.wait(), timeout=5.0)
        print("    GET_CAPS acknowledged — sending START")
    except asyncio.TimeoutError:
        print("    GET_CAPS timeout — sending START anyway")

    recording = True
    await client.write_gatt_char(AB5E_CMD, bytes([0x0A, 0x00]), response=False)

    for remaining in range(seconds, 0, -1):
        print(f"\r    Recording: {remaining}s  ", end="", flush=True)
        await asyncio.sleep(1)
    print("\r    Recording done.        ")

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


# ── Conversion + playback ─────────────────────────────────────────────────────

def _convert_and_play(frames: list[bytes], mac: str) -> None:
    """Strip 6-byte headers → IMA ADPCM → WAV via sox → play."""
    out_dir  = Path("logs")
    out_dir.mkdir(exist_ok=True)
    safe_mac = mac.replace(":", "")
    ima_path = out_dir / f"franken_{safe_mac}.ima"
    wav_path = out_dir / f"franken_{safe_mac}.wav"

    adpcm = b"".join(f[6:] for f in frames if len(f) > 6)
    ima_path.write_bytes(adpcm)
    print(f"    IMA: {ima_path}  ({len(adpcm)} B ADPCM, {len(frames)} frames)")

    try:
        subprocess.run(
            ["sox", "-t", "ima", "-e", "ima-adpcm", "-r", "16000",
             str(ima_path), "-e", "signed-integer", str(wav_path), "norm", "-12"],
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
            print("    Playback done.")
            return
        except (subprocess.CalledProcessError, FileNotFoundError):
            continue
    print("    No player found (install aplay or paplay)")


# ── Main flow ─────────────────────────────────────────────────────────────────

async def run(do_unpair: bool, reconnect: bool) -> None:

    # [1] Agent
    print("[1] Registering BlueZ pairing agent...")
    auth_event = asyncio.Event()
    try:
        _bus, _agent = await _register_agent(auth_event)
    except Exception as e:
        print(f"    FAILED: {e}")
        return

    # [2] Connect
    if reconnect:
        print(f"[2] Reconnecting to {KNOWN_ADDRESS} (bonded)...")
        client     = BleakClient(KNOWN_ADDRESS, timeout=15.0)
        dev_address = KNOWN_ADDRESS
    else:
        print(f"[2] Waiting for '{DEVICE_NAME}' in pairing mode...")
        print("    Hold the pairing button on the gamepad.")
        dev = await BleakScanner.find_device_by_name(DEVICE_NAME, timeout=60.0)
        if dev is None:
            print("    Not found.")
            _bus.disconnect()
            return
        print(f"    Found: {dev.address}")
        dev_address = dev.address
        client      = BleakClient(dev, timeout=15.0)

    try:
        await client.connect()
    except Exception as e:
        print(f"    Connect failed: {e}")
        _bus.disconnect()
        return
    print(f"    Connected: {client.is_connected}")

    # [3] Pair + trust (Linux D-Bus)
    print("[3] Pair + trust via D-Bus...")
    try:
        await _pair_and_trust(dev_address)
    except Exception as e:
        print(f"    [pair/trust] {e}")

    try:
        # [4] Device info
        print("[4] Device info...")
        try:
            val = await client.read_gatt_char(_CHAR_MODEL)
            print(f"    Model:   {val.decode('utf-8', errors='replace').strip()}")
        except Exception as e:
            print(f"    Model: {e}")
        try:
            val = await client.read_gatt_char(_CHAR_BATTERY)
            print(f"    Battery: {val[0]}%")
        except Exception as e:
            print(f"    Battery: {e}")

        # [5] Warmup (Windows logic)
        print("[5] Warmup...")
        await _warmup(client)

        # [6] Recording (Windows logic)
        print(f"[6] Recording {RECORD_SECS}s — speak into the mic...")
        frames = await _capture_audio(client, RECORD_SECS)
        print(f"\n    ── Result: {len(frames)} audio frames ──")
        if frames:
            total = sum(len(f) for f in frames)
            print(f"    Total: {total} B raw  ({total - len(frames)*6} B ADPCM)")
            print("[7] Converting + playing back...")
            _convert_and_play(frames, client.address)
        else:
            print("    No audio received.")

        # [8] Unpair
        if do_unpair:
            print("[8] Unpairing...")
            await client.disconnect()
            await _unpair(dev_address)
        else:
            print("[8] Skipping unpair (--no-unpair)")

    finally:
        if client.is_connected:
            await client.disconnect()

    _bus.disconnect()
    print("\nDone.")


# ── CLI ───────────────────────────────────────────────────────────────────────

class _Tee(io.TextIOBase):
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
    p = argparse.ArgumentParser(description="Franken test: Linux pairing + Windows audio")
    p.add_argument("--no-unpair",  action="store_true", help="Keep bonded after test")
    p.add_argument("--reconnect",  action="store_true", help=f"Skip scan, connect to {KNOWN_ADDRESS}")
    args = p.parse_args()

    log_path = Path("franken_last_run.log")
    with log_path.open("w", encoding="utf-8") as lf:
        sys.stdout = _Tee(sys.__stdout__, lf)
        try:
            asyncio.run(run(do_unpair=not args.no_unpair, reconnect=args.reconnect))
        except KeyboardInterrupt:
            print("\nInterrupted.")
        finally:
            sys.stdout = sys.__stdout__


if __name__ == "__main__":
    main()
