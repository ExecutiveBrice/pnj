"""The real mobile companion's disclosure contract, using localhost sockets."""

import json

import pytest
from websockets.sync.client import connect

from npc.app import NPCApp
from npc.events import SecretDisclosureRequested, SecretDisclosureResolved, SecretDisclosuresChanged
from npc.mobile_protocol import event_to_message, gm_event_to_message
from npc.mobile_server import MobileServer
from npc.session.secrets import SecretsSheet

from test_app_pipeline import FakeLLM, FakeSpeaker, drain


@pytest.fixture
def server(config, campaign):
    (campaign / "characters").mkdir()
    (campaign / "characters" / "smith.md").write_text("# Smith\nA smith.\n")
    (campaign / "secrets").mkdir()
    (campaign / "secrets" / "smith.md").write_text(
        "## teleporter-key\nhint: the smith's key\n\nThe smith hid it in his forge.\n")
    server = None

    def on_event(event):
        if server is not None:
            server.publish(event)

    app = NPCApp(config, llm=FakeLLM(), speaker=FakeSpeaker(), on_event=on_event)
    app.start()
    server = MobileServer(app, port=0)
    server.start()
    yield server
    app.shutdown(summarize=False)
    server.stop()


def client(server, **kwargs):
    return connect(f"ws://127.0.0.1:{server.port}/ws", **kwargs)


def recv(ws):
    return json.loads(ws.recv(timeout=2))


def until(ws, kind):
    for _ in range(50):
        message = recv(ws)
        if message["type"] == kind:
            return message
    pytest.fail(f"did not receive {kind}")


def send(ws, **message):
    ws.send(json.dumps(message))


def snapshot(ws):
    send(ws, type="status")
    until(ws, "status")
    return until(ws, "secret_disclosure_list")["requests"]


def request(server, ws, context="Where is the key?"):
    server.app.llm.reply = "Give me a moment. [CHECK:teleporter-key]"
    send(ws, type="say", text=context)
    proposal = until(ws, "secret_disclosure_requested")
    until(ws, "state")  # speaking (non-streaming fake)
    until(ws, "state")  # idle
    return proposal


def decide(ws, proposal, decision="approved"):
    send(ws, type="resolve_secret_disclosure", request_id=proposal["request_id"], decision=decision)


def prompts(app):
    return "\n".join(system + "\n" + "\n".join(m["content"] for m in messages)
                     for system, messages in app.llm.calls)


def test_capability_snapshot_and_reconnect_match_mobile_contract(server):
    with client(server) as ws:
        assert recv(ws)["capabilities"]["secret_approval"] is True
        proposal = request(server, ws)
        secret = server.app.active.secrets.get("teleporter-key")
        assert proposal == {"type": "secret_disclosure_requested",
                            "request_id": proposal["request_id"],
                            "npc": server.app.npc_name, "secret": secret.body,
                            "context": "Where is the key?"}
        expected = [{k: v for k, v in proposal.items() if k != "type"}]
        assert snapshot(ws) == expected
        assert secret.body not in prompts(server.app)
        assert "CHECK" not in str(server.app.speaker.spoken)
    with client(server) as reconnected:
        assert recv(reconnected)["type"] == "hello"
        assert snapshot(reconnected) == expected


@pytest.mark.parametrize("decision", ["approved", "rejected"])
def test_decision_broadcast_retry_and_conflict_do_not_repeat_delivery(server, decision):
    with client(server) as first, client(server) as second:
        recv(first)
        recv(second)
        proposal = request(server, first)
        assert until(second, "secret_disclosure_requested") == proposal
        server.app.llm.reply = "The key is hidden nearby."
        decide(first, proposal, decision)
        expected = {"type": "secret_disclosure_resolved", "request_id": proposal["request_id"],
                    "decision": decision}
        assert until(first, "secret_disclosure_resolved") == expected
        assert until(second, "secret_disclosure_resolved") == expected
        drain(server.app)
        calls = len(server.app.llm.calls)
        assert calls == (2 if decision == "approved" else 1)
        assert snapshot(first) == []
        decide(second, proposal, decision)
        assert until(second, "secret_disclosure_resolved") == expected
        decide(first, proposal, "rejected" if decision == "approved" else "approved")
        assert until(first, "secret_disclosure_list")["requests"] == []
        drain(server.app)
        assert len(server.app.llm.calls) == calls
        slot = server.app.active
        saved = SecretsSheet.load(slot.secrets_path).get("teleporter-key")
        assert bool(saved.revealed) == (decision == "approved")
        assert ("teleporter-key" in slot.denied_secrets) == (decision == "rejected")
        if decision == "approved":
            assert saved.body in server.app.llm.calls[-1][0]
        else:
            assert saved.body not in prompts(server.app)


@pytest.mark.parametrize("decision", ["approved", "rejected"])
def test_decisions_target_original_npc_and_preserve_other_pending_requests(server, decision):
    with client(server) as ws:
        recv(ws)
        original = server.app.active
        first = request(server, ws)
        send(ws, type="switch_npc", npc="smith")
        until(ws, "npc_switched")
        second = request(server, ws, "And your key?")
        assert first["request_id"] != second["request_id"]
        assert len(snapshot(ws)) == 2
        server.app.llm.reply = "Here is what I know."
        decide(ws, first, decision)
        assert until(ws, "secret_disclosure_resolved")["decision"] == decision
        drain(server.app)
        assert bool(original.secrets.get("teleporter-key").revealed) == (decision == "approved")
        assert not server.app.roster["smith"].secrets.get("teleporter-key").revealed
        assert snapshot(ws) == [{k: v for k, v in second.items() if k != "type"}]
        assert server.app.active is (original if decision == "approved"
                                     else server.app.roster["smith"])
        if decision == "rejected":
            assert original.ooc_notes
            assert not server.app.roster["smith"].ooc_notes


@pytest.mark.parametrize("command,decision", [("/yes", "approved"), ("/no", "rejected"),
                                              ("/reveal teleporter-key", "approved")])
def test_cli_decisions_confirm_mobile_requests(server, command, decision):
    with client(server) as ws:
        recv(ws)
        proposal = request(server, ws)
        server.app.llm.reply = "Here is the key."
        server.app.handle_line(command)
        assert until(ws, "secret_disclosure_resolved") == {
            "type": "secret_disclosure_resolved", "request_id": proposal["request_id"],
            "decision": decision}
        drain(server.app)
        assert snapshot(ws) == []


def test_later_invalidates_old_request_and_new_proposal_gets_new_identity(server):
    with client(server) as ws:
        recv(ws)
        first = request(server, ws)
        server.app.handle_line("/later")
        assert until(ws, "secret_disclosure_list")["requests"] == []
        second = request(server, ws)
        assert first["request_id"] != second["request_id"]
        decide(ws, first)
        assert until(ws, "secret_disclosure_list")["requests"][0]["request_id"] == second["request_id"]
        assert not server.app.active.secrets.get("teleporter-key").revealed


@pytest.mark.parametrize("replacement", ["", "## teleporter-key\nhint: changed\n\nNew body.\n"])
def test_reload_invalidates_deleted_or_edited_proposals(server, replacement):
    with client(server) as ws:
        recv(ws)
        proposal = request(server, ws)
        server.app.active.secrets_path.write_text(replacement, encoding="utf-8")
        server.app.handle_line("/reload")
        assert until(ws, "secret_disclosure_list")["requests"] == []
        decide(ws, proposal)
        assert until(ws, "secret_disclosure_list")["requests"] == []
        drain(server.app)
        assert len(server.app.llm.calls) == 1
        assert not server.app.active.pending_secret
        assert not server.app.active.secrets.revealed()


def test_approval_is_not_confirmed_if_persistence_fails(server, monkeypatch):
    with client(server) as ws:
        recv(ws)
        proposal = request(server, ws)
        slot = server.app.active
        events = []
        on_event = server.app._on_event

        def observe(event):
            events.append(event)
            on_event(event)

        server.app._on_event = observe

        def fail(path):
            raise OSError("private path")

        monkeypatch.setattr(slot.secrets, "save", fail)
        decide(ws, proposal)
        send(ws, type="status")
        until(ws, "status")
        # A status can overtake queued work; wait for that work before checking again.
        until(ws, "secret_disclosure_list")
        drain(server.app)
        assert snapshot(ws)[0]["request_id"] == proposal["request_id"]
        assert not slot.secrets.get("teleporter-key").revealed
        assert len(server.app.llm.calls) == 1
        assert not any(isinstance(e, SecretDisclosureResolved) for e in events)


def test_concurrent_opposite_decisions_apply_only_one_outcome(server):
    with client(server) as first, client(server) as second:
        recv(first)
        recv(second)
        proposal = request(server, first)
        until(second, "secret_disclosure_requested")
        server.app.llm.reply = "Here is the key."
        # Both sockets submit before either observes a confirmation.
        decide(first, proposal, "approved")
        decide(second, proposal, "rejected")
        outcome = until(first, "secret_disclosure_resolved")
        assert until(second, "secret_disclosure_resolved") == outcome
        drain(server.app)
        slot = server.app.active
        approved = outcome["decision"] == "approved"
        assert bool(slot.secrets.get("teleporter-key").revealed) == approved
        assert ("teleporter-key" in slot.denied_secrets) != approved
        assert len(server.app.llm.calls) == (2 if approved else 1)
        assert snapshot(first) == []


def test_player_connections_cannot_see_or_resolve_gm_disclosures(server):
    server._authorize_gm = lambda req: req.headers.get("X-GM") == "yes"
    with client(server, additional_headers={"X-GM": "yes"}) as gm, client(server) as player:
        assert recv(gm)["capabilities"]["secret_approval"] is True
        assert recv(player)["capabilities"]["secret_approval"] is False
        proposal = request(server, gm)
        # Drain all public messages up to a status barrier; no GM payload may cross.
        send(player, type="status")
        messages = []
        while True:
            message = recv(player)
            messages.append(message)
            if message["type"] == "status":
                break
        assert not any(m["type"].startswith("secret_") for m in messages)
        assert proposal["secret"] not in json.dumps(messages)
        decide(player, proposal)
        assert until(player, "error")["message"] == "Secret approval is unavailable for this connection"
        assert not server.app.active.secrets.get("teleporter-key").revealed


@pytest.mark.parametrize("values", [
    {}, {"request_id": "x", "decision": True}, {"request_id": [], "decision": "approved"},
    {"request_id": "x", "decision": "later"}, {"request_id": " ", "decision": "approved"},
])
def test_invalid_decision_fields_return_error_and_keep_connection_usable(server, values):
    with client(server) as ws:
        recv(ws)
        send(ws, type="resolve_secret_disclosure", **values)
        assert until(ws, "error")["type"] == "error"
        assert snapshot(ws) == []


def test_new_gm_events_stay_out_of_player_serializer():
    events = [SecretDisclosureRequested("id", "NPC", "body", "context"),
              SecretDisclosureResolved("id", "approved"), SecretDisclosuresChanged()]
    for event in events:
        assert event.dm_only
        assert event_to_message(event, "NPC") is None
    assert gm_event_to_message(events[0])["secret"] == "body"


def test_gm_cards_and_secret_inspection_target_inactive_npc_without_unlocking(server):
    slot = server.app.roster["smith"]
    slot.path.write_text(
        "# Smith\n- Scénario : Forge\n- Attitude : Méfiant\n"
        "- Motivation : Protéger sa forge\n- Levier de négociation : Du charbon\n"
        "- Cache : Une confidence de la forge.\n")
    server.app.handle_line("/reload")
    with client(server) as ws:
        hello = recv(ws)
        cards = until(ws, "gm_npc_list")["npcs"]
        card = next(c for c in cards if c["id"] == "smith")
        assert card == {"id": "smith", "name": "Smith", "active": False,
                        "scenario": "Forge", "attitude": "Méfiant",
                        "motivation": "Protéger sa forge", "negotiation": "Du charbon",
                        "secret_count": 2}
        assert slot.secrets.entries[0].body not in json.dumps(cards)
        send(ws, type="get_npc_secrets", npc="smith")
        response = until(ws, "gm_npc_secrets")
        assert response["id"] == "smith"
        assert response["secrets"] == [
            {"body": "Une confidence de la forge.", "hint": "", "status": "local"},
            {"body": "The smith hid it in his forge.", "hint": "the smith's key",
             "status": "locked"}]
        assert server.app.npc_name == hello["npc"]
        assert not slot.secrets.entries[0].revealed
        assert len(slot.history) == 0
        assert not slot.ooc_notes
        assert not server.app.llm.calls
        # The same connection observes an edited card and an added NPC after reload.
        slot.path.write_text("# Smith\n- Scénario : Updated\n## Secrets locaux\n- New clue.")
        (slot.path.parent / "scout.md").write_text("# Scout\n- Scénario : Forest\n")
        server.app.handle_line("/reload")
        cards = until(ws, "gm_npc_list")["npcs"]
        assert next(c for c in cards if c["id"] == "smith")["scenario"] == "Updated"
        assert {c["id"] for c in cards} == {"character", "smith", "scout"}
        send(ws, type="get_npc_secrets", npc="smith")
        assert until(ws, "gm_npc_secrets")["secrets"][0]["body"] == "New clue."
        # Reading a blocked secret on the phone must not expose it to a later model call.
        assert server.app.switch_npc("smith")
        drain(server.app)
        assert server.app.submit_player_text("Where is the key?")
        drain(server.app)
        assert "The smith hid it in his forge." not in prompts(server.app)
        assert not slot.secrets.entries[0].revealed


def test_player_cannot_fetch_npc_secrets_or_receive_gm_cards(server):
    server._authorize_gm = lambda req: req.headers.get("X-GM") == "yes"
    with client(server) as ws:
        assert recv(ws)["capabilities"]["secret_approval"] is False
        server.app.handle_line("/reload")
        send(ws, type="get_npc_secrets", npc="smith")
        messages = []
        while True:
            message = recv(ws)
            messages.append(message)
            if message["type"] == "error":
                break
        assert message["message"] == "NPC secrets are unavailable for this connection"
        send(ws, type="status")
        while True:
            message = recv(ws)
            messages.append(message)
            if message["type"] == "status":
                break
        assert not any(m["type"].startswith("gm_npc_") for m in messages)
        assert "the smith's key" not in json.dumps(messages)
        assert "scenario" not in json.dumps(messages)


@pytest.mark.parametrize("npc", [None, [], {}, "../secrets", "SMITH", "unknown"])
def test_secret_inspection_uses_exact_validated_npc_ids(server, npc):
    with client(server) as ws:
        send(ws, type="get_npc_secrets", npc=npc)
        assert until(ws, "error")["message"] == "Unknown NPC"
        send(ws, type="get_npc_secrets", npc="smith")
        assert until(ws, "gm_npc_secrets")["name"] == "Smith"


def test_mobile_gm_buttons_target_npc_without_a_reply_tone(server):
    with client(server) as ws:
        cards = until(ws, "gm_npc_list")
        assert "speech_attitudes" not in cards
        assert cards["speech_actions"] == [
            {"id": "presentation", "label": "Présentation"},
            {"id": "fill_wait", "label": "Meubler pour attendre"},
            {"id": "greeting", "label": "Salutation"},
        ]
        send(ws, type="speak_npc", npc="smith")
        reply = until(ws, "npc_replied")
        until(ws, "state")  # idle after playback
        assert reply["npc"] == "Smith"
        assert len(server.app.llm.calls) == 1
        assert "The smith hid it in his forge." not in prompts(server.app)
        assert not server.app.active.secrets.revealed()
        assert server.app.active.player_turns == 0


def test_player_connections_cannot_issue_speech_cues(server):
    server._authorize_gm = lambda req: False
    with client(server) as ws:
        send(ws, type="speak_npc", npc="smith", attitude="complaisant")
        assert until(ws, "error")["message"] == "NPC speech cues are unavailable for this connection"
        assert server.app.active.stem == "character"
        assert not server.app.llm.calls


@pytest.mark.parametrize("values", [
    {},
    {"npc": "../secrets", "attitude": "rude"}, {"npc": [], "attitude": "rude"},
    *({"npc": "smith", "attitude": "joyeux", "action": action}
      for action in ([], {}, "", "unknown", "constructor")),
])
def test_bad_mobile_speech_cue_does_not_switch_and_connection_survives(server, values):
    with client(server) as ws:
        send(ws, type="speak_npc", **values)
        assert until(ws, "error")["message"] == "NPC speech cue rejected"
        assert server.app.active.stem == "character"
        send(ws, type="speak_npc", npc="smith", attitude="amusant")
        assert until(ws, "npc_replied")["npc"] == "Smith"



@pytest.mark.parametrize("language", ["en", "fr-CA"])
@pytest.mark.parametrize("action,en_action,fr_action", [
    ("presentation", "Introduction:", "Présentation :"),
    ("fill_wait", "Fill the wait:", "Meubler pour attendre :"),
    ("greeting", "Greeting:", "Salutation :"),
])
@pytest.mark.parametrize("legacy", [
    {}, *({"attitude": value} for value in (
        "humoriste", "hargneux", "presse", "joyeux",
        "agacement", "amusant", "rude", "complaisant",
        "INJECTED TONE", [],
    )),
])
def test_mobile_speech_actions_reach_the_voice_pipeline(
        server, language, action, en_action, fr_action, legacy):
    server.app.config.language = language
    server.app.llm.reply = ("Bienvenue dans ma forge." if language.startswith("fr")
                            else "Welcome to my forge.")
    with client(server) as ws:
        send(ws, type="speak_npc", npc="smith", action=action, **legacy)
        reply = until(ws, "npc_replied")
        drain(server.app)
        assert reply["npc"] == "Smith"
        assert len(server.app.llm.calls) == 1
        cue = server.app.llm.calls[0][1][-1]["content"]
        assert (fr_action if language.startswith("fr") else en_action) in cue
        from npc.session.prompt import opening_instruction

        assert cue == opening_instruction(language, action=action)
        assert server.app.speaker.spoken == [reply["text"]]
        assert "The smith hid it in his forge." not in prompts(server.app)
        assert not server.app.active.secrets.revealed()
        assert server.app.active.player_turns == 0
        assert cue not in str(server.app.history.as_messages())
        assert not server.app.active.ooc_notes


@pytest.mark.parametrize("format", ["bullet", "section"])
@pytest.mark.parametrize("language", ["en", "fr"])
@pytest.mark.parametrize("action,reference", [
    ("presentation", "INTRODUCTION REFERENCE"),
    ("fill_wait", "WAIT REFERENCE"),
    ("greeting", "FAREWELL REFERENCE"),
])
def test_mobile_action_uses_only_its_sheet_reference(server, format, language, action, reference):
    server.app.config.language = language
    server.app.llm.reply = "Bienvenue dans ma forge." if language == "fr" else "Welcome to my forge."
    slot = server.app.roster["smith"]
    entries = {"Introduction": "INTRODUCTION REFERENCE", "Meuble": "WAIT REFERENCE",
               "Salutation": "FAREWELL REFERENCE"}
    speech = "".join(f"- {label} : {value}\n" if format == "bullet"
                     else f"## {label}\n{value}\n" for label, value in entries.items())
    slot.path.write_text("# Smith\nA smith.\n" + speech + "## Secrets\n- A local confidence.\n",
                         encoding="utf-8")
    server.app.handle_line("/reload")
    drain(server.app)
    assert next(card for card in server.app.gm_npc_details()
                if card["id"] == "smith")["secret_count"] == 2
    with client(server) as ws:
        send(ws, type="speak_npc", npc="smith", action=action)
        until(ws, "npc_replied")
        drain(server.app)
        system, messages = server.app.llm.calls[-1]
        cue = messages[-1]["content"]
        assert reference in cue
        for other in entries.values():
            assert other not in system
            assert (other in cue) == (other == reference)
        assert "The smith hid it in his forge." not in system + cue
        assert "REFERENCE" not in str(slot.history.as_messages())
        assert not slot.ooc_notes
        server.app.llm.reply = ("Les affaires vont bien aujourd'hui." if language == "fr"
                                else "Business is good today.")
        send(ws, type="say", text="How is business?")
        until(ws, "npc_replied")
        drain(server.app)
        system, messages = server.app.llm.calls[-1]
        assert "REFERENCE" not in system + str(messages)
