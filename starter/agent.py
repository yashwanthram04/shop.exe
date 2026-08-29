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

from dotenv import load_dotenv

load_dotenv()  # loads .env (gitignored, per-person local keys) into os.environ, if present

from .clarify import pick_attribute_to_ask
from .rank import rank
from .retrieval import RetrievalIndex, retrieve
from .router import classify_track, extract_slots
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
            return self._respond(state, user_message, turn, top_k)
        except Exception:
            # A crash counts as a miss for this session regardless (see
            # AGENTS.md) — fail safe with a valid empty response instead of
            # raising, so one bad turn doesn't corrupt the whole run.
            return dict(EMPTY_RESPONSE)

    def _respond(self, state: SessionState, user_message: str, turn: int, top_k: int) -> dict:
        state.advance_turn(turn)
        # One call folds boundary/override/slot-extraction into state, and
        # refreshes state.durable_notes — see router.py's module docstring
        # for the full cross-team contract this satisfies.
        extract_slots(state, user_message)

        track = classify_track(state)
        # state.durable_notes (slot summary + this turn's raw text) is what
        # retrieval.py searches on — the AGENTS.md-flagged state->retrieval
        # hookup, now built once in state.py rather than duplicated here.
        candidates = retrieve(self.index, state.durable_notes, state.filled_slots, track, top_n=50)

        # pick_attribute_to_ask now owns the "should I even ask" gate
        # internally (Rule C, formerly the standalone pool_is_too_broad
        # call here) as well as which attribute to ask about (Rule D) and
        # the boundary-just-fired skip (Rule A, auto-detected from state).
        ask_attribute = pick_attribute_to_ask(candidates, state, top_k)
        state.record_ask(ask_attribute)
        state.log_turn(turn, track, len(candidates), ask_attribute)

        # index=self.index opts into rating/popularity/slot-fit scoring
        # (see the note at the bottom of rank.py). rank.py's own `usage`
        # stays {0, 0} — it makes no LLM call — but router.py's extraction
        # (state.turn_usage) does whenever GROQ_API_KEY is set, so both are
        # merged into the one number this turn actually costs.
        rank_usage = {"prompt_tokens": 0, "completion_tokens": 0}
        ranked_ids = rank(candidates, state, index=self.index, usage=rank_usage)
        usage = {
            "prompt_tokens": state.turn_usage["prompt_tokens"] + rank_usage["prompt_tokens"],
            "completion_tokens": state.turn_usage["completion_tokens"] + rank_usage["completion_tokens"],
        }
        message = (
            f"Do you have a {ask_attribute} preference?"
            if ask_attribute
            else "Here are some options based on what you've told me so far."
        )
        return {
            "message": message,
            "ask_attribute": ask_attribute,
            "recommendations": [{"parent_asin": asin} for asin in ranked_ids],
            "usage": usage,
        }
