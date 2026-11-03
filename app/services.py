"""The model services one piece of work uses, with every call counted against the limits."""

from __future__ import annotations

from dataclasses import dataclass

from . import db, limits
from .llm import LLM
from .matching import Ranker
from .ranking import WordRanker
from .vultr import Vultr, VultrRanker


@dataclass
class Services:
    vultr: Vultr
    llm: LLM
    ranker: Ranker


def for_workspace(wid: str | None) -> Services:
    def count(service: str) -> None:
        with db.tx() as c:
            limits.model_call(c, service, wid)

    v = Vultr(count=count)
    llm = LLM(count=count)
    ranker: Ranker = VultrRanker(v) if v.ready else WordRanker()
    return Services(v, llm, ranker)


# Tests replace this with a function that returns fakes.
factory = for_workspace
