#!/usr/bin/env python3
"""
Win32 Raw Input + WM_APPCOMMAND probe.

Creates a visible foreground window to catch:
  - WM_INPUT     (raw HID reports)
  - WM_APPCOMMAND (media/browser buttons: Back, Home, Play, etc.)

Usage:
    python raw_input_probe.py
"""

import ctypes
import ctypes.wintypes as wt
import sys

# ─── Constants ────────────────────────────────────────────────────────────────
WM_INPUT        = 0x00FF
WM_APPCOMMAND   = 0x0319
WM_DESTROY      = 0x0002
WM_CLOSE        = 0x0010
RIDEV_INPUTSINK = 0x00000100
RID_INPUT       = 0x10000003
RIM_TYPEHID     = 2
PM_REMOVE       = 0x0001
WS_OVERLAPPEDWINDOW = 0x00CF0000
SW_SHOW         = 5
HWND_TOP        = 0

APPCOMMANDS: dict[int, str] = {
    1:  "BROWSER_BACK",
    2:  "BROWSER_FORWARD",
    3:  "BROWSER_REFRESH",
    4:  "BROWSER_STOP",
    5:  "BROWSER_SEARCH",
    6:  "BROWSER_FAVORITES",
    7:  "BROWSER_HOME",
    8:  "VOLUME_MUTE",
    9:  "VOLUME_DOWN",
    10: "VOLUME_UP",
    11: "MEDIA_NEXT",
    12: "MEDIA_PREV",
    13: "MEDIA_STOP",
    14: "MEDIA_PLAY_PAUSE",
    46: "MEDIA_PLAY",
    47: "MEDIA_PAUSE",
    48: "MEDIA_RECORD",
    49: "MEDIA_FAST_FORWARD",
    50: "MEDIA_REWIND",
    27: "HELP",
}

# ─── Raw Input structures ──────────────────────────────────────────────────────
class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [
        ("usUsagePage", wt.USHORT),
        ("usUsage",     wt.USHORT),
        ("dwFlags",     wt.DWORD),
        ("hwndTarget",  wt.HWND),
    ]

class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [
        ("dwType",  wt.DWORD),
        ("dwSize",  wt.DWORD),
        ("hDevice", wt.HANDLE),
        ("wParam",  wt.WPARAM),
    ]

class RAWHID(ctypes.Structure):
    _fields_ = [
        ("dwSizeHid", wt.DWORD),
        ("dwCount",   wt.DWORD),
        ("bRawData",  ctypes.c_byte * 128),
    ]

class RAWINPUT(ctypes.Structure):
    _fields_ = [
        ("header", RAWINPUTHEADER),
        ("hid",    RAWHID),
    ]

class WNDCLASSEX(ctypes.Structure):
    _fields_ = [
        ("cbSize",        wt.UINT),
        ("style",         wt.UINT),
        ("lpfnWndProc",   ctypes.c_void_p),
        ("cbClsExtra",    ctypes.c_int),
        ("cbWndExtra",    ctypes.c_int),
        ("hInstance",     wt.HINSTANCE),
        ("hIcon",         wt.HANDLE),
        ("hCursor",       wt.HANDLE),
        ("hbrBackground", wt.HANDLE),
        ("lpszMenuName",  wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR),
        ("hIconSm",       wt.HANDLE),
    ]

class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd",    wt.HWND),
        ("message", wt.UINT),
        ("wParam",  wt.WPARAM),
        ("lParam",  wt.LPARAM),
        ("time",    wt.DWORD),
        ("pt",      wt.POINT),
    ]


# ─── Globals shared with WndProc ──────────────────────────────────────────────
_prev_hid: bytes | None = None
_running = True


def _handle_wm_input(lparam: int) -> None:
    global _prev_hid
    size = wt.UINT(0)
    ctypes.windll.user32.GetRawInputData(
        ctypes.cast(lparam, wt.HANDLE),
        RID_INPUT, None,
        ctypes.byref(size), ctypes.sizeof(RAWINPUTHEADER),
    )
    if size.value == 0:
        return
    buf = ctypes.create_string_buffer(size.value)
    ctypes.windll.user32.GetRawInputData(
        ctypes.cast(lparam, wt.HANDLE),
        RID_INPUT, buf,
        ctypes.byref(size), ctypes.sizeof(RAWINPUTHEADER),
    )
    ri = ctypes.cast(buf, ctypes.POINTER(RAWINPUT)).contents
    if ri.header.dwType != RIM_TYPEHID:
        return
    n    = ri.hid.dwSizeHid * ri.hid.dwCount
    data = bytes(ri.hid.bRawData[:n])
    if data and data != _prev_hid:
        parts = []
        for i, b in enumerate(data):
            if _prev_hid and i < len(_prev_hid) and b != _prev_hid[i]:
                parts.append(f"[{b:02x}]")
            else:
                parts.append(f" {b:02x} ")
        print(f"  HID        {''.join(parts)}")
        _prev_hid = data


def _handle_wm_appcommand(lparam: int) -> None:
    cmd = (lparam >> 16) & 0x0FFF
    name = APPCOMMANDS.get(cmd, f"UNKNOWN_{cmd}")
    print(f"  APPCOMMAND {name}  (cmd={cmd})")


# On 64-bit Windows WPARAM/LPARAM are 64-bit; wt.WPARAM/LPARAM are only 32-bit
_WPARAM = ctypes.c_size_t    # unsigned 64-bit on x64
_LPARAM = ctypes.c_ssize_t   # signed   64-bit on x64

WndProcType = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, wt.HWND, wt.UINT, _WPARAM, _LPARAM
)

_DefWindowProcW = ctypes.windll.user32.DefWindowProcW
_DefWindowProcW.argtypes = [wt.HWND, wt.UINT, _WPARAM, _LPARAM]
_DefWindowProcW.restype  = ctypes.c_ssize_t


@WndProcType
def _wnd_proc(hwnd: wt.HWND, msg: int, wparam: int, lparam: int) -> int:
    if msg == WM_INPUT:
        _handle_wm_input(lparam)
        return 0
    if msg == WM_APPCOMMAND:
        _handle_wm_appcommand(lparam)
        return 1
    if msg in (WM_DESTROY, WM_CLOSE):
        ctypes.windll.user32.PostQuitMessage(0)
        return 0
    return _DefWindowProcW(hwnd, msg, wparam, lparam)


# ─── Main ─────────────────────────────────────────────────────────────────────

def run() -> None:
    user32   = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    class_name = "RawInputProbeWnd"
    hinstance  = kernel32.GetModuleHandleW(None)

    wc               = WNDCLASSEX()
    wc.cbSize        = ctypes.sizeof(WNDCLASSEX)
    wc.style         = 0
    wc.lpfnWndProc   = ctypes.cast(_wnd_proc, ctypes.c_void_p)  # keep _wnd_proc alive
    wc.hInstance     = hinstance
    wc.hbrBackground = ctypes.cast(6, wt.HANDLE)   # COLOR_WINDOW+1
    wc.lpszClassName = class_name

    if not user32.RegisterClassExW(ctypes.byref(wc)):
        print(f"RegisterClassExW failed: {kernel32.GetLastError()}")
        return

    hwnd = user32.CreateWindowExW(
        0, class_name, "Raw Input Probe — press gamepad buttons, then close",
        WS_OVERLAPPEDWINDOW,
        100, 100, 600, 120,
        None, None, hinstance, None,
    )
    if not hwnd:
        print(f"CreateWindowExW failed: {kernel32.GetLastError()}")
        return

    user32.ShowWindow(hwnd, SW_SHOW)
    user32.UpdateWindow(hwnd)
    user32.SetForegroundWindow(hwnd)

    # Register for Consumer Controls raw input
    rid             = RAWINPUTDEVICE()
    rid.usUsagePage = 0x000C
    rid.usUsage     = 0x0001
    rid.dwFlags     = RIDEV_INPUTSINK
    rid.hwndTarget  = hwnd

    if not user32.RegisterRawInputDevices(
        ctypes.byref(rid), 1, ctypes.sizeof(RAWINPUTDEVICE)
    ):
        print(f"RegisterRawInputDevices failed: {kernel32.GetLastError()}")

    print("  Window open — click it to give it focus, then press gamepad buttons.")
    print("  Close the window or press Ctrl+C to stop.\n")

    msg = MSG()
    try:
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
    except KeyboardInterrupt:
        pass
    finally:
        user32.DestroyWindow(hwnd)
        user32.UnregisterClassW(class_name, hinstance)
        print("\n  Stopped.")


if __name__ == "__main__":
    run()
