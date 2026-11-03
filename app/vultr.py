"""Vultr Serverless Inference: what the agent uses Vultr's models for.

- Speech to text (whisper-large-v3-turbo) turns an owner's voice note into words.
- Embeddings (qwen3-embedding-4b) find the accounts closest to a bank line's wording.
- Reranking (bge-reranker-v2-m3) orders customers, suppliers and accounts for a bank line.
- Content safety (nemotron-3.5-content-safety) screens every message, transcript and statement
  before the reasoning model reads it.

The API is OpenAI-compatible at https://api.vultrinference.com/v1 (VULTR_INFERENCE_URL) with a
Serverless Inference key (VULTR_INFERENCE_KEY). Errors never carry the provider's text to the
visitor; the caller gets VultrError and decides what to say.
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from typing import Callable, Sequence

import httpx

from .config import settings
from .matching import Cuts


RETRIES = 2
RETRY_STATUS = {429, 500, 502, 503, 504}
RETRY_WAIT_S = 0.4


class VultrError(Exception):
    """A Vultr call failed. The message is for logs only, never for the visitor."""


@dataclass
class Screen:
    safe: bool
    categories: list[str]
    checked: bool  # False when no screen ran (no key configured)


Counter = Callable[[str], None]


class Vultr:
    def __init__(self, client: httpx.Client | None = None, count: Counter | None = None):
        s = settings()
        self.base = s.vultr_base_url
        self.key = s.vultr_api_key
        self.timeout = s.vultr_timeout_s
        self.models = {
            "stt": s.vultr_stt_model,
            "embed": s.vultr_embed_model,
            "rerank": s.vultr_rerank_model,
            "safety": s.vultr_safety_model,
        }
        self._client = client
        self._count = count or (lambda service: None)
        self.log: list[dict] = []  # what was called, for the step stream: service, model, ms

    @property
    def ready(self) -> bool:
        return bool(self.base and self.key)

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout)
        return self._client

    def _post(self, service: str, path: str, **kw) -> dict:
        if not self.ready:
            raise VultrError("no Vultr inference key")
        self._count(service)
        # A busy or briefly failing endpoint (seen once on rerank, 3 Nov 2026) gets two more tries,
        # so one 502 does not send a whole close to the word ranker.
        for attempt in range(RETRIES + 1):
            t0 = time.monotonic()
            try:
                r = self._http().post(
                    f"{self.base}{path}", headers={"Authorization": f"Bearer {self.key}"}, **kw
                )
            except httpx.TransportError as exc:
                if attempt < RETRIES:
                    time.sleep(RETRY_WAIT_S * (attempt + 1))
                    continue
                raise VultrError(f"{service}: {type(exc).__name__}") from None
            except httpx.HTTPError as exc:
                raise VultrError(f"{service}: {type(exc).__name__}") from None
            ms = int((time.monotonic() - t0) * 1000)
            self.log.append({"service": service, "model": self.models[service], "ms": ms, "status": r.status_code})
            if r.status_code in RETRY_STATUS and attempt < RETRIES:
                time.sleep(RETRY_WAIT_S * (attempt + 1))
                continue
            break
        if r.status_code != 200:
            raise VultrError(f"{service}: HTTP {r.status_code}")
        try:
            return r.json()
        except ValueError:
            raise VultrError(f"{service}: not JSON") from None

    def transcribe(self, wav: bytes, language: str | None = None) -> str:
        data = {"model": self.models["stt"], "response_format": "json"}
        if language:
            data["language"] = language
        out = self._post(
            "stt", "/audio/transcriptions", data=data, files={"file": ("note.wav", wav, "audio/wav")}
        )
        text = out.get("text")
        if not isinstance(text, str):
            raise VultrError("stt: no text")
        return " ".join(text.split())

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out = self._post("embed", "/embeddings", json={"model": self.models["embed"], "input": list(texts)})
        items = sorted(out.get("data") or [], key=lambda d: d.get("index", 0))
        vecs = [d.get("embedding") for d in items]
        if len(vecs) != len(texts) or not all(isinstance(v, list) and v for v in vecs):
            raise VultrError("embed: wrong shape")
        return vecs

    def rerank(self, query: str, docs: Sequence[str]) -> list[float]:
        if not docs:
            return []
        out = self._post(
            "rerank",
            "/rerank",
            json={
                "model": self.models["rerank"],
                "query": query,
                "documents": list(docs),
                "top_n": len(docs),
                "return_documents": False,
            },
        )
        scores = [0.0] * len(docs)
        for r in out.get("results") or []:
            i = r.get("index")
            if isinstance(i, int) and 0 <= i < len(docs):
                scores[i] = float(r.get("relevance_score") or 0.0)
        return scores

    def screen(self, text: str) -> Screen:
        """Ask the content-safety model whether a piece of input is safe to act on."""
        if not self.ready:
            return Screen(True, [], False)
        model = self.models["safety"]
        out = self._post(
            "safety",
            "/chat/completions",
            json={
                "model": model,
                "messages": [{"role": "user", "content": text[:4000]}],
                "temperature": 0.01,
                "max_tokens": 100,
            },
        )
        # An unknown model id on Vultr's chat route is answered by another model, so the reply
        # must say it came from the safety model before it counts.
        if str(out.get("model", "")).lower() != model.lower():
            raise VultrError("safety: answered by another model")
        try:
            reply = out["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            raise VultrError("safety: no reply") from None
        return parse_screen(reply)


def parse_screen(reply: str) -> Screen:
    """Read Nemotron's 'User Safety: safe|unsafe' and 'Safety Categories: ...' lines.
    Anything else counts as unsafe: a screen that cannot be read does not let input through."""
    m = re.search(r"user\s*safety\"?\s*[:=]\s*\"?(safe|unsafe)", reply, re.I)
    cats: list[str] = []
    c = re.search(r"safety\s*categories\"?\s*[:=]\s*\"?([^\n\"}]+)", reply, re.I)
    if c:
        cats = [x.strip(' ."') for x in c.group(1).split(",") if x.strip(' ."')][:6]
    if not m:
        return Screen(False, cats, True)
    return Screen(m.group(1).lower() == "safe", cats, True)


def _cos(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


# bge-reranker-v2-m3 on terse bank wording, measured on the October statement (3 Nov 2026): the right
# name scores 0.08 to 0.53 and the right account 0.016 to 0.97, while every wrong one scores 0.000 to
# 0.007. So its view counts when the first choice clears a small floor and is five times the second;
# CASH WITHDRAWAL (0.006) and INTERNET TRANSFER (0.006) stay with the owner.
RERANK_CUTS = Cuts(name_sure=0.05, name_margin=0.0, account_sure=0.01, account_margin=0.0, lead=5.0,
                   strong=0.1, weak=0.01)


class VultrRanker:
    """The matcher's ranker on Vultr: embeddings to shortlist, the reranker to score."""

    cuts = RERANK_CUTS

    def __init__(self, v: Vultr):
        self.v = v
        self.name = f"Vultr {v.models['rerank']} and {v.models['embed']}"
        self._doc_vecs: dict[tuple[str, ...], list[list[float]]] = {}

    def scores(self, query: str, docs: Sequence[str]) -> list[float]:
        return self.v.rerank(query, docs)

    def shortlist(self, queries: Sequence[str], docs: Sequence[str], k: int) -> list[list[int]]:
        key = tuple(docs)
        if key not in self._doc_vecs:
            self._doc_vecs[key] = self.v.embed(list(docs))
        dvecs = self._doc_vecs[key]
        out: list[list[int]] = []
        qvecs = self.v.embed(list(queries)) if queries else []
        for q in qvecs:
            sims = [_cos(q, d) for d in dvecs]
            out.append(sorted(range(len(docs)), key=lambda i: sims[i], reverse=True)[:k])
        return out
