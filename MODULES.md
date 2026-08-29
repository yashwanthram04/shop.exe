# Shopping Copilot — Module Contracts

Collated answers to the per-person integration questions. Owner: Person D (integration/QA).
Status: Person A — **open**, Person B — answered, Person C — answered.

---

## Person A — Retrieval

| Question | Answer |
|---|---|
| What is the exact query built for retrieval? | **TBD — not yet answered** |
| Are we using current-turn message only or slot state too? | **TBD — not yet answered** |
| What are the hard filters? | **TBD — not yet answered** |
| What is the fallback if embedding search fails? | **TBD — not yet answered** |

---

## Person B — Slot Extraction, Overrides, Staleness, Routing

**Interface contract:**
- `extract_slots(state, message) -> state` — mutates `state` in place (boundary/override handling, slot extraction, `durable_notes`). `agent.py` must call `state.advance_turn(turn)` first so slot timestamps land on the right turn.
- `classify_track(state) -> str` — reads only `state.filled_slots` (no raw message), caches result on `state.mode`.
- Neither function raises on normal input; regex/keyword misses are no-ops, not exceptions. `agent.py`'s whole-turn try/except is the only safety net needed.

**What slots are extracted from each message?**
All 9 attributes: `category, material, color, size, style, brand, budget, feature, use_case`.
- If `state.last_asked` is set and the message matches the evaluator's answer template ("For that, what matters is: X; Y."), the value is trusted directly — no keyword guessing. Multiple facts in one reply join into a single slot as `"cotton; leather"`.
- Otherwise (turn 1, an override, or unrecognized text): classify by regex/keyword. `category` comes from the "I'm looking for X" prefix (near-100% reliable). `budget/material/color/size/style/use_case` via keyword lists. `brand` is best-effort/low-yield. No blind fallback bucket — unmatched filler text never falsely fills a slot.

**How are overrides handled?**
Detected by keyword markers ("actually", "ignore my earlier", etc.). `apply_override` classifies the new value's category, clears the specific stale slot the old preference was in, and sets the new one — a real replace, including cross-category (an old `style` value correctly clears if the override is actually a `material` constraint).

**How do we clear stale constraints?**
Two automatic mechanisms:
1. **Override clearing** (above) — only touches freeform/unprompted-sourced slots, never a slot the customer directly answered when asked (protected), and never `category`.
2. **Decay** — `state.decayed_slots(turn)` returns each slot with a confidence weight that drops with age (doesn't delete). Floored at 0.3 for freeform-sourced slots, 0.7 for directly-asked ones.

**How is browsing vs. buying decided?**
`classify_track(state)` → `"buying"` if any of `budget/material/color/size/style/use_case` is filled, else `"browsing"`. Recomputed every turn (not sticky), cached on `state.mode`. `category` is deliberately excluded — every turn-1 message discloses a category, so including it would flip every session to "buying" immediately.

---

## Person C — Attribute Selection, Scope, Ranking

**How do we decide what attribute to ask?**
`pick_attribute_to_ask()` builds the "open" list itself (unfilled, not `filled_null`), splits it into two tiers — never-asked-yet vs. already-asked-but-still-open (`asked_categories`) — and only uses tier two if tier one is empty. Within a tier: if catalog access is available, computes Shannon entropy per attribute over the current candidate pool; otherwise falls back to a fixed priority order. `category`/`brand` are always pushed to the back, since the simulated customer rarely answers about them regardless of catalog signal.

**What qualifies as "too broad"?**
`pool_is_too_broad()` — a per-track threshold (buying: 15, browsing: 40) that shrinks by 2 per turn, floored at 5. More patient early, forced to commit near the 10-turn cap.

**How does ranking combine retrieval score + preference fit + popularity?**
Weighted sum:
```
1.0 × retrieval_score + 0.4 × slot_fit + 0.15 × normalized_rating + 0.10 × normalized_popularity
```
`slot_fit` splits multi-value slots (e.g. `"cotton; wool"`) on `"; "` before matching against product text.

**What happens if ranking fails?**
Two layers: inside `rank()`, one malformed candidate falls back to its raw score rather than killing the whole sort; outside, `agent.py` wraps the whole turn in try/except and returns a safe empty response rather than crashing the session.

---

## Cross-cutting integration notes (Person D)

- **Call order dependency:** `agent.py` must call `state.advance_turn(turn)` before `extract_slots`, and must set `state.last_asked` correctly before the next `extract_slots` call whenever the bot asks a clarifying question (so B's answer-template match works). This is an implicit contract between whoever generates the bot's question and B's module.
- **`classify_track` is not sticky** — recomputed every turn. Anything downstream assuming "buying" mode persists once set will have a bug.
- **`filled_slots` vs `decayed_slots`** — confirm which callers (ranking, attribute selection) are supposed to use the raw vs. decay-weighted view; using the wrong one silently skips staleness handling.
- **Person A's answers are still missing** — retrieval query construction, message-only vs. slot-state input, hard filters, and embedding-failure fallback are all open and block a full picture of the retrieval → ranking → response pipeline.