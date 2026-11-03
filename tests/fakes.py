"""Stand-ins for the two model services, so the tests run with no network and no key."""

from __future__ import annotations

import json
from typing import Callable

from app.llm import LLM, LLMError
from app.ranking import WordRanker
from app.services import Services
from app.vultr import Screen, Vultr, VultrError


class FakeVultr(Vultr):
    def __init__(self, *, ready: bool = True, safe: bool = True, transcript: str = "", fail: bool = False):
        super().__init__()
        self._ready = ready
        self.safe = safe
        self.transcript = transcript
        self.fail = fail
        self.screened: list[str] = []
        self.heard: list[int] = []

    @property
    def ready(self) -> bool:
        return self._ready

    def _note(self, service: str) -> None:
        if self.fail:
            raise VultrError(f"{service}: down")
        self.log.append({"service": service, "model": self.models[service], "ms": 5, "status": 200})

    def transcribe(self, wav: bytes, language: str | None = None) -> str:
        self._note("stt")
        self.heard.append(len(wav))
        return self.transcript

    def screen(self, text: str) -> Screen:
        if not self._ready:
            return Screen(True, [], False)
        self._note("safety")
        self.screened.append(text)
        return Screen(self.safe, [] if self.safe else ["Fraud/Deception"], True)

    def rerank(self, query, docs):
        self._note("rerank")
        return WordRanker().scores(query, docs)


Step = dict | Callable[[list[dict], dict], dict]


class FakeLLM(LLM):
    """Answers from a script: each item is an assistant message, or a function of the messages."""

    def __init__(self, script: list[Step] | None = None, *, ready: bool = True):
        super().__init__()
        self.script = list(script or [])
        self._ready = ready
        self.calls: list[tuple[list[dict], dict]] = []

    @property
    def ready(self) -> bool:
        return self._ready

    def chat(self, messages, **kw):
        self.calls.append(([dict(m) for m in messages], kw))
        if not self._ready or not self.script:
            raise LLMError("no answer scripted")
        step = self.script.pop(0)
        return step(messages, kw) if callable(step) else step


def tool_call(name: str, args: dict | None = None, call_id: str = "c1") -> dict:
    return {"content": "", "tool_calls": [
        {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args or {})}}
    ]}


def say(text: str) -> dict:
    return {"content": text}


def services(vultr: FakeVultr | None = None, llm: FakeLLM | None = None) -> Callable[[str | None], Services]:
    v = vultr or FakeVultr()
    m = llm or FakeLLM(ready=False)
    return lambda wid: Services(v, m, WordRanker())
