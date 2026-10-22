"""Standalone specification-to-WAV workflow, without models or audio hardware."""

import json
import wave
from types import SimpleNamespace

import numpy as np
import pytest

from npc.cli import main
from npc.engines.qwen import DEFAULT_MODEL
from npc import voice_design


def write_spec(folder, *, name="conteur.json", **changes):
    spec = {"text": "Bonjour, voyageurs.", "instruct": "Une voix grave.", "language": "French"}
    spec.update(changes)
    path = folder / name
    path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.mark.parametrize("extension", ["json", "toml"])
def test_command_writes_playable_wav_beside_spec_from_another_directory(
        tmp_path, monkeypatch, capsys, extension):
    folder = tmp_path / "Mes voix"
    folder.mkdir()
    spec = write_spec(folder)
    if extension == "toml":
        spec.unlink()
        spec = folder / "conteur.toml"
        spec.write_text('text = "Bonjour, voyageurs."\n'
                        'instruct = "Une voix grave."\nlanguage = "French"\n',
                        encoding="utf-8-sig")
    monkeypatch.chdir(tmp_path)
    calls = []
    samples = np.array([-1234, 0, 4321], dtype=np.int16)

    def runtime_factory(model, device):
        assert (model, device) == (DEFAULT_MODEL, "auto")
        return SimpleNamespace(synthesize=lambda *args: (
            calls.append(args) or samples, 22050))

    monkeypatch.setattr(voice_design, "get_runtime", runtime_factory)
    assert main(["voice-design", str(spec.relative_to(tmp_path))]) == 0
    target = folder / "conteur.wav"
    with wave.open(str(target), "rb") as audio:
        assert (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) == (1, 2, 22050)
        assert audio.readframes(audio.getnframes()) == samples.astype("<i2").tobytes()
    assert calls == [("Bonjour, voyageurs.", "Une voix grave.", "French")]
    assert str(target) in capsys.readouterr().out
    assert set(folder.iterdir()) == {spec, target}


@pytest.mark.parametrize("content,extension", [
    ('{"text": "bonjour", "language": "French"}', "json"),
    ('{"text": 42, "instruct": "voix", "language": "French"}', "json"),
    ('{"text": " ", "instruct": "voix", "language": "French"}', "json"),
    ('{"text": "bonjour", "instruct": "voix", "language": "french"}', "json"),
    ('[]', "json"),
    ('{', "json"),
    ('text = ', "toml"),
    ('bonjour', "txt"),
])
def test_bad_spec_fails_before_loading_model(tmp_path, monkeypatch, capsys, content, extension):
    spec = tmp_path / f"bad.{extension}"
    spec.write_text(content, encoding="utf-8")
    monkeypatch.setattr(voice_design, "get_runtime", lambda *a: pytest.fail("model loaded"))
    assert main(["voice-design", str(spec)]) == 1
    assert "Génération vocale impossible" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == [spec]


def test_missing_spec_is_reported_without_traceback(tmp_path, capsys):
    assert main(["voice-design", str(tmp_path / "missing.json")]) == 1
    assert "Génération vocale impossible" in capsys.readouterr().err


def test_existing_wav_requires_force_before_loading_model(tmp_path, monkeypatch, capsys):
    spec = write_spec(tmp_path)
    target = spec.with_suffix(".wav")
    target.write_bytes(b"existing audio")
    monkeypatch.setattr(voice_design, "get_runtime", lambda *a: pytest.fail("model loaded"))
    assert main(["voice-design", str(spec)]) == 1
    assert "--force" in capsys.readouterr().err
    assert target.read_bytes() == b"existing audio"


def test_long_text_and_model_options(tmp_path, monkeypatch):
    text = "Une phrase assez longue, avec des accents. " * 30
    spec = write_spec(tmp_path, text=text)
    calls = []

    def runtime_factory(model, device):
        assert (model, device) == ("local-model", "cpu")
        return SimpleNamespace(synthesize=lambda *args: (
            calls.append(args) or np.ones(10, dtype=np.int16), 24000))

    monkeypatch.setattr(voice_design, "get_runtime", runtime_factory)
    assert main(["voice-design", str(spec), "--model", "local-model", "--device", "cpu"]) == 0
    assert len(calls) > 1
    assert " ".join(" ".join(c[0] for c in calls).split()) == " ".join(text.split())
    assert all(len(c[0]) <= 400 and c[1:] == ("Une voix grave.", "French") for c in calls)
    with wave.open(str(spec.with_suffix(".wav")), "rb") as audio:
        assert audio.getnframes() == 10 * len(calls)


@pytest.mark.parametrize("failure", ["error", "interrupt", "empty", "rate_change", "none"])
def test_force_replaces_only_complete_audio_and_cleans_temporary_files(
        tmp_path, monkeypatch, failure):
    spec = write_spec(tmp_path, text="Une phrase complète. " * 30)
    target = spec.with_suffix(".wav")
    original = b"existing audio"
    target.write_bytes(original)
    calls = []

    def synthesize(*args):
        calls.append(args)
        if len(calls) == 2:
            if failure == "error":
                raise RuntimeError("synthesis failed")
            if failure == "interrupt":
                raise KeyboardInterrupt
            if failure == "empty":
                return np.zeros(0, dtype=np.int16), 24000
            if failure == "rate_change":
                return np.ones(10, dtype=np.int16), 22050
        return np.ones(10, dtype=np.int16), 24000

    monkeypatch.setattr(voice_design, "get_runtime", lambda *a: SimpleNamespace(synthesize=synthesize))
    result = main(["voice-design", str(spec), "--force"])
    assert result == (0 if failure == "none" else 130 if failure == "interrupt" else 1)
    if failure == "none":
        with wave.open(str(target), "rb") as audio:
            assert audio.getnframes() == 10 * len(calls)
    else:
        assert target.read_bytes() == original
    assert set(tmp_path.iterdir()) == {spec, target}
