# Mobile WebSocket API (protocol 1)

The mobile endpoint is an optional player interface to the existing engine.
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
```

For a phone on the same LAN, explicitly change `listen` to the computer's LAN
IP or `"0.0.0.0"`. Startup prints the WebSocket URL and a warning when listening
beyond loopback. Anyone who can connect can send dialogue, switch NPCs and stop
local speech. There is no authentication or encryption in this version.
Configuration changes require restarting `npc run`; `/reload` reports this.
If the port is unavailable, the CLI continues with mobile disabled.

## Connection and commands

Connect to `/ws`. Other paths return HTTP 404. The first text frame is UTF-8 JSON:

```json
{"type":"hello","protocol":1,"npc":"Mordrek","state":"idle"}
```

The name and state are current when connecting, including mid-turn. `npc` is
the character's display name; use the file stem, name or unambiguous name prefix
to switch, just like `/npc`. Commands are JSON objects, one per text frame:

| Command | Example | Behavior |
| --- | --- | --- |
| `say` | `{"type":"say","text":"Bonjour, aubergiste."}` | Same worker pipeline as `/say`; never interprets the text as a CLI/GM command. |
| `switch_npc` | `{"type":"switch_npc","npc":"mordrek"}` | Queued behind current work, using the existing roster resolver. |
| `status` | `{"type":"status"}` | Replies only to the requester with `{"type":"status","npc":"Mordrek","state":"idle"}`. |
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

Other forwarded events: `recording_started` (with `auto_stop`),
`recording_discarded`, `heard_nothing`, `busy`, `voice_unavailable`, and
`session_ending`. Reply chunks occur when the existing LLM/TTS streaming path
is active. `npc_replied` is the completed reply; its timing relative to audio
follows the existing engine pipeline (before playback in plain mode, after
playback in streaming mode). Display it as the final text, not an extra turn.

The mobile serializer requires `table_safe=True` and `dm_only=False` on every
event, including loopback connections, and exports only allowlisted fields.
GM notes, secrets, secret identifiers, pending/reveal requests, logbook data,
model configuration and the GM status report are never forwarded. Unknown
event types fail closed. Engine errors remain on the GM console; mobile
`error` messages describe protocol/command failures without internal details.
The existing secret approval and marker scrubbers remain in charge of what
the NPC may say; mobile cannot approve or unlock secrets.

## Piper audio output

Piper synthesis is observed when a sanitized segment starts local playback.
Both `say` and sentence streaming use the same observer, including per-NPC
voices. Local audio is still played, with no duplicate synthesis. If spoken
replies are unavailable locally, the mobile API still sends text but no audio.

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

- **Phone audio upload:** binary input is currently rejected with a protocol
  error. A future transport can validate/assemble a clip and call
  `app.submit_audio_clip(AudioClip(samples, sample_rate))`. Use a one-dimensional
  NumPy `int16` array and a positive integer sample rate. No microphone is
  needed, but a transcriber must be available. The app owns a copy and queues
  the unchanged energy gate → Whisper → player event → LLM → Piper pipeline.
  Invalid/too-short clips, a busy/recording engine, or shutdown return `False`.
  Silence/hallucination handling remains in the worker.
- **Authentication:** `MobileServer(..., authorize=callback)` provides a
  handshake hook before hello. The callback receives the WebSocket request
  and returns a boolean; a rejection returns HTTP 401. A later token scheme
  can use this seam without exposing GM capabilities.
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
  private queue, LLM, secrets or memory. Stop the server in the embedding
  application's `finally` block.

Tests run with existing fakes and real localhost sockets:

```bash
uv run pytest
uv run ruff check .
```
