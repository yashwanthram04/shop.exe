"""Slot extraction + Buying/Browsing routing + boundary/override signal
detection.

Owner: Person B. This is the "brain" that reads the customer's raw message
and decides (a) what structured facts it contains, (b) whether this session
is behaving like a Buying or Browsing customer, and (c) whether the message
is a boundary ("no preference") or override ("actually, I changed my mind")
signal. See AGENTS.md for how the real evaluator's simulated customer
produces these signals.
"""
from __future__ import annotations

import re

from .state import SessionState

MATERIAL_WORDS = (
    "cotton", "polyester", "nylon", "leather", "wool",
    "spandex", "silk", "rayon", "denim", "fabric",
)
COLOR_WORDS = (
    "black", "white", "blue", "red", "pink", "green",
    "brown", "gray", "grey", "purple", "yellow", "orange",
)
SIZE_WORDS = ("small", "medium", "large", "size", "xl", "xs", "wide", "narrow")
BOUNDARY_PHRASES = ("no preference", "whatever", "doesn't matter", "don't care", "any is fine", "up to you", "your judgment")
OVERRIDE_PHRASES = ("actually", "instead", "forget", "change my mind", "no longer", "ignore my earlier", "on second thought")
BUDGET_RE = re.compile(r"\$?\s?(\d+(?:\.\d+)?)")

# TODO (Person B): this is intentionally simple regex/keyword matching so
# the agent works with zero external dependencies. Swap in an LLM JSON call
# here later if you want richer extraction — keep the same return shape
# ({attribute: value}) so nothing downstream needs to change.


def extract_slots(message: str) -> dict[str, str]:
    """Pull structured {attribute: value} pairs out of free text.

    Placeholder implementation: single best guess per attribute per call.
    TODO (Person B): handle multiple facts in one message (the real customer
    simulator can say two things in one reply, e.g. "cotton; under $40" —
    see AGENTS.md's ask_attribute section).
    """
    text = message.lower()
    slots: dict[str, str] = {}

    for word in MATERIAL_WORDS:
        if word in text:
            slots["material"] = word
            break

    for word in COLOR_WORDS:
        if word in text:
            slots["color"] = word
            break

    if "$" in text or "under" in text or "budget" in text:
        match = BUDGET_RE.search(text)
        if match:
            slots["budget"] = match.group(1)

    for word in SIZE_WORDS:
        if word in text:
            slots["size"] = word
            break

    return slots


def detect_boundary(message: str) -> bool:
    """True if the message reads like 'I have no preference for that'."""
    text = message.lower()
    return any(phrase in text for phrase in BOUNDARY_PHRASES)


def detect_override_signal(message: str) -> bool:
    """True if the message reads like the customer is contradicting/replacing
    an earlier stated preference. TODO (Person B): use this to decide which
    specific slot is being overridden rather than just flagging that *some*
    override happened (currently `extract_slots` overwriting a slot key
    already handles the mechanical replacement; this flag is for logging /
    future strategy-switching use).
    """
    text = message.lower()
    return any(phrase in text for phrase in OVERRIDE_PHRASES)


def classify_track(state: SessionState, message: str) -> str:
    """'buying' if the customer has stated concrete hard constraints,
    'browsing' otherwise.

    Placeholder heuristic: any one of brand/budget/size/color/material
    present flips it to buying. TODO (Person B): tune this against the 200
    dev sessions' scenario_metrics breakdown (buying should already do
    reasonably well; browsing is the one to watch).
    """
    hard_signal_attributes = ("brand", "budget", "size", "color", "material")
    if any(attribute in state.slots for attribute in hard_signal_attributes):
        return "buying"
    return "browsing"
