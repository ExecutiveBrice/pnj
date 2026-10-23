"""say_stream threading: bounded queue, ordering, and barge-in cancellation."""

import threading

import numpy as np

from npc.tts import PiperSpeaker


def test_audition_catalog_uses_only_installed_piper_pairs_and_current_backend(tmp_path):
    from npc.config import TtsConfig
    from npc.engines.pocket import POCKET_VOICES
    from npc.tts import available_voices

    config = TtsConfig(voices_dir=str(tmp_path), voice="missing")
    (tmp_path / "installed.onnx").touch()
    (tmp_path / "installed.onnx.json").write_text("{}")
    (tmp_path / "incomplete.onnx").touch()
    assert available_voices(config) == ("installed",)
    config.backend = "pocket"
    assert available_voices(config) == POCKET_VOICES
    config.backend = "qwen"
    config.voice = "A calm voice."
    config.voices = {"guard": "A deep voice.", "smith": "A calm voice."}
    assert available_voices(config) == ("A calm voice.", "A deep voice.")


class StubPiperSpeaker(PiperSpeaker):
    """PiperSpeaker with the voice model stubbed out (no piper needed)."""

    def __init__(self, player):
        self._player = player
        self._cancel = threading.Event()
        self.local_playback = True

    def synthesize(self, text):
        return np.ones(len(text), dtype=np.int16), 22_050


class RecordingPlayer:
    def __init__(self):
        self.played = []

    def play(self, samples, sample_rate):
        self.played.append(len(samples))

    def stop(self):
        pass


def test_say_stream_plays_everything_in_order():
    player = RecordingPlayer()
    speaker = StubPiperSpeaker(player)
    spoken, cancelled = speaker.say_stream(iter(["one", "two", "three"]))
    assert spoken == ["one", "two", "three"]
    assert not cancelled
    assert player.played == [3, 3, 5]  # one synth per sentence, played in order


def test_say_stream_stop_cancels_playback_and_consumption():
    first_playing = threading.Event()

    class BlockingPlayer(RecordingPlayer):
        def __init__(self):
            super().__init__()
            self.unblock = threading.Event()

        def play(self, samples, sample_rate):
            super().play(samples, sample_rate)
            first_playing.set()
            self.unblock.wait(timeout=2)  # "audio playing" until stop()

        def stop(self):
            self.unblock.set()

    speaker = StubPiperSpeaker(BlockingPlayer())
    result = {}

    def run():
        result["r"] = speaker.say_stream(iter(["one", "two", "three", "four", "five"]))

    worker = threading.Thread(target=run)
    worker.start()
    assert first_playing.wait(timeout=2)
    speaker.stop()                                 # barge-in mid-first-sentence
    worker.join(timeout=2)
    assert not worker.is_alive()

    spoken, cancelled = result["r"]
    assert cancelled
    assert spoken == ["one"]                       # the rest never started playing
