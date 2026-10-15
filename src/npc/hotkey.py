"""Push-to-talk via evdev: real key-down/up events on X11 and Wayland alike.

A future dedicated USB button is just another /dev/input device — pin it with
`hotkey.device` in config.toml and optionally `grab = true` so its presses
never reach the terminal.
"""

from __future__ import annotations

import glob
import selectors
import sys
import threading
from typing import Callable


class HotkeyUnavailable(Exception):
    pass


def keycode_from_name(name: str) -> int:
    if sys.platform != "linux":
        raise HotkeyUnavailable("global push-to-talk requires Linux; use `npc gui` instead")
    from evdev import ecodes

    try:
        return ecodes.ecodes[name]
    except KeyError:
        raise HotkeyUnavailable(f"unknown key name {name!r} (try e.g. KEY_SPACE, KEY_F12)")


def find_ptt_devices(keycode: int, device_path: str = ""):
    """All readable input devices that can emit the configured key."""
    import evdev
    from evdev import ecodes

    if device_path:
        try:
            return [evdev.InputDevice(device_path)]
        except (PermissionError, OSError) as e:
            raise HotkeyUnavailable(f"cannot open {device_path}: {e}")

    devices = []
    # list_devices() silently omits devices we lack permission for
    accessible = evdev.list_devices()
    for path in accessible:
        try:
            dev = evdev.InputDevice(path)
        except OSError:
            continue
        if keycode in dev.capabilities().get(ecodes.EV_KEY, []):
            devices.append(dev)
        else:
            dev.close()

    if not devices:
        hidden = set(glob.glob("/dev/input/event*")) - set(accessible)
        if hidden:
            raise HotkeyUnavailable(
                "no permission to read /dev/input devices — run "
                "`sudo usermod -aG input $USER`, then log out and back in"
            )
        raise HotkeyUnavailable("no input device with the configured key found")
    return devices


class PTTListener:
    """Watches devices for the push-to-talk key; fires on_press/on_release.

    _handle_event is a pure state machine (value 1=down, 2=repeat, 0=up)
    so it is unit-testable with synthetic events.
    """

    def __init__(self, devices, keycode: int,
                 on_press: Callable[[], None], on_release: Callable[[], None],
                 grab: bool = False):
        self.devices = devices
        self.keycode = keycode
        self.on_press = on_press
        self.on_release = on_release
        self._grab = grab
        self._down = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _handle_event(self, event) -> None:
        from evdev import ecodes

        if event.type != ecodes.EV_KEY or event.code != self.keycode:
            return
        if event.value == 1 and not self._down:
            self._down = True
            self.on_press()
        elif event.value == 0 and self._down:
            self._down = False
            self.on_release()
        # value == 2 (auto-repeat) is ignored

    def run_forever(self) -> None:
        selector = selectors.DefaultSelector()
        for dev in self.devices:
            if self._grab:
                dev.grab()
            selector.register(dev, selectors.EVENT_READ)
        try:
            while not self._stop.is_set():
                for key, _ in selector.select(timeout=0.2):
                    try:
                        for event in key.fileobj.read():
                            self._handle_event(event)
                    except OSError:
                        selector.unregister(key.fileobj)
        finally:
            for dev in self.devices:
                try:
                    if self._grab:
                        dev.ungrab()
                    dev.close()
                except OSError:
                    pass

    def start(self) -> None:
        self._thread = threading.Thread(target=self.run_forever, daemon=True,
                                        name="ptt-listener")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)
