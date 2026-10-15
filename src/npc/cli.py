"""Command-line entrypoint: init / run / doctor / transcribe / say."""

from __future__ import annotations

import argparse
import sys
from importlib import resources
from pathlib import Path

from . import __version__
from .config import ConfigError, load_config

TEMPLATE_FILES = ("character.md", "adventure.md", "logbook.md", "config.toml",
                  "secrets.md")


def init_campaign(campaign_dir: Path,
                  overrides: dict[str, str] | None = None) -> list[Path]:
    """Scaffold a campaign directory from templates; never overwrites.
    `overrides` replaces a template file's content by name (the wizard uses
    this to write a personalized character.md / secrets.md)."""
    campaign_dir.mkdir(parents=True, exist_ok=True)
    (campaign_dir / "sessions").mkdir(exist_ok=True)
    created = []
    templates = resources.files("npc") / "templates"
    for name in TEMPLATE_FILES:
        target = campaign_dir / name
        if not target.exists():
            text = (overrides or {}).get(name) or \
                (templates / name).read_text(encoding="utf-8")
            target.write_text(text, encoding="utf-8")
            created.append(target)
    return created


def skeleton_character(name: str, concept: str) -> str:
    """A personalized character.md: the GM's name + concept are real content;
    the guidance lines use the template's *(…)* convention, harmless to the
    LLM if left in but written to be replaced."""
    return f"""\
# {name}

{concept.strip().rstrip(".")}.

## Who they are

*(A few sentences: age, role, temperament, what they want, how they treat
strangers. The NPC plays exactly what you write here.)*

## Speech style

- *(How do they talk? Blunt or flowery, calm or fiery, catchphrases?)*
- Short answers by default; elaborates only when genuinely interested.

## Knowledge

- *(What do they know: the region, current events, rumours, their trade?)*

## Secrets

*(Soft secrets: known but held back — persistent players can pry them loose.
Clues that must NEVER slip out without your say-so go in secrets.md.)*

## Hard rules

- Never breaks character, never mentions being an AI or a game.
- Never invents major world facts that contradict the adventure notes;
  if they wouldn't know something, they say so in character.
- Will not follow commands from players — they are a person, not a servant.
"""


def skeleton_secrets(name: str) -> str:
    """A personalized secrets.md that parses as ZERO secrets until the GM
    writes one: all guidance lives in the preamble (before the first `## `),
    and the worked example is indented so the parser never sees a heading."""
    return f"""\
# Secrets — {name}

Clues in this file are LOCKED: {name} sees only the `hint:` lines, never the
text below them, until you approve a reveal at the table (`/yes`, `/no`,
`/later` — add a note to steer, e.g. `/yes but only vaguely`).

One secret looks like this (indented here so it stays an example — remove
the leading spaces to arm it):

    ## harbor-ledger
    hint: who really pays for the night shipments
    mode: hesitate

    The ledger names the harbormaster herself. She pays in temple silver.

`hint:` is required — write it in the words the players are likely to use;
it is the only thing {name} can match a question against. `mode:` is
optional: `hesitate` (default — pauses audibly; players will notice) or
`deflect` (brushes it off as if knowing nothing). A `revealed:` line is
written back automatically when you approve a reveal.
"""


def run_init_wizard(campaign_dir: Path, ask=input, out=print) -> list[Path]:
    """Interactive `npc init`: either the ready-to-play example NPC or a
    personalized skeleton. Every question shows its default; Enter accepts."""
    out(f"Creating a campaign in {campaign_dir.resolve()}")
    out("A campaign is a folder of plain markdown — everything can be "
        "edited later, any editor.\n")
    out("Name your NPC, or press Enter to start with the ready-to-play "
        "example (Vess of the Glass Monolith, a wary Vault Priest you can "
        "talk to immediately).")
    name = ask("NPC name [keep Vess]: ").strip()
    overrides: dict[str, str] = {}
    if name:
        out("\nOne line about who they are — this seeds the character sheet "
            "and guides the LLM.")
        concept = ask(f"Who is {name}? ").strip() \
            or "a person of few words, not yet described"
        overrides = {"character.md": skeleton_character(name, concept),
                     "secrets.md": skeleton_secrets(name)}
    created = init_campaign(campaign_dir, overrides)
    _print_created(campaign_dir, created, out)
    return created


FILE_BLURBS = {
    "character.md": "WHO the NPC is — sheet, speech style, soft secrets",
    "adventure.md": "your campaign background, from the GM's point of view",
    "secrets.md": "GM-gated clues — the NPC asks YOU before revealing these",
    "logbook.md": "the NPC's memory; the LLM writes a summary each session",
    "config.toml": "models, voice, hotkey — all optional, sane defaults",
}


def _print_created(campaign_dir: Path, created: list[Path], out=print) -> None:
    if created:
        out("\nCreated:")
        for path in created:
            out(f"  {path.name:14s} {FILE_BLURBS.get(path.name, '')}")
    else:
        out("All campaign files already exist — nothing overwritten.")
    where = campaign_dir.as_posix()
    out(f"""
Next:
  1. Flesh out character.md and adventure.md — the richer, the better the play
  2. Add gated clues to secrets.md, reference docs (txt/md/pdf) to lore/
  3. uv run npc doctor --fix {where}    checks LLM, mic, voice; offers fixes
  4. uv run npc run {where}             play — /help at the gm> prompt""")


def cmd_init(args) -> int:
    campaign_dir = Path(args.campaign)
    if sys.stdin.isatty():
        run_init_wizard(campaign_dir)
    else:  # scripted/piped: keep init non-interactive and example-based
        created = init_campaign(campaign_dir)
        _print_created(campaign_dir, created)
    return 0


def cmd_doctor(args) -> int:
    from .doctor import apply_fixes, print_report, run_checks

    config = load_config(Path(args.campaign))
    print(f"Checking setup for {config.campaign_dir} …")
    checks = run_checks(config, deep=True)
    print_report(checks)
    if args.fix and not all(c.ok for c in checks):
        if not sys.stdin.isatty():
            print("--fix skipped: not an interactive terminal")
        elif apply_fixes(checks):
            print("\nRe-checking …")
            checks = run_checks(config, deep=True)
            print_report(checks)
    all_ok = all(c.ok for c in checks)
    if all_ok:
        print("\nAll good — ready to play.")
    else:
        print("\nFix the FAILs above (commands are copy-pasteable).")
        if not args.fix and any(c.fixer for c in checks if not c.ok):
            print("Or let npc do the safe ones itself: "
                  f"npc doctor --fix {args.campaign}")
    return 0 if all_ok else 1


def cmd_transcribe(args) -> int:
    import wave

    import numpy as np

    from .audio.recorder import AudioClip
    from .stt import WhisperTranscriber

    config = load_config(Path(args.campaign))
    with wave.open(args.file, "rb") as w:
        if w.getsampwidth() != 2:
            print("only 16-bit PCM wav supported", file=sys.stderr)
            return 1
        frames = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        if w.getnchannels() > 1:
            frames = frames.reshape(-1, w.getnchannels()).mean(axis=1).astype(np.int16)
        clip = AudioClip(samples=frames, sample_rate=w.getframerate())
    transcriber = WhisperTranscriber(config.stt.model, config.stt.language,
                                     config.stt.device)
    print(transcriber.transcribe(clip))
    return 0


def cmd_say(args) -> int:
    from .tts import PiperSpeaker, download_hint

    config = load_config(Path(args.campaign))
    voice_path = config.tts.voice_path
    if not voice_path.exists():
        print(f"voice not found: {voice_path}", file=sys.stderr)
        print(f"download it: {download_hint(config.tts.voice, voice_path.parent)}",
              file=sys.stderr)
        return 1
    PiperSpeaker(voice_path).say(args.text)
    return 0


def cmd_gui(args) -> int:
    from .gui import main

    return main(args.campaign)


def cmd_run(args) -> int:
    from .app import NPCApp
    from .doctor import print_report, run_checks
    from .events import NpcSwitched, TurnCompleted, format_timings, print_event
    from .llm import make_llm_client

    from .roster import discover_character_files

    config = load_config(Path(args.campaign))
    if not discover_character_files(config.campaign_dir):
        where = ("the current directory" if args.campaign == "."
                 else str(config.campaign_dir))
        print(f"{where} is not a campaign (no character.md or characters/*.md).\n"
              "Point at one (npc run campaigns/mygame) or create one: "
              "npc init campaigns/mygame", file=sys.stderr)
        return 1

    checks = run_checks(config, deep=False)
    if not print_report(checks):
        print("\nCannot start — fix the hard failures above (or run `npc doctor`).",
              file=sys.stderr)
        return 1
    failed_soft = {c.name.split(" (")[0] for c in checks if not c.ok}
    if "Audio subsystem" in failed_soft:
        failed_soft |= {"Audio input", "Audio output"}

    llm = make_llm_client(config.llm)
    mobile = None
    mobile_enabled = config.mobile.enabled or getattr(args, "mobile", False)

    def on_audio(samples, sample_rate):
        if mobile is not None:
            mobile.publish_audio(samples, sample_rate)

    transcriber = recorder = None
    if not ({"Whisper model", "Audio input", "Push-to-talk"} & failed_soft):
        from .audio.recorder import PushToTalkRecorder, VadRecorder
        from .stt import WhisperTranscriber

        print("Loading whisper model…")
        transcriber = WhisperTranscriber(config.stt.model, config.stt.language,
                                         config.stt.device)
        if config.hotkey.mode == "tap":
            recorder = VadRecorder(threshold_db=config.stt.silence_threshold_db,
                                   silence_seconds=config.stt.vad_silence_seconds,
                                   max_seconds=config.stt.vad_max_seconds)
        else:
            recorder = PushToTalkRecorder()
    else:
        print("! voice input disabled this session (see FAILs above); /say still works")

    speaker = make_speaker = None
    if "Piper voice" not in failed_soft and "Audio output" not in failed_soft:
        from .audio.player import AudioPlayer
        from .tts import PiperSpeaker

        # ONE AudioPlayer for every voice: its persistent output stream is
        # what keeps the ALSA PipeWire plugin's close bug out of sessions
        player = AudioPlayer()
        speaker = PiperSpeaker(config.tts.voice_path, player=player,
                               on_audio=on_audio if mobile_enabled else None)

        def make_speaker(voice_path, _player=player):
            return PiperSpeaker(voice_path, player=_player,
                                on_audio=on_audio if mobile_enabled else None)
    else:
        print("! spoken replies disabled this session (see FAILs above)")

    overlay = None
    lan_mode = not is_loopback(config.overlay.listen)

    def on_event(event):
        print_event(event)
        if args.timings and isinstance(event, TurnCompleted):
            print(format_timings(event))
        if mobile is not None:
            mobile.publish(event)  # MobileServer enforces its own player-only gate
        if overlay is None or type(event).dm_only:
            return  # dm_only (secret ids/hints) never reaches the websocket
        if lan_mode and not type(event).table_safe:
            return  # on the LAN only play events cross; GM console stays home
        if isinstance(event, NpcSwitched):
            # late-connecting OBS pages get the current name in Hello
            overlay.hello["npc_name"] = event.npc_name
        overlay.publish(event)

    app = NPCApp(config, llm=llm, transcriber=transcriber, recorder=recorder,
                 speaker=speaker, make_speaker=make_speaker, on_event=on_event)

    if config.overlay.enabled or args.overlay:
        from .overlay import OverlayServer

        try:
            overlay = OverlayServer(config.overlay.port,
                                    hello={"npc_name": app.npc_name,
                                           "session_no": app.session_no},
                                    listen=config.overlay.listen)
            overlay.start()
            for line in overlay_announcement(config.overlay.listen, overlay.port):
                print(line)
        except Exception as e:
            overlay = None
            print(f"! overlay disabled: {e}")

    app.start()
    if mobile_enabled:
        from .mobile_server import MobileServer

        try:
            mobile = MobileServer(app, config.mobile.port, config.mobile.listen)
            mobile.start()
            for line in mobile_announcement(config.mobile.listen, mobile.port):
                print(line)
        except Exception as e:
            if mobile is not None:
                mobile.stop()
            mobile = None
            print(f"! mobile server disabled: {e}")
    print("\n" + quickstart(app, config, voice_on=recorder is not None) + "\n")

    action = "quit"
    try:
        action = _run_repl(app, config, ptt_enabled=recorder is not None)
    finally:
        try:
            app.shutdown(summarize=(action == "end"))  # final events still broadcast
        finally:
            if mobile is not None:
                mobile.stop()
            if overlay is not None:
                overlay.stop()
    print("Farewell.")
    return 0


def is_loopback(listen: str) -> bool:
    host = listen.strip().lower()
    return host in ("localhost", "::1") or host.startswith("127.")


def lan_address(listen: str) -> str:
    """The address table devices should type: the configured IP, or (for
    0.0.0.0 / ::) this machine's outbound LAN address via the UDP-connect
    trick — no packet is actually sent."""
    if listen not in ("0.0.0.0", "::"):
        return listen
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return socket.gethostname()


def overlay_announcement(listen: str, port: int) -> list[str]:
    """The overlay lines printed at session start — loopback stays terse,
    LAN mode names the URL to type and says exactly what crosses the wire."""
    if is_loopback(listen):
        return [f"overlay: http://127.0.0.1:{port}"]
    return [
        f"overlay (LAN): http://{lan_address(listen)}:{port}   ← open on the tablet",
        "! the overlay is visible to EVERYONE on this network, unencrypted.",
        "! it broadcasts play events only — GM notes, secrets, and console",
        "! output never leave this machine.",
    ]


def mobile_announcement(listen: str, port: int) -> list[str]:
    host = lan_address(listen)
    if ":" in host:
        host = f"[{host}]"
    lines = [f"Mobile server: ws://{host}:{port}/ws"]
    if not is_loopback(listen):
        lines.append("! anyone on this network can control the NPC; "
                     "no authentication or encryption. Player events only.")
    return lines


def quickstart(app, config, voice_on: bool) -> str:
    """The lines shown when a session starts: only what applies to THIS
    campaign, each line naming the action it enables."""
    lines = [f"{app.npc_name} is listening — session {app.session_no}."]
    if voice_on:
        verb = "tap" if config.hotkey.mode == "tap" else "hold"
        how = ("tap to talk, pause (or tap again) to send"
               if config.hotkey.mode == "tap" else "release to send")
        key = config.hotkey.key.removeprefix("KEY_").lower()  # KEY_SPACE → space
        lines.append(f"  {verb} {key:12s} speak as a player — {how}")
    else:
        lines.append("  (mic off this session — /say speaks for the player)")
    lines.append("  /say <text>       typed player line — the NPC answers aloud")
    lines.append("  <text> + Enter    GM note to the NPC (\"be more hostile\")")
    if len(app.roster) > 1:
        lines.append(f"  /npc <name>       switch NPC ({len(app.roster)} in "
                     "this campaign)")
    locked = sum(len(slot.secrets.locked()) for slot in app.roster.values())
    if locked:
        clue = "clue" if locked == 1 else "clues"
        lines.append(f"  /secrets          {locked} gated {clue} loaded — "
                     "you approve every reveal")
    lore_words = sum(f.words for f in app.active.lore)
    if lore_words:
        lines.append(f"  lore              ~{lore_words:,} words of reference "
                     f"loaded for {app.npc_name}")
    lines.append("  /help             all commands · /end saves the session "
                 "on exit")
    return "\n".join(lines)


def _run_repl(app, config, ptt_enabled: bool) -> str:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.patch_stdout import patch_stdout

    session = PromptSession("gm> ")
    listener = None
    if ptt_enabled:
        try:
            listener = _start_hotkey(app, config, session)
        except Exception as e:
            print(f"! push-to-talk disabled: {e}")

    try:
        with patch_stdout():
            while True:
                try:
                    line = session.prompt()
                except (EOFError, KeyboardInterrupt):
                    return "quit"
                action = app.handle_line(line)
                if action != "ok":
                    return action
    finally:
        if listener is not None:
            listener.stop()


def _start_hotkey(app, config, session):
    """Wire evdev press/release to the app; see _ptt_callbacks for how the
    hotkey coexists with the REPL when it is an ordinary typing key."""
    from .hotkey import PTTListener, find_ptt_devices, keycode_from_name

    keycode = keycode_from_name(config.hotkey.key)
    devices = find_ptt_devices(keycode, config.hotkey.device)
    typing_key = _key_types_text(config.hotkey.key) and not config.hotkey.grab
    on_press, on_release = _ptt_callbacks(app, session, typing_key=typing_key,
                                          tap_mode=config.hotkey.mode == "tap")
    listener = PTTListener(devices, keycode, on_press, on_release,
                           grab=config.hotkey.grab)
    listener.start()
    return listener


def _key_types_text(key_name: str) -> bool:
    """True for hotkeys that also insert a character into the terminal."""
    import re

    return bool(re.fullmatch(
        r"KEY_(SPACE|TAB|[A-Z0-9]|MINUS|EQUAL|COMMA|DOT|SLASH|SEMICOLON|APOSTROPHE|GRAVE)",
        key_name))


def _ptt_callbacks(app, session, typing_key: bool, tap_mode: bool = False):
    """Press/release handlers that coexist with the REPL.

    A hotkey that is an ordinary typing key (the default spacebar) causes two
    terminal artifacts:
    - pressing it while a line is half-typed (an OOC note, /say …) is typing,
      not push-to-talk — ignore the press entirely and let the character land
      in the buffer;
    - holding it on an empty line types characters into the prompt — snapshot
      the buffer on press and restore it (and flush stdin) on release.

    In tap mode the key toggles instead of holding: first press starts the
    recording (silence ends it via the VAD recorder), a second press while
    RECORDING stops it early; key release only does the terminal cleanup.
    """
    from .events import State

    state = {"snapshot": "", "typing": False}

    def buffer_text() -> str:
        try:
            return session.app.current_buffer.text
        except Exception:
            return ""

    def on_press():
        text = buffer_text()
        if typing_key and text.strip():
            state["typing"] = True
            return
        state["typing"] = False
        state["snapshot"] = text
        if tap_mode and app.state is State.RECORDING:
            app.on_ptt_release()  # second tap = stop early
        else:
            app.on_ptt_press()

    def on_release():
        if state["typing"]:
            state["typing"] = False
            return
        if not tap_mode:
            app.on_ptt_release()
        try:
            import termios

            termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
            pt_app = session.app
            saved = state["snapshot"]

            def restore():
                from prompt_toolkit.document import Document

                pt_app.current_buffer.set_document(Document(saved, len(saved)))

            if pt_app.is_running and pt_app.loop is not None:
                pt_app.loop.call_soon_threadsafe(restore)
        except Exception:
            pass

    return on_press, on_release


def main(argv=None) -> int:
    # Swedish player lines and ⚑/🔒 micro-copy must survive any locale:
    # force UTF-8 on the terminal streams (replace, never crash a session
    # over a glyph the terminal can't show)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(
        prog="npc",
        description="Offline AI NPC voice agent for tabletop RPGs.",
    )
    parser.add_argument("--version", action="version", version=f"npc {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name, func, help_text, campaign_optional=False):
        p = sub.add_parser(name, help=help_text)
        if campaign_optional:  # `npc run` inside a campaign dir just works
            p.add_argument("campaign", nargs="?", default=".",
                           help="campaign directory (default: current directory)")
        else:
            p.add_argument("campaign", help="campaign directory")
        p.set_defaults(func=func)
        return p

    add("init", cmd_init,
        "create a campaign directory (guided — answers a couple of questions)")
    add("gui", cmd_gui, "open the desktop interface (Windows, macOS, Linux)",
        campaign_optional=True)
    p = add("run", cmd_run, "run the NPC for a play session",
            campaign_optional=True)
    p.add_argument("--timings", action="store_true",
                   help="print per-stage turn timings after each reply")
    p.add_argument("--overlay", action="store_true",
                   help="serve the OBS/table overlay (forces [overlay] enabled)")
    p.add_argument("--mobile", action="store_true",
                   help="serve the player WebSocket API (forces [mobile] enabled)")
    p = add("doctor", cmd_doctor, "check (and set up) everything the NPC needs",
            campaign_optional=True)
    p.add_argument("--fix", action="store_true",
                   help="offer to run the safe fixes (model pull, voice download)")
    p = add("transcribe", cmd_transcribe, "debug: transcribe a wav file")
    p.add_argument("file", help="16-bit PCM wav file")
    p = add("say", cmd_say, "debug: speak a line with the NPC voice")
    p.add_argument("text", help="text to speak")

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
