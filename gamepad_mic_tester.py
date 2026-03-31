#!/usr/bin/env python3
"""
BLE Gamepad Microphone Tester — Realtek G100-4722

Usage:
    python gamepad_mic_tester.py [--seconds 5]

Dependencies:
    pip install bleak sounddevice soundfile
    sox must be available in PATH
"""

import argparse
import asyncio
import json
import logging
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr.encoding and sys.stderr.encoding.lower() != "utf-8":
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from bleak import BleakClient, BleakScanner

# ─── Device ───────────────────────────────────────────────────────────────────
KNOWN_ADDRESS  = "F4:22:7A:4A:AA:E0"
KNOWN_ADDR_INT = int(KNOWN_ADDRESS.replace(":", ""), 16)
DEVICE_NAME    = "GAME"

# ─── UUIDs ────────────────────────────────────────────────────────────────────
AB5E_CMD   = "ab5e0002-5a21-4f05-bc7d-af01f617b664"  # write: commands
AB5E_AUDIO = "ab5e0003-5a21-4f05-bc7d-af01f617b664"  # notify: audio data
AB5E_RESP  = "ab5e0004-5a21-4f05-bc7d-af01f617b664"  # notify: command responses

_CHAR_MODEL  = "00002a24-0000-1000-8000-00805f9b34fb"
_CHAR_SERIAL = "00002a25-0000-1000-8000-00805f9b34fb"

# ─── Config ───────────────────────────────────────────────────────────────────
LOG_DIR      = Path("logs")
_SUMMARY_FILE = LOG_DIR / "tested_devices.json"

# ─── Logging ──────────────────────────────────────────────────────────────────
_FMT = logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s", datefmt="%H:%M:%S")


def _make_logger(name: str = "mic_tester") -> logging.Logger:
    """Create a logger with WARNING-level console output and DEBUG-level file output."""
    log = logging.getLogger(name)
    log.setLevel(logging.DEBUG)
    if not log.handlers:
        ch = logging.StreamHandler()
        ch.setLevel(logging.WARNING)
        ch.setFormatter(_FMT)
        log.addHandler(ch)
    return log


def _add_file_handler(log: logging.Logger, tag: str) -> Path:
    """Attach a timestamped DEBUG-level file handler and return its path."""
    LOG_DIR.mkdir(exist_ok=True)
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = LOG_DIR / f"{tag}_{ts}.log"
    fh   = logging.FileHandler(path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(_FMT)
    log.addHandler(fh)
    return path


# ─── Connection ───────────────────────────────────────────────────────────────

async def connect(log: logging.Logger, scan_only: bool = False) -> BleakClient | None:
    """Scan for DEVICE_NAME and connect.

    If scan_only=True, returns None when device not found (instead of WinRT bypass).
    If scan_only=False, falls back to the known bonded address via WinRT bypass.
    """
    log.info(f"Scanning for '{DEVICE_NAME}' ...")
    dev = await BleakScanner.find_device_by_name(DEVICE_NAME, timeout=6.0)

    if dev:
        log.info(f"Found via scan: {dev.address}")
        client = BleakClient(dev, timeout=15.0)
    elif not scan_only:
        log.info(f"Not in scan — WinRT bypass for {KNOWN_ADDRESS}")
        client = BleakClient(KNOWN_ADDRESS, timeout=15.0)
        client._backend._device_info = KNOWN_ADDR_INT
    else:
        return None

    await client.connect()
    log.info(f"Connected: {client.is_connected}")
    return client


# ─── Device info ──────────────────────────────────────────────────────────────

async def _read_device_info(client: BleakClient, log: logging.Logger) -> dict:
    """Read Model Number and Serial Number from Device Information Service."""
    info = {"serial": "", "model": ""}
    for key, uuid in (("serial", _CHAR_SERIAL), ("model", _CHAR_MODEL)):
        try:
            val = await client.read_gatt_char(uuid)
            info[key] = val.decode("utf-8", errors="replace").strip()
        except Exception:
            pass
    log.debug(f"Device info: {info}")
    return info


# ─── Audio pipeline ───────────────────────────────────────────────────────────

async def _warmup(client: BleakClient, log: logging.Logger) -> None:
    """Fire one GET_CAPS → START → STOP cycle to prime the device audio pipeline.

    After pairing, the first START cycle is acknowledged but produces no audio.
    This burn-in cycle ensures the following _capture_audio call works immediately.
    """
    ready = asyncio.Event()

    def on_resp(sender, data: bytearray):
        if data and data[0] == 0x0C:
            ready.set()

    await client.start_notify(AB5E_RESP, on_resp)
    await client.write_gatt_char(AB5E_CMD, bytes([0x0C, 0x00]), response=False)
    try:
        await asyncio.wait_for(ready.wait(), timeout=5.0)
    except asyncio.TimeoutError:
        log.warning("Warm-up: GET_CAPS timeout")
    await client.write_gatt_char(AB5E_CMD, bytes([0x0A, 0x00]), response=False)
    await asyncio.sleep(0.2)
    await client.write_gatt_char(AB5E_CMD, bytes([0x0B, 0x00]), response=False)
    await asyncio.sleep(0.3)
    await client.stop_notify(AB5E_RESP)
    log.info("Warm-up complete.")


async def _capture_audio(client: BleakClient, seconds: int, log: logging.Logger) -> list[bytes]:
    """Send GET_CAPS + START, collect audio BLE frames for `seconds`, then STOP.

    Waits for a GET_CAPS response before sending START to ensure the device is ready.
    Returns raw BLE notification payloads from AB5E_AUDIO.
    """
    frames: list[bytes] = []
    recording = False
    caps_received = asyncio.Event()

    def on_audio(sender, data: bytearray):
        if recording:
            frames.append(bytes(data))

    def on_resp(sender, data: bytearray):
        log.debug(f"RESP {bytes(data).hex()}")
        if data:
            caps_received.set()

    await client.start_notify(AB5E_AUDIO, on_audio)
    await client.start_notify(AB5E_RESP, on_resp)

    await client.write_gatt_char(AB5E_CMD, bytes([0x0C, 0x00]), response=False)
    try:
        await asyncio.wait_for(caps_received.wait(), timeout=5.0)
        log.info("GET_CAPS acknowledged — sending START")
    except asyncio.TimeoutError:
        log.warning("GET_CAPS timeout — sending START anyway")

    recording = True
    await client.write_gatt_char(AB5E_CMD, bytes([0x0A, 0x00]), response=False)
    log.info(f"Recording {seconds}s...")

    for remaining in range(seconds, 0, -1):
        print(f"\r  Recording: {remaining}s  ", end="", flush=True)
        await asyncio.sleep(1)
    print("\r  Recording done.      ")

    recording = False
    for stop in (bytes([0x0B, 0x00]), bytes([0x00, 0x00])):
        try:
            await client.write_gatt_char(AB5E_CMD, stop, response=False)
            await asyncio.sleep(0.05)
        except Exception:
            pass

    await client.stop_notify(AB5E_AUDIO)
    await client.stop_notify(AB5E_RESP)

    log.info(f"Captured {len(frames)} frames ({sum(len(f) for f in frames)} B)")
    return frames


def _play_wav(wav_path: Path) -> None:
    """Play a WAV file via PowerShell SoundPlayer (synchronous, no COM side effects)."""
    print("  Playing back...")
    subprocess.run(
        ["powershell", "-NoProfile", "-c",
         f"(New-Object Media.SoundPlayer '{wav_path.resolve()}').PlaySync()"],
        check=False,
    )


def _convert_and_play(ima_path: Path, wav_path: Path, log: logging.Logger) -> bool:
    """Convert raw IMA file to WAV via sox and play back with sounddevice.

    Packet IMA block headers (bytes 0-5: type+seq+pred+step) are already stripped;
    ima_path contains only raw ADPCM nibbles starting at packet offset 6.
    Returns True on success.
    """
    try:
        subprocess.run(
            ["sox", "-t", "ima", "-e", "ima-adpcm", "-r", "16000", str(ima_path),
             "-e", "signed-integer", str(wav_path), "norm", "-12"],
            check=True, capture_output=True,
        )
    except subprocess.CalledProcessError as e:
        print(f"  sox error: {e.stderr.decode(errors='replace')}")
        log.error(f"sox failed: {e.stderr.decode(errors='replace')}")
        return False
    except FileNotFoundError:
        print("  sox not found in PATH")
        return False

    log.info(f"WAV: {wav_path}")
    print(f"  WAV: {wav_path}")
    _play_wav(wav_path)
    return True


# ─── Summary log ──────────────────────────────────────────────────────────────

def _update_summary(mac: str, model: str, log: logging.Logger) -> None:
    """Append or update the tested-devices JSON log and print running totals."""
    LOG_DIR.mkdir(exist_ok=True)
    try:
        data: dict = json.loads(_SUMMARY_FILE.read_text(encoding="utf-8")) if _SUMMARY_FILE.exists() else {}
    except Exception:
        data = {}

    ts = datetime.now().isoformat(timespec="seconds")
    if mac not in data:
        data[mac] = {"model": model, "first_tested": ts, "last_tested": ts, "test_count": 1}
    else:
        data[mac]["last_tested"] = ts
        data[mac]["test_count"] = data[mac].get("test_count", 0) + 1

    _SUMMARY_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info(f"Summary: {len(data)} unique device(s)")
    print(f"\n  Unique devices tested: {len(data)}")
    print(f"  This device ({mac}): tests — {data[mac]['test_count']}, first — {data[mac]['first_tested']}")


# ─── Input ────────────────────────────────────────────────────────────────────

def _read_key() -> str:
    """Read a single keypress without requiring Enter (Windows msvcrt).

    Returns the uppercase character, empty string for Enter / unrecognised keys.
    Extended keys (arrows, F-keys) are consumed and discarded.
    """
    import msvcrt
    key = msvcrt.getch()
    if key in (b"\x00", b"\xe0"):
        msvcrt.getch()
        return ""
    if key in (b"\r", b"\n"):
        return ""
    try:
        return key.decode("utf-8", errors="replace").upper()
    except Exception:
        return ""


# ─── Test flow ────────────────────────────────────────────────────────────────

async def _test_one_device(client: BleakClient, seconds: int, log: logging.Logger) -> str:
    """Pair, warm-up, run the record loop, unpair.  Returns the last user choice."""
    loop_ev = asyncio.get_running_loop()

    # Pair
    try:
        await client.pair()
        log.info("pair() sent — waiting for bonding...")
    except Exception as e:
        log.warning(f"pair() failed: {e}")

    print("  Pairing...", end="", flush=True)
    deadline = loop_ev.time() + 30.0
    while loop_ev.time() < deadline:
        try:
            await client.read_gatt_char(_CHAR_MODEL)
            break
        except Exception:
            print(".", end="", flush=True)
            await asyncio.sleep(1.0)
    else:
        print(" timeout!")
        log.warning("Pairing timeout — proceeding anyway")
    print(" done.")

    print("  Initialising...", end="", flush=True)
    await _warmup(client, log)
    print(" done.")

    info     = await _read_device_info(client, log)
    model    = info["model"] or "G100"
    mac      = client.address
    safe_mac = mac.replace(":", "")
    print(f"  Device: {model}  |  MAC: {mac}")
    log.info(f"model={model} addr={mac}")

    LOG_DIR.mkdir(exist_ok=True)
    choice = "Q"
    wav_path = LOG_DIR / f"test_{safe_mac}.wav"

    while True:
        print()
        print("  Speak into the microphone after the signal...")
        await asyncio.sleep(0.5)
        print("  *** GO ***")

        frames = await _capture_audio(client, seconds, log)

        if not frames:
            print("  No audio received.")
            log.warning("No audio frames captured.")
        else:
            ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
            ima_path = LOG_DIR / f"test_{safe_mac}_{ts}.ima"
            raw_path = LOG_DIR / f"test_{safe_mac}_{ts}.bin"

            for old in LOG_DIR.glob(f"test_{safe_mac}.wav"):
                old.unlink(missing_ok=True)

            raw_path.write_bytes(b"".join(frames))
            ima_path.write_bytes(b"".join(f[6:] for f in frames if len(f) > 6))
            _convert_and_play(ima_path, wav_path, log)

        while True:
            print("\n  [Enter] Next  [R] Re-record  [P] Play back  [Q] Quit", end=" ", flush=True)
            key = await loop_ev.run_in_executor(None, _read_key)
            print(key or "↵")
            if key in ("", "\r", "\n"):
                choice = "N"
                break
            if key == "R":
                choice = "R"
                break
            if key == "P":
                if wav_path.exists():
                    _play_wav(wav_path)
                else:
                    print("  No WAV file to play.")
            elif key == "Q":
                choice = "Q"
                break

        if choice in ("N", "Q"):
            break

    try:
        await client.unpair()
        print("  Unpaired.")
    except Exception as e:
        log.warning(f"unpair() failed: {e}")

    await client.disconnect()
    _update_summary(mac, model, log)
    return choice


async def mode_test(seconds: int) -> None:
    """Cyclic mic test: scan → pair → record loop → unpair → next device."""
    log = _make_logger()
    _add_file_handler(log, "test")

    print()
    print("  ╔══════════════════════════════════════════╗")
    print("  ║    Gamepad Microphone Test — G100        ║")
    print("  ╚══════════════════════════════════════════╝")

    while True:
        print()
        print("  Waiting for device 'GAME'...")
        client = None
        while client is None:
            client = await connect(log, scan_only=True)
            if client is None:
                await asyncio.sleep(2.0)

        choice = await _test_one_device(client, seconds, log)
        if choice == "Q":
            break

    print("\n  Session ended.")


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(description="BLE Gamepad Mic Tester — Realtek G100")
    p.add_argument("--seconds", type=int, default=5, help="Record duration in seconds (default 5)")
    args = p.parse_args()

    exc: BaseException | None = None

    def _run():
        nonlocal exc
        try:
            asyncio.run(mode_test(args.seconds))
        except KeyboardInterrupt:
            pass
        except BaseException as e:
            exc = e

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    try:
        t.join()
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(0)

    if exc is not None:
        raise exc


if __name__ == "__main__":
    main()
