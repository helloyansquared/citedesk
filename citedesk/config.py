"""Runtime settings, read from environment variables (and an optional .env file)."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

HF_ROUTER_URL = "https://router.huggingface.co/v1"
HF_INFERENCE_URL = "https://router.huggingface.co/hf-inference/models"

# Chat models served through Hugging Face Inference Providers. Availability changes over time;
# any chat model id works.
SUGGESTED_MODELS = [
    "google/gemma-4-31B-it",
    "Qwen/Qwen3.6-35B-A3B",
    "meta-llama/Llama-3.3-70B-Instruct",
]
DEFAULT_MODEL = SUGGESTED_MODELS[0]

# Multilingual, so a customer can ask in French or Chinese about English documents. Picked after
# probing the models Hugging Face serves: it separates related from unrelated text most clearly.
DEFAULT_EMBED_MODEL = "BAAI/bge-m3"

EMBED_BACKENDS = ("auto", "hf", "openai", "none")


@dataclass(frozen=True)
class Settings:
    # -- answering (any OpenAI-compatible chat API) --------------------------
    base_url: str = HF_ROUTER_URL
    api_key: str | None = None
    model: str = DEFAULT_MODEL
    temperature: float = 0.1
    max_tokens: int = 1024
    timeout: float = 90.0
    reasoning_effort: str | None = None  # e.g. "none" turns thinking off on Ollama
    json_mode: str = "auto"  # auto | schema | object | off
    # Provider-specific request fields, e.g. {"thinking": {"type": "disabled"}} for DeepSeek
    extra_body: dict | None = None

    # -- retrieval ----------------------------------------------------------
    embed_backend: str = "auto"  # auto | hf | openai | none (keyword search only)
    embed_model: str = DEFAULT_EMBED_MODEL
    embed_base_url: str | None = None  # for embed_backend=openai; defaults to base_url
    embed_api_key: str | None = None  # defaults to api_key
    top_k: int = 6
    # Below this cosine similarity (and with no keyword overlap) a question is treated as
    # "nothing relevant in the knowledge base" and handed to a human without asking the LLM
    # to improvise. None = use the embedding model's calibrated default.
    min_similarity: float | None = None

    # -- knowledge base -----------------------------------------------------
    index_dir: str = "index"
    sources: tuple[str, ...] = ("samples/loopline",)
    company: str = "Loopline E-Bikes"
    chunk_chars: int = 900
    chunk_overlap: int = 150

    # -- server -------------------------------------------------------------
    admin_key: str | None = None  # protects /reindex; without it /reindex only accepts localhost
    cors_origins: tuple[str, ...] = ("*",)
    rate_limit_per_minute: int = 20  # per client IP, for /ask and /contact
    handoff_webhook: str | None = None  # n8n / Zapier / Make webhook that receives handoff events
    webhook_secret: str | None = None  # sent as X-CiteDesk-Secret so the webhook can reject strangers
    log_path: str | None = "logs/conversations.jsonl"  # set CITEDESK_LOG_PATH= (empty) to disable
    demo_page: bool = True

    @property
    def resolved_embed_backend(self) -> str:
        if self.embed_backend != "auto":
            return self.embed_backend
        if self.embed_base_url:
            return "openai"
        if self.api_key and "huggingface.co" in self.base_url:
            return "hf"
        return "none"

    @classmethod
    def from_env(cls, env_file: str | Path | None = ".env") -> Settings:
        if env_file:
            load_dotenv(env_file)
        api_key = os.getenv("CITEDESK_API_KEY") or os.getenv("HF_TOKEN") or os.getenv("OPENAI_API_KEY") or None
        log_path = os.getenv("CITEDESK_LOG_PATH", "logs/conversations.jsonl")
        min_sim = os.getenv("CITEDESK_MIN_SIMILARITY")
        embed_backend = os.getenv("CITEDESK_EMBED_BACKEND", "auto").strip().lower()
        if embed_backend not in EMBED_BACKENDS:
            raise ValueError(f"CITEDESK_EMBED_BACKEND must be one of {EMBED_BACKENDS}, got {embed_backend!r}")
        return cls(
            base_url=os.getenv("CITEDESK_BASE_URL", HF_ROUTER_URL),
            api_key=api_key,
            model=os.getenv("CITEDESK_MODEL", DEFAULT_MODEL),
            temperature=float(os.getenv("CITEDESK_TEMPERATURE", "0.1")),
            max_tokens=int(os.getenv("CITEDESK_MAX_TOKENS", "1024")),
            timeout=float(os.getenv("CITEDESK_TIMEOUT", "90")),
            reasoning_effort=os.getenv("CITEDESK_REASONING_EFFORT") or None,
            json_mode=os.getenv("CITEDESK_JSON_MODE", "auto"),
            extra_body=_json_env("CITEDESK_EXTRA_BODY"),
            embed_backend=embed_backend,
            embed_model=os.getenv("CITEDESK_EMBED_MODEL", DEFAULT_EMBED_MODEL),
            embed_base_url=os.getenv("CITEDESK_EMBED_BASE_URL") or None,
            embed_api_key=os.getenv("CITEDESK_EMBED_API_KEY") or None,
            top_k=int(os.getenv("CITEDESK_TOP_K", "6")),
            min_similarity=float(min_sim) if min_sim else None,
            index_dir=os.getenv("CITEDESK_INDEX_DIR", "index"),
            sources=_split_list(os.getenv("CITEDESK_SOURCES", "samples/loopline")),
            company=os.getenv("CITEDESK_COMPANY", "Loopline E-Bikes"),
            chunk_chars=int(os.getenv("CITEDESK_CHUNK_CHARS", "900")),
            chunk_overlap=int(os.getenv("CITEDESK_CHUNK_OVERLAP", "150")),
            admin_key=os.getenv("CITEDESK_ADMIN_KEY") or None,
            cors_origins=_split_list(os.getenv("CITEDESK_CORS_ORIGINS", "*")),
            rate_limit_per_minute=int(os.getenv("CITEDESK_RATE_LIMIT", "20")),
            handoff_webhook=os.getenv("CITEDESK_HANDOFF_WEBHOOK") or None,
            webhook_secret=os.getenv("CITEDESK_WEBHOOK_SECRET") or None,
            log_path=log_path or None,
            demo_page=os.getenv("CITEDESK_DEMO_PAGE", "true").strip().lower() not in ("0", "false", "no", "off"),
        )


def _json_env(name: str) -> dict | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f'{name} must be a JSON object, e.g. {{"thinking": {{"type": "disabled"}}}}') from exc
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a JSON object")
    return value


def _split_list(value: str) -> tuple[str, ...]:
    """Comma- or semicolon-separated list (semicolons let Windows paths with commas through)."""
    sep = ";" if ";" in value else ","
    return tuple(part.strip() for part in value.split(sep) if part.strip())


def load_dotenv(path: str | Path) -> None:
    """Minimal .env loader: KEY=VALUE lines, never overrides real env vars."""
    path = Path(path)
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.removeprefix("export ").strip()
        value = value.strip()
        if value[:1] in ('"', "'"):
            value = value[1:].split(value[0], 1)[0]  # quoted: keep everything inside the quotes
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()  # drop inline comments
        if key and key not in os.environ:
            os.environ[key] = value
