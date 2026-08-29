"""Local-only tuning tool: validate `classify_track()`'s Buying/Browsing
heuristic against the 200 public dev sessions' known `scenario_type` labels.

Reuses `evaluator.local_evaluator`'s own message-generation helpers
(`materialize_hidden_fields`, `initial_message`, `customer_reply`) instead of
reinventing dialogue simulation, and drives only `state.py` + `router.py` (no
retrieval/clarify/rank) — an isolated test of Person B's components, not a
full pipeline run. Never edits or monkeypatches the evaluator module.

Not part of the required submission deliverable.

Usage: python3 -m scripts.tune_classify_track
"""
from __future__ import annotations

from collections import defaultdict

from evaluator.local_evaluator import (
    MAX_TURNS,
    catalog_index,
    coarse_category,
    customer_reply,
    initial_message,
    load_jsonl,
    materialize_hidden_fields,
)
from starter.router import classify_track, extract_slots
from starter.state import SessionState

# Rotates through attributes so the stub "agent" always has something to ask
# about — the point here is exercising extract_slots/classify_track against
# realistic customer replies, not a real clarification strategy.
ASK_ROTATION = ("material", "budget", "color", "size", "style", "use_case", "brand", "category")


def simulate_and_classify(sample: dict, categories: dict, products: dict) -> tuple[str, int | None]:
    """Drive one session's dialogue through state.py/router.py only.
    Returns (final_track, first_turn_track_became_"buying").
    """
    state = SessionState(user_profile=sample["user_profile"])
    target = str(sample["ground_truth"]["parent_asin"])
    intent_card, behavior = materialize_hidden_fields(sample, products)
    effective_sample = {**sample, "intent_card": intent_card, "behavior": behavior}
    disclosed: set[str] = set()
    boundary_used = False
    override_applied = sample["scenario_type"] != "intent_override"
    category = coarse_category(categories.get(target, []))
    user_message = initial_message(effective_sample, category, disclosed)

    first_buying_turn: int | None = None
    track = "browsing"

    for turn in range(1, MAX_TURNS + 1):
        state.advance_turn(turn)
        extract_slots(state, user_message)

        track = classify_track(state)
        if track == "buying" and first_buying_turn is None:
            first_buying_turn = turn

        ask_attribute = ASK_ROTATION[turn % len(ASK_ROTATION)]
        state.record_ask(ask_attribute)

        if turn == MAX_TURNS:
            break
        override = effective_sample.get("behavior", {}).get("override") or {}
        if not override_applied and turn + 1 == int(override.get("turn", 3)):
            override_applied = True
            new_value = str(override.get("new_value", ""))
            if new_value:
                disclosed.add(new_value)
            user_message = str(override.get("message", "Actually, please ignore my earlier preference."))
        else:
            user_message, boundary_used = customer_reply(effective_sample, ask_attribute, disclosed, boundary_used)

    return track, first_buying_turn


def main() -> None:
    samples = load_jsonl("data/public_set.jsonl")
    _catalog_ids, categories, products = catalog_index("data/catalog.jsonl")

    by_scenario: dict[str, list[tuple[str, int | None]]] = defaultdict(list)
    for sample in samples:
        by_scenario[sample["scenario_type"]].append(simulate_and_classify(sample, categories, products))

    print(f"{'scenario':<16}{'n':>5}{'ended buying':>14}{'avg 1st-buy turn':>20}")
    for scenario in sorted(by_scenario):
        results = by_scenario[scenario]
        n = len(results)
        ended_buying = sum(1 for track, _ in results if track == "buying")
        buy_turns = [t for _, t in results if t is not None]
        avg_turn = sum(buy_turns) / len(buy_turns) if buy_turns else float("nan")
        print(f"{scenario:<16}{n:>5}{ended_buying:>14}{avg_turn:>20.2f}")


if __name__ == "__main__":
    main()
