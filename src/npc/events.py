"""Structured events emitted by NPCApp.

The app never prints. It emits Event objects to a single on_event callback;
the terminal UI renders them with format_event(), and the overlay
(overlay.py) broadcasts them as JSON to WebSocket clients. External pages
consume {"type": <class name>, ...fields} — renaming an event class or field
is therefore a BREAKING change for overlay consumers.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar, Literal


class State(StrEnum):
    IDLE = "idle"
    RECORDING = "recording"
    PROCESSING = "processing"
    SPEAKING = "speaking"


@dataclass(frozen=True, slots=True)
class Event:
    """Base class for all app events."""

    # DM-eyes-only: never published to the overlay websocket (cli.on_event
    # gates on this) — secret ids and hints must not reach the table screen
    dm_only: ClassVar[bool] = False
    # published even when the overlay listens beyond localhost; everything
    # not marked table_safe is GM-console material (notes, status, errors)
    # that must stay on this machine — see the block at the end of this file
    table_safe: ClassVar[bool] = False


@dataclass(frozen=True, slots=True)
class StateChanged(Event):
    """Every state transition — the heartbeat an overlay would visualize."""
    state: State


@dataclass(frozen=True, slots=True)
class RecordingStarted(Event):
    auto_stop: bool = False  # True when a VAD recorder will end it on silence


@dataclass(frozen=True, slots=True)
class RecordingDiscarded(Event):
    reason: str


@dataclass(frozen=True, slots=True)
class HeardNothing(Event):
    pass


@dataclass(frozen=True, slots=True)
class PlayerSpoke(Event):
    """A voice utterance was transcribed (always in-character)."""
    text: str


@dataclass(frozen=True, slots=True)
class NpcReplied(Event):
    npc_name: str
    text: str


@dataclass(frozen=True, slots=True)
class NpcReplyChunk(Event):
    """A fragment of the reply as it streams from the LLM. Overlay material —
    the CLI waits for the complete NpcReplied instead."""
    text: str


@dataclass(frozen=True, slots=True)
class GmNoteAdded(Event):
    text: str


@dataclass(frozen=True, slots=True)
class NpcSwitched(Event):
    """The GM switched the active NPC with /npc."""
    npc_name: str
    voice: str | None = None  # piper voice name; None = campaign default


@dataclass(frozen=True, slots=True)
class Busy(Event):
    pass


@dataclass(frozen=True, slots=True)
class VoiceUnavailable(Event):
    pass


@dataclass(frozen=True, slots=True)
class MicrophoneError(Event):
    message: str


@dataclass(frozen=True, slots=True)
class ErrorOccurred(Event):
    message: str


@dataclass(frozen=True, slots=True)
class TurnCompleted(Event):
    """Per-stage timings for one player turn, measured from the clip landing
    in the worker (or the /say line) until the reply finished. On the
    streaming path the LLM and TTS stages overlap, so stages don't sum to
    total. Hidden in the CLI by default — `npc run --timings` prints them."""
    stt_seconds: float | None            # None for typed /say turns
    llm_first_token_seconds: float | None  # None on the non-streaming path
    llm_seconds: float
    speak_seconds: float | None          # None when spoken replies are disabled
    total_seconds: float


@dataclass(frozen=True, slots=True)
class SessionEnding(Event):
    pass


@dataclass(frozen=True, slots=True)
class LogbookWritten(Event):
    path: str
    kind: Literal["save", "checkpoint", "end"]


@dataclass(frozen=True, slots=True)
class ConfigReloaded(Event):
    applied: tuple[str, ...]
    restart_needed: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StatusReport(Event):
    state: State
    npc_name: str
    model: str
    session_no: int
    player_turns: int
    gm_notes: int
    last_turn_seconds: float | None = None
    avg_turn_seconds: float | None = None
    roster_size: int | None = None  # rendered only when the campaign has >1 NPC


@dataclass(frozen=True, slots=True)
class SecretDisclosureRequested(Event):
    """Explicit mobile GM payload; never sent to the table overlay."""
    dm_only: ClassVar[bool] = True
    request_id: str
    npc: str
    secret: str
    context: str


@dataclass(frozen=True, slots=True)
class SecretDisclosureResolved(Event):
    dm_only: ClassVar[bool] = True
    request_id: str
    decision: str


@dataclass(frozen=True, slots=True)
class SecretDisclosuresChanged(Event):
    """Ask mobile GM clients to refresh their complete pending snapshot."""
    dm_only: ClassVar[bool] = True


@dataclass(frozen=True, slots=True)
class SecretRevealRequested(Event):
    """The NPC hit a locked topic and asks the GM to unlock it (/yes or /no)."""
    dm_only: ClassVar[bool] = True
    npc_name: str
    secret_id: str
    hint: str
    player_line: str  # what the player said that triggered it ("" for none)


@dataclass(frozen=True, slots=True)
class SecretResolved(Event):
    dm_only: ClassVar[bool] = True
    npc_name: str
    secret_id: str
    approved: bool
    note: str  # the GM's free-text rider, "" if none


@dataclass(frozen=True, slots=True)
class SecretPending(Event):
    """Per-turn reminder that a reveal request still awaits /yes or /no."""
    dm_only: ClassVar[bool] = True
    npc_name: str
    secret_id: str


@dataclass(frozen=True, slots=True)
class SecretList(Event):
    """The /secrets status listing for the active NPC."""
    dm_only: ClassVar[bool] = True
    npc_name: str
    lines: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SecretNote(Event):
    """Free-text secret-related notice that must stay off the overlay."""
    dm_only: ClassVar[bool] = True
    message: str


@dataclass(frozen=True, slots=True)
class SecretPondering(Event):
    """Overlay flavor while a hesitate-mode request goes to the GM: the NPC
    is 'thinking'. Deliberately content-free — safe for the table screen.
    active=False clears it once the GM has decided."""
    npc_name: str
    active: bool = True


@dataclass(frozen=True, slots=True)
class Info(Event):
    """Free-text output with no structured payload (help text, usage hints)."""
    message: str


# The overlay exposure contract, tier 2 (tier 1 is dm_only above): when the
# overlay listens beyond localhost, ONLY these events cross the network.
# Assigned in one block rather than per class so the whole table-facing
# surface is reviewable at a glance — think before adding to this list.
for _cls in (StateChanged, RecordingStarted, RecordingDiscarded, HeardNothing,
             PlayerSpoke, NpcReplyChunk, NpcReplied, NpcSwitched, Busy,
             MicrophoneError, VoiceUnavailable, TurnCompleted, SessionEnding,
             SecretPondering):
    _cls.table_safe = True
del _cls


def format_event(event: Event) -> str | None:
    """Render an event for the terminal; None means 'not shown in the CLI'."""
    match event:
        case StateChanged():
            return None  # overlay material — too chatty for the terminal
        case TurnCompleted():
            return None  # shown only via `npc run --timings` (format_timings)
        case RecordingStarted(auto_stop=auto):
            return "[recording… pause to send]" if auto else "[recording… release to send]"
        case RecordingDiscarded(reason=reason):
            return f"[{reason} — discarded]"
        case HeardNothing():
            return "[heard nothing]"
        case PlayerSpoke(text=text):
            return f"[player] {text}"
        case NpcReplied(npc_name=name, text=text):
            return f"[{name}] {text}"
        case NpcReplyChunk():
            return None  # the CLI prints the finished NpcReplied line
        case GmNoteAdded():
            return "[noted — will shape the NPC's behavior]"
        case NpcSwitched(npc_name=name, voice=voice):
            text = f"[now speaking: {name}"
            return text + (f" — voice {voice}]" if voice else "]")
        case Busy():
            return "[busy — still working on the previous line]"
        case VoiceUnavailable():
            return "[voice input unavailable — run `npc doctor`]"
        case MicrophoneError(message=message):
            return f"[microphone error: {message}]"
        case ErrorOccurred(message=message):
            return f"[error: {message}]"
        case SessionEnding():
            return "[summarizing session into logbook…]"
        case LogbookWritten(path=path, kind=kind):
            return {"save": f"[logbook saved: {path}]",
                    "checkpoint": "[logbook checkpoint written]",
                    "end": f"[logbook updated: {path}]"}[kind]
        case ConfigReloaded(applied=applied, restart_needed=restart_needed):
            text = f"[reloaded: {', '.join(applied)}]"
            if restart_needed:
                text += f"\n[changed {', '.join(restart_needed)} — takes effect after restart]"
            return text
        case StatusReport() as s:
            roster = (f" (roster {s.roster_size})"
                      if s.roster_size and s.roster_size > 1 else "")
            timing = ""
            if s.last_turn_seconds is not None:
                timing = (f" | last turn {s.last_turn_seconds:.1f}s, "
                          f"avg {s.avg_turn_seconds:.1f}s")
            return (f"[state: {s.state} | NPC: {s.npc_name}{roster} | "
                    f"model: {s.model} | "
                    f"session {s.session_no}, {s.player_turns} player turns | "
                    f"{s.gm_notes} standing GM notes{timing}]")
        case SecretRevealRequested(npc_name=name, secret_id=sid,
                                   hint=hint, player_line=player_line):
            lines = [f"⚑ {name} asks to reveal ({sid}) — {hint}"]
            if player_line:
                lines.append(f'   player: "{player_line}"')
            lines.append("   → /yes [note] · /no [note] · /later — stays pending until you decide")
            return "\n".join(lines)
        case SecretResolved(secret_id=sid, approved=approved, note=note):
            verdict = "revealed" if approved else "stays hidden"
            return f"[secret ({sid}) {verdict}" + (f" — {note}]" if note else "]")
        case SecretPending(secret_id=sid):
            return f"[reminder: ({sid}) still awaits /yes or /no]"
        case SecretList(npc_name=name, lines=lines):
            return "\n".join([f"Secrets of {name}:", *(f"  {line}" for line in lines)])
        case SecretNote(message=message):
            return message
        case SecretPondering():
            return None  # overlay flavor; the CLI shows SecretRevealRequested
        case SecretDisclosureRequested() | SecretDisclosureResolved() | SecretDisclosuresChanged():
            return None  # mobile GM payloads; the CLI has its own secret events
        case Info(message=message):
            return message
        case _:
            return repr(event)


def format_timings(event: TurnCompleted) -> str:
    """Render TurnCompleted for `npc run --timings`."""
    parts = []
    if event.stt_seconds is not None:
        parts.append(f"stt {event.stt_seconds:.1f}s")
    llm = f"llm {event.llm_seconds:.1f}s"
    if event.llm_first_token_seconds is not None:
        llm += f" (first token {event.llm_first_token_seconds:.2f}s)"
    parts.append(llm)
    if event.speak_seconds is not None:
        parts.append(f"speak {event.speak_seconds:.1f}s")
    return f"[turn {event.total_seconds:.1f}s: {' | '.join(parts)}]"


def print_event(event: Event) -> None:
    """Default subscriber: print what format_event renders."""
    text = format_event(event)
    if text is not None:
        print(text)
