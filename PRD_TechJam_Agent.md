# Product Requirements Document
## TechJam Conversational E-Commerce Search Challenge — Agent Architecture

**Status:** Architecture locked, ready for implementation
**Scope:** Full specification of the `Agent` class (`reset`/`respond`) per the required interface in `competition_specification.md` and `submission_rules.md`

---

## 1. Objective

Build a multi-turn shopping `Agent` that finds a hidden target product as early and as highly ranked as possible, across four scenario types (Buying 40%, Browsing 40%, Intent Override 15%, Boundary 5%), while remaining reproducible, latency-bounded, and functional under a network-disabled final judging environment.

**Scored objective function:**
```
TechnicalScore = 0.50 × HitRate@10 + 0.30 × MRR + 0.20 × Efficiency
Efficiency = clip((11 - MTTC) / 10, 0, 1)
```
Every architectural decision below is made in service of this function, subject to the hard constraint that the submission must degrade gracefully (never crash, never hang) if network/LLM access is disabled at final scoring.

---

## 2. High-Level Architecture

```
                         ┌─────────────────────────────┐
                         │      BUILD-TIME (offline)     │
                         │  Catalog index (BM25+Dense)   │
                         │  Attribute table + taxonomy   │
                         │  Invalidation graph            │
                         │  Popularity table               │
                         │  Prompt templates + thresholds │
                         └───────────────┬─────────────┘
                                         │ loaded at Agent init
                                         ▼
 reset(session_id, profile) ──► Session State Store (per session_id)
                                         │
                                         ▼
              ┌───────────────────────────────────────────────┐
              │            respond() — PER TURN PIPELINE        │
              │                                                  │
              │ 1. Input intake & exclusion update                │
              │ 2. Heuristic override/boundary detection          │
              │ 3. Combined LLM call (slots+notes+message)        │
              │    [fallback: dictionary match]                   │
              │ 4. Slot merge + invalidation graph application    │
              │ 5. Mode re-derivation (Buying/Browsing)           │
              │ 6. Retrieval (BM25 or Dense, per mode)            │
              │ 7. Constraint filtering (hard/soft)               │
              │ 8. Merge + adaptive pool sizing                   │
              │ 9. LLM reranker [fallback: deterministic merge]   │
              │ 10. Deterministic ask_attribute selection          │
              │ 11. Output assembly + validation                  │
              │ 12. Telemetry logging                              │
              └───────────────────────────────────────────────┘
```

---

## 3. Component Specifications

Each component below states: **Decision**, **Rationale**, **Spec**, **Inputs/Outputs**, **Edge Cases**.

### 3.1 Session State Store

**Decision:** In-memory dict keyed by `session_id`, held for the lifetime of the Agent process.

**Spec — state object fields:**
```python
SessionState = {
    "mode": "buying" | "browsing",          # re-derived every turn, see 3.5
    "filled_slots": {                         # 9 attribute categories
        "category": <value|None>, "material": ..., "color": ...,
        "size": ..., "style": ..., "brand": ..., "budget": ...,
        "feature": ..., "use_case": ...,
    },
    "filled_null": set(),                     # categories explicitly answered "no preference"
    "asked_categories": set(),                 # categories the agent has asked about
    "excluded_asins": set(),                   # cumulative, session-wide (Decision D)
    "durable_notes": "",                       # free-text, from combined call (Decision F)
    "dialog_window": deque(maxlen=3),          # sliding window, raw text (Decision G)
    "turn": 0,
    "last_boundary_attribute": None,           # which attribute (if any) got a no-pref answer this turn
}
```

**Inputs:** `reset(session_id, user_profile)` initializes this object; `user_profile` is stored separately, read-only, used only in §3.9 as a tie-breaker.

**Edge cases:**
- `respond()` called without prior `reset()` → raise, matching starter agent's existing guard.
- Session state must be evicted/bounded if the harness runs many sessions in one process (simple LRU or explicit cleanup after turn 10 / hit).

---

### 3.2 Input Intake & Exclusion Update

**Decision (D):** Cumulative, session-wide exclusion — not last-turn-only.

**Rationale:** The evaluator only tells you last turn's misses, but nothing stops the Agent from remembering every ASIN it has ever shown this session. Cumulative exclusion strictly dominates last-turn-only: it never wastes a scored slot re-surfacing a confirmed miss, at the cost of a trivial `set.update()`.

**Spec:**
```python
def on_turn_start(state, last_turn_wrong_asins):
    state["excluded_asins"].update(last_turn_wrong_asins)
    state["turn"] += 1
```
This exclusion set is applied as a hard filter at the retrieval stage (§3.7), before any scoring/reranking — excluded ASINs must never enter the candidate pool at all, not just be de-ranked.

---

### 3.3 Heuristics-First Override/Boundary Detector

**Decision (build-time #9, narrow scope):** Only override and boundary/no-preference detection get a heuristic pass. All slot value extraction always goes to the LLM (§3.4) — heuristics do not attempt to extract attribute values.

**Rationale:** Override and boundary signals cluster around a small, learnable set of discourse markers ("actually," "never mind," "whatever," "either is fine," "no preference"), making them cheap and reliable to catch with keyword/regex matching. Slot values (material, style, price phrasing, etc.) are far more open-ended and don't compress well into a fixed rule set — sending them to the LLM every turn is the right cost/accuracy tradeoff, especially since that same call is already happening for message generation (§3.4).

**Spec:**
- Maintain two keyword/regex lists, built once at build-time (§6.4):
  - `OVERRIDE_MARKERS`: e.g. "actually", "no wait", "instead", "change my mind", "never mind that", "on second thought"
  - `BOUNDARY_MARKERS`: e.g. "whatever", "either is fine", "doesn't matter", "no preference", "any (X) works", "i don't care"
- Run both regex passes on `user_message` (case-insensitive, tokenized).
- **If a clear match is found → heuristic result is authoritative, no LLM escalation for this specific detection.**
- **If the message is ambiguous** (e.g., contains negation combined with a slot value in a way regex can't safely resolve — "actually not blue, but I still want the sneakers" mixes override + retained constraint) → flag `escalate_to_llm=True`, and this flag is passed as a hint into the combined call (§3.4) so the LLM's slot-merge output also confirms/refines override scope.
- Boundary detection is specifically checked against `state["asked_categories"]` — a boundary signal only resolves an attribute if that attribute was the one most recently asked about (`last_asked_attribute`). A bare "whatever" with no prior question is not a boundary event; log it but do not mark any category filled-null.

**Output:** `{override_detected: bool, boundary_detected: bool, boundary_target_attribute: str|None, escalate_to_llm: bool}` — passed into §3.4.

**Edge cases:**
- Turn 1 can never have `boundary_detected=True` (nothing was asked yet) — guard explicitly.
- Multiple markers in one message (e.g. both an override and a boundary phrase) → both flags can be true simultaneously; downstream merge logic (§3.4) must handle both.

---

### 3.4 Combined LLM Call (Slot Extraction + Durable Notes + Message Generation)

**Decision (A + F):** Single combined LLM call per turn returns three things together: extracted slot updates, a free-text `durable_notes` field, and the customer-facing `message`. No separate summarization call, no separate message-generation call.

**Rationale:** Since slot extraction must hit the LLM every single turn regardless (narrow heuristic scope, §3.3), bundling message phrasing and durable-memory notes into the same call costs zero extra round trips. This directly protects `Efficiency` (turn latency) and keeps per-turn cost close to one call, which also simplifies the degrade-in-place fallback surface to a single call site instead of three.

**Decision (G):** Context window = sliding window of last 2–3 raw turns + current filled-slot state + `durable_notes` + current message. Not full history, not slot-state-only.

**Rationale:** Full history grows unbounded token cost across up to 10 turns for no real benefit once slots capture resolved facts. Slot-state-only risks losing recent tone/phrasing nuance (hedging, sarcasm, a not-yet-resolved reference) that hasn't made it into `durable_notes` yet. A 2–3 turn window is the cheap middle ground.

**Spec — prompt input assembly (deterministic, built by code before the call):**
```python
llm_input = {
    "current_message": user_message,
    "recent_turns": list(state["dialog_window"]),       # last ≤3 raw (agent_msg, user_msg) pairs
    "filled_slots": state["filled_slots"],
    "filled_null": list(state["filled_null"]),
    "durable_notes": state["durable_notes"],
    "heuristic_hints": {                                  # from §3.3
        "override_detected": ...,
        "boundary_detected": ...,
        "boundary_target_attribute": ...,
    },
    "target_ask_attribute": chosen_attribute_or_null,     # from §3.10 — DETERMINISTIC, passed in, not decided by LLM
}
```

**Spec — required LLM output schema (strict JSON):**
```json
{
  "slot_updates": {
    "category": "string|null", "material": "string|null", "color": "string|null",
    "size": "string|null", "style": "string|null", "brand": "string|null",
    "budget": "string|null", "feature": "string|null", "use_case": "string|null"
  },
  "explicit_no_preference": ["list of category names the user just said they don't care about"],
  "override_confirmed": true,
  "override_scope": ["list of category names actually being replaced, empty if none"],
  "durable_notes": "updated free-text preference summary, ≤300 chars",
  "message": "natural language reply to the customer, must reference target_ask_attribute if one was provided"
}
```
- Only categories with a non-null new value are merged into `filled_slots`; unmentioned categories are left untouched.
- `explicit_no_preference` entries are added to `filled_null` and removed from any future entropy scoring (§3.10).
- If `target_ask_attribute` was passed in, the LLM's job is ONLY to phrase a question about it — **the LLM must not introduce a different `ask_attribute` on its own**; that field in the final output always comes from §3.10, not from this call.

**Edge cases / validation:**
- LLM returns malformed JSON, an out-of-vocabulary category name, or hallucinated attribute values not present in the catalog vocabulary → reject that field, keep prior slot value, log a validation failure event (this counts toward the fallback trigger if failures are frequent — see §3.5).
- LLM call times out or errors → invoke §3.5 fallback immediately; do not retry more than once (retries cost turn latency against the 10-turn cap).

---

### 3.5 Slot-Extraction Fallback (Dictionary Match)

**Decision (build-time #10):** Reuse the same catalog vocabulary built in §6.2 (per-product structured attribute table) as a dictionary for runtime fallback extraction.

**Rationale:** Building a second, independent vocabulary purely for the fallback path duplicates the taxonomy effort for no real benefit — the catalog-side vocabulary already contains every known value per attribute category, and matching the message against it directly. This also guarantees consistency between what the catalog "speaks" and what the fallback recognizes.

**Spec:**
```python
def fallback_extract(user_message, catalog_vocab):
    matches = {}
    for category, known_values in catalog_vocab.items():
        for value in known_values:
            if value.lower() in user_message.lower():
                matches[category] = value
                break
    # numeric fields handled separately via regex, not vocab lookup
    matches.update(extract_price_regex(user_message))    # "under $50", "less than 50 dollars"
    matches.update(extract_size_regex(user_message))      # "size 8", "medium", "M"
    return matches
```
- This fallback **only** covers structured/enumerable fields well (category, budget, size, color, brand). Freeform categories (style, material as adjectives, feature, use_case) will have lower recall in fallback mode — this is an accepted, documented degradation, not a bug.
- `durable_notes` and `message` have no LLM-driven fallback equivalent: in fallback mode, `message` is generated from a small set of pre-written templates keyed by scenario/mode/ask_attribute (built at build-time, §6.4), and `durable_notes` simply stops updating (last known value is retained, not regenerated).

**Trigger condition:** Any of — LLM call exception, timeout beyond a configured budget, network probe failure at Agent init (used to pre-decide "LLM mode" vs "fallback mode" for the whole session up front, avoiding a timeout tax on every turn once it's known the LLM is unreachable).

---

### 3.6 Override-Invalidation Graph

**Decision (build-time #7):** Small, conservative, hand-authored graph — not aggressive clear-all, not a full 9×9 pairwise graph.

**Rationale:** Aggressive clear-all discards useful constraints unnecessarily (a color change shouldn't force re-asking budget). A full pairwise graph requires reasoning through 36 category-pair relationships with diminishing returns on the rare (15%) Intent Override scenario. A small, targeted graph capturing the clearly-necessary invalidations gets most of the benefit for a fraction of the design effort.

**Spec — starter table (to be refined by the team's domain judgment before final submission):**

| Overridden category | Invalidates |
|---|---|
| `category` | `style`, `size`, `use_case` |
| `size` | *(none — size is category-independent enough to survive most overrides)* |
| `budget` | *(none)* |
| `brand` | *(none)* |
| `style`, `material`, `color`, `feature`, `use_case` | *(none — leaf attributes, don't cascade)* |

```python
INVALIDATION_GRAPH = {
    "category": {"style", "size", "use_case"},
}

def apply_override(state, overridden_categories):
    for cat in overridden_categories:
        for invalidated in INVALIDATION_GRAPH.get(cat, set()):
            state["filled_slots"][invalidated] = None
            state["filled_null"].discard(invalidated)
            state["asked_categories"].discard(invalidated)  # allow re-asking
```

**Note:** This table is intentionally sparse at design time — expand it only if local evaluation against the 200 public sessions shows a specific category pivot causing repeated bad recommendations from a stale slot.

---

### 3.7 Retrieval Layer

**Decision (build-time #1 + per-turn E):** Hybrid BM25 + local dense embeddings, with the channel selected per turn by a continuously re-derived mode, not a sticky label.

**Decision (E):** Mode = `"buying"` if **any** hard-constraint slot (`category`, `budget`, `size`, `brand`, `color` — see §3.8 for the hard/soft split) is currently filled; else `"browsing"`. Recomputed fresh every turn from `state["filled_slots"]`, independent of whether an override flag fired this turn.

**Rationale:** The agent's true objective each turn is "what retrieval channel maximizes hit probability right now," not "what type of session is this." A session that starts vague (Browsing) but reveals a price constraint on turn 5 should immediately benefit from filter-heavy retrieval — waiting for an explicit "override" phrase to unlock that would leave hit-rate on the table for no reason. Continuous re-derivation is strictly more responsive and costs nothing extra to compute (it's just a boolean check on already-maintained state).

**Spec:**
```python
HARD_CONSTRAINT_FIELDS = {"category", "budget", "size", "brand", "color"}

def derive_mode(state):
    if any(state["filled_slots"][f] is not None for f in HARD_CONSTRAINT_FIELDS):
        return "buying"
    return "browsing"
```

**Channel selection per turn:**
| Scenario condition | Channel |
|---|---|
| `mode == "buying"` | BM25 (filter-heavy) |
| `mode == "browsing"` | Dense (embedding cosine similarity) |
| `boundary_detected == True` (this turn) | Dense-only, overriding whatever `mode` says |
| Override just applied | Re-derive `mode` fresh post-invalidation (§3.6), then use that channel |

**BM25 spec:** Reuse/extend the starter's SQLite FTS5 virtual table approach; query built from filled slot values + any unfilled freeform terms from the message, `AND`-combined with hard filters (§3.8).

**Dense spec:** Local sentence-transformer embeds `user_message + durable_notes` as the query vector; cosine similarity against the precomputed catalog embedding matrix (§6.1); embeddings and index must be loadable with zero network calls at runtime.

---

### 3.8 Constraint Filtering (Hard/Soft Hybrid)

**Decision (B):** Hybrid — hard-filter on reliably-parsed fields, soft-boost on subjective ones.

**Rationale:** Pure strict filtering risks a catastrophic failure mode: if slot extraction mis-parses a value (e.g., wrong price threshold), the true target gets excluded from the candidate pool **permanently for the rest of the session**, not just missed this turn — since exclusion happens at the retrieval source, not the ranking stage. Pure soft-boost avoids that risk entirely but lets the agent recommend obviously out-of-range items, hurting perceived quality on fields where parsing confidence is genuinely high. The hybrid takes hard filtering only where confidence is structurally high (numeric/enum fields), and soft-boosts everywhere parsing is inherently fuzzier.

**Spec — field classification:**
| Field | Filter type | Rationale |
|---|---|---|
| `category` | Hard | Near-zero parse ambiguity; wrong category is almost never intended |
| `budget` | Hard | Numeric, regex-extractable with high confidence |
| `size` | Hard | Numeric/enum, high-confidence regex |
| `brand` | Hard | Discrete match against known brand list |
| `color` | Hard | Discrete match against known color vocabulary |
| `material` | Soft | Descriptive/adjectival, multiple valid phrasings |
| `style` | Soft | Highly subjective, fuzzy boundaries |
| `feature` | Soft | Open-ended, catalog `features` field is free text |
| `use_case` | Soft | Inferred, not literal |

```python
def apply_filters(candidates, filled_slots):
    for field in HARD_FILTER_FIELDS:
        value = filled_slots.get(field)
        if value:
            candidates = [c for c in candidates if matches(c, field, value)]
    for field in SOFT_BOOST_FIELDS:
        value = filled_slots.get(field)
        if value:
            for c in candidates:
                c.score += SOFT_BOOST_WEIGHT * similarity(c, field, value)
    return candidates
```
`SOFT_BOOST_WEIGHT` is a tunable constant (§6.5).

**Edge case — zero-pool safety net:** see §10.2 for the mandatory recovery step when hard filtering ever empties the candidate pool.

---

### 3.9 Candidate Merge, Adaptive Pool Sizing, and Personalization Tie-Break

**Decision (H):** Adaptive candidate pool size sent forward to the reranker, scaled by constraint confidence.

**Rationale:** A small fixed pool risks excluding the true target when few constraints are known (high uncertainty → need breadth). A large fixed pool wastes LLM reranking cost/latency once several hard constraints have already narrowed the field. Scaling pool size to the number of currently-filled hard-constraint fields captures this tradeoff automatically.

**Spec:**
```python
def pool_size(state):
    n_hard_filled = sum(1 for f in HARD_CONSTRAINT_FIELDS if state["filled_slots"][f])
    return max(15, 50 - 8 * n_hard_filled)   # exact constants tuned in §6.5 against public set
```

**Decision (§ user_profile personalization):** Tie-breaker only.

**Rationale:** `user_profile` is an anonymized aggregate (purchase frequency, rating summaries, preference tags) — it reflects historical behavior, not necessarily current intent. Using it as a primary ranking signal risks anchoring hard on stale history for a session whose live message contradicts it. Using it only to break near-ties between otherwise-equal candidates captures upside with near-zero downside risk.

**Spec:**
```python
def apply_profile_tiebreak(candidates, user_profile):
    for c in candidates:
        if c.store in user_profile.get("preferred_stores", []):
            c.score += TIEBREAK_EPSILON   # small constant, must never outweigh a real signal
    return candidates
```

---

### 3.10 LLM Reranker

**Decision (C):** Always invoke the LLM reranker on the (adaptively-sized) candidate pool, every turn.

**Rationale:** Given the pool is already small and adaptively sized (§3.9), the marginal cost of reranking it is bounded and predictable, while an "always-on" reranker gives the most consistent quality — it captures dialog nuance (subtle negation, relative phrasing like "something dressier than that") that pure lexical/embedding scores miss, directly serving the Innovation direction on semantic reranking called out in the spec.

**Spec:**
```python
reranker_input = {
    "candidates": [{"parent_asin": c.id, "title": c.title, "key_attrs": c.attrs} for c in pool],
    "dialog_context": recent_turns + durable_notes,
    "filled_slots": state["filled_slots"],
}
# LLM returns a reordered list of parent_asin, best-to-worst, length ≤ top_k
```
**Fallback (degrade-in-place):** If this call fails/times out, use the deterministic weighted merge score directly (BM25/dense score × filter-match × popularity × profile tie-break) with no reranking pass — this is not a quality-free fallback, but it is a functioning one.

---

### 3.11 Clarification Engine — `ask_attribute` Selection

**Decision (I):** Fully deterministic. The LLM never decides whether or what to ask — it only phrases the question once code has made the choice (fed in via `target_ask_attribute`, §3.4).

**Rationale:** `ask_attribute` indirectly shapes `HitRate`/`MRR` (it determines what information gets collected and when), unlike message wording, which affects neither. Keeping this decision deterministic keeps it reproducible, debuggable, and directly tunable against the public 200 sessions — exactly the kind of decision that should not be delegated to a harder-to-audit LLM judgment call.

**Spec — selection algorithm, run every turn before the combined LLM call:**
```python
def select_ask_attribute(state, candidate_pool):
    unfilled = [
        cat for cat in ALL_NINE_CATEGORIES
        if state["filled_slots"][cat] is None and cat not in state["filled_null"]
    ]

    # Rule A: boundary/no-pref just detected this turn → do not ask again this turn
    if heuristic_hints["boundary_detected"]:
        return None

    # Rule D: no unfilled categories remain → forced recommend-only
    if not unfilled:
        return None

    # Rule B: pool already small/confident → skip clarification
    if len(candidate_pool) <= CONFIDENCE_POOL_THRESHOLD:
        return None

    # Rule C: otherwise, pick max value-diversity attribute
    scores = {
        cat: diversity_score(candidate_pool, cat)
        for cat in unfilled
    }
    best = max(scores, key=scores.get)
    state["asked_categories"].add(best)
    return best

def diversity_score(pool, category):
    values = [c.attrs.get(category) for c in pool if c.attrs.get(category)]
    if not values:
        return 0.0
    counts = Counter(values)
    probs = [n / len(values) for n in counts.values()]
    return -sum(p * math.log(p) for p in probs)   # Shannon entropy; distinct-count is an acceptable simpler fallback
```
- `CONFIDENCE_POOL_THRESHOLD` is a tuned constant (§6.5), starting guess: `top_k` itself (i.e., if the pool is already ≤10, don't bother asking — just recommend). **Superseded by §10.4:** this is not a single fixed constant but a threshold that loosens as `state["turn"]` increases, and Rule D is forced (`ask_attribute: null`) unconditionally from turn 8 onward regardless of pool size.
- Categories in `state["asked_categories"]` that later got a no-preference response are moved to `filled_null` (via §3.4's `explicit_no_preference`) and are excluded from `unfilled` on all future turns — this is the "treat it as filled-null, not unfilled" rule from your original notes.
- **Pool-shrink recovery (§10.3):** before Rule C's entropy scoring runs, `unfilled` is additionally filtered to drop any category flagged by the pool-shrink tracker in §10.3 (i.e., a category the agent already asked about this session that did not measurably narrow the pool).

---

### 3.12 Output Assembly & Validation

**Mandatory, deterministic, every turn — no decision fork, only correctness guards:**
```python
def assemble_output(message, ask_attribute, ranked_asins, usage):
    assert isinstance(message, str)
    assert ask_attribute in ALLOWED_ASK_ATTRIBUTES or ask_attribute is None
    valid_unique = []
    seen = set()
    for asin in ranked_asins:
        if asin in CATALOG_VALID_ASINS and asin not in seen:
            valid_unique.append(asin)
            seen.add(asin)
        if len(valid_unique) == 10:
            break
    return {
        "message": message,
        "ask_attribute": ask_attribute,
        "recommendations": [{"parent_asin": a} for a in valid_unique],
        "usage": {"prompt_tokens": max(0, usage.get("prompt_tokens", 0)),
                   "completion_tokens": max(0, usage.get("completion_tokens", 0))},
    }
```
`ALLOWED_ASK_ATTRIBUTES = {category, material, color, size, style, brand, budget, feature, use_case, other}` per the spec's fixed enum.

---

### 3.13 Degrade-in-Place Fallback System (cross-cutting)

**Decision (build-time #3):** Every LLM call site has its own deterministic fallback; the agent never depends on all LLM calls succeeding to produce a valid turn.

**Spec — call sites and their fallback:**
| Call site | Normal path | Fallback |
|---|---|---|
| Override/boundary ambiguous case (§3.3) | LLM escalation | Treat as no override/no boundary (conservative default) |
| Combined slots+notes+message call (§3.4) | LLM | Dictionary match (§3.5) for slots; templated message; frozen `durable_notes` |
| Reranker (§3.10) | LLM | Deterministic weighted merge score, no reorder |

**Session-level network probe:** At `reset()` (or lazily on first LLM call failure), determine LLM availability once and cache it for the session, rather than re-attempting and re-timing-out on every turn — this protects `Efficiency`/MTTC from repeated timeout penalties within one session.

**Top-level guard:** Wrap the entire `respond()` body in a try/except; on any uncaught exception, return a minimal valid output (empty/best-effort recommendations from raw BM25 on the message text, `ask_attribute: null`) rather than raising — since "exceptions, invalid output, and timeouts may count as a miss" per the spec, a degraded valid response is strictly better than a crash.

---

### 3.14 Telemetry & Usage Logging

Deterministic instrumentation, every turn:
- Sum `prompt_tokens`/`completion_tokens` across every LLM call actually made this turn (combined call + reranker + any escalation call) into the `usage` field.
- Log per-call latency locally (not returned to evaluator) for the team's own cost/latency disclosure in the final report.
- Log fallback-trigger events (which call site, why) to support the "offline fallback" disclosure required by `submission_rules.md`.
- **Local debug log (§10.6):** in addition to the above, emit one structured per-turn debug record — mode, filled slots, which attribute won entropy scoring and why, retrieval channel used, pool size before/after filtering, and whether any fallback fired. Local-only, never sent to the evaluator; built in week one since it costs nothing at runtime.

---

## 4. Build-Time Artifacts (Offline Pipeline)

### 4.1 Catalog Indexing
- **BM25:** SQLite FTS5 virtual table over `title`, `categories`, `features`, `details`, `store`, `description` — extend the starter's existing approach.
- **Dense:** Local sentence-transformer model embeds a concatenation of the same fields per product; store as a flat matrix (e.g., numpy array + ID list) for cosine similarity at runtime. No network calls at runtime — model weights must be bundled or downloadable once at build time and shipped/cached.

### 4.2 Attribute Taxonomy & Per-Product Structured Table
**Decision (build-time #5):** Hybrid regex-first, LLM-on-failure, run once offline.
- Pass 1: regex/dictionary extraction per catalog field, mapped to the 9 fixed categories (field-position heuristics, e.g. a `details.Material` key maps directly to `material`).
- Pass 2: for products where Pass 1 yields low coverage (e.g. <3 of 9 categories filled), send to an LLM batch job to extract remaining attributes.
- Output: a structured table (`parent_asin → {9 category values}`) bundled as a local asset (allowed under `submission_rules.md`), plus the derived **vocabulary lists per category** — this vocabulary is reused directly by the runtime fallback (§3.5).
- **Size check required:** confirm the bundled table stays within reasonable size for a submitted local asset before finalizing.

### 4.3 Popularity Table
**Decision (build-time #8):** Category-relative percentile ranking.
```python
def build_popularity_table(catalog):
    by_category = group_by(catalog, "category")
    for category, products in by_category.items():
        rating_pct = percentile_rank([p.average_rating for p in products])
        volume_pct = percentile_rank([p.rating_number for p in products])
        for p, r, v in zip(products, rating_pct, volume_pct):
            p.popularity_score = 0.5 * r + 0.5 * v   # weight tunable, §6.5
```
Used only for Boundary-scenario fallback ranking when no other signal is available.

### 4.4 Prompt Template Library
Written once, versioned:
- Combined call system prompt (§3.4 schema)
- Reranker prompt (§3.10 schema)
- Ambiguous override/boundary escalation prompt (§3.3)
- Templated fallback messages, keyed by `(mode, ask_attribute|None)` — used only in fallback mode

### 4.5 Tuning Loop (repeats throughout build, not a one-time task)
Run `evaluator.local_evaluator` against the 200 public sessions to tune:
- Hybrid merge weights (BM25 vs dense vs popularity vs profile-tiebreak)
- `SOFT_BOOST_WEIGHT`, `TIEBREAK_EPSILON`
- `CONFIDENCE_POOL_THRESHOLD` for clarification skip
- Adaptive pool-size formula constants (§3.9)
- Compare metrics per-scenario (Buying/Browsing/Override/Boundary) since the same weights may not be optimal across all four.

---

## 5. Data Contracts

### 5.1 Required Agent Interface (must not deviate)
```python
class Agent:
    def reset(self, session_id: str, user_profile: dict) -> None: ...
    def respond(self, session_id: str, user_message: str, turn: int, top_k: int) -> dict:
        return {
            "message": str,
            "ask_attribute": str | None,   # one of the fixed enum, or null
            "recommendations": [{"parent_asin": str}, ...],   # ≤10, valid, unique, best-to-worst
            "usage": {"prompt_tokens": int, "completion_tokens": int},
        }
```

### 5.2 Fixed `ask_attribute` Enum
`category, material, color, size, style, brand, budget, feature, use_case, other, null`

---

## 6. Decision Register

| # | Decision Point | Chosen | Key Rationale |
|---|---|---|---|
| 1 | Base retrieval architecture | Hybrid BM25 + dense, routed per-turn by scenario | Covers both Buying (exact) and Browsing (semantic) without over-building |
| 2 | LLM call structure | Heuristics-first cascade | Lowest average cost, natural degradation path |
| 3 | Offline fallback strategy | Degrade-in-place | Mandatory per submission rules; pairs with cascade |
| 4 | Embedding model | Local runtime, hosted for dev only | Must survive network-disabled judging |
| 5 | Attribute extraction method | Hybrid regex-first, LLM on failure | Balances coverage and one-time build cost |
| 6 | `user_profile` usage | Tie-breaker only | Avoids anchoring on stale aggregate history |
| 7 | Override-invalidation graph | Conservative, small, hand-authored | Avoids discarding useful constraints unnecessarily |
| 8 | Popularity formula | Category-relative percentile | Fair across categories with different rating norms |
| 9 | Heuristics-cascade scope | Narrow (override/boundary only) | Slot values are too open-ended for regex |
| 10 | Slot-extraction fallback | Reuse catalog vocabulary | Avoids duplicating taxonomy effort |
| A | LLM call bundling | Single combined call | Zero extra round trips since slots call LLM anyway |
| B | Constraint filtering strictness | Hybrid hard/soft | Avoids permanently excluding true target on parse error |
| C | Reranker trigger | Always | Pool is already small/adaptive, cost is bounded |
| D | Exclusion scope | Cumulative, session-wide | Never wastes a scored slot on a repeat miss |
| E | Mode determination | Continuous re-derivation | Reacts to constraints as soon as they appear |
| F | Memory/summarization | Folded into combined call as `durable_notes` | No redundant LLM call |
| G | Context window | Sliding window + slot state | Bounded cost, retains recent nuance |
| H | Reranker pool size | Adaptive | Balances recall vs cost by constraint confidence |
| I | `ask_attribute` decision locus | Fully deterministic | Reproducible, directly tunable, protects scored metrics |
| J | Scenario handler coverage | Scoped to the four official scenarios only; no comparison-request or stalled/confused handlers | Simulated customer never reacts to specific recommendations and won't produce true out-of-distribution scenarios — extra handlers are unexercised complexity |
| K | Empty-pool recovery | Immediate drop of the most recently applied hard filter, re-retrieve, before anything else that turn | A zero pool is the single worst failure mode (zero hit chance for the rest of the session) |
| L | Bad-turn recovery signal | Pool-shrink tracking, not a full self-critique loop | Self-critique is hard to calibrate and a miscalibrated version can actively hurt performance; pool-shrink is a cheap, auditable proxy |
| M | Clarification aggressiveness over time | Turn-budget-driven threshold curve (loosens turns 5–7, forced recommend-only turn 8+) | A miss costs the same on turn 8 as turn 10, but extra questions tax Efficiency directly |
| N | State-change triggers | Auditable only: new fact learned, turn threshold crossed, zero-pool event — never a soft/subjective signal | Prevents a second, uncontrolled axis of adaptivity that would cause erratic flip-flopping |
| O | Local debug log (explainability) | Per-turn structured log, local-only, built in week one | Costs nothing at runtime; pays for itself the first time a session needs tracing during tuning |
| P | Overall strategy shape | Named-policy backbone (scoped to 4 scenarios) + turn-budget aggressiveness, with two narrow confidence-gating tripwires (zero-pool, pool-shrink); full self-critique loop explicitly rejected | Matches a scripted, non-adaptive simulated customer — general-purpose adaptivity is unrewarded complexity |

---

## 7. Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Combined-call JSON malformed/hallucinated values | Per-field validation against catalog vocabulary before merge; reject bad fields individually rather than discarding the whole response |
| Hard filter excludes true target due to parse error | Hard-filter only high-confidence fields (§3.8); everything else soft-boosted |
| LLM timeout eats turn latency budget repeatedly | Session-level network probe/cache, not per-turn retry |
| Invalidation graph too sparse, stale slots persist | Flagged as tunable; expand only where local eval shows repeated failures |
| Reranker always-on inflates cost/latency | Adaptive pool sizing keeps the reranked list small when constraints are already known |
| Bundled attribute table too large for submission | Explicit size check during §6.2 build |

---

## 8. Reproducibility & Submission Compliance Checklist

- [ ] Document exact Python version requirement (if non-default)
- [ ] `requirements.txt` with pinned versions (local embedding model dependency included)
- [ ] One command to run the agent in the official harness
- [ ] Document any non-obvious environment variables (LLM API keys via env var only, never committed)
- [ ] Explicitly disclose: does the system require network access? (Yes, for the combined LLM call and reranker) — describe the offline fallback behavior (§3.5, §3.13) per `submission_rules.md`
- [ ] Disclose model choice, approximate cost, token usage, latency in the final report
- [ ] Confirm no private evaluation data, organizer-only files, or evaluator modifications are included in the submission bundle

---

## 9. Open Items for the Tuning Loop

These are intentionally **not** finalized in this PRD — they require empirical tuning against the 200 public sessions before final submission:

1. Exact hybrid merge weights (BM25 / dense / popularity / profile-tiebreak)
2. `CONFIDENCE_POOL_THRESHOLD` per-band values (turns 1–4 / 5–7 / 8+) — §10.4 fixes the shape of the curve, not the numbers
3. Adaptive pool-size formula constants
4. `SOFT_BOOST_WEIGHT` and `TIEBREAK_EPSILON` magnitudes
5. Whether the override-invalidation graph (§3.6) needs additional edges beyond the starter table
6. Per-scenario metric breakdown to check whether any one scenario type is dragging down aggregate `TechnicalScore` disproportionately
7. What counts as "didn't shrink meaningfully" for the pool-shrink recovery signal (§10.3) — an absolute count drop vs. a percentage threshold
8. Whether the turn-band boundaries themselves (4 / 7 / 8) hold up against the 200 public sessions, or need shifting per-scenario

---

## 10. Addendum: General Robustness Decisions

This addendum resolves the remaining "how adaptive should the agent be" question left open by the Decision Register (§6) and layers a small set of robustness rules onto the components already specified in §3. Nothing here replaces §3.6, §3.8, §3.11, or §3.14 — each subsection below states exactly which existing spec it extends. Decision Register rows J–P (§6) summarize these.

### 10.1 Scenario Coverage Boundaries

**Decision (J):** No handlers beyond the four official scenario types (Buying, Browsing, Intent Override, Boundary, per §1).

**Rationale:** The simulated customer never reacts to a specific recommendation the agent makes, so comparison requests ("which is more durable") are out of distribution for this evaluator and building a handler for them is speculative complexity with no scoring upside. Similarly, a "stalled/confused" customer is not a distinct scenario — it is the degenerate case of Browsing where several turns in a row fail to fill any slot. It doesn't need its own branch; it needs the existing Browsing path to degrade gracefully (bounded latency, no crash, no infinite re-asking of the same category) under repeated zero-fill turns.

**Spec:**
- Do not add a comparison-request intent or handler.
- Do not add a separate stalled/confused mode or state field. Verify instead, as part of local evaluation (§4.5), that a run of several consecutive turns with zero newly-filled slots still produces valid, non-repeating `ask_attribute` choices and does not loop or stall — this is a test case against the existing Browsing path, not new production code.

---

### 10.2 Zero-Pool Safety Net

**Decision (K):** If the candidate pool ever reaches zero after hard filtering (§3.8), the agent immediately drops the most recently applied hard filter and re-retrieves, before doing anything else that turn.

**Rationale:** An empty candidate pool means zero hit probability for every remaining turn in the session — this is strictly the worst failure mode the agent can enter, worse than any single bad recommendation, so it gets priority over every other per-turn rule including clarification selection (§3.11) and reranking (§3.10).

**Spec:**
```python
def retrieve_with_safety_net(state, filled_slots):
    filter_order = state.get("hard_filter_apply_order", [])  # most-recent last
    candidates = apply_filters(retrieve(state, filled_slots), filled_slots)
    while not candidates and filter_order:
        dropped = filter_order.pop()
        filled_slots = {**filled_slots, dropped: None}
        candidates = apply_filters(retrieve(state, filled_slots), filled_slots)
    return candidates, filled_slots
```
- This runs inline in the retrieval step (§3.7/§3.8), ahead of pool sizing (§3.9) and clarification selection (§3.11) — both of those assume a non-empty pool.
- The dropped filter is not permanently discarded from `state["filled_slots"]` for future turns — only the retrieval call for this turn is re-run against the relaxed constraint set. If the same filter causes a zero pool again next turn, the safety net fires again independently.
- Log every trigger of this path via §10.6's debug log — a filter that repeatedly causes zero-pool events is a signal that slot extraction (§3.4/§3.5) is likely mis-parsing that field.

---

### 10.3 Pool-Shrink Recovery Signal (Bad-Turn Recovery)

**Decision (L):** Track candidate pool size before vs. after applying each turn's new customer answer. If the pool did not shrink meaningfully, stop asking about that attribute category again this session — even if entropy scoring (§3.11 Rule C) would otherwise re-select it. This replaces a full self-critique/self-correction loop, which was explicitly rejected.

**Rationale:** A general self-critique loop, where the agent re-evaluates and potentially reverses its own prior turns, is hard to calibrate correctly, and a miscalibrated version can make the agent actively worse than doing nothing — it adds a second, harder-to-audit source of behavior change on top of the deterministic pipeline the rest of this PRD is built around (§3.11's rationale). Pool-shrink tracking gets most of the same practical benefit — noticing that a line of questioning isn't working — as a simple, cheap, fully auditable signal instead.

**Spec:**
```python
def update_pool_shrink_tracker(state, category, pool_size_before, pool_size_after):
    if pool_size_after >= pool_size_before:  # exact "meaningful" threshold tuned per §9 item 7
        state.setdefault("unproductive_categories", set()).add(category)
```
- `unproductive_categories` is checked by §3.11's `select_ask_attribute` before Rule C entropy scoring runs (see the updated §3.11 spec) — any category in this set is excluded from `unfilled` for the rest of the session.
- This is a permanent, one-way exclusion per session (no re-enabling), consistent with §10.5's rule that state changes only on an auditable trigger — the trigger here is "answering this category did not narrow the pool," which is itself auditable and logged.
- Interacts with §3.6 (override-invalidation graph): if a category is cleared back to unfilled by an override, it is also removed from `unproductive_categories`, since the invalidation represents new information that makes the earlier non-shrink observation stale.

---

### 10.4 Turn-Budget-Driven Clarification Threshold

**Decision (M):** `CONFIDENCE_POOL_THRESHOLD` (§3.11) is not a single fixed constant — it loosens as the session's turn budget runs out, and clarification is force-disabled outright once turns run low.

**Rationale:** A missed hit costs the objective function the same whether it happens on turn 8 or turn 10, but every additional clarification question directly taxes `Efficiency` (20% of `TechnicalScore`, via `MTTC`, §1). Early turns can afford to spend a question narrowing the pool; late turns cannot — the expected value of one more question turns negative well before the 10-turn cap.

**Spec:**
```python
def confidence_pool_threshold(turn):
    if turn <= 4:
        return BASE_THRESHOLD                    # ask freely — tuned per §9 item 2
    elif turn <= 7:
        return BASE_THRESHOLD * RAISED_MULTIPLIER  # lean toward recommending — tuned per §9 item 2
    else:
        return float("inf")                       # unreachable in practice, see below

def select_ask_attribute(state, candidate_pool):  # supersedes the Rule B/D check in §3.11
    if state["turn"] >= 8:
        return None   # force recommend-only, no exceptions
    ...
    if len(candidate_pool) <= confidence_pool_threshold(state["turn"]):
        return None
    ...
```
- Turn bands (1–4 free, 5–7 raised, 8+ forced-off) and the exact `BASE_THRESHOLD`/`RAISED_MULTIPLIER` values are starting guesses, tunable against the 200 public sessions per §4.5 and tracked as open items (§9, items 2 and 8).
- This directly modifies the §3.11 spec's Rule B and Rule D — Rule D becomes unconditional from turn 8 onward regardless of `unfilled` or pool size.

---

### 10.5 State-Change Discipline (Consistency vs. Adaptivity)

**Decision (N):** Formalize, as an explicit rule, behavior already implied by §3's design: session state changes only on an auditable trigger — a new fact learned (§3.4 slot merge), a turn-count threshold crossed (§10.4), or a zero-pool event (§10.2). Never on a soft or subjective signal (e.g., an LLM's own confidence score, sentiment, or a heuristic "this seems off" judgment).

**Rationale:** Every adaptive mechanism in this PRD — mode re-derivation (§3.7), the invalidation graph (§3.6), pool-shrink tracking (§10.3), and the turn-budget curve (§10.4) — is deliberately built on deterministic, loggable triggers. Adding a second, independent axis of adaptivity driven by a soft signal (e.g., a self-assessed confidence score gating extra behavior changes) is exactly the kind of change that causes erratic flip-flopping between turns, undermining both reproducibility and the debuggability the rest of the architecture optimizes for.

**Spec:** No new code — this is a design constraint on future changes to this PRD. Any new adaptive behavior proposed after this point must name its trigger and show that trigger is one of: a slot value change, a turn-count boundary, a zero-pool event, or a pool-shrink observation. A proposal whose trigger is a model-reported confidence/certainty score should be rejected or redesigned around one of the above instead.

---

### 10.6 Explainability: Local Debug Log

**Decision (O, telemetry extension of §3.14):** Emit a per-turn local debug log, separate from the `usage` field returned to the evaluator, recording: current mode, filled slots, which attribute won entropy scoring and why (including anything excluded by §10.3's `unproductive_categories`), which retrieval channel was used, candidate pool size before and after filtering (including any §10.2 safety-net triggers), and whether any fallback (§3.13) fired.

**Rationale:** This costs nothing at runtime — it's a local log write, not a network call or scored field — and pays for itself the first time a session's behavior needs to be traced back to a specific decision point during tuning (§4.5) or debugging a bad local-eval run. Building it in week one, before the rest of the pipeline is fully tuned, means every subsequent debugging session benefits from it rather than only the ones built after some debugging pain motivates it.

**Spec:** See the updated §3.14 spec for the log fields; implementation is a plain structured log write (e.g., one JSON line per turn to a local file), gated off entirely in the submitted evaluator-facing output.

---

### 10.7 Overall Strategy Synthesis

**Decision (P):** The agent's overall adaptivity strategy is layered, not monolithic:
- **Backbone:** a named-policy approach, scoped only to the four official scenarios (§10.1) — not a general-purpose scenario library, since the simulator will not produce out-of-scope scenarios.
- **Layered on top:** turn-budget-driven aggressiveness (§10.4), shifting the clarification/recommend balance as the turn cap approaches.
- **Two narrow tripwires**, borrowed from confidence-gating thinking but deliberately not generalized into a full confidence score: the zero-pool safety net (§10.2) and the pool-shrink recovery signal (§10.3).
- **Explicitly excluded:** a full self-critique/self-correction loop. The complexity and erratic-behavior risk (§10.3's rationale, §10.5) isn't justified against a scripted, non-adaptive simulated customer — there is no adversarial or evolving counterpart for a self-critique loop to be correcting against.

**Rationale:** This combination targets the specific failure modes the objective function (§1) actually penalizes — permanent pool exhaustion, wasted questions late in a session, and repeatedly asking about an attribute that isn't narrowing the search — without introducing an adaptive mechanism whose behavior is harder to predict or tune than the failure modes it would guard against.
