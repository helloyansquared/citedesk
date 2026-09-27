"""Data models: internal (documents, chunks, search hits) and public (the answer returned by the API)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------- internal


@dataclass
class Section:
    """A piece of a document under one heading (or one PDF page)."""

    heading: str  # "Returns > E-bikes"; empty when the document has no structure
    text: str
    page: int | None = None


@dataclass
class Document:
    source: str  # relative path or URL, shown to the customer as the citation
    title: str
    sections: list[Section]
    url: str | None = None  # link a citation can open (web pages, or a public URL for a file)
    checksum: str = ""  # of the raw content, for incremental re-indexing
    path: str | None = None  # absolute local path, so the server can let customers open the file


@dataclass
class Chunk:
    id: str  # "<source>#<n>"
    source: str
    title: str
    heading: str
    text: str
    page: int | None = None
    url: str | None = None
    checksum: str = ""  # of the text that gets embedded; unchanged checksum = reuse the old vector

    @property
    def label(self) -> str:
        """Human-readable location, e.g. 'Warranty policy › Batteries (p. 2)'."""
        parts = [self.title]
        if self.heading and self.heading != self.title:
            parts.append(self.heading.replace(" > ", " › "))
        label = " › ".join(parts)
        return f"{label} (p. {self.page})" if self.page else label

    @property
    def search_text(self) -> str:
        """What gets indexed: the location gives short chunks their context ("Returns", "Warranty"...)."""
        return f"{self.label}\n{self.text}"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Hit:
    chunk: Chunk
    score: float  # fused rank score (higher is better)
    keyword_score: float = 0.0  # BM25
    similarity: float | None = None  # cosine similarity, None without embeddings
    keyword_rank: int | None = None
    vector_rank: int | None = None


@dataclass
class IndexStats:
    sources: int = 0
    chunks: int = 0
    added: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    unchanged: int = 0
    embedded: int = 0  # chunks that needed a new embedding (the rest were reused)
    failed: dict[str, str] = field(default_factory=dict)
    seconds: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- public

Status = Literal["answered", "partial", "not_found", "handoff", "off_topic", "smalltalk"]


class Citation(BaseModel):
    n: int = Field(description="The number used in the answer text, e.g. 1 for [1]")
    source: str
    title: str
    location: str = Field(description="Document › section (page)")
    page: int | None = None
    url: str | None = None
    snippet: str


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=4000)


class Answer(BaseModel):
    question: str
    answer: str
    status: Status
    needs_human: bool
    handoff_reason: str | None = None
    citations: list[Citation] = Field(default_factory=list)
    model: str = ""
    latency_ms: int = 0
    retrieved: list[str] = Field(default_factory=list, description="chunk ids given to the model, best first")


class LLMReply(BaseModel):
    """What the model is asked to return (as JSON)."""

    answer: str
    sources: list[int] = Field(default_factory=list)
    status: Status = "answered"
    handoff_reason: str | None = None
