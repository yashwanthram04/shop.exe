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
NOTE (verified during merge): the comment below originally claimed
clear_freeform_override() discards from filled_null/asked_categories too,
not just filled_slots — checked state.py directly, it does not (only
touches filled_slots/slot_turn/slot_source). Either that fix didn't get
committed, or landed somewhere this merge didn't pick up. Flagging rather
than guessing at the intended fix — confirm with Person B what was
actually meant/changed.

`feature` slot data will now populate in more Buying sessions than before
(a fix on Person A's side surfaces it). Added to SOFT_FIELDS_FOR_FIT below,
but with LOOSE token-overlap matching rather than the exact-substring check
used for material/color/size/category — per B, a feature value is a free-
text blurb (e.g. "quick dry moisture wicking"), not a single keyword, so
requiring the whole substring to appear verbatim in product text would
almost never match even when the underlying products genuinely fit.
"""
from __future__ import annotations

import json
import math
import os
import re

from .state import SessionState

WEIGHT_RETRIEVAL_SCORE = 0.15  # see ISSUES.md #14/#19 for the sweeps that picked this value
WEIGHT_SLOT_FIT = 0.5  # see ISSUES.md #26/#27 -- 0.3-0.6 tie at cap=2.5, 0.5 kept mid-plateau
SLOT_FIT_CAP = 2.5  # see ISSUES.md #27 -- re-tuned after merging with the category-matching fix (Issue 22's neighbor); 1.0 was calibrated before category contributed to the sum
WEIGHT_RATING = 0.15
WEIGHT_POPULARITY = 0.10
WEIGHT_VERBATIM = 0.0  # see ISSUES.md #20 for the sweep that picks this value -- tested, rejected, kept off
WEIGHT_PROFILE_FIT = 0.0  # see ISSUES.md #21 for the sweep that picks this value
WEIGHT_BUCKET_POPULARITY = 0.30  # see ISSUES.md #24 -- bucket-mode only, not the default formula
WEIGHT_BUCKET_OVERLAP = 0.3  # see ISSUES.md #24 -- best measured weight; bucket mode itself is opt-in only

# Long-term preference_tags (from reset()'s user_profile, never the current
# turn's slots) are single words ("fit", "comfort", "durability", ...) that
# don't literally appear in catalog text as-is -- expand each to a few
# related words/phrases that actually show up in real listings, so the
# match has something to find. Deliberately small and literal (no stemming/
# fuzzy matching) to keep this auditable against ISSUES.md #21's measurement.
PROFILE_TAG_SYNONYMS: dict[str, tuple[str, ...]] = {
    "fit": ("true to size", "fitted", "relaxed fit", "regular fit", "slim fit"),
    "comfort": ("comfortable", "comfort", "soft", "cushioned", "breathable"),
    "durability": ("durable", "sturdy", "long-lasting", "heavy duty", "high quality"),
    "style": ("stylish", "fashion", "trendy", "classic", "elegant"),
    "material": ("premium material", "quality fabric", "100%"),
    "performance": ("moisture wicking", "quick dry", "athletic", "performance"),
    "warmth": ("warm", "insulated", "fleece lined", "thermal"),
    "weather": ("waterproof", "windproof", "water resistant", "all weather"),
}

# Narrow LLM role #3 (ISSUES.md #16). Reuses router.py's Groq setup (same
# OpenAI-SDK-compatible endpoint) rather than importing from it, to keep
# rank.py's only cross-team dependency on state.py.
GROQ_MODEL = "openai/gpt-oss-20b"
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
GROQ_TIMEOUT_SECONDS = 6
LLM_RERANK_POOL = 25  # shortlist size handed to the LLM -- keeps the prompt small/cheap
LLM_RERANK_SYSTEM_PROMPT = (
    "You rank shopping products for one customer, combining their CURRENT "
    "stated preferences with their LONG-TERM shopping profile (past rating "
    "behavior, purchase frequency, general preference tags). Reply with "
    "ONLY a JSON array of the given product IDs, reordered best-match-first. "
    "Include every ID exactly once. No other text."
)
# ISSUES.md #17: this system's customer preferences are frequently VERBATIM
# QUOTES lifted directly from the correct product's own listing text (this
# is how the underlying evaluator generates them). Each candidate below is
# tagged with verbatim_overlap: N — how many of the customer's own exact
# words appear in THAT product's own text. Treat a high verbatim_overlap as
# strong evidence of correctness, even over a candidate that otherwise
# seems like a more generic/plausible fit.
LLM_RERANK_SYSTEM_PROMPT_V2 = LLM_RERANK_SYSTEM_PROMPT + (
    " IMPORTANT: this customer's stated preferences are frequently exact "
    "quotes copied from the correct product's own listing text. Each "
    "candidate is tagged with verbatim_overlap: N, the count of the "
    "customer's own words that appear verbatim in that product's text. "
    "Weight verbatim_overlap heavily — a high count is strong evidence of "
    "the correct answer, more reliable than general plausibility."
)

# Attributes worth boosting at rank time by matching against product text.
# material/color/size are exact-keyword-ish and matched by substring
# containment; category/feature are free-text blurbs and matched by loose
# token overlap instead (see FREE_TEXT_FIELDS below). style/brand/use_case
# are still skipped — no reliable per-product signal for them yet.
SOFT_FIELDS_FOR_FIT = ("category", "material", "color", "size", "feature")

# Attributes whose slot value is a blurb, not a single keyword — matched by
# shared meaningful words instead of requiring the exact phrase to appear
# verbatim in the product's text.
#
# `category` was previously matched by exact substring containment like
# material/color, but a customer-phrased category is a multi-word phrase
# (e.g. "tees & blouses tunics") that essentially never appears verbatim in
# product text — silently zeroing this bonus for nearly every session,
# worst on turn-1 hits where category is the only known slot (confirmed:
# public_0041 hit turn 1 at rank 9 with bonus_slot=0.000 across the entire
# top-10). retrieval.py's own filter_candidates() already solves this exact
# problem correctly via word-overlap containment — this fixes the same gap
# here by reusing the token-overlap path already proven for `feature`.
FREE_TEXT_FIELDS = {"category", "feature"}

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
    if SLOT_FIT_CAP is not None:
        bonus = min(bonus, SLOT_FIT_CAP)
    return bonus


def _profile_summary(user_profile: dict) -> str:
    """Flatten the anonymized long-term profile from reset() into one
    line. Every field here is accepted into SessionState.__init__ but was,
    until this function existed, never read anywhere else in the codebase
    — a real gap against the brief's "long-term user profiles" /
    Personalized Context Distillation pillar (ISSUES.md #16)."""
    if not user_profile:
        return "no purchase history available"
    parts = []
    if user_profile.get("purchase_frequency"):
        parts.append(f"purchase frequency: {user_profile['purchase_frequency']}")
    if user_profile.get("average_prior_rating") is not None:
        parts.append(f"average rating given: {user_profile['average_prior_rating']}")
    if user_profile.get("rating_style"):
        parts.append(f"rating style: {user_profile['rating_style']}")
    if user_profile.get("preference_tags"):
        parts.append(f"general preferences: {', '.join(user_profile['preference_tags'])}")
    return "; ".join(parts) if parts else "no purchase history available"


def _verbatim_overlap(customer_text: str, product: dict) -> int:
    """Count of the customer's own meaningful words that appear verbatim in
    this product's own text — the explicit signal handed to the LLM in
    Issue 17, computed the same way _slot_fit_bonus already reasons
    internally but exposed as a visible number instead of a hidden score.
    """
    customer_tokens = _tokenize(customer_text)
    if not customer_tokens:
        return 0
    haystack = " ".join([
        str(product.get("title", "")),
        str(product.get("features", "")),
        str(product.get("description", "")),
        str(product.get("details", "")),
        str(product.get("categories", "")),
    ])
    product_tokens = _tokenize(haystack)
    return len(customer_tokens & product_tokens)


def _verbatim_bonus(customer_tokens: set[str], product: dict) -> float:
    """Deterministic counterpart to the signal handed to the LLM in Issue
    17 (`_verbatim_overlap`): fraction of the customer's own meaningful
    words that appear verbatim in this product's own text, in [0, 1].
    Encodes the same evaluator mechanism the LLM was only told about --
    see ISSUES.md #20 for why encoding it directly beat asking a model to
    approximate it.
    """
    if not customer_tokens:
        return 0.0
    haystack = " ".join([
        str(product.get("title", "")),
        str(product.get("features", "")),
        str(product.get("description", "")),
        str(product.get("details", "")),
        str(product.get("categories", "")),
    ])
    product_tokens = _tokenize(haystack)
    return len(customer_tokens & product_tokens) / len(customer_tokens)


def _profile_tag_bonus(product: dict, preference_tags) -> float:
    """Fraction of the customer's LONG-TERM preference_tags (from reset()'s
    user_profile, distinct from anything said this session) whose expanded
    synonyms appear in this product's own text, in [0, 1]. The only place
    in the pipeline that reads user_profile for scoring, not just an LLM
    prompt (see ISSUES.md #21: user_profile was previously accepted and
    stored but never actually consulted outside the opt-in LLM reranker).
    """
    if not preference_tags:
        return 0.0
    haystack = " ".join([
        str(product.get("title", "")),
        str(product.get("features", "")),
        str(product.get("description", "")),
    ]).lower()
    matched = 0
    total = 0
    for tag in preference_tags:
        synonyms = PROFILE_TAG_SYNONYMS.get(str(tag).lower())
        if not synonyms:
            continue
        total += 1
        if any(syn in haystack for syn in synonyms):
            matched += 1
    return matched / total if total else 0.0


def llm_rerank(ordered: list[dict], state: SessionState, index, usage: dict | None) -> list[dict]:
    """Narrow LLM role #3 (ISSUES.md #16, v2 in #17): re-rank the already
    formula-sorted shortlist using BOTH current-turn slots AND the
    long-term user_profile — distinct from the two roles already measured
    negative (extraction, Issue 9; query-text rewriting, Issue 15). This
    one only reorders a shortlist the formula ranker already produced,
    using richer context (the profile) than the formula alone has access
    to, rather than replacing any existing signal.

    v2 (Issue 17): the v1 prompt asked the LLM to judge general plausibility
    from a title/attrs summary and lost badly (0.845 -> 0.785, MRR
    collapsed) — traced to this evaluator defining "correct" as verbatim
    text reuse from the target's own listing, which general-plausibility
    reasoning can't see. v2 computes that overlap explicitly
    (_verbatim_overlap) and hands it to the model as a visible number per
    candidate, with an instruction to weight it heavily — the same signal
    the formula ranker already exploits, now legible to the LLM too,
    instead of asking it to rediscover it from vibes.

    Falls back to `ordered` unchanged on ANY failure — no opt-in, no key,
    network error, timeout, malformed/hallucinated response. Must never
    make the result WORSE than the formula alone, only optionally better.
    """
    if os.environ.get("USE_LLM_RERANK", "").strip().lower() not in ("1", "true", "yes"):
        return ordered
    if not os.environ.get("GROQ_API_KEY"):
        return ordered
    pool = ordered[:LLM_RERANK_POOL]
    if len(pool) < 2:
        return ordered
    try:
        from openai import OpenAI

        customer_text = state.durable_notes or state.summary()
        listing_lines = []
        for item in pool:
            product = index.products.get(item["parent_asin"], {}) if index else {}
            title = str(product.get("title", ""))[:80]
            price = product.get("price")
            rating = product.get("average_rating")
            overlap = _verbatim_overlap(customer_text, product)
            listing_lines.append(
                f"{item['parent_asin']}: {title} | price={price} | rating={rating} "
                f"| attrs={item.get('attrs', {})} | verbatim_overlap={overlap}"
            )

        client = OpenAI(api_key=os.environ["GROQ_API_KEY"], base_url=GROQ_BASE_URL, timeout=GROQ_TIMEOUT_SECONDS)
        response = client.chat.completions.create(
            model=GROQ_MODEL,
            temperature=0,
            reasoning_effort="low",
            messages=[
                {"role": "system", "content": LLM_RERANK_SYSTEM_PROMPT_V2},
                {
                    "role": "user",
                    "content": (
                        f"Current stated preferences: {state.summary()}\n"
                        f"Long-term profile: {_profile_summary(state.user_profile)}\n\n"
                        f"Candidates:\n" + "\n".join(listing_lines)
                    ),
                },
            ],
        )

        if usage is not None and response.usage:
            usage["prompt_tokens"] = usage.get("prompt_tokens", 0) + response.usage.prompt_tokens
            usage["completion_tokens"] = usage.get("completion_tokens", 0) + response.usage.completion_tokens

        content = (response.choices[0].message.content or "").strip()
        if content.startswith("```"):
            content = content.strip("`")
            content = content.split("\n", 1)[-1] if "\n" in content else content
        llm_order = json.loads(content)
        if not isinstance(llm_order, list):
            return ordered

        by_asin = {item["parent_asin"]: item for item in pool}
        reordered = [by_asin[asin] for asin in llm_order if isinstance(asin, str) and asin in by_asin]
        seen_asins = {item["parent_asin"] for item in reordered}
        reordered.extend(item for item in pool if item["parent_asin"] not in seen_asins)
        return reordered + ordered[LLM_RERANK_POOL:]
    except Exception:
        return ordered


def _bucket_rank_score(customer_tokens: set[str], product: dict) -> float:
    """ISSUES.md #24: within a category-bucket-restricted pool, the target
    is a strong review-count popularity outlier (median rating_number 6,846
    vs the catalog's 12) and the pool is small/homogeneous enough that
    plain word-overlap with everything the customer has said beats the
    full slot-fit/retrieval-score/rating blend used outside the bucket
    (measured: rating STARS are flat-to-negative here, only review COUNT
    carries signal -- see ISSUES.md #24 part 4d). Deliberately simple and
    separate from the default formula below, not a replacement for it.
    """
    overlap = _verbatim_bonus(customer_tokens, product)
    popularity = _normalized_popularity(product.get("rating_number"))
    return WEIGHT_BUCKET_OVERLAP * overlap + WEIGHT_BUCKET_POPULARITY * popularity


def rank(
    candidates: list[dict],
    state: SessionState,
    index=None,
    usage: dict | None = None,
    bucket_mode: bool = False,
) -> list[str]:
    """Order candidates best-to-worst and return up to 10 unique parent_asin.

    Each candidate from retrieval.py's retrieve() is
    `{"parent_asin": str, "score": float, "attrs": dict}` (not a bare
    (asin, score) tuple).

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

    `bucket_mode=True` (ISSUES.md #24): candidates already came from
    retrieval.py's category-bucket path, not the hybrid fusion — uses
    `_bucket_rank_score` instead of the blend below. Only meaningful with
    `index` also set; ignored otherwise.
    """
    if not candidates:
        return []

    if bucket_mode and index is not None:
        customer_tokens = _tokenize(state.durable_notes or state.summary())
        rescored = [
            (item, _bucket_rank_score(customer_tokens, index.products.get(item["parent_asin"], {})))
            for item in candidates
        ]
        ordered = [entry for entry, _final_score in sorted(rescored, key=lambda pair: -pair[1])]
    elif index is None:
        ordered = sorted(candidates, key=lambda item: -item["score"])
    else:
        # Min-max normalize retrieval score to [0, 1] within this turn's
        # pool before blending with the bonuses (ISSUES.md #14). Raw RRF
        # scores (retrieval.py's `weight / (RRF_K + rank)`, RRF_K=60) only
        # span ~0.003-0.013 — two orders of magnitude below the bonus terms'
        # 0-0.65 range — so WEIGHT_RETRIEVAL_SCORE=1.0 was nearly inert:
        # final order was decided almost entirely by rating/popularity/
        # slot-fit, not by how well a candidate actually matched the query.
        # Invisible at top_n=50 (a small pool has few "similar enough"
        # lookalikes competing on generic bonuses), it became the dominant
        # effect once top_n grew to 250 (Issue 13): 46 of 198 hits landed at
        # rank 6-10, because plausible-but-wrong products with a good rating
        # or a shared material were routinely outscoring the actual target
        # on relevance to the specific query.
        raw_scores = [item["score"] for item in candidates]
        lo, hi = min(raw_scores), max(raw_scores)
        spread = hi - lo

        current_turn = getattr(state, "turn", 0)
        decayed = state.decayed_slots(current_turn)
        customer_tokens = _tokenize(state.durable_notes or state.summary()) if WEIGHT_VERBATIM else set()
        preference_tags = (state.user_profile or {}).get("preference_tags") if WEIGHT_PROFILE_FIT else None
        rescored: list[tuple[dict, float]] = []
        for item in candidates:
            normalized_score = (item["score"] - lo) / spread if spread > 0 else 1.0
            product = index.products.get(item["parent_asin"])
            bonus = 0.0
            if product:
                bonus += WEIGHT_RATING * _normalized_rating(product.get("average_rating"))
                bonus += WEIGHT_POPULARITY * _normalized_popularity(product.get("rating_number"))
                bonus += WEIGHT_SLOT_FIT * _slot_fit_bonus(product, decayed)
                if WEIGHT_VERBATIM:
                    bonus += WEIGHT_VERBATIM * _verbatim_bonus(customer_tokens, product)
                if WEIGHT_PROFILE_FIT:
                    bonus += WEIGHT_PROFILE_FIT * _profile_tag_bonus(product, preference_tags)
            rescored.append((item, WEIGHT_RETRIEVAL_SCORE * normalized_score + bonus))
        ordered = [entry for entry, _final_score in sorted(rescored, key=lambda pair: -pair[1])]

    # Demote (don't drop) anything already shown in a previous turn's
    # top-10: if it was scored and the session continued, it is provably
    # not the target, so a fresh candidate is strictly a better use of the
    # slot. Demotion rather than exclusion keeps the list at a full 10 even
    # once the pool is exhausted — a stable-sort partition, so relative
    # order inside each group is preserved.
    shown: set[str] = getattr(state, "shown_asins", set()) or set()
    if shown:
        ordered = (
            [item for item in ordered if item["parent_asin"] not in shown]
            + [item for item in ordered if item["parent_asin"] in shown]
        )

    if index is not None:
        ordered = llm_rerank(ordered, state, index, usage)

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

    # feature loose-match: customer's free-text blurb doesn't appear
    # verbatim in any product's text, but shares meaningful words with a
    # product that has "moisture wicking quick dry" in its features.
    feature_index = _FakeIndex()
    feature_index.products = {
        "F1": {"title": "athletic socks", "features": "moisture wicking quick dry fabric", "average_rating": 4.5, "rating_number": 200},
        "F2": {"title": "dress socks", "features": "elegant formal design", "average_rating": 4.9, "rating_number": 500},
    }
    feature_candidates = [
        {"parent_asin": "F1", "score": 0.5, "attrs": {}},
        {"parent_asin": "F2", "score": 0.5, "attrs": {}},
    ]  # tied score on purpose
    feature_state = SessionState(user_profile={})
    feature_state.advance_turn(1)
    feature_state.set_slot("feature", "needs to dry quickly for running", turn=1, source="freeform")
    feature_state.advance_turn(2)
    print("Feature loose-match:      ", rank(feature_candidates, feature_state, feature_index),
          "(expect F1 first — shares 'quick'/'dry' despite no exact phrase match)")
