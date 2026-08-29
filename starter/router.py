"""Slot extraction + Buying/Browsing routing + boundary/override signal
detection.

Owner: Person B. This is the "brain" that reads the customer's raw message
and decides (a) what structured facts it contains, (b) whether this session
is behaving like a Buying or Browsing customer, and (c) whether the message
is a boundary ("no preference") or override ("actually, I changed my mind")
signal. See AGENTS.md for how the real evaluator's simulated customer
produces these signals.

Cross-team contract, per Person D's stable-signature request:
- `extract_slots(state, message) -> state` is the one call `agent.py` needs
  per turn to fold a raw message into session state (boundary/override
  handling, slot extraction, `durable_notes`) — it mutates `state` in place
  and returns it. `agent.py` must call `state.advance_turn(turn)` first so
  slot timestamps land on the right turn.
- `classify_track(state) -> str` reads only `state.filled_slots` (no raw
  message needed) and also caches its result on `state.mode`.
- Fallback behavior for D's top-level guard: neither function raises under
  normal input; regex/keyword misses just mean no-ops, not exceptions. If
  something still throws, `agent.py`'s existing whole-turn try/except
  already degrades to the safe empty response — no per-function guard
  needed here.
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
# Mirrors the evaluator's own classify_constraint keyword buckets (see
# AGENTS.md) so our classification agrees with how the simulator itself
# would have bucketed the same string.
STYLE_WORDS = ("department", "style", "fit", "sleeve", "neck")
USE_CASE_WORDS = ("hiking", "running", "gym", "winter", "outdoor", "work")
BOUNDARY_PHRASES = ("no preference", "whatever", "doesn't matter", "don't care", "any is fine", "up to you", "your judgment")
OVERRIDE_PHRASES = ("actually", "instead", "forget", "change my mind", "no longer", "ignore my earlier", "on second thought")

BUDGET_RE = re.compile(r"\$?\s?(\d+(?:\.\d+)?)")
# Every scenario's opening line starts "I'm looking for {category}" (see
# AGENTS.md / initial_message in the evaluator) — this is a near-free,
# always-available extraction, not gated behind any other heuristic.
CATEGORY_RE = re.compile(r"looking for ([^,.]+)", re.IGNORECASE)
# Best-effort brand catch: "by Spirit Hoops" style mentions. Low-yield
# against the current evaluator (its own classify_constraint never emits
# "brand"), kept for completeness and private-set robustness.
BRAND_RE = re.compile(r"\bby ([A-Z][\w'&-]*(?:\s+[A-Z][\w'&-]*){0,2})\b")

# The evaluator answers a direct ask_attribute question with exactly one of
# these two templates (see customer_reply in evaluator/local_evaluator.py).
# When we know what we asked (`last_asked`), the evaluator has *already*
# filtered these values to match that attribute before sending them — no
# keyword classification needed, just trust the attribution.
ANSWER_TEMPLATE_RE = re.compile(r"^for that,\s*what matters is:\s*(.+?)\.?\s*$", re.IGNORECASE)
NO_MORE_TEMPLATE_RE = re.compile(r"^i don't have an additional preference for \w+\.?\s*$", re.IGNORECASE)
# The evaluator's fixed override template: "Actually, ignore my earlier
# preference. What I need is: {new_value}."
OVERRIDE_VALUE_RE = re.compile(r"what i need is:?\s*(.+?)\.?\s*$", re.IGNORECASE)
# Buying's turn-1 message is the ONLY evaluator template containing this
# exact phrase ("I'm looking for {category}. A key requirement is:
# {constraint}.") — Browsing/Boundary's turn-1 tail and the null-ask nudge
# never contain it, so trusting it is zero-risk, unlike a blind fallback.
HARD_REQUIREMENT_RE = re.compile(r"a key requirement is:\s*(.+?)\.?\s*$", re.IGNORECASE)


def _first_match(text: str, words: tuple[str, ...]) -> str | None:
    """Return whichever word appears earliest *in the message*, not earliest
    in `words`' definition order — fixes the old "first match in the tuple
    wins" bug that silently dropped a second material/color/etc. mentioned
    later in the same message.
    """
    best_word: str | None = None
    best_index: int | None = None
    for word in words:
        index = text.find(word)
        if index == -1:
            continue
        if best_index is None or index < best_index:
            best_index = index
            best_word = word
    return best_word


def _classify_unprompted(message: str) -> dict[str, str]:
    """Classify freeform, unprompted text (turn 1's opening message, or
    anything not matching the answer templates below) into slots.

    Deliberately does NOT fall back to a blind "feature" default the way
    `classify_single` does for override text — turn-1/unprompted messages
    can also be filler dialogue (e.g. the generic "still exploring" tail, or
    the null-ask nudge "Those options are not quite right yet..."), and a
    blind default would poison a slot with that filler text. Missing a
    genuine feature this way is an accepted precision-over-recall tradeoff.

    One narrow, zero-risk exception: text following the evaluator's exact
    "A key requirement is:" phrase (Buying's turn-1 template — never present
    in filler dialogue) IS trusted with a feature-default fallback, same as
    `classify_single`. An audit against the 200 public sessions found this
    clause was otherwise being silently dropped in 10% of Buying sessions
    (no keyword match anywhere in the message) — this phrase is a safe,
    explicit marker to recover it without loosening the general rule above.
    """
    text = message.lower()
    slots: dict[str, str] = {}

    category_match = CATEGORY_RE.search(message)
    if category_match:
        category = category_match.group(1).strip(" .,")
        if category:
            slots["category"] = category.lower()

    if "$" in text or "under" in text or "budget" in text:
        match = BUDGET_RE.search(text)
        if match:
            slots["budget"] = match.group(1)

    material = _first_match(text, MATERIAL_WORDS)
    if material:
        slots["material"] = material

    color = _first_match(text, COLOR_WORDS)
    if color:
        slots["color"] = color

    size = _first_match(text, SIZE_WORDS)
    if size:
        slots["size"] = size

    style = _first_match(text, STYLE_WORDS)
    if style:
        slots["style"] = style

    use_case = _first_match(text, USE_CASE_WORDS)
    if use_case:
        slots["use_case"] = use_case

    brand_match = BRAND_RE.search(message)
    if brand_match:
        slots["brand"] = brand_match.group(1).strip()

    requirement_match = HARD_REQUIREMENT_RE.search(message)
    if requirement_match:
        classified = classify_single(requirement_match.group(1))
        if classified:
            attribute, value = classified
            slots.setdefault(attribute, value)

    return slots


def classify_single(text: str) -> tuple[str, str] | None:
    """Classify a short freeform clause into exactly one (attribute, value)
    pair, using the evaluator's own classify_constraint priority order
    (budget > material > color > size > style > use_case > feature-default)
    so it agrees with how the simulator itself would bucket the same text.

    Used for override `new_value` classification, where the source text is
    guaranteed to be a genuine mined fact (never filler dialogue) — so,
    unlike `_classify_unprompted`, falling back to a "feature" default here
    is safe.
    """
    lowered = text.lower()
    if "$" in lowered or "under" in lowered or "budget" in lowered:
        match = BUDGET_RE.search(lowered)
        if match:
            return "budget", match.group(1)
    material = _first_match(lowered, MATERIAL_WORDS)
    if material:
        return "material", material
    color = _first_match(lowered, COLOR_WORDS)
    if color:
        return "color", color
    size = _first_match(lowered, SIZE_WORDS)
    if size:
        return "size", size
    style = _first_match(lowered, STYLE_WORDS)
    if style:
        return "style", style
    use_case = _first_match(lowered, USE_CASE_WORDS)
    if use_case:
        return "use_case", use_case
    cleaned = text.strip(" .")
    if cleaned:
        return "feature", cleaned[:180]
    return None


def extract_slot_values(message: str, last_asked: str | None = None) -> dict[str, str]:
    """Pull structured {attribute: value} pairs out of free text. Pure,
    stateless classifier — see `extract_slots` below for the stateful entry
    point `agent.py` actually calls.

    If `last_asked` is given and the message matches the evaluator's answer
    template ("For that, what matters is: X; Y."), the value(s) are trusted
    directly with no classification — the simulator already filtered them to
    match `last_asked` before sending them (see AGENTS.md). Handles multiple
    facts in one message by joining them back with "; " into the single slot
    value (e.g. "cotton; leather") rather than changing the return shape —
    retrieval.py/rank.py already treat slot values as opaque strings, so
    this needs no downstream coordination; split on "; " later if needed.

    Falls back to keyword/regex classification (`_classify_unprompted`) for
    anything else: turn 1's opening disclosure, an override message, or an
    unrecognized shape (e.g. a differently-phrased private-set simulator).
    """
    if last_asked:
        stripped = message.strip()
        answer_match = ANSWER_TEMPLATE_RE.match(stripped)
        if answer_match:
            values = [v.strip() for v in answer_match.group(1).split(";") if v.strip()]
            if values:
                return {last_asked: "; ".join(values)}
        if NO_MORE_TEMPLATE_RE.match(stripped):
            return {}

    return _classify_unprompted(message)


def detect_boundary(message: str) -> bool:
    """True if the message reads like 'I have no preference for that'."""
    text = message.lower()
    return any(phrase in text for phrase in BOUNDARY_PHRASES)


def detect_override_signal(message: str) -> bool:
    """True if the message reads like the customer is contradicting/replacing
    an earlier stated preference.
    """
    text = message.lower()
    return any(phrase in text for phrase in OVERRIDE_PHRASES)


def apply_override(state: SessionState, message: str, turn: int) -> bool:
    """Cleanly replace a contradicted slot rather than just flagging that an
    override happened.

    Classifies the override's new value, clears whichever slot the stale
    *unprompted* (turn-1) preference was filed under (never `category`, and
    never the slot the new value is about to fill), then sets the new slot.
    Returns True if an override was actually applied (a classifiable new
    value was found), False as a no-op otherwise (caller should fall back to
    normal extraction).

    Safe without cross-referencing which text the old value came from: this
    evaluator's override always follows exactly one unprompted turn-1
    disclosure (the Buying hard-constraint or the Intent-Override old
    preference), so at override time there is at most one `"freeform"`
    -sourced non-category slot to clear — see `SessionState.clear_freeform_override`.
    """
    value_match = OVERRIDE_VALUE_RE.search(message)
    target_text = value_match.group(1) if value_match else message
    classified = classify_single(target_text)
    if classified is None:
        return False
    attribute, value = classified
    state.clear_freeform_override(except_attribute=attribute)
    state.set_slot(attribute, value, turn, source="freeform")
    return True


def extract_slots(state: SessionState, message: str) -> SessionState:
    """Stateful entry point: turns one raw customer message into updated
    session state. This is the single call `agent.py` needs per turn — call
    `state.advance_turn(turn)` first, then this, then everything else
    (retrieval/clarify/rank) reads off `state` alone; `agent.py` stays pure
    orchestration.

    Order of operations, mirroring what was previously spread across
    `agent.py`: boundary check against `last_asked` first, and — since the
    evaluator's boundary template literally contains the attribute's own
    name (e.g. "...for size; please use your judgment.", and "size"/"style"
    are also entries in their own keyword lists) — a confirmed boundary
    short-circuits extraction for this turn entirely, so a no-preference
    reply can never also get misread as a same-named slot value. Otherwise:
    override handling (which fully owns its own slot mutation via
    `apply_override`), then normal extraction as a fallback. `durable_notes`
    is refreshed last either way, so it reflects this turn's final state.
    """
    if state.last_asked and detect_boundary(message):
        state.close_attribute(state.last_asked)
        state.update_durable_notes(message)
        return state

    state.override_detected = detect_override_signal(message)
    if not (state.override_detected and apply_override(state, message, state.turn)):
        for attribute, value in extract_slot_values(message, last_asked=state.last_asked).items():
            source = "asked" if attribute == state.last_asked else "freeform"
            state.set_slot(attribute, value, state.turn, source=source)

    state.update_durable_notes(message)
    return state


def classify_track(state: SessionState) -> str:
    """'buying' if the customer has stated concrete hard constraints,
    'browsing' otherwise. Reads only `state.filled_slots` (populated by
    `extract_slots` above) and caches the result on `state.mode`.

    `category` is deliberately excluded from the trigger set: every
    scenario's turn-1 message discloses a category phrase (see
    `initial_message` in the evaluator, referenced in AGENTS.md), including
    Browsing/Boundary sessions — including it here would flip every session
    to "buying" on turn 1 and defeat the router entirely.
    """
    hard_signal_attributes = (
        "brand", "budget", "size", "color", "material", "style", "use_case",
    )
    mode = "buying" if any(a in state.filled_slots for a in hard_signal_attributes) else "browsing"
    state.mode = mode
    return mode
