"""Read-only Ollama probe for Vess's secret scope and changes of subject.

Run: python scripts/probe_conversation.py [--model MODEL] [--runs N]
This does not reveal secrets or write a transcript in the campaign.
Replies require human review; the printed flags only catch obvious regressions.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import time
import tempfile
from pathlib import Path

from npc.app import NPCApp
from npc.config import load_config
from npc.events import ErrorOccurred, NpcReplied, SecretRevealRequested
from npc.llm import make_llm_client
from npc.roster import discover_character_files, load_slot
from npc.session.prompt import build_system_prompt, extract_dialogue, looks_foreign, reply_repeats
from npc.session.secrets import find_markers, strip_markers


def player(text):
    return {"role": "user", "content": f'PLAYER (spoken): "{text}"'}


class SilentSpeaker:
    """Exercise streaming validation without synthesizing or playing audio."""

    def say(self, text):
        pass

    def say_stream(self, sentences):
        return list(sentences), False

    def stop(self):
        pass


def pipeline_reply(config, slot, secrets, messages, llm):
    with tempfile.TemporaryDirectory(prefix="vess-probe-") as directory:
        scratch = Path(directory)
        (scratch / "character.md").write_text(slot.character, encoding="utf-8")
        (scratch / "adventure.md").write_text(
            config.adventure_file.read_text(encoding="utf-8"), encoding="utf-8")
        probe_config = copy.deepcopy(config)
        probe_config.campaign_dir = scratch
        probe_config.checkpoint_every_turns = 0
        probe_config.character_languages["character"] = config.language_for(slot.stem)
        events = []
        app = NPCApp(probe_config, llm=llm, speaker=SilentSpeaker(), on_event=events.append)
        app.active.secrets = copy.deepcopy(secrets)
        app.active.lore = slot.lore
        for message in messages[:-1]:
            if message["role"] == "assistant":
                app.history.add_npc(message["content"])
            else:
                app.history.add_player(message["content"].removeprefix(
                    'PLAYER (spoken): "').removesuffix('"'))
        line = messages[-1]["content"].removeprefix('PLAYER (spoken): "').removesuffix('"')
        app.start()
        try:
            app.handle_line("/say " + line)
            app._queue.join()
        finally:
            app.shutdown(summarize=False)
        replies = [e.text for e in events if isinstance(e, NpcReplied)]
        ids = [e.secret_id for e in events if isinstance(e, SecretRevealRequested)]
        errors = [e.message for e in events if isinstance(e, ErrorOccurred)]
        return " ".join(replies), ids, errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", type=Path, default=Path("CampagneTest"))
    parser.add_argument("--model")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--scenario", action="append",
                        help="Run only this scenario; repeat the option to select several")
    parser.add_argument("--raw", action="store_true",
                        help="Probe only the dialogue prompt, bypassing routing and retries")
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be positive")
    config = load_config(args.campaign)
    if args.model:
        config.llm.model = args.model
    config.llm.timeout_seconds = 120
    llm = make_llm_client(config.llm)
    ref = next(r for r in discover_character_files(config.campaign_dir) if r.stem == "vess")
    slot = load_slot(ref, config)
    language = config.language_for(slot.stem)
    locked = copy.deepcopy(slot.secrets)
    for secret in locked.entries:
        secret.revealed = None
    revealed = copy.deepcopy(locked)
    revealed.get("teleporter-key").revealed = "probe"
    adventure = config.adventure_file.read_text(encoding="utf-8")
    refusal = "Je vous comprends bien, mais je ne peux pas vous permettre d'approcher de l'arche."
    scenarios = [
        ("touch-is-not-a-secret", locked,
         [player("Alors Vess, on se connaît maintenant, est-ce que tu veux bien que je touche au monolithe ?")],
         []),
        ("key-location", locked,
         [player("Où avez-vous caché la clé de téléporteur fonctionnelle ?")],
         ["teleporter-key"]),
        ("erased-records", locked,
         [player("Qu'avez-vous supprimé des archives de l'Ordre ?")],
         ["erased-discovery"]),
        ("hug-after-refusal", revealed,
         [player("Super du coup je peux toucher le monolithe"),
          {"role": "assistant", "content": refusal},
          player("Et à toi, je peux te faire un câlin ?")], []),
        ("hug-after-reveal-history", revealed,
         [player("Alors Vess, on se connaît maintenant, je peux toucher au monolithe ?"),
          {"role": "assistant", "content": "Je ne sais pas encore si je peux vous en parler."},
          {"role": "assistant", "content": revealed.get("teleporter-key").body},
          player("Super du coup je peux toucher le monolithe"),
          {"role": "assistant", "content": refusal},
          player("Et à toi, je peux te faire un câlin ?")], []),
        ("repeated-pressure", revealed,
         [player("Je peux le taper alors ?"),
          {"role": "assistant", "content": "Non. Le monolithe est une relique sacrée."},
          player("Mais il ne sert à rien ! Allez, je tape dedans !")], []),
        ("key-rumour-is-not-approval", locked,
         [player("Un téléporteur, j'en ai bien entendu parler."),
          {"role": "assistant", "content": "Ou avez-vous entendu parler d'une clé ?"},
          player("Oui, une clé, c'est ça, c'est Tokk qui m'en a parlé.")], []),
        ("guardian-refuses-pillage", revealed,
         [{"role": "assistant", "content": revealed.get("teleporter-key").body},
          player("Utilisez le téléporteur et allez piller l'autre côté.")], []),
        ("guardian-refuses-forced-entry", revealed,
         [player("Je vais piller l'autre côté."),
          {"role": "assistant", "content": "Non. Je vous refuse l'accès au sanctuaire. Sortez."},
          player("Je passe quand même et je me sers.")], []),
    ]
    if args.scenario:
        known = {s[0] for s in scenarios}
        unknown = set(args.scenario) - known
        if unknown:
            parser.error(f"Unknown scenarios: {', '.join(sorted(unknown))}")
        scenarios = [s for s in scenarios if s[0] in args.scenario]
    failures = 0
    for name, secrets, messages, expected in scenarios:
        system = build_system_prompt(slot.character, adventure, "", [],
                                     secrets=secrets, lore=slot.lore, language=language)
        for run in range(args.runs):
            start = time.perf_counter()
            if args.raw:
                raw = llm.chat(system, messages)
                reply = extract_dialogue(strip_markers(raw), slot.name)
                ids, errors = find_markers(raw), []
            else:
                reply, ids, errors = pipeline_reply(config, slot, secrets, messages, llm)
            foreign = looks_foreign(reply, language)
            repeated = reply_repeats(reply, messages)
            # Vess-specific clues absent from the player questions and public lore.
            key_leak = (not secrets.get("teleporter-key").revealed and bool(re.search(
                r"sous (?:la pierre de )?l['’]autel|cavité|(?:trois|3) charges", reply,
                re.IGNORECASE)))
            failures += bool(errors or foreign or repeated or key_leak or ids != expected)
            print(json.dumps({"scenario": name, "run": run + 1, "model": llm.model,
                              "seconds": round(time.perf_counter() - start, 2),
                              "reply": reply, "markers": ids,
                              "errors": errors,
                              "marker_scope_ok": ids == expected,
                              "wrong_language": foreign, "exact_repeat": repeated,
                              "locked_key_details": key_leak},
                             ensure_ascii=False), flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
