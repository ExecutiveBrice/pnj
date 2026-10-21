# Mobile WebSocket API (protocol 1)

The mobile endpoint is an optional GM companion interface to the existing engine.
It works offline and runs alongside the CLI and overlay. Start it with:

```bash
npc run campaigns/mygame --mobile
```

Or configure the campaign's `config.toml`:

```toml
[mobile]
enabled = true         # default: false
listen = "127.0.0.1"   # default: local connections only
port = 8766
local_playback = true  # false: audio on connected clients only; PC speakers stay silent
secret_approval = true # default: true; false restores a player-only endpoint
```

For a phone on the same LAN, explicitly change `listen` to the computer's LAN
IP or `"0.0.0.0"`. Startup prints the WebSocket URL and a warning when listening
beyond loopback. Anyone who can connect can send dialogue, switch NPCs and stop
local speech. There is no authentication or encryption in this version.
With `secret_approval = true`, connected phones are trusted GM consoles: they
also receive proposed secrets and can approve or reject disclosure. Reserve
this endpoint for the GM; set it to `false` when connecting player devices.
Configuration changes require restarting `npc run`; `/reload` reports this.
If the port is unavailable, the CLI continues with mobile disabled.

## Connection and commands

Connect to `/ws`. Other paths return HTTP 404. The first text frame is UTF-8 JSON:

```json
{"type":"hello","protocol":1,"npc":"Mordrek","state":"idle","npcs":[{"id":"mordrek","name":"Mordrek"},{"id":"vess","name":"Vess"}],"capabilities":{"text_input":true,"audio_input":true,"audio_output":true,"npc_switch":true,"secret_approval":true}}
```

The name and state are current when connecting, including mid-turn. `npc` is
the character's display name; use the file stem, name or unambiguous name prefix
to switch, just like `/npc`. `npcs` lists the campaign's characters for a selector:
display each `name` and send its `id` as `switch_npc.npc`. IDs are file stems,
so selection also works when display names coincide. Both `hello` and `status`
include this list; request `status` after a GM `/reload` to refresh it. Character
sheets, voices, secrets and conversation history are not included in these snapshots.
For mobile clients whose picker consumes a separate event, the server also
sends `{"type":"npc_list","npcs":[{"id":"mordrek","name":"Mordrek"},{"id":"vess","name":"Vess"}]}`
immediately after `hello` and each `status` response, to the same client. This
event carries the same list as the preceding snapshot; no client update is needed.
`audio_input` is true only when the server has a
transcriber; `audio_output` reflects whether a speaker is available. With mobile
enabled, Whisper can load even when the PC microphone or hotkey is unavailable.
Commands are JSON objects, one per text frame:

| Command | Example | Behavior |
| --- | --- | --- |
| `say` | `{"type":"say","text":"Bonjour, aubergiste."}` | Same worker pipeline as `/say`; never interprets the text as a CLI/GM command. |
| `switch_npc` | `{"type":"switch_npc","npc":"mordrek"}` | Queued behind current work, using the existing roster resolver. |
| `status` | `{"type":"status"}` | Replies only to the requester with the current `npc`, `state` and `npcs` list, as in `hello`. |
| `stop_speaking` | `{"type":"stop_speaking"}` | Calls the active speaker's `stop()` if the engine is speaking; otherwise a no-op. |

`say`, switching and stop have no separate success acknowledgement. Observe
conversation/state events, or request status. Accepted dialogue and switches
are serialized by the same worker as CLI actions; concurrent clients share
the active NPC and conversation. A queued command is not necessarily complete
when the next status request is answered. Invalid/ambiguous requests produce
`{"type":"error","message":"..."}` for the requester. Internal exception
details stay local. Commands larger than 32 KiB close the connection (1009).

States are `idle`, `processing`, `speaking`, and `recording` (local microphone).
Stop interrupts local playback, including the existing sentence-streaming path;
it does not cancel a processing LLM request or remove already queued turns.
A mobile client should stop its own audio playback when it requests a stop,
and when it receives `recording` for local push-to-talk barge-in.

## Published events

All connected clients receive these UTF-8 JSON text frames:

```json
{"type":"state","state":"processing"}
{"type":"player_spoke","text":"Bonjour…"}
{"type":"npc_reply_chunk","npc":"Mordrek","text":"Je ne sais…"}
{"type":"npc_replied","npc":"Mordrek","text":"Je ne sais pas où il est parti."}
{"type":"npc_switched","npc":"Mordrek"}
```

`npc_list` is sent to each client on connection and when it requests `status`,
as described above. Other clients do not receive that status refresh.

Other forwarded events: `recording_started` (with `auto_stop`),
`recording_discarded`, `heard_nothing`, `busy`, `voice_unavailable`, and
`session_ending`. Reply chunks occur when the existing LLM/TTS streaming path
is active. `npc_replied` is the completed reply; its timing relative to audio
follows the existing engine pipeline (before playback in plain mode, after
playback in streaming mode). Display it as the final text, not an extra turn.

The player event serializer requires `table_safe=True` and `dm_only=False` on every
event, including loopback connections, and exports only allowlisted fields.
GM notes, secrets, secret identifiers, pending/reveal requests, logbook data,
model configuration and the GM status report are never forwarded through it. Unknown
event types fail closed. Engine errors remain on the GM console; mobile
`error` messages describe protocol/command failures without internal details.
The existing secret approval and marker scrubbers remain in charge of what
the NPC may say. The separate GM disclosure extension below exports only its
explicit payloads to GM connections; the table overlay keeps filtering all
`dm_only` events.

## GM approval of secret disclosures

The Écho mobile app uses this protocol 1 extension when
`hello.capabilities.secret_approval` is true. Restart the server after updating
and reconnect the phone to refresh capabilities. No mobile code change is needed.

When an NPC requests permission, GM connections receive:

```json
{"type":"secret_disclosure_requested","request_id":"unique-proposal-id","npc":"Mordrek","secret":"The passage beneath the tower leads to the treasure.","context":"How can we get inside the tower?"}
```

`secret` is the actual proposed content for the GM to review. It is kept out of
the LLM prompt, player text and audio until approval. `context` is the triggering
player line, possibly empty. Each proposal has a unique ID, stable across phone
reconnections for the running session. Every `status` response to a GM is followed
by `npc_list` and then a complete snapshot across **all** NPCs, including inactive
ones (the same four fields, without `type`):

```json
{"type":"secret_disclosure_list","requests":[]}
```

The mobile app sends a decision, and the worker confirms it to all GM clients
after applying it:

```json
{"type":"resolve_secret_disclosure","request_id":"unique-proposal-id","decision":"approved"}
{"type":"secret_disclosure_resolved","request_id":"unique-proposal-id","decision":"approved"}
```

Decisions are `approved` or `rejected`. Approval uses the existing `/yes` flow:
persist the reveal and generate its spoken delivery. If that NPC is inactive,
the engine switches to it before delivery so its voice, language and mobile
attribution are correct. Rejection uses `/no`: keep the body locked, add the
standing deflection instruction to that NPC only, and leave the current NPC
unchanged. The NPC has already stalled or deflected on the triggering turn.

Decisions are serialized with dialogue and CLI commands. A repeated identical
decision re-sends confirmation without another delivery; a conflicting or stale
decision refreshes the pending list without changing the original outcome.
CLI `/yes`, `/no` and `/reveal` also confirm the corresponding mobile request.
`/later` removes it; a subsequent request gets a new ID. `/reload` invalidates
requests whose proposed body or hint changed or disappeared, and sends a fresh
list. Approval is not confirmed if writing the reveal fails.

With `secret_approval = false`, the capability is false, disclosure events and
snapshots are withheld, and decision commands are rejected. Embedders can also
pass `MobileServer(..., authorize_gm=callback)` to decide the GM role at handshake
per connection. The callback receives the WebSocket request and must validate
the embedding application's credentials; a client-side capability flag does not
authenticate anyone. `authorize` still controls access to the whole endpoint.

## Phone microphone input

When `hello.capabilities.audio_input` is true, upload one recorded clip:

1. Send `{"type":"audio_input_start","sample_rate":16000,"channels":1,"format":"pcm_s16le"}`.
2. Send binary messages of at most 32768 bytes: signed 16-bit little-endian
   PCM, mono, without a WAV header or Base64.
3. Send `{"type":"audio_input_end"}`.

The server assembles a separate clip per connection, limited to 65 seconds
(the phone records for 60 seconds). Invalid formats, empty/truncated samples,
too-short clips, unavailable recognition and busy engines return an error to
the requester. Disconnecting or sending a command other than status/start/end
discards unfinished audio. A second start discards the current upload and
returns an error; start again to retry. No audio is replayed on reconnect.

Accepted clips use the existing energy gate, Whisper transcription and player
dialogue pipeline, producing `player_spoke` and the usual NPC replies. No PC
microphone is required. Upload and transcription start after release, rather
than while the phone is recording. Restart the server after updating and
reconnect the phone to receive the new capabilities.

## Piper and Pocket TTS audio output

Pocket TTS uses the same WebSocket endpoint and campaign voice assignments as
the desktop interface. From a source checkout, start with
`uv run --python 3.12 --locked --extra pocket npc run campaigns/mygame --mobile`
(add `--extra cuda` before `npc` for Whisper on NVIDIA GPUs). Set `[tts]`
`backend = "pocket"`, `model = "french"`, and `voice = "estelle"` in the campaign.
Pocket emits native streaming chunks: a reply contains multiple
`audio_start` / binary PCM / `audio_end` sequences at **24000 Hz**. Clients
must use the announced sample rate and queue the segments for continuous
playback. `audio_end` ends an audio segment, not the entire NPC reply.
Remote Pocket playback uses a monotonic sample clock with a bounded 160 ms
lead, rather than accumulating a full sleep after every chunk. Clients should
maintain a small jitter buffer and a continuous playback/resampling timeline;
restarting a player or resampler at each 80 ms segment can cause clicks or gaps.
`[mobile] local_playback = false` sends audio only to connected clients.

Piper synthesis is observed when a sanitized segment starts local playback.
Both `say` and sentence streaming use the same observer, including per-NPC
voices. Local audio is still played, with no duplicate synthesis. If spoken
replies are unavailable locally, the mobile API still sends text but no audio.

If the PC and a phone/browser are audible in the same room, their playback delay
can sound like an echo. Set `[mobile] local_playback = false` and restart the
server to listen through clients only. This also applies when switching NPCs.
Keep audio enabled on only one nearby client to avoid overlapping client voices.
If mobile is disabled or fails to start, local playback remains available.

Each segment (one sentence in streaming mode, the reply in plain mode) is:

1. A JSON text frame:
   `{"type":"audio_start","npc":"Mordrek","sample_rate":22050,"channels":1,"format":"pcm_s16le"}`.
2. One or more **binary** WebSocket messages, each at most 32768 bytes.
   Concatenate them in order: signed 16-bit little-endian PCM, mono, without
   a WAV header or Base64. Read the sample rate from `audio_start`; voices vary.
3. A JSON text frame: `{"type":"audio_end"}`.

There is no interleaving of other application messages inside a segment.
`audio_end` means the bytes were sent, not that playback completed. Clients
queue decoded segments for playback. They must explicitly use little-endian
decoding (e.g. `DataView.getInt16(offset, true)`) and the advertised sample rate.
This is sentence audio delivery, not synchronized real-time audio streaming.
Audio already delivered may outlast a local interruption; clients manage
their playback buffers as described above. No past audio is replayed when a
client reconnects.

## Minimal browser test

This example logs JSON events and binary PCM; it does not implement playback.
Run it in a browser context that allows a local `ws://` connection.

```javascript
const ws = new WebSocket("ws://localhost:8766/ws");
ws.binaryType = "arraybuffer";

ws.onmessage = event => {
  if (typeof event.data === "string") {
    console.log(JSON.parse(event.data));
  } else {
    console.log("PCM bytes:", event.data.byteLength);
  }
};

ws.onopen = () => {
  ws.send(JSON.stringify({ type: "say", text: "Bonjour, aubergiste." }));
};

// Once connected, with a matching NPC in this campaign:
// ws.send(JSON.stringify({ type: "switch_npc", npc: "mordrek" }));
// ws.send(JSON.stringify({ type: "status" }));
// ws.send(JSON.stringify({ type: "stop_speaking" }));
```

## Extension points and isolation

- **Phone audio upload:** validated uploads call
  `app.submit_audio_clip(AudioClip(samples, sample_rate))`. Use a one-dimensional
  NumPy `int16` array and a positive integer sample rate. No microphone is
  needed, but a transcriber must be available. The app owns a copy and queues
  the unchanged energy gate → Whisper → player event → LLM → Piper pipeline.
  Invalid/too-short clips, a busy/recording engine, or shutdown return `False`.
  Silence/hallucination handling remains in the worker.
- **Authentication:** `MobileServer(..., authorize=callback)` provides a
  handshake hook before hello. The callback receives the WebSocket request
  and returns a boolean; a rejection returns HTTP 401. A later token scheme
  can use this seam. `authorize_gm` separately gates GM disclosure access.
- **Audio consumers:** `PiperSpeaker(..., on_audio=callback)` receives
  `(samples, sample_rate)` at playback start. The callback must enqueue and
  return promptly; copy retained samples. Observer exceptions are isolated
  from local playback. `Speaker.say(text)` and `synthesize(text)` retain their
  existing contracts.
- **Network failures:** one asyncio thread handles all sockets. The engine
  publishes without awaiting network I/O through a bounded 128-item outbox,
  then bounded 64-item client outboxes. A segment is one item. Client queue
  overflow closes that client with code 1013; global outbox overflow closes
  current clients rather than letting a partial stream appear complete.
  Sends have a two-second deadline. Reconnect for a fresh hello/status;
  conversation replay and command replay are not part of protocol 1.
- **Embedding:** the CLI connects `on_event` to `mobile.publish` and Piper's
  observer to `mobile.publish_audio`. `MobileServer` never accesses the app's
  private queue, LLM, secret sheets or memory. GM commands and snapshots use
  explicit public engine methods. Stop the server in the embedding
  application's `finally` block.

Tests run with existing fakes and real localhost sockets:

```bash
uv run pytest
uv run ruff check .
```
