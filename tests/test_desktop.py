"""Desktop lifecycle/protocol tests: real NPCApp, fake models and audio."""

import io
import json
import queue
import sys
from types import SimpleNamespace

import pytest
from test_app_pipeline import FakeLLM, FakeRecorder, FakeSpeaker, FakeTranscriber

from npc.app import NPCApp
from npc.desktop import DesktopProcess
from npc.desktop_worker import build_app, prepare_models, run_session
from npc.doctor import CheckResult
from npc.events import NpcReplied


def commands(*items):
    return io.StringIO("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in items))


def test_desktop_dialogue_then_summary(config):
    llm = FakeLLM()
    events, messages = [], []
    app = NPCApp(config, llm=llm, on_event=events.append)
    result = run_session(app, commands(
        {"action": "line", "text": "/say Où sommes-nous ?"},
        {"action": "shutdown", "summarize": True},
    ), lambda **msg: messages.append(msg))
    assert result == 0
    assert messages[0]["kind"] == "ready"
    assert messages[0]["microphone"] is False
    assert messages[-1] == {"kind": "stopping"}
    assert any(isinstance(event, NpcReplied) for event in events)
    assert "Où sommes-nous ?" in app.transcript.read()
    assert len(llm.summarize_calls) == 1
    assert not app._worker.is_alive()


def test_desktop_recording_does_not_use_evdev(config):
    llm = FakeLLM()
    recorder = FakeRecorder()
    app = NPCApp(config, llm=llm, recorder=recorder, transcriber=FakeTranscriber())
    run_session(app, commands(
        {"action": "record"}, {"action": "release"},
        {"action": "shutdown", "summarize": False},
    ), lambda **msg: None)
    assert len(llm.calls) == 1
    assert not llm.summarize_calls


@pytest.mark.parametrize("action", ["/quit", "/end"])
def test_desktop_honors_exit_commands(config, action):
    llm = FakeLLM()
    app = NPCApp(config, llm=llm)
    run_session(app, commands(
        {"action": "line", "text": "/say Hello"},
        {"action": "line", "text": action},
        {"action": "line", "text": "/say This must never be processed"},
    ), lambda **msg: None)
    assert len(llm.calls) == 1
    assert bool(llm.summarize_calls) == (action == "/end")


def test_desktop_stdin_eof_releases_microphone(config):
    app = NPCApp(config, llm=FakeLLM(), recorder=FakeRecorder(),
                 transcriber=FakeTranscriber(), speaker=FakeSpeaker())
    run_session(app, commands({"action": "record"}), lambda **msg: None)
    assert not app._worker.is_alive()
    assert app.speaker.stopped == 1
    assert len(app.llm.calls) == 1


def test_desktop_rejects_bad_commands_and_keeps_session(config):
    app = NPCApp(config, llm=FakeLLM())
    messages = []
    bad = 'no json\n[]\n{"action": "line", "text": 12}\n{"action": "bad"}\n'
    run_session(app, io.StringIO(bad), lambda **msg: messages.append(msg))
    assert len([msg for msg in messages if msg["kind"] == "log"]) == 4
    assert not app._worker.is_alive()


def test_desktop_build_blocks_missing_llm(config, monkeypatch):
    monkeypatch.setattr("npc.doctor.run_checks", lambda *a, **kw: [
        CheckResult("LLM server", False, hard=True),
    ])
    with pytest.raises(RuntimeError, match="Ollama"):
        build_app(config, lambda **msg: None)


def test_desktop_build_falls_back_to_text_and_skips_hotkey(config, monkeypatch):
    calls, messages = [], []

    def checks(*args, **kwargs):
        calls.append(kwargs)
        return [CheckResult("Audio subsystem", False),
                CheckResult("Whisper model (small)", False),
                CheckResult("Piper voice (test)", False)]

    monkeypatch.setattr("npc.doctor.run_checks", checks)
    monkeypatch.setattr("npc.llm.make_llm_client", lambda config: FakeLLM())
    app = build_app(config, lambda **msg: messages.append(msg))
    assert calls == [{"check_hotkey": False}]
    assert app.recorder is app.speaker is None
    assert messages


def test_preparation_downloads_fixable_models_and_reports_failures(config, monkeypatch):
    runs = []

    def broken():
        raise RuntimeError("download unavailable")

    def checks(*a, **kwargs):
        assert kwargs == {"deep": True, "check_hotkey": False}
        return [CheckResult("model", False, fixer=lambda: runs.append("model")),
                CheckResult("voice", False, fixer=broken)]

    messages = []
    monkeypatch.setattr("npc.doctor.run_checks", checks)
    assert prepare_models(config, lambda **msg: messages.append(msg)) == 1
    assert runs == ["model"]
    assert any("download unavailable" in msg.get("text", "") for msg in messages)


def test_bridge_preserves_unicode_spaces_and_uses_same_python(tmp_path, monkeypatch):
    launched = []
    import threading

    class Process:
        def __init__(self, command, **options):
            launched.append((command, options))
            self.stdin = io.StringIO()
            self.stdout = io.StringIO()

        def poll(self):
            return None

    # Don't start a reader: inspect a still-running child's stdin.
    monkeypatch.setattr("npc.desktop.subprocess.Popen", Process)
    monkeypatch.setattr(threading.Thread, "start", lambda self: None)
    bridge = DesktopProcess()
    campaign = tmp_path / "Campagne été et dragons"
    bridge.start(campaign, microphone=False)
    command, options = launched[0]
    assert command[0] == sys.executable
    assert str(campaign.resolve()) in command
    assert "--no-microphone" in command
    assert "shell" not in options
    bridge.send("line", text="/say Étrange château !")
    assert json.loads(bridge.process.stdin.getvalue())["text"] == "/say Étrange château !"
    with pytest.raises(RuntimeError):
        bridge.start(campaign)


def test_bridge_reads_logs_protocol_and_exit_in_order():
    bridge = DesktopProcess()
    process = SimpleNamespace(
        stdout=io.StringIO('library progress\n[1]\n{"kind":"ready","microphone":false}\n'),
        stdin=io.StringIO(), wait=lambda: 7,
    )
    bridge._read(process)
    messages = []
    while not bridge.messages.empty():
        messages.append(bridge.messages.get_nowait())
    assert [msg["kind"] for msg in messages] == ["log", "log", "ready", "exit"]
    assert messages[-1]["code"] == 7
    assert process.stdout.closed and process.stdin.closed


def test_non_linux_cli_hotkey_fails_cleanly(monkeypatch):
    from npc.hotkey import HotkeyUnavailable, keycode_from_name

    monkeypatch.setattr("npc.hotkey.sys.platform", "win32")
    with pytest.raises(HotkeyUnavailable, match="npc gui"):
        keycode_from_name("KEY_SPACE")


def test_gui_command_dispatches_without_loading_tk(monkeypatch):
    from npc.cli import main

    called = []
    monkeypatch.setattr("npc.gui.main", lambda campaign: called.append(campaign) or 0)
    assert main(["gui", "my campaign"]) == 0
    assert called == ["my campaign"]


def test_bridge_real_worker_reports_bad_config_and_exits(tmp_path):
    # Real subprocess/protocol smoke: no models, devices or network needed.
    (tmp_path / "config.toml").write_text("[broken", encoding="utf-8")
    bridge = DesktopProcess()
    bridge.start(tmp_path)
    received = []
    try:
        while True:
            msg = bridge.messages.get(timeout=15)
            received.append(msg)
            if msg["kind"] == "exit":
                break
    except queue.Empty:
        bridge.terminate()
        pytest.fail("worker did not exit")
    assert any(msg["kind"] == "error" and "config.toml" in msg["text"] for msg in received)
    assert received[-1]["code"] == 1
    bridge.acknowledge_exit()
    assert bridge.process is None
