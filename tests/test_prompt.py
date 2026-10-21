import pytest

from npc.session.prompt import (
    asks_to_repeat,
    build_system_prompt,
    extract_dialogue,
    looks_foreign,
    reply_may_repeat,
    reply_repeats,
)
from npc.session.secrets import Secret, SecretsSheet


def sheet():
    return SecretsSheet(entries=[
        Secret(id="duke-tomb", hint="where the Duke is buried",
               body="In the salt vault."),
        Secret(id="smuggler-name", hint="who runs the shipments",
               body="Alderman Voss.", mode="deflect", revealed="session 1"),
    ])


def test_secrets_blocks_hints_locked_bodies_only_when_revealed():
    prompt = build_system_prompt("CHARSHEET", "ADV", "", [], secrets=sheet())
    # locked: hint + handling + marker listed, body absent
    assert "- topic (duke-tomb): where the Duke is buried" in prompt
    assert "[CHECK:duke-tomb]" in prompt
    assert "salt vault" not in prompt
    # revealed: body present under its id
    assert "## smuggler-name" in prompt and "Alderman Voss." in prompt
    # both blocks sit between the sheet and the adventure notes
    order = ["CHARSHEET", "Knowledge you may now share", "Locked knowledge", "ADV"]
    positions = [prompt.index(p) for p in order]
    assert positions == sorted(positions)


def test_denied_secrets_are_delisted_from_the_prompt():
    prompt = build_system_prompt("X", "", "", [], secrets=sheet(),
                                 denied={"duke-tomb"})
    # the only locked secret is denied → no locked block at all this session
    assert "Locked knowledge" not in prompt
    assert "duke-tomb" not in prompt
    # revealed knowledge is unaffected
    assert "Alderman Voss." in prompt


def test_secrets_blocks_absent_without_secrets():
    for secrets in (None, SecretsSheet()):
        prompt = build_system_prompt("X", "", "", [], secrets=secrets)
        assert "Locked knowledge" not in prompt
        assert "Knowledge you may now share" not in prompt


def test_all_sections_in_order():
    prompt = build_system_prompt("CHARSHEET", "ADVENTURE", "LOGTAIL",
                                 ["be hostile", "players stole the idol"])
    expected_order = ["You are role-playing", "CHARSHEET", "ADVENTURE", "LOGTAIL",
                      "- be hostile", "- players stole the idol"]
    positions = [prompt.index(part) for part in expected_order]
    assert positions == sorted(positions)


def test_empty_sections_are_omitted():
    prompt = build_system_prompt("CHARSHEET", "", "", [])
    assert "CHARSHEET" in prompt
    assert "Adventure notes" not in prompt
    assert "Logbook" not in prompt
    assert "Standing GM instructions" not in prompt


def test_reply_language_is_english_even_for_swedish_input():
    prompt = build_system_prompt("X", "", "", [])
    assert "Always answer in English" in prompt
    assert "Swedish" in prompt


def test_prompt_demands_voice_only_non_assistant_behavior():
    prompt = build_system_prompt("X", "", "", [])
    assert "spoken words" in prompt
    assert "not an assistant" in prompt


@pytest.mark.parametrize("raw, spoken", [
    ("Greetings, traveler.", "Greetings, traveler."),
    ("*strokes beard* Aye, that's the truth.", "Aye, that's the truth."),
    ("*He pauses.* Very well. *nods slowly*", "Very well."),
    ("(chuckles) You drive a hard bargain.", "You drive a hard bargain."),
    ("[sighs] Fine. Take it.", "Fine. Take it."),
    ("I hid it (glances at the door) under the floor.", "I hid it under the floor."),
    ('"Stay close to the lantern light."', "Stay close to the lantern light."),
    ("NPC: I know nothing of it.", "I know nothing of it."),
    ("Vess: The monolith hums at night.", "The monolith hums at night."),
    ("**Vess:** Ask the abbot.", "Ask the abbot."),
    # quoted TITLES (no terminal punctuation inside) are kept in place
    ('Well, "The Broken Crown" is what we call it.',
     'Well, "The Broken Crown" is what we call it.'),
    # real qwen2.5:7b failures captured 2026-07-16 — narration around quoted speech
    ('I raise an eyebrow, my expression stern. "A weary guardian of ancient secrets, '
     'young traveler. Approach with respect, or leave now."',
     "A weary guardian of ancient secrets, young traveler. "
     "Approach with respect, or leave now."),
    ("A glance falls upon the device you bring forth. It hums softly, like a whisper "
     'from another time. "You\'ve ventured far," I say, my voice a mix of caution and '
     'curiosity. "What is this?"',
     "You've ventured far. What is this?"),
    # attribution with lowercase continuation keeps the comma (one split sentence)
    ('"Not tonight," she whispered, "not while the moon watches."',
     "Not tonight, not while the moon watches."),
    # streaming fragments: a multi-sentence quote split apart by iter_sentences
    ('"A weary guardian of ancient secrets, young traveler.',
     "A weary guardian of ancient secrets, young traveler."),
    ('Approach with respect, or leave now."', "Approach with respect, or leave now."),
])
def test_extract_dialogue_keeps_only_the_spoken_words(raw, spoken):
    assert extract_dialogue(raw, "Vess") == spoken


def test_quoting_someone_else_loses_framing_words_known_tradeoff():
    # documented false positive: better than reading "my expression stern" aloud
    assert extract_dialogue('She said, "Bring the crown."', "Vess") == "Bring the crown."


def test_prompt_bans_first_person_narration_with_example():
    prompt = build_system_prompt("X", "", "", [])
    assert "NEVER narrate" in prompt
    assert "WRONG:" in prompt and "RIGHT:" in prompt


def test_prompt_locks_language_even_when_challenged():
    prompt = build_system_prompt("X", "", "", [])
    assert "translate into, or demonstrate another language" in prompt


@pytest.mark.parametrize("text", [
    "Ja, jag talar flera tungomål, resenär.",
    "Ja, jag talar svenska, ju.",                      # short + all-ASCII Swedish
    "Oui, je parle la langue des anciens.",
    "Ich verstehe die alte Sprache sehr gut.",
    "Claro que sí, pero el conocimiento tiene su precio.",
    # real reply captured at the table 2026-07-16 — non-Latin script is decisive
    "はい、私には日本語ができます。なぜそう尋ねるのですか？",
    "はい。",                                          # even a single CJK word
    "Да, я говорю на древнем языке.",                  # Cyrillic too
])
def test_foreign_replies_are_flagged(text):
    assert looks_foreign(text)


@pytest.mark.parametrize("text", [
    "Of course I speak the old tongue — its words are not for untrained ears.",
    "You will die here, traveler.",                    # one list collision is not enough
    "The elders called it 'månsten', long ago.",       # quoting one foreign word is fine
    "The dragon's den lies east of the spire.",
    "",
])
def test_english_replies_are_not_flagged(text):
    assert not looks_foreign(text)


def test_pure_stage_direction_falls_back_to_plain_text():
    # nothing speakable survives — better an odd line than dead air at the table
    assert extract_dialogue("*stares at you in silence*", "Vess") == "stares at you in silence"


def test_lore_block_sits_between_sheet_and_secrets():
    from npc.session.lore import LoreFile

    prompt = build_system_prompt(
        "CHARSHEET", "ADV", "", [], secrets=sheet(),
        lore=[LoreFile("guilds.md", "Nine guilds rule the docks.", 5)])
    order = ["CHARSHEET", "Reference knowledge", "Nine guilds rule the docks.",
             "Knowledge you may now share", "Locked knowledge", "ADV"]
    positions = [prompt.index(p) for p in order]
    assert positions == sorted(positions)
    # absent when there is none
    assert "Reference knowledge" not in build_system_prompt("X", "", "", [],
                                                            lore=[])


@pytest.mark.parametrize("language, regional", [
    ("fr", "France"), ("fr-CH", "Swiss"), ("fr-CA", "Quebec"), ("fr-BE", "Belgian"),
])
def test_french_variants_replace_english_rule(language, regional):
    prompt = build_system_prompt("CHARSHEET", "", "", [], language=language)
    assert "Always answer in French" in prompt
    assert "Always answer in English" not in prompt
    assert regional in prompt
    assert "stereotypes" in prompt
    assert not looks_foreign("Oui, je vous écoute. Que désirez-vous ?", language)
    assert looks_foreign("You are welcome, my friend.", language)


@pytest.mark.parametrize("language", ["fr", "fr-BE", "fr-CH", "fr-CA"])
def test_french_secret_examples_are_localized_without_exposing_locked_bodies(language):
    prompt = build_system_prompt("CHARSHEET", "", "", [], secrets=sheet(), language=language)
    assert "Give me a moment" not in prompt
    assert "[CHECK:duke-tomb]" in prompt
    assert "salt vault" not in prompt
    assert "Alderman Voss." in prompt  # previously approved knowledge stays available


@pytest.mark.parametrize("text", [
    "Hm. Give me a moment — que cela ne soit pas un sujet que je discute.",
    "Give me a moment, je vous répondrai ensuite.",
])
def test_english_stalling_in_mixed_french_is_detected(text):
    assert looks_foreign(text, "fr-BE")


def test_french_quoted_dialogue_is_kept_without_the_surrounding_narration():
    raw = ('Vess vous observe un instant. « Pourquoi toucher le monolithe ? » '
           'dit-elle calmement. « Je préfère comprendre avant d\'expérimenter. »')
    assert extract_dialogue(raw) == (
        "Pourquoi toucher le monolithe? Je préfère comprendre avant d'expérimenter.")


REFUSAL = "Je vous comprends bien, mais je ne peux pas vous permettre d'approcher de l'arche."


@pytest.mark.parametrize("reply, expected", [
    (REFUSAL, True),
    (REFUSAL.upper().replace(",", ""), True),
    ("Je vous comprends bien, mais je peux vous permettre d'approcher de l'arche.", False),
    ("Je préfère garder mes distances, mais je vous remercie de demander.", False),
    ("Non.", False),
])
def test_substantive_repetition_ignores_punctuation_but_preserves_meaning(reply, expected):
    history = [{"role": "assistant", "content": REFUSAL}]
    assert reply_repeats(reply, history) is expected


def test_copied_opening_can_diverge_into_a_new_streamed_reply():
    history = [{"role": "assistant", "content": "Non. Le monolithe est une relique sacrée."}]
    assert reply_may_repeat("Non.", history)
    assert reply_may_repeat("Non. Le monolithe est une relique sacrée.", history)
    assert not reply_may_repeat("Non. Je préfère garder mes distances.", history)
    assert not reply_repeats("Non.", history)


@pytest.mark.parametrize("text", ["Répétez, s'il vous plaît.", "Tu peux répéter ?", "Repeat that."])
def test_explicit_requests_to_repeat_are_recognized(text):
    assert asks_to_repeat(text)
