"""Streaming, cancellation, and voice caching contracts without models or hardware."""

import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import wave

import numpy as np
import pytest

spec = importlib.util.spec_from_file_location("pocket_demo_engine", Path(__file__).parents[1] / "engine.py")
engine = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = engine
spec.loader.exec_module(engine)


class Chunk:
    def detach(self):
        return self

    def cpu(self):
        return self

    def numpy(self):
        return np.full(240, 0.1, dtype=np.float32)


def setup_runner(monkeypatch, events, stop_after_first=False, fail_second=False):
    class Model:
        sample_rate = 24000

        def generate_audio_stream(self, state, text, *, copy_state, stop):
            assert copy_state is True
            for i in range(3):
                if stop.is_set():
                    break
                if i == 1 and fail_second:
                    raise RuntimeError("stream failed")
                events.append(("yield", i))
                yield Chunk()

    class Sink:
        def __init__(self, origin, stop):
            self.first_submit_s = 0.001
            self.device_name = "test device"
            self.error = None
            self.underflows = 0

            def submit(item):
                events.append(("submit", len(item[0])))
                if stop_after_first:
                    stop.set()

            self.queue = SimpleNamespace(put=submit)

        def finish(self):
            events.append(("finish", None))

    def write(path, audio, rate, subtype):
        with wave.open(str(path), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(rate)
            stream.writeframes((audio * 32767).astype("int16").tobytes())

    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(manual_seed=lambda _: None))
    monkeypatch.setitem(sys.modules, "soundfile", SimpleNamespace(write=write))
    monkeypatch.setattr(engine, "Playback", Sink)
    runner = engine.Engine()
    runner.model = Model()
    monkeypatch.setattr(runner, "prepare", lambda *args: (0.0, 0.0, object()))
    return runner


def test_native_chunks_are_submitted_without_waiting_for_complete_speech(monkeypatch, tmp_path):
    events = []
    runner = setup_runner(monkeypatch, events)
    result = runner.run("Bonjour.", engine.Settings(), output_dir=tmp_path)
    assert events[:4] == [("yield", 0), ("submit", 240), ("yield", 1), ("submit", 240)]
    assert result["audio_s"] == pytest.approx(0.03)
    assert result["first_audio_ready_s"] <= result["generation_s"]
    assert len(result["chunks"]) == 3
    assert Path(result["wav"]).with_suffix(".json").is_file()


def test_cancel_stops_generation_and_saves_partial_audio(monkeypatch, tmp_path):
    events = []
    runner = setup_runner(monkeypatch, events, stop_after_first=True)
    result = runner.run("Bonjour.", engine.Settings(), output_dir=tmp_path)
    assert result["cancelled"] is True
    assert len(result["chunks"]) == 1
    assert events[-1] == ("finish", None)


def test_generation_failure_finishes_playback(monkeypatch, tmp_path):
    events = []
    runner = setup_runner(monkeypatch, events, fail_second=True)
    with pytest.raises(RuntimeError, match="stream failed"):
        runner.run("Bonjour.", engine.Settings(), output_dir=tmp_path)
    assert events[-1] == ("finish", None)


def test_voice_state_is_reused_and_changed_reference_invalidates_cache(monkeypatch, tmp_path):
    runner = engine.Engine()
    calls = []
    runner.model = SimpleNamespace(has_voice_cloning=True,
                                  get_state_for_audio_prompt=lambda source: calls.append(source) or object())
    monkeypatch.setattr(runner, "load", lambda *args: 0.0)
    reference = tmp_path / "reference.wav"
    reference.write_bytes(b"reference")
    settings = engine.Settings(reference=str(reference))
    _, _, first = runner.prepare(settings)
    _, _, second = runner.prepare(settings)
    assert first is second
    reference.write_bytes(b"different reference length")
    _, _, third = runner.prepare(settings)
    assert first is not third
    assert len(calls) == 2


def test_unavailable_clone_has_clear_error_before_reading_reference(monkeypatch, tmp_path):
    runner = engine.Engine()
    runner.model = SimpleNamespace(has_voice_cloning=False)
    monkeypatch.setattr(runner, "load", lambda *args: 0.0)
    path = tmp_path / "reference.wav"
    path.write_bytes(b"reference")
    with pytest.raises(ValueError, match="poids de clonage"):
        runner.prepare(engine.Settings(reference=str(path)))
