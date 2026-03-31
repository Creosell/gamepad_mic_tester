#!/usr/bin/env python3
"""
BLE Gamepad Microphone Tester — Realtek G100-4722
Supports three modes: sniff, probe, record.

Usage:
    python gamepad_mic_tester.py sniff  [--seconds 15]
    python gamepad_mic_tester.py probe
    python gamepad_mic_tester.py record [--cmd "04 00"] [--seconds 5] [--uuid ab5e0003]

Dependencies:
    pip install bleak numpy sounddevice
"""

import argparse
import asyncio
import logging
import struct
import subprocess
import sys
import time
import threading
from datetime import datetime
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr.encoding and sys.stderr.encoding.lower() != "utf-8":
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import numpy as np
from bleak import BleakClient, BleakScanner

# ─── Device ───────────────────────────────────────────────────────────────────
KNOWN_ADDRESS = "F4:22:7A:4A:AA:E0"
KNOWN_ADDR_INT = int(KNOWN_ADDRESS.replace(":", ""), 16)
DEVICE_NAME   = "GAME"

# ─── UUIDs ────────────────────────────────────────────────────────────────────

# Realtek audio service (ab5e0001)
AB5E_CMD   = "ab5e0002-5a21-4f05-bc7d-af01f617b664"  # write: commands
AB5E_AUDIO = "ab5e0003-5a21-4f05-bc7d-af01f617b664"  # notify: audio data
AB5E_RESP  = "ab5e0004-5a21-4f05-bc7d-af01f617b664"  # notify: command responses

# ATV BLE service (6287)
ATV_WRITE  = "00006387-3c17-d293-8e48-14fe2e4da212"  # write: commands
ATV_READ   = "00006487-3c17-d293-8e48-14fe2e4da212"  # write+notify: data

# HID-like service (d1ff)
HID_NOTIFY = "0000a001-0000-1000-8000-00805f9b34fb"  # write-without-response+notify
HID_WRITE  = "0000a002-0000-1000-8000-00805f9b34fb"  # write

# ─── Config ───────────────────────────────────────────────────────────────────
LOG_DIR     = Path("logs")
SAMPLE_RATE = 16_000

# ─── Logging ──────────────────────────────────────────────────────────────────
_FMT = logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s", datefmt="%H:%M:%S")


def _make_logger(name: str = "mic_tester") -> logging.Logger:
    """Create a stderr logger; file handler added per-session."""
    log = logging.getLogger(name)
    log.setLevel(logging.DEBUG)
    if not log.handlers:
        ch = logging.StreamHandler()
        ch.setLevel(logging.INFO)
        ch.setFormatter(_FMT)
        log.addHandler(ch)
    return log


def _add_file_handler(log: logging.Logger, tag: str) -> Path:
    """Attach a timestamped file handler and return its path."""
    LOG_DIR.mkdir(exist_ok=True)
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = LOG_DIR / f"{tag}_{ts}.log"
    fh   = logging.FileHandler(path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(_FMT)
    log.addHandler(fh)
    return path


# ─── Connection ───────────────────────────────────────────────────────────────

async def connect(log: logging.Logger) -> BleakClient:
    """Connect to the gamepad.

    Tries scanning first; if the device is already bonded and not advertising,
    injects the known integer address directly into the WinRT backend to bypass
    the scanner (bonded devices don't advertise on Windows).
    """
    log.info(f"Scanning for '{DEVICE_NAME}' ...")
    dev = await BleakScanner.find_device_by_name(DEVICE_NAME, timeout=6.0)

    if dev:
        log.info(f"Found via scan: {dev.address}")
        client = BleakClient(dev, timeout=15.0)
    else:
        log.info(f"Not in scan — using bonded address {KNOWN_ADDRESS} (WinRT bypass)")
        client = BleakClient(KNOWN_ADDRESS, timeout=15.0)
        client._backend._device_info = KNOWN_ADDR_INT

    await client.connect()
    log.info(f"Connected: {client.is_connected}")
    return client


# ─── IMA ADPCM ────────────────────────────────────────────────────────────────
_STEP_TABLE = [
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31, 34,
    37, 41, 45, 50, 55, 60, 66, 73, 80, 88, 97, 107, 118, 130, 143,
    157, 173, 190, 209, 230, 253, 279, 307, 337, 371, 408, 449, 494,
    544, 598, 658, 724, 796, 876, 963, 1060, 1166, 1282, 1411, 1552,
    1707, 1878, 2066, 2272, 2499, 2749, 3024, 3327, 3660, 4026, 4428,
    4871, 5358, 5894, 6484, 7132, 7845, 8630, 9493, 10442, 11487,
    12635, 13899, 15289, 16818, 18500, 20350, 22385, 24623, 27086,
    29794, 32767,
]
_IDX_TABLE = [-1, -1, -1, -1, 2, 4, 6, 8, -1, -1, -1, -1, 2, 4, 6, 8]


def _decode_adpcm_stream(stream: bytes, hi_nibble_first: bool = False) -> np.ndarray:
    """Decode raw IMA ADPCM byte stream to int16 samples.

    Args:
        stream: concatenated ADPCM bytes (no headers).
        hi_nibble_first: if True, process high nibble before low nibble per byte
                         (some devices reverse the standard nibble order).
    """
    pred = 0
    sidx = 0
    samples: list[int] = []
    for byte in stream:
        lo, hi = byte & 0x0F, (byte >> 4) & 0x0F
        nibbles = (hi, lo) if hi_nibble_first else (lo, hi)
        for nibble in nibbles:
            step = _STEP_TABLE[sidx]
            diff = step >> 3
            if nibble & 1: diff += step >> 2
            if nibble & 2: diff += step >> 1
            if nibble & 4: diff += step
            if nibble & 8: diff = -diff
            pred = max(-32768, min(32767, pred + diff))
            sidx = max(0, min(88, sidx + _IDX_TABLE[nibble]))
            samples.append(pred)
    return np.array(samples, dtype=np.int16)


def _decode_adpcm_block_headers(frames: list[bytes], pkt_header: int = 3) -> np.ndarray:
    """Decode ADPCM where each packet contains a 4-byte IMA block header.

    Block header layout (after pkt_header bytes):
      bytes 0-1 : initial predictor (LE int16)
      byte  2   : initial step index
      byte  3   : reserved (ignored)
      bytes 4+  : ADPCM nibbles

    Each packet resets decoder state independently.
    """
    samples: list[int] = []
    for frame in frames:
        off = pkt_header
        if len(frame) < off + 5:
            continue
        pred = struct.unpack_from("<h", frame, off)[0]
        sidx = max(0, min(88, frame[off + 2]))
        payload = frame[off + 4:]
        for byte in payload:
            for nibble in (byte & 0x0F, (byte >> 4) & 0x0F):
                step = _STEP_TABLE[sidx]
                diff = step >> 3
                if nibble & 1: diff += step >> 2
                if nibble & 2: diff += step >> 1
                if nibble & 4: diff += step
                if nibble & 8: diff = -diff
                pred = max(-32768, min(32767, pred + diff))
                sidx = max(0, min(88, sidx + _IDX_TABLE[nibble]))
                samples.append(pred)
    return np.array(samples, dtype=np.int16)


def decode_frames(frames: list[bytes], header_offset: int = 3) -> np.ndarray:
    """Decode Realtek ab5e audio stream from a list of BLE notification packets.

    Packet layout (confirmed from probe log):
      byte 0    : 0x07 — audio packet type marker
      bytes 1-2 : sequence number LE uint16
      bytes 3+  : continuous IMA ADPCM nibble stream (no per-packet header)

    ADPCM state (predictor + step_index) is maintained across packets.
    The stream is decoded as a whole after stripping the 3-byte packet header
    from each frame and concatenating the payloads.

    Args:
        frames: raw notification payloads collected from ab5e0003.
        header_offset: bytes to skip at the start of each packet (default 3).
    """
    if not frames:
        return np.array([], dtype=np.int16)

    # Concatenate all payloads (skip per-packet header bytes)
    stream = b"".join(f[header_offset:] for f in frames if len(f) > header_offset)
    if not stream:
        return np.array([], dtype=np.int16)

    return _decode_adpcm_stream(stream)


# ─── Probe commands ───────────────────────────────────────────────────────────
_PROBE_COMMANDS: list[tuple[str, bytes, str]] = [
    # (target_char_uuid, payload, description)
    (AB5E_CMD, bytes([0x0C, 0x00]), "ab5e GET_CAPS"),
    (AB5E_CMD, bytes([0x04, 0x00]), "ab5e START 0x04 0x00"),
    (AB5E_CMD, bytes([0x04, 0x01]), "ab5e START 0x04 0x01"),
    (AB5E_CMD, bytes([0x04, 0x0F]), "ab5e START 0x04 0x0F"),
    (AB5E_CMD, bytes([0x08, 0x00]), "ab5e START 0x08 0x00"),
    (AB5E_CMD, bytes([0x01, 0x00]), "ab5e START 0x01 0x00"),
    (AB5E_CMD, bytes([0x02, 0x00]), "ab5e START 0x02 0x00"),
    (AB5E_CMD, bytes([0x03, 0x00]), "ab5e START 0x03 0x00"),
    (AB5E_CMD, bytes([0x05, 0x00]), "ab5e START 0x05 0x00"),
    (AB5E_CMD, bytes([0x06, 0x00]), "ab5e START 0x06 0x00"),
    (AB5E_CMD, bytes([0x0A, 0x00]), "ab5e 0x0A"),
    (AB5E_CMD, bytes([0x0B, 0x00]), "ab5e 0x0B"),
    (ATV_WRITE, bytes([0x04, 0x00]), "ATV OPEN_MIC 0x04 0x00"),
    (ATV_WRITE, bytes([0x0A, 0x00]), "ATV GET_CAPS 0x0A"),
    (ATV_WRITE, bytes([0x0C, 0x00]), "ATV 0x0C"),
    (HID_WRITE, bytes([0x0C, 0x00]), "HID write 0x0C 0x00"),
    (HID_WRITE, bytes([0x04, 0x00]), "HID write 0x04 0x00"),
]


# ─── Audio post-processing ───────────────────────────────────────────────────

def _highpass_numpy(signal: np.ndarray, cutoff: float, sr: int, order: int = 4) -> np.ndarray:
    """N-th order high-pass filter implemented as cascaded first-order IIR sections.

    Works without scipy.  Each pass applies the recurrence:
        y[n] = alpha * (y[n-1] + x[n] - x[n-1])
    where alpha = tau / (tau + dt), tau = 1 / (2*pi*cutoff).
    Running forward then backward (zero-phase) halves the effective order per pair.
    """
    import math
    dt    = 1.0 / sr
    tau   = 1.0 / (2.0 * math.pi * cutoff)
    alpha = tau / (tau + dt)

    y = signal.astype(np.float64)
    for _ in range(order):
        # forward pass
        out = np.empty_like(y)
        out[0] = 0.0
        for i in range(1, len(y)):
            out[i] = alpha * (out[i-1] + y[i] - y[i-1])
        # backward pass (zero-phase)
        y2 = np.empty_like(out)
        y2[-1] = 0.0
        for i in range(len(out)-2, -1, -1):
            y2[i] = alpha * (y2[i+1] + out[i] - out[i+1])
        y = y2
    return y.astype(np.float32)


def _postprocess(pcm: np.ndarray, sr: int) -> np.ndarray:
    """De-clip, high-pass filter, and RMS-normalise decoded PCM.

    Pipeline:
      1. Subtract DC offset (mean).
      2. Soft de-clipping: linear interpolation over saturated runs.
      3. High-pass at 280 Hz via scipy Butterworth (order 6) if available,
         otherwise cascaded IIR high-pass at 280 Hz (numpy-only, zero-phase).
         This removes 50/100/150 Hz mains hum and any remaining DC drift.
      4. RMS normalisation to -12 dBFS.
    """
    audio = pcm.astype(np.float32)

    # 1. Remove DC offset
    audio -= float(np.mean(audio))

    # 2. Soft de-clipping
    CLIP = 32760.0
    idx  = np.where(np.abs(audio) >= CLIP)[0]
    if len(idx):
        breaks = np.where(np.diff(idx) > 1)[0] + 1
        for run in np.split(idx, breaks):
            a, b = run[0], run[-1]
            n = b - a + 1
            if a > 0 and b < len(audio) - 1 and n > 0:
                audio[a:b+1] = np.linspace(float(audio[a-1]), float(audio[b+1]), n + 2)[1:-1]

    # 3. High-pass at 280 Hz
    try:
        from scipy.signal import butter, sosfiltfilt
        sos   = butter(6, 280.0 / (sr / 2.0), btype="high", output="sos")
        audio = sosfiltfilt(sos, audio).astype(np.float32)
    except ImportError:
        audio = _highpass_numpy(audio, 280.0, sr, order=4)

    # 4. RMS normalisation to -12 dBFS
    rms = float(np.sqrt(np.mean(audio ** 2)))
    if rms > 1e-6:
        audio *= (0.25 * 32768.0) / rms
    audio = np.clip(audio, -32768.0, 32767.0)
    return audio / 32768.0


def _save_wav(path: Path, audio_f32: np.ndarray, sr: int) -> None:
    """Write float32 [-1,1] audio to a 16-bit PCM WAV file without extra deps."""
    import wave
    pcm16 = (audio_f32 * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm16.tobytes())


# ─── Modes ────────────────────────────────────────────────────────────────────

async def mode_sniff(seconds: int) -> None:
    """Subscribe to all notify characteristics and dump every byte received."""
    log = _make_logger()
    log_path = _add_file_handler(log, "sniff")
    log.info(f"Log: {log_path}")

    client = await connect(log)
    frames_by_uuid: dict[str, list[bytes]] = {}

    def make_cb(uuid: str, label: str):
        frames_by_uuid[uuid] = []
        def cb(sender, data: bytearray):
            b = bytes(data)
            frames_by_uuid[uuid].append(b)
            n = len(frames_by_uuid[uuid])
            ts = time.strftime("%H:%M:%S")
            hex_s = b.hex()
            print(f"  [{ts}] {label} #{n:4d}  {len(b):3d}B  {hex_s[:72]}")
            log.debug(f"NOTIFY {uuid} #{n} {len(b)}B {hex_s}")
        return cb

    subs = []
    for svc in client.services:
        for ch in svc.characteristics:
            if set(ch.properties) & {"notify", "indicate"}:
                label = ch.uuid[:8]
                try:
                    await client.start_notify(ch.uuid, make_cb(ch.uuid, label))
                    subs.append(ch.uuid)
                    log.info(f"Subscribed: {ch.uuid}")
                except Exception as e:
                    log.warning(f"Could not subscribe {ch.uuid}: {e}")

    # GET_CAPS to confirm ab5e is alive
    try:
        await client.write_gatt_char(AB5E_CMD, bytes([0x0C, 0x00]), response=False)
        log.info("Sent GET_CAPS (0x0C 0x00) to ab5e0002")
    except Exception as e:
        log.warning(f"GET_CAPS failed: {e}")

    print()
    print(f"  >>> PRESS ASSISTANT BUTTON NOW — listening {seconds}s <<<")
    print()

    await asyncio.sleep(seconds)

    for uuid in subs:
        try:
            await client.stop_notify(uuid)
        except Exception:
            pass
    await client.disconnect()

    # Save raw frames
    LOG_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    print("\n  ── Summary ──────────────────────────────────────────────────────")
    for uuid, frames in frames_by_uuid.items():
        if not frames:
            continue
        sizes = sorted(set(len(f) for f in frames))
        total = sum(len(f) for f in frames)
        print(f"  {uuid[:8]}  {len(frames):4d} frames  {total:6d} B  sizes={sizes}")
        log.info(f"SUMMARY {uuid} frames={len(frames)} bytes={total} sizes={sizes}")
        raw_path = LOG_DIR / f"raw_{uuid[:8]}_{ts}.bin"
        raw_path.write_bytes(b"".join(frames))
        print(f"           → saved {raw_path}")


async def mode_probe() -> None:
    """Send candidate commands one by one, log every response received."""
    log = _make_logger()
    log_path = _add_file_handler(log, "probe")
    log.info(f"Log: {log_path}")

    client = await connect(log)
    responses: list[tuple[float, str, bytes]] = []

    def make_cb(label: str):
        def cb(sender, data: bytearray):
            b = bytes(data)
            ts = time.time()
            responses.append((ts, label, b))
            print(f"    ← [{label}]  {b.hex()}")
            log.debug(f"RESP [{label}] {b.hex()}")
        return cb

    # Subscribe to all response channels
    resp_uuids = {
        AB5E_RESP: "ab5e/resp",
        AB5E_AUDIO: "ab5e/audio",
        ATV_READ: "atv/read",
        HID_NOTIFY: "hid/notify",
    }
    subs = []
    for uuid, label in resp_uuids.items():
        try:
            await client.start_notify(uuid, make_cb(label))
            subs.append(uuid)
        except Exception as e:
            log.warning(f"Could not subscribe {uuid}: {e}")

    print()
    print("  Probing commands (0.6s gap between each)...")
    print()

    before_counts = {label: 0 for label in resp_uuids.values()}

    for char_uuid, payload, desc in _PROBE_COMMANDS:
        count_before = len(responses)
        hex_payload = payload.hex(" ")
        print(f"  → {desc:<32s}  [{hex_payload}]")
        log.info(f"PROBE {desc} → {char_uuid[:8]} [{hex_payload}]")
        try:
            await client.write_gatt_char(char_uuid, payload, response=False)
        except Exception as e:
            print(f"       write error: {e}")
            log.warning(f"  write error: {e}")
        await asyncio.sleep(0.6)
        n_new = len(responses) - count_before
        if n_new:
            log.info(f"  → triggered {n_new} response(s)")

    # Also listen 3 more seconds passively in case something is delayed
    print()
    print("  Listening 3s passively for delayed responses...")
    await asyncio.sleep(3)

    for uuid in subs:
        try:
            await client.stop_notify(uuid)
        except Exception:
            pass
    await client.disconnect()

    print()
    print("  ── Probe results ────────────────────────────────────────────────")
    print(f"  Total responses received: {len(responses)}")
    for ts, label, data in responses:
        print(f"  [{label}]  {data.hex()}")


async def mode_record(cmd_hex: str, seconds: int, audio_uuid: str) -> None:
    """Send one activation command then capture audio notifications and play back."""
    # sounddevice imported late — importing it early triggers PortAudio/WASAPI COM
    # initialisation as STA, which breaks bleak's WinRT MTA requirement.

    log = _make_logger()
    log_path = _add_file_handler(log, "record")
    log.info(f"Log: {log_path}")

    # Parse command bytes
    try:
        cmd = bytes.fromhex(cmd_hex.replace(" ", ""))
    except ValueError as e:
        print(f"Invalid --cmd format: {e}  (use hex like '04 00' or '0400')")
        return

    client = await connect(log)
    frames: list[bytes] = []
    recording = False

    def on_audio(sender, data: bytearray):
        if recording:
            b = bytes(data)
            frames.append(b)
            if len(frames) % 20 == 1:
                print(f"  [audio] frame #{len(frames)}  {len(b)}B  {b.hex()[:40]}")

    def on_resp(sender, data: bytearray):
        print(f"  [resp]  {bytes(data).hex()}")
        log.debug(f"RESP {bytes(data).hex()}")

    await client.start_notify(audio_uuid, on_audio)
    await client.start_notify(AB5E_RESP, on_resp)

    # Step 1: GET_CAPS handshake — device requires this before START
    log.info("Sending GET_CAPS (0x0C 0x00) to ab5e0002")
    try:
        await client.write_gatt_char(AB5E_CMD, bytes([0x0C, 0x00]), response=False)
    except Exception as e:
        log.warning(f"GET_CAPS failed: {e}")
    await asyncio.sleep(0.4)

    # Step 2: enable recording before sending start so no early frames are lost
    recording = True

    log.info(f"Sending cmd [{cmd.hex(' ')}] to ab5e0002")
    try:
        await client.write_gatt_char(AB5E_CMD, cmd, response=False)
    except Exception as e:
        log.warning(f"Write to ab5e0002 failed: {e}")

    print(f"\n  >> Recording {seconds}s — говорите в микрофон ...")
    await asyncio.sleep(seconds)
    recording = False

    # Stop mic
    for stop_cmd in [bytes([0x0B, 0x00]), bytes([0x00, 0x00])]:
        try:
            await client.write_gatt_char(AB5E_CMD, stop_cmd, response=False)
            await asyncio.sleep(0.1)
        except Exception:
            pass

    await client.stop_notify(audio_uuid)
    await client.stop_notify(AB5E_RESP)
    await client.disconnect()

    total_bytes = sum(len(f) for f in frames)
    log.info(f"Captured {len(frames)} frames, {total_bytes} bytes")

    if not frames:
        print("  No audio data received.")
        return

    # Save raw binary
    LOG_DIR.mkdir(exist_ok=True)
    ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    raw_path = LOG_DIR / f"raw_audio_{ts_str}.bin"
    raw_path.write_bytes(b"".join(frames))
    print(f"\n  Raw saved: {raw_path}")
    print(f"  Frames: {len(frames)}  Total: {total_bytes} B")
    print(f"  First frame: {frames[0].hex()[:60]}")

    # Вместо decode_frames + _postprocess + _save_wav
    stream = b"".join(f[4:] for f in frames if len(f) > 4)
    raw_path = LOG_DIR / f"raw_audio_{ts_str}.raw"
    raw_path.write_bytes(stream)

    wav_path = LOG_DIR / f"audio_{ts_str}.wav"
    subprocess.run([
        "sox", "-t", "ima", "-e", "ima-adpcm", "-r", "16000", "-N",
        str(raw_path), "-e", "signed-integer", str(wav_path), "vol", "0.25"
    ], check=True)

    print("  Playing back...")
    import sounddevice as sd  # imported here so PortAudio COM init doesn't affect BLE
    import soundfile as sf
    data, fs = sf.read(str(wav_path))
    sd.play(data, samplerate=fs, blocking=True)
    print("  Playback done.")


def mode_analyze(raw_path: Path, frame_size: int) -> None:
    """Decode a raw binary dump with multiple ADPCM variants and save WAV files.

    Splits the binary into fixed-size frames (use the packet size reported by the
    device, e.g. 134 bytes for G100).  For each variant the pipeline is:
    decode → _postprocess → save WAV.  Filenames encode the variant parameters so
    the user can listen and compare without remembering which file is which.

    Variants tried:
      adpcm_16k_lo3  — current default (low nibble first, 16 kHz, 3-byte header)
      adpcm_16k_hi3  — high nibble first (some devices reverse nibble order)
      adpcm_8k_lo3   — same as default but interpreted as 8 kHz
      adpcm_blk_16k  — per-packet IMA block header (resets state each packet)
      pcm16_8k       — raw LE int16 at 8 kHz (no ADPCM)
      pcm16_16k      — raw LE int16 at 16 kHz (no ADPCM)
    """
    log = _make_logger()
    if not raw_path.exists():
        print(f"File not found: {raw_path}")
        return

    data = raw_path.read_bytes()
    total = len(data)
    log.info(f"Loaded {total} bytes from {raw_path}")

    # Split into frames
    if frame_size > 0 and total >= frame_size:
        n_frames  = total // frame_size
        remainder = total % frame_size
        frames = [data[i * frame_size:(i + 1) * frame_size] for i in range(n_frames)]
        print(f"  {n_frames} frames × {frame_size}B  (remainder {remainder}B)")
    else:
        frames = [data]
        print(f"  Treating {total}B as one continuous blob (no frame split)")

    out_dir = raw_path.parent
    base    = raw_path.stem

    def _save_variant(name: str, pcm: np.ndarray, sr: int) -> None:
        if pcm.size < 100:
            print(f"  {name}: {pcm.size} samples — skipped (too few)")
            return
        duration = pcm.size / sr
        rms      = float(np.sqrt(np.mean(pcm.astype(np.float32) ** 2)))
        audio    = _postprocess(pcm, sr)
        wav_path = out_dir / f"{base}_{name}.wav"
        _save_wav(wav_path, audio, sr)
        print(f"  {name:<22s}  {pcm.size:7d} samples  {duration:5.2f}s  RMS={rms:6.0f}  → {wav_path.name}")

    pkt_header = 3  # type byte + seq LE16

    # Variant 1: current decoder (continuous stream, low nibble first, 16 kHz)
    stream = b"".join(f[pkt_header:] for f in frames if len(f) > pkt_header)
    _save_variant("adpcm_16k_lo3",  _decode_adpcm_stream(stream, hi_nibble_first=False), 16000)

    # Variant 2: high nibble first
    _save_variant("adpcm_16k_hi3",  _decode_adpcm_stream(stream, hi_nibble_first=True),  16000)

    # Variant 3: same stream decoded as 8 kHz
    _save_variant("adpcm_8k_lo3",   _decode_adpcm_stream(stream, hi_nibble_first=False),  8000)

    # Variant 4: per-packet IMA block header (predictor+step_index in bytes 3-6)
    _save_variant("adpcm_blk_16k",  _decode_adpcm_block_headers(frames, pkt_header),     16000)

    # Variant 5-6: raw 16-bit PCM (no ADPCM)
    raw_payload = b"".join(f[pkt_header:] for f in frames if len(f) > pkt_header)
    if len(raw_payload) % 2:
        raw_payload = raw_payload[:-1]
    pcm16 = np.frombuffer(raw_payload, dtype=np.int16)
    _save_variant("pcm16_8k",  pcm16.copy(),  8000)
    _save_variant("pcm16_16k", pcm16.copy(), 16000)

    print(f"\n  All variants saved to {out_dir}/")


# ─── CLI ──────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="BLE Gamepad Mic Tester — Realtek G100")
    sub = p.add_subparsers(dest="mode", required=True)

    s = sub.add_parser("sniff", help="Subscribe to all notify chars and dump traffic")
    s.add_argument("--seconds", type=int, default=15, help="Listen duration (default 15)")

    sub.add_parser("probe", help="Send candidate activation commands and log responses")

    r = sub.add_parser("record", help="Send activation command and capture audio")
    r.add_argument("--cmd",     default="0a 00",   help="Hex command bytes (default '0a 00')")
    r.add_argument("--seconds", type=int, default=5, help="Record duration (default 5)")
    r.add_argument("--uuid",    default=AB5E_AUDIO, help="Audio notify UUID to subscribe")

    a = sub.add_parser("analyze", help="Offline: decode raw binary with multiple ADPCM variants")
    a.add_argument("raw",        type=Path, nargs="?",
                   default=Path("logs/raw_audio_20260331_110633.bin"),
                   help="Path to raw binary (default: logs/raw_audio_20260331_110633.bin)")
    a.add_argument("--frame-size", type=int, default=134,
                   help="Bytes per BLE notification packet (default 134)")

    return p.parse_args()


def main() -> None:
    args = _parse_args()

    # analyze runs synchronously — no BLE, no asyncio needed
    if args.mode == "analyze":
        mode_analyze(args.raw, args.frame_size)
        return

    if args.mode == "sniff":
        coro = mode_sniff(args.seconds)
    elif args.mode == "probe":
        coro = mode_probe()
    else:
        coro = mode_record(args.cmd, args.seconds, args.uuid)

    exc: BaseException | None = None

    def _run():
        nonlocal exc
        try:
            asyncio.run(coro)
        except KeyboardInterrupt:
            pass
        except BaseException as e:
            exc = e

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    try:
        t.join()
    except KeyboardInterrupt:
        print("\nПрервано.")
        sys.exit(0)

    if exc is not None:
        raise exc


if __name__ == "__main__":
    main()
