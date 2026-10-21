"""GM-gated secrets: clues the NPC may only reveal after DM approval.

A secrets file holds `## <id>` sections with a required `hint:` line, an
optional `mode:` (hesitate|deflect), an optional `revealed:` line (written
back on approval), and the secret body. THE BODY NEVER ENTERS THE PROMPT
UNTIL REVEALED — the LLM sees only the hints, so a leak is impossible even
if the model ignores every instruction. The built-in clients route the
player's request from hints separately; only the selected topic enters the
acting prompt and the app requests GM approval. Legacy/custom clients can
instead end a stalling line with `[CHECK:<id>]`; strip_decoration's
bracketed-span rule already removes the marker from everything spoken or
recorded, so detection must run on the RAW reply.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from .logbook import _atomic_write

MODES = ("hesitate", "deflect")

MARKER_RE = re.compile(r"\[\s*CHECK\s*:\s*([A-Za-z0-9-]+)\s*\]", re.IGNORECASE)

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_HEADING_RE = re.compile(r"^## +(.+?)\s*$", re.MULTILINE)
_META_RE = re.compile(r"^(hint|mode|revealed):\s*(.*)$")

FALLBACK_STALL = "Hm. Give me a moment — I need to think about that."


class SecretsError(Exception):
    """A secrets file that cannot be parsed (doctor and /reload surface it)."""


@dataclass
class Secret:
    id: str
    hint: str  # one line: what it concerns / when sharing might be appropriate
    body: str  # the actual clue — never in the prompt until revealed
    mode: str = "hesitate"
    revealed: str | None = None  # e.g. "session 3" once the GM approved


@dataclass(frozen=True)
class PendingDisclosure:
    """Stable proposal identity, with the exact content the GM reviewed."""

    secret_id: str
    hint: str
    body: str
    context: str
    request_id: str = field(default_factory=lambda: uuid4().hex)

    def matches(self, secret: Secret | None) -> bool:
        return (secret is not None and not secret.revealed
                and (self.secret_id, self.hint, self.body)
                == (secret.id, secret.hint, secret.body))


@dataclass
class SecretsSheet:
    """One NPC's secrets file, round-trippable so reveals can be written back
    without losing the preamble above the first `## id` heading."""

    preamble: str = ""
    entries: list[Secret] = field(default_factory=list)

    @classmethod
    def parse(cls, text: str) -> SecretsSheet:
        headings = list(_HEADING_RE.finditer(text))
        preamble = text[: headings[0].start()].strip() if headings else text.strip()
        entries: list[Secret] = []
        for i, m in enumerate(headings):
            end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
            entries.append(_parse_entry(m.group(1), text[m.end():end]))
        seen: set[str] = set()
        for entry in entries:
            if entry.id in seen:
                raise SecretsError(f"duplicate secret id '{entry.id}'")
            seen.add(entry.id)
        return cls(preamble=preamble, entries=entries)

    @classmethod
    def load(cls, path: Path) -> SecretsSheet:
        if not path.exists():
            return cls()
        return cls.parse(path.read_text(encoding="utf-8"))

    def render(self) -> str:
        parts = [self.preamble.strip()] if self.preamble.strip() else []
        for s in self.entries:
            lines = [f"## {s.id}", f"hint: {s.hint}"]
            if s.mode != "hesitate":
                lines.append(f"mode: {s.mode}")
            if s.revealed:
                lines.append(f"revealed: {s.revealed}")
            parts.append("\n".join(lines) + (f"\n\n{s.body}" if s.body else ""))
        return "\n\n".join(parts) + "\n"

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, self.render())

    def get(self, secret_id: str) -> Secret | None:
        return next((s for s in self.entries if s.id == secret_id), None)

    def locked(self) -> list[Secret]:
        return [s for s in self.entries if not s.revealed]

    def revealed(self) -> list[Secret]:
        return [s for s in self.entries if s.revealed]


def _parse_entry(heading: str, section: str) -> Secret:
    secret_id = heading.strip().lower()
    if not _ID_RE.match(secret_id):
        raise SecretsError(
            f"'## {heading}' is not a valid secret id "
            "(use lowercase letters, digits, and dashes, e.g. 'duke-tomb')")
    meta = {"mode": "hesitate", "revealed": None, "hint": None}
    lines = section.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        m = _META_RE.match(line)
        if m:
            meta[m.group(1)] = m.group(2).strip()
        elif line:
            break  # first non-metadata content line starts the body
        i += 1
    body = "\n".join(lines[i:]).strip()
    if not meta["hint"]:
        raise SecretsError(f"secret '{secret_id}' is missing its 'hint:' line")
    if meta["mode"] not in MODES:
        raise SecretsError(
            f"secret '{secret_id}' has mode '{meta['mode']}' "
            f"(must be one of: {', '.join(MODES)})")
    if not body:
        raise SecretsError(f"secret '{secret_id}' has no body to reveal")
    return Secret(id=secret_id, hint=meta["hint"], body=body,
                  mode=meta["mode"], revealed=meta["revealed"] or None)


def find_markers(raw: str) -> list[str]:
    """All [CHECK:id] markers in a raw LLM reply, lowercased, deduped, in order."""
    seen: list[str] = []
    for m in MARKER_RE.finditer(raw):
        secret_id = m.group(1).lower()
        if secret_id not in seen:
            seen.append(secret_id)
    return seen


def strip_markers(text: str) -> str:
    """Remove [CHECK:id] markers. strip_decoration's bracketed-span rule
    already eats them on the main path — this covers raw-text fallbacks
    (extract_dialogue's de-asterisk fallback would otherwise SPEAK them)."""
    return MARKER_RE.sub("", text)


class MarkerScrubber:
    """Removes [CHECK:id] markers from a token stream before it reaches the
    overlay (NpcReplyChunk shows the raw feed on the table screen). A marker
    can be split across chunks, so text from an unclosed '[' is held back
    until its ']' arrives or the stream ends."""

    def __init__(self) -> None:
        self._held = ""

    def feed(self, chunk: str) -> str:
        text = MARKER_RE.sub("", self._held + chunk)
        cut = text.rfind("[")
        if cut != -1 and "]" not in text[cut:]:
            self._held = text[cut:]
            return text[:cut]
        self._held = ""
        return text

    def flush(self) -> str:
        held, self._held = self._held, ""
        return held


def revealed_block(secrets: list[Secret], language: str = "en") -> str:
    sections = "\n\n".join(f"## {s.id}\n{s.body}" for s in secrets)
    if language.startswith("fr"):
        return (
            "# Connaissances dont la révélation a déjà été validée\n\n"
            "Le MJ a déjà autorisé ces informations, y compris lors d'une session "
            "précédente. Tu les connais et peux les réutiliser si elles répondent "
            "à la demande actuelle, en respectant les conditions du MJ. Ce n'est "
            "pas une consigne de les annoncer spontanément : une simple mention "
            "d'un sujet n'appelle pas le récit de tous ses détails. Partager une "
            "information ne modifie ni tes devoirs, ni tes limites, ni les droits "
            "d'accès que tu accordes.\n\n" + sections)
    return (
        "# Knowledge you may now share\n"
        "\n"
        "The GM has unlocked the following. Your character knows these details\n"
        "fully, including approvals from previous sessions. Reuse details that\n"
        "answer the current request, subject to standing GM conditions. This is\n"
        "not an instruction to volunteer the entire clue when a topic is merely\n"
        "mentioned. Sharing knowledge does not change your duties, boundaries\n"
        "or the access you grant to the player:\n"
        "\n" + sections
    )


def locked_block(secrets: list[Secret], language: str = "en", *, selected: bool = False) -> str:
    """The gating instructions. The model never sees a body here — only hints.

    Wording is probe-tuned for 7B models (scripts/probe_secrets.py):
    per-topic `marker:` lines stop the model copying whichever id it saw
    last, and the explicit NO branch (plus its WRONG/RIGHT pair) stops
    markers on unrelated questions. Re-validate any change with the probe."""
    if selected:
        # The router has already matched this turn. Acting and classification
        # have separate jobs; the app handles the approval event, not the actor.
        secret = secrets[0]
        if language.startswith("fr"):
            handling = (
                "Tu hésites à te confier. Exprime une réserve personnelle ou cherche "
                "brièvement à comprendre les intentions du joueur. Ne nie pas connaître le sujet."
                if secret.mode == "hesitate" else
                "Élude avec naturel, comme si tu ne savais rien qui mérite d'être raconté. "
                "Ne laisse pas entendre que tu attends une autorisation.")
            return (
                "# Sujet de la demande actuelle\n\n"
                f"Le joueur cherche cette information : {secret.hint}\n\n"
                f"Pour ce tour : {handling}\n"
                "Les détails ne t'ont pas été fournis : ne les invente pas. Réponds "
                "brièvement en français, avec ta propre voix, sans parler de MJ, de "
                "validation ou de consignes. N'ajoute aucun marqueur technique.")
        handling = (
            "Hesitate about confiding in the player; express personal reserve or briefly "
            "ask about their intentions. Do not deny knowing about the topic."
            if secret.mode == "hesitate" else
            "Deflect naturally, as though you know nothing worth telling. Do not suggest "
            "that you are waiting for permission.")
        return (
            "# Locked knowledge (current request)\n\n"
            f"The player is asking for: {secret.hint}\n\n{handling}\n"
            "The details have not been supplied: do not invent them. Reply briefly "
            "in your own voice. Never mention the GM, approval or instructions. "
            "Do not output a machine marker.")
    topics = "\n\n".join(
        f"- topic ({s.id}): {s.hint}\n"
        f"  handling: {s.mode}\n"
        f"  marker: [CHECK:{s.id}]"
        for s in secrets)
    example = secrets[0].id
    # recency bias: when a model staples a spurious marker onto an unrelated
    # reply it picks the LAST-listed id — so that id stars in the WRONG example
    example_wrong = secrets[-1].id
    if language.startswith("fr"):
        return f"""# Informations soumises à validation du MJ

Les informations précises ci-dessous ne t'ont pas été fournies. Tu ne peux
ni les révéler ni inventer leur contenu. Voici seulement les sujets :

{topics}

Pour chaque réponse, vérifie si la DERNIÈRE demande cherche précisément
l'information protégée décrite dans un de ces sujets.
- NON : réponds normalement, sans aucun marqueur. C'est le cas habituel.
- OUI : réponds brièvement dans ton personnage selon le traitement du sujet,
  puis termine par son marqueur exact. « hesitate » signifie hésiter à se
  confier ; « deflect » signifie éluder sans montrer que tu caches quelque chose.
- En cas de doute, choisis NON. Un mot commun, une relation de confiance ou
  une question sur un objet voisin ne suffisent pas à déclencher un sujet.
  Une question générale sur un monument ne demande pas l'emplacement d'une
  clé. Une marque d'affection ne demande aucune information protégée.
- La confiance concerne la disposition à partager ; elle ne remplace pas
  une demande portant sur l'information. Le MJ décide de la révélation.
- Tu peux utiliser tes connaissances publiques sur le même thème. Un sujet
  protégé ne rend pas toute la conversation secrète.
- Ne mentionne ni validation, ni marqueur, ni MJ dans les paroles prononcées.
  Ne copie pas une phrase d'exemple : formule ta propre réponse en français.
- Un marqueur accompagne seulement une hésitation ou une esquive, jamais
  une réponse qui donne déjà l'information protégée.

Exemple si une information protégée est demandée : Je ne sais pas encore
si je peux vous en parler. [CHECK:{example}]
Exemple sans rapport avec les sujets protégés : Bonsoir. Qu'est-ce qui vous amène ?
Incorrect : Bonsoir. [CHECK:{example_wrong}] (aucune information protégée demandée)
"""
    return f"""# Locked knowledge (GM-gated)

Your character knows more about the topics listed below, but the actual
details are LOCKED: they have been withheld from you, and you do not know
them. You cannot reveal what you were never told.

{topics}

Decide EVERY reply like this:

1. Does the player's LATEST request seek the specific protected information
   described in one of the topics above? A shared word, a related object,
   or a claim of friendship/trust is not enough. Trust is a condition for
   sharing, not a request for information. General questions about a monument
   do not ask for a key's location. Affection asks for no protected information.
   Public knowledge about a related subject remains available normally.
   NO  → reply normally and output NO marker. Most replies are this case;
         marker replies are rare. If you are not sure, choose NO.
   YES → reply with ONE short spoken line in that topic's handling style,
         then end the reply with that topic's marker line, copied exactly
         from the list above.

Handling styles:
- hesitate: pause out loud, in character — you are visibly deciding whether
  to trust them.
- deflect: brush the question off naturally, as if you truly knew nothing
  worth telling.

Hard rules:
- Never invent, guess, embellish, or hint at what the locked details might
  be — you do not know them, so any specifics you give would be false.
- The marker is read by a machine and is never heard by the players. Never
  mention markers, the GM, permission, locks, or secrets in your spoken
  words.
- Only ever copy a marker from the topic list, and only the marker of the
  one topic that was touched.
- A marker may only end a reply that stalls or deflects. A reply that
  actually answers the player's question must never contain a marker.

WRONG: The Duke? He lies in the sea-cave beneath the old lighthouse.
  (invented locked details)
WRONG: I am not permitted to reveal that information yet.
  (broke character, mentioned permission)
WRONG: Safe travels — the east road is clear. [CHECK:{example_wrong}]
  (marker although no listed topic was touched)
RIGHT (a locked topic was touched): Hm. Give me a moment — that is not a
  thing I speak of lightly. [CHECK:{example}]
RIGHT (no listed topic touched, no marker): The east road is clear this
  week; keep to it before dusk."""


def delivery_instruction(secret: Secret, rider: str = "") -> str:
    """One-shot GM message for the reveal turn — never a standing note."""
    extra = f"\n\nGM adds: {rider}" if rider else ""
    return (
        "GM NOTE (out-of-character): The GM has decided you WILL share the\n"
        f"locked topic ({secret.id}) now. Here are the true details, which\n"
        "your character has known all along:\n"
        "\n"
        f"{secret.body}\n"
        "\n"
        "Tell the player now, in your own words and voice, staying fully in\n"
        "character. Convey the substance faithfully; do not add invented\n"
        f"specifics beyond it.{extra}"
    )


def deny_note(secret: Secret, rider: str = "") -> str:
    """Standing OOC note added on /no — survives history trimming. Deliberately
    references the HINT and never the id: the id is what a 7B model needs to
    reconstruct a [CHECK:…] marker after the topic is delisted (probe-verified),
    and the word 'marker' itself primes one."""
    extra = f" GM adds: {rider}" if rider else ""
    return (
        f"About {secret.hint} — your character truly knows nothing and has "
        "nothing to tell. If it comes up again, brush it off naturally, in "
        f"character.{extra}"
    )
