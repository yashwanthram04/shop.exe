"""Over-generality check + clarification-attribute selection + turn-budget
pressure.

Owner: Person C (paired with rank.py). This decides, each turn, whether the
candidate pool is still too broad to just answer, and if so which
ask_attribute is most worth spending a turn on. See AGENTS.md: MTTC
penalizes turns, so the threshold here should tighten as turns run out.
"""
from __future__ import annotations

from .state import SessionState

# TODO (Person C): tune these against the 200 dev sessions' scenario_metrics.
BROAD_POOL_THRESHOLD = {"buying": 15, "browsing": 40}

# NOTE: "category" deliberately excluded. router.py's extract_slots never
# fills it, and the evaluator's simulated customer never discloses a fact
# classified as "category" either (see AGENTS.md) — asking about it is a
# guaranteed dead-end turn that can never be satisfied, which previously
# caused the agent to ask the same unanswerable question every turn.
ASK_PRIORITY = (
    "budget", "color", "material",
    "size", "style", "brand", "use_case", "feature",
)


def pool_is_too_broad(candidate_count: int, track: str, turn: int) -> bool:
    """True if we should stop and ask a clarifying question instead of
    just ranking/answering this turn.

    Placeholder: a fixed per-track threshold that shrinks as turns pass
    (turn-budget pressure) — more willing to just commit to a guess near
    turn 10 than to spend another turn asking.
    """
    threshold = BROAD_POOL_THRESHOLD.get(track, 30)
    threshold = max(5, threshold - turn * 2)
    return candidate_count > threshold


def pick_attribute_to_ask(pool: list[dict], state: SessionState) -> str | None:
    """Which ask_attribute is most worth spending this turn on.

    `pool` is retrieval.py's candidate list — each item is
    `{"parent_asin": str, "score": float, "attrs": dict}`, where `attrs`
    holds that product's parsed material/color/style/brand/category.

    Placeholder: fixed priority order over open (unfilled, non-boundary)
    attributes, ignoring `pool` entirely. TODO (Person C): replace with an
    info-gain style choice — for each open attribute, look at the spread of
    `attrs[attribute]` values across `pool` (e.g. via Counter) and pick
    whichever attribute would split the current pool the most evenly if we
    knew the customer's answer, rather than always asking in the same fixed
    order regardless of what's actually still ambiguous.
    """
    open_attributes = set(state.open_attributes())
    for attribute in ASK_PRIORITY:
        if attribute in open_attributes:
            return attribute
    return None
