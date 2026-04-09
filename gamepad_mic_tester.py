#!/usr/bin/env python3
"""
BLE Gamepad Microphone Tester — Realtek G100-4722

Usage:
    python gamepad_mic_tester.py [--seconds 5] [--debug]

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

_CHAR_MODEL   = "00002a24-0000-1000-8000-00805f9b34fb"
_CHAR_SERIAL  = "00002a25-0000-1000-8000-00805f9b34fb"
_CHAR_FW      = "00002a26-0000-1000-8000-00805f9b34fb"
_CHAR_HW      = "00002a27-0000-1000-8000-00805f9b34fb"
_CHAR_BATTERY = "00002a19-0000-1000-8000-00805f9b34fb"

# ─── Config ───────────────────────────────────────────────────────────────────
LOG_DIR       = Path("logs")
_SUMMARY_FILE = LOG_DIR / "tested_devices.json"

# Set to True by --debug flag in main()
_DEBUG = False

# ─── Logging ──────────────────────────────────────────────────────────────────
_FMT = logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s", datefmt="%H:%M:%S")


def _make_logger(name: str = "mic_tester") -> logging.Logger:
    """Create a logger. Console level: DEBUG if --debug, else WARNING. File level: always DEBUG."""
    log = logging.getLogger(name)
    log.setLevel(logging.DEBUG)
    if not log.handlers:
        ch = logging.StreamHandler()
        ch.setLevel(logging.DEBUG if _DEBUG else logging.WARNING)
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

    Scans first; falls back to direct address for already-bonded device on Windows.
    If scan_only=True, returns None when device not found.
    """
    log.debug(f"Scanning for '{DEVICE_NAME}' (timeout=6s)...")
    dev = await BleakScanner.find_device_by_name(DEVICE_NAME, timeout=6.0)
    log.debug(f"Scan result: {'found' if dev else 'not found'}"
              + (f" — {dev.address}" if dev else ""))

    if dev:
        log.info(f"Found via scan: {dev.address}")
        client = BleakClient(dev, timeout=15.0)
    elif not scan_only:
        log.info(f"Not in scan — connecting directly to {KNOWN_ADDRESS}")
        client = BleakClient(KNOWN_ADDRESS, timeout=15.0)
        if sys.platform == "win32":
            log.debug("Win32: injecting _device_info for WinRT bypass")
            client._backend._device_info = KNOWN_ADDR_INT
    else:
        log.debug("scan_only=True and device not found — returning None")
        return None

    log.debug("Calling client.connect()...")
    await client.connect()
    log.info(f"Connected: {client.is_connected}")
    return client


# ─── GATT diagnostics ────────────────────────────────────────────────────────

async def _debug_list_services(client: BleakClient, log: logging.Logger) -> None:
    """Log all GATT services and characteristics visible to bleak."""
    log.debug("=== GATT service map (bleak view) ===")
    for svc in client.services:
        log.debug(f"  SVC {svc.uuid}")
        for char in svc.characteristics:
            log.debug(f"    CHAR {char.uuid}  props={char.properties}  handle=0x{char.handle:04x}")
            for desc in char.descriptors:
                log.debug(f"      DESC {desc.uuid}  handle=0x{desc.handle:04x}")
    log.debug("=== end service map ===")


# ─── Device info ──────────────────────────────────────────────────────────────

async def _read_device_info(client: BleakClient, log: logging.Logger) -> dict:
    """Read device metadata from Device Information Service and Battery Service."""
    info: dict = {"model": "", "serial": "", "fw": "", "hw": "", "battery": -1}

    for key, uuid in (("model", _CHAR_MODEL), ("serial", _CHAR_SERIAL),
                      ("fw", _CHAR_FW), ("hw", _CHAR_HW)):
        log.debug(f"Reading {key} ({uuid})...")
        try:
            val = await client.read_gatt_char(uuid)
            info[key] = val.decode("utf-8", errors="replace").strip()
            log.debug(f"  {key} = {info[key]!r}")
        except Exception as e:
            log.debug(f"  {key} read failed: {type(e).__name__}: {e}")

    log.debug(f"Reading battery ({_CHAR_BATTERY})...")
    try:
        val = await client.read_gatt_char(_CHAR_BATTERY)
        info["battery"] = val[0]  # uint8, 0-100 %
        log.debug(f"  battery = {info['battery']}%")
    except Exception as e:
        log.debug(f"  battery read failed: {type(e).__name__}: {e}")

    log.debug(f"Device info complete: {info}")
    return info


# ─── Audio pipeline ───────────────────────────────────────────────────────────

async def _warmup(client: BleakClient, log: logging.Logger) -> None:
    """Fire one GET_CAPS → START → STOP cycle to prime the device audio pipeline.

    After the very first BLE pairing the device ignores the first START command.
    This dummy cycle primes the firmware so the next capture works correctly.
    """
    log.debug("Warmup: starting (GET_CAPS → START → STOP dummy cycle)")
    ready = asyncio.Event()

    def on_resp(sender, data: bytearray):
        log.debug(f"Warmup: RESP notification: {bytes(data).hex()}")
        if data and data[0] == 0x0C:
            log.debug("Warmup: GET_CAPS acknowledged (0x0C)")
            ready.set()

    await client.start_notify(AB5E_RESP, on_resp)

    await client.write_gatt_char(AB5E_CMD, bytes([0x0C, 0x00]), response=False)
    try:
        await asyncio.wait_for(ready.wait(), timeout=5.0)
    except asyncio.TimeoutError:
        log.warning("Warmup: GET_CAPS timeout (5s) — no response on AB5E_RESP")

    await client.write_gatt_char(AB5E_CMD, bytes([0x0A, 0x00]), response=False)
    await asyncio.sleep(0.2)
    await client.write_gatt_char(AB5E_CMD, bytes([0x0B, 0x00]), response=False)
    await asyncio.sleep(0.3)

    await client.stop_notify(AB5E_RESP)
    log.debug("Warmup: complete")


async def _capture_audio(client: BleakClient, seconds: int, log: logging.Logger) -> list[bytes]:
    """Send GET_CAPS + START, collect audio BLE frames for `seconds`, then STOP.

    Waits for a GET_CAPS response before sending START to ensure the device is ready.
    Returns raw BLE notification payloads from AB5E_AUDIO.
    """
    frames: list[bytes] = []
    recording = False
    caps_received = asyncio.Event()
    start_acked  = asyncio.Event()

    def on_audio(sender, data: bytearray):
        if recording:
            n = len(frames)
            frames.append(bytes(data))
            if n < 5 or n % 50 == 0:
                log.debug(f"Audio frame #{n + 1}: total={len(data)}B  "
                          f"header={bytes(data[:6]).hex()}  adpcm={max(0, len(data) - 6)}B")

    def on_resp(sender, data: bytearray):
        b = bytes(data)
        log.debug(f"Capture: RESP notification: {b.hex()}")
        if b:
            caps_received.set()
        # START ack: device replies with 0x0B (STOP code) as stream-start confirmation
        if b and b[0] == 0x0B:
            log.debug("Capture: START ack received (0x0B)")
            start_acked.set()

    log.debug("Capture: subscribing to AB5E_AUDIO and AB5E_RESP...")
    try:
        await client.start_notify(AB5E_AUDIO, on_audio)
        log.debug("Capture: AB5E_AUDIO subscribed OK")
    except Exception as e:
        log.error(f"Capture: AB5E_AUDIO start_notify FAILED: {type(e).__name__}: {e}")
    try:
        await client.start_notify(AB5E_RESP, on_resp)
        log.debug("Capture: AB5E_RESP subscribed OK")
    except Exception as e:
        log.error(f"Capture: AB5E_RESP start_notify FAILED: {type(e).__name__}: {e}")
    log.debug("Capture: subscriptions active")

    log.debug("Capture: sending GET_CAPS (0x0C 0x00)...")
    await client.write_gatt_char(AB5E_CMD, bytes([0x0C, 0x00]), response=False)
    try:
        await asyncio.wait_for(caps_received.wait(), timeout=5.0)
        log.debug("Capture: GET_CAPS acknowledged")
    except asyncio.TimeoutError:
        log.warning("GET_CAPS timeout (5s) — sending START anyway")

    log.debug("Capture: sending START (0x0A 0x00)...")
    await client.write_gatt_char(AB5E_CMD, bytes([0x0A, 0x00]), response=False)
    try:
        await asyncio.wait_for(start_acked.wait(), timeout=3.0)
        log.debug("Capture: stream ack received — recording")
    except asyncio.TimeoutError:
        log.debug("Capture: no stream ack within 3s — recording anyway")

    recording = True
    log.info(f"Recording {seconds}s...")
    log.debug(f"Capture: recording started, collecting for {seconds}s")

    for remaining in range(seconds, 0, -1):
        print(f"\r  Recording: {remaining}s  ", end="", flush=True)
        await asyncio.sleep(1)
        log.debug(f"Capture: tick — {len(frames)} frames so far")
    print("\r  Recording done.      ")

    recording = False
    log.debug(f"Capture: recording stopped — {len(frames)} frames collected so far")

    for label, stop in (("STOP", bytes([0x0B, 0x00])), ("STOP-alt", bytes([0x00, 0x00]))):
        try:
            log.debug(f"Capture: sending {label} ({stop.hex()})...")
            await client.write_gatt_char(AB5E_CMD, stop, response=False)
            await asyncio.sleep(0.05)
        except Exception as e:
            log.debug(f"Capture: {label} write failed: {type(e).__name__}: {e}")

    await client.stop_notify(AB5E_AUDIO)
    await client.stop_notify(AB5E_RESP)
    log.debug("Capture: unsubscribed from AB5E_AUDIO and AB5E_RESP")

    total_raw  = sum(len(f) for f in frames)
    total_adpcm = sum(max(0, len(f) - 6) for f in frames)
    log.info(f"Captured {len(frames)} frames ({total_raw} B raw, {total_adpcm} B ADPCM)")
    log.debug(f"Capture: avg frame size = {total_raw // len(frames) if frames else 0} B")
    return frames


def _play_wav(wav_path: Path, log: logging.Logger | None = None) -> None:
    """Play a WAV file. Uses PowerShell SoundPlayer on Windows, aplay/paplay/sox on Linux."""
    print("  Playing back...")
    if sys.platform == "win32":
        if log:
            log.debug(f"Playback: PowerShell SoundPlayer — {wav_path}")
        subprocess.run(
            ["powershell", "-NoProfile", "-c",
             f"(New-Object Media.SoundPlayer '{wav_path.resolve()}').PlaySync()"],
            check=False,
        )
    else:
        for cmd in (
            ["aplay", str(wav_path)],
            ["paplay", str(wav_path)],
            ["sox", str(wav_path), "-d"],
        ):
            if log:
                log.debug(f"Playback: trying {cmd[0]}...")
            try:
                subprocess.run(cmd, check=True, capture_output=True)
                if log:
                    log.debug(f"Playback: {cmd[0]} succeeded")
                return
            except FileNotFoundError:
                if log:
                    log.debug(f"Playback: {cmd[0]} not found — skipping")
            except subprocess.CalledProcessError as e:
                if log:
                    log.debug(f"Playback: {cmd[0]} failed (rc={e.returncode}): "
                              f"{e.stderr.decode(errors='replace').strip()}")
        print("  No audio player found (install aplay or paplay)")


def _convert_and_play(ima_path: Path, wav_path: Path, log: logging.Logger) -> bool:
    """Convert raw IMA file to WAV via sox and play back.

    Packet IMA block headers (bytes 0-5: type+seq+pred+step) are already stripped;
    ima_path contains only raw ADPCM nibbles starting at packet offset 6.
    Returns True on success.
    """
    ima_size = ima_path.stat().st_size
    log.debug(f"sox input: {ima_path} ({ima_size} B)")
    sox_cmd = ["sox", "-t", "ima", "-e", "ima-adpcm", "-r", "16000", str(ima_path),
               "-e", "signed-integer", str(wav_path), "norm", "-12"]
    log.debug(f"sox command: {' '.join(sox_cmd)}")
    try:
        result = subprocess.run(sox_cmd, check=True, capture_output=True)
        if result.stderr:
            log.debug(f"sox stderr: {result.stderr.decode(errors='replace').strip()}")
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode(errors="replace")
        print(f"  sox error: {stderr}")
        log.error(f"sox failed (rc={e.returncode}): {stderr}")
        return False
    except FileNotFoundError:
        print("  sox not found in PATH")
        log.error("sox not found in PATH")
        return False

    wav_size = wav_path.stat().st_size
    log.debug(f"sox output: {wav_path} ({wav_size} B)")
    log.info(f"WAV: {wav_path}")
    print(f"  WAV: {wav_path}")
    _play_wav(wav_path, log)
    return True


# ─── History log ─────────────────────────────────────────────────────────────

_HISTORY_FILE = LOG_DIR / "history.log"


def _append_history(mac: str, model: str, fw: str, hw: str, battery: int,
                    frames: int, ok: bool) -> None:
    """Append one line per recording attempt to the cumulative history log."""
    LOG_DIR.mkdir(exist_ok=True)
    ts     = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    status = "OK" if ok else "NO_AUDIO"
    bat    = f"{battery}%" if battery >= 0 else "n/a"
    line   = (f"{ts}  {mac}  {model:<16}  "
              f"fw={fw or 'n/a':<12}  hw={hw or 'n/a':<8}  bat={bat:<4}  "
              f"frames={frames:4d}  {status}\n")
    with _HISTORY_FILE.open("a", encoding="utf-8") as f:
        f.write(line)


# ─── Summary log ──────────────────────────────────────────────────────────────

def _update_summary(mac: str, model: str, fw: str, hw: str,
                    log: logging.Logger) -> None:
    """Append or update the tested-devices JSON log and print running totals."""
    LOG_DIR.mkdir(exist_ok=True)
    try:
        data: dict = json.loads(_SUMMARY_FILE.read_text(encoding="utf-8")) if _SUMMARY_FILE.exists() else {}
    except Exception:
        data = {}

    ts = datetime.now().isoformat(timespec="seconds")
    if mac not in data:
        data[mac] = {"model": model, "fw": fw, "hw": hw,
                     "first_tested": ts, "last_tested": ts, "test_count": 1}
        log.debug(f"Summary: new device added — {mac}")
    else:
        data[mac].update({"model": model, "fw": fw, "hw": hw,
                          "last_tested": ts,
                          "test_count": data[mac].get("test_count", 0) + 1})
        log.debug(f"Summary: existing device updated — {mac} (test #{data[mac]['test_count']})")

    _SUMMARY_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info(f"Summary: {len(data)} unique device(s)")
    print(f"\n  Unique devices tested: {len(data)}")
    print(f"  This device ({mac}): tests — {data[mac]['test_count']}, first — {data[mac]['first_tested']}")


# ─── Input ────────────────────────────────────────────────────────────────────

def _read_key() -> str:
    """Read a single keypress without requiring Enter.

    Returns the uppercase character, empty string for Enter / unrecognised keys.
    Extended/escape sequences (arrows, F-keys) are consumed and discarded.
    """
    if sys.platform == "win32":
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
    else:
        import select
        import termios
        import tty
        fd = sys.stdin.fileno()
        try:
            old = termios.tcgetattr(fd)
        except termios.error:
            ch = sys.stdin.read(1)
            return "" if ch in ("\r", "\n", "") else ch.upper()
        try:
            tty.setraw(fd)
            ch = sys.stdin.read(1)
            if ch == "\x1b":
                if select.select([sys.stdin], [], [], 0.05)[0]:
                    sys.stdin.read(2)
                return ""
            if ch == "\x03":
                raise KeyboardInterrupt
            if ch in ("\r", "\n"):
                return ""
            return ch.upper()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


# ─── Test flow ────────────────────────────────────────────────────────────────

async def _test_one_device(client: BleakClient, seconds: int, log: logging.Logger) -> str:
    """Warm-up, run the record loop, unpair.  Returns the last user choice."""
    loop_ev = asyncio.get_running_loop()

    print("  Initialising...", end="", flush=True)
    if _DEBUG:
        await _debug_list_services(client, log)

    log.debug("Starting warmup cycle...")
    t0 = loop_ev.time()
    await _warmup(client, log)
    log.debug(f"Warmup complete in {loop_ev.time() - t0:.2f}s")
    print(" done.")

    log.debug("Reading device info...")
    info     = await _read_device_info(client, log)
    model    = info["model"] or "G100"
    mac      = client.address
    safe_mac = mac.replace(":", "")
    bat      = f"{info['battery']}%" if info["battery"] >= 0 else "n/a"
    fw       = info["fw"]  or "n/a"
    hw       = info["hw"]  or "n/a"
    print(f"  Device:   {model}  |  MAC: {mac}")
    print(f"  FW: {fw}  |  HW: {hw}  |  Battery: {bat}")
    log.info(f"model={model} fw={fw} hw={hw} battery={bat} addr={mac}")

    LOG_DIR.mkdir(exist_ok=True)
    choice = "Q"
    wav_path = LOG_DIR / f"test_{safe_mac}.wav"

    while True:
        print()
        print("  Speak into the microphone after the signal...")
        await asyncio.sleep(0.5)
        print("  *** GO ***")

        log.debug(f"Starting capture ({seconds}s)...")
        t0 = loop_ev.time()
        frames = await _capture_audio(client, seconds, log)
        log.debug(f"Capture finished in {loop_ev.time() - t0:.2f}s — {len(frames)} frames")

        if not frames:
            print("  No audio received.")
            log.warning("No audio frames captured.")
            _append_history(mac, model, fw, hw, info["battery"], 0, False)
        else:
            ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
            ima_path = LOG_DIR / f"test_{safe_mac}_{ts}.ima"
            raw_path = LOG_DIR / f"test_{safe_mac}_{ts}.bin"

            for old in LOG_DIR.glob(f"test_{safe_mac}.wav"):
                old.unlink(missing_ok=True)

            raw_bytes = b"".join(frames)
            raw_path.write_bytes(raw_bytes)
            log.debug(f"Raw .bin written: {raw_path} ({len(raw_bytes)} B)")

            adpcm_bytes = b"".join(f[6:] for f in frames if len(f) > 6)
            ima_path.write_bytes(adpcm_bytes)
            log.debug(f"IMA written: {ima_path} ({len(adpcm_bytes)} B ADPCM, "
                      f"skipped {sum(1 for f in frames if len(f) <= 6)} short frames)")

            ok = _convert_and_play(ima_path, wav_path, log)
            _append_history(mac, model, fw, hw, info["battery"], len(frames), ok)

            raw_path.unlink(missing_ok=True)
            ima_path.unlink(missing_ok=True)
            log.debug("Temp files cleaned up")

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
                    _play_wav(wav_path, log)
                else:
                    print("  No WAV file to play.")
            elif key == "Q":
                choice = "Q"
                break

        if choice in ("N", "Q"):
            break

    log.debug(f"Session choice: {choice} — disconnecting")
    try:
        await client.unpair()
        print("  Unpaired.")
        log.debug("Unpaired OK")
    except Exception as e:
        log.warning(f"unpair() failed: {e}")

    try:
        await client.disconnect()
        log.debug("Disconnected OK")
    except Exception as e:
        log.warning(f"disconnect error: {e}")

    _update_summary(mac, model, fw, hw, log)
    return choice


async def mode_test(seconds: int) -> None:
    """Cyclic mic test: scan → pair → record loop → unpair → next device."""
    log = _make_logger()
    log_path = _add_file_handler(log, "test")
    log.debug(f"Session started — seconds={seconds} debug={_DEBUG} log={log_path}")

    print()
    print("  ╔══════════════════════════════════════════╗")
    print("  ║    Gamepad Microphone Test — G100        ║")
    print("  ╚══════════════════════════════════════════╝")
    if _DEBUG:
        print(f"  [debug mode — log: {log_path}]")

    while True:
        print()
        print("  Waiting for device 'GAME'...")
        client = None
        while client is None:
            print("  Scanning...", end="\r", flush=True)
            client = await connect(log, scan_only=True)
            if client is None:
                log.debug("Device not found — retrying in 2s")
                await asyncio.sleep(2.0)

        log.debug(f"Device found and connected: {client.address}")
        choice = await _test_one_device(client, seconds, log)
        if choice == "Q":
            break

    log.debug("Session ended by user")
    print("\n  Session ended.")


# ─── CLI ──────────────────────────────────────────────────────────────────────

class _Tee:
    """Write to both a stream and a file simultaneously."""
    def __init__(self, stream, path: Path):
        self._stream = stream
        path.parent.mkdir(exist_ok=True)
        self._file = path.open("w", encoding="utf-8", errors="replace")

    def write(self, data):
        self._stream.write(data)
        self._file.write(data)

    def flush(self):
        self._stream.flush()
        self._file.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)


def main() -> None:
    global _DEBUG
    p = argparse.ArgumentParser(description="BLE Gamepad Mic Tester — Realtek G100")
    p.add_argument("--seconds", type=int, default=5, help="Record duration in seconds (default 5)")
    p.add_argument("--debug", action="store_true", help="Enable verbose debug output on console and in log files")
    args = p.parse_args()

    _DEBUG = args.debug

    # Tee all console output to logs/last_run.log
    _log_path = LOG_DIR / "last_run.log"
    sys.stdout = _Tee(sys.stdout, _log_path)
    sys.stderr = _Tee(sys.stderr, _log_path)

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
