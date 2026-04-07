#!/usr/bin/env python3
"""Quick test: does pynput catch Consumer Controls buttons (Back/Home/Play/SalutLogo)?"""

from pynput import keyboard

VK_NAMES = {
    0xA6: "VK_BROWSER_BACK",
    0xA7: "VK_BROWSER_FORWARD",
    0xAC: "VK_BROWSER_HOME",
    0xB3: "VK_MEDIA_PLAY_PAUSE",
    0xB0: "VK_MEDIA_NEXT_TRACK",
    0xB1: "VK_MEDIA_PREV_TRACK",
    0xAD: "VK_VOLUME_MUTE",
}

def on_press(key):
    vk = getattr(key, "vk", None)
    name = VK_NAMES.get(vk, "")
    tag = f"  ← {name}" if name else ""
    print(f"  PRESS   {key}{tag}")

def on_release(key):
    vk = getattr(key, "vk", None)
    name = VK_NAMES.get(vk, "")
    tag = f"  ← {name}" if name else ""
    print(f"  release {key}{tag}")
    if key == keyboard.Key.esc:
        return False

print("Listening... press Back / Home / Play / SalutLogo on gamepad (Esc to quit)\n")
with keyboard.Listener(on_press=on_press, on_release=on_release) as l:
    l.join()
