"""Final ranking: turn merged retrieval candidates into the top-10 ordered
list that actually gets scored.

Owner: Person C (paired with clarify.py). See AGENTS.md: only the first 10
valid unique parent_asin values are ever scored, and MRR rewards getting the
true target as close to #1 as possible — so this is where "good enough
recall" turns into "good ranking."

Verified 2026-08-29 directly against the real state.py/agent.py.
state.decayed_slots(current_turn) returns {attribute: (value, weight)} with
weight floored at 0.7 for "asked"-sourced slots (direct answer to our own
question) and 0.3 for "freeform"-sourced ones (inferred from unprompted
text), decaying by 0.1 per turn of age — confirmed to exactly match what
was already assumed/tested here against B's description, and now against
the real file. state.turn is a real, current field (set via
state.advance_turn(turn) at the top of every _respond() call, before rank()
runs later in that same turn), so it's read directly rather than inferred.

rank() is backward compatible with the current agent.py call site
(`rank(candidates, state)`, no index passed yet) — rating/popularity/
slot-fit boosting activates once agent.py passes `index=self.index`, see
the note at the bottom of this file.

Multi-value slot handling confirmed necessary and correct straight from
router.py's own module docstring: multiple facts disclosed in one message
get joined with "; " into a single slot value (e.g. "cotton; leather"), and
router.py explicitly says downstream consumers should split on "; " if
needed — that's exactly what _split_multi_value() below does.
"""
from __future__ import annotations

import math

from .state import SessionState

WEIGHT_RETRIEVAL_SCORE = 1.0
WEIGHT_SLOT_FIT = 0.4
WEIGHT_RATING = 0.15
WEIGHT_POPULARITY = 0.10

# Attributes worth boosting at rank time by matching against product text.
# (category/material/color/size chosen because they show up reliably in
# title/description text; style/brand/feature/use_case are too free-form
# for a naive text-contains check and are skipped until a real attribute
# table exists — same gap noted in clarify.py. category is kept here even
# though clarify.py deprioritizes *asking* about it — it's auto-filled from
# the customer's first message per router.py's CATEGORY_RE, so it's often
# present and still a useful ranking signal even though rarely worth an
# explicit follow-up question.)
SOFT_FIELDS_FOR_FIT = ("category", "material", "color", "size")


def _normalized_rating(rating) -> float:
    if not rating:
        return 0.0
    try:
        r = float(rating)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, r / 5.0))


def _normalized_popularity(rating_number) -> float:
    if not rating_number:
        return 0.0
    try:
        n = float(rating_number)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, math.log10(n + 1) / 5.0))


def _split_multi_value(value) -> list[str]:
    """A filled slot can be a '; '-joined multi-value string (router.py's
    extract_slot_values joins multiple disclosed facts this way) — split
    before matching against product text, or a multi-value slot would
    never match anything.
    """
    if not value:
        return []
    return [v.strip().lower() for v in str(value).split("; ") if v.strip()]


def _slot_fit_bonus(product: dict, decayed_slots: dict[str, tuple[str, float]]) -> float:
    if not product:
        return 0.0
    haystack = " ".join([
        str(product.get("title", "")),
        str(product.get("features", "")),
        str(product.get("description", "")),
        str(product.get("details", "")),
        str(product.get("categories", "")),
    ]).lower()
    bonus = 0.0
    for attribute in SOFT_FIELDS_FOR_FIT:
        if attribute not in decayed_slots:
            continue
        value, weight = decayed_slots[attribute]
        for sub_value in _split_multi_value(value):
            if sub_value in haystack:
                bonus += weight
                break  # count this attribute once even with multiple values
    return bonus


def rank(candidates: list[dict], state: SessionState, index=None, usage: dict | None = None) -> list[str]:
    """Order candidates best-to-worst and return up to 10 unique parent_asin.

    Each candidate from retrieval.py's retrieve() is
    `{"parent_asin": str, "score": float, "attrs": dict}` (not a bare
    (asin, score) tuple — adjusted here to match the real retrieval.py).

    Called as rank(candidates, state) -> trusts the merged retrieval score
    order as-is, just dedupes defensively.

    Called as rank(candidates, state, index) -> also blends in product
    rating, review-volume popularity, and a decayed-slot text-match bonus
    per candidate, using state.decayed_slots(state.turn) and the raw
    product record from index.products (NOT candidate["attrs"], which only
    covers material/color/style/brand/category today — _slot_fit_bonus
    searches full product text instead, so it isn't limited by that gap).

    `usage` is accepted for contract stability (agent.py passes a shared
    dict here) but currently unused — no LLM call happens in this file.
    Groq is used for message understanding in router.py instead, not
    ranking: an A/B test showed LLM reranking of the formula's own top 20
    barely moved the score (+0.003, noise-level) at real added cost/latency,
    while genuine free-text understanding was the actual gap worth an LLM.
    """
    if not candidates:
        return []

    if index is None:
        ordered = sorted(candidates, key=lambda item: -item["score"])
    else:
        current_turn = getattr(state, "turn", 0)
        decayed = state.decayed_slots(current_turn)
        rescored: list[tuple[dict, float]] = []
        for item in candidates:
            product = index.products.get(item["parent_asin"])
            bonus = 0.0
            if product:
                bonus += WEIGHT_RATING * _normalized_rating(product.get("average_rating"))
                bonus += WEIGHT_POPULARITY * _normalized_popularity(product.get("rating_number"))
                bonus += WEIGHT_SLOT_FIT * _slot_fit_bonus(product, decayed)
            rescored.append((item, WEIGHT_RETRIEVAL_SCORE * item["score"] + bonus))
        ordered = [entry for entry, _final_score in sorted(rescored, key=lambda pair: -pair[1])]

    seen: set[str] = set()
    result: list[str] = []
    for item in ordered:
        asin = item["parent_asin"]
        if asin in seen:
            continue
        seen.add(asin)
        result.append(asin)
        if len(result) >= 10:
            break
    return result


# ---------------------------------------------------------------------------
# NOTE FOR PERSON D — already wired into the real agent.py's _respond() as:
#
#   usage = {"prompt_tokens": 0, "completion_tokens": 0}
#   ranked_ids = rank(candidates, state, index=self.index, usage=usage)
#   ... use `usage` as the response's "usage" field ...
#
# `index` unlocks rating/popularity/slot-fit. `usage` stays {0, 0} here —
# router.py's LLM-based extraction is what writes real token counts into
# it now, not this file; kept as a parameter so agent.py's call site and
# the final response's "usage" field don't need to change either way.
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    class _FakeIndex:
        products = {
            "A1": {"title": "cotton crew socks", "average_rating": 4.8, "rating_number": 1200},
            "A2": {"title": "wool crew socks", "average_rating": 4.0, "rating_number": 50},
            "A3": {"title": "cotton dress socks", "average_rating": 4.9, "rating_number": 3000},
        }

    fake_index = _FakeIndex()
    fake_candidates = [
        {"parent_asin": "A1", "score": 0.5, "attrs": {}},
        {"parent_asin": "A2", "score": 0.9, "attrs": {}},
        {"parent_asin": "A3", "score": 0.4, "attrs": {}},
    ]

    fake_state = SessionState(user_profile={})
    fake_state.advance_turn(1)
    fake_state.set_slot("material", "cotton", turn=1, source="asked")  # 0.7 floor
    fake_state.advance_turn(2)
    print("Score-only (no index):     ", rank(fake_candidates, fake_state))
    print("With rating/slot-fit:      ", rank(fake_candidates, fake_state, fake_index))

    # Multi-value + mixed confidence: "cotton; wool" from freeform text
    # (0.3 floor) should still match both A1/A3 (cotton) and A2 (wool).
    fake_state_multi = SessionState(user_profile={})
    fake_state_multi.advance_turn(1)
    fake_state_multi.set_slot("material", "cotton; wool", turn=1, source="freeform")
    fake_state_multi.advance_turn(2)
    print("Multi-value slot fit:      ", rank(fake_candidates, fake_state_multi, fake_index))
