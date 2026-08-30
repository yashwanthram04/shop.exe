"""Per-session conversation memory: slots, decay, boundary/override tracking.

Owner: Person B. See AGENTS.md for why slots exist and how they map onto
the ask_attribute enum (category, material, color, size, style, brand,
budget, feature, use_case).

Cross-team contract (what this object exposes, and who reads it):
- Person A (retrieval.py) reads `filled_slots` (hard-filter source) and
  `durable_notes` (what semantic_candidates() should embed as the query).
- Person C (clarify.py/rank.py) reads `filled_slots`, `filled_null`,
  `asked_categories`, `turn` directly off this object.
- `ATTRIBUTES` below is the exact vocabulary every slot key is drawn from —
  it matches docs/agent_api_contract.json's ask_attribute enum minus
  "other"/null, so nothing B ever writes into `filled_slots` can be a
  category A/C/D don't recognize.
- Fallback behavior (for D's top-level guard): every method here either
  succeeds or is a no-op on bad input — nothing in this file raises under
  normal use. If something upstream still throws while calling into this
  object, the existing whole-turn try/except in `agent.py`'s `respond()`
  already degrades to the safe empty response; state mutations made earlier
  in that same turn are harmless partial state, never corrupted state.
"""
from __future__ import annotations

ATTRIBUTES = (
    "category", "material", "color", "size", "style",
    "brand", "budget", "feature", "use_case",
)


class SessionState:
    """Holds everything the agent has learned about one customer session."""

    def __init__(self, user_profile: dict) -> None:
        self.user_profile = user_profile
        self.filled_slots: dict[str, str] = {}
        self.slot_turn: dict[str, int] = {}
        self.slot_source: dict[str, str] = {}  # attribute -> "asked" | "freeform"
        self.filled_null: set[str] = set()  # boundary: "no preference" confirmed
        self.asked_categories: set[str] = set()  # every attribute ever asked, cumulative
        self.last_asked: str | None = None  # only the most recent ask (drives attribution)
        self.override_detected = False
        self.turn: int = 0
        self.durable_notes: str = ""  # free text for Person A's semantic query, see update_durable_notes
        self.mode: str = "browsing"
        self.debug_log: list[dict] = []  # one entry per turn, for local debugging only
        self.turn_usage: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0}
        self.other_asked_count: int = 0  # see record_ask()

    def advance_turn(self, turn: int) -> None:
        """Call once at the start of each `respond()` call, before anything
        else touches this state, so every method below can rely on
        `self.turn` being current. Also resets `turn_usage` — the contract's
        `usage` field reports THIS turn's token cost, not a running total
        (the evaluator sums it across turns itself), so whatever LLM call
        happens during extraction should record into a freshly-zeroed
        counter each turn."""
        self.turn = turn
        self.turn_usage = {"prompt_tokens": 0, "completion_tokens": 0}

    def set_slot(self, attribute: str, value: str, turn: int, source: str = "freeform") -> None:
        """Accumulate a new slot, or overwrite an existing one (override).

        A plain dict assignment already gives us "last write wins" for
        same-attribute overwrites — if the customer previously said "blue"
        and now says "red", this naturally replaces it.

        `source` records *why* this slot is filled: "asked" if it's a direct
        answer to our own `ask_attribute` question (high confidence), or
        "freeform" if we inferred it from unprompted text (lower confidence,
        the default). This one piece of bookkeeping feeds two things:
        `decayed_slots()` below (asked slots decay slower), and
        `clear_freeform_override()` (only freeform slots are ever silently
        replaced by an override — an answer we were directly given should
        never be discarded without an explicit new answer to the same
        question).
        """
        self.filled_slots[attribute] = value
        self.slot_turn[attribute] = turn
        self.slot_source[attribute] = source

    def clear_freeform_override(self, except_attribute: str | None) -> None:
        """On a detected override, drop the stale unprompted slot being
        replaced — real replacement, not just a flag.

        Only ever touches "freeform"-sourced slots (see `set_slot`), and
        never `category` or `except_attribute` (the slot the override's new
        value is about to fill). Safe without knowing exactly which text the
        old value came from: this evaluator's override always follows
        exactly one unprompted turn-1 disclosure (see AGENTS.md), so at
        override time there is at most one other freeform slot to clear.
        """
        for attribute, source in list(self.slot_source.items()):
            if source == "freeform" and attribute not in ("category", except_attribute):
                self.filled_slots.pop(attribute, None)
                self.slot_turn.pop(attribute, None)
                self.slot_source.pop(attribute, None)

    def close_attribute(self, attribute: str) -> None:
        """Customer has no preference for this attribute; stop asking about it."""
        self.filled_null.add(attribute)

    def record_ask(self, attribute: str | None) -> None:
        """Call once per turn with whatever `ask_attribute` this turn's
        response actually used, so `last_asked` (single, drives next turn's
        attribution) and `asked_categories` (cumulative, drives Person C's
        "don't re-ask this" logic) both stay correct.

        `other_asked_count` tracks "other" specifically, separately from
        `asked_categories` — "other" reveals up to 2 undisclosed facts of
        ANY type per ask (see AGENTS.md), so unlike a named attribute it's
        worth asking a second time, and a plain set can't distinguish
        "asked once" from "asked twice".
        """
        self.last_asked = attribute
        if attribute:
            self.asked_categories.add(attribute)
            if attribute == "other":
                self.other_asked_count += 1

    def decayed_slots(self, current_turn: int) -> dict[str, tuple[str, float]]:
        """Slot values with a confidence weight that shrinks the older they
        are — but a slot the customer directly confirmed by answering our
        own question ("asked") decays slower than one we only inferred from
        unprompted text ("freeform"), per AGENTS.md's decay spec ("older
        unconfirmed slots count for less than recently confirmed ones").
        """
        result: dict[str, tuple[str, float]] = {}
        for attribute, value in self.filled_slots.items():
            age = current_turn - self.slot_turn.get(attribute, current_turn)
            floor = 0.7 if self.slot_source.get(attribute) == "asked" else 0.3
            weight = max(floor, 1.0 - 0.1 * age)
            result[attribute] = (value, weight)
        return result

    def open_attributes(self) -> list[str]:
        """Attributes not yet filled and not closed via a boundary signal."""
        return [a for a in ATTRIBUTES if a not in self.filled_slots and a not in self.filled_null]

    def summary(self) -> str:
        """Short human-readable recap, rebuilt fresh each turn (not
        accumulated text) so prompt/token cost stays flat across turns.
        Feeds `message` composition and `update_durable_notes` below.
        """
        if not self.filled_slots:
            return "no preferences stated yet"
        return ", ".join(f"{attribute}: {value}" for attribute, value in self.filled_slots.items())

    def update_durable_notes(self, message: str) -> None:
        """Refresh `durable_notes` — the free-text query material handed to
        Person A's `semantic_candidates()`. Combines the structured slot
        summary (so confirmed facts are always represented) with the
        current turn's raw message (so freeform nuance not captured by any
        slot still reaches semantic search). Rebuilt fresh each turn, not
        accumulated indefinitely, so token/embedding cost stays flat.

        When no slots are filled yet, `summary()`'s "no preferences stated
        yet" filler adds no retrieval signal and only dilutes the embedding
        query — skip it and use the raw message alone (see ISSUES.md #3).
        """
        if not self.filled_slots:
            self.durable_notes = message.strip()
        else:
            self.durable_notes = f"{self.summary()}. {message}".strip()

    def log_turn(self, turn: int, track: str, candidate_count: int, ask_attribute: str | None) -> None:
        """Lightweight per-turn trace for debugging against the 200 dev
        sessions — this is never sent to the evaluator, purely local.
        """
        self.debug_log.append({
            "turn": turn,
            "track": track,
            "candidate_count": candidate_count,
            "ask_attribute": ask_attribute,
            "slots": dict(self.filled_slots),
        })
