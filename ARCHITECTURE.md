# Architecture

`npc` is a fully-offline voice agent that role-plays a TTRPG NPC: hold a
key, speak, and a local LLM answers aloud in character. This document is the
map for people who want to read or change the code.

## The pipeline

```mermaid
flowchart LR
    PTT[push-to-talk\nevdev hotkey] --> REC[Recorder\nsounddevice]
    REC --> STT[faster-whisper\nsv/en auto-detect]
    STT --> PROMPT[prompt assembly\nsheet + lore + secrets\n+ logbook + GM notes]
    PROMPT --> LLM[local LLM\nOllama / OpenAI-compatible]
    LLM -->|token stream| SENT[sentence regrouper]
    SENT --> CLEAN[dialogue sanitizer\nstrips narration & markers]
    CLEAN --> TTS[Piper TTS]
    TTS --> OUT[speaker]
    LLM -. events .-> BUS
    STT -. events .-> BUS
    BUS[(event bus)] --> CLI[terminal UI]
    BUS --> WS[overlay websocket\nOBS / table display]
    BUS --> MOBILE[mobile websocket\nplayer API]
    MOBILE --> WORKER[NPCApp worker queue]
    WORKER --> PROMPT
    TTS -. played PCM .-> MOBILE
```

Everything runs on one machine. Nothing ever calls the internet.

## Threads and state

Four threads, coordinated by `NPCApp` (`src/npc/app.py`), a state machine
`IDLE → RECORDING → PROCESSING → SPEAKING`:

1. **Main** — the `prompt_toolkit` REPL (`gm>`), where typed lines are
   out-of-character GM instructions and `/commands`.
2. **Hotkey listener** — evdev key-down/key-up (terminals don't deliver
   key-up, which is why this exists). Pressing the key while the NPC speaks
   is barge-in: playback stops, recording starts.
3. **Worker** — a single queue serializes every turn (STT → LLM → TTS) and
   every state-touching command, so nothing races.
4. **Audio callback** — sounddevice's thread, which only copies samples.

Voice input is *always* in-character player dialogue; the keyboard is
*always* the GM. That split is the core interaction design.

## Events, not prints

The app never prints. It emits typed events (`src/npc/events.py`) —
`PlayerSpoke`, `NpcReplied`, `SecretRevealRequested`, … — to one callback;
the CLI renders them, tests assert on them, and the overlay broadcasts them
as JSON. Two class-level flags form the exposure contract:

- `dm_only` — never leaves the machine (everything about secrets).
- `table_safe` — the only events broadcast when the overlay listens on the
  LAN; GM notes, status, and errors are neither and stay home.

## The campaign directory

A campaign is a folder of plain markdown — the whole "database":

```
character.md      who the NPC is (first # heading = display name)
characters/*.md   or several NPCs; /npc switches at the table
adventure.md      GM background
secrets.md        GM-gated clues — see below
lore/             reference docs injected as established fact
logbook.md        per-NPC session memory, written by the LLM
sessions/         raw transcripts, appended crash-safe every turn
config.toml       models, voices, hotkey — all optional
```

Every piece of per-NPC state (sheet, history, GM notes, logbook, secrets,
lore, voice) lives on one `CharacterSlot` (`src/npc/roster.py`); switching
NPCs swaps the slot. Memory is strictly per-NPC, in-session and across
sessions.

## Five guarantees

1. **Fully offline.** Whisper, the LLM, and Piper are all local; the only
   network listeners are the optional overlay and mobile API, loopback-bound
   unless you opt in.
2. **Per-NPC isolation.** What players tell one NPC never reaches another —
   not in the prompt, not in the logbook, not in the summaries.
3. **Secrets can't leak.** Gated clues never enter the prompt until the GM
   approves at the console; the model cannot reveal text it has never seen.
   (`scripts/probe_secrets.py` pins the behavior against a real model.)
4. **English out, always.** Players can speak Swedish; replies are forced to
   English (the TTS voice's language) by prompt rules plus a detector with
   a corrective re-ask — the prompt alone doesn't survive baiting.
5. **Crash-safe writes.** Logbooks and secrets write via temp-file +
   `os.replace`; transcripts append per turn. A crash costs nothing.

## Seams

Every hardware/model dependency sits behind a small Protocol —
`Transcriber`, `Speaker`, `Recorder`, the LLM clients — and the entire test
suite (280+ tests) runs against fakes: no GPU, no microphone, no server.
`tests/test_app_pipeline.py` is the best starting point for how the pieces
meet; `npc doctor` is the runtime mirror of the same seams.

## Mobile player interface

`mobile_server.py` owns a separate asyncio thread and `/ws` endpoint on port
8766. It is disabled by default (`[mobile] enabled` / `npc run --mobile`).
It calls only public `NPCApp` player methods: `submit_player_text`, `switch_npc`,
`stop_speaking`, and `player_status`. Text and switching enter the existing
worker queue; the server owns no conversation state. `submit_audio_clip`
prepares the same seam for future phone uploads without duplicating STT.

`mobile_protocol.py` defines version 1 and explicitly serializes player payloads.
Both `dm_only=False` and `table_safe=True` are mandatory, even on loopback;
an allowlist further excludes unknown events and all arbitrary exception text.
The overlay's existing wire format and exposure policy are unchanged.

`PiperSpeaker.on_audio` observes each sanitized segment when playback begins,
on both the plain and streaming paths. It never synthesizes twice, and an
observer failure cannot interrupt local playback. The CLI wires this callback
for the default speaker and every per-NPC voice, sharing the existing player.
Mobile publication queues whole PCM segments without awaiting network I/O;
bounded engine/client queues and send deadlines disconnect lagging clients.
See [docs/mobile.md](docs/mobile.md) for framing, limitations and extension points.

## Reading order

`events.py` → `app.py` → `session/prompt.py` → `roster.py`, then whichever
seam you care about (`stt.py`, `tts.py`, `llm.py`, `overlay.py`,
`session/secrets.py`, `session/lore.py`).
