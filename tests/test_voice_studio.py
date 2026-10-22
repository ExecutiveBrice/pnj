"""Persisted WAVs, standalone story writing and real mobile exchanges with fake inference."""

import json
import threading
import wave
from types import SimpleNamespace

import httpx
import numpy as np
import pytest
from websockets.sync.client import connect

from npc.app import NPCApp
from npc.mobile_server import MobileServer
from npc.voice_studio import StudioCancelled, VoiceStudio
from test_app_pipeline import FakeLLM


class StubStudio(VoiceStudio):
    @property
    def available(self):
        return True


@pytest.fixture
def studio(config):
    calls = []

    def synthesize(text, description, language, *, cancel):
        calls.append((text, description, language))
        return np.full(240, 123, dtype=np.int16), 24000

    studio = StubStudio(config, runtime_factory=lambda *a: SimpleNamespace(synthesize=synthesize),
                        llm_factory=lambda: FakeLLM())
    studio.calls = calls
    return studio


def request(studio, kind="voice_design", **values):
    return studio.validate({"type": kind, "title": "Le gardien", "description": "Grave et calme.",
                            "language": "French", "text": "Bonjour, voyageurs.", **values})


def test_voice_profile_reused_for_literal_story_and_persisted_across_restart(studio):
    voice = studio.run(request(studio))["item"]
    text = ("La forêt s’éveille. Les voyageurs reprennent leur chemin. " * 30).strip()
    story = studio.run(request(studio, "narrate_text", voice_id=voice["id"], text=text))["item"]
    assert story["text"] == text and story["voice_id"] == voice["id"]
    campaign = studio.config.campaign_dir
    assert studio.audio_path(voice["id"]) == campaign / "voix" / f"{voice['id']}.wav"
    assert studio.audio_path(story["id"]) == campaign / "histoires" / f"{story['id']}.wav"
    assert (campaign / "histoires" / f"{story['id']}.md").read_text(encoding="utf-8") == (
        f"# Le gardien\n\n{text}\n")
    assert not (campaign / "voice_studio").exists()
    assert all(call[1:] == ("Grave et calme.", "French") for call in studio.calls)
    with wave.open(str(studio.audio_path(story["id"]))) as audio:
        assert audio.getframerate() == 24000 and audio.getnchannels() == 1
        assert audio.getnframes() == 240 * (len(studio.calls) - 1)
    restored = StubStudio(studio.config)
    assert restored.catalog()["voices"] == [voice]
    assert restored.catalog()["stories"] == [story]
    assert restored.audio_path("../../config.toml") is None
    assert not list(studio.folder.rglob("*.tmp"))


def test_generate_story_uses_only_the_user_idea_without_campaign_secrets(studio):
    llm = FakeLLM()
    studio.llm_factory = lambda: llm
    result = studio.run(request(studio, "generate_story", prompt="Un dragon qui a peur du noir."))
    assert result["item"]["text"] == "Greetings, traveler."
    story = result["item"]
    assert (studio.config.campaign_dir / "histoires" / f"{story['id']}.md").read_text(
        encoding="utf-8") == "# Le gardien\n\nGreetings, traveler.\n"
    system, messages = llm.calls[0]
    assert "French" in system
    assert messages == [{"role": "user", "content": "Un dragon qui a peur du noir."}]
    assert studio.config.campaign_dir.name not in system


def test_legacy_library_still_plays_without_moving_old_files(studio):
    voice = studio.run(request(studio))["item"]
    folder = studio.config.campaign_dir
    legacy = folder / "voice_studio"
    (legacy / "audio").mkdir(parents=True)
    (legacy / "voices").mkdir()
    for suffix, destination in ((".wav", "audio"), (".json", "voices")):
        (folder / "voix" / f"{voice['id']}{suffix}").rename(
            legacy / destination / f"{voice['id']}{suffix}")
    restored = StubStudio(studio.config)
    assert restored.catalog()["voices"] == [voice]
    assert restored.audio_path(voice["id"]) == legacy / "audio" / f"{voice['id']}.wav"
    story = studio.run(request(studio, "narrate_text", voice_id=voice["id"]))["item"]
    assert studio.audio_path(story["id"]).parent == folder / "histoires"


def test_campaign_wavs_without_studio_metadata_are_listed_and_seekable(studio_server):
    server = studio_server
    folder = server.studio.folder / "histoires"
    folder.mkdir()
    path = folder / "La forêt des murmures.wav"
    with wave.open(str(path), "wb") as audio:
        audio.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        audio.writeframes(np.zeros(24000, dtype="<i2").tobytes())
    path.with_suffix(".md").write_text("# La forêt\n\nIl était une fois…", encoding="utf-8")
    (folder / "brouillon.wav").write_bytes(b"incomplete")
    (folder / "texte-seul.md").write_text("Sans audio", encoding="utf-8")
    (folder / "mj").mkdir()
    (folder / "mj" / "secret.wav").write_bytes(path.read_bytes())

    with connect(f"ws://127.0.0.1:{server.port}/ws") as ws:
        receive_until(ws, "hello")
        ws.send(json.dumps({"type": "studio_list"}))
        catalog = receive_until(ws, "studio_catalog")
    assert len(catalog["stories"]) == 1
    story = catalog["stories"][0]
    assert story["title"] == "La forêt" and story["duration"] == 1
    assert "Il était une fois" in story["text"]
    assert server.studio.audio_path(story["id"]) == path
    assert StubStudio(server.app.config).catalog()["stories"] == [story]
    with httpx.Client(trust_env=False) as http:
        url = f"http://127.0.0.1:{server.port}" + story["audio_url"]
        assert http.get(url).content == path.read_bytes()
        assert http.get(url, headers={"Range": "bytes=-10"}).content == path.read_bytes()[-10:]
    assert not server.studio.calls



def test_standalone_web_app_can_download_audio_without_bypassing_authorization(studio_server):
    server = studio_server
    story = server.studio.run(request(server.studio, "narrate_text"))["item"]
    url = f"http://127.0.0.1:{server.port}" + story["audio_url"]
    origin = {"Origin": "http://localhost:4200"}
    with httpx.Client(trust_env=False) as http:
        response = http.get(url, headers=origin)
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "*"
        assert response.headers["content-type"] == "audio/wav"
        assert response.content == server.studio.audio_path(story["id"]).read_bytes()
        assert "access-control-allow-credentials" not in response.headers
        ranged = http.get(url, headers={**origin, "Range": "bytes=0-43"})
        assert ranged.status_code == 206
        assert ranged.headers["access-control-allow-origin"] == "*"
        assert "Content-Range" in ranged.headers["access-control-expose-headers"]
        server._authorize = lambda request: False
        assert http.get(url, headers=origin).status_code == 401


def test_campaign_id_is_stable_and_does_not_expose_path(studio, tmp_path):
    identifier = studio.catalog()["campaign_id"]
    assert len(identifier) == 32
    assert StubStudio(studio.config).catalog()["campaign_id"] == identifier
    other = StubStudio(SimpleNamespace(campaign_dir=tmp_path / "other"))
    assert other.catalog()["campaign_id"] != identifier


def test_story_discovery_does_not_follow_links_outside_campaign(studio, tmp_path):
    outside = tmp_path / "private.wav"
    with wave.open(str(outside), "wb") as audio:
        audio.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\x00\x00")
    folder = studio.folder / "histoires"
    folder.mkdir()
    linked = folder / "secret.wav"
    try:
        linked.symlink_to(outside)
    except OSError:
        pytest.skip("Symlinks unavailable")
    assert studio.catalog()["stories"] == []
    assert studio.audio_path(studio._file_id(linked)) is None


def test_offline_scripts_are_served_and_require_authorization(studio_server):
    base = f"http://127.0.0.1:{studio_server.port}"
    with httpx.Client(trust_env=False) as http:
        for resource in ("/mobile-sw.js", "/mobile-offline.js"):
            response = http.get(base + resource)
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/javascript")
            studio_server._authorize = lambda request: False
            assert http.get(base + resource).status_code == 401
            studio_server._authorize = None


def test_metadata_failure_leaves_no_incomplete_story(studio, monkeypatch):
    write = studio._write_text

    def fail_metadata(path, text):
        if path.suffix == ".json":
            raise OSError("disk full")
        write(path, text)

    monkeypatch.setattr(studio, "_write_text", fail_metadata)
    with pytest.raises(OSError, match="disk full"):
        studio.run(request(studio, "narrate_text"))
    assert list((studio.config.campaign_dir / "histoires").iterdir()) == []
    assert studio.catalog()["stories"] == []


@pytest.mark.parametrize("values", [{"text": ""}, {"text": "a" * 12001}, {"language": "Klingon"},
                                    {"description": []}, {"voice_id": "unknown"}])
def test_invalid_input_never_starts_inference(studio, values):
    with pytest.raises(ValueError):
        request(studio, "narrate_text", **values)
    assert studio.calls == []


def test_cancel_and_generation_error_remove_partial_files(studio):
    cancel = threading.Event()
    normal = studio.runtime_factory(None, None).synthesize

    def interrupted(*args, **kwargs):
        cancel.set()
        return normal(*args, **kwargs)

    studio.runtime_factory = lambda *a: SimpleNamespace(synthesize=interrupted)
    with pytest.raises(StudioCancelled):
        studio.run(request(studio), cancel=cancel)
    assert not list(studio.folder.rglob("*.wav"))
    assert not list(studio.folder.rglob("*.tmp"))
    assert studio.catalog()["voices"] == []

    def broken(*args, **kwargs):
        raise RuntimeError("inference failed")

    studio.runtime_factory = lambda *a: SimpleNamespace(synthesize=broken)
    with pytest.raises(RuntimeError, match="inference failed"):
        studio.run(request(studio))
    assert not list(studio.folder.rglob("*.tmp"))
    assert not list(studio.folder.rglob("*.wav"))


@pytest.fixture
def studio_server(config, studio):
    app = NPCApp(config, llm=FakeLLM(), on_event=lambda e: None)
    server = MobileServer(app, port=0, studio=studio)
    server.start()
    yield server
    server.stop()


def receive_until(ws, kind):
    for _ in range(40):
        message = json.loads(ws.recv(timeout=3))
        if message["type"] == kind:
            return message
    pytest.fail(f"Missing {kind}")


def test_mobile_creates_voice_then_downloads_seekable_wav_and_lists_library(studio_server):
    server = studio_server
    base = f"http://127.0.0.1:{server.port}"
    with connect(f"ws://127.0.0.1:{server.port}/ws") as ws:
        assert receive_until(ws, "hello")["capabilities"]["voice_design"]
        ws.send(json.dumps({"type": "voice_design", "title": "Gardien", "description": "Grave."}))
        receive_until(ws, "studio_started")
        result = receive_until(ws, "studio_result")
        url = base + result["item"]["audio_url"]
        with httpx.Client(trust_env=False) as http:
            page = http.get(base + "/")
            assert page.status_code == 200 and "Le studio du conteur" in page.text
            assert page.headers["content-type"].startswith("text/html")
            audio = http.get(url)
            assert audio.status_code == 200 and audio.content[:4] == b"RIFF"
            assert audio.headers["content-type"] == "audio/wav"
            partial = http.get(url, headers={"Range": "bytes=0-43"})
            assert partial.status_code == 206 and partial.content == audio.content[:44]
            assert http.get(url, headers={"Range": "bytes=999999-"}).status_code == 416
            assert http.get(base + "/studio/audio/../../config.toml").status_code == 404
        ws.send(json.dumps({"type": "studio_list"}))
        assert receive_until(ws, "studio_catalog")["voices"] == [result["item"]]
    assert server.app.llm.calls == []


def test_studio_job_is_bounded_cancellable_and_does_not_block_status(studio_server):
    server = studio_server
    entered = threading.Event()

    def synthesize(*args, cancel, **kwargs):
        entered.set()
        assert cancel.wait(3)
        return np.ones(10, dtype=np.int16), 24000

    server.studio.runtime_factory = lambda *a: SimpleNamespace(synthesize=synthesize)
    with connect(f"ws://127.0.0.1:{server.port}/ws") as first, connect(
            f"ws://127.0.0.1:{server.port}/ws") as second:
        receive_until(first, "hello")
        receive_until(second, "hello")
        command = json.dumps({"type": "voice_design", "description": "Grave."})
        first.send(command)
        receive_until(first, "studio_started")
        assert entered.wait(2)
        second.send(command)
        assert "occupé" in receive_until(second, "error")["message"]
        first.send(json.dumps({"type": "status"}))
        assert receive_until(first, "status")["state"] == "idle"
        first.send(json.dumps({"type": "studio_cancel"}))
        receive_until(first, "studio_cancelled")
    assert not list(server.studio.folder.rglob("*.wav"))


def test_studio_http_respects_existing_authorization(studio_server):
    server = studio_server
    server._authorize = lambda request: False
    response = httpx.get(f"http://127.0.0.1:{server.port}/", trust_env=False)
    assert response.status_code == 401
