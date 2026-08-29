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

--- Update 2026-08-29, per Person B ---
filled_null / asked_categories override-clearing bug (raised in this
thread) is now fixed at the state.py level — clear_freeform_override
discards from both sets, not just filled_slots. No action needed here;
this file reads both exclusively through state.open_attributes()/
state.filled_null, never its own cached copy, so the fix flows through
automatically.

`feature` slot data will now populate in more Buying sessions than before
(a fix on Person A's side surfaces it). Added to SOFT_FIELDS_FOR_FIT below,
but with LOOSE token-overlap matching rather than the exact-substring check
used for material/color/size/category — per B, a feature value is a free-
text blurb (e.g. "quick dry moisture wicking"), not a single keyword, so
requiring the whole substring to appear verbatim in product text would
almost never match even when the underlying products genuinely fit.
"""
from __future__ import annotations

import math
import re

from .state import SessionState

WEIGHT_RETRIEVAL_SCORE = 1.0
WEIGHT_SLOT_FIT = 0.4
WEIGHT_RATING = 0.15
WEIGHT_POPULARITY = 0.10

# Attributes worth boosting at rank time by matching against product text.
# category/material/color/size are exact-keyword-ish and matched by
# substring containment; feature is free-text and matched by loose token
# overlap instead (see FREE_TEXT_FIELDS below). style/brand/use_case are
# still skipped — no reliable per-product signal for them yet.
SOFT_FIELDS_FOR_FIT = ("category", "material", "color", "size", "feature")

# Attributes whose slot value is a blurb, not a keyword — matched by
# shared meaningful words instead of requiring the exact phrase to appear
# verbatim in the product's text.
FREE_TEXT_FIELDS = {"feature"}

_WORD_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "for", "from",
    "in", "is", "it", "of", "on", "or", "that", "the", "this", "to", "with",
}
MIN_TOKEN_OVERLAP = 1  # at least this many shared meaningful words counts as a loose match


def _tokenize(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall(text.lower()) if len(w) > 2 and w not in _STOPWORDS}


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
    haystack_tokens: set[str] | None = None  # lazy — only tokenize if a free-text field is actually present

    bonus = 0.0
    for attribute in SOFT_FIELDS_FOR_FIT:
        if attribute not in decayed_slots:
            continue
        value, weight = decayed_slots[attribute]
        for sub_value in _split_multi_value(value):
            if attribute in FREE_TEXT_FIELDS:
                if haystack_tokens is None:
                    haystack_tokens = _tokenize(haystack)
                value_tokens = _tokenize(sub_value)
                if value_tokens and len(value_tokens & haystack_tokens) >= MIN_TOKEN_OVERLAP:
                    bonus += weight
                    break
            else:
                if sub_value in haystack:
                    bonus += weight
                    break  # count this attribute once even with multiple values
    return bonus


def rank(candidates: list[tuple[str, float]], state: SessionState, index=None) -> list[str]:
    """Order candidates best-to-worst and return up to 10 unique parent_asin.

    Called as rank(candidates, state) -> trusts the merged retrieval score
    order as-is, just dedupes defensively (matches the placeholder
    behavior exactly).

    Called as rank(candidates, state, index) -> also blends in product
    rating, review-volume popularity, and a decayed-slot text-match bonus
    per candidate, using state.decayed_slots(state.turn).
    """
    if not candidates:
        return []

    if index is None:
        ordered = sorted(candidates, key=lambda item: -item[1])
    else:
        current_turn = getattr(state, "turn", 0)
        decayed = state.decayed_slots(current_turn)
        rescored: list[tuple[str, float]] = []
        for asin, score in candidates:
            product = index.products.get(asin)
            bonus = 0.0
            if product:
                bonus += WEIGHT_RATING * _normalized_rating(product.get("average_rating"))
                bonus += WEIGHT_POPULARITY * _normalized_popularity(product.get("rating_number"))
                bonus += WEIGHT_SLOT_FIT * _slot_fit_bonus(product, decayed)
            rescored.append((asin, WEIGHT_RETRIEVAL_SCORE * score + bonus))
        ordered = sorted(rescored, key=lambda item: -item[1])

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


# ---------------------------------------------------------------------------
# NOTE FOR PERSON D — one-line change to unlock rating/popularity/slot-fit,
# against the CURRENT real agent.py's _respond():
#
#   ranked_ids = rank(candidates, state, index=self.index)
#
# Omitting index still works exactly as before (raw score order) — this is
# a pure opt-in upgrade, same pattern as clarify.py's change.
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    class _FakeIndex:
        products = {
            "A1": {"title": "cotton crew socks", "average_rating": 4.8, "rating_number": 1200},
            "A2": {"title": "wool crew socks", "average_rating": 4.0, "rating_number": 50},
            "A3": {"title": "cotton dress socks", "average_rating": 4.9, "rating_number": 3000},
        }

    fake_index = _FakeIndex()
    fake_candidates = [("A1", 0.5), ("A2", 0.9), ("A3", 0.4)]

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

    # feature loose-match: customer's free-text blurb doesn't appear
    # verbatim in any product's text, but shares meaningful words with a
    # product that has "moisture wicking quick dry" in its features.
    feature_index = _FakeIndex()
    feature_index.products = {
        "F1": {"title": "athletic socks", "features": "moisture wicking quick dry fabric", "average_rating": 4.5, "rating_number": 200},
        "F2": {"title": "dress socks", "features": "elegant formal design", "average_rating": 4.9, "rating_number": 500},
    }
    feature_candidates = [("F1", 0.5), ("F2", 0.5)]  # tied score on purpose
    feature_state = SessionState(user_profile={})
    feature_state.advance_turn(1)
    feature_state.set_slot("feature", "needs to dry quickly for running", turn=1, source="freeform")
    feature_state.advance_turn(2)
    print("Feature loose-match:      ", rank(feature_candidates, feature_state, feature_index),
          "(expect F1 first — shares 'quick'/'dry' despite no exact phrase match)")
