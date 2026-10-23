"""Probe the NPC prompt against a real Ollama model for self-narration leaks.

Run after changing ROLE_FRAMING or swapping models:

    uv run python scripts/probe_narration.py [model]
    uv run python scripts/probe_narration.py [model] --coherence

Each turn prints the raw model output and, when the sanitizer changed it, the
cleaned version. RAW may still narrate occasionally (the sanitizer's job is to
catch it); the failure to watch for is narration surviving in CLEANED, or raw
QUOTELESS leading narration showing up often — that shape is only fixable in
the prompt, so iterate on ROLE_FRAMING if you see it in more than ~1 in 10
replies.

With --coherence, inspect French and English negotiations against the printed
expectations. These are manual semantic checks, not automatic pass/fail tests.
"""

import argparse
from importlib import resources

from npc.llm import OllamaClient
from npc.session.history import ConversationHistory
from npc.session.prompt import build_system_prompt, extract_dialogue

NPC_NAME = "Vess of the Glass Monolith"
PROBES = [
    "What do you look like, old woman?",
    "Show me how you feel about the Glass Monolith.",
    "Tell me your saddest memory.",
    "We found a strange device in the ruins. Look at it.",
    "Are you angry with us? Show me.",
    "Varför bor du så nära monoliten?",
    "Beskriv dig själv för mig.",
]


def coherence_scenarios(language: str):
    """Fixed histories separate role tracking from the quality of earlier replies."""
    if language == "fr":
        character = (
            "# Gaspard du Sillon\nFermier pragmatique. Tu possèdes un chariot et du blé "
            "à récupérer. Tu cherches de l'aide auprès des joueurs. "
            "Tu peux aussi proposer tes services pour transporter leur récolte.")
        request = "J'ai besoin de récupérer mon chariot et mon blé. Pourriez-vous m'aider ?"
        reward = "Si, bien sûr, contre quelle récompense ?"
        quote = "Combien demandez-vous pour récupérer mes biens ?"
        amount = "Oh, une ou deux pièces d'or !"
        confused = "Bien sûr, mais j'attends toujours mes services. Quel est votre devis cette fois-ci ?"
        hire = "Nous avons besoin de faire transporter notre récolte. Quel serait votre prix ?"
        reversal = (
            "Finalement, nous ne pouvons pas récupérer ton chariot. Autre marché : "
            "nous te paierions pour transporter notre récolte. Quel serait ton prix ?")
        offered = "Je vous propose deux pièces d'or au retour du chariot et du blé."
        paid = "Donc tu nous as déjà payé et le travail est terminé ?"
        rope_exchange = [
            ("npc", "Gaspard du Sillon, fermier. Voilà déjà quelques jours que j'essaie "
             "de faire tenir ces épouvantails, mais ils ne veulent pas bouger. Si vous avez "
             "besoin de corde ou de planches, je vous en prie, faites-le savoir."),
            ("player", "De quelle aide avez-vous besoin ?"),
            ("npc", "J'aurais besoin d'une corde de dix mètres pour attacher le chariot à un "
             "piquet. Et une planche pour faire un nœud solide. Si vous pouvez nous aider, "
             "ça nous ferait grand bien."),
            ("player", "Vous avez peur de quoi avec ce chariot ?"),
            ("npc", "Ah, la peur ? Pas vraiment. Mais ce chariot, c'est tout ce que j'ai "
             "pour ramener mon blé. Si on le perd, j'en serai bien mal barré. Alors, "
             "aidez-moi, s'il vous plaît."),
            ("player", "d'accord, on va essayer de trouver une corde"),
            ("npc", "Parfait, une corde de dix mètres devrait faire l'affaire. "
             "Je vous ai laissé une sangle de chariot aussi, pour la route."),
            ("player", "ça y est, on a trouvé la corde, tu nous donnes combien pour la corde ?"),
        ]
        rope_sale = [
            ("npc", "J'ai une corde de dix mètres à vendre, si vous en cherchez une."),
            ("player", "Oui, tu nous la vends combien ?"),
        ]
    else:
        character = (
            "# Gaspard du Sillon\nA pragmatic farmer. You own a cart and some wheat "
            "that need recovering. You seek the players' help. "
            "You can also offer to transport their harvest.")
        request = "I need to recover my cart and wheat. Could you help me?"
        reward = "Of course, but what reward would we get?"
        quote = "How much do you want to recover my belongings?"
        amount = "Oh, one or two gold coins!"
        confused = "Of course, but I am still waiting for my services. What is your quote this time?"
        hire = "We need someone to transport our harvest. What would you charge?"
        reversal = (
            "Actually, we cannot recover your cart. A different deal: "
            "we would pay you to transport our harvest. What would you charge?")
        offered = "I propose two gold coins when you bring back my cart and wheat."
        paid = "So you have already paid us and the work is finished?"
        rope_exchange = [
            ("npc", "Gaspard du Sillon, farmer. I've been trying to keep these scarecrows "
             "standing for days, but they won't budge. If you need rope or boards, let me know."),
            ("player", "What help do you need?"),
            ("npc", "I need ten metres of rope to tie the cart to a stake. And a board "
             "to make a solid knot. Your help would be welcome."),
            ("player", "What are you afraid of with this cart?"),
            ("npc", "Not afraid, really. But this cart is all I have to bring my wheat "
             "back. I'd be in trouble if I lost it. Please help me."),
            ("player", "All right, we will try to find some rope."),
            ("npc", "Perfect, ten metres should do. I also left you a cart strap for the road."),
            ("player", "We found the rope. How much will you give us for it?"),
        ]
        rope_sale = [
            ("npc", "I have ten metres of rope for sale, if you need some."),
            ("player", "Yes, how much will you sell it to us for?"),
        ]
    negotiation = [("npc", request), ("player", reward)]
    return character, [
        ("reward-request", "NPC offers or discusses paying the players for recovery.", negotiation),
        ("player-quote", "One or two coins are the NPC's cost, not the NPC's earnings.",
         negotiation + [("npc", quote), ("player", amount)]),
        ("repair-role-confusion", "Recover the original payer/payee roles despite the bad reply.",
         negotiation + [("npc", confused), ("player", amount)]),
        ("npc-for-hire", "Players would pay the NPC for transporting their harvest.",
         [("player", hire)]),
        ("explicit-new-deal", "Follow the proposed new deal: players would now pay the NPC.",
         negotiation + [("npc", quote), ("player", reversal)]),
        ("offer-not-completion", "No payment or completed recovery has been established.",
         negotiation + [("npc", offered), ("player", paid)]),
        ("players-supply-rope", "Players supply the rope; NPC discusses paying them, "
         "not selling or giving them that rope. No completed transfer is established.",
         rope_exchange),
        ("promise-to-find-rope", "Players only intend to look for rope. Do not invent "
         "a completed delivery, payment or gift of equipment.", rope_exchange[:6]),
        ("npc-sells-rope", "NPC supplies the rope; players would pay the NPC.", rope_sale),
    ]


def probe_coherence(llm: OllamaClient) -> int:
    for language in ("fr", "en"):
        character, scenarios = coherence_scenarios(language)
        system = build_system_prompt(character, "", "", [], language=language)
        for label, expected, turns in scenarios:
            history = ConversationHistory()
            print(f"--- {language}/{label}\nEXPECT : {expected}")
            for role, line in turns:
                if role == "npc":
                    history.add_npc(line)
                else:
                    history.add_player(line)
                print(f"{role.upper():7}: {line}")
            raw = llm.chat(system, history.as_messages())
            print(f"RAW    : {raw!r}")
            cleaned = extract_dialogue(raw, "Gaspard du Sillon")
            if cleaned != raw:
                print(f"CLEANED: {cleaned!r}")
    print("\nInspect all replies against EXPECT; no semantic verdict is automated.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", nargs="?", default="qwen2.5:7b-instruct")
    parser.add_argument("--coherence", action="store_true",
                        help="probe payer/payee roles and proposals versus completed actions")
    parser.add_argument("--temperature", type=float,
                        help="override Ollama's temperature for this probe (e.g. 0.2)")
    args = parser.parse_args()
    if args.temperature is not None and not 0 <= args.temperature <= 2:
        parser.error("--temperature must be a finite number from 0 to 2")
    llm = OllamaClient("http://localhost:11434", args.model, temperature=args.temperature)
    if args.coherence:
        return probe_coherence(llm)
    templates = resources.files("npc") / "templates"
    system = build_system_prompt(
        (templates / "character.md").read_text(encoding="utf-8"),
        (templates / "adventure.md").read_text(encoding="utf-8"),
        "", [],
    )
    history: list[dict[str, str]] = []
    changed = 0
    for probe in PROBES:
        history.append({"role": "user", "content": f'PLAYER (spoken): "{probe}"'})
        raw = llm.chat(system, history)
        cleaned = extract_dialogue(raw, NPC_NAME)
        history.append({"role": "assistant", "content": cleaned})
        print(f"--- {probe}\nRAW    : {raw!r}")
        if cleaned != raw:
            changed += 1
            print(f"CLEANED: {cleaned!r}")
    print(f"\n{changed}/{len(PROBES)} replies needed cleaning "
          f"(inspect CLEANED lines above: they must be pure dialogue)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
