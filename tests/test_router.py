from __future__ import annotations

import unittest

from starter.router import (
    apply_override,
    classify_single,
    classify_track,
    detect_boundary,
    detect_override_signal,
    extract_slot_values,
    extract_slots,
)
from starter.state import SessionState


class ExtractSlotValuesAnswerTemplateTest(unittest.TestCase):
    """Fixtured on the evaluator's exact customer_reply() templates (see
    evaluator/local_evaluator.py) — not invented strings. Exercises the
    pure classifier `extract_slot_values`; the stateful entry point
    `extract_slots(state, message)` is covered separately below."""

    def test_single_value_answer_trusted_via_last_asked(self) -> None:
        slots = extract_slot_values("For that, what matters is: cotton.", last_asked="material")
        self.assertEqual(slots, {"material": "cotton"})

    def test_multi_value_answer_joined_with_semicolon(self) -> None:
        slots = extract_slot_values("For that, what matters is: cotton; leather.", last_asked="material")
        self.assertEqual(slots, {"material": "cotton; leather"})

    def test_answer_trusted_even_for_categories_with_no_keyword_list(self) -> None:
        # style/use_case/feature have no dedicated answer-side keyword list —
        # the whole point of the last_asked path is that none is needed.
        slots = extract_slot_values("For that, what matters is: Department: Womens.", last_asked="style")
        self.assertEqual(slots, {"style": "Department: Womens"})

    def test_no_additional_preference_yields_no_slot_change(self) -> None:
        slots = extract_slot_values("I don't have an additional preference for budget.", last_asked="budget")
        self.assertEqual(slots, {})

    def test_without_last_asked_answer_template_is_not_trusted(self) -> None:
        # No attribution context -> falls through to unprompted classification.
        slots = extract_slot_values("For that, what matters is: cotton.")
        self.assertEqual(slots.get("material"), "cotton")
        self.assertNotIn("style", slots)


class ExtractSlotValuesUnpromptedTest(unittest.TestCase):
    def test_category_extracted_from_buying_opening_message(self) -> None:
        slots = extract_slot_values("I'm looking for Earrings Hoop. A key requirement is: cotton.")
        self.assertEqual(slots["category"], "earrings hoop")
        self.assertEqual(slots["material"], "cotton")

    def test_category_extracted_from_browsing_opening_message(self) -> None:
        slots = extract_slot_values("I'm looking for clothing item, but I'm still exploring.")
        self.assertEqual(slots["category"], "clothing item")
        self.assertNotIn("material", slots)
        self.assertNotIn("feature", slots)

    def test_budget_around_price_prefix_classified_as_budget(self) -> None:
        slots = extract_slot_values("I'm looking for Sandals. A key requirement is: budget around $49.99.")
        self.assertEqual(slots["budget"], "49.99")

    def test_color_prefix_classified_as_color(self) -> None:
        slots = extract_slot_values("I'm looking for Boots. A key requirement is: color: blue.")
        self.assertEqual(slots["color"], "blue")

    def test_message_order_not_definition_order_wins_multi_material(self) -> None:
        # "leather" appears before "cotton" in the message even though
        # cotton is earlier in MATERIAL_WORDS' definition order.
        slots = extract_slot_values("I need something in leather, not cotton.")
        self.assertEqual(slots["material"], "leather")

    def test_null_ask_nudge_does_not_pollute_a_feature_slot(self) -> None:
        slots = extract_slot_values("Those options are not quite right yet. Ask me about one specific attribute.")
        self.assertNotIn("feature", slots)

    def test_hard_requirement_with_no_keyword_match_recovered_as_feature(self) -> None:
        # A genuine Buying turn-1 constraint with no recognizable keyword —
        # previously silently dropped (10% of real Buying sessions per audit).
        slots = extract_slot_values(
            "I'm looking for Earrings. A key requirement is: lightweight dangle design for everyday wear."
        )
        self.assertEqual(slots["feature"], "lightweight dangle design for everyday wear")

    def test_hard_requirement_with_keyword_match_not_double_counted(self) -> None:
        slots = extract_slot_values("I'm looking for Boots. A key requirement is: leather.")
        self.assertEqual(slots["material"], "leather")
        self.assertNotIn("feature", slots)

    def test_still_exploring_tail_unaffected_by_the_new_fallback(self) -> None:
        slots = extract_slot_values("I'm looking for clothing item, but I'm still exploring.")
        self.assertNotIn("feature", slots)


class ExtractSlotsStatefulEntryPointTest(unittest.TestCase):
    """Covers the D-facing contract: extract_slots(state, message) -> state,
    mutating in place, refreshing durable_notes, and requiring
    state.advance_turn() to have been called first."""

    def test_mutates_and_returns_the_same_state(self) -> None:
        state = SessionState(user_profile={})
        state.advance_turn(1)
        result = extract_slots(state, "I'm looking for Boots. A key requirement is: leather.")
        self.assertIs(result, state)
        self.assertEqual(state.filled_slots["category"], "boots")
        self.assertEqual(state.filled_slots["material"], "leather")
        self.assertEqual(state.slot_source["material"], "freeform")

    def test_answer_to_last_asked_is_sourced_as_asked(self) -> None:
        state = SessionState(user_profile={})
        state.advance_turn(2)
        state.record_ask("material")
        extract_slots(state, "For that, what matters is: cotton.")
        self.assertEqual(state.filled_slots["material"], "cotton")
        self.assertEqual(state.slot_source["material"], "asked")

    def test_boundary_reply_closes_last_asked_attribute(self) -> None:
        state = SessionState(user_profile={})
        state.advance_turn(2)
        state.record_ask("size")
        extract_slots(state, "I don't have a preference for size; please use your judgment.")
        self.assertIn("size", state.filled_null)
        # Regression: the boundary template contains the attribute's own
        # name ("...for size...", "...for style..."), and "size"/"style" are
        # themselves entries in SIZE_WORDS/STYLE_WORDS — a boundary reply
        # must never also get misread as a same-named slot value.
        self.assertNotIn("size", state.filled_slots)

    def test_style_boundary_reply_does_not_self_match_its_own_keyword(self) -> None:
        state = SessionState(user_profile={})
        state.advance_turn(2)
        state.record_ask("style")
        extract_slots(state, "I don't have a preference for style; please use your judgment.")
        self.assertIn("style", state.filled_null)
        self.assertNotIn("style", state.filled_slots)

    def test_durable_notes_refreshed_from_slots_and_raw_message(self) -> None:
        state = SessionState(user_profile={})
        state.advance_turn(1)
        extract_slots(state, "I'm looking for Boots. A key requirement is: leather.")
        self.assertIn("material: leather", state.durable_notes)
        self.assertIn("I'm looking for Boots", state.durable_notes)

    def test_override_routed_through_apply_override_not_normal_extraction(self) -> None:
        state = SessionState(user_profile={})
        state.advance_turn(1)
        extract_slots(state, "I'm looking for Boots. A key requirement is: department: womens.")
        state.advance_turn(3)
        extract_slots(state, "Actually, ignore my earlier preference. What I need is: leather.")
        self.assertEqual(state.filled_slots.get("material"), "leather")
        self.assertNotIn("style", state.filled_slots)


class OverrideHandlingTest(unittest.TestCase):
    def test_detect_override_signal_matches_evaluator_template(self) -> None:
        message = "Actually, ignore my earlier preference. What I need is: leather."
        self.assertTrue(detect_override_signal(message))

    def test_classify_single_falls_back_to_feature_default(self) -> None:
        self.assertEqual(classify_single("a lightweight travel-friendly design"), (
            "feature", "a lightweight travel-friendly design",
        ))

    def test_apply_override_cleanly_replaces_cross_category_slot(self) -> None:
        state = SessionState(user_profile={})
        # Turn 1: unprompted freeform disclosure lands in "style".
        for attribute, value in extract_slot_values(
            "I'm looking for Boots. A key requirement is: department: womens."
        ).items():
            state.set_slot(attribute, value, turn=1, source="freeform")
        self.assertEqual(state.filled_slots.get("style"), "department")

        # Turn 3: override to a material constraint (different category).
        applied = apply_override(
            state, "Actually, ignore my earlier preference. What I need is: leather.", turn=3,
        )
        self.assertTrue(applied)
        self.assertEqual(state.filled_slots.get("material"), "leather")
        self.assertNotIn("style", state.filled_slots, "stale cross-category slot must be cleared, not just flagged")
        self.assertEqual(state.filled_slots.get("category"), "boots", "category must survive an override")

    def test_apply_override_never_clears_an_asked_sourced_slot(self) -> None:
        state = SessionState(user_profile={})
        state.set_slot("style", "casual", turn=1, source="asked")
        applied = apply_override(
            state, "Actually, ignore my earlier preference. What I need is: leather.", turn=3,
        )
        self.assertTrue(applied)
        self.assertEqual(
            state.filled_slots.get("style"), "casual", "a directly-confirmed answer must survive an override",
        )


class DetectBoundaryTest(unittest.TestCase):
    def test_matches_evaluator_boundary_template(self) -> None:
        message = "I don't have a preference for size; please use your judgment."
        self.assertTrue(detect_boundary(message))


class ClassifyTrackTest(unittest.TestCase):
    def test_browsing_by_default(self) -> None:
        state = SessionState(user_profile={})
        state.set_slot("category", "boots", turn=1, source="freeform")
        self.assertEqual(classify_track(state), "browsing")
        self.assertEqual(state.mode, "browsing")

    def test_buying_once_a_hard_signal_slot_is_filled(self) -> None:
        state = SessionState(user_profile={})
        state.set_slot("category", "boots", turn=1, source="freeform")
        state.set_slot("material", "leather", turn=1, source="freeform")
        self.assertEqual(classify_track(state), "buying")
        self.assertEqual(state.mode, "buying")


if __name__ == "__main__":
    unittest.main()
