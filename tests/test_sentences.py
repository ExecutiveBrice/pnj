"""Token stream → speakable sentences (the streaming-TTS regrouper)."""

from npc.session.sentences import iter_sentences


def test_min_chars_merges_short_sentences():
    chunks = ["Yes. I know the man", " you seek. He drinks at the Rusted Anchor."]
    assert list(iter_sentences(chunks)) == [
        "Yes. I know the man you seek.",
        "He drinks at the Rusted Anchor.",
    ]


def test_whitespace_does_not_count_toward_minimum_sentence_length():
    chunks = ["  Yes.", " ", "Wait. ", "No. ", "Please stay."]
    assert list(iter_sentences(chunks)) == ["Yes. Wait.", "No. Please stay."]


def test_splits_across_chunk_boundaries():
    chunks = ["What brings you to", " the docks? Speak quickly, stranger."]
    assert list(iter_sentences(chunks)) == [
        "What brings you to the docks?",
        "Speak quickly, stranger.",
    ]


def test_ellipsis_question_exclamation():
    text = "Hmm… perhaps. You dare?! Ha!"
    assert list(iter_sentences([text], min_chars=1)) == [
        "Hmm…", "perhaps.", "You dare?!", "Ha!",
    ]


def test_closing_quote_stays_with_its_sentence():
    text = '"Stay close." Then run.'
    assert list(iter_sentences([text], min_chars=1)) == ['"Stay close."', "Then run."]


def test_unterminated_tail_is_flushed_at_stream_end():
    assert list(iter_sentences(["I wonder about", " you"])) == ["I wonder about you"]


def test_empty_stream_yields_nothing():
    assert list(iter_sentences([])) == []
    assert list(iter_sentences(["", "  "])) == []
