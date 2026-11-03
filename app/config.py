"""Settings, read once from the environment. Nothing secret has a default."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    try:
        return int(raw) if raw.strip() else default
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Limits:
    """Per-address and site-wide ceilings. Each one is a setting so it can change without code."""

    copies_per_hour: int = field(default_factory=lambda: _int("LIMIT_COPIES_PER_HOUR", 6))
    copies_per_day: int = field(default_factory=lambda: _int("LIMIT_COPIES_PER_DAY", 20))
    copies_site_per_day: int = field(default_factory=lambda: _int("LIMIT_COPIES_SITE_PER_DAY", 1500))
    copies_open: int = field(default_factory=lambda: _int("LIMIT_COPIES_OPEN", 150))
    messages_per_copy: int = field(default_factory=lambda: _int("LIMIT_MESSAGES_PER_COPY", 25))
    messages_per_day: int = field(default_factory=lambda: _int("LIMIT_MESSAGES_PER_DAY", 60))
    voice_per_copy: int = field(default_factory=lambda: _int("LIMIT_VOICE_PER_COPY", 10))
    voice_per_day: int = field(default_factory=lambda: _int("LIMIT_VOICE_PER_DAY", 25))
    closes_per_copy: int = field(default_factory=lambda: _int("LIMIT_CLOSES_PER_COPY", 4))
    closes_per_day: int = field(default_factory=lambda: _int("LIMIT_CLOSES_PER_DAY", 12))
    uploads_per_copy: int = field(default_factory=lambda: _int("LIMIT_UPLOADS_PER_COPY", 8))
    # Every call to any model (the reasoning model or a Vultr inference model) counts here.
    model_calls_site_per_day: int = field(default_factory=lambda: _int("LIMIT_MODEL_CALLS_PER_DAY", 4000))
    csv_max_bytes: int = field(default_factory=lambda: _int("CSV_MAX_BYTES", 20 * 1024))
    voice_max_bytes: int = field(default_factory=lambda: _int("VOICE_MAX_BYTES", 1024 * 1024))
    voice_max_seconds: int = field(default_factory=lambda: _int("VOICE_MAX_SECONDS", 30))
    json_max_bytes: int = field(default_factory=lambda: _int("JSON_MAX_BYTES", 8 * 1024))
    message_max_chars: int = field(default_factory=lambda: _int("MESSAGE_MAX_CHARS", 600))


@dataclass(frozen=True)
class Settings:
    database_url: str = field(default_factory=lambda: os.environ.get("DATABASE_URL", ""))
    # Signs the visitor's cookie and keys the address hashes. Required outside tests.
    secret_key: str = field(default_factory=lambda: os.environ.get("SECRET_KEY", ""))
    copy_minutes: int = field(default_factory=lambda: _int("COPY_MINUTES", 120))
    # The reasoning model: any OpenAI-compatible chat endpoint with tool calling.
    llm_base_url: str = field(default_factory=lambda: os.environ.get("LLM_BASE_URL", "").rstrip("/"))
    llm_api_key: str = field(default_factory=lambda: os.environ.get("LLM_API_KEY", ""))
    llm_model: str = field(default_factory=lambda: os.environ.get("LLM_MODEL", ""))
    llm_timeout_s: int = field(default_factory=lambda: _int("LLM_TIMEOUT_SECONDS", 60))
    # Vultr Serverless Inference: speech to text, embeddings, reranking and the input screen.
    vultr_base_url: str = field(
        default_factory=lambda: os.environ.get("VULTR_INFERENCE_URL", "https://api.vultrinference.com/v1").rstrip("/")
    )
    vultr_api_key: str = field(default_factory=lambda: os.environ.get("VULTR_INFERENCE_KEY", ""))
    vultr_stt_model: str = field(default_factory=lambda: os.environ.get("VULTR_STT_MODEL", "whisper-large-v3-turbo"))
    vultr_embed_model: str = field(default_factory=lambda: os.environ.get("VULTR_EMBED_MODEL", "qwen3-embedding-4b"))
    vultr_rerank_model: str = field(default_factory=lambda: os.environ.get("VULTR_RERANK_MODEL", "bge-reranker-v2-m3"))
    vultr_safety_model: str = field(
        default_factory=lambda: os.environ.get("VULTR_SAFETY_MODEL", "nemotron-3.5-content-safety")
    )
    vultr_timeout_s: int = field(default_factory=lambda: _int("VULTR_TIMEOUT_SECONDS", 30))
    # Where the web app and the no-network parser container swap files.
    sandbox_dir: str = field(default_factory=lambda: os.environ.get("SANDBOX_DIR", ""))
    # Behind Caddy the socket peer is the proxy; the client address comes from X-Forwarded-For.
    trust_proxy: bool = field(default_factory=lambda: _bool("TRUST_PROXY", True))
    secure_cookies: bool = field(default_factory=lambda: _bool("SECURE_COOKIES", True))
    limits: Limits = field(default_factory=Limits)

    @property
    def llm_ready(self) -> bool:
        return bool(self.llm_base_url and self.llm_api_key and self.llm_model)

    @property
    def vultr_ready(self) -> bool:
        return bool(self.vultr_base_url and self.vultr_api_key)


_settings: Settings | None = None


def settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings() -> None:
    """Tests change the environment and call this to read it again."""
    global _settings
    _settings = None
