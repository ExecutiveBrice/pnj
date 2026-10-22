"""Startup checks with copy-pasteable fixes. `npc doctor` runs the deep
version (pre-downloads the whisper model); `npc run` runs the quick version
and refuses to start only on hard failures (Ollama + model)."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .tts import download_hint


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""
    fix: str = ""
    hard: bool = False  # hard failures prevent `npc run` from starting
    fixer: Callable[[], None] | None = None  # safe in-process fix (--fix); never sudo
    fix_label: str = ""  # human sentence for the [y/N] prompt
    skipped: bool = False  # unsupported/irrelevant checks are informational, not failures


def _whisper_cache_dir(model_size: str) -> Path:
    hub = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub"
    return hub / f"models--Systran--faster-whisper-{model_size}"


def whisper_detail(device: str) -> str:
    """Pure so the CPU nudge is testable without loading a model."""
    if device == "cpu":
        return ("cached, running on CPU — with an NVIDIA GPU, reinstall as "
                'ttrpg-npc[cuda] for much faster transcription')
    return f"cached, running on {device}"


def run_checks(config: Config, deep: bool = False, *,
               check_hotkey: bool = True) -> list[CheckResult]:
    checks: list[CheckResult] = []

    # 1. LLM server (Ollama or any OpenAI-compatible app)
    from .llm import OllamaClient, make_llm_client

    client = make_llm_client(config.llm)
    is_ollama = isinstance(client, OllamaClient)
    up = client.is_up()
    ollama_fix = ("curl -fsSL https://ollama.com/install.sh | sh   "
                  "# then: systemctl --user start ollama  (or)  sudo systemctl start ollama"
                  if sys.platform == "linux" else
                  "install Ollama from https://ollama.com/download and launch the app")
    checks.append(CheckResult(
        "LLM server", up, hard=True,
        detail=f"{config.llm.backend} at {config.llm.host}",
        fix=(ollama_fix if is_ollama else
             "start your LLM app (Jan, LM Studio, …) and enable its local API "
             "server; put its address in config.toml under [llm] host"),
    ))

    # 2. LLM model present
    if up:
        has = client.has_model()
        detail = config.llm.model
        if not has:
            others = client.available_models()
            if others:
                detail += f" (available: {', '.join(others[:5])})"
        def fix_model():
            def progress(status: str) -> None:
                print(f"\r         {status:<60}", end="", flush=True)

            try:
                client.pull_model(progress)
            finally:
                print()

        checks.append(CheckResult(
            "LLM model", has, detail=detail, hard=True,
            fix=(f"ollama pull {config.llm.model}" if is_ollama else
                 "download/load the model in your LLM app, or set [llm] model in "
                 "config.toml to one of the available names"),
            fixer=fix_model if is_ollama else None,
            fix_label=f"pull {config.llm.model} with Ollama (may be several GB)",
        ))

    # 3. Whisper model cached (offline after first download)
    cached = _whisper_cache_dir(config.stt.model).exists()
    detail = "cached" if cached else "not downloaded yet"
    if deep:
        # Loading it is the only way to learn which device it really got —
        # the CUDA→CPU fallback is silent, and on a GPU machine that is the
        # difference between 0.2 s and several seconds per turn.
        try:
            from .stt import WhisperTranscriber

            transcriber = WhisperTranscriber(config.stt.model, config.stt.language,
                                             config.stt.device)
            cached = True
            detail = whisper_detail(transcriber.device)
        except Exception as e:
            checks.append(CheckResult("Whisper model download", False, detail=str(e)))
    checks.append(CheckResult(
        f"Whisper model ({config.stt.model})", cached, detail=detail,
        fix="npc doctor   # downloads it (needs internet once)",
    ))

    # 4. Only check/download the selected speech engine.
    checks.extend(tts_checks(config, deep))
    checks.extend(npc_voice_checks(config))
    checks.extend(npc_secrets_checks(config))
    checks.extend(npc_lore_checks(config))

    # 5. Audio devices
    try:
        import sounddevice as sd

        for kind in ("input", "output"):
            try:
                dev = sd.query_devices(kind=kind)
                checks.append(CheckResult(f"Audio {kind}", True, detail=dev["name"]))
            except Exception as e:
                checks.append(CheckResult(f"Audio {kind}", False, detail=str(e)))
    except Exception as e:
        checks.append(CheckResult(
            "Audio subsystem", False, detail=str(e),
            fix=("sudo apt install libportaudio2" if sys.platform == "linux" else
                 "reinstall sounddevice and check the system audio permissions/devices"),
        ))

    # 6. Push-to-talk key readable
    if not check_hotkey:
        return checks  # desktop microphone is controlled by an on-screen button
    if sys.platform != "linux":
        checks.append(CheckResult(
            f"Push-to-talk ({config.hotkey.key})", True, skipped=True,
            detail="global hotkey is Linux-only; use `npc gui` for the desktop "
                   "recording button, /say for typed dialogue, or mobile voice input",
        ))
        return checks
    from .hotkey import HotkeyUnavailable, find_ptt_devices, keycode_from_name

    try:
        keycode = keycode_from_name(config.hotkey.key)
        devices = find_ptt_devices(keycode, config.hotkey.device)
        names = ", ".join(d.name for d in devices[:3])
        for d in devices:
            d.close()
        checks.append(CheckResult(f"Push-to-talk ({config.hotkey.key})", True, detail=names))
    except HotkeyUnavailable as e:
        checks.append(CheckResult(
            f"Push-to-talk ({config.hotkey.key})", False, detail=str(e),
            fix=("sudo usermod -aG input $USER   # then log out and back in"
                 if sys.platform == "linux" else
                 "npc gui   # use the desktop recording button"),
        ))

    return checks


def tts_checks(config: Config, deep: bool = False) -> list[CheckResult]:
    if config.tts.backend == "qwen":
        from importlib.util import find_spec

        if find_spec("qwen_tts") is None:
            return [CheckResult("Qwen3-TTS", False, detail="optional dependency missing",
                                fix="uv sync --extra qwen")]
        result = CheckResult("Qwen3-TTS", True,
                             detail=f"VoiceDesign / {config.tts.qwen_device}")
        if deep:
            try:
                from .engines.qwen import get_runtime

                runtime = get_runtime(config.tts.qwen_model, config.tts.qwen_device)
                result.detail = f"VoiceDesign loaded on {runtime.device}"
            except Exception as exc:
                result = CheckResult("Qwen3-TTS", False, detail=str(exc))
        return [result]
    if config.tts.backend == "pocket":
        from importlib.util import find_spec

        from .pocket import POCKET_VOICES

        if find_spec("pocket_tts") is None:
            return [CheckResult("Pocket TTS", False, detail="optional dependency missing",
                                fix="uv sync --extra pocket")]
        if config.tts.voice not in POCKET_VOICES:
            return [CheckResult("Pocket TTS", False, detail=f"unknown voice: {config.tts.voice}",
                                fix=f"Choose one of: {', '.join(POCKET_VOICES)}")]
        checks = [CheckResult("Pocket TTS", True,
                              detail=f"CPU / {config.tts.model}; model prepared at session startup")]
        if deep:
            try:
                from .pocket import get_runtime

                runtime = get_runtime(config.tts.model)
                runtime.prepare(config.tts.voice)
                checks[0].detail = f"CPU / {config.tts.model}; model and default voice loaded"
                for voice in sorted(set(config.tts.voices.values()) - {config.tts.voice}):
                    try:
                        runtime.prepare(voice)
                        checks.append(CheckResult(f"NPC voice ({voice})", True, detail="Pocket ready"))
                    except Exception as exc:
                        checks.append(CheckResult(f"NPC voice ({voice})", False, detail=str(exc)))
            except Exception as exc:
                checks[0] = CheckResult("Pocket TTS", False, detail=str(exc),
                                        fix="Check internet for the first model/voice download, then retry.")
        return checks

    voice_path = config.tts.voice_path

    def fix_voice():
        from piper.download_voices import download_voice

        voice_path.parent.mkdir(parents=True, exist_ok=True)
        download_voice(config.tts.voice, voice_path.parent)

    return [CheckResult(
        f"Piper voice ({config.tts.voice})", voice_path.exists(), detail=str(voice_path),
        fix=download_hint(config.tts.voice, voice_path.parent),
        fixer=fix_voice,
        fix_label=f"download the {config.tts.voice} voice (~60 MB)",
    )]


def npc_voice_checks(config: Config) -> list[CheckResult]:
    """[tts.voices] sanity: every key must name a character file stem, every
    mapped voice must exist on disk (downloadable via --fix). Named "NPC
    voice …" on purpose — cmd_run's soft-fail matching for "Piper voice"
    must not catch these, because a missing per-NPC voice only means a
    fallback to the default voice, never disabled speech."""
    from .roster import discover_character_files

    checks: list[CheckResult] = []
    stems = {ref.stem for ref in discover_character_files(config.campaign_dir)}
    for key in sorted(config.tts.voices):
        if key not in stems:
            checks.append(CheckResult(
                f"NPC voice mapping ({key})", False,
                detail=(f"no character file for stem {key!r} — have: "
                        f"{', '.join(sorted(stems)) or 'none'}"),
                fix="rename the [tts.voices] key or the character file",
            ))
    for voice in sorted(set(config.tts.voices.values()) - {config.tts.voice}):
        if config.tts.backend == "qwen":
            checks.append(CheckResult(f"NPC voice ({voice})", bool(voice.strip()),
                                      detail="Qwen VoiceDesign description"))
            continue
        if config.tts.backend == "pocket":
            from .pocket import POCKET_VOICES

            checks.append(CheckResult(f"NPC voice ({voice})", voice in POCKET_VOICES,
                                      detail="Pocket predefined voice",
                                      fix=f"Choose one of: {', '.join(POCKET_VOICES)}"))
            continue
        path = config.tts.voice_path_for(voice)

        def fix_npc_voice(voice=voice, path=path):
            from piper.download_voices import download_voice

            path.parent.mkdir(parents=True, exist_ok=True)
            download_voice(voice, path.parent)

        checks.append(CheckResult(
            f"NPC voice ({voice})", path.exists(), detail=str(path),
            fix=download_hint(voice, path.parent),
            fixer=fix_npc_voice,
            fix_label=f"download the {voice} voice (~60 MB)",
        ))
    if (config.character_file.exists()
            and (config.characters_dir / "character.md").exists()):
        checks.append(CheckResult(
            "Character files", False,
            detail="both character.md and characters/character.md exist — "
                   "the campaign-root file wins, the other is ignored",
            fix="rename characters/character.md",
        ))
    return checks


def npc_secrets_checks(config: Config) -> list[CheckResult]:
    """Parse every NPC's secrets file so format mistakes (missing hint:, bad
    mode, duplicate or invalid ids) surface here instead of silently
    disabling the secret mid-session. Soft: a broken file never blocks
    `npc run` — the app plays on without that NPC's secrets."""
    from .roster import discover_character_files
    from .session.secrets import SecretsError, SecretsSheet

    checks: list[CheckResult] = []
    for ref in discover_character_files(config.campaign_dir):
        if not ref.secrets_path.exists():
            continue
        try:
            sheet = SecretsSheet.parse(
                ref.secrets_path.read_text(encoding="utf-8"))
        except SecretsError as e:
            checks.append(CheckResult(
                f"Secrets ({ref.stem})", False,
                detail=f"{ref.secrets_path.name}: {e}",
                fix="fix the secrets file — format is described in the "
                    "template secrets.md",
            ))
            continue
        locked = len(sheet.locked())
        revealed = len(sheet.revealed())
        checks.append(CheckResult(
            f"Secrets ({ref.stem})", True,
            detail=f"{locked} locked, {revealed} revealed",
        ))
    return checks


def npc_lore_checks(config: Config) -> list[CheckResult]:
    """Lore files load cleanly AND the resulting prompt fits the context
    window — an oversized prompt is silently truncated by Ollama, which
    plays like the NPC forgetting its instructions. All soft."""
    from .roster import discover_character_files, load_slot
    from .session.lore import estimate_tokens, suggest_num_ctx
    from .session.prompt import build_system_prompt

    checks: list[CheckResult] = []
    adventure = (config.adventure_file.read_text(encoding="utf-8")
                 if config.adventure_file.exists() else "")
    lore_cache = {}
    for ref in discover_character_files(config.campaign_dir):
        slot = load_slot(ref, config, lore_cache=lore_cache)
        for error in slot.lore_errors:
            checks.append(CheckResult(
                f"Lore ({ref.stem})", False, detail=error,
                fix="fix or remove the file — .txt/.md are the most reliable",
            ))
        if slot.lore:
            words = sum(f.words for f in slot.lore)
            tokens = sum(estimate_tokens(f.text) for f in slot.lore)
            thin = [f.name for f in slot.lore
                    if f.pages and f.words / f.pages < 20]
            if thin:
                checks.append(CheckResult(
                    f"Lore ({ref.stem})", False,
                    detail=f"{', '.join(thin)}: extracted very little text — "
                           "scanned/image PDF?",
                    fix="convert it to .txt for reliable knowledge",
                ))
            else:
                checks.append(CheckResult(
                    f"Lore ({ref.stem})", True,
                    detail=f"{len(slot.lore)} file(s), ~{words:,} words "
                           f"(~{tokens:,} tokens)",
                ))
        if config.llm.backend == "ollama":
            system = build_system_prompt(
                slot.character, adventure,
                slot.logbook.tail(config.logbook_sessions_in_prompt),
                [], secrets=slot.secrets, lore=slot.lore,
                language=config.language_for(slot.stem))
            used = estimate_tokens(system) + 1500  # history + reply allowance
            budget = config.llm.num_ctx or 4096
            if used > budget:
                checks.append(CheckResult(
                    f"Context budget ({ref.stem})", False,
                    detail=f"prompt ≈ {used:,} tokens, window ~{budget:,} — "
                           "Ollama will silently truncate",
                    fix=f"set num_ctx = {suggest_num_ctx(used)} under [llm] "
                        "in config.toml (larger windows use more VRAM)",
                ))
    return checks


def apply_fixes(checks: list[CheckResult], ask=input, out=print) -> bool:
    """Interactively run the safe fixers of failed checks (`doctor --fix`).
    Returns True when anything was attempted, so the caller re-runs the
    checks. sudo-level problems (libportaudio2, the input group) never get a
    fixer — their fix strings stay copy-paste-only on purpose."""
    attempted = False
    for check in checks:
        if check.ok or check.fixer is None:
            continue
        answer = ask(f"  fix now — {check.fix_label or check.name}? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            continue
        attempted = True
        try:
            check.fixer()
            out(f"  fixed: {check.name}")
        except Exception as e:
            out(f"  fix failed for {check.name}: {e}")
    return attempted


def print_report(checks: list[CheckResult], out=print) -> bool:
    """Prints PASS/FAIL/SKIP lines; returns True if no hard check failed."""
    ok = True
    for c in checks:
        mark = "SKIP" if c.skipped else ("PASS" if c.ok else "FAIL")
        out(f"  [{mark}] {c.name}" + (f" — {c.detail}" if c.detail else ""))
        if not c.ok:
            if c.fix:
                out(f"         fix: {c.fix}")
            if c.hard:
                ok = False
    return ok
