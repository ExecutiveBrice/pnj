"""French Pocket TTS: native PCM streaming, CPU inference, and local measurements."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import queue
import threading
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parent
os.environ.setdefault("HF_HOME", str(ROOT / ".cache" / "huggingface"))
os.environ.setdefault("TORCH_HOME", str(ROOT / ".cache" / "torch"))
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
EXAMPLE = "Bonjour, voyageurs. Asseyez-vous près du feu, je vais vous raconter une histoire."
VOICES = ("estelle", "marius", "javert", "jean", "alba", "anna")


@dataclass(frozen=True)
class Settings:
    language: str = "french"
    voice: str = "estelle"
    reference: str = ""
    seed: int = 42

    def validate(self, text):
        if not text.strip() or len(text) > 500:
            raise ValueError("Saisissez une réplique de 1 à 500 caractères.")
        if self.language not in ("french", "french_24l"):
            raise ValueError("Profil français inconnu.")
        if not 0 <= self.seed <= 2**32 - 1:
            raise ValueError("La graine doit être comprise entre 0 et 4294967295.")
        if self.reference and not Path(self.reference).is_file():
            raise ValueError("L'extrait de référence est introuvable.")
        if not self.reference and self.voice not in VOICES:
            raise ValueError("Voix inconnue.")


class Playback:
    def __init__(self, origin, stop):
        self.origin, self.stop = origin, stop
        self.first_submit_s = None
        self.device_name = None
        self.error = None
        self.underflows = 0
        self.queue = queue.Queue()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        stream = None
        try:
            import sounddevice as sd
            self.device_name = sd.query_devices(kind="output")["name"]
            while (item := self.queue.get()) is not None:
                samples, rate = item
                if self.stop.is_set():
                    continue
                if stream is None:
                    stream = sd.OutputStream(samplerate=rate, channels=1, dtype="float32")
                    stream.start()
                for index in range(0, len(samples), 1024):
                    if self.stop.is_set():
                        stream.abort()
                        break
                    underflow = stream.write(samples[index:index + 1024])
                    self.underflows += bool(underflow)
                    if self.first_submit_s is None:
                        self.first_submit_s = time.perf_counter() - self.origin
            if stream is not None and not self.stop.is_set():
                stream.stop()
        except Exception as exc:
            self.error = str(exc)
        finally:
            if stream is not None:
                stream.close()

    def finish(self):
        self.queue.put(None)
        self.thread.join()


class Engine:
    def __init__(self):
        self.model = None
        self.language = None
        self.voices = {}

    def load(self, settings, report=print):
        if self.model is not None and settings.language == self.language:
            return 0.0
        from pocket_tts import TTSModel
        report("Chargement du modèle français sur CPU ; téléchargement au premier essai…")
        started = time.perf_counter()
        if self.model is not None:
            self.model = None
            self.voices.clear()
            import gc
            gc.collect()
        self.model = TTSModel.load_model(language=settings.language)
        self.language = settings.language
        if not self.model.has_voice_cloning:
            report("Poids sans clonage chargés : les voix prédéfinies sont disponibles. "
                   "Les extraits personnels nécessitent un accès aux poids de clonage sur Hugging Face.")
        return time.perf_counter() - started

    def prepare(self, settings, report=print):
        load_s = self.load(settings, report)
        started = time.perf_counter()
        if settings.reference:
            if not self.model.has_voice_cloning:
                raise ValueError(
                    "Les voix prédéfinies fonctionnent, mais les poids de clonage ne sont pas "
                    "accessibles dans cette installation. Pour vos extraits personnels, consultez "
                    "https://huggingface.co/kyutai/pocket-tts et connectez votre compte après "
                    "avoir accepté les conditions. Aucun compte n'est nécessaire pour tester Estelle.")
            path = Path(settings.reference).resolve()
            key = (str(path), path.stat().st_mtime_ns, path.stat().st_size)
            source = path
        else:
            key, source = settings.voice, settings.voice
        if key not in self.voices:
            report("Préparation de la voix ; son état sera conservé pour les prochains essais…")
            self.voices[key] = self.model.get_state_for_audio_prompt(source)
        return load_s, time.perf_counter() - started, self.voices[key]

    def run(self, text, settings, *, playback=True, stop=None, output_dir=None, report=print):
        settings.validate(text)
        request_start = time.perf_counter()
        import numpy as np
        import soundfile as sf
        import torch

        stop = stop if stop is not None else threading.Event()
        load_s, prep_s, state = self.prepare(settings, report)
        if stop.is_set():
            raise RuntimeError("Essai annulé après le chargement.")
        torch.manual_seed(settings.seed)
        origin = time.perf_counter()
        sink = Playback(origin, stop) if playback else None
        chunks, pieces, first_ready = [], [], None
        report("Génération et lecture progressive…")
        try:
            for chunk in self.model.generate_audio_stream(state, text, copy_state=True, stop=stop):
                audio = np.asarray(chunk.detach().cpu().numpy(), dtype=np.float32).reshape(-1)
                if not audio.size:
                    continue
                if not np.isfinite(audio).all():
                    raise RuntimeError("Le modèle a produit des valeurs audio invalides.")
                ready = time.perf_counter() - origin
                if first_ready is None:
                    first_ready = ready
                    report(f"Premier bloc audio disponible : {ready:.3f} s.")
                pieces.append(audio)
                chunks.append({"ready_s": ready, "audio_s": len(audio) / self.model.sample_rate})
                if sink:
                    sink.queue.put((audio, self.model.sample_rate))
            generation_s = time.perf_counter() - origin
        finally:
            if sink:
                sink.finish()
        if not pieces:
            raise RuntimeError("Aucun audio généré (annulation ou erreur du modèle).")
        audio = np.concatenate(pieces)
        if np.max(np.abs(audio)) == 0:
            raise RuntimeError("Le signal généré est entièrement silencieux.")
        rate = self.model.sample_rate
        folder = Path(output_dir) if output_dir else ROOT / "outputs"
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"pocket-{time.strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:8]}.wav"
        sf.write(str(target), audio, rate, subtype="PCM_16")
        signal = np.flatnonzero(np.abs(audio) > 0.005)
        result = {
            "settings": asdict(settings), "text": text, "device": "cpu",
            "load_s": load_s, "voice_preparation_s": prep_s,
            "first_audio_ready_s": first_ready,
            "first_audio_submitted_s": sink.first_submit_s if sink else None,
            "generation_s": generation_s, "audio_s": len(audio) / rate,
            "real_time_factor": generation_s / (len(audio) / rate),
            "initial_low_signal_s": int(signal[0]) / rate if signal.size else None,
            "total_request_s": time.perf_counter() - request_start,
            "audio_device": sink.device_name if sink else None,
            "playback_error": sink.error if sink else None,
            "playback_underflows": sink.underflows if sink else None,
            "cancelled": stop.is_set(), "wav": str(target.resolve()), "chunks": chunks,
        }
        target.with_suffix(".json").write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                                encoding="utf-8")
        return result
