"""The knowledge base: load sources, chunk, embed, save to disk, and hybrid search.

Search combines BM25 keyword ranking with embedding similarity through reciprocal rank fusion
(RRF), so exact terms ("E07", "€149") and paraphrases ("send it back" ~ "returns") both work.
No vector database is needed: a few thousand chunks fit in one NumPy matrix and a search is a
single matrix-vector product.

Re-indexing is incremental: chunks whose text did not change keep their stored vectors, so
running it nightly costs almost nothing when little changed.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from citedesk.bm25 import BM25
from citedesk.chunking import chunk_documents
from citedesk.embeddings import Embedder
from citedesk.loaders import LoaderError, is_url, iter_source_files, load_file, load_url
from citedesk.schema import Chunk, Document, Hit, IndexStats

log = logging.getLogger(__name__)

INDEX_VERSION = 1
RRF_K = 60


class KnowledgeBaseError(RuntimeError):
    pass


def load_sources(sources: Iterable[str]) -> tuple[list[Document], dict[str, str]]:
    """Load every file/folder/URL. Failures are collected rather than aborting the whole run."""
    docs: list[Document] = []
    failed: dict[str, str] = {}
    seen: set[str] = set()
    for source in sources:
        try:
            if is_url(source):
                items = [load_url(source)]
            else:
                items = []
                for path, name in iter_source_files(source):
                    try:
                        items.append(load_file(path, name))
                    except (LoaderError, OSError) as exc:
                        failed[name] = str(exc)
        except (LoaderError, OSError) as exc:
            failed[source] = str(exc)
            continue
        for doc in items:
            name, n = doc.source, 2
            while doc.source in seen:  # two folders with a faq.md each
                doc.source = f"{name} ({n})"
                n += 1
            seen.add(doc.source)
            docs.append(doc)
    return docs, failed


class KnowledgeBase:
    def __init__(
        self,
        chunks: list[Chunk],
        vectors: np.ndarray | None = None,
        *,
        embedder_name: str | None = None,
        documents: list[dict] | None = None,
        built_at: str | None = None,
    ):
        if vectors is not None and len(vectors) != len(chunks):
            raise KnowledgeBaseError(f"{len(chunks)} chunks but {len(vectors)} vectors")
        self.chunks = chunks
        self.vectors = vectors
        self.embedder_name = embedder_name if vectors is not None else None
        self.documents = documents or []
        self.built_at = built_at
        self.bm25 = BM25([c.search_text for c in chunks])

    # ------------------------------------------------------------------ build

    @classmethod
    def build(
        cls,
        sources: Iterable[str],
        embedder: Embedder | None = None,
        *,
        chunk_chars: int = 900,
        chunk_overlap: int = 150,
        previous: KnowledgeBase | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> tuple[KnowledgeBase, IndexStats]:
        started = time.perf_counter()
        say = progress or (lambda _msg: None)
        docs, failed = load_sources(sources)
        if not docs:
            detail = "; ".join(f"{k}: {v}" for k, v in failed.items()) or "no supported files found"
            raise KnowledgeBaseError(f"Nothing to index ({detail})")
        say(f"Loaded {len(docs)} document(s)" + (f", {len(failed)} failed" if failed else ""))
        chunks = chunk_documents(docs, chunk_chars, chunk_overlap)

        vectors: np.ndarray | None = None
        embedded = 0
        if embedder is not None:
            reuse: dict[str, np.ndarray] = {}
            if previous is not None and previous.vectors is not None and previous.embedder_name == embedder.name:
                reuse = {c.checksum: previous.vectors[i] for i, c in enumerate(previous.chunks)}
            todo = [i for i, c in enumerate(chunks) if c.checksum not in reuse]
            say(f"Embedding {len(todo)} new/changed chunk(s), reusing {len(chunks) - len(todo)}")
            new_vectors = embedder.embed_documents([chunks[i].search_text for i in todo]) if todo else None
            dim = new_vectors.shape[1] if new_vectors is not None else len(next(iter(reuse.values())))
            vectors = np.zeros((len(chunks), dim), dtype=np.float32)
            for i, chunk in enumerate(chunks):
                if chunk.checksum in reuse:
                    vectors[i] = reuse[chunk.checksum]
            for j, i in enumerate(todo):
                vectors[i] = new_vectors[j]  # type: ignore[index]
            embedded = len(todo)

        per_doc: dict[str, int] = {}
        for chunk in chunks:
            per_doc[chunk.source] = per_doc.get(chunk.source, 0) + 1
        documents = [
            {
                "source": d.source,
                "title": d.title,
                "checksum": d.checksum,
                "chunks": per_doc.get(d.source, 0),
                "url": d.url,
                "path": d.path,
            }
            for d in docs
        ]
        kb = cls(
            chunks,
            vectors,
            embedder_name=embedder.name if embedder is not None else None,
            documents=documents,
            built_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        stats = _compare(previous, kb)
        stats.embedded = embedded
        stats.failed = failed
        stats.seconds = round(time.perf_counter() - started, 2)
        return kb, stats

    # ------------------------------------------------------------------ persistence

    def save(self, directory: str | Path) -> None:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        with open(directory / "chunks.jsonl", "w", encoding="utf-8") as fh:
            for chunk in self.chunks:
                fh.write(json.dumps(chunk.to_dict(), ensure_ascii=False) + "\n")
        vec_path = directory / "vectors.npy"
        if self.vectors is not None:
            np.save(vec_path, self.vectors.astype(np.float32))
        elif vec_path.exists():
            vec_path.unlink()
        meta = {
            "version": INDEX_VERSION,
            "built_at": self.built_at,
            "embedder": self.embedder_name,
            "dimensions": int(self.vectors.shape[1]) if self.vectors is not None else None,
            "chunks": len(self.chunks),
            "documents": self.documents,
        }
        (directory / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    @classmethod
    def load(cls, directory: str | Path) -> KnowledgeBase:
        directory = Path(directory)
        meta_path = directory / "meta.json"
        if not meta_path.is_file():
            raise KnowledgeBaseError(f"No index in '{directory}'. Build one first: citedesk ingest")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if meta.get("version") != INDEX_VERSION:
            raise KnowledgeBaseError("The index was built by another CiteDesk version. Rebuild it: citedesk ingest")
        with open(directory / "chunks.jsonl", encoding="utf-8") as fh:
            chunks = [Chunk(**json.loads(line)) for line in fh if line.strip()]
        vec_path = directory / "vectors.npy"
        vectors = np.load(vec_path) if meta.get("embedder") and vec_path.is_file() else None
        return cls(
            chunks,
            vectors,
            embedder_name=meta.get("embedder"),
            documents=meta.get("documents", []),
            built_at=meta.get("built_at"),
        )

    # ------------------------------------------------------------------ search

    def can_use(self, embedder: Embedder | None) -> bool:
        return embedder is not None and self.vectors is not None and embedder.name == self.embedder_name

    def search(
        self,
        query: str,
        k: int = 6,
        embedder: Embedder | None = None,
        *,
        candidates: int = 30,
        mode: str = "hybrid",
    ) -> list[Hit]:
        """``mode`` is "hybrid" (default), "keyword" or "vector" (the last two exist for evaluation)."""
        if mode not in ("hybrid", "keyword", "vector"):
            raise ValueError(f"unknown search mode {mode!r}")
        if not self.chunks or not query.strip():
            return []
        keyword = self.bm25.scores(query)
        keyword_ranked = sorted(keyword.items(), key=lambda item: (-item[1], item[0]))[:candidates]
        keyword_rank = {i: r for r, (i, _) in enumerate(keyword_ranked, 1)} if mode != "vector" else {}
        if mode == "keyword":
            embedder = None

        similarity: np.ndarray | None = None
        vector_rank: dict[int, int] = {}
        if self.can_use(embedder):
            q = embedder.embed_query(query)  # type: ignore[union-attr]
            similarity = self.vectors @ q  # type: ignore[operator]
            top = np.argsort(-similarity, kind="stable")[:candidates]
            vector_rank = {int(i): r for r, i in enumerate(top, 1)}
        elif embedder is not None and self.vectors is not None:
            log.warning("Index was embedded with %s; rebuild it to use %s", self.embedder_name, embedder.name)

        fused: dict[int, float] = {}
        for ranks in (keyword_rank, vector_rank):
            for i, r in ranks.items():
                fused[i] = fused.get(i, 0.0) + 1.0 / (RRF_K + r)
        ordered = sorted(fused.items(), key=lambda item: (-item[1], item[0]))[:k]
        return [
            Hit(
                chunk=self.chunks[i],
                score=round(score, 6),
                keyword_score=round(keyword.get(i, 0.0), 4),
                similarity=round(float(similarity[i]), 4) if similarity is not None else None,
                keyword_rank=keyword_rank.get(i),
                vector_rank=vector_rank.get(i),
            )
            for i, score in ordered
        ]

    def document(self, source: str) -> dict | None:
        return next((d for d in self.documents if d["source"] == source), None)


def has_relevant_hit(hits: list[Hit], min_similarity: float) -> bool:
    """Is anything in the knowledge base plausibly about this question?

    With embeddings the similarity decides (common words like "bike" give almost every chunk a
    keyword score, so keyword overlap alone proves little). Without embeddings any keyword match
    counts.
    """
    if not hits:
        return False
    sims = [h.similarity for h in hits if h.similarity is not None]
    if sims:
        return max(sims) >= min_similarity
    return any(h.keyword_score > 0 for h in hits)


def _compare(previous: KnowledgeBase | None, current: KnowledgeBase) -> IndexStats:
    stats = IndexStats(sources=len(current.documents), chunks=len(current.chunks))
    old = {d["source"]: d["checksum"] for d in (previous.documents if previous else [])}
    new = {d["source"]: d["checksum"] for d in current.documents}
    stats.added = sorted(s for s in new if s not in old)
    stats.removed = sorted(s for s in old if s not in new)
    stats.changed = sorted(s for s in new if s in old and old[s] != new[s])
    stats.unchanged = sum(1 for s in new if s in old and old[s] == new[s])
    return stats
