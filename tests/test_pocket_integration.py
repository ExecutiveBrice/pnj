"""Small integration checks for voice settings, native streaming and interruption."""

import sys
import threading
from types import SimpleNamespace

import numpy as np

from npc.audio.player import AudioPlayer
from npc.config import load_config, save_tts
from npc.pocket import PocketSpeaker, _RemoteAudioClock


def test_remote_audio_clock_compensates_scheduling_delay():
    now = [100.0]

    class Scheduler:
        def wait(self, seconds):
            now[0] += seconds + 0.006  # Windows/network scheduling overhead

    clock = _RemoteAudioClock(Scheduler(), 24000, clock=lambda: now[0])
    for _ in range(60):
        clock.advance(1920)
    clock.finish()
    # Per-chunk sleeps would accumulate 360 ms; an absolute clock keeps only
    # the final scheduling error, while maintaining a bounded 160 ms lead.
    assert abs(now[0] - 104.806) < 0.00001


def test_switching_engines_restores_character_voices(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('language = "fr"\n[tts]\nvoice = "fr_FR-siwis-medium"\n'
                    '[tts.voices]\ngarde = "fr_FR-mls-medium"\n'
                    '[llm]\nmodel = "keep-this-model"\n', encoding="utf-8")
    save_tts(tmp_path, backend="pocket")
    save_tts(tmp_path, voice="marius", stem="garde")
    config = load_config(tmp_path)
    assert (config.tts.backend, config.tts.voice) == ("pocket", "estelle")
    assert config.tts.voices == {"garde": "marius"}
    assert config.llm.model == "keep-this-model"
    save_tts(tmp_path, backend="piper")
    assert load_config(tmp_path).tts.voices == {"garde": "fr_FR-mls-medium"}
    save_tts(tmp_path, backend="pocket")
    assert load_config(tmp_path).tts.voices == {"garde": "marius"}


def test_pocket_delivers_pcm_before_generation_finishes_and_stops():
    delivered = threading.Event()
    states = {}

    class Chunk:
        def detach(self):
            return self

        def cpu(self):
            return self

        def numpy(self):
            return np.ones(1920, dtype=np.float32) * 0.25

    class Model:
        sample_rate = 24000

        def generate_audio_stream(self, state, text, *, copy_state, stop):
            assert state is states["marius"] and copy_state
            yield Chunk()
            assert delivered.wait(2), "playback waited for complete synthesis"
            assert stop.is_set()

    class Player:
        def play_stream(self, chunks, rate, *, cancel):
            for pcm in chunks:
                assert pcm.dtype == np.int16 and rate == 24000
                speaker.stop()
                delivered.set()

        def stop(self):
            pass

    runtime = SimpleNamespace(model=Model(), lock=threading.RLock(),
                              prepare=lambda voice: states.setdefault(voice, {}))
    speaker = PocketSpeaker(runtime, "marius", Player())
    assert speaker.say_stream(["Bonjour.", "Ne doit pas être prononcé."]) == (["Bonjour."], True)


def test_streaming_audio_drains_only_after_last_chunk(monkeypatch):
    operations = []

    class Stream:
        active = False

        def __init__(self, **kwargs):
            pass

        def start(self):
            self.active = True
            operations.append("start")

        def write(self, pcm):
            operations.append("write")

        def stop(self):
            self.active = False
            operations.append("drain")

    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(OutputStream=Stream))
    AudioPlayer().play_stream([np.ones(1920, dtype=np.int16)] * 3, 24000)
    assert operations == ["start", "write", "write", "write", "drain"]
