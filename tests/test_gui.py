"""Opt-in tests of real Tk widgets. NPC_TEST_GUI=1 pytest tests/test_gui.py."""

import os
import queue

import pytest

pytestmark = [pytest.mark.gui, pytest.mark.skipif(
    os.environ.get("NPC_TEST_GUI") != "1", reason="set NPC_TEST_GUI=1 with a desktop display")]


@pytest.fixture
def window(campaign):
    import tkinter as tk

    from npc.gui import DesktopWindow

    root = tk.Tk()
    window = DesktopWindow(root, str(campaign))
    root.update()
    yield window
    root.destroy()


@pytest.mark.parametrize("geometry", ["1000x800", "900x650"])
def test_desktop_controls_stay_inside_window(window, geometry):
    root = window.root
    root.geometry(geometry)
    root.update()
    for widget in (window.path_entry, window.start_button, window.mic_option,
                   window.entry, window.record_button, window.language_target_picker,
                   window.language_picker, window.speech_picker,
                   *window.shortcuts, *window.speech_buttons):
        assert widget.winfo_ismapped()
        x = widget.winfo_rootx() - root.winfo_rootx()
        y = widget.winfo_rooty() - root.winfo_rooty()
        assert 0 <= x and x + widget.winfo_width() <= root.winfo_width()
        assert 0 <= y and y + widget.winfo_height() <= root.winfo_height()


def test_desktop_player_gm_and_shutdown_controls(window):
    class Engine:
        process = None
        messages = queue.Queue()

        def __init__(self):
            self.sent = []

        def start(self, *args, **kwargs):
            self.process = object()
            self.messages.put({"kind": "ready", "microphone": True, "npc": "Vess",
                               "npc_id": "character", "npcs": [
                                   {"id": "character", "name": "Vess"},
                                   {"id": "smith", "name": "Smith"}]})

        def send(self, action, **values):
            self.sent.append((action, values))

    window.engine = engine = Engine()
    window.start_button.invoke()
    assert str(window.start_button["state"]) == "disabled"
    window.poll()
    window.text.set("Who are you?")
    window.send_button.invoke()
    window.mode.set("Note du MJ")
    window.text.set("Be suspicious")
    window.send_button.invoke()
    assert engine.sent == [("line", {"text": "/say Who are you?"}),
                           ("line", {"text": "Be suspicious"})]
    window.speech_target.set("Smith (smith)")
    for button in window.speech_buttons:
        button.invoke()
    assert engine.sent[-4:] == [("speak_npc", {"npc": "smith", "attitude": attitude})
                               for attitude in ("agacement", "amusant", "rude", "complaisant")]
    window.record_button.invoke()
    assert all(str(button["state"]) == "disabled" for button in window.speech_buttons)
    window.record_button.invoke()
    assert engine.sent[-2:] == [("record", {}), ("release", {})]
    window.stop_button.invoke()
    assert engine.sent[-1] == ("shutdown", {"summarize": True})
    assert str(window.entry["state"]) == "disabled"
    assert all(str(button["state"]) == "disabled" for button in window.speech_buttons)
    assert str(window.force_button["state"]) == "normal"


def test_desktop_saves_campaign_and_character_language(window):
    from pathlib import Path

    from npc.config import load_config

    window.language.set("Français (Belgique)")
    window.change_language()
    config = load_config(Path(window.path.get()))
    assert config.language == "fr-BE"
    target = next(label for label, stem in window.language_targets.items() if stem == "character")
    window.language_target.set(target)
    window.refresh_language()
    assert window.language.get() == "Suivre la campagne"
    window.language.set("Français (Suisse)")
    window.change_language()
    window.refresh_languages()
    window.language_target.set(target)
    window.refresh_language()
    assert window.language.get() == "Français (Suisse)"
    config = load_config(Path(window.path.get()))
    assert config.language_for("character") == "fr-CH"
    window.language.set("Suivre la campagne")
    window.change_language()
    assert load_config(Path(window.path.get())).language_for("character") == "fr-BE"
