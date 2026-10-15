"""Mobile configuration, CLI wiring and opt-in behavior with fake dependencies."""

import json

import pytest
from websockets.sync.client import connect

from npc import cli, doctor, llm
from npc.config import ConfigError, load_config
from npc.doctor import CheckResult
from npc.mobile_server import MobileServer

from test_app_pipeline import FakeLLM


def test_mobile_defaults_and_lan_opt_in(tmp_path):
    config = load_config(tmp_path)
    assert not config.mobile.enabled
    assert config.mobile.listen == "127.0.0.1"
    assert config.mobile.port == 8766
    (tmp_path / "config.toml").write_text(
        '[mobile]\nenabled = true\nlisten = "0.0.0.0"\nport = 8767\n')
    mobile = load_config(tmp_path).mobile
    assert (mobile.enabled, mobile.listen, mobile.port) == (True, "0.0.0.0", 8767)


@pytest.mark.parametrize("assignment", [
    'enabled = "false"', 'listen = " "', 'listen = 4',
    'port = 0', 'port = 65536', 'port = true', 'port = "8766"',
])
def test_mobile_config_rejects_invalid_types_and_values(tmp_path, assignment):
    (tmp_path / "config.toml").write_text(f"[mobile]\n{assignment}\n")
    with pytest.raises(ConfigError, match="mobile"):
        load_config(tmp_path)


def test_mobile_announcement_and_ipv6():
    assert cli.mobile_announcement("127.0.0.1", 8766) == [
        "Mobile server: ws://127.0.0.1:8766/ws"]
    assert cli.mobile_announcement("::1", 8766) == ["Mobile server: ws://[::1]:8766/ws"]
    lan = cli.mobile_announcement("192.168.1.20", 8766)
    assert lan[0] == "Mobile server: ws://192.168.1.20:8766/ws"
    assert "anyone on this network can control" in lan[1]


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
