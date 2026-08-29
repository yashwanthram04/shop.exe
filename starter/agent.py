"""Shopping copilot Agent — orchestrates slot tracking, intent routing,
hybrid retrieval, clarification, and ranking into the required
reset()/respond() contract.

Owner: Person D. Do not add retrieval/ranking/routing logic directly in
this file — wire in real implementations from state.py, router.py,
retrieval.py, clarify.py, rank.py as teammates finish them. This file's job
is orchestration, defensive error handling, and response composition. See
AGENTS.md for the scoring mechanics this design is built around, and for
the list of files that must never be modified.
"""
from __future__ import annotations

from pathlib import Path

from .clarify import pick_attribute_to_ask, pool_is_too_broad
from .rank import rank
from .retrieval import RetrievalIndex, retrieve
from .router import classify_track, detect_boundary, detect_override_signal, extract_slots
from .state import SessionState

EMPTY_RESPONSE = {
    "message": "",
    "ask_attribute": None,
    "recommendations": [],
    "usage": {"prompt_tokens": 0, "completion_tokens": 0},
}


class Agent:
    def __init__(self, catalog_path: str | Path = "data/catalog.jsonl") -> None:
        self.index = RetrievalIndex(catalog_path)
        self._states: dict[str, SessionState] = {}

    def reset(self, session_id: str, user_profile: dict) -> None:
        self._states[session_id] = SessionState(user_profile)

    def respond(self, session_id: str, user_message: str, turn: int, top_k: int) -> dict:
        state = self._states.get(session_id)
        if state is None:
            raise RuntimeError("reset must be called before respond")
        try:
            return self._respond(state, user_message, turn)
        except Exception:
            # A crash counts as a miss for this session regardless (see
            # AGENTS.md) — fail safe with a valid empty response instead of
            # raising, so one bad turn doesn't corrupt the whole run.
            return dict(EMPTY_RESPONSE)

    def _respond(self, state: SessionState, user_message: str, turn: int) -> dict:
        state.override_detected = detect_override_signal(user_message)
        if state.last_asked and detect_boundary(user_message):
            state.close_attribute(state.last_asked)

        for attribute, value in extract_slots(user_message).items():
            state.set_slot(attribute, value, turn)

        track = classify_track(state, user_message)
        candidates = retrieve(self.index, user_message, state.slots, track, top_n=50)

        ask_attribute = None
        if pool_is_too_broad(len(candidates), track, turn):
            ask_attribute = pick_attribute_to_ask(state)
        state.last_asked = ask_attribute
        state.log_turn(turn, track, len(candidates), ask_attribute)

        ranked_ids = rank(candidates, state)
        message = (
            f"Do you have a {ask_attribute} preference?"
            if ask_attribute
            else "Here are some options based on what you've told me so far."
        )
        return {
            "message": message,
            "ask_attribute": ask_attribute,
            "recommendations": [{"parent_asin": asin} for asin in ranked_ids],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0},
        }
