from __future__ import annotations

import unittest

from starter.state import ATTRIBUTES, SessionState


class SetSlotAndSourceTest(unittest.TestCase):
    def test_source_defaults_to_freeform(self) -> None:
        state = SessionState(user_profile={})
        state.set_slot("material", "cotton", turn=1)
        self.assertEqual(state.slot_source["material"], "freeform")

    def test_source_can_be_asked(self) -> None:
        state = SessionState(user_profile={})
        state.set_slot("material", "cotton", turn=2, source="asked")
        self.assertEqual(state.slot_source["material"], "asked")

    def test_last_write_wins_for_same_attribute(self) -> None:
        state = SessionState(user_profile={})
        state.set_slot("color", "blue", turn=1, source="freeform")
        state.set_slot("color", "red", turn=2, source="asked")
        self.assertEqual(state.filled_slots["color"], "red")
        self.assertEqual(state.slot_source["color"], "asked")


class ClearFreeformOverrideTest(unittest.TestCase):
    def test_clears_freeform_slot(self) -> None:
        state = SessionState(user_profile={})
        state.set_slot("style", "casual", turn=1, source="freeform")
        state.clear_freeform_override(except_attribute="material")
        self.assertNotIn("style", state.filled_slots)
        self.assertNotIn("style", state.slot_turn)
        self.assertNotIn("style", state.slot_source)

    def test_never_clears_category(self) -> None:
        state = SessionState(user_profile={})
        state.set_slot("category", "boots", turn=1, source="freeform")
        state.clear_freeform_override(except_attribute="material")
        self.assertIn("category", state.filled_slots)

    def test_never_clears_the_except_attribute(self) -> None:
        state = SessionState(user_profile={})
        state.set_slot("material", "leather", turn=3, source="freeform")
        state.clear_freeform_override(except_attribute="material")
        self.assertIn("material", state.filled_slots)

    def test_never_clears_an_asked_sourced_slot(self) -> None:
        state = SessionState(user_profile={})
        state.set_slot("style", "casual", turn=1, source="asked")
        state.clear_freeform_override(except_attribute="material")
        self.assertIn("style", state.filled_slots)


class DecayedSlotsTest(unittest.TestCase):
    def test_asked_slots_decay_slower_than_freeform(self) -> None:
        state = SessionState(user_profile={})
        state.set_slot("material", "cotton", turn=1, source="asked")
        state.set_slot("color", "blue", turn=1, source="freeform")
        decayed = state.decayed_slots(current_turn=8)
        _, asked_weight = decayed["material"]
        _, freeform_weight = decayed["color"]
        self.assertGreater(asked_weight, freeform_weight)
        self.assertEqual(asked_weight, 0.7)  # floor for "asked"
        self.assertEqual(freeform_weight, 0.3)  # floor for "freeform"

    def test_weight_decreases_with_age_before_hitting_floor(self) -> None:
        state = SessionState(user_profile={})
        state.set_slot("material", "cotton", turn=5, source="asked")
        decayed = state.decayed_slots(current_turn=6)
        _, weight = decayed["material"]
        self.assertAlmostEqual(weight, 0.9)


class OpenAttributesTest(unittest.TestCase):
    def test_excludes_filled_and_closed_attributes(self) -> None:
        state = SessionState(user_profile={})
        state.set_slot("material", "cotton", turn=1)
        state.close_attribute("brand")
        open_attrs = state.open_attributes()
        self.assertNotIn("material", open_attrs)
        self.assertNotIn("brand", open_attrs)
        self.assertIn("color", open_attrs)


class RecordAskAndAdvanceTurnTest(unittest.TestCase):
    def test_record_ask_sets_last_asked_and_accumulates_asked_categories(self) -> None:
        state = SessionState(user_profile={})
        state.record_ask("material")
        state.record_ask("color")
        self.assertEqual(state.last_asked, "color")
        self.assertEqual(state.asked_categories, {"material", "color"})

    def test_record_ask_with_none_does_not_add_to_asked_categories(self) -> None:
        state = SessionState(user_profile={})
        state.record_ask("material")
        state.record_ask(None)
        self.assertIsNone(state.last_asked)
        self.assertEqual(state.asked_categories, {"material"})

    def test_advance_turn_updates_turn(self) -> None:
        state = SessionState(user_profile={})
        state.advance_turn(4)
        self.assertEqual(state.turn, 4)


class DurableNotesTest(unittest.TestCase):
    def test_combines_slot_summary_and_raw_message(self) -> None:
        state = SessionState(user_profile={})
        state.set_slot("material", "cotton", turn=1)
        state.update_durable_notes("I need something warm")
        self.assertIn("material: cotton", state.durable_notes)
        self.assertIn("I need something warm", state.durable_notes)

    def test_no_slots_yet_still_includes_raw_message(self) -> None:
        state = SessionState(user_profile={})
        state.update_durable_notes("still exploring")
        self.assertIn("still exploring", state.durable_notes)


class AttributeVocabularyTest(unittest.TestCase):
    def test_attributes_match_ask_attribute_enum_minus_other_and_null(self) -> None:
        # docs/agent_api_contract.json's ask_attribute enum is these 9 plus
        # "other" and null — ATTRIBUTES is the fact-extraction vocabulary,
        # "other" is a request-side wildcard, never a fact category.
        self.assertEqual(set(ATTRIBUTES), {
            "category", "material", "color", "size", "style",
            "brand", "budget", "feature", "use_case",
        })
        self.assertNotIn("other", ATTRIBUTES)


if __name__ == "__main__":
    unittest.main()
