"""Per-session conversation memory: slots, decay, boundary/override tracking.

Owner: Person B. See AGENTS.md for why slots exist and how they map onto
the ask_attribute enum (category, material, color, size, style, brand,
budget, feature, use_case).
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
        self.slots: dict[str, str] = {}
        self.slot_turn: dict[str, int] = {}
        self.closed_attributes: set[str] = set()  # boundary: "no preference"
        self.last_asked: str | None = None
        self.override_detected = False
        self.debug_log: list[dict] = []  # one entry per turn, for local debugging only

    def set_slot(self, attribute: str, value: str, turn: int) -> None:
        """Accumulate a new slot, or overwrite an existing one (override).

        A plain dict assignment already gives us "last write wins" — if the
        customer previously said "blue" and now says "red", this naturally
        replaces it. TODO (Person B): decide if some overrides should merge
        instead of replace (e.g. multiple `feature` mentions).
        """
        self.slots[attribute] = value
        self.slot_turn[attribute] = turn

    def close_attribute(self, attribute: str) -> None:
        """Customer has no preference for this attribute; stop asking about it."""
        self.closed_attributes.add(attribute)

    def decayed_slots(self, current_turn: int) -> dict[str, tuple[str, float]]:
        """Slot values with a confidence weight that shrinks the older they are.

        TODO (Person B): tune the decay curve; used by retrieval/ranking to
        down-weight stale, unconfirmed preferences vs. fresh ones.
        """
        result: dict[str, tuple[str, float]] = {}
        for attribute, value in self.slots.items():
            age = current_turn - self.slot_turn.get(attribute, current_turn)
            weight = max(0.3, 1.0 - 0.1 * age)
            result[attribute] = (value, weight)
        return result

    def open_attributes(self) -> list[str]:
        """Attributes not yet filled and not closed via a boundary signal."""
        return [a for a in ATTRIBUTES if a not in self.slots and a not in self.closed_attributes]

    def summary(self) -> str:
        """Short human-readable recap, rebuilt fresh each turn (not
        accumulated text) so prompt/token cost stays flat across turns.
        Feeds into `message` composition and any future LLM rerank prompt.
        """
        if not self.slots:
            return "no preferences stated yet"
        return ", ".join(f"{attribute}: {value}" for attribute, value in self.slots.items())

    def log_turn(self, turn: int, track: str, candidate_count: int, ask_attribute: str | None) -> None:
        """Lightweight per-turn trace for debugging against the 200 dev
        sessions — this is never sent to the evaluator, purely local.
        """
        self.debug_log.append({
            "turn": turn,
            "track": track,
            "candidate_count": candidate_count,
            "ask_attribute": ask_attribute,
            "slots": dict(self.slots),
        })
