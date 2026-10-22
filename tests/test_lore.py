"""Lore loading: txt/md/pdf extraction, error collection, prompt block."""

from npc.session.lore import (
    LoreFile,
    estimate_tokens,
    load_lore,
    lore_block,
)


def make_pdf(text: str) -> bytes:
    """A minimal valid one-page PDF with a single text object — offsets are
    computed, so pypdf parses it without leaning on repair heuristics."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
        + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode() + b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_pos}\n%%EOF\n").encode()
    return bytes(out)


def test_loads_txt_and_md_sorted_ignoring_others(tmp_path):
    (tmp_path / "b-region.txt").write_text("The river forks twice.",
                                           encoding="utf-8")
    (tmp_path / "a-guilds.md").write_text("# Guilds\n\nNine in total.",
                                          encoding="utf-8")
    (tmp_path / "notes.docx").write_text("ignored", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "deep.txt").write_text("ignored", encoding="utf-8")

    files, errors = load_lore(tmp_path)
    assert errors == []
    assert [f.name for f in files] == ["a-guilds.md", "b-region.txt"]
    assert files[1].text == "The river forks twice."
    assert files[1].words == 4


def test_missing_dir_is_empty(tmp_path):
    assert load_lore(tmp_path / "absent") == ([], [])


def test_pdf_extraction(tmp_path):
    (tmp_path / "monolith.pdf").write_bytes(
        make_pdf("The Kelsari raised the monolith in the year 912."))
    files, errors = load_lore(tmp_path)
    assert errors == []
    assert files[0].pages == 1
    assert "Kelsari" in files[0].text and "912" in files[0].text


def test_broken_pdf_lands_in_errors_not_exceptions(tmp_path):
    (tmp_path / "broken.pdf").write_bytes(b"%PDF-1.4 this is not a pdf")
    (tmp_path / "fine.txt").write_text("still loads", encoding="utf-8")
    files, errors = load_lore(tmp_path)
    assert [f.name for f in files] == ["fine.txt"]
    assert len(errors) == 1 and errors[0].startswith("broken.pdf:")


def test_shared_pdf_failure_is_cached_but_reported_to_each_npc(tmp_path, monkeypatch):
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"broken")
    attempts = []

    def fail(path):
        attempts.append(path)
        raise ValueError("invalid PDF")

    monkeypatch.setattr("npc.session.lore._extract_pdf", fail)
    cache = {}
    for name in ("alice", "bob"):
        files, errors = load_lore(tmp_path / name, shared_files=(path,), cache=cache)
        assert files == []
        assert errors == ["broken.pdf: invalid PDF"]
    assert attempts == [path]


def test_lore_block_framing_and_sections():
    block = lore_block([
        LoreFile("guilds.md", "Nine guilds rule the docks.", 5),
        LoreFile("empty.txt", "", 0),  # empty text → no section
    ])
    assert block.startswith("# Reference knowledge")
    assert "established fact" in block
    assert "say so in\ncharacter instead of inventing" in block
    assert "## guilds.md\nNine guilds rule the docks." in block
    assert "empty.txt" not in block


def test_estimate_tokens_heuristic():
    assert estimate_tokens("") == 0
    assert estimate_tokens("a" * 400) == 100


def test_swedish_lore_round_trips(tmp_path):
    (tmp_path / "sv.txt").write_text("Hertigens släkt härstammar från öarna.",
                                     encoding="utf-8")
    files, _ = load_lore(tmp_path)
    assert "härstammar från öarna" in files[0].text
