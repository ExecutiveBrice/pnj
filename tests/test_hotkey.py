from types import SimpleNamespace

import pytest

from npc.hotkey import PTTListener, keycode_from_name

ecodes = pytest.importorskip("evdev.ecodes", reason="Linux evdev hotkey tests")

SPACE = ecodes.ecodes["KEY_SPACE"]


def event(code, value, type_=ecodes.EV_KEY):
    return SimpleNamespace(type=type_, code=code, value=value)


def make_listener():
    calls = []
    listener = PTTListener(
        devices=[], keycode=SPACE,
        on_press=lambda: calls.append("press"),
        on_release=lambda: calls.append("release"),
    )
    return listener, calls


def test_press_and_release_fire_once():
    listener, calls = make_listener()
    listener._handle_event(event(SPACE, 1))   # down
    listener._handle_event(event(SPACE, 2))   # auto-repeat, ignored
    listener._handle_event(event(SPACE, 2))
    listener._handle_event(event(SPACE, 0))   # up
    assert calls == ["press", "release"]


def test_release_without_press_is_ignored():
    listener, calls = make_listener()
    listener._handle_event(event(SPACE, 0))
    assert calls == []


def test_other_keys_and_event_types_ignored():
    listener, calls = make_listener()
    listener._handle_event(event(ecodes.ecodes["KEY_A"], 1))
    listener._handle_event(event(SPACE, 1, type_=ecodes.EV_SYN))
    assert calls == []


def test_keycode_from_name():
    assert keycode_from_name("KEY_SPACE") == SPACE
    assert keycode_from_name("KEY_F12") == ecodes.ecodes["KEY_F12"]
