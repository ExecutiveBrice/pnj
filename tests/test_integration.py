"""Real-model smoke tests: `uv run pytest -m integration`.

Each test skips itself if its service/model isn't available, so this can run
on any machine without failing spuriously.
"""

import pytest

pytestmark = pytest.mark.integration


def test_qwen_voice_design_with_prepared_local_weights():
    """Opt in with NPC_TEST_QWEN_MODEL pointing to a prepared local model folder."""
    import os
    import threading
    from pathlib import Path

    import numpy as np

    folder = os.environ.get("NPC_TEST_QWEN_MODEL")
    if not folder or not Path(folder).is_dir():
        pytest.skip("set NPC_TEST_QWEN_MODEL to a downloaded VoiceDesign model directory")
    pytest.importorskip("qwen_tts")
    from npc.engines.qwen import QwenRuntime

    runtime = QwenRuntime(folder, os.environ.get("NPC_TEST_QWEN_DEVICE", "cpu"))
    pcm, rate = runtime.synthesize("Bonjour, voyageurs.", "Une voix calme et grave.",
                                   "French", cancel=threading.Event())
    assert pcm.dtype == np.int16 and len(pcm) > rate * .2
    assert np.max(np.abs(pcm.astype(np.int32))) > 100


def test_piper_whisper_roundtrip(config, tmp_path):
    """Synthesize speech with Piper, transcribe it back with whisper."""
    if not config.tts.voice_path.exists():
        pytest.skip(f"piper voice not downloaded: {config.tts.voice_path}")
    from npc.audio.recorder import AudioClip
    from npc.stt import WhisperTranscriber
    from npc.tts import PiperSpeaker

    speaker = PiperSpeaker(config.tts.voice_path)
    samples, rate = speaker.synthesize("Hello traveler, welcome to the elder world.")
    assert len(samples) > rate * 0.5  # at least half a second of audio

    transcriber = WhisperTranscriber(config.stt.model, "auto", config.stt.device)
    text = transcriber.transcribe(AudioClip(samples=samples, sample_rate=rate))
    assert "hello" in text.lower()
    assert "traveler" in text.lower() or "traveller" in text.lower()


def test_ollama_chat_smoke(config):
    from npc.llm import OllamaClient

    client = OllamaClient(config.llm.host, config.llm.model)
    if not client.is_up():
        pytest.skip("ollama not running")
    if not client.has_model():
        pytest.skip(f"model {config.llm.model} not pulled")

    reply = client.chat(
        "You are a terse innkeeper NPC. Answer with one short sentence of dialogue.",
        [{"role": "user", "content": 'PLAYER (spoken): "Do you have rooms free?"'}],
    )
    assert isinstance(reply, str) and len(reply) > 0
