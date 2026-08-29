"""Local-only, read-only audit: surface how often specific edge cases in the
200 public dev sessions actually occur, so state.py/router.py decisions are
made against real frequency data instead of guesses.

Reuses evaluator.local_evaluator's own helpers, never edits/monkeypatches it.
Not part of the required submission deliverable.

Usage: python3 -m scripts.audit_edge_cases
"""
from __future__ import annotations

from collections import Counter

from evaluator.local_evaluator import (
    catalog_index,
    coarse_category,
    initial_message,
    load_jsonl,
    materialize_hidden_fields,
)
from starter.router import apply_override, classify_single
from starter.state import SessionState


def main() -> None:
    samples = load_jsonl("data/public_set.jsonl")
    _catalog_ids, categories, products = catalog_index("data/catalog.jsonl")

    turn1_lost_constraint = Counter()   # scenario -> count where only category got filled
    turn1_total = Counter()
    title_fallback = 0                  # intent_card had no real candidates, used title
    soft_equals_hard = 0                # soft_preferences[0] == hard_constraints[0]
    override_classify_failed = 0
    override_total = 0
    brand_hits = 0

    for sample in samples:
        target = str(sample["ground_truth"]["parent_asin"])
        product = products[target]
        intent_card, behavior = materialize_hidden_fields(sample, products)
        effective_sample = {**sample, "intent_card": intent_card, "behavior": behavior}
        category = coarse_category(categories.get(target, []))

        hard = intent_card.get("hard_constraints", [])
        soft = intent_card.get("soft_preferences", [])
        if hard and hard[0] == (product.get("title") or "product").strip()[:180].rstrip():
            title_fallback += 1
        if hard and soft and hard[0] == soft[0]:
            soft_equals_hard += 1

        disclosed: set[str] = set()
        message = initial_message(effective_sample, category, disclosed)

        state = SessionState(user_profile=sample["user_profile"])
        state.advance_turn(1)
        from starter.router import extract_slots
        extract_slots(state, message)

        scenario = sample["scenario_type"]
        turn1_total[scenario] += 1
        non_category = {k: v for k, v in state.filled_slots.items() if k != "category"}
        if scenario in ("buying", "intent_override") and not non_category:
            turn1_lost_constraint[scenario] += 1

        if "brand" in state.filled_slots:
            brand_hits += 1

        if scenario == "intent_override":
            override_total += 1
            new_value = str(behavior.get("override", {}).get("new_value", ""))
            if classify_single(new_value) is None:
                override_classify_failed += 1

    print("=== Edge case audit against 200 public sessions ===\n")
    print("1. Turn-1 hard-constraint/old-value text produces ZERO extracted")
    print("   slots (other than category) -- the clause is silently lost:")
    for scenario in ("buying", "intent_override"):
        n = turn1_total[scenario]
        lost = turn1_lost_constraint[scenario]
        print(f"   {scenario:<16} {lost}/{n} sessions ({lost/n:.0%})")

    print(f"\n2. intent_card had no real candidates, fell back to product title:")
    print(f"   {title_fallback}/{len(samples)} sessions")

    print(f"\n3. soft_preferences[0] == hard_constraints[0] (override old==new risk):")
    print(f"   {soft_equals_hard}/{len(samples)} sessions")

    print(f"\n4. Override new_value failed to classify into any attribute:")
    print(f"   {override_classify_failed}/{override_total} intent_override sessions")

    print(f"\n5. 'brand' slot ever fired across all 200 sessions' turn-1 message:")
    print(f"   {brand_hits}/{len(samples)}")


if __name__ == "__main__":
    main()
