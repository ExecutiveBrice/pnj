"""Local VoiceDesign profiles and narrated stories, independent of NPC memory."""

from __future__ import annotations

import json
import os
import re
import threading
import wave
from datetime import datetime, timezone
from importlib.util import find_spec
from uuid import uuid4

from .engines.qwen import QWEN_LANGUAGES, get_runtime, passages

MAX_TEXT = 12000


class StudioCancelled(Exception):
    pass


def required_text(value, name, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{name} : saisissez entre 1 et {limit} caractères.")
    return value.strip()


class VoiceStudio:
    def __init__(self, config, *, runtime_factory=get_runtime, llm_factory=None):
        self.config = config
        self.folder = config.campaign_dir / "voice_studio"
        self.runtime_factory = runtime_factory
        self.llm_factory = llm_factory

    @property
    def available(self):
        return find_spec("qwen_tts") is not None

    def catalog(self):
        result = {"type": "studio_catalog", "available": self.available,
                  "languages": list(QWEN_LANGUAGES), "voices": [], "stories": []}
        for category in ("voices", "stories"):
            for path in sorted((self.folder / category).glob("*.json"), reverse=True):
                try:
                    item = json.loads(path.read_text(encoding="utf-8"))
                    if self.audio_path(item["id"]) is not None:
                        result[category].append(item)
                except (OSError, ValueError, KeyError, TypeError):
                    continue  # an interrupted/external write cannot hide the library
            result[category].sort(key=lambda item: item["created_at"], reverse=True)
        return result

    def audio_path(self, identifier):
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-f0-9]{32}", identifier):
            return None
        path = self.folder / "audio" / f"{identifier}.wav"
        return path if path.is_file() else None

    def validate(self, message):
        """Validate before reserving the only synthesis slot or loading weights."""
        kind = message.get("type")
        if kind not in ("voice_design", "narrate_text", "generate_story"):
            raise ValueError("Commande de studio inconnue.")
        language = message.get("language", "French")
        if not isinstance(language, str) or language not in QWEN_LANGUAGES:
            raise ValueError("Langue Qwen non prise en charge.")
        request = {"type": kind, "language": language,
                   "title": required_text(message.get("title", "Ma voix" if kind == "voice_design"
                                                       else "Mon histoire"), "Titre", 120)}
        voice_id = message.get("voice_id")
        if voice_id is not None and kind != "voice_design":
            voice = next((voice for voice in self.catalog()["voices"]
                          if voice["id"] == voice_id), None)
            if voice is None:
                raise ValueError("Voix enregistrée introuvable.")
            request.update(description=voice["description"], voice_id=voice_id)
        else:
            request["description"] = required_text(message.get("description"),
                                                    "Description de la voix", 1000)
        key = "prompt" if kind == "generate_story" else "text"
        default = "Bonjour, voyageurs. Installez-vous près du feu, voici mon histoire."
        request[key] = required_text(message.get(key, default if kind == "voice_design" else None),
                                     "Idée" if key == "prompt" else "Texte",
                                     2000 if key == "prompt" else MAX_TEXT)
        if kind == "voice_design" and len(request[key]) > 500:
            raise ValueError("L'aperçu vocal est limité à 500 caractères.")
        return request

    def run(self, request, *, cancel=None, progress=lambda message, **fields: None):
        cancel = cancel if cancel is not None else threading.Event()

        def check_cancel():
            if cancel.is_set():
                raise StudioCancelled

        check_cancel()
        text = request.get("text")
        if request["type"] == "generate_story":
            progress("Rédaction de l’histoire…", stage="writing")
            if self.llm_factory is None:
                raise RuntimeError("Le LLM local est indisponible.")
            llm = self.llm_factory()
            # A standalone request: never supply sheets, secrets or NPC history.
            text = llm.chat(
                f"Write a complete short story in {request['language']} from the user's idea. "
                "Return only the story, in plain prose, at most 1500 words. No stage directions.",
                [{"role": "user", "content": request["prompt"]}])
            text = required_text(text, "Histoire générée", MAX_TEXT)
        check_cancel()
        progress("Chargement de Qwen3-TTS VoiceDesign…", stage="loading")
        runtime = self.runtime_factory(self.config.tts.qwen_model, self.config.tts.qwen_device)
        check_cancel()
        parts = list(passages(text))
        identifier = uuid4().hex
        folder = self.folder / "audio"
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{identifier}.wav"
        temporary = target.with_suffix(".tmp")
        rate, frames = None, 0
        try:
            with wave.open(str(temporary), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(24000)  # a cancellation before the first PCM still closes cleanly
                for index, part in enumerate(parts):
                    check_cancel()
                    progress(f"Synthèse du passage {index + 1}/{len(parts)}…",
                             stage="synthesizing", current=index + 1, total=len(parts))
                    pcm, current_rate = runtime.synthesize(
                        part, request["description"], request["language"], cancel=cancel)
                    check_cancel()
                    if rate is None:
                        rate = current_rate
                        output.setframerate(rate)
                    if rate != current_rate or not len(pcm):
                        raise RuntimeError("Le signal audio généré est incomplet.")
                    output.writeframes(pcm.astype("<i2", copy=False).tobytes())
                    frames += len(pcm)
            check_cancel()
            os.replace(temporary, target)
            item = {"id": identifier, "title": request["title"], "text": text,
                    "description": request["description"], "language": request["language"],
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "duration": frames / rate, "audio_url": f"/studio/audio/{identifier}.wav"}
            if "voice_id" in request:
                item["voice_id"] = request["voice_id"]
            category = "voices" if request["type"] == "voice_design" else "stories"
            metadata = self.folder / category / f"{identifier}.json"
            metadata.parent.mkdir(parents=True, exist_ok=True)
            draft = metadata.with_suffix(".tmp")
            try:
                draft.write_text(json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8")
                os.replace(draft, metadata)
            finally:
                draft.unlink(missing_ok=True)
            return {"type": "studio_result", "category": category, "item": item}
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        finally:
            temporary.unlink(missing_ok=True)
