"""The reasoning model: any OpenAI-compatible chat completions endpoint with tool calling,
set by LLM_BASE_URL, LLM_API_KEY and LLM_MODEL. It drafts records from the owner's words,
answers questions with the agent's tools, and writes the follow-ups and the month's summary.
Code checks every figure it writes against the books before anything is shown.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable

import httpx

from .config import settings


class LLMError(Exception):
    """The model could not be reached or gave nothing usable. Never shown to the visitor."""


class LLM:
    def __init__(self, client: httpx.Client | None = None, count: Callable[[str], None] | None = None):
        s = settings()
        self.base = s.llm_base_url
        self.key = s.llm_api_key
        self.model = s.llm_model
        self.timeout = s.llm_timeout_s
        self._client = client
        self._count = count or (lambda service: None)

    @property
    def ready(self) -> bool:
        return bool(self.base and self.key and self.model)

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout)
        return self._client

    def chat(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        json_schema: dict | None = None,
        max_tokens: int = 900,
        temperature: float = 0.2,
    ) -> dict:
        """One completion. Returns the assistant message (content and any tool_calls)."""
        if not self.ready:
            raise LLMError("no model configured")
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if json_schema:
            body["response_format"] = {"type": "json_schema", "json_schema": json_schema}
        self._count("llm")
        last = ""
        for attempt in range(2):
            try:
                r = self._http().post(
                    f"{self.base}/chat/completions",
                    headers={"Authorization": f"Bearer {self.key}"},
                    json=body,
                )
            except httpx.HTTPError as exc:
                last = type(exc).__name__
                time.sleep(0.8)
                continue
            if r.status_code in (429, 500, 502, 503, 504) and attempt == 0:
                last = f"HTTP {r.status_code}"
                time.sleep(1.2)
                continue
            if r.status_code != 200:
                raise LLMError(f"HTTP {r.status_code}")
            try:
                return r.json()["choices"][0]["message"]
            except (ValueError, KeyError, IndexError, TypeError):
                raise LLMError("no message") from None
        raise LLMError(last or "unreachable")

    def json(self, messages: list[dict], schema_name: str, schema: dict, **kw) -> dict:
        """A completion that must be one JSON object of the given schema."""
        msg = self.chat(messages, json_schema={"name": schema_name, "schema": schema, "strict": True}, **kw)
        text = (msg.get("content") or "").strip()
        if text.startswith("```"):
            text = text.strip("`")
            text = text[text.find("{"):]
        try:
            start, end = text.index("{"), text.rindex("}") + 1
            return json.loads(text[start:end])
        except ValueError:
            raise LLMError("not JSON") from None
