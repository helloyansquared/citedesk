from citedesk.chunking import chunk_document, split_text
from citedesk.schema import Document, Section
from citedesk.text import clean_whitespace, fold, tokenize


def test_fold_strips_accents_and_case():
    assert fold("Réparation ÉLECTRIQUE") == "reparation electrique"


def test_tokenize_drops_stopwords_and_trims_plurals():
    assert tokenize("What are the batteries' charging times?") == ["battery", "charging", "time"]


def test_tokenize_splits_cjk_into_bigrams_and_keeps_codes():
    tokens = tokenize("显示屏出现E03是什么意思")
    assert "e03" in tokens
    assert "显示" in tokens and "示屏" in tokens
    assert "保修" in tokenize("电池的保修期是多久")


def test_tokenize_single_cjk_character():
    assert tokenize("车") == ["车"]


def test_clean_whitespace_keeps_paragraphs():
    assert clean_whitespace("a  b\r\n\r\n\r\n\n c d ") == "a b\n\nc d"


def test_short_text_is_one_chunk():
    assert split_text("hello world", 200, 50) == ["hello world"]


def test_long_text_is_split_on_paragraphs_with_overlap():
    paras = [f"Paragraph {i}. " + "word " * 30 for i in range(8)]
    chunks = split_text("\n\n".join(paras), 400, 200)
    assert len(chunks) > 1
    assert all(len(c) <= 400 for c in chunks)
    # consecutive chunks share a paragraph (the overlap)
    assert any(p.strip() in chunks[0] and p.strip() in chunks[1] for p in paras)
    # nothing is lost
    for p in paras:
        assert any(p.strip() in c for c in chunks)


def test_overlong_line_without_punctuation_is_cut_at_spaces():
    text = "word " * 400
    chunks = split_text(text, 300, 0)
    assert all(len(c) <= 300 for c in chunks)
    assert sum(c.count("word") for c in chunks) == 400


def test_chinese_sentences_split():
    text = "这是第一句话。" * 80
    chunks = split_text(text, 250, 0)
    assert len(chunks) > 1 and all(len(c) <= 250 for c in chunks)


def test_chunk_document_ids_labels_and_checksums():
    doc = Document(
        source="manual.pdf",
        title="Manual",
        sections=[Section("3. Charging", "Charge it.", page=2), Section("", "Intro.", page=1)],
    )
    chunks = chunk_document(doc)
    assert [c.id for c in chunks] == ["manual.pdf#1", "manual.pdf#2"]
    assert chunks[0].label == "Manual › 3. Charging (p. 2)"
    assert chunks[1].label == "Manual (p. 1)"
    assert chunks[0].search_text.startswith("Manual › 3. Charging")
    assert chunks[0].checksum and chunks[0].checksum != chunks[1].checksum


def test_heading_path_uses_arrows():
    doc = Document(source="x.md", title="Help", sections=[Section("Returns > E-bikes", "Text")])
    assert chunk_document(doc)[0].label == "Help › Returns › E-bikes"
