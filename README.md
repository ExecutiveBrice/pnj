# npc — an offline AI NPC for your game table

[![PyPI](https://img.shields.io/pypi/v/ttrpg-npc)](https://pypi.org/project/ttrpg-npc/)
[![vibe coded](https://img.shields.io/badge/vibe_coded-%E2%9C%A8-ff69b4?style=flat-square)](https://en.wikipedia.org/wiki/Vibe_coding)
[![coded with Claude](https://img.shields.io/badge/coded_with-Claude_Code-CC785C?style=flat-square&logo=anthropic)](https://claude.ai/code)
[![license: AGPL v3](https://img.shields.io/badge/license-AGPL_v3-blue?style=flat-square)](LICENSE)

**Hold a button, talk to an NPC, and it talks back — in character, out loud,
with no cloud in sight.**

![Demo: a spoken exchange with an NPC, including a GM-gated secret reveal](docs/demo.gif)

🔊 *Hear the voice: [docs/samples/vess-greeting.mp3](docs/samples/vess-greeting.mp3)* ·
🖥️ *[The table overlay](docs/overlay.png)* ·
🎲 *Try it now: `uv run npc run examples/rusty-lantern` — see the
[gallery](#example-campaigns)*

`npc` is a terminal program for game masters running tabletop RPGs like
[Numenera](https://numenera.com). You describe an NPC in a markdown file, start
a session, and hand your players a voice: hold the push-to-talk key, speak as a
player (in English **or Swedish**), and the NPC answers in spoken British
English, staying true to its personality, knowledge, and secrets. Between
sessions the NPC keeps a logbook, so it remembers what your party did last time.

Everything runs **locally on your own machine**. After the one-time model
downloads, no internet connection is used — your campaign never leaves the table.

## How it works

```mermaid
flowchart LR
    A["🔘 Hold key<br>(spacebar / USB button)"] --> B["🎤 Record<br>sounddevice"]
    B --> C["📝 Speech-to-text<br>faster-whisper<br>(sv/en auto-detect)"]
    C --> D["🧠 Local LLM<br>Ollama<br>+ character sheet<br>+ logbook"]
    D --> E["🔊 Text-to-speech<br>Piper (Alba, en_GB)"]
```

Two channels, one terminal:

| Input | Meaning |
|---|---|
| **Voice** (hold the push-to-talk key) | Always **in-character player dialogue** — the NPC hears a player speaking to it |
| **Typed text** at the `gm>` prompt | Always an **out-of-character instruction** to the underlying LLM — *"be more hostile"*, *"you just saw them steal your idol"* |

This separation is absolute by design: the LLM can never mistake table chatter
for stage direction, and your players can never "prompt-inject" the NPC by
talking to it.

## Desktop interface (Windows 11 / macOS / Linux)

The native Python desktop interface can create/open campaigns, download the
configured models, start/stop a session, send player dialogue or GM notes,
and record the microphone with an on-screen button. It runs the Python engine
in a separate process so model loading and generation do not block the window.
It does not require WSL or the Linux global hotkey.

Install Python 3.12 **with Tcl/Tk** (the python.org installer includes it),
[uv](https://docs.astral.sh/uv/getting-started/installation/), and
[Ollama](https://ollama.com/download). Start Ollama, then from this clone:

```bash
uv run --python 3.12 --frozen npc gui
# Optional: open a campaign immediately
uv run --python 3.12 --frozen npc gui examples/rusty-lantern
```

Alternatively, double-click **Lancer NPC.bat** on Windows or
**Lancer NPC.command** on macOS. These launchers use uv to install the locked
dependencies on first launch; internet is required for that setup. They are
source launchers, not standalone .exe/.app bundles. After an installed-package
setup, `npc-gui` also opens the window without a console on Windows.

In the window, choose/create a campaign, click **Préparer les modèles**, then
**Démarrer**. Preparation downloads Whisper, the configured Ollama model and
Piper voices; external API backends still require loading their model in their
own app. Use **Parole du joueur** for dialogue, **Note du MJ** for instructions,
or **Commande** for `/npc name`, `/save`, etc. Click **Enregistrer**, speak,
then click **Envoyer l’enregistrement**. Allow microphone access for Python
in your OS settings. This button works independently of `[hotkey]` settings.
**Terminer et mémoriser** waits for pending turns and writes the final summary.
**Forcer l’arrêt** can interrupt startup, downloads, or shutdown; it cannot
guarantee a final summary. Replies still use the project's English-only rules.

If Tkinter is missing, install a Python distribution with Tcl/Tk and recreate
the virtual environment using that interpreter. On Linux, install the
distribution's `python3-tk` package; on macOS with Homebrew Python, use the
matching `python-tk` package. Audio still needs the system's default input and
output devices. The desktop uses the campaign's `config.toml` for server,
model, language and voice settings; the OBS overlay remains a CLI feature.

[Guide d’installation en français](docs/desktop-fr.md).

## Requirements

- Windows 11 / macOS for the desktop interface, or Linux (X11 or Wayland for
  the terminal's global push-to-talk)
- Python ≥ 3.11 and [uv](https://docs.astral.sh/uv/)
- A local LLM server: [Ollama](https://ollama.com) (default) — **or** any app
  serving an OpenAI-compatible API, like [Jan](https://jan.ai),
  [LM Studio](https://lmstudio.ai), llama.cpp's server, or vLLM
- A microphone and speakers
- Optional but recommended: an NVIDIA GPU (a 7B–14B model + GPU whisper make
  responses fast enough for live play; CPU-only works with smaller models)

Disk footprint after setup: roughly 5–6 GB (LLM ≈ 4.5 GB, whisper-small
≈ 0.5 GB, Piper voice ≈ 60 MB).

## Installation

### 1. System packages (Ubuntu/Debian)

```bash
sudo apt install libportaudio2                  # microphone & speaker access for Python
sudo apt install python3-dev build-essential    # to build evdev, the key listener
sudo usermod -aG input $USER                    # lets npc read key press/release events
```

**Log out and back in** after the `usermod` — group changes only apply to new
logins. (Why: terminals never report key *releases*, so hold-to-talk reads the
keyboard device directly via evdev, which requires membership in the `input`
group. No root needed.)

### 2. A local LLM server

**Option A — Ollama** (default, fully terminal-driven):

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen2.5:7b-instruct       # the default model (~4.5 GB)
```

**Option B — an LLM app you already run** (Jan, LM Studio, llama.cpp, vLLM, …):
no extra software needed. Enable the app's local API server, load a model, and
point `config.toml` at it — see
[Using Jan, LM Studio, or another LLM app](#using-jan-lm-studio-or-another-llm-app).

### 3. This project

The easiest way is from [PyPI](https://pypi.org/project/ttrpg-npc/) — the
package is `ttrpg-npc`, the installed command is `npc`:

```bash
uv tool install ttrpg-npc             # or: pipx install ttrpg-npc
uv tool install "ttrpg-npc[cuda]"     # with NVIDIA GPU (adds CUDA libs for whisper)
```

That puts `npc` on your PATH (`~/.local/bin`); the `uv run` prefix in the
examples below becomes optional.

> **Install fails with `fatal error: Python.h: No such file or directory`?**
> The `evdev` dependency ships no prebuilt wheels, so it compiles a small C
> extension against whichever Python your installer picked — that Python needs
> its dev headers. Either install them (`sudo apt install python3-dev
> build-essential`, step 1 above), or have uv build against a managed Python
> that bundles its own: `uv tool install --managed-python ttrpg-npc`.

**Or from source**, if you want to hack on it:

```bash
git clone https://github.com/seetee/npc.git
cd npc
uv sync                               # CPU-only
uv sync --extra cuda                  # with NVIDIA GPU
```

After a source install there is no bare `npc` command — the program lives
inside the project's virtualenv, so you invoke it through uv, from the
project directory:

```bash
uv run npc <command> …
```

To get a direct `npc` command from a clone too, install it as an editable uv
tool: `uv tool install --editable ~/path/to/npc` — it then always runs the
current code in your clone; pulling new commits is enough, no reinstall.

### 4. Check everything

```bash
uv run npc init campaigns/mygame      # scaffold a campaign
uv run npc doctor campaigns/mygame    # verify + download speech models
```

`doctor` checks every link in the chain — Ollama running, model pulled,
whisper cached, Piper voice downloaded, audio devices present, input
permissions — and prints a copy-pasteable fix for anything that fails. Run it
until everything says `PASS`. The whisper model and Piper voice download
automatically on the first run; after that you're fully offline.

`npc doctor --fix` goes one step further and offers to run the safe fixes for
you (pull the Ollama model, download the Piper voice) after a `[y/N]` prompt.
Anything needing sudo stays copy-paste-only.

## Creating your NPC

`npc init campaigns/mygame` walks you through it: press Enter to start with
the ready-to-play example NPC (Vess), or type a name and a one-line concept
to get a personalized skeleton to flesh out. Either way you end up with a
campaign directory of plain markdown — edit freely, no special syntax:

```
campaigns/mygame/
├── character.md    # WHO the NPC is: personality, speech style, knowledge,
│                   # secrets, hard rules. First "# heading" = the NPC's name.
├── adventure.md    # your campaign background, from the GM's point of view
├── secrets.md      # GM-gated clues: the NPC asks YOU before revealing these
├── lore/           # reference docs (.txt/.md/.pdf) the NPC knows deeply
├── logbook.md      # session summaries — written by the LLM, editable by you
├── config.toml     # models, voice, hotkey (all optional, defaults included)
└── sessions/       # raw per-session transcripts, appended turn by turn
```

The scaffolded `character.md` contains a complete example NPC (Vess, a wary
Vault Priest) showing the intended level of detail. The **Secrets** and **Hard
rules** sections are where you control what the NPC will and won't reveal.

### Gated secrets: the NPC checks with you first

`character.md` secrets are *soft* — the NPC knows them and is told to hold
back, but a persistent table can pry them loose. Clues in `secrets.md` are
*locked*: the NPC is shown only a one-line hint per secret, never the content,
so it **cannot** leak details it has never been given — no matter how hard the
players push.

When the conversation touches a locked topic, the NPC stalls in character
("Hm. Give me a moment…") and a prompt appears at your `gm>` console:

```
⚑ Vess asks to reveal (teleporter-key) — the location of a working teleporter key
   player: "What's hidden under the altar?"
   → /yes [note] · /no [note] — stays pending until you decide
```

Take your time — call for a die roll, check your notes; the request stays
pending while play continues. Then answer, optionally steering the reveal:
`/yes but only vaguely, she's still frightened` or `/no she lies and blames
the raiders`. On `/yes` the secret's full text is handed to the NPC and it
shares the clue in its own words; on `/no` the topic stays locked for the rest
of the session; `/later` just dismisses the request (for a misfire, or "not
yet") so it can come up again. `/secrets` lists each secret's status, and
`/reveal <id>` unlocks one proactively (the NPC brings it up at the next
natural moment).
Reveals are written back into `secrets.md`, so they persist across sessions.

Each secret has a `mode`: `hesitate` (default — the NPC pauses audibly, which
players *will* notice) or `deflect` (it brushes the question off as if it knew
nothing, and can "change its mind" if you approve). Write each secret's
one-line `hint:` in the words players are likely to use — the hint is the only
thing the NPC can match a question against, since it never sees the secret
itself. The scaffolded `secrets.md` documents the format; multi-NPC campaigns
use one file per character in `secrets/<name>.md`. Nothing about a secret —
not even its id — is ever sent to the table overlay.

### Encyclopedic knowledge (lore files)

Give an NPC deep, reliable knowledge of a topic by dropping reference
documents into `lore/` — `.txt`, `.md`, or `.pdf`. The contents are injected
into the NPC's prompt as established fact: it relies on them when answering
and admits ignorance in character when they don't cover something. Files at
the root of `lore/` belong to the campaign's `character.md` NPC; in multi-NPC
campaigns each character reads `lore/<name>/` — knowledge is as strictly
per-NPC as memory is.

Mind the context window: documents share the prompt with everything else, so
attaching more than a page or two means raising `num_ctx` under `[llm]` in
`config.toml` (e.g. `num_ctx = 16384` — roughly 8–10k words of lore on a
12 GB GPU with a 7B model). Don't guess: `npc doctor` measures your actual
prompt and prints the value to set, and the session warns you once if the
prompt outgrows the window. PDFs are extracted as text — fine for prose,
unreliable for two-column rulebooks and tables; doctor flags PDFs that
extract suspiciously little (scanned pages), and converting to `.txt` is
always the safer choice.

### Multiple NPCs

Drop more character sheets into a `characters/` directory — each `.md` file
is an NPC, named by its first `# heading` — and switch at the table with
`/npc <name>` (`/npc` alone lists everyone). Each NPC keeps **separate
memory**: their own conversation, their own standing GM notes, and their own
logbook (`logbooks/<file>.md`), so a secret told to one NPC never reaches
another — not even across sessions. Give each their own voice in
`config.toml`:

```toml
[tts.voices]
korval = "en_GB-northern_english_male-medium"   # characters/korval.md
```

`npc doctor --fix` offers to download any mapped voice that's missing. The
original `character.md` keeps working unchanged alongside (or instead of)
`characters/`.

### Example campaigns

Two complete campaigns ship in [`examples/`](examples/) — play them as-is, or
copy one as a starting point:

- **[The Rusty Lantern](examples/rusty-lantern/)** — a classic-fantasy inn.
  Mera Vex knows everyone on the Saltmarsh road, waters the ale of the rude,
  and holds two gated secrets about the old innkeeper's death.
  `uv run npc run examples/rusty-lantern`
- **[The Glass Monolith](examples/glass-monolith/)** — a far-future
  multi-NPC campaign: the wary priest Vess (with lore and secrets) and Tokk
  the scavenger, showing `/npc` switching and per-character voices.
  `uv run npc run examples/glass-monolith`

## Playing a session

```bash
uv run npc run campaigns/mygame
```

| At the `gm>` prompt | Effect |
|---|---|
| **hold spacebar**, speak, release | player speaks to the NPC; it answers out loud |
| press while the NPC is talking | interrupts the reply and starts recording (walkie-talkie style) |
| type anything | out-of-character instruction to the LLM |
| `/say Have you seen the raiders?` | typed in-character player line (no mic needed) |
| `/npc korval` | switch which NPC you're talking to (`/npc` lists them) |
| `/yes only the rough direction` / `/no` | answer a pending secret-reveal request, with an optional steer |
| `/later` | dismiss a reveal request without deciding (it can come up again) |
| `/secrets` | list the active NPC's gated secrets and their status |
| `/reveal teleporter-key` | unlock a secret without waiting to be asked |
| `/save` | write the session summary to the logbook now |
| `/reload` | re-read `character.md`, `adventure.md`, `config.toml` |
| `/status` | current state, model, session number |
| `/end` | summarize the session into the logbook and exit |
| `/quit` | exit without saving a summary |

Ending with `/end` is what gives the NPC memory: the LLM distills the session's
transcript into a dated logbook entry (location, the NPC's attitude, highlights,
open threads), and the most recent entries are fed back to it next session. The
logbook also auto-checkpoints every 20 player turns, so a crash costs you
nothing.

Prefer tapping to holding? Set `mode = "tap"` under `[hotkey]` in
`config.toml`: tap once to start talking and a pause (1.2 s of silence by
default, tunable via `[stt] vad_silence_seconds`) sends the recording; a
second tap sends it immediately.

Two optional flags on `npc run`:

- `--timings` prints per-stage latency after each reply
  (`[turn 4.9s: stt 0.8s | llm 2.1s (first token 0.31s) | speak 3.2s]`);
  `/status` always shows the last/average turn time.
- `--overlay` (or `[overlay] enabled = true` in `config.toml`) serves a live
  table display at `http://127.0.0.1:8765` — add it as an OBS browser source
  or open it in a browser on the same machine (e.g. a second monitor facing
  the players). It shows the NPC's name, a state light, the last player line,
  and the reply streaming in as it's spoken.

### Overlay on a tablet (LAN)

By default the overlay listens on this machine only. To put it on a tablet or
phone on the table's Wi-Fi, opt in explicitly in `config.toml`:

```toml
[overlay]
enabled = true
listen = "0.0.0.0"     # or your machine's LAN IP
```

`npc run` then prints the exact URL to type on the tablet, plus a warning.
Know what you're opening: the stream is **unauthenticated and unencrypted**,
so anyone on that network can watch it. In LAN mode only play events are
broadcast — the player's line, the NPC's reply, state changes. GM notes,
`/status`, errors, and everything about secrets (even their existence) never
leave the machine, on any setting.

> **Tell your table:** everything said to the NPC is transcribed and stored
> locally in `sessions/`, and summarized into the logbook. It never leaves the
> machine — but recording people's words is still something your players should
> know about and be okay with.

## Choosing and changing models

`config.toml` is scaffolded with every setting present as a comment showing
its default:

```toml
[llm]
# backend = "ollama"                # or "openai" for Jan, LM Studio, llama.cpp, …
# model = "qwen2.5:7b-instruct"     # Ollama tag, or the model name your app lists
# host = "http://localhost:11434"
[stt]
# model = "small"                   # whisper size: tiny/base/small/medium/large-v3
# language = "auto"                 # auto-detects Swedish and English
[tts]
# voice = "en_GB-alba-medium"       # Piper voice (Alba, British English)
```

Uncomment and edit to change. **The LLM model can be swapped mid-session**:
edit `config.toml`, type `/reload`, and the next reply comes from the new model
(handy if an NPC turns out to need more brainpower — or less). Whisper and
Piper models are loaded into memory at startup, so changing those takes effect
on the next `npc run`; `/reload` will tell you when a restart is needed.

Rough model guidance:

| Hardware | LLM | Whisper |
|---|---|---|
| CPU only | `qwen2.5:3b-instruct`, `llama3.2:3b` | `base` or `small` |
| 8 GB VRAM | `qwen2.5:7b-instruct` (default), `llama3.1:8b` | `small` |
| 12+ GB VRAM | `qwen2.5:14b-instruct`, `mistral-nemo` | `small` or `medium` |

### Using Jan, LM Studio, or another LLM app

If a machine already runs an LLM app, `npc` can use it instead of Ollama —
anything that serves the standard OpenAI-compatible API works. In the app,
enable its local API server and note the model name it exposes, then:

```toml
[llm]
backend = "openai"                     # aliases: jan, lmstudio, llamacpp, vllm
host = "http://localhost:1337"         # Jan's default; LM Studio uses :1234
model = "qwen2.5-7b-instruct"          # exactly as the app lists it
```

(`/v1` is appended automatically if you leave it off.) `npc doctor` will show
which models the server actually offers if the configured name doesn't match.
The mid-session `/reload` model swap works the same as with Ollama.

If the server wants an API key, set `api_key` under `[llm]` — or better,
`export NPC_LLM_API_KEY=…`, which overrides the file so the key never sits in
`config.toml` in plaintext.

## Languages

Players can speak **Swedish or English** — whisper auto-detects the language
per utterance. The NPC always *answers* in English with the Alba voice; this is
enforced in its instructions, so a Swedish question gets an English answer.
Pin `stt.language = "sv"` in `config.toml` if auto-detection ever guesses wrong.

## Using a hardware button

Any USB device that emits key events works — a foot pedal, a macro pad, a
Shuttle controller. Find it and pin it:

```bash
ls /dev/input/by-id/
```

```toml
[hotkey]
key = "KEY_F13"                                  # whatever your button sends
device = "/dev/input/by-id/usb-...-event-kbd"    # pin this exact device
grab = true                                      # exclusive: presses never leak to the terminal
```

Only enable `grab` for a *dedicated* button — grabbing your actual keyboard
would swallow all your typing. Without a dedicated button, if holding spacebar
leaves stray spaces annoying you, switch `key` to `KEY_RIGHTCTRL` or `KEY_F12`.

## Development

```bash
uv sync --group dev
uv run pytest                  # unit tests — no audio hardware or Ollama needed
uv run pytest -m integration   # real whisper/piper/ollama smoke tests (auto-skip)
uv run ruff check .

uv run npc say campaigns/mygame "test"            # debug: TTS only
uv run npc transcribe campaigns/mygame clip.wav   # debug: STT only
```

The full design — pipeline, threads, the event bus, and the five guarantees
(offline, per-NPC isolation, unleakable secrets, English lock, crash-safe
writes) — is written up in [ARCHITECTURE.md](ARCHITECTURE.md). Every stage
sits behind a Protocol, so the whole loop is tested with fakes: no GPU,
microphone, or LLM server needed for the test suite. See
[CONTRIBUTING.md](CONTRIBUTING.md) for the workflow (including the prompt
probe scripts) and [CHANGELOG.md](CHANGELOG.md) for release history.

## License

[AGPL-3.0-or-later](LICENSE). Piper is GPL-3.0; whisper, Ollama and the models
carry their own licenses.
