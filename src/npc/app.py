"""NPCApp: state machine, worker thread, and command dispatch.

Voice input (push-to-talk) is ALWAYS in-character player dialogue.
Typed lines are out-of-character GM instructions; /commands control the app.
All output happens as structured events (see events.py).
"""

from __future__ import annotations

import queue
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import date

from .audio.recorder import AudioClip
from .config import Config
from .languages import LANGUAGES
from .events import (
    Busy,
    ConfigReloaded,
    ErrorOccurred,
    Event,
    GmNoteAdded,
    HeardNothing,
    Info,
    LogbookWritten,
    MicrophoneError,
    NpcReplied,
    NpcReplyChunk,
    NpcSwitched,
    PlayerSpoke,
    RecordingDiscarded,
    RecordingStarted,
    SecretList,
    SecretNote,
    SecretPending,
    SecretPondering,
    SecretResolved,
    SecretRevealRequested,
    SessionEnding,
    State,
    StateChanged,
    StatusReport,
    TurnCompleted,
    VoiceUnavailable,
    print_event,
)
from .llm import StreamingNotSupported
from .roster import CharacterSlot, discover_character_files, load_slot, render_turns
from .session.logbook import Transcript
from .session.lore import estimate_tokens, suggest_num_ctx
from .session.prompt import (
    build_system_prompt,
    extract_dialogue,
    looks_foreign,
    strip_decoration,
)
from .session.secrets import (
    FALLBACK_STALL,
    MarkerScrubber,
    Secret,
    delivery_instruction,
    deny_note,
    find_markers,
    strip_markers,
)
from .session.sentences import iter_sentences
from .stt import looks_like_hallucination


class _WrongLanguage(Exception):
    """Streaming control flow: the reply opened in an unexpected language — abandon
    the stream before any audio plays and let the non-streaming path re-ask."""

HELP = """\
Talking
  voice (push-to-talk key)  in-character player dialogue — the NPC answers aloud
  <typed text> + Enter      out-of-character GM instruction ("be more hostile")
  /say <text>               typed in-character player line (no mic needed)
  /npc [name]               list NPCs / switch who you're talking to

Secrets (gated clues from secrets.md — the NPC asks before revealing)
  /yes [note]               approve the pending reveal; the note steers HOW
                            ("/yes but only vaguely")
  /no [note]                refuse — hidden for the rest of this session;
                            the note steers the cover story ("/no she lies")
  /later                    dismiss without deciding; it may come up again
  /secrets                  this NPC's secrets and their status
  /reveal <id>              unlock one yourself; the NPC raises it naturally

Session
  /save                     write the session summary to the logbook now
  /reload                   pick up edits to character/adventure/secrets/config
  /status                   state, model, session number, turn timings
  /end                      summarize into the logbook and exit
  /quit                     exit WITHOUT saving a summary
  /help                     this text"""

COMMANDS = ("/say", "/npc", "/yes", "/no", "/later", "/secrets", "/reveal",
            "/save", "/reload", "/status", "/help", "/end", "/quit")


class NPCApp:
    def __init__(self, config: Config, *, llm, transcriber=None, recorder=None,
                 speaker=None, make_speaker=None,
                 on_event: Callable[[Event], None] | None = None):
        self.config = config
        self.llm = llm
        self.transcriber = transcriber
        self.recorder = recorder
        self.speaker = speaker  # the ACTIVE speaker (hotkey thread reads it for barge-in)
        self._default_speaker = speaker
        self._make_speaker = make_speaker  # builds a Speaker for a voice .onnx path
        self._speakers = {config.tts.voice: speaker} if speaker is not None else {}
        self._on_event = on_event or print_event

        self.transcript = Transcript(config.sessions_dir)

        self._queue: queue.Queue = queue.Queue()
        self._state = State.IDLE
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._accepting = True
        self._player_turns = 0  # campaign-global; drives checkpoint cadence
        self._silence_hinted = False
        self._ctx_warned = False
        self._last_turn: TurnCompleted | None = None
        self._turn_totals: deque[float] = deque(maxlen=20)

        self.adventure = ""
        self.roster: dict[str, CharacterSlot] = {}
        self.active: CharacterSlot
        self._load_roster()
        # the campaign session clock is shared across every NPC's logbook
        self.session_no = max(slot.logbook.next_session_number()
                              for slot in self.roster.values())

        if self.recorder is not None:
            # only a VAD recorder ever fires this; harmless for push-to-talk
            self.recorder.on_auto_stop = self.on_auto_stop

    # ---------- per-NPC state (delegates to the active CharacterSlot) ----------

    @property
    def history(self):
        return self.active.history

    @property
    def ooc_notes(self) -> list[str]:
        return self.active.ooc_notes

    @property
    def character(self) -> str:
        return self.active.character

    @property
    def npc_name(self) -> str:
        return self.active.name

    @property
    def logbook(self):
        return self.active.logbook

    # ---------- lifecycle ----------

    def _load_roster(self) -> None:
        """Discover character files. On /reload, existing slots are refreshed
        IN PLACE (the worker may hold a slot reference mid-turn) and their
        conversation state survives; new files gain slots; slots whose files
        vanished keep their memory until /end."""
        refs = discover_character_files(self.config.campaign_dir)
        if not refs:
            raise FileNotFoundError(
                f"no character.md or characters/*.md in {self.config.campaign_dir}")
        for ref in refs:
            if ref.stem in self.roster:
                self.roster[ref.stem].refresh(self.config)
            else:
                self.roster[ref.stem] = load_slot(ref, self.config)
            slot = self.roster[ref.stem]
            if slot.secrets_error:  # dm_only: parse errors can name secret ids
                self._emit(SecretNote(
                    f"[{slot.name}: secrets file ignored — {slot.secrets_error}]"))
                slot.secrets_error = None
            for lore_error in slot.lore_errors:
                self._emit(ErrorOccurred(
                    f"{slot.name}: lore file skipped — {lore_error}"))
            slot.lore_errors = []
        gone = self.roster.keys() - {ref.stem for ref in refs}
        if gone:
            self._emit(Info(f"[character files removed: {', '.join(sorted(gone))} "
                            "— keeping their memory until /end]"))
        if not hasattr(self, "active"):
            self.active = next(iter(self.roster.values()))
            self.speaker = self._speaker_for(self.active)
        if self.config.adventure_file.exists():
            self.adventure = self.config.adventure_file.read_text(encoding="utf-8")

    def _reload_config(self) -> None:
        """Re-read config.toml mid-session. The LLM model applies immediately
        (it's per-request); STT/TTS/hotkey are loaded at startup and need a
        restart."""
        from .config import load_config

        new = load_config(self.config.campaign_dir)
        applied = [f"character & adventure notes ({len(self.roster)} NPCs, "
                   f"active: {self.npc_name})"]
        if new.llm.model != self.llm.model:
            self.llm.model = new.llm.model
            applied.append(f"LLM model → {new.llm.model}")
        if new.llm.num_ctx != getattr(self.llm, "num_ctx", None):  # per-request → live
            self.llm.num_ctx = new.llm.num_ctx
            applied.append(f"context window → {new.llm.num_ctx or 'server default'}")
        restart_needed = tuple(
            name for name, changed in (
                ("llm.backend", new.llm.backend != self.config.llm.backend),
                ("llm.host", new.llm.host != self.config.llm.host),
                ("llm.timeout/retries",
                 (new.llm.timeout_seconds, new.llm.retries)
                 != (self.config.llm.timeout_seconds, self.config.llm.retries)),
                ("llm.api_key", new.llm.api_key != self.config.llm.api_key),
                ("[stt]", new.stt != self.config.stt),
                ("[tts]", new.tts != self.config.tts),
                ("[hotkey]", new.hotkey != self.config.hotkey),
                ("[overlay]", new.overlay != self.config.overlay),
                ("[mobile]", new.mobile != self.config.mobile),
            ) if changed
        )
        for slot in self.roster.values():
            slot.history.limit = new.history_limit
        self.config = new
        self._emit(ConfigReloaded(applied=tuple(applied), restart_needed=restart_needed))

    def start(self) -> None:
        self._worker = threading.Thread(target=self._worker_loop, daemon=True,
                                        name="npc-worker")
        self._worker.start()

    def shutdown(self, summarize: bool) -> None:
        with self._lock:
            self._accepting = False
            self._queue.put(None)
        if self._worker is not None:
            self._worker.join(timeout=120)
            if self._worker.is_alive():
                # a turn is stuck (hung LLM call?) — don't also write the
                # logbook from this thread while the worker might
                self._emit(ErrorOccurred(
                    "a turn is still running; skipping the final summary — "
                    "run /save next session or edit logbook.md by hand"))
                return
        if summarize:
            if any(slot.player_turns > 0 for slot in self.roster.values()):
                self._emit(SessionEnding())
                self._write_logbook_entries("end")
            else:
                self._emit(Info("[nothing happened this session — logbook unchanged]"))

    # ---------- events & state ----------

    def _emit(self, event: Event) -> None:
        try:
            self._on_event(event)
        except Exception:  # a broken subscriber must not kill the session
            pass

    @property
    def state(self) -> State:
        with self._lock:
            return self._state

    def _set_state_if(self, expected: set[State], new: State) -> bool:
        with self._lock:
            changed = self._state in expected
            if changed:
                self._state = new
        if changed:
            self._emit(StateChanged(new))
        return changed

    # ---------- push-to-talk callbacks (hotkey thread) ----------

    def on_ptt_press(self) -> None:
        if self.recorder is None or self.transcriber is None:
            self._emit(VoiceUnavailable())
            return
        with self._lock:
            prior = self._state
            if prior not in (State.RECORDING, State.PROCESSING):
                self._state = State.RECORDING
        if prior in (State.RECORDING, State.PROCESSING):
            self._emit(Busy())
            return
        if prior is State.SPEAKING and self.speaker is not None:
            self.speaker.stop()  # barge-in
        self._emit(StateChanged(State.RECORDING))
        try:
            self.recorder.start()
        except Exception as e:
            self._set_state_if({State.RECORDING}, State.IDLE)
            self._emit(MicrophoneError(str(e)))
            return
        self._emit(RecordingStarted(
            auto_stop=getattr(self.recorder, "is_auto_stop", False)))

    def on_ptt_release(self) -> None:
        if not self._set_state_if({State.RECORDING}, State.PROCESSING):
            return
        try:
            clip = self.recorder.stop()
        except Exception as e:
            self._set_state_if({State.PROCESSING}, State.IDLE)
            self._emit(MicrophoneError(str(e)))
            return
        self._enqueue_clip(clip)

    def on_auto_stop(self, clip: AudioClip) -> None:
        """Fired by a VAD recorder (from its finalizer thread) when silence
        ends the recording; a manual stop that won the state race makes this
        a no-op — exactly one of the two paths enqueues the clip."""
        if not self._set_state_if({State.RECORDING}, State.PROCESSING):
            return
        self._enqueue_clip(clip)

    def _enqueue_clip(self, clip: AudioClip) -> None:
        if clip.duration < self.config.min_clip_seconds:
            self._set_state_if({State.PROCESSING}, State.IDLE)
            self._emit(RecordingDiscarded("too short"))
            return
        self._queue.put(("utterance", clip))

    # ---------- player interfaces (CLI / mobile / future audio transport) ----------

    def submit_player_text(self, text: str) -> bool:
        """Queue in-character dialogue, never interpret it as a GM command."""
        if not isinstance(text, str) or not text.strip():
            return False
        with self._lock:
            if not self._accepting:
                return False
            self._queue.put(("say", text.strip()))
        return True

    def submit_audio_clip(self, clip: AudioClip) -> bool:
        """Queue an owned mono int16 clip through the existing STT pipeline.

        No microphone is required. Busy/recording engines reject the clip;
        the caller may retry after idle. Whisper handles sample-rate conversion.
        """
        import numpy as np

        if (not isinstance(clip, AudioClip)
                or not isinstance(clip.samples, np.ndarray)
                or clip.samples.ndim != 1 or clip.samples.dtype != np.dtype("int16")
                or isinstance(clip.sample_rate, bool)
                or not isinstance(clip.sample_rate, int) or clip.sample_rate <= 0):
            return False
        if self.transcriber is None:
            self._emit(VoiceUnavailable())
            return False
        owned = AudioClip(clip.samples.copy(), clip.sample_rate)
        if owned.duration < self.config.min_clip_seconds:
            self._emit(RecordingDiscarded("too short"))
            return False
        with self._lock:
            if not self._accepting or self._state is not State.IDLE:
                return False
            self._queue.put(("utterance", owned))
        return True

    def switch_npc(self, name: str) -> bool:
        """Resolve and queue a switch using the same rules as /npc."""
        if not isinstance(name, str) or not name.strip():
            return False
        return self._cmd_npc(name.strip())

    def stop_speaking(self) -> None:
        """Interrupt playback immediately; the worker settles the turn's state."""
        with self._lock:
            speaker = self.speaker if self._state is State.SPEAKING else None
        if speaker is not None:
            speaker.stop()

    def player_status(self) -> dict:
        """Explicit player-safe snapshot: no model config, GM notes or memory."""
        with self._lock:
            return {"npc": self.npc_name, "state": self._state.value}

    # ---------- typed input (main thread) ----------

    def handle_line(self, line: str) -> str:
        """Returns "ok", "end" (quit + summarize) or "quit" (no summary)."""
        line = line.strip()
        if not line:
            return "ok"
        if line.startswith("/"):
            return self._handle_command(line)
        # through the worker queue so a note typed right after /npc lands on
        # the NPC it was meant for, not whoever is active mid-turn
        self._queue.put(("ooc", line))
        return "ok"

    def _handle_command(self, line: str) -> str:
        cmd, _, arg = line.partition(" ")
        arg = arg.strip()
        match cmd:
            case "/say":
                if not arg:
                    self._emit(Info("[usage: /say <what the player says>]"))
                else:
                    self.submit_player_text(arg)
            case "/npc":
                self._cmd_npc(arg)
            case "/yes" | "/no":
                if not self.active.pending_secret:
                    self._emit(SecretNote("[no secret is awaiting a decision]"))
                else:
                    self._queue.put(("secret", (cmd == "/yes", arg)))
            case "/later":
                self._queue.put(("later", None))
            case "/reveal":
                self._cmd_reveal(arg)
            case "/secrets":
                self._cmd_secrets()
            case "/save":
                self._queue.put(("save", None))
            case "/reload":
                self._load_roster()
                try:
                    self._reload_config()
                except Exception as e:
                    self._emit(ErrorOccurred(f"config.toml not reloaded: {e}"))
            case "/status":
                totals = self._turn_totals
                self._emit(StatusReport(
                    state=self.state, npc_name=self.npc_name,
                    model=self.llm.model, session_no=self.session_no,
                    player_turns=self._player_turns, gm_notes=len(self.ooc_notes),
                    last_turn_seconds=(self._last_turn.total_seconds
                                       if self._last_turn else None),
                    avg_turn_seconds=sum(totals) / len(totals) if totals else None,
                    roster_size=len(self.roster),
                ))
            case "/help":
                self._emit(Info(HELP))
            case "/end":
                return "end"
            case "/quit":
                return "quit"
            case _:
                import difflib

                close = difflib.get_close_matches(cmd, COMMANDS, n=1)
                did_you_mean = f" — did you mean {close[0]}?" if close else ""
                self._emit(Info(f"[unknown command {cmd}{did_you_mean} "
                                "(/help lists all; a line without / is a GM "
                                "note to the NPC)]"))
        return "ok"

    # ---------- worker thread ----------

    def _worker_loop(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                kind, payload = item
                if kind == "utterance":
                    self._handle_utterance(payload)
                elif kind == "say":
                    self._emit(PlayerSpoke(payload))
                    self._respond_to_player(payload)
                elif kind == "ooc":
                    self._add_ooc(payload)
                elif kind == "npc":
                    self._switch_npc(payload)
                elif kind == "secret":
                    self._resolve_secret(*payload)
                elif kind == "later":
                    self._dismiss_secret()
                elif kind == "unlock":
                    self._unlock_secret(payload)
                elif kind == "save":
                    if any(s.dirty and s.player_turns > 0
                           for s in self.roster.values()):
                        self._write_logbook_entries("save")
                    else:
                        self._emit(Info("[nothing to save yet]"))
            except Exception as e:
                self._emit(ErrorOccurred(str(e)))
            finally:
                self._set_state_if({State.PROCESSING, State.SPEAKING}, State.IDLE)
                self._queue.task_done()

    def _handle_utterance(self, clip: AudioClip) -> None:
        self._set_state_if({State.IDLE}, State.PROCESSING)
        if clip.dbfs() < self.config.stt.silence_threshold_db:
            self._emit(RecordingDiscarded("silence"))
            if not self._silence_hinted:  # once per session, not every miss
                self._silence_hinted = True
                self._emit(Info(
                    "[nothing rose above the noise floor — speak while the key "
                    "is held; if real speech keeps landing here, lower "
                    "stt.silence_threshold_db (e.g. -55) in config.toml]"))
            return
        t0 = time.perf_counter()
        text = self.transcriber.transcribe(clip)
        stt_seconds = time.perf_counter() - t0
        if not text:
            self._emit(HeardNothing())
            return
        if looks_like_hallucination(text):
            self._emit(RecordingDiscarded(f"whisper hallucination {text!r}"))
            return
        self._emit(PlayerSpoke(text))
        self._respond_to_player(text, stt_seconds=stt_seconds)

    def _system_prompt(self, slot: CharacterSlot) -> str:
        return build_system_prompt(
            slot.character,
            self.adventure,
            slot.logbook.tail(self.config.logbook_sessions_in_prompt),
            slot.ooc_notes,
            secrets=slot.secrets,
            denied=slot.denied_secrets,
            lore=slot.lore,
            language=self.config.language_for(slot.stem),
        )

    def _respond_to_player(self, text: str, stt_seconds: float | None = None) -> None:
        turn_start = time.perf_counter()
        self._set_state_if({State.IDLE}, State.PROCESSING)
        slot = self.active  # switches also run on this thread — stable for the turn
        slot.history.add_player(text)
        slot.turns.append(("PLAYER", text))
        self.transcript.append_turn(self._player_tag(slot), text)
        system = self._system_prompt(slot)
        messages = slot.history.as_messages()
        self._warn_if_over_budget(system, messages)

        timings: dict[str, float] = {}
        requested = self._reply_turn(slot, system, messages, timings, text)
        self._player_turns += 1
        slot.player_turns += 1
        slot.dirty = True
        if slot.pending_secret and not requested:
            self._emit(SecretPending(slot.name, slot.pending_secret))

        turn = TurnCompleted(
            stt_seconds=stt_seconds,
            llm_first_token_seconds=timings.get("first_token"),
            llm_seconds=timings.get("llm", 0.0),
            speak_seconds=timings.get("speak"),
            total_seconds=time.perf_counter() - turn_start + (stt_seconds or 0.0),
        )
        self._last_turn = turn
        self._turn_totals.append(turn.total_seconds)
        self._emit(turn)

        if (self.config.checkpoint_every_turns > 0
                and self._player_turns % self.config.checkpoint_every_turns == 0):
            self._write_logbook_entries("checkpoint")

    def _warn_if_over_budget(self, system: str,
                             messages: list[dict[str, str]]) -> None:
        """One-time hint when the prompt outgrows the context window — Ollama
        silently drops part of an oversized prompt (typically the NPC's
        instructions), which looks like the NPC 'forgetting who it is'."""
        if self._ctx_warned or self.config.llm.backend != "ollama":
            return
        used = (estimate_tokens(system)
                + sum(estimate_tokens(m["content"]) for m in messages)
                + 512)  # reply headroom
        budget = self.config.llm.num_ctx or 4096
        if used <= budget:
            return
        self._ctx_warned = True
        self._emit(Info(
            f"[prompt ≈ {used:,} tokens but the context window is ~{budget:,} "
            "— the server will silently drop part of it (usually the NPC's "
            f"instructions). Set num_ctx = {suggest_num_ctx(used)} under "
            "[llm] in config.toml]"))

    def _reply_turn(self, slot: CharacterSlot, system: str,
                    messages: list[dict[str, str]], timings: dict[str, float],
                    player_line: str) -> bool:
        """LLM → language check → secret markers → history → TTS, shared by
        player turns and secret-delivery turns. Returns True when the reply
        opened a new secret-reveal request."""
        language = self.config.language_for(slot.stem)
        language_name = LANGUAGES[language][1]
        stall = ("Hm. Laissez-moi un instant pour y réfléchir."
                 if language.startswith("fr") else FALLBACK_STALL)
        result = (self._stream_reply(system, messages, timings)
                  if self._can_stream() else None)
        if result is None:  # streaming off/rejected, or the reply used the wrong language
            timings.clear()
            t = time.perf_counter()
            raw = self.llm.chat(system, messages)
            marker_ids = find_markers(raw)
            reply = extract_dialogue(strip_markers(raw), self.npc_name)
            if looks_foreign(reply, language):  # voice must match the reply language
                self._emit(Info(f"[reply was not in {language_name} — asking again]"))
                raw = self.llm.chat(system, messages + [
                    {"role": "assistant", "content": reply},
                    {"role": "user", "content":
                     f"GM NOTE (out-of-character): You broke the {language_name}-only "
                     f"rule. Give that reply again, entirely in {language_name}."},
                ])
                marker_ids += find_markers(raw)
                reply = extract_dialogue(strip_markers(raw), self.npc_name)
            timings["llm"] = time.perf_counter() - t
            requested = self._handle_markers(slot, marker_ids, player_line)
            if not reply and requested:  # marker-only reply: never dead air
                reply = stall
            if reply:
                self._record_npc_reply(slot, reply)
            if reply and self.speaker is not None:
                if looks_foreign(reply, language):  # retry failed too — never voice this
                    self._emit(Info(f"[reply still not in {language_name} — shown, not spoken]"))
                elif self._set_state_if({State.PROCESSING}, State.SPEAKING):
                    t = time.perf_counter()
                    self.speaker.say(reply)
                    timings["speak"] = time.perf_counter() - t
        else:
            reply, cancelled, marker_ids = result
            requested = self._handle_markers(slot, marker_ids, player_line)
            if not reply and requested and not cancelled:
                reply = stall  # marker-only reply: never dead air
                self.speaker.say(reply)
            if reply:  # "" = barged in before any audio
                self._record_npc_reply(slot, reply)
        return requested

    def _can_stream(self) -> bool:
        return (self.config.llm.stream
                and self.speaker is not None
                and hasattr(self.llm, "chat_stream")
                and hasattr(self.speaker, "say_stream"))

    def _stream_reply(self, system: str, messages: list[dict[str, str]],
                      timings: dict[str, float],
                      ) -> tuple[str, bool, list[str]] | None:
        """Speak sentence-by-sentence while the LLM generates. Returns
        (text to record, barged in?, secret marker ids) — the recorded text is
        the whole reply, or only what was heard if barged in — or None when
        the server rejects streaming (caller falls back). Markers are scanned
        on RAW sentences (strip_decoration deletes bracketed spans, so the
        marker itself is never spoken). SPEAKING covers the entire stream, so
        barge-in also cancels a reply whose audio hasn't started yet."""
        raw: list[str] = []
        marker_ids: list[str] = []

        def cleaned_sentences():
            def chunks():
                # the raw feed reaches the table screen via NpcReplyChunk —
                # scrub [CHECK:id] markers or the secret id shows on stream
                scrub = MarkerScrubber()
                t0 = time.perf_counter()
                for chunk in self.llm.chat_stream(system, messages):
                    timings.setdefault("first_token", time.perf_counter() - t0)
                    raw.append(chunk)
                    if shown := scrub.feed(chunk):
                        self._emit(NpcReplyChunk(shown))
                    timings["llm"] = time.perf_counter() - t0
                    yield chunk
                if shown := scrub.flush():
                    self._emit(NpcReplyChunk(shown))

            spoken_any = False
            for sentence in iter_sentences(chunks()):
                marker_ids.extend(find_markers(sentence))
                cleaned = strip_decoration(sentence, self.npc_name)
                if not cleaned:  # a pure stage direction is never spoken
                    continue
                if looks_foreign(cleaned, self.config.language_for(self.active.stem)):
                    if not spoken_any:
                        raise _WrongLanguage  # whole reply likely foreign — bail pre-audio
                    continue  # mid-reply language flip: skip speaking this sentence
                spoken_any = True
                yield cleaned

        self._set_state_if({State.PROCESSING}, State.SPEAKING)
        t_speak = time.perf_counter()
        try:
            spoken, cancelled = self.speaker.say_stream(cleaned_sentences())
        except (StreamingNotSupported, _WrongLanguage):
            self._set_state_if({State.SPEAKING}, State.PROCESSING)
            return None
        timings["speak"] = time.perf_counter() - t_speak
        if cancelled:
            return " ".join(spoken), True, marker_ids
        return (extract_dialogue(strip_markers("".join(raw)), self.npc_name),
                False, marker_ids)

    def _record_npc_reply(self, slot: CharacterSlot, reply: str) -> None:
        slot.history.add_npc(reply)
        slot.turns.append((slot.name, reply))
        self.transcript.append_turn(slot.name, reply)
        self._emit(NpcReplied(slot.name, reply))

    def _add_ooc(self, text: str, emit: bool = True) -> None:
        """Worker handler for bare typed lines: a standing GM note for
        whichever NPC is active when it is processed. Secret-flow notes pass
        emit=False — GmNoteAdded reaches the overlay, and secret ids/hints
        must never ride the table-facing stream."""
        slot = self.active
        slot.ooc_notes.append(text)
        slot.history.add_ooc(text)
        slot.turns.append(("GM", text))
        slot.dirty = True
        self.transcript.append_turn("GM", text)
        if emit:
            self._emit(GmNoteAdded(text))

    # ---------- GM-gated secrets ----------

    def _handle_markers(self, slot: CharacterSlot, marker_ids: list[str],
                        player_line: str) -> bool:
        """Turn the reply's [CHECK:id] markers into at most one reveal request
        for the GM. Returns True when a new request was opened."""
        for secret_id in marker_ids:
            secret = slot.secrets.get(secret_id)
            if secret is None:
                self._emit(SecretNote(
                    f"[{slot.name} emitted unknown marker [CHECK:{secret_id}] "
                    "— ignored]"))
                continue
            if (secret.revealed or secret_id in slot.denied_secrets
                    or slot.pending_secret is not None):
                continue  # duplicate ask, settled secret, or one already pending
            slot.pending_secret = secret_id
            if secret.mode == "hesitate":
                # content-free table flavor; deflect mode stays invisible
                self._emit(SecretPondering(slot.name))
            self._emit(SecretRevealRequested(slot.name, secret_id,
                                             secret.hint, player_line))
            return True
        return False

    def _cmd_reveal(self, arg: str) -> None:
        """Main thread: validate, then serialize the unlock via the worker."""
        slot = self.active
        secret_id = arg.strip().lower()
        if not secret_id:
            self._emit(SecretNote("[usage: /reveal <secret-id> — /secrets lists them]"))
        elif (secret := slot.secrets.get(secret_id)) is None:
            self._emit(SecretNote(
                f"[unknown secret {secret_id!r} for {slot.name} — /secrets lists them]"))
        elif secret.revealed:
            self._emit(SecretNote(f"[({secret_id}) is already revealed]"))
        else:
            self._queue.put(("unlock", secret_id))

    def _cmd_secrets(self) -> None:
        slot = self.active
        if not slot.secrets.entries:
            where = slot.secrets_path or "a secrets file"
            self._emit(SecretNote(f"[no secrets for {slot.name} — add them in {where}]"))
            return
        lines = []
        for s in slot.secrets.entries:
            if s.revealed:
                mark = f"✓ revealed ({s.revealed})"
            elif s.id == slot.pending_secret:
                mark = "⏳ pending your /yes or /no"
            elif s.id in slot.denied_secrets:
                mark = "✗ denied this session"
            else:
                mark = "🔒 locked"
            lines.append(f"{mark} — ({s.id}) {s.hint}")
        self._emit(SecretList(slot.name, tuple(lines)))

    def _resolve_secret(self, approved: bool, note: str) -> None:
        """Worker: settle the pending request (/yes or /no)."""
        slot = self.active
        secret_id = slot.pending_secret
        secret = slot.secrets.get(secret_id) if secret_id else None
        if secret is None:  # raced a /reload or the queue; nothing to settle
            slot.pending_secret = None
            self._emit(SecretNote("[no secret is awaiting a decision]"))
            return
        slot.pending_secret = None
        if secret.mode == "hesitate":  # clear the table's pondering state
            self._emit(SecretPondering(slot.name, active=False))
        if not approved:
            slot.denied_secrets.add(secret.id)
            self._add_ooc(deny_note(secret, note), emit=False)
            self._emit(SecretResolved(slot.name, secret.id, False, note))
            return
        self._mark_revealed(slot, secret)
        self._emit(SecretResolved(slot.name, secret.id, True, note))
        self._deliver_secret(slot, secret, note)

    def _dismiss_secret(self) -> None:
        """Worker: /later — clear the pending request WITHOUT denying, e.g.
        for a spurious marker on an unrelated line. The secret stays locked
        and listed, so the NPC may raise it again."""
        slot = self.active
        secret_id = slot.pending_secret
        if not secret_id:
            self._emit(SecretNote("[no secret is awaiting a decision]"))
            return
        slot.pending_secret = None
        secret = slot.secrets.get(secret_id)
        if secret is not None and secret.mode == "hesitate":
            self._emit(SecretPondering(slot.name, active=False))
        self._emit(SecretNote(
            f"[dismissed — ({secret_id}) stays locked and may come up again]"))

    def _unlock_secret(self, secret_id: str) -> None:
        """Worker: /reveal — unlock proactively, no immediate speech; the NPC
        volunteers it at the next natural moment instead."""
        slot = self.active
        secret = slot.secrets.get(secret_id)
        if secret is None or secret.revealed:
            self._emit(SecretNote(f"[({secret_id}) not unlockable — /secrets lists them]"))
            return
        if slot.pending_secret == secret.id:
            slot.pending_secret = None
            if secret.mode == "hesitate":
                self._emit(SecretPondering(slot.name, active=False))
        slot.denied_secrets.discard(secret.id)
        self._mark_revealed(slot, secret)
        self._add_ooc(
            f"The GM has unlocked the topic ({secret.id}) — your character "
            "now knows it fully (see 'Knowledge you may now share'); bring it "
            "up at the next natural moment.", emit=False)
        self._emit(SecretResolved(slot.name, secret.id, True, ""))

    def _mark_revealed(self, slot: CharacterSlot, secret: Secret) -> None:
        """Persist the reveal (atomic write-back) and put it on the record so
        the session summary knows what the players learned."""
        secret.revealed = f"session {self.session_no}"
        if slot.secrets_path is not None:
            slot.secrets.save(slot.secrets_path)
        slot.turns.append(("GM", f"revealed the secret ({secret.id}): {secret.hint}"))
        slot.dirty = True
        self.transcript.append_turn("GM", f"[revealed secret ({secret.id}): {secret.hint}]")

    def _deliver_secret(self, slot: CharacterSlot, secret: Secret,
                        rider: str) -> None:
        """The /yes follow-up turn: the body is now in the prompt's revealed
        block, and a one-shot GM instruction tells the NPC to share it."""
        self._set_state_if({State.IDLE}, State.PROCESSING)
        system = self._system_prompt(slot)
        messages = slot.history.as_messages() + [
            {"role": "user", "content": delivery_instruction(secret, rider)}]
        timings: dict[str, float] = {}
        self._reply_turn(slot, system, messages, timings, player_line="")

    def _player_tag(self, slot: CharacterSlot) -> str:
        """Transcript attribution: single-NPC campaigns stay clean."""
        return "PLAYER" if len(self.roster) == 1 else f"PLAYER → {slot.name}"

    # ---------- NPC switching ----------

    def _cmd_npc(self, arg: str) -> bool:
        """Main thread: resolve the name and enqueue the switch (it must
        serialize with in-flight turns); bare /npc lists the roster."""
        from .roster import resolve_npc

        if not arg:
            self._emit(Info(self._roster_listing()))
            return False
        found = resolve_npc(self.roster, arg)
        if isinstance(found, list):
            problem = "ambiguous" if found else "unknown"
            self._emit(Info(f"[{problem} NPC {arg!r}]\n" + self._roster_listing()))
            return False
        if self.state is not State.IDLE:
            self._emit(Info(f"[switching to {found.name} after the current line]"))
        with self._lock:
            if not self._accepting:
                return False
            self._queue.put(("npc", found.stem))
        return True

    def _roster_listing(self) -> str:
        lines = ["NPCs in this campaign:"]
        for slot in self.roster.values():
            marker = "  * " if slot is self.active else "    "
            voice = slot.voice or self.config.tts.voice
            lines.append(f"{marker}{slot.name} ({slot.stem}) — voice {voice}"
                         + ("  [active]" if slot is self.active else ""))
        return "\n".join(lines)

    def _switch_npc(self, stem: str) -> None:
        """Worker thread: swap the active slot. Speaker is assigned BEFORE
        the slot so the hotkey thread's barge-in reference never lags."""
        slot = self.roster[stem]
        if slot is self.active:
            self._emit(Info(f"[already speaking with {slot.name}]"))
            return
        speaker = self._speaker_for(slot)
        with self._lock:
            self.speaker = speaker
            self.active = slot
        self._emit(NpcSwitched(slot.name, slot.voice))
        if slot.pending_secret:  # a decision is still owed for this NPC
            self._emit(SecretPending(slot.name, slot.pending_secret))

    def _speaker_for(self, slot: CharacterSlot):
        """The speaker for a slot's voice, created lazily and cached by voice
        name; unavailable voices fall back to the campaign default."""
        voice = slot.voice or self.config.tts.voice
        if voice in self._speakers:
            return self._speakers[voice]
        if self._make_speaker is None:
            return self._default_speaker
        try:
            speaker = self._make_speaker(self.config.tts.voice_path_for(voice))
        except Exception as e:
            self._emit(ErrorOccurred(
                f"voice {voice!r} unavailable ({e}) — using the default voice"))
            return self._default_speaker
        self._speakers[voice] = speaker
        return speaker

    def _write_logbook_entries(self, kind) -> None:
        """Summarize every NPC with unsummarized turns into THEIR OWN logbook
        (strict per-NPC memory) — each from their own turn buffer, so an NPC
        never learns what players told someone else."""
        for slot in self.roster.values():
            if slot.player_turns == 0 or not slot.dirty:
                continue
            body = self.llm.summarize_session(
                render_turns(slot.turns),
                slot.logbook.tail(self.config.logbook_sessions_in_prompt),
            )
            slot.logbook.upsert_entry(self.session_no, date.today().isoformat(), body)
            slot.dirty = False
            self._emit(LogbookWritten(str(slot.logbook.path), kind))
