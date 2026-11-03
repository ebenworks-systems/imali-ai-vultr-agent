"""Rankers for the matcher. `VultrRanker` (in vultr.py) is the one the live app uses: Vultr's
embedding model shortlists accounts and its reranker scores names. `WordRanker` here needs no
network and is what the app falls back to when Vultr cannot be reached, and what the tests use.
Both only order things; the matcher's code decides.
"""

from __future__ import annotations

import re
from typing import Sequence

from .matching import WORD_CUTS

# Words that every bank line carries and that say nothing about who or what it was.
_STOP = {
    "EFT", "CREDIT", "DEBIT", "ORDER", "PAYMENT", "TRANSFER", "INTERNET", "BRANCH", "CARD", "REF",
    "INV", "OCT", "OCTOBER", "AND", "THE", "FOR", "OF", "TO", "FROM", "A", "PTY", "LTD", "JOB",
}


def words(text: str) -> list[str]:
    out = []
    for w in re.findall(r"[A-Za-z]+", text.upper()):
        if len(w) >= 2 and w not in _STOP:
            out.append(w)
    return out


def _word_score(q: str, doc_words: list[str], initials: str) -> float:
    best = 0.0
    for d in doc_words:
        if q == d:
            return 1.0
        if len(q) >= 3 and len(d) >= 3 and (d.startswith(q) or q.startswith(d)):
            best = max(best, 0.85)
    if len(q) >= 2 and q in initials:
        best = max(best, 0.7)
    return best


class WordRanker:
    """Scores by shared words, word stems and initials ("FOURIE SF" for Fourie Steel Fixers)."""

    name = "word match"
    cuts = WORD_CUTS

    def scores(self, query: str, docs: Sequence[str]) -> list[float]:
        q = words(query)
        out = []
        for doc in docs:
            dw = words(doc.split(":")[0]) + words(doc.split(":", 1)[1]) if ":" in doc else words(doc)
            if not q or not dw:
                out.append(0.0)
                continue
            head = words(doc.split(":")[0]) if ":" in doc else dw
            initials = "".join(w[0] for w in head)
            pairs = [_word_score(w, dw, initials) for w in q]
            q_cov = sum(pairs) / len(q)
            hits = sum(1 for d in head if any(_word_score(w, [d], "") > 0 for w in q))
            d_cov = hits / len(head) if head else 0.0
            out.append(round(0.6 * q_cov + 0.4 * d_cov, 4))
        return out

    def shortlist(self, queries: Sequence[str], docs: Sequence[str], k: int) -> list[list[int]]:
        out = []
        for q in queries:
            s = self.scores(q, docs)
            out.append(sorted(range(len(docs)), key=lambda i: s[i], reverse=True)[:k])
        return out
