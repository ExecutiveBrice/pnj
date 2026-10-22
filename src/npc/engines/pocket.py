"""Pocket TTS adapter: one CPU model, cached voices and cancellable PCM streaming."""

from __future__ import annotations

import queue
import tempfile
import threading
import time
from functools import lru_cache

import numpy as np

POCKET_VOICES = ("estelle", "marius", "javert", "jean", "alba", "anna")


class _RemoteAudioClock:
    """Pace PCM against one clock instead of accumulating per-chunk sleeps.

    Keep two native chunks ahead so a client can absorb thread/network jitter.
    This is a bounded lead, not a growing queue of future speech.
    """

    def __init__(self, cancel, sample_rate, *, clock=time.monotonic):
        self.cancel = cancel
        self.sample_rate = sample_rate
        self.clock = clock
        self.deadline = None
        self.lead_seconds = 0.160

    def advance(self, frames):
        now = self.clock()
        self.deadline = max(self.deadline or now, now) + frames / self.sample_rate
        self._wait(self.deadline - self.lead_seconds)

    def finish(self):
        if self.deadline is not None:
            self._wait(self.deadline)

    def _wait(self, deadline):
        remaining = deadline - self.clock()
        if remaining > 0:
            self.cancel.wait(remaining)


class PocketRuntime:
    def __init__(self, profile: str):
        try:
            from pocket_tts import TTSModel
        except ImportError as exc:
            raise RuntimeError("Pocket TTS manque : installez ttrpg-npc[pocket] "
                               "ou lancez uv sync --extra pocket.") from exc

        try:
            self.model = TTSModel.load_model(language=profile)
        except UnicodeDecodeError:
            # Pocket 3.3 reads its YAML using the Windows ANSI encoding. Keep
            # CLI entry points usable even when Python wasn't started in UTF-8.
            from pathlib import Path

            import yaml
            from pocket_tts.utils.config import CONFIGS_DIR

            origin = CONFIGS_DIR / f"{profile}.yaml"
            data = yaml.safe_load(origin.read_text(encoding="utf-8"))
            with tempfile.TemporaryDirectory(prefix="npc-pocket-") as folder:
                safe_config = Path(folder) / f"{profile}.yaml"
                safe_config.write_text(yaml.safe_dump(data, allow_unicode=False), encoding="ascii")
                self.model = TTSModel.load_model(config=safe_config)
            # Predefined embeddings are selected by the official config's name.
            self.model.origin = origin
        self.voices = {}
        self.lock = threading.RLock()
        self.warmed = False

    def prepare(self, voice: str):
        if voice not in POCKET_VOICES:
            raise ValueError(f"Voix Pocket inconnue : {voice}. Choix : {', '.join(POCKET_VOICES)}")
        with self.lock:
            if voice not in self.voices:
                self.voices[voice] = self.model.get_state_for_audio_prompt(voice)
            return self.voices[voice]

    def warm_up(self, voice: str):
        with self.lock:
            if not self.warmed:
                state = self.prepare(voice)
                for _ in self.model.generate_audio_stream(state, "Bonjour.", copy_state=True):
                    pass  # warm the model without playing anything
                self.warmed = True


@lru_cache(maxsize=1)
def get_runtime(profile: str) -> PocketRuntime:
    return PocketRuntime(profile)


class PocketSpeaker:
    def __init__(self, runtime, voice, player, *, on_audio=None, local_playback=True):
        self._runtime = runtime
        self._voice = runtime.prepare(voice)
        self._player = player
        self._cancel = threading.Event()
        self.on_audio = on_audio
        self.local_playback = local_playback

    def say(self, text: str) -> None:
        self.say_stream((text,))

    def say_stream(self, sentences) -> tuple[list[str], bool]:
        self._cancel.clear()
        pending = queue.Queue(maxsize=12)  # about one second of native 80 ms chunks
        spoken, errors = [], []
        rate = self._runtime.model.sample_rate

        def chunks():
            last_sentence = None
            while (item := pending.get()) is not None:
                index, text, audio = item
                if self._cancel.is_set():
                    continue
                if index != last_sentence:
                    spoken.append(text)
                    last_sentence = index
                if self.on_audio is not None:
                    try:
                        self.on_audio(audio, rate)
                    except Exception:
                        pass  # a remote listener cannot break PC playback
                yield audio

        def playback():
            try:
                if self.local_playback:
                    self._player.play_stream(chunks(), rate, cancel=self._cancel)
                else:
                    clock = _RemoteAudioClock(self._cancel, rate)
                    for audio in chunks():
                        clock.advance(len(audio))
                    clock.finish()
            except BaseException as exc:
                errors.append(exc)
                self._cancel.set()  # release a producer waiting on a full queue

        thread = threading.Thread(target=playback, name="pocket-playback", daemon=True)
        thread.start()
        try:
            with self._runtime.lock:
                for index, text in enumerate(sentences):
                    if self._cancel.is_set():
                        break
                    stream = self._runtime.model.generate_audio_stream(
                        self._voice, text, copy_state=True, stop=self._cancel)
                    try:
                        for chunk in stream:
                            if self._cancel.is_set():
                                break
                            audio = np.asarray(chunk.detach().cpu().numpy(), dtype=np.float32).reshape(-1)
                            if not audio.size:
                                continue
                            if not np.isfinite(audio).all():
                                raise RuntimeError("Pocket TTS a produit un signal audio invalide.")
                            pcm = np.rint(np.clip(audio, -1, 1) * 32767).astype(np.int16)
                            while not self._cancel.is_set():
                                try:
                                    pending.put((index, text, pcm), timeout=0.05)
                                    break
                                except queue.Full:
                                    pass
                    finally:
                        stream.close()
        except BaseException:
            self.stop()
            raise
        finally:
            while thread.is_alive():
                try:
                    pending.put(None, timeout=0.05)
                    break
                except queue.Full:
                    pass
            thread.join()
        if errors:
            raise errors[0]
        return spoken, self._cancel.is_set()

    def stop(self) -> None:
        self._cancel.set()
        self._player.stop()
