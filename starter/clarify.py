"""
clarify.py — Owned by Person C.

Decides WHAT to ask next (`ask_attribute`), deterministically, per PRD §3.11.
The LLM (owned by whoever writes the combined call) only *phrases* the
question once this module has already chosen the target attribute — this
file is the only thing allowed to decide the choice itself.

-------------------------------------------------------------------------
CONFIRMED CONTRACTS, against the real state.py / retrieval.py (these were
originally written as assumptions before those files were locked — now
verified and adjusted where reality differed):

state is the real SessionState OBJECT (state.py), not a dict — read as
attributes, not `.get()`:
    state.filled_slots      : dict[str, str]   # only filled attributes are keys
    state.filled_null       : set[str]         # explicit no-preference
    state.asked_categories  : set[str]         # already asked, cumulative
    state.turn              : int
    state.last_asked        : str | None       # what we asked last turn

candidate (from retrieval.py's retrieve()) is a dict:
    candidate["parent_asin"]   : str
    candidate["attrs"]         : dict[str, str]   # NOT all 9 categories —
                                   retrieval.py's _extract_attrs only
                                   populates material/color/style/brand/
                                   category today. Rule D's coverage gate
                                   (MIN_COVERAGE_TO_ASK) already handles
                                   this safely: budget/size/feature/use_case
                                   will just never clear the coverage bar
                                   and won't be picked via entropy — same
                                   gap noted in rank.py's SOFT_FIELDS_FOR_FIT.
    candidate["score"]         : float
-------------------------------------------------------------------------
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Any, Mapping, Optional, Sequence

from .state import SessionState

# ---------------------------------------------------------------------------
# Config — tune these against evaluator.local_evaluator, per scenario if needed
# ---------------------------------------------------------------------------

# NOTE: earlier local-only code had a fixed ASK_PRIORITY that deliberately
# excluded "category", because at the time router.py never filled it and
# asking about it was a guaranteed dead-end. router.py now extracts
# "category" near-100% reliably from the turn-1 "I'm looking for X" line
# (see AGENTS.md), so it's almost always already filled by the time
# `unfilled` is computed below — the entropy/coverage gates in Rule D also
# independently protect against asking about it when it wouldn't help.
ALL_NINE_CATEGORIES: tuple[str, ...] = (
    "category", "material", "color", "size", "style",
    "brand", "budget", "feature", "use_case",
)

# If the candidate pool is already this small, don't bother asking —
# just recommend. Starting guess per PRD: equal to top_k.
CONFIDENCE_POOL_THRESHOLD = 10

# Once this many turns have elapsed, stop asking regardless of entropy —
# protects MTTC / Efficiency near the 10-turn cap.
HARD_STOP_ASK_TURN = 8

# Below this entropy (bits), an attribute isn't worth asking about —
# the pool already mostly agrees on a value for it.
MIN_ENTROPY_TO_ASK = 0.35

# An attribute must have a non-null value on at least this fraction of the
# pool to be considered askable at all (avoids asking about an attribute
# almost nothing in the pool even has data for).
MIN_COVERAGE_TO_ASK = 0.30


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Support both dict-like and attribute-style candidate objects."""
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def diversity_score(candidate_pool: Sequence[Any], category: str) -> tuple[float, float]:
    """
    Shannon entropy (in bits) of `category`'s values across candidate_pool,
    plus the coverage fraction (how many candidates had a non-null value).

    High entropy  -> asking about this attribute would meaningfully split
                      the pool (values are spread out).
    Low entropy   -> the pool already agrees; asking wastes a turn.
    """
    values = [
        _get(_get(c, "attrs", {}), category)
        for c in candidate_pool
    ]
    values = [v for v in values if v]
    if not values:
        return 0.0, 0.0

    coverage = len(values) / len(candidate_pool) if candidate_pool else 0.0
    counts = Counter(values)
    total = len(values)
    entropy = -sum(
        (n / total) * math.log2(n / total)
        for n in counts.values()
    )
    return entropy, coverage


def pool_is_too_broad(candidate_pool: Sequence[Any], turn: int, top_k: int) -> bool:
    """
    True if the pool is still large/uncertain enough that clarification
    is worth the turn cost. False means: pool is already tight, or we're
    out of turn budget — go straight to recommending.
    """
    if turn >= HARD_STOP_ASK_TURN:
        return False
    if len(candidate_pool) <= max(CONFIDENCE_POOL_THRESHOLD, top_k):
        return False
    return True


def pick_attribute_to_ask(
    candidate_pool: Sequence[Any],
    state: SessionState,
    top_k: int,
    boundary_detected_this_turn: Optional[bool] = None,
) -> Optional[str]:
    """
    Deterministic selection of the next `ask_attribute`, or None to skip
    clarification and just recommend this turn.

    Order of checks mirrors PRD §3.11 Rules A–D:
      A. a no-preference / boundary signal just fired this turn -> don't ask
      B. no unfilled categories remain -> forced recommend-only
      C. pool already small/confident, or turn budget exhausted -> skip
      D. otherwise: pick the unfilled attribute with the highest
         (entropy, subject to a minimum coverage) — i.e. the attribute
         that would most split the current pool if we knew the answer.
    """
    # `state` is the real SessionState object (state.py), not a dict — these
    # were originally written against an assumed dict-like contract (see the
    # module docstring's "CONFIRMED CONTRACTS" note) before state.py was
    # locked. Reads as attributes now that it's confirmed.
    filled_slots: dict = state.filled_slots
    filled_null: set = state.filled_null
    asked_categories: set = state.asked_categories
    turn: int = state.turn

    # Rule A. Auto-detected when not explicitly passed: a boundary reply
    # just closed whatever we asked about last turn (state.last_asked was
    # added to filled_null by router.py's extract_slots, called earlier this
    # same turn) — skip asking again immediately rather than pestering the
    # customer with another question right after "I don't care."
    if boundary_detected_this_turn is None:
        boundary_detected_this_turn = bool(state.last_asked and state.last_asked in filled_null)
    if boundary_detected_this_turn:
        return None

    unfilled = [
        cat for cat in ALL_NINE_CATEGORIES
        if filled_slots.get(cat) is None and cat not in filled_null
    ]

    # Rule B
    if not unfilled:
        return None

    # Rule C
    if not pool_is_too_broad(candidate_pool, turn, top_k):
        return None

    # Rule D — score every unfilled, still-unasked-preferred candidate.
    # Categories already asked (but not yet filled/null) are still eligible
    # to re-ask only as a last resort — prefer fresh categories first.
    fresh_unfilled = [c for c in unfilled if c not in asked_categories]
    pool_for_scoring = fresh_unfilled or unfilled

    scored: list[tuple[str, float]] = []
    for cat in pool_for_scoring:
        entropy, coverage = diversity_score(candidate_pool, cat)
        if coverage < MIN_COVERAGE_TO_ASK:
            continue
        if entropy < MIN_ENTROPY_TO_ASK:
            continue
        scored.append((cat, entropy))

    if not scored:
        # Nothing clears the bar — don't waste a turn asking about an
        # attribute unlikely to narrow anything down.
        return None

    best_category, _ = max(scored, key=lambda pair: pair[1])
    asked_categories.add(best_category)  # mutate in place; state is shared
    return best_category


# ---------------------------------------------------------------------------
# Smoke test — run `python clarify.py` standalone with fake data, no
# dependency on A's or B's real code, to sanity-check the logic in isolation.
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    fake_pool = [
        {"parent_asin": "A1", "attrs": {"material": "cotton", "color": "blue"}, "score": 0.9},
        {"parent_asin": "A2", "attrs": {"material": "wool", "color": "blue"}, "score": 0.8},
        {"parent_asin": "A3", "attrs": {"material": "polyester", "color": "black"}, "score": 0.7},
        {"parent_asin": "A4", "attrs": {"material": "cotton", "color": "red"}, "score": 0.6},
        {"parent_asin": "A5", "attrs": {"material": "wool", "color": "green"}, "score": 0.5},
        {"parent_asin": "A6", "attrs": {"material": "cotton", "color": "blue"}, "score": 0.4},
        {"parent_asin": "A7", "attrs": {"material": "silk", "color": "black"}, "score": 0.3},
        {"parent_asin": "A8", "attrs": {"material": "cotton", "color": "white"}, "score": 0.2},
        {"parent_asin": "A9", "attrs": {"material": "wool", "color": "blue"}, "score": 0.1},
        {"parent_asin": "A10", "attrs": {"material": "polyester", "color": "red"}, "score": 0.05},
        {"parent_asin": "A11", "attrs": {"material": "cotton", "color": "black"}, "score": 0.02},
    ]
    fake_state = SessionState(user_profile={})
    fake_state.advance_turn(1)
    choice = pick_attribute_to_ask(fake_pool, fake_state, top_k=10)
    print("Pool size:", len(fake_pool))
    for cat in ("material", "color"):
        print(cat, "entropy/coverage:", diversity_score(fake_pool, cat))
    print("Chosen ask_attribute:", choice)
