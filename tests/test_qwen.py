"""Official VoiceDesign API adapter, optional imports and engine switching."""

import sys
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from npc.config import ConfigError, load_config, save_tts
from npc.engines.qwen import DEFAULT_MODEL, QwenRuntime, QwenSpeaker, passages
from npc.tts import SpeakerFactory
from test_tts import RecordingPlayer


def test_model_loading_and_official_voice_design_arguments(monkeypatch):
    calls = []
    model = SimpleNamespace(generate_voice_design=lambda **kw: (
        calls.append(kw) or [np.array([-2, -.5, 0, .5, 2])], 24000))
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(
        cuda=SimpleNamespace(is_available=lambda: False), float32="fp32", bfloat16="bf16"))
    monkeypatch.setitem(sys.modules, "qwen_tts", SimpleNamespace(Qwen3TTSModel=SimpleNamespace(
        from_pretrained=lambda *a, **kw: calls.append((a, kw)) or model)))
    runtime = QwenRuntime()
    pcm, rate = runtime.synthesize("Bonjour.", "Une voix grave.", "French")
    assert calls == [((DEFAULT_MODEL,), {"device_map": "cpu", "dtype": "fp32",
                                        "attn_implementation": "sdpa"}),
                     {"text": "Bonjour.", "language": "French", "instruct": "Une voix grave."}]
    assert rate == 24000 and pcm.dtype == np.int16
    assert pcm.tolist() == [-32767, -16384, 0, 16384, 32767]
    model.generate_voice_design = lambda **kw: ([np.array([np.nan])], 24000)
    with pytest.raises(RuntimeError, match="invalide"):
        runtime.synthesize("Bonjour.", "voix", "French")


def test_long_passages_preserve_words_and_bound_each_model_request():
    text = ("Une phrase un peu longue, avec des accents.\nUne autre phrase ! " * 30).strip()
    parts = list(passages(text))
    assert len(parts) > 1 and all(0 < len(part) <= 400 for part in parts)
    assert " ".join(" ".join(parts).split()) == " ".join(text.split())
    assert "".join(passages("a" * 901)) == "a" * 901


def test_qwen_remote_only_speaker_publishes_once_and_cancels():
    player, observed, calls = RecordingPlayer(), [], []

    def synthesize(text, description, language, *, cancel):
        calls.append((text, description, language))
        return np.ones(8, dtype=np.int16), 24000

    speaker = QwenSpeaker(SimpleNamespace(synthesize=synthesize), "Narrateur grave", player,
                          language="French", local_playback=False)
    speaker.on_audio = lambda pcm, rate: (observed.append(rate), speaker.stop())
    spoken, cancelled = speaker.say_stream(["Bonjour.", "Au revoir."])
    assert spoken == ["Bonjour."] and cancelled
    assert observed == [24000] and player.played == []
    assert all(call[1:] == ("Narrateur grave", "French") for call in calls)


def test_qwen_factory_shares_runtime_player_and_preserves_all_engine_assignments(tmp_path,
                                                                                monkeypatch):
    save_tts(tmp_path, backend="pocket")
    save_tts(tmp_path, voice="marius", stem="garde")
    save_tts(tmp_path, backend="qwen")
    save_tts(tmp_path, voice="Une voix grave.", stem="garde")
    config = load_config(tmp_path)
    runtime = SimpleNamespace(synthesize=lambda *a, **kw: (np.ones(1, dtype=np.int16), 24000))
    monkeypatch.setattr("npc.engines.qwen.get_runtime", lambda *a: runtime)
    factory = SpeakerFactory(config.tts, local_playback=False)
    first, second = factory.default(), factory(config.tts.voice_resource_for("Une voix grave."))
    assert first.runtime is second.runtime is runtime
    assert first._player is second._player is factory.player
    assert factory.default() is first
    save_tts(tmp_path, backend="pocket")
    assert load_config(tmp_path).tts.voices == {"garde": "marius"}
    save_tts(tmp_path, backend="qwen")
    assert load_config(tmp_path).tts.voices == {"garde": "Une voix grave."}


@pytest.mark.parametrize("setting", ['qwen_language = "Swedish"', 'qwen_device = "remote"',
                                     'qwen_model = ""'])
def test_invalid_qwen_config_fails_before_loading_models(tmp_path, setting):
    (tmp_path / "config.toml").write_text(f"[tts]\n{setting}\n")
    with pytest.raises(ConfigError, match="qwen"):
        load_config(tmp_path)


def test_playback_failure_does_not_deadlock_the_synthesis_queue():
    class BrokenPlayer:
        def play(self, *args):
            raise OSError("audio device lost")

    speaker = QwenSpeaker(SimpleNamespace(synthesize=lambda *a, **kw: (
        np.ones(10, dtype=np.int16), 24000)), "voice", BrokenPlayer())
    errors = []

    def run():
        try:
            speaker.say_stream(["hello"] * 20)
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(2)
    assert not thread.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], OSError)
