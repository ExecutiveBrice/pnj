"""Exercise early TCP disconnects against both real localhost servers."""

import json
import logging
import socket

import pytest
from websockets.sync.client import connect

from npc.app import NPCApp
from npc.mobile_server import MobileServer
from npc.overlay import OverlayServer

from test_app_pipeline import FakeLLM, FakeSpeaker


@pytest.fixture(params=["overlay", "mobile"])
def server(request, config):
    app = None
    if request.param == "overlay":
        server = OverlayServer(port=0)
    else:
        app = NPCApp(config, llm=FakeLLM(), speaker=FakeSpeaker())
        app.start()
        server = MobileServer(app, port=0)
    server.start()
    try:
        yield server
    finally:
        server.stop()
        if app is not None:
            app.shutdown(summarize=False)


def disconnect(server, request):
    with socket.create_connection(("127.0.0.1", server.port), timeout=2) as client:
        if request:
            client.sendall(request)
        client.shutdown(socket.SHUT_WR)
        assert client.recv(4096) == b""


def test_empty_connection_is_debug_and_server_remains_available(server, caplog):
    caplog.set_level(logging.DEBUG, logger="npc.websocket_logging")
    disconnect(server, b"")

    with connect(f"ws://127.0.0.1:{server.port}/ws") as client:
        assert json.loads(client.recv(timeout=2))["type"].lower() == "hello"
    # Shutdown waits for all handshake handlers, including their logging.
    server.stop()
    errors = [r for r in caplog.records if r.getMessage() == "opening handshake failed"]
    assert len(errors) == 1
    assert errors[0].levelno == logging.DEBUG
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)


def test_empty_connection_is_quiet_at_normal_log_level(server, caplog):
    caplog.set_level(logging.INFO, logger="npc.websocket_logging")
    disconnect(server, b"")
    server.stop()
    assert not any(r.getMessage() == "opening handshake failed" for r in caplog.records)


@pytest.mark.parametrize("payload", [
    b"GET /ws",
    b"GET /ws HTTP/1.1\r\n",
    b"not an HTTP request\r\n\r\n",
])
def test_incomplete_and_invalid_requests_remain_errors(server, caplog, payload):
    caplog.set_level(logging.DEBUG, logger="npc.websocket_logging")
    disconnect(server, payload)
    server.stop()
    errors = [r for r in caplog.records if r.getMessage() == "opening handshake failed"]
    assert len(errors) == 1
    assert errors[0].levelno == logging.ERROR
    assert errors[0].exc_info is not None
