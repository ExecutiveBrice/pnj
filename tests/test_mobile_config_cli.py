"""Mobile configuration, CLI wiring and opt-in behavior with fake dependencies."""

import json

import pytest
from websockets.sync.client import connect

from npc import cli, doctor, llm
from npc.config import ConfigError, load_config
from npc.doctor import CheckResult
from npc.mobile_server import MobileServer

from test_app_pipeline import FakeLLM, FakeTranscriber
from test_tts import RecordingPlayer, StubPiperSpeaker


@pytest.mark.parametrize("failure", ["Audio input", "Audio subsystem", "Push-to-talk"])
@pytest.mark.parametrize("mobile_enabled", [False, True])
def test_phone_transcription_does_not_require_pc_audio_or_hotkey(
        campaign, monkeypatch, failure, mobile_enabled):
    monkeypatch.setattr(doctor, "run_checks", lambda *a, **k: [
        CheckResult(failure, False), CheckResult("Piper voice", False)])
    monkeypatch.setattr(doctor, "print_report", lambda checks: True)
    monkeypatch.setattr(llm, "make_llm_client", lambda config: FakeLLM())
    transcriber = FakeTranscriber()
    monkeypatch.setattr("npc.stt.WhisperTranscriber", lambda *a: transcriber)

    class TestServer(MobileServer):
        def start(self):
            pass

    monkeypatch.setattr("npc.mobile_server.MobileServer", TestServer)

    def repl(app, config, **kwargs):
        assert app.recorder is None
        assert app.transcriber is (transcriber if mobile_enabled else None)
        return "quit"

    monkeypatch.setattr(cli, "_run_repl", repl)
    args = ["run", str(campaign)] + (["--mobile"] if mobile_enabled else [])
    assert cli.main(args) == 0


def test_mobile_defaults_and_lan_opt_in(tmp_path):
    config = load_config(tmp_path)
    assert not config.mobile.enabled
    assert config.mobile.listen == "127.0.0.1"
    assert config.mobile.port == 8766
    assert config.mobile.local_playback is True
    assert config.mobile.secret_approval is True
    (tmp_path / "config.toml").write_text(
        '[mobile]\nenabled = true\nlisten = "0.0.0.0"\nport = 8767\nlocal_playback = false\n')
    mobile = load_config(tmp_path).mobile
    assert (mobile.enabled, mobile.listen, mobile.port) == (True, "0.0.0.0", 8767)
    assert mobile.local_playback is False


@pytest.mark.parametrize("assignment", [
    'enabled = "false"', 'listen = " "', 'listen = 4',
    'port = 0', 'port = 65536', 'port = true', 'port = "8766"',
    'local_playback = "false"', 'local_playback = 0',
    'secret_approval = "false"', 'secret_approval = 0',
])
def test_mobile_config_rejects_invalid_types_and_values(tmp_path, assignment):
    (tmp_path / "config.toml").write_text(f"[mobile]\n{assignment}\n")
    with pytest.raises(ConfigError, match="mobile"):
        load_config(tmp_path)


def test_mobile_announcement_and_ipv6():
    assert cli.mobile_announcement("127.0.0.1", 8766) == [
        "Mobile server: ws://127.0.0.1:8766/ws",
        "Studio voix et histoires : http://127.0.0.1:8766/"]
    assert cli.mobile_announcement("::1", 8766) == ["Mobile server: ws://[::1]:8766/ws",
                                                   "Studio voix et histoires : http://[::1]:8766/"]
    lan = cli.mobile_announcement("192.168.1.20", 8766)
    assert lan[0] == "Mobile server: ws://192.168.1.20:8766/ws"
    assert "anyone on this network can control" in lan[2]


@pytest.mark.parametrize("mode", ["enabled", "disabled", "failure"])
def test_client_only_audio_routes_default_and_switched_voices(campaign, monkeypatch, mode):
    (campaign / "config.toml").write_text(
        f'[mobile]\nenabled = {str(mode != "disabled").lower()}\nlocal_playback = false\n')
    monkeypatch.setattr(doctor, "run_checks", lambda *a, **k: [
        CheckResult("Whisper model", False)])
    monkeypatch.setattr(doctor, "print_report", lambda checks: True)
    monkeypatch.setattr(llm, "make_llm_client", lambda config: FakeLLM())
    speakers, published = [], []

    def make_speaker(path, player=None, *, on_audio=None, local_playback=True):
        speaker = StubPiperSpeaker(RecordingPlayer())
        speaker.on_audio = on_audio
        speaker.local_playback = local_playback
        speakers.append(speaker)
        return speaker

    class TestServer(MobileServer):
        def start(self):
            if mode == "failure":
                raise OSError("port busy")

        def publish_audio(self, samples, rate):
            published.append((len(samples), rate))

    monkeypatch.setattr("npc.tts.PiperSpeaker", make_speaker)
    monkeypatch.setattr("npc.mobile_server.MobileServer", TestServer)

    def repl(app, config, **kwargs):
        app._queue.join()  # the opening uses the configured audio routing too
        app.speaker.say("Hello")
        switched = app._make_speaker(config.tts.voice_path_for("other-voice"))
        switched.say("World")
        return "quit"

    monkeypatch.setattr(cli, "_run_repl", repl)
    assert cli.main(["run", str(campaign)]) == 0
    assert len(speakers) == 2
    assert speakers[0]._player.played == ([] if mode == "enabled" else [20, 5])
    assert speakers[1]._player.played == ([] if mode == "enabled" else [5])
    assert published == ([(20, 22050), (5, 22050), (5, 22050)] if mode == "enabled" else [])


@pytest.mark.parametrize("mode", ["default", "flag", "config", "failure", "repl_failure"])
def test_run_mobile_is_optional_and_stops_on_exit(campaign, monkeypatch, mode, capsys):
    monkeypatch.setattr(doctor, "run_checks", lambda *a, **k: [
        CheckResult("Whisper model", False), CheckResult("Piper voice", False)])
    monkeypatch.setattr(doctor, "print_report", lambda checks: True)
    monkeypatch.setattr(llm, "make_llm_client", lambda config: FakeLLM())
    started = []

    class TestServer(MobileServer):
        def __init__(self, app, port, listen):
            super().__init__(app, port=0, listen=listen)
            started.append(self)

        def start(self):
            if mode == "failure":
                raise OSError("port busy")
            super().start()

    monkeypatch.setattr("npc.mobile_server.MobileServer", TestServer)
    if mode == "config":
        (campaign / "config.toml").write_text("[mobile]\nenabled = true\n")

    def repl(app, config, **kwargs):
        if mode == "repl_failure":
            raise RuntimeError("REPL failed")
        if mode in ("flag", "config"):
            with connect(f"ws://127.0.0.1:{started[0].port}/ws") as ws:
                assert json.loads(ws.recv(timeout=2))["type"] == "hello"
                ws.send(json.dumps({"type": "say", "text": "Hello"}))
                for _ in range(10):
                    if json.loads(ws.recv(timeout=2))["type"] == "npc_replied":
                        break
                else:
                    pytest.fail("CLI did not wire app events to the mobile server")
        return "quit"

    monkeypatch.setattr(cli, "_run_repl", repl)
    args = ["run", str(campaign)] + ([] if mode in ("default", "config") else ["--mobile"])
    if mode == "repl_failure":
        with pytest.raises(RuntimeError, match="REPL failed"):
            cli.main(args)
    else:
        assert cli.main(args) == 0
    assert len(started) == (0 if mode == "default" else 1)
    for server in started:
        assert server._thread is None or not server._thread.is_alive()
    output = capsys.readouterr().out
    if mode == "default":
        assert "Mobile server:" not in output
    if mode == "failure":
        assert "mobile server disabled: port busy" in output
