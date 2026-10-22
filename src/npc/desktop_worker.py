"""Private JSON-lines engine for the desktop interface (no Tk or evdev)."""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import threading
from pathlib import Path

from .config import load_config
from .events import State, StateChanged, format_event


def prepare_models(config, emit) -> int:
    from .doctor import run_checks

    emit(kind="log", text="Vérification et téléchargement des modèles…")
    checks = run_checks(config, deep=True, check_hotkey=False)
    for check in checks:
        if not check.ok and check.fixer is not None:
            emit(kind="log", text=f"Préparation : {check.fix_label or check.name}")
            try:
                check.fixer()
            except Exception as exc:
                emit(kind="log", text=f"Échec ({check.name}) : {exc}")
    checks = run_checks(config, deep=True, check_hotkey=False)
    report_checks(checks, emit)
    return 0 if all(check.ok for check in checks) else 1


def report_checks(checks, emit):
    for check in checks:
        label = "OK" if check.ok else "À vérifier"
        emit(kind="log", text=f"{label} — {check.name} : {check.detail}")
        if not check.ok and check.fix:
            emit(kind="log", text=check.fix)


def build_app(config, emit, *, microphone=True):
    from .app import NPCApp
    from .doctor import run_checks
    from .llm import make_llm_client
    from .roster import discover_character_files

    if not discover_character_files(config.campaign_dir):
        raise ValueError("Ce dossier ne contient aucun personnage. Créez d'abord une campagne.")
    checks = run_checks(config, check_hotkey=False)
    report_checks(checks, emit)
    if any(check.hard and not check.ok for check in checks):
        raise RuntimeError("Démarrez Ollama (ou votre serveur local), puis préparez les modèles.")
    failed = {check.name.split(" (")[0] for check in checks if not check.ok}
    transcriber = recorder = speaker = make_speaker = None
    if microphone and not {"Whisper model", "Audio input", "Audio subsystem"} & failed:
        try:
            from .audio.recorder import PushToTalkRecorder
            from .stt import WhisperTranscriber

            emit(kind="log", text="Chargement de la reconnaissance vocale…")
            transcriber = WhisperTranscriber(config.stt.model, config.stt.language,
                                             config.stt.device)
            recorder = PushToTalkRecorder()
        except Exception as exc:
            emit(kind="log", text=f"Micro indisponible, la saisie reste active : {exc}")
    if not {"Piper voice", "Pocket TTS", "Qwen3-TTS", "Audio output", "Audio subsystem"} & failed:
        try:
            from .tts import SpeakerFactory

            make_speaker = SpeakerFactory(config.tts)
            make_speaker.preload(lambda text: emit(kind="log", text=text))
            speaker = make_speaker.default()
        except Exception as exc:
            speaker = make_speaker = None
            emit(kind="log", text=f"Voix indisponible, les réponses restent affichées : {exc}")

    def on_event(event):
        text = format_event(event)
        if text:
            emit(kind="log", text=text)
        if isinstance(event, StateChanged):
            emit(kind="state", state=event.state.value)

    return NPCApp(config, llm=make_llm_client(config.llm), transcriber=transcriber,
                  recorder=recorder, speaker=speaker, make_speaker=make_speaker,
                  on_event=on_event)


def run_session(app, commands, emit) -> int:
    """Only this thread dispatches input; NPCApp serializes model work itself."""
    summarize = False
    app.start()
    mobile = None
    original_on_event = app._on_event
    if app.config.mobile.enabled:
        from .cli import mobile_announcement
        from .mobile_server import MobileServer

        try:
            mobile = MobileServer(app, app.config.mobile.port, app.config.mobile.listen)
            mobile.start()

            def on_event(event):
                original_on_event(event)
                mobile.publish(event)

            app._on_event = on_event
            factory = app._make_speaker
            if factory is not None:
                factory.set_audio_observer(mobile.publish_audio)
                factory.set_local_playback(app.config.mobile.local_playback)
            for speaker in app._speakers.values():
                speaker.on_audio = mobile.publish_audio
                speaker.local_playback = app.config.mobile.local_playback
            for text in mobile_announcement(app.config.mobile.listen, mobile.port):
                emit(kind="log", text=text)
        except Exception as exc:
            app._on_event = original_on_event
            if mobile is not None:
                mobile.stop()
            mobile = None
            emit(kind="log", text=f"Serveur mobile indisponible : {exc}")
    emit(kind="ready", microphone=app.recorder is not None, npc=app.npc_name)
    try:
        for raw in commands:
            try:
                command = json.loads(raw)
                if not isinstance(command, dict):
                    raise ValueError("commande attendue")
                action = command.get("action")
                if action == "shutdown":
                    summarize = bool(command.get("summarize", True))
                    break
                if action == "record":
                    app.on_ptt_press()
                elif action == "release":
                    app.on_ptt_release()
                elif action == "line":
                    line = command.get("text", "")
                    if not isinstance(line, str):
                        raise ValueError("texte attendu")
                    result = app.handle_line(line)
                    if result in ("end", "quit"):
                        summarize = result == "end"
                        break
                else:
                    raise ValueError("commande inconnue")
            except (ValueError, TypeError) as exc:
                emit(kind="log", text=f"Commande invalide : {exc}")
    finally:
        emit(kind="stopping")
        if app.state is State.RECORDING:
            app.on_ptt_release()
        if app.speaker is not None:
            app.speaker.stop()
        try:
            app.shutdown(summarize=summarize)
        finally:
            if mobile is not None:
                mobile.stop()
            app._on_event = original_on_event
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("campaign", type=Path)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--no-microphone", action="store_true")
    parser.add_argument("--test-voice", metavar="VOICE")
    args = parser.parse_args(argv)
    output = sys.stdout
    lock = threading.Lock()

    def emit(**message):
        with lock:
            output.write(json.dumps(message, ensure_ascii=False) + "\n")
            output.flush()

    # Third-party progress/logs cannot corrupt our JSON output or the GUI.
    with contextlib.redirect_stdout(sys.stderr):
        try:
            config = load_config(args.campaign)
            if args.test_voice:
                from .tts import SpeakerFactory

                factory = SpeakerFactory(config.tts)
                emit(kind="log", text=f"Préparation et écoute de la voix {args.test_voice}…")
                french = (config.tts.model.startswith("french") if config.tts.backend == "pocket"
                          else config.language.startswith("fr"))
                factory(config.tts.voice_resource_for(args.test_voice)).say(
                    "Bonjour, voyageurs. Installez-vous près du feu. Que puis-je faire pour vous ?"
                    if french else
                    "Welcome, travellers. Come and sit by the fire.")
                emit(kind="log", text="Écoute terminée.")
                return 0
            if args.prepare:
                return prepare_models(config, emit)
            app = build_app(config, emit, microphone=not args.no_microphone)
            return run_session(app, sys.stdin, emit)
        except Exception as exc:
            emit(kind="error", text=str(exc))
            return 1


if __name__ == "__main__":
    sys.exit(main())
