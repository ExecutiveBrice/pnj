"""Voice directions, untrusted settings and optional pitch/tempo processing."""

import sys

import numpy as np
import pytest

from npc.voice_controls import apply_voice_controls, validate_controls, voice_instruction


@pytest.mark.parametrize("settings", [[], {"unknown": 1}, {"pitch": []}, {"pitch": "bass"},
                                     {"gender": "unknown"}, {"gender": []},
                                     {"pitch_shift": True}, {"pitch_shift": -13},
                                     {"speed": "0.8"}, {"speed": 0}, {"speed": float("nan")},
                                     {"speed": float("inf")}])
def test_invalid_voice_controls_are_rejected(settings):
    with pytest.raises(ValueError):
        validate_controls(settings)


def test_neutral_audio_does_not_import_optional_dependencies(monkeypatch):
    monkeypatch.setitem(sys.modules, "librosa", None)
    pcm = np.arange(200, dtype=np.int16)
    assert apply_voice_controls(pcm, 24000, {}) is pcm
    assert apply_voice_controls(pcm, 24000, {"pitch_shift": 0, "speed": 1}) is pcm
    assert voice_instruction("Une voix grave.", {}) == "Une voix grave."


@pytest.mark.parametrize("shift,speed", [(-12, 1), (0, 0.8), (-3, 1.25)])
def test_pitch_and_tempo_are_independent_and_keep_pcm_format(shift, speed):
    pytest.importorskip("librosa")
    rate = 24000
    t = np.arange(rate * 2) / rate
    pcm = np.rint(np.sin(2 * np.pi * 220 * t) * 10000).astype(np.int16)
    result = apply_voice_controls(pcm, rate, {"pitch_shift": shift, "speed": speed})
    assert result.dtype == np.int16
    assert abs(len(result) - len(pcm) / speed) <= 1
    # Ignore phase-vocoder edge transients when measuring the generated tone.
    middle = result[rate // 4:-rate // 4]
    spectrum = np.abs(np.fft.rfft(middle * np.hanning(len(middle))))
    frequency = np.fft.rfftfreq(len(middle), 1 / rate)[spectrum.argmax()]
    assert frequency == pytest.approx(220 * 2 ** (shift / 12), abs=2)
