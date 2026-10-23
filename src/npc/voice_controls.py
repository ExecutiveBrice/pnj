"""Validated voice directions and optional, lazy audio processing for the studio."""

from __future__ import annotations

import math

import numpy as np

# Labels belong to the UI; instructions describe acoustic properties for Qwen.
VOICE_DIRECTIONS = {
    "gender": ("Sexe de la voix", {
        "male": ("Masculine", "A masculine-sounding voice. Preserve the requested pitch, "
                 "texture and fantasy character traits."),
        "female": ("Féminine", "A feminine-sounding voice. Preserve the requested pitch, "
                   "texture and fantasy character traits."),
    }),
    "pitch": ("Hauteur", {
        "very_low": ("Très grave", "A very low bass speaking voice."),
        "low": ("Grave", "A low-pitched speaking voice."),
        "medium": ("Médium", "A mid-range speaking pitch."),
        "high": ("Aiguë", "A high-pitched speaking voice."),
    }),
    "intonation": ("Musicalité / intonation", {
        "flat": ("Presque monotone", "Nearly monotone speech, with a narrow pitch range "
                 "and level phrase endings. Spoken delivery, without singing."),
        "restrained": ("Peu de variations", "Restrained intonation with small pitch movements."),
        "expressive": ("Expressive", "Expressive speech with varied pitch and lively phrasing."),
    }),
    "pace": ("Débit de parole", {
        "slow": ("Lent", "Slow, deliberate speech with measured pauses and clear articulation."),
        "moderate": ("Modéré", "A moderate, steady speaking pace."),
        "fast": ("Rapide", "A brisk speaking pace with clear articulation."),
    }),
    "texture": ("Grain", {
        "clear": ("Clair", "A clear, smooth vocal texture."),
        "raspy": ("Rocailleux", "A rough, gravelly, raspy vocal texture."),
        "guttural": ("Guttural / grondant", "A very rough guttural texture, with audible "
                     "vocal fry and a low growl underneath articulated words."),
        "breathy": ("Soufflé", "A soft, airy, breathy vocal texture."),
    }),
    "resonance": ("Résonance", {
        "chest": ("Poitrine", "A dark, full chest resonance."),
        "cavernous": ("Caverneuse", "A massive, dark, hollow throat resonance and thick "
                      "rounded vowels, suggesting a very large vocal tract."),
        "nasal": ("Nasale", "A bright nasal resonance."),
    }),
}


def controls_catalog():
    return [{"key": key, "label": label,
             "choices": [{"value": value, "label": entry[0]}
                         for value, entry in choices.items()]}
            for key, (label, choices) in VOICE_DIRECTIONS.items()]


def validate_controls(value):
    if value is None:
        return {}
    if not isinstance(value, dict) or value.keys() - (
            VOICE_DIRECTIONS.keys() | {"pitch_shift", "speed"}):
        raise ValueError("Réglages vocaux inconnus.")
    result = {}
    for key, setting in value.items():
        if key in VOICE_DIRECTIONS:
            if not isinstance(setting, str) or setting not in VOICE_DIRECTIONS[key][1]:
                raise ValueError(f"Réglage vocal invalide : {key}.")
        else:
            lower, upper = (-12, 12) if key == "pitch_shift" else (0.5, 1.5)
            if (type(setting) not in (int, float) or not math.isfinite(setting)
                    or not lower <= setting <= upper):
                raise ValueError(f"{key} doit être compris entre {lower} et {upper}.")
        result[key] = setting
    return result


def voice_instruction(description, controls):
    directions = [VOICE_DIRECTIONS[key][1][value][1] for key, value in controls.items()
                  if key in VOICE_DIRECTIONS]
    if not directions:
        return description
    return (description + "\n\nVoice direction (these settings take priority over conflicting "
            "attributes above):\n" + "\n".join(directions))


def apply_voice_controls(pcm, rate, controls):
    """Change pitch without changing duration, and tempo without changing pitch.

    librosa is supplied by the optional qwen-tts dependency. Neutral controls
    never import it. The WAV keeps the model's sample rate.
    """
    shift, speed = controls.get("pitch_shift", 0), controls.get("speed", 1)
    if shift == 0 and speed == 1:
        return pcm
    import librosa

    audio = np.asarray(pcm, dtype=np.float32) / 32768
    if shift:
        audio = librosa.effects.pitch_shift(audio, sr=rate, n_steps=shift)
    if speed != 1:
        audio = librosa.effects.time_stretch(audio, rate=speed)
    if not audio.size or not np.isfinite(audio).all():
        raise RuntimeError("Le traitement de la voix a produit un signal invalide.")
    return np.rint(np.clip(audio, -1, 1) * 32767).astype(np.int16)
