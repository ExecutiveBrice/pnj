"""Terminal startup without a supported global hotkey must keep mobile STT available."""

import sys

import pytest
from test_app_pipeline import FakeLLM, FakeTranscriber

from npc import cli
from npc.doctor import CheckResult


@pytest.mark.parametrize("platform", ["win32", "darwin"])
@pytest.mark.parametrize("mobile_enabled", [False, True])
def test_skipped_hotkey_keeps_local_recording_off_and_mobile_stt_available(
        campaign, monkeypatch, capsys, platform, mobile_enabled):
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr("npc.doctor.run_checks", lambda *a, **kw: [
        CheckResult("Push-to-talk (KEY_SPACE)", True, skipped=True),
        CheckResult("Piper voice", False)])
    monkeypatch.setattr("npc.llm.make_llm_client", lambda config: FakeLLM())
    transcriber = FakeTranscriber()
    loaded = []

    def load_transcriber(*args):
        loaded.append(True)
        return transcriber

    monkeypatch.setattr("npc.stt.WhisperTranscriber", load_transcriber)
    monkeypatch.setattr("npc.audio.recorder.PushToTalkRecorder",
                        lambda: pytest.fail("unsupported local recording must stay off"))
    monkeypatch.setattr("npc.mobile_server.MobileServer.start", lambda self: None)

    def repl(app, config, **kwargs):
        assert app.recorder is None
        assert app.transcriber is (transcriber if mobile_enabled else None)
        assert kwargs == {"ptt_enabled": False}
        return "quit"

    monkeypatch.setattr(cli, "_run_repl", repl)
    args = ["run", str(campaign)] + (["--mobile"] if mobile_enabled else [])
    assert cli.main(args) == 0
    assert loaded == ([True] if mobile_enabled else [])
    output = capsys.readouterr().out
    assert "[SKIP] Push-to-talk" in output
    assert "Local microphone: use `npc gui`" in output
    assert "! local voice input disabled" not in output
