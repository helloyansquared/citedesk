"""Split documents into retrieval-sized chunks without cutting through the middle of a thought.

Sections stay separate (a chunk never mixes "Returns" with "Warranty"), paragraphs are packed
together up to ``max_chars``, and only paragraphs that are themselves too long are split, first
into lines and then into sentences. Consecutive chunks of one section overlap a little so a fact
that straddles a boundary is still found.
"""

from __future__ import annotations

import hashlib
import re

from citedesk.schema import Chunk, Document

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(«])|(?<=[。！？])")


def chunk_documents(docs: list[Document], max_chars: int = 900, overlap: int = 150) -> list[Chunk]:
    chunks: list[Chunk] = []
    for doc in docs:
        chunks.extend(chunk_document(doc, max_chars, overlap))
    return chunks


def chunk_document(doc: Document, max_chars: int = 900, overlap: int = 150) -> list[Chunk]:
    if max_chars < 200:
        raise ValueError("max_chars must be at least 200")
    overlap = max(0, min(overlap, max_chars // 3))
    chunks: list[Chunk] = []
    for section in doc.sections:
        for text in split_text(section.text, max_chars, overlap):
            chunk = Chunk(
                id=f"{doc.source}#{len(chunks) + 1}",
                source=doc.source,
                title=doc.title,
                heading=section.heading,
                text=text,
                page=section.page,
                url=doc.url,
            )
            chunk.checksum = hashlib.sha1(chunk.search_text.encode("utf-8")).hexdigest()[:16]
            chunks.append(chunk)
    return chunks


def split_text(text: str, max_chars: int, overlap: int) -> list[str]:
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]
    units = _units(text, max_chars)
    out: list[str] = []
    current: list[str] = []
    size = 0
    for unit in units:
        extra = len(unit) + (1 if current else 0)
        if current and size + extra > max_chars:
            out.append(_join(current))
            current = _tail(current, overlap)
            size = len(_join(current)) if current else 0
            if current and size + 1 + len(unit) > max_chars:
                current, size = [], 0  # the overlap would push this chunk over the limit
            extra = len(unit) + (1 if current else 0)
        current.append(unit)
        size += extra
    if current:
        out.append(_join(current))
    return out


def _units(text: str, max_chars: int) -> list[str]:
    """Paragraphs; over-long paragraphs become lines; over-long lines become sentences/words."""
    units: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        if len(para) <= max_chars:
            units.append(para)
            continue
        for line in para.split("\n"):
            line = line.strip()
            if not line:
                continue
            if len(line) <= max_chars:
                units.append(line)
                continue
            for sentence in _SENTENCE_END.split(line):
                sentence = sentence.strip()
                while len(sentence) > max_chars:  # no punctuation at all: cut at a space
                    cut = sentence.rfind(" ", 0, max_chars)
                    cut = cut if cut > max_chars // 2 else max_chars
                    units.append(sentence[:cut].strip())
                    sentence = sentence[cut:].strip()
                if sentence:
                    units.append(sentence)
    return units


def _join(units: list[str]) -> str:
    return "\n".join(units)


def _tail(units: list[str], overlap: int) -> list[str]:
    """The last whole units that fit in the overlap budget."""
    tail: list[str] = []
    size = 0
    for unit in reversed(units):
        if size + len(unit) > overlap:
            break
        tail.insert(0, unit)
        size += len(unit) + 1
    return tail
