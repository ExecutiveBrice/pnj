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
from concurrent.futures import Future
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
    SecretDisclosureRequested,
    SecretDisclosureResolved,
    SecretDisclosuresChanged,
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
from .llm import LlmError, StreamingNotSupported
from .roster import CharacterSlot, discover_character_files, load_slot, render_turns
from .attitudes import SPEECH_ACTIONS
from .session.logbook import Transcript
from .session.lore import estimate_tokens, suggest_num_ctx
from .session.prompt import (
    asks_to_repeat,
    build_system_prompt,
    extract_dialogue,
    looks_foreign,
    opening_instruction,
    reply_may_repeat,
    reply_repeats,
    split_character_speech,
    strip_decoration,
)
from .session.reset import clear_session_directories
from .session.secrets import (
    FALLBACK_STALL,
    MarkerScrubber,
    PendingDisclosure,
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


class _RepeatedReply(Exception):
    """A copied reply was buffered before audio; regenerate it once."""


class _SessionReset(Exception):
    """Abandon an old turn before it can publish or persist more output."""

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
        self._make_speaker = make_speaker  # Piper path or Pocket voice identifier
        self._speakers = {config.tts.voice: speaker} if speaker is not None else {}
        self._on_event = on_event or print_event

        self.transcript = Transcript(config.sessions_dir)

        self._queue: queue.Queue = queue.Queue()
        self._state = State.IDLE
        self._lock = threading.Lock()
        self._disclosure_lock = threading.Lock()
        self._disclosure_decisions: dict[str, str] = {}
        self._worker: threading.Thread | None = None
        self._accepting = True
        self._resetting = threading.Event()
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
                f"no character.md, characters/*.md or zones/*/characters/*.md "
                f"in {self.config.campaign_dir}")
        lore_cache = {}
        for ref in refs:
            new_slot = ref.stem not in self.roster
            with self._disclosure_lock:
                if ref.stem in self.roster:
                    self.roster[ref.stem].refresh(self.config, lore_cache=lore_cache)
                else:
                    self.roster[ref.stem] = load_slot(ref, self.config, lore_cache=lore_cache)
            slot = self.roster[ref.stem]
            if new_slot and (revealed := slot.secrets.revealed()):
                names = ", ".join(f"{s.id} ({s.revealed})" for s in revealed)
                if self.config.language_for(slot.stem).startswith("fr"):
                    notice = (f"{slot.name} — secrets déjà révélés : {names}. "
                              "Les autorisations du MJ sont conservées entre les sessions.")
                else:
                    notice = (f"{slot.name} — previously revealed secrets: {names}. "
                              "GM approvals persist between sessions.")
                self._emit(SecretNote(notice))
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
        """Start the worker and wait for an explicit dialogue action."""
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
            if not self._accepting or self._resetting.is_set():
                return
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
        with self._lock:
            if self._accepting and not self._resetting.is_set():
                self._queue.put(("utterance", clip))

    # ---------- player interfaces (CLI / mobile / future audio transport) ----------

    def submit_player_text(self, text: str) -> bool:
        """Queue in-character dialogue, never interpret it as a GM command."""
        if not isinstance(text, str) or not text.strip():
            return False
        with self._lock:
            if not self._accepting or self._resetting.is_set():
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
            if (not self._accepting or self._resetting.is_set()
                    or self._state is not State.IDLE):
                return False
            self._queue.put(("utterance", owned))
        return True

    def switch_npc(self, name: str) -> bool:
        """Resolve and queue a switch using the same rules as /npc."""
        if not isinstance(name, str) or not name.strip():
            return False
        return self._cmd_npc(name.strip())

    def speak_npc(self, npc_id: str, attitude: str | None = None, *,
                  action: str | None = None) -> bool:
        """Queue sheet-based speech; the legacy attitude argument is ignored."""
        if (not isinstance(npc_id, str)
                or (action is not None
                    and (not isinstance(action, str) or action not in SPEECH_ACTIONS))):
            return False
        with self._lock:
            if (not self._accepting or self._resetting.is_set() or npc_id not in self.roster
                    or self._state is State.RECORDING):
                return False
            self._queue.put(("speak", (npc_id, action)))
        return True

    def stop_speaking(self) -> None:
        """Interrupt playback immediately; the worker settles the turn's state."""
        with self._lock:
            speaker = self.speaker if self._state is State.SPEAKING else None
        if speaker is not None:
            speaker.stop()

    def reset_session(self) -> bool:
        """Interrupt old work and wait for the worker's complete campaign reset.

        Network callbacks call this public method off their event loop. A
        blocking model call must return before cleanup can be acknowledged.
        """
        with self._lock:
            if (not self._accepting or self._resetting.is_set()
                    or self._worker is None or not self._worker.is_alive()):
                return False
            self._resetting.set()
            recording = self._state is State.RECORDING
            if recording:
                self._state = State.PROCESSING
            speaker = self.speaker
        done: Future = Future()
        try:
            if speaker is not None:
                speaker.stop()
            if recording and self.recorder is not None:
                self.recorder.stop()  # discard the clip, including any VAD callback
        except Exception:
            self._resetting.clear()
            self._set_state_if({State.PROCESSING}, State.IDLE)
            raise
        with self._lock:
            if not self._accepting:
                self._resetting.clear()
                return False
            self._queue.put(("reset", done))
        return done.result()

    def _check_session_reset(self) -> None:
        if self._resetting.is_set():
            raise _SessionReset

    def _reset_session(self) -> None:
        """Worker-only: prepare fresh slots, clean disk, then replace memory."""
        lore_cache = {}
        roster = {ref.stem: load_slot(ref, self.config, lore_cache=lore_cache)
                  for ref in discover_character_files(self.config.campaign_dir)}
        if not roster:
            raise ValueError("No characters found for session reset")
        adventure = (self.config.adventure_file.read_text(encoding="utf-8")
                     if self.config.adventure_file.exists() else "")
        session_no = max(slot.logbook.next_session_number() for slot in roster.values())
        active = roster.get(self.active.stem, next(iter(roster.values())))
        speaker = self._speaker_for(active)
        clear_session_directories(self.config.campaign_dir)
        with self._disclosure_lock:
            with self._lock:
                self.active = active
                self.speaker = speaker
                self.roster = roster
            self._disclosure_decisions.clear()
        self.adventure = adventure
        self.transcript = Transcript(self.config.sessions_dir)
        self.session_no = session_no
        self._player_turns = 0
        self._silence_hinted = False
        self._ctx_warned = False
        self._last_turn = None
        self._turn_totals.clear()
        self._emit(SecretDisclosuresChanged())

    def player_status(self) -> dict:
        """Explicit player-safe snapshot: no model config, GM notes or memory."""
        with self._lock:
            return {"npc": self.npc_name, "state": self._state.value,
                    "npcs": [{"id": slot.stem, "name": slot.name}
                             for slot in tuple(self.roster.values())]}

    def secret_disclosures(self) -> list[dict]:
        """GM-only snapshot across all NPCs, including inactive characters."""
        with self._disclosure_lock:
            return [{"request_id": pending.request_id, "npc": slot.name,
                     "secret": pending.body, "context": pending.context}
                    for slot in tuple(self.roster.values())
                    if (pending := slot.pending_disclosure) is not None
                    and slot.pending_secret == pending.secret_id
                    and pending.matches(slot.secrets.get(pending.secret_id))]

    def gm_npc_details(self) -> list[dict]:
        """GM-only cards for every NPC, without fetching secret bodies."""
        with self._disclosure_lock:
            return [{"id": slot.stem, "name": slot.name,
                     "active": slot is self.active,
                     "scenario": slot.details.scenario, "attitude": slot.details.attitude,
                     "motivation": slot.details.motivation,
                     "negotiation": slot.details.negotiation,
                     "secret_count": len(slot.details.local_secrets) + len(slot.secrets.entries)}
                    for slot in tuple(self.roster.values())]

    def gm_npc_secrets(self, npc_id: str) -> dict | None:
        """Read a specific NPC's secrets without selecting it or approving a reveal."""
        if not isinstance(npc_id, str):
            return None
        with self._disclosure_lock:
            slot = self.roster.get(npc_id)
            if slot is None:
                return None
            entries = [{"body": body, "status": "local", "hint": ""}
                       for body in slot.details.local_secrets]
            for secret in slot.secrets.entries:
                status = ("revealed" if secret.revealed else
                          "pending" if secret.id == slot.pending_secret else
                          "denied" if secret.id in slot.denied_secrets else "locked")
                entries.append({"body": secret.body, "hint": secret.hint, "status": status})
            return {"id": slot.stem, "name": slot.name, "secrets": entries}

    def resolve_secret_disclosure(self, request_id: str, decision: str) -> bool:
        """Queue a targeted GM decision; the worker arbitrates all retries."""
        if (not isinstance(request_id, str) or not request_id.strip()
                or not isinstance(decision, str) or decision not in ("approved", "rejected")):
            return False
        with self._lock:
            if not self._accepting or self._resetting.is_set():
                return False
            self._queue.put(("disclosure", (request_id, decision)))
        return True

    # ---------- typed input (main thread) ----------

    def handle_line(self, line: str) -> str:
        """Returns "ok", "end" (quit + summarize) or "quit" (no summary)."""
        if self._resetting.is_set():
            return "ok"
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
                self._invalidate_disclosures()
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
            reset_done = None
            reset_error = None
            try:
                if item is None:
                    return
                kind, payload = item
                if kind == "reset":
                    reset_done = payload
                    self._reset_session()
                elif self._resetting.is_set():
                    continue  # discard pre-reset actions, including saves and approvals
                elif kind == "utterance":
                    self._handle_utterance(payload)
                elif kind == "say":
                    self._emit(PlayerSpoke(payload))
                    self._respond_to_player(payload)
                elif kind == "ooc":
                    self._add_ooc(payload)
                elif kind == "npc":
                    self._switch_npc(payload)
                elif kind == "speak":
                    stem, action = payload
                    self._switch_npc(stem)
                    self._speak_npc(action=action)
                elif kind == "secret":
                    self._resolve_secret(*payload)
                elif kind == "disclosure":
                    self._resolve_disclosure(*payload)
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
            except _SessionReset:
                pass
            except Exception as e:
                reset_error = e
                self._emit(ErrorOccurred(str(e)))
            finally:
                self._set_state_if({State.PROCESSING, State.SPEAKING}, State.IDLE)
                if reset_done is not None:
                    self._resetting.clear()
                    if reset_error is not None:
                        reset_done.set_exception(reset_error)
                    else:
                        reset_done.set_result(True)
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
        self._check_session_reset()
        stt_seconds = time.perf_counter() - t0
        if not text:
            self._emit(HeardNothing())
            return
        if looks_like_hallucination(text):
            self._emit(RecordingDiscarded(f"whisper hallucination {text!r}"))
            return
        self._emit(PlayerSpoke(text))
        self._respond_to_player(text, stt_seconds=stt_seconds)

    def _system_prompt(self, slot: CharacterSlot,
                       secret_topics: set[str] | None = None) -> str:
        return build_system_prompt(
            slot.character,
            self.adventure,
            slot.logbook.tail(self.config.logbook_sessions_in_prompt),
            slot.ooc_notes,
            secrets=slot.secrets,
            denied=slot.denied_secrets,
            lore=slot.lore,
            language=self.config.language_for(slot.stem),
            secret_topics=secret_topics,
        )

    def _respond_to_player(self, text: str, stt_seconds: float | None = None) -> None:
        turn_start = time.perf_counter()
        self._set_state_if({State.IDLE}, State.PROCESSING)
        slot = self.active  # switches also run on this thread — stable for the turn
        slot.history.add_player(text)
        slot.turns.append(("PLAYER", text))
        self.transcript.append_turn(self._player_tag(slot), text)
        messages = slot.history.as_messages()
        secret_topics = None
        select_topic = getattr(self.llm, "select_secret_topic", None)
        if callable(select_topic):
            # Decide intent separately so the acting model does not associate
            # every mention of friendship/the monument with a locked clue.
            topics = {s.id: s.hint for s in slot.secrets.locked()
                      if s.id not in slot.denied_secrets}
            selected = select_topic(text, topics, messages) if topics else None
            self._check_session_reset()
            secret_topics = {selected} if selected in topics else set()
        system = self._system_prompt(slot, secret_topics=secret_topics)
        self._warn_if_over_budget(system, messages)

        timings: dict[str, float] = {}
        requested = self._reply_turn(slot, system, messages, timings, text,
                                     secret_topics=secret_topics)
        self._check_session_reset()
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
                    player_line: str, secret_topics: set[str] | None = None,
                    *, opening: bool = False) -> bool:
        """LLM → language check → secret markers → history → TTS, shared by
        player turns and secret-delivery turns. Returns True when the reply
        opened a new secret-reveal request."""
        self._check_session_reset()
        language = self.config.language_for(slot.stem)
        language_name = LANGUAGES[language][1]
        stall = ("Hm. Laissez-moi un instant pour y réfléchir."
                 if language.startswith("fr") else FALLBACK_STALL)
        # Explicit requests to repeat and GM-directed delivery turns may
        # legitimately reuse spoken information. Compare only this NPC's history.
        previous = messages if opening or (player_line and not asks_to_repeat(player_line)) else []
        if opening and slot.last_opening:
            previous = [*previous, {"role": "assistant", "content": slot.last_opening}]
        earlier_players = [m for m in messages[:-1] if m["role"] == "user"
                           and m["content"].startswith("PLAYER (spoken):")]
        if earlier_players and earlier_players[-1] == messages[-1]:
            previous = []  # asking the exact same question can have the same factual answer
        repeated_retry = False
        try:
            result = (self._stream_reply(system, messages, timings, previous, opening=opening)
                      if self._can_stream() else None)
        except _RepeatedReply:
            repeated_retry = True
            self._emit(Info("[repeated reply — asking again before speaking]"))
            messages = self._repetition_retry(messages, language, player_line, opening=opening)
            result = None
        if result is None:  # streaming off/rejected, or the reply used the wrong language
            timings.clear()
            t = time.perf_counter()
            raw = self.llm.chat(system, messages)
            self._check_session_reset()
            reply = extract_dialogue(strip_markers(raw), self.npc_name)
            if looks_foreign(reply, language):  # voice must match the reply language
                self._emit(Info(f"[reply was not in {language_name} — asking again]"))
                raw = self.llm.chat(system, messages + [
                    {"role": "assistant", "content": reply},
                    {"role": "user", "content":
                     f"GM NOTE (out-of-character): You broke the {language_name}-only "
                     f"rule. Give that reply again, entirely in {language_name}."},
                ])
                self._check_session_reset()
                reply = extract_dialogue(strip_markers(raw), self.npc_name)
            if reply_repeats(reply, previous, include_short=opening) and not repeated_retry:
                self._emit(Info("[repeated reply — asking again before speaking]"))
                raw = self.llm.chat(system, self._repetition_retry(
                    messages, language, player_line, opening=opening))
                self._check_session_reset()
                reply = extract_dialogue(strip_markers(raw), self.npc_name)
            if reply_repeats(reply, previous, include_short=opening):
                raise LlmError(
                    "the model repeated an earlier reply even after correction — "
                    "try again or choose another LLM model in config.toml")
            # A discarded language/repetition attempt must not request a secret.
            marker_ids = find_markers(raw)
            timings["llm"] = time.perf_counter() - t
            requested = self._handle_markers(
                slot, sorted(secret_topics) if secret_topics is not None else marker_ids, player_line)
            if not reply and requested:  # marker-only reply: never dead air
                reply = stall
            if reply:
                self._record_npc_reply(slot, reply)
            if reply and self.speaker is not None:
                if looks_foreign(reply, language):  # retry failed too — never voice this
                    self._emit(Info(f"[reply still not in {language_name} — shown, not spoken]"))
                elif self._set_state_if({State.PROCESSING}, State.SPEAKING):
                    self._check_session_reset()
                    t = time.perf_counter()
                    self.speaker.say(reply)
                    timings["speak"] = time.perf_counter() - t
        else:
            self._check_session_reset()
            reply, cancelled, marker_ids = result
            requested = self._handle_markers(
                slot, sorted(secret_topics) if secret_topics is not None else marker_ids, player_line)
            if not reply and requested and not cancelled:
                self._check_session_reset()
                reply = stall  # marker-only reply: never dead air
                self.speaker.say(reply)
            if reply:  # "" = barged in before any audio
                self._record_npc_reply(slot, reply)
        if reply and opening:
            slot.last_opening = reply
        return requested

    @staticmethod
    def _repetition_retry(messages: list[dict[str, str]], language: str,
                          player_line: str, *, opening: bool = False) -> list[dict[str, str]]:
        if opening:
            note = (messages[-1]["content"] + "\n" +
                    ("Ta tentative recopie une réplique précédente. Trouve un autre accueil."
                     if language.startswith("fr") else
                     "Your attempt copied an earlier reply. Find a different greeting."))
        elif language.startswith("fr"):
            note = (
                "GM NOTE (out-of-character): Ta tentative recopie une réponse précédente. "
                "Réponds à la demande actuelle ci-dessous, en français et dans ton personnage. "
                "Identifie de qui ou de quoi le joueur parle maintenant. Formule une réponse "
                "adaptée ; ne paraphrase pas simplement le refus précédent et n'invente pas "
                "de faits. Ne demande aucune révélation sans demande d'information protégée. "
                f"Demande actuelle du joueur : {player_line}")
        else:
            note = (
                "GM NOTE (out-of-character): Your attempt copied an earlier reply. "
                "Answer the current request below, in English and in character. Identify "
                "who or what the player means now. Respond to that request, rather than "
                "merely paraphrasing an earlier refusal. Do not invent facts or flag a "
                "secret unless protected information is requested. "
                f"Current player request: {player_line}")
        return messages + [{"role": "user", "content": note}]

    def _can_stream(self) -> bool:
        return (self.config.llm.stream
                and self.speaker is not None
                and hasattr(self.llm, "chat_stream")
                and hasattr(self.speaker, "say_stream"))

    def _stream_reply(self, system: str, messages: list[dict[str, str]],
                      timings: dict[str, float],
                      previous: list[dict[str, str]],
                      *, opening: bool = False,
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
        pending_events: list[str] = []

        def cleaned_sentences():
            spoken_any = False
            pending_sentences: list[str] = []

            def flush_events():
                for text in pending_events:
                    self._emit(NpcReplyChunk(text))
                pending_events.clear()

            def chunks():
                # the raw feed reaches the table screen via NpcReplyChunk —
                # scrub [CHECK:id] markers or the secret id shows on stream
                scrub = MarkerScrubber()
                t0 = time.perf_counter()
                for chunk in self.llm.chat_stream(system, messages):
                    self._check_session_reset()
                    timings.setdefault("first_token", time.perf_counter() - t0)
                    raw.append(chunk)
                    if shown := scrub.feed(chunk):
                        if spoken_any:
                            self._emit(NpcReplyChunk(shown))
                        else:
                            pending_events.append(shown)
                    timings["llm"] = time.perf_counter() - t0
                    yield chunk
                if shown := scrub.flush():
                    if spoken_any:
                        self._emit(NpcReplyChunk(shown))
                    else:
                        pending_events.append(shown)

            for sentence in iter_sentences(chunks()):
                marker_ids.extend(find_markers(sentence))
                cleaned = strip_decoration(sentence, self.npc_name)
                if not cleaned:  # a pure stage direction is never spoken
                    continue
                if looks_foreign(cleaned, self.config.language_for(self.active.stem)):
                    if not spoken_any:
                        raise _WrongLanguage  # whole reply likely foreign — bail pre-audio
                    continue  # mid-reply language flip: skip speaking this sentence
                if not spoken_any:
                    pending_sentences.append(cleaned)
                    if reply_may_repeat(" ".join(pending_sentences), previous,
                                        include_short=opening):
                        continue  # retain a copied opening until it diverges or ends
                    flush_events()
                    spoken_any = True
                    yield from pending_sentences
                    pending_sentences.clear()
                    continue
                spoken_any = True
                yield cleaned
            if pending_sentences:
                if reply_repeats(" ".join(pending_sentences), previous, include_short=opening):
                    raise _RepeatedReply
                flush_events()
                yield from pending_sentences
            flush_events()

        self._set_state_if({State.PROCESSING}, State.SPEAKING)
        t_speak = time.perf_counter()
        try:
            spoken, cancelled = self.speaker.say_stream(cleaned_sentences())
        except (StreamingNotSupported, _WrongLanguage):
            self._set_state_if({State.SPEAKING}, State.PROCESSING)
            return None
        except _RepeatedReply:
            self._set_state_if({State.SPEAKING}, State.PROCESSING)
            raise
        timings["speak"] = time.perf_counter() - t_speak
        if cancelled:
            return " ".join(spoken), True, marker_ids
        return (extract_dialogue(strip_markers("".join(raw)), self.npc_name),
                False, marker_ids)

    def _record_npc_reply(self, slot: CharacterSlot, reply: str) -> None:
        self._check_session_reset()
        slot.history.add_npc(reply)
        slot.turns.append((slot.name, reply))
        self.transcript.append_turn(slot.name, reply)
        self._emit(NpcReplied(slot.name, reply))

    def _add_ooc(self, text: str, emit: bool = True,
                 slot: CharacterSlot | None = None) -> None:
        """Worker handler for bare typed lines: a standing GM note for
        whichever NPC is active when it is processed. Secret-flow notes pass
        emit=False — GmNoteAdded reaches the overlay, and secret ids/hints
        must never ride the table-facing stream."""
        slot = slot or self.active
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
            with self._disclosure_lock:
                slot.pending_secret = secret_id
                pending = PendingDisclosure(secret_id, secret.hint, secret.body, player_line)
                slot.pending_disclosure = pending
            if secret.mode == "hesitate":
                # content-free table flavor; deflect mode stays invisible
                self._emit(SecretPondering(slot.name))
            self._emit(SecretRevealRequested(slot.name, secret_id,
                                             secret.hint, player_line))
            self._emit(SecretDisclosureRequested(pending.request_id, slot.name,
                                                 pending.body, pending.context))
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

    def _resolve_secret(self, approved: bool, note: str,
                        slot: CharacterSlot | None = None,
                        request_id: str | None = None) -> None:
        """Worker: settle the pending request (/yes or /no)."""
        slot = slot or self.active
        with self._disclosure_lock:
            secret_id = slot.pending_secret
            secret = slot.secrets.get(secret_id) if secret_id else None
            pending = slot.pending_disclosure
            stale = request_id is not None and (
                pending is None or pending.request_id != request_id or not pending.matches(secret))
            if not stale:
                if secret is not None and approved:
                    self._mark_revealed(slot, secret)
                slot.pending_secret = None
        if stale:
            self._invalidate_disclosures()
            self._emit(SecretDisclosuresChanged())
            return
        if secret is None:  # raced a /reload or the queue; nothing to settle
            self._emit(SecretNote("[no secret is awaiting a decision]"))
            return
        if secret.mode == "hesitate":  # clear the table's pondering state
            self._emit(SecretPondering(slot.name, active=False))
        if not approved:
            slot.denied_secrets.add(secret.id)
            self._add_ooc(deny_note(secret, note), emit=False, slot=slot)
            self._emit(SecretResolved(slot.name, secret.id, False, note))
            self._finish_disclosure(slot, "rejected")
            return
        self._emit(SecretResolved(slot.name, secret.id, True, note))
        self._finish_disclosure(slot, "approved")
        if slot is not self.active:
            self._switch_npc(slot.stem)
        self._deliver_secret(slot, secret, note)

    def _finish_disclosure(self, slot: CharacterSlot, decision: str) -> None:
        with self._disclosure_lock:
            pending, slot.pending_disclosure = slot.pending_disclosure, None
            if pending is None:
                return
            self._disclosure_decisions[pending.request_id] = decision
        self._emit(SecretDisclosureResolved(pending.request_id, decision))

    def _resolve_disclosure(self, request_id: str, decision: str) -> None:
        """Worker only: resolve the original NPC, never whichever is active now."""
        self._invalidate_disclosures()
        with self._disclosure_lock:
            previous = self._disclosure_decisions.get(request_id)
            slot = next((s for s in self.roster.values()
                         if s.pending_disclosure is not None
                         and s.pending_disclosure.request_id == request_id), None)
        if previous is not None:
            if previous == decision:
                self._emit(SecretDisclosureResolved(request_id, previous))
            else:
                self._emit(SecretDisclosuresChanged())
            return
        if slot is None:
            self._emit(SecretDisclosuresChanged())
            return
        self._resolve_secret(decision == "approved", "", slot=slot, request_id=request_id)

    def _invalidate_disclosures(self) -> None:
        """An edited/deleted proposal requires a fresh request and GM review."""
        changed = False
        with self._disclosure_lock:
            for slot in tuple(self.roster.values()):
                pending = slot.pending_disclosure
                if pending is None:
                    continue
                if (slot.pending_secret == pending.secret_id
                        and pending.matches(slot.secrets.get(pending.secret_id))):
                    continue
                if slot.pending_secret == pending.secret_id:
                    slot.pending_secret = None
                slot.pending_disclosure = None
                changed = True
        if changed:
            self._emit(SecretDisclosuresChanged())

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
        with self._disclosure_lock:
            slot.pending_disclosure = None
        self._emit(SecretDisclosuresChanged())

    def _unlock_secret(self, secret_id: str) -> None:
        """Worker: /reveal — unlock proactively, no immediate speech; the NPC
        volunteers it at the next natural moment instead."""
        slot = self.active
        secret = slot.secrets.get(secret_id)
        if secret is None or secret.revealed:
            self._emit(SecretNote(f"[({secret_id}) not unlockable — /secrets lists them]"))
            return
        self._mark_revealed(slot, secret)
        if slot.pending_secret == secret.id:
            slot.pending_secret = None
            if secret.mode == "hesitate":
                self._emit(SecretPondering(slot.name, active=False))
        slot.denied_secrets.discard(secret.id)
        self._add_ooc(
            f"The GM has unlocked the topic ({secret.id}) — your character "
            "now knows it fully (see 'Knowledge you may now share'); bring it "
            "up at the next natural moment.", emit=False)
        self._emit(SecretResolved(slot.name, secret.id, True, ""))
        if (pending := slot.pending_disclosure) is not None and pending.secret_id == secret.id:
            self._finish_disclosure(slot, "approved")

    def _mark_revealed(self, slot: CharacterSlot, secret: Secret) -> None:
        """Persist the reveal (atomic write-back) and put it on the record so
        the session summary knows what the players learned."""
        previous = secret.revealed
        secret.revealed = f"session {self.session_no}"
        try:
            if slot.secrets_path is not None:
                slot.secrets.save(slot.secrets_path)
        except Exception:
            secret.revealed = previous
            raise
        slot.turns.append(("GM", f"revealed the secret ({secret.id}): {secret.hint}"))
        slot.dirty = True
        self.transcript.append_turn("GM", f"[revealed secret ({secret.id}): {secret.hint}]")

    def _deliver_secret(self, slot: CharacterSlot, secret: Secret,
                        rider: str) -> None:
        """The /yes follow-up turn: the body is now in the prompt's revealed
        block, and a one-shot GM instruction tells the NPC to share it."""
        self._set_state_if({State.IDLE}, State.PROCESSING)
        system = self._system_prompt(slot, secret_topics=set())
        messages = slot.history.as_messages() + [
            {"role": "user", "content": delivery_instruction(secret, rider)}]
        timings: dict[str, float] = {}
        self._reply_turn(slot, system, messages, timings, player_line="", secret_topics=set())

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
            if not self._accepting or self._resetting.is_set():
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

    def _speak_npc(self, *, action: str | None = None) -> None:
        """Worker-only GM speech, using the same voice and guards as a reply."""
        self._set_state_if({State.IDLE}, State.PROCESSING)
        slot = self.active
        system = self._system_prompt(slot, secret_topics=set())
        _, speech_contexts = split_character_speech(slot.character)
        messages = slot.history.as_messages()
        # Keep the last opening available even after history trimming.
        if slot.last_opening and not any(
                m["role"] == "assistant" and m["content"] == slot.last_opening
                for m in messages):
            messages.append({"role": "assistant", "content": slot.last_opening})
        messages.append({"role": "user", "content": opening_instruction(
            self.config.language_for(slot.stem), action=action,
            context=speech_contexts.get(action, ""))})
        self._warn_if_over_budget(system, messages)
        self._reply_turn(slot, system, messages, {}, player_line="",
                         secret_topics=set(), opening=True)

    def _speaker_for(self, slot: CharacterSlot):
        """The speaker for a slot's voice, created lazily and cached by voice
        name; unavailable voices fall back to the campaign default."""
        voice = slot.voice or self.config.tts.voice
        if voice in self._speakers:
            return self._speakers[voice]
        if self._make_speaker is None:
            return self._default_speaker
        try:
            speaker = self._make_speaker(self.config.tts.voice_resource_for(voice))
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
            self._check_session_reset()
            slot.logbook.upsert_entry(self.session_no, date.today().isoformat(), body)
            slot.dirty = False
            self._emit(LogbookWritten(str(slot.logbook.path), kind))
