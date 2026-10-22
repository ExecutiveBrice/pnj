"""Generate a standalone WAV beside a user-provided voice specification."""

from __future__ import annotations

import json
import os
import tempfile
import tomllib
import wave
from pathlib import Path

from .engines.qwen import DEFAULT_MODEL, QWEN_LANGUAGES, get_runtime, passages


def read_spec(path: Path) -> dict[str, str]:
    content = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".toml":
        spec = tomllib.loads(content)
    elif path.suffix.lower() == ".json":
        spec = json.loads(content)
    else:
        raise ValueError("Le fichier de spécification doit être au format .toml ou .json.")
    if not isinstance(spec, dict):
        raise ValueError("La spécification doit contenir text, instruct et language.")
    for field in ("text", "instruct", "language"):
        value = spec.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Le champ {field} est obligatoire et doit être un texte non vide.")
    if spec["language"] not in QWEN_LANGUAGES:
        raise ValueError(f"Langue non prise en charge : {spec['language']}. "
                         f"Valeurs possibles : {', '.join(QWEN_LANGUAGES)}.")
    return {field: spec[field] for field in ("text", "instruct", "language")}


def generate_voice_file(spec_path: Path, *, model: str = DEFAULT_MODEL,
                        device: str = "auto", overwrite: bool = False,
                        progress=lambda message: None) -> Path:
    """Validate before loading weights; publish only a complete 16-bit mono WAV."""
    spec_path = spec_path.absolute()
    spec = read_spec(spec_path)
    target = spec_path.with_suffix(".wav")
    if target.exists() and not overwrite:
        raise FileExistsError(f"{target} existe déjà. Utilisez --force pour le remplacer.")

    progress("Chargement de Qwen3-TTS VoiceDesign…")
    runtime = get_runtime(model, device)
    parts = list(passages(spec["text"]))
    with tempfile.NamedTemporaryFile(prefix=f".{target.stem}-", suffix=".tmp",
                                     dir=target.parent, delete=False) as draft:
        temporary = Path(draft.name)
    try:
        rate = None
        with wave.open(str(temporary), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(24000)
            for index, part in enumerate(parts, start=1):
                progress(f"Synthèse du passage {index}/{len(parts)}…")
                pcm, current_rate = runtime.synthesize(part, spec["instruct"], spec["language"])
                if not len(pcm):
                    raise RuntimeError("Le signal audio généré est vide.")
                if rate is None:
                    rate = current_rate
                    output.setframerate(rate)
                if current_rate != rate:
                    raise RuntimeError("La fréquence audio a changé entre deux passages.")
                output.writeframes(pcm.astype("<i2", copy=False).tobytes())
        if overwrite:
            os.replace(temporary, target)
        else:
            # Exclusive publication also protects a file created during synthesis.
            os.link(temporary, target)
        return target
    finally:
        temporary.unlink(missing_ok=True)
