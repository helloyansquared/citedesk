"""CiteDesk: a customer-support chatbot that answers from your documents, cites every source,
and hands the conversation to a human when it isn't sure."""

from citedesk.answer import answer_question
from citedesk.config import Settings
from citedesk.embeddings import make_embedder
from citedesk.index import KnowledgeBase
from citedesk.llm import OpenAICompatibleClient
from citedesk.schema import Answer, Citation, Message

__version__ = "0.1.0"

__all__ = [
    "Answer",
    "Citation",
    "KnowledgeBase",
    "Message",
    "OpenAICompatibleClient",
    "Settings",
    "answer_question",
    "make_embedder",
]
