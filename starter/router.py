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

import json
import os
import re

from .state import ATTRIBUTES, SessionState

# LLM message understanding (Groq). This is deliberately NOT used for the
# reranking stage (rank.py) — an A/B test there showed reshuffling the
# formula's own top 20 barely moved the score (+0.003, noise) for real
# added cost/latency. Free-text slot extraction is the genuine gap: a
# message like "lightweight dangle design for everyday wear" has no
# exploitable keyword pattern for regex, but an LLM understands it
# immediately (confirmed: extracts style="lightweight dangle",
# use_case="everyday wear" correctly where _classify_unprompted below
# would extract nothing at all).
GROQ_MODEL = "openai/gpt-oss-20b"
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
GROQ_TIMEOUT_SECONDS = 6  # a single turn shouldn't hang waiting on this call
LLM_EXTRACTION_SYSTEM_PROMPT = (
    "You extract shopping preference slots from a customer's message. "
    f"Valid keys: {', '.join(ATTRIBUTES)}. Only include a key if the "
    "message genuinely expresses a preference for it — ignore filler "
    "dialogue with no real preference (e.g. \"still exploring\", \"ask me "
    "about one specific attribute\") and return {} for those. For "
    "'budget', extract ONLY the bare numeric dollar amount as a string "
    "(e.g. \"25\", never \"under 25 dollars\"). Reply with ONLY a JSON "
    "object, no other text."
)

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


def _classify_unprompted_llm(message: str, usage: dict | None) -> dict[str, str] | None:
    """Real free-text understanding via Groq. Returns None on ANY failure —
    no GROQ_API_KEY set, network error, timeout, malformed/non-dict JSON —
    so the caller falls back to _classify_unprompted's regex/keyword
    matching. Must never raise: a crash here would count as a miss for the
    whole session (see AGENTS.md), so a bad/slow API call should degrade
    quietly to the existing deterministic path, never break a turn.

    `usage` (state.turn_usage, if given) is updated in place with real
    prompt/completion token counts — this is a genuine model call, not a
    $0 heuristic, and the contract's `usage` field should reflect that.
    """
    # Opt-in, not merely key-present. Measured on the 200 public sessions
    # (ISSUES.md #9): LLM extraction scores 0.8370 vs 0.8400 for the
    # regex path — no better, and it costs network dependency, latency,
    # per-run non-determinism, and money. Two structural reasons it loses
    # against THIS evaluator, whose customer messages are template-
    # generated and highly regular:
    #   1. It correctly reads "but I'm still exploring" as expressing no
    #      preference and returns {} — discarding the category, which is
    #      the one signal every turn-1 message reliably carries. Every
    #      Browsing session uses that phrasing.
    #   2. It normalizes/truncates ("watches wrist watches" -> "Watches"),
    #      losing retrieval specificity the verbatim regex value keeps.
    # Kept available because it is the genuinely right tool for real,
    # messy user input — just not for this simulator.
    if os.environ.get("USE_LLM_EXTRACTION", "").strip().lower() not in ("1", "true", "yes"):
        return None
    if not os.environ.get("GROQ_API_KEY"):
        return None
    try:
        from openai import OpenAI  # lazy: avoid requiring/loading this when no Groq key is set

        client = OpenAI(api_key=os.environ["GROQ_API_KEY"], base_url=GROQ_BASE_URL, timeout=GROQ_TIMEOUT_SECONDS)
        response = client.chat.completions.create(
            model=GROQ_MODEL,
            temperature=0,
            reasoning_effort="low",  # cuts latency/tokens sharply for this small extraction task
            messages=[
                {"role": "system", "content": LLM_EXTRACTION_SYSTEM_PROMPT},
                {"role": "user", "content": message},
            ],
        )

        if usage is not None and response.usage:
            usage["prompt_tokens"] = usage.get("prompt_tokens", 0) + response.usage.prompt_tokens
            usage["completion_tokens"] = usage.get("completion_tokens", 0) + response.usage.completion_tokens

        content = (response.choices[0].message.content or "").strip()
        if content.startswith("```"):
            content = content.strip("`")
            content = content.split("\n", 1)[-1] if "\n" in content else content
        parsed = json.loads(content)
        if not isinstance(parsed, dict):
            return None
        return {
            str(key): str(value).strip()
            for key, value in parsed.items()
            if key in ATTRIBUTES and value not in (None, "")
        }
    except Exception:
        return None


def _understand_unprompted(message: str, usage: dict | None) -> dict[str, str]:
    """Entry point for freeform text: real LLM understanding when a Groq
    key is available, the regex/keyword fallback otherwise. Same shape
    either way — nothing downstream needs to know which path ran.
    """
    llm_result = _classify_unprompted_llm(message, usage)
    return llm_result if llm_result is not None else _classify_unprompted(message)


def _classify_unprompted(message: str) -> dict[str, str]:
    """Classify freeform, unprompted text (turn 1's opening message, or
    anything not matching the answer templates below) into slots.

    Deliberately does NOT fall back to a blind "feature" default the way
    `classify_single` does for override text — turn-1/unprompted messages
    can also be filler dialogue (e.g. the generic "still exploring" tail, or
    the null-ask nudge "Those options are not quite right yet..."), and a
    blind default would poison a slot with that filler text. Missing a
    genuine feature this way is an accepted precision-over-recall tradeoff.
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


def extract_slot_values(message: str, last_asked: str | None = None, usage: dict | None = None) -> dict[str, str]:
    """Pull structured {attribute: value} pairs out of free text. Pure,
    stateless classifier — see `extract_slots` below for the stateful entry
    point `agent.py` actually calls.

    If `last_asked` is given and the message matches the evaluator's answer
    template ("For that, what matters is: X; Y."), the value(s) are trusted
    directly with no classification (free, no LLM call) — the simulator
    already filtered them to match `last_asked` before sending them (see
    AGENTS.md). Handles multiple facts in one message by joining them back
    with "; " into the single slot value (e.g. "cotton; leather") rather
    than changing the return shape — retrieval.py/rank.py already treat
    slot values as opaque strings, so this needs no downstream coordination;
    split on "; " later if needed.

    EXCEPTION: `last_asked == "other"` is not a real attribute, so its
    answer can't be blindly attributed to one slot — the evaluator matches
    "other" against ANY undisclosed fact regardless of type (see
    AGENTS.md's ISSUES.md-linked note on `ask_attribute: "other"`), so a
    reply can legitimately mix a material AND a budget in one answer. Each
    disclosed value is classified individually (`classify_single`, the same
    priority order the evaluator itself uses) into its real attribute
    instead — otherwise every fact revealed this way would land in a
    `filled_slots["other"]` key nothing downstream ever reads, silently
    discarding real information.

    Falls back to real understanding (`_understand_unprompted`, LLM-first)
    for anything else: turn 1's opening disclosure, an override message, or
    an unrecognized shape (e.g. a differently-phrased private-set
    simulator) — this is the only branch that ever costs a model call,
    since the template match above is free and already 100% certain.
    """
    if last_asked:
        stripped = message.strip()
        answer_match = ANSWER_TEMPLATE_RE.match(stripped)
        if answer_match:
            values = [v.strip() for v in answer_match.group(1).split(";") if v.strip()]
            if values:
                if last_asked == "other":
                    result: dict[str, str] = {}
                    for value in values:
                        classified = classify_single(value)
                        if classified is None:
                            continue
                        attribute, classified_value = classified
                        result[attribute] = (
                            f"{result[attribute]}; {classified_value}" if attribute in result else classified_value
                        )
                    return result
                return {last_asked: "; ".join(values)}
        if NO_MORE_TEMPLATE_RE.match(stripped):
            return {}

    return _understand_unprompted(message, usage)


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

    MERGES into the target attribute rather than blindly overwriting it
    (ISSUES.md #7): `classify_single`'s fallback bucketing means unrelated
    facts often land under the same generic key (e.g. two different
    disclosed facts neither matching a specific material/color/etc. word
    list both become "feature"). If that key already holds a value, a
    plain overwrite would silently destroy a fact that was never actually
    contradicted — only the OLD stated preference is supposed to be
    replaced (handled by `clear_freeform_override` above, which targets
    the specific stale slot), not whatever else happens to share this new
    value's attribute bucket.
    """
    value_match = OVERRIDE_VALUE_RE.search(message)
    target_text = value_match.group(1) if value_match else message
    classified = classify_single(target_text)
    if classified is None:
        return False
    attribute, value = classified
    state.clear_freeform_override(except_attribute=attribute)
    existing = state.filled_slots.get(attribute)
    if existing:
        existing_parts = existing.split("; ")
        if value not in existing_parts:
            value = f"{existing}; {value}"
        else:
            # Already present as part of a bigger compound value — keep the
            # fuller existing string. Overwriting with just `value` here
            # would be the exact same destructive downgrade this fix is
            # for, just via a different path (dropping the sibling fact
            # instead of never merging it in).
            value = existing
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
    # BUGFIX (found by adversarial testing, not in ISSUES.md): a disclosed
    # constraint can coincidentally contain an OVERRIDE_PHRASES substring
    # (e.g. a product feature mentioning "forget" or "instead") — without
    # this guard, detect_override_signal() would fire on a completely
    # normal trusted-template answer and misroute it into apply_override(),
    # which uses lossy single-value classification and can wipe an
    # unrelated slot via clear_freeform_override(), instead of the correct
    # multi-value trusted extraction extract_slot_values() already handles
    # for this exact message shape. The boundary check above already gets
    # this kind of priority over general text heuristics; override
    # detection didn't, and needed the same guard. Confirmed reachable
    # (not just theoretical): 1/200 public-set intent cards already
    # contains such a collision (public_0168, "forget" in a disclosed
    # bracelet-feature constraint) — it happened not to matter there only
    # because that session hit on turn 1, before the colliding text was
    # ever disclosed via a customer reply.
    if state.last_asked:
        stripped = message.strip()
        if ANSWER_TEMPLATE_RE.match(stripped) or NO_MORE_TEMPLATE_RE.match(stripped):
            state.override_detected = False
    if not (state.override_detected and apply_override(state, message, state.turn)):
        extracted = extract_slot_values(message, last_asked=state.last_asked, usage=state.turn_usage)
        for attribute, value in extracted.items():
            # A direct answer to our own question deserves "asked"
            # confidence even when `last_asked == "other"` — the value
            # still came from a direct reply to a question we asked, it
            # just wasn't attributable to one named slot until classified
            # (see extract_slot_values' "other" handling above).
            source = "asked" if attribute == state.last_asked or state.last_asked == "other" else "freeform"
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
