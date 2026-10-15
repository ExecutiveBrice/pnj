"""Version 1 player protocol. Explicit payloads prevent accidental field leaks.

The overlay's class-name/asdict wire format remains independent and unchanged.
New mobile events must pass BOTH exposure flags and this allowlist.
"""

from __future__ import annotations

import json

from .events import (
    Busy,
    Event,
    HeardNothing,
    NpcReplied,
    NpcReplyChunk,
    NpcSwitched,
    PlayerSpoke,
    RecordingDiscarded,
    RecordingStarted,
    SessionEnding,
    StateChanged,
    VoiceUnavailable,
)

PROTOCOL_VERSION = 1


def encode(message: dict) -> str:
    return json.dumps(message, ensure_ascii=False)


def event_to_message(event: Event, npc: str) -> dict | None:
    """Fail closed, even on loopback. Never serialize arbitrary event fields."""
    if event.dm_only or not event.table_safe:
        return None
    # Exact types also prevent a subclass from smuggling extra private fields.
    kind = type(event)
    if kind is StateChanged:
        return {"type": "state", "state": event.state.value}
    if kind is PlayerSpoke:
        return {"type": "player_spoke", "text": event.text}
    if kind is NpcReplyChunk:
        return {"type": "npc_reply_chunk", "npc": npc, "text": event.text}
    if kind is NpcReplied:
        return {"type": "npc_replied", "npc": event.npc_name, "text": event.text}
    if kind is NpcSwitched:
        return {"type": "npc_switched", "npc": event.npc_name}
    if kind is RecordingStarted:
        return {"type": "recording_started", "auto_stop": event.auto_stop}
    if kind is RecordingDiscarded:
        # The engine's reason may include a raw transcription. Keep it local.
        return {"type": "recording_discarded"}
    if kind in (Busy, HeardNothing, VoiceUnavailable, SessionEnding):
        name = {Busy: "busy", HeardNothing: "heard_nothing",
                VoiceUnavailable: "voice_unavailable", SessionEnding: "session_ending"}[kind]
        return {"type": name}
    return None
