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
    True if the pool is still large/uncertain enough that a *targeted*
    (entropy-selected) clarification is worth making. False means the pool
    is already tight — prefer the broad "other" probe over a narrow one.

    NOTE: this no longer gates whether we ask *at all* (see
    pick_attribute_to_ask). Asking is free — a response carries
    `ask_attribute` AND `recommendations` together, and MTTC only counts
    the turn the target is hit — so there is never a reason to stay
    silent. `HARD_STOP_ASK_TURN` is deliberately not consulted here any
    more for the same reason: it used to force `None` on turns 8-10,
    guaranteeing three dead turns in every long session.
    """
    return len(candidate_pool) > max(CONFIDENCE_POOL_THRESHOLD, top_k)


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
      B. no unfilled categories remain -> fall back to the broad "other" probe
      C. pool already tight -> skip the *targeted* ask, still probe "other"
      D. otherwise: pick the unfilled attribute with the highest
         (entropy, subject to a minimum coverage) — i.e. the attribute
         that would most split the current pool if we knew the answer.

    Returning None is now reserved for Rule A alone. Measured on the 200
    public sessions (ISSUES.md #8): *every* missed session used to end in a
    trailing run of `None`, averaging 4.7 dead turns, because once the
    entropy-eligible attributes were exhausted there was nothing left to
    return. The evaluator answers `None` with "Those options are not quite
    right yet. Ask me about one specific attribute." — strictly zero
    information — while `"other"` matches ANY still-undisclosed fact
    (evaluator/local_evaluator.py:178-181 short-circuits the class check
    for it), so it is never worse and often better. Asking also costs
    nothing: `ask_attribute` and `recommendations` ship in the same
    response, and MTTC only counts the hit turn.
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

    # Rule B — every named attribute is filled or explicitly nulled, but
    # undisclosed facts may still exist that no named attribute maps onto
    # (the evaluator's fact pool is not partitioned by our 9 categories).
    # "other" is the only probe that can still reach them.
    if not unfilled:
        return "other"

    # Rule C — pool is already tight, so a targeted entropy ask has little
    # left to split. Still probe broadly rather than going silent.
    if not pool_is_too_broad(candidate_pool, turn, top_k):
        return "other"

    # Rule D0 — "other" bootstrap. Per AGENTS.md, asking "other" matches
    # ANY undisclosed hidden fact regardless of type (up to 2 per ask,
    # ~4 facts total), unlike a named attribute which only matches facts
    # of that one type — the highest-yield probe available (ISSUES.md #2).
    # Spend it early, before pool-based entropy has much retrieved-candidate
    # data to reason over.
    #
    # The old `other_asked_count < 2` cap was a proxy for "~4 facts / 2 per
    # ask", but it broke whenever an ask never got answered — in
    # intent_override sessions the scripted override message *replaces* the
    # reply to that turn's question, silently burning one of the two
    # allowed probes (traced on public_0002). The bootstrap is still capped
    # so entropy gets a turn, but exhausting it no longer dead-ends the
    # session: "other" remains available as the Rule-D fallback below.
    if turn <= 2 and state.other_asked_count < 2:
        return "other"

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
        # Nothing clears the entropy/coverage bar for a targeted ask —
        # which is the common case late in a session, and is guaranteed for
        # size/budget/feature/use_case because retrieval.py's
        # _extract_attrs never populates them (ISSUES.md #6), so their
        # coverage is always 0.0. Fall back to the broad probe instead of
        # going silent; this is the specific line that used to start the
        # trailing all-None run in every missed session.
        return "other"

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
