"""Regroups a streaming token feed into speakable sentences.

Feeding Piper whole sentences — not tokens, not the finished reply — is what
makes streaming worth it: synthesis of the first sentence starts while the
LLM is still generating the rest.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator

# terminal punctuation, optional closing quote/bracket, then whitespace
_SENTENCE_END = re.compile(r"[.!?…]['\"’”)\]]?\s")

MIN_CHARS = 5  # merge shorter fragments into the next sentence: better prosody, fewer synth calls


def iter_sentences(chunks: Iterable[str], min_chars: int = MIN_CHARS) -> Iterator[str]:
    """Yield complete sentences as soon as the stream contains them; a final
    unterminated fragment is flushed when the stream ends."""
    buffer = ""
    for chunk in chunks:
        buffer += chunk
        while (end := _next_break(buffer, min_chars)) is not None:
            piece = buffer[:end].strip()
            buffer = buffer[end:]
            if piece:
                yield piece
    tail = buffer.strip()
    if tail:
        yield tail


def _next_break(buffer: str, min_chars: int) -> int | None:
    for match in _SENTENCE_END.finditer(buffer):
        if len(buffer[:match.end()].strip()) >= min_chars:
            return match.end()
    return None
