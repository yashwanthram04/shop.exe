"""Final ranking: turn merged retrieval candidates into the top-10 ordered
list that actually gets scored.

Owner: Person C (paired with clarify.py). See AGENTS.md: only the first 10
valid unique parent_asin values are ever scored, and MRR rewards getting the
true target as close to #1 as possible — so this is where "good enough
recall" turns into "good ranking."
"""
from __future__ import annotations

from .state import SessionState


def rank(candidates: list[tuple[str, float]], state: SessionState) -> list[str]:
    """Order candidates best-to-worst and return up to 10 unique parent_asin.

    Placeholder: trust the merged retrieval score order as-is, just dedupe
    defensively (the evaluator already dedupes, but don't waste a slot on a
    repeat while retrieval logic is still evolving).

    TODO (Person C): fold in additional signals here — e.g. product rating,
    how well each candidate matches `state.decayed_slots()` — or replace
    this whole function with a small trained model (logistic regression /
    LightGBM over a handful of features) or an LLM/cross-encoder rerank of
    the top ~30 candidates. Keep the same return shape (list[str] of
    parent_asin, best first, len <= 10).
    """
    ordered = sorted(candidates, key=lambda item: -item[1])
    seen: set[str] = set()
    result: list[str] = []
    for asin, _score in ordered:
        if asin in seen:
            continue
        seen.add(asin)
        result.append(asin)
        if len(result) >= 10:
            break
    return result
