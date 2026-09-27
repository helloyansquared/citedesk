import shutil

import numpy as np
import pytest

from citedesk.index import KnowledgeBase, KnowledgeBaseError, has_relevant_hit, load_sources
from citedesk.schema import Chunk, Hit
from tests.helpers import SAMPLES, FakeEmbedder


def test_keyword_search_finds_exact_codes(keyword_kb):
    hits = keyword_kb.search("What does E07 mean?", k=3)
    assert hits[0].chunk.source == "loopline-city-2-manual.pdf"
    assert "E07" in hits[0].chunk.text
    assert hits[0].similarity is None and hits[0].keyword_rank == 1


def test_keyword_search_ignores_footer_boilerplate(keyword_kb):
    hits = keyword_kb.search("newsletter discount first order", k=6)
    assert all("Newsletter" not in h.chunk.text for h in hits)


def test_hybrid_search_uses_both_rankings(hybrid_kb):
    kb, embedder = hybrid_kb
    hits = kb.search("rent a cargo bike for a week", k=4, embedder=embedder)
    assert hits[0].chunk.source == "rental-prices.csv"
    assert hits[0].similarity is not None
    assert all(h.score > 0 for h in hits)


def test_search_modes(hybrid_kb):
    kb, embedder = hybrid_kb
    keyword = kb.search("battery", k=3, embedder=embedder, mode="keyword")
    vector = kb.search("battery", k=3, embedder=embedder, mode="vector")
    assert all(h.vector_rank is None for h in keyword)
    assert all(h.keyword_rank is None for h in vector)
    with pytest.raises(ValueError):
        kb.search("x", mode="magic")


def test_empty_query_returns_nothing(keyword_kb):
    assert keyword_kb.search("   ") == []


def test_other_embedder_falls_back_to_keywords(hybrid_kb):
    kb, _ = hybrid_kb
    other = FakeEmbedder(dim=32)
    assert not kb.can_use(other)
    hits = kb.search("E07", k=2, embedder=other)
    assert hits and hits[0].similarity is None


def test_save_load_roundtrip(tmp_path, hybrid_kb):
    kb, embedder = hybrid_kb
    kb.save(tmp_path / "idx")
    loaded = KnowledgeBase.load(tmp_path / "idx")
    assert len(loaded.chunks) == len(kb.chunks)
    assert loaded.embedder_name == embedder.name
    assert np.allclose(loaded.vectors, kb.vectors)
    assert loaded.documents == kb.documents
    q = "store the battery in winter"
    assert [h.chunk.id for h in loaded.search(q, 3, embedder)] == [h.chunk.id for h in kb.search(q, 3, embedder)]


def test_save_without_vectors_removes_stale_file(tmp_path, hybrid_kb, keyword_kb):
    hybrid_kb[0].save(tmp_path)
    keyword_kb.save(tmp_path)
    assert not (tmp_path / "vectors.npy").exists()
    assert KnowledgeBase.load(tmp_path).vectors is None


def test_load_missing_index(tmp_path):
    with pytest.raises(KnowledgeBaseError, match="citedesk ingest"):
        KnowledgeBase.load(tmp_path)


def test_incremental_reindex_reuses_vectors(tmp_path):
    docs = tmp_path / "docs"
    shutil.copytree(SAMPLES, docs)
    embedder = FakeEmbedder()
    kb, stats = KnowledgeBase.build([str(docs)], embedder)
    first = embedder.document_calls
    assert stats.added and stats.embedded == len(kb.chunks) == first

    faq = docs / "faq.md"
    faq.write_text(faq.read_text(encoding="utf-8").replace("10:00–16:00", "11:00–17:00"), encoding="utf-8")
    (docs / "rental-prices.csv").unlink()
    (docs / "new.txt").write_text("Gift cards never expire.", encoding="utf-8")

    kb2, stats2 = KnowledgeBase.build([str(docs)], embedder, previous=kb)
    assert stats2.changed == ["faq.md"]
    assert stats2.removed == ["rental-prices.csv"]
    assert stats2.added == ["new.txt"]
    assert stats2.embedded == 2  # the edited FAQ section and the new file
    assert embedder.document_calls == first + 2
    hit = kb2.search("gift card expire", 1, embedder)[0]
    assert hit.chunk.source == "new.txt"


def test_changing_embedder_re_embeds_everything(tmp_path):
    kb, _ = KnowledgeBase.build([str(SAMPLES / "faq.md")], FakeEmbedder(64))
    other = FakeEmbedder(128)
    _, stats = KnowledgeBase.build([str(SAMPLES / "faq.md")], other, previous=kb)
    assert stats.embedded == len(kb.chunks)


def test_failed_sources_are_reported_not_fatal(tmp_path):
    (tmp_path / "broken.docx").write_bytes(b"not a zip")
    (tmp_path / "ok.md").write_text("# Title\n\nHello", encoding="utf-8")
    docs, failed = load_sources([str(tmp_path), "missing-folder"])
    assert [d.source for d in docs] == ["ok.md"]
    assert set(failed) == {"broken.docx", "missing-folder"}


def test_nothing_to_index_raises(tmp_path):
    with pytest.raises(KnowledgeBaseError, match="Nothing to index"):
        KnowledgeBase.build([str(tmp_path)])


def test_duplicate_names_get_suffixes(tmp_path):
    for folder in ("a", "b"):
        (tmp_path / folder).mkdir()
        (tmp_path / folder / "faq.md").write_text(f"# {folder}\n\ntext {folder}", encoding="utf-8")
    docs, _ = load_sources([str(tmp_path / "a"), str(tmp_path / "b")])
    assert [d.source for d in docs] == ["faq.md", "faq.md (2)"]


def _hit(similarity=None, keyword=0.0):
    return Hit(chunk=Chunk("x#1", "x", "X", "", "t"), score=1.0, keyword_score=keyword, similarity=similarity)


def test_relevance_gate():
    assert not has_relevant_hit([], 0.5)
    assert has_relevant_hit([_hit(0.62)], 0.5)
    # with embeddings, keyword overlap alone ("bike") is not enough
    assert not has_relevant_hit([_hit(0.31, keyword=4.0)], 0.5)
    # keyword-only mode: any keyword match counts
    assert has_relevant_hit([_hit(None, keyword=1.2)], 0.5)
    assert not has_relevant_hit([_hit(None, keyword=0.0)], 0.5)
