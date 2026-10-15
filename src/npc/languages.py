"""Supported conversation languages and regional character instructions."""

LANGUAGES = {
    "en": ("English", "English", "Use natural English."),
    "fr": ("Français (France)", "French", "Use French as spoken in France."),
    "fr-CH": ("Français (Suisse)", "French", "Use Swiss French vocabulary naturally, "
              "including septante and nonante when appropriate."),
    "fr-CA": ("Français (Canada / Québec)", "French", "Use Canadian French, especially "
              "natural Quebec vocabulary and idioms appropriate to the character."),
    "fr-BE": ("Français (Belgique)", "French", "Use Belgian French vocabulary naturally, "
              "including septante and nonante when appropriate."),
}


def default_voice(language: str) -> str:
    return "fr_FR-siwis-medium" if language.startswith("fr") else "en_GB-alba-medium"


def language_instruction(language: str) -> str:
    _, name, regional = LANGUAGES[language]
    return (f"Always answer in {name}, regardless of the player's language. "
            f"The reply stays in {name}, even when asked to translate into, or demonstrate "
            f"another language. {regional} Keep regional expressions subtle and authentic; "
            "avoid stereotypes, exaggerated accents and phonetic spelling. "
            "Adapt the register to the character and the setting.")
