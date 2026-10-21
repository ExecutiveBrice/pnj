"""Campaign character roster: discovery, per-NPC state, and name resolution.

A campaign hosts one NPC (the legacy character.md) or many (characters/*.md).
Each roster entry is a CharacterSlot carrying ALL per-NPC state — sheet text,
conversation history, standing GM notes, its own logbook, and the turn buffer
that feeds per-NPC session summaries — so switching NPCs means swapping one
slot. Kept import-light (config + session only) so doctor.py can reuse the
discovery without pulling in the app.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .session.history import ConversationHistory
from .session.logbook import Logbook
from .session.lore import LoreFile, load_lore
from .session.secrets import PendingDisclosure, SecretsSheet


@dataclass(frozen=True)
class CharacterFile:
    stem: str
    path: Path
    logbook_path: Path
    secrets_path: Path
    lore_dir: Path
    legacy: bool  # the campaign-root character.md (keeps using logbook.md)


def discover_character_files(campaign_dir: Path) -> list[CharacterFile]:
    """Legacy character.md first (if present), then characters/*.md sorted by
    stem. characters/character.md is skipped when the legacy file exists —
    the stems would collide (doctor warns about that)."""
    found: list[CharacterFile] = []
    legacy = campaign_dir / "character.md"
    if legacy.exists():
        found.append(CharacterFile("character", legacy,
                                   campaign_dir / "logbook.md",
                                   campaign_dir / "secrets.md",
                                   campaign_dir / "lore", legacy=True))
    characters_dir = campaign_dir / "characters"
    if characters_dir.is_dir():
        for path in sorted(characters_dir.glob("*.md")):
            if path.stem == "character" and found:
                continue  # stem collision with the legacy character.md
            found.append(CharacterFile(
                path.stem, path,
                campaign_dir / "logbooks" / f"{path.stem}.md",
                campaign_dir / "secrets" / f"{path.stem}.md",
                campaign_dir / "lore" / path.stem, legacy=False))
    return found


def read_display_name(text: str, fallback: str) -> str:
    """The first '# ' heading of a character sheet, else the fallback."""
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return fallback


@dataclass
class CharacterSlot:
    """Everything that is per-NPC. The app swaps its `active` slot on /npc."""

    stem: str
    path: Path
    name: str
    character: str  # sheet text, rebuilt into the system prompt every turn
    logbook: Logbook
    history: ConversationHistory
    voice: str | None = None  # piper voice name; None = campaign default
    ooc_notes: list[str] = field(default_factory=list)
    turns: list[tuple[str, str]] = field(default_factory=list)
    player_turns: int = 0
    dirty: bool = False  # has turns not yet summarized into the logbook
    secrets_path: Path | None = None
    secrets: SecretsSheet = field(default_factory=SecretsSheet)
    secrets_error: str | None = None  # parse failure; the app surfaces it
    pending_secret: str | None = None  # id awaiting the GM's /yes or /no
    pending_disclosure: PendingDisclosure | None = None
    denied_secrets: set[str] = field(default_factory=set)  # /no'd this session
    lore_dir: Path | None = None
    lore: list[LoreFile] = field(default_factory=list)
    lore_errors: list[str] = field(default_factory=list)  # app surfaces these

    def refresh(self, config: Config) -> None:
        """Re-read the character file (/reload). Conversation state is
        deliberately preserved — only text, name, voice, secrets, and lore
        are replaced. A broken secrets file keeps the old sheet and records
        the error instead of crashing the session."""
        self.character = self.path.read_text(encoding="utf-8")
        self.name = read_display_name(self.character, self.stem)
        self.voice = config.tts.voices.get(self.stem)
        self.secrets, self.secrets_error = _load_sheet(self.secrets_path,
                                                       self.secrets)
        if self.lore_dir is not None:
            self.lore, self.lore_errors = load_lore(self.lore_dir)


def _load_sheet(path: Path | None,
                keep: SecretsSheet) -> tuple[SecretsSheet, str | None]:
    from .session.secrets import SecretsError

    if path is None:
        return keep, None
    try:
        return SecretsSheet.load(path), None
    except SecretsError as exc:
        return keep, f"{path.name}: {exc}"


def load_slot(ref: CharacterFile, config: Config) -> CharacterSlot:
    text = ref.path.read_text(encoding="utf-8")
    secrets, secrets_error = _load_sheet(ref.secrets_path, SecretsSheet())
    lore, lore_errors = load_lore(ref.lore_dir)
    return CharacterSlot(
        stem=ref.stem,
        path=ref.path,
        name=read_display_name(text, ref.stem),
        character=text,
        logbook=Logbook(ref.logbook_path),
        history=ConversationHistory(limit=config.history_limit),
        voice=config.tts.voices.get(ref.stem),
        secrets_path=ref.secrets_path,
        secrets=secrets,
        secrets_error=secrets_error,
        lore_dir=ref.lore_dir,
        lore=lore,
        lore_errors=lore_errors,
    )


def resolve_npc(roster: dict[str, CharacterSlot],
                query: str) -> CharacterSlot | list[CharacterSlot]:
    """Match by stem or display name, case-insensitive; an exact match wins,
    otherwise a unique prefix. Returns the slot, or the candidate list
    ([] = unknown, 2+ = ambiguous) so the caller can render a helpful line."""
    wanted = query.strip().lower()
    exact = [slot for slot in roster.values()
             if wanted in (slot.stem.lower(), slot.name.lower())]
    if len(exact) == 1:
        return exact[0]
    prefixed = [slot for slot in roster.values()
                if slot.stem.lower().startswith(wanted)
                or slot.name.lower().startswith(wanted)]
    if len(prefixed) == 1:
        return prefixed[0]
    return exact or prefixed


def render_turns(turns: list[tuple[str, str]]) -> str:
    """Per-NPC summarizer input, in the same shape as the session transcript."""
    return "\n\n".join(f"**{speaker}:** {text}" for speaker, text in turns)
