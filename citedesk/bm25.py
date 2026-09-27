"""Okapi BM25 keyword search: exact terms like "E07", "€149" or a product name, which embeddings blur."""

from __future__ import annotations

import math
from collections import Counter

from citedesk.text import tokenize


class BM25:
    def __init__(self, texts: list[str], k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.doc_len: list[int] = []
        self.postings: dict[str, list[tuple[int, int]]] = {}
        for i, text in enumerate(texts):
            counts = Counter(tokenize(text))
            self.doc_len.append(sum(counts.values()))
            for term, tf in counts.items():
                self.postings.setdefault(term, []).append((i, tf))
        self.n = len(texts)
        self.avg_len = (sum(self.doc_len) / self.n) if self.n else 0.0
        self.idf = {term: math.log(1 + (self.n - len(p) + 0.5) / (len(p) + 0.5)) for term, p in self.postings.items()}

    def scores(self, query: str) -> dict[int, float]:
        """Score of every document that shares at least one term with the query."""
        out: dict[int, float] = {}
        for term in set(tokenize(query)):
            postings = self.postings.get(term)
            if not postings:
                continue
            idf = self.idf[term]
            for i, tf in postings:
                norm = tf + self.k1 * (1 - self.b + self.b * self.doc_len[i] / (self.avg_len or 1))
                out[i] = out.get(i, 0.0) + idf * tf * (self.k1 + 1) / norm
        return out

    def search(self, query: str, k: int = 10) -> list[tuple[int, float]]:
        ranked = sorted(self.scores(query).items(), key=lambda item: (-item[1], item[0]))
        return ranked[:k]
