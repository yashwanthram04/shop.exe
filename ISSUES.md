# Known Issues — measured, ranked by impact

All figures below come from instrumented runs against `data/public_set.jsonl`,
not estimates. Where a run used a subset, the sample size is stated.

**Running score, full 200-session runs, each isolating one fix:**

| stage | TechnicalScore |
|---|---|
| Original baseline | 0.1070 |
| + hybrid retrieval/filters/embeddings (pre-diagnosis) | 0.4342 |
| + Issue 1 (RRF fusion) | 0.6084 |
| + Issue 2 ("other" probe) | 0.7891 |
| + Issue 5 (confirmed neutral, LLM extraction kept on) | 0.7903 |
| + Issue 7 (override merge fix) | **0.7964** |
| + chaithra/retrieval-fixes branch (query pollution, override guard, RRF weights) | 0.8041 |
| + Issue 8 (never return `None`) | 0.8193 |
| + Issue 10 (stop re-recommending disproven products) | **0.8400** |

All scores above are measured with LLM extraction OFF, which is now the
default (see Issue 9). Earlier numbers in this file that were taken with
Groq enabled are not directly comparable — Groq was silently failing on a
variable fraction of calls, making those runs non-reproducible.

Remaining open: Issue 3 (retrieval ceiling) — now the dominant limit, see
Issue 10's measurement. Issue 6 (attrs coverage) is deliberately NOT being
fixed; see Issue 8.

Reference numbers from a 40-session instrumented replay (Groq disabled, so
these are independent of any LLM behaviour):

| stage | sessions |
|---|---|
| target reaches candidate pool | 29/40 (72.5%) — **retrieval ceiling** |
| target reaches final top-10 | 18/40 (45.0%) — actual hit rate |
| **lost between pool and top-10** | **11/40 — ranking loss** |

---

## Issue 1 — Score-scale mismatch in retrieval fusion 🔴 CRITICAL

**Status:** ✅ fixed — confirmed on full 200-session run, Groq isolated off:

| | before | after |
|---|---|---|
| TechnicalScore | 0.4342 | **0.6084** |
| Hit Rate — Boundary/Browsing/Buying/Override | 50/58.75/50/46.67% | **80/70/72.5/60%** |
| MTTC | 6.79 | **5.13** |

Fixed by switching `retrieve()`'s fusion from a raw weighted sum to
Reciprocal Rank Fusion (`score = weight / (RRF_K + rank)`, `RRF_K = 60`),
which is scale-free by construction. Every scenario improved; this was
the dominant bug in the whole pipeline.
**File:** `starter/retrieval.py`, `retrieve()`
**Owner:** Person A

`retrieve()` blends two scores on incompatible scales:

```
BM25 (keyword) : [28.23, 27.78, 27.45, ...]
cosine (semantic): [0.628, 0.599, 0.597, ...]
```

The weighted sum `keyword_weight * bm25 + semantic_weight * cosine` is
therefore meaningless. Even in the semantic-favouring "browsing" branch
(`0.3 * 28 = 8.4` vs `0.7 * 0.6 = 0.42`), keyword outweighs semantic ~20x.

Consequences:
- the per-track weighting (0.7/0.3 vs 0.3/0.7) has essentially no effect
- semantic-only candidates enter the pool but sort *below every keyword
  candidate*, so they almost never survive into the scored top-10
- this masked two earlier experiments: OpenAI vs local embeddings scored
  **identically** (0.434242 both), and LLM reranking moved the score only
  +0.003 — both because embeddings were barely influencing final order

**Measured fix** (Reciprocal Rank Fusion, scale-free, 40 sessions):

| | current (raw scores) | rank fusion |
|---|---|---|
| pool ceiling | 72.5% | 72.5% |
| **top-10 hits** | **45.0%** | **72.5%** |
| ranking loss | 11 sessions | **0 sessions** |

RRF converts every successfully-retrieved target into a hit. Expected to be
worth more than every other item in this file combined.

---

## Issue 2 — Clarification wastes turns; never uses the strongest probe 🟠 HIGH

**Status:** ✅ fixed — confirmed on full 200-session run, Groq isolated off:

| | after Issue 1 | after Issue 2 |
|---|---|---|
| TechnicalScore | 0.6084 | **0.7891** |
| Hit Rate — Boundary/Browsing/Buying/Override | 80/70/72.5/60% | **90/95/91.25/70%** |
| MTTC | 5.13 | **3.10** |

Fixed in three places:
- `clarify.py`: added `"other"` as an early (turn <= 2, up to twice) bootstrap
  probe, and as the fallback when nothing clears the entropy bar — replacing
  the previous `return None` (which the evaluator answers with a fully
  information-free "ask me about one specific attribute" reply).
- `router.py`: `"other"` isn't a real attribute, so its answer can mix
  facts of different types in one reply ("cotton; under $40") — added
  per-value classification (`classify_single`) instead of blindly
  attributing the whole reply to a fake `filled_slots["other"]` key, which
  would have silently discarded the extracted information.
- `router.py`: facts extracted from an `"other"` reply are now tagged
  `source="asked"` (high decay-confidence), not `"freeform"` — they came
  from a direct answer to our own question, just not attributable to one
  named slot until classified.
- `state.py`: added `other_asked_count` (a set can't distinguish "asked
  once" from "asked twice", and `"other"` is worth asking up to twice per
  AGENTS.md's ~4-facts-total math).

Browsing went from the weakest scenario in the project (58.75%) to the
strongest (95%). Intent Override is now the clear relative laggard (70% vs
90%+ elsewhere) — worth investigating next, likely the override-clearing
mechanics rather than clarification.
**Files:** `starter/clarify.py` (`ALL_NINE_CATEGORIES`, `pick_attribute_to_ask`)
**Owner:** Person C

Over 40 replayed sessions:
- **81 asks** returned *"I don't have an additional preference for X"* — the
  attribute had no undisclosed fact left, so the ask yielded nothing
- **`ask_attribute` was `None` on 131 turns**. The evaluator answers `None`
  with *"Those options are not quite right yet. Ask me about one specific
  attribute."* — a fully information-free turn. Returning `None` is never
  better than asking *something*, because a response may carry
  `ask_attribute` **and** `recommendations` together (see `AGENTS.md`).

Additionally, `ALL_NINE_CATEGORIES` **omits `"other"`**. Per
`evaluator/local_evaluator.py:180`, the match condition is
`attribute == "other" or classify_constraint(value) == attribute` — so
`"other"` matches **any** undisclosed fact regardless of its type, while a
named attribute only matches facts of that one type. `"other"` is strictly
the highest-yield probe available and the agent never uses it.

Directly inflates MTTC (currently ~6.8–6.9), which is 20% of TechnicalScore
via `Efficiency = clip((11 - MTTC) / 10, 0, 1)`.

---

## Issue 3 — Hard retrieval ceiling of ~72.5% 🟡 MEDIUM

**Status:** open
**File:** `starter/retrieval.py`
**Owner:** Person A

In 27.5% of sessions the target never enters the candidate pool on *any*
turn. Even flawless ranking caps the score there. Leads worth testing:

- `state.durable_notes` prefixes the literal filler `"no preferences stated
  yet"` into turn-1 queries (`state.py: summary()`), polluting the embedding
- the pool is capped at `top_n=50` per route; both routes contribute
  uniquely (9 keyword-only, 5 semantic-only finds across 40 sessions), so
  widening the cap is cheap and non-destructive
- `filter_candidates` may be excluding the target before ranking ever sees
  it — the `return kept if kept else candidate_ids` fallback only triggers
  when the pool is emptied *entirely*, not when the target alone is dropped

---

## Issue 4 — Category hard-filter was case-sensitive 🟢 FIXED

**Status:** ✅ fixed
**File:** `starter/retrieval.py`, `filter_candidates()`
**Owner:** Person A

`haystack` was lowercased but the search words were not, so a Title Case
value matched nothing:

```
'necklaces'         -> matched   8/400
'Necklaces'         -> matched   0/400   <-- silently zeroed
```

Regex extraction happened to emit lowercase; LLM extraction emits Title Case
(`"Jewelry Necklaces"`, `"Watches"`), so enabling Groq silently disabled
category filtering altogether. The `kept if kept else candidate_ids`
fallback disguised this as a harmless no-op rather than surfacing an error.

Fixed by lowercasing the comparison words. Verified: `'Necklaces'` now
matches the same 8/400 as `'necklaces'`.

---

## Issue 5 — LLM slot extraction is a net regression 🟡 MEDIUM

**Status:** ✅ resolved — hypothesis confirmed by re-measuring after Issue 1:

| | Groq off | Groq on |
|---|---|---|
| TechnicalScore (fixed baseline) | 0.78912 | **0.790345** |
| Per-scenario hit rates | — | identical to Groq-off |
| Token cost, 200 sessions | 0 | 2,282 tokens |

The -0.007 regression is gone — now neutral-to-slightly-positive (+0.001,
within noise), confirming it was Issue 1 (fusion weights doing nothing)
masking/interacting with the richer extraction, not a flaw in the LLM
approach itself. Left enabled: genuine understanding capability
(recovers `material`/`feature`/`use_case`/`style` from text regex misses
entirely — see the "lightweight dangle design" example above) at
negligible cost once the downstream bug it was interacting with is fixed.
**Files:** `starter/router.py`, `starter/rank.py`
**Owner:** Person B / Person C

Enabling Groq extraction moved TechnicalScore 0.4342 → 0.4274 (-0.007),
concentrated in Browsing (58.75% → 57.5%) and Intent Override (46.67% →
43.3%). Token cost was modest (3,437 prompt / 487 completion for the whole
200-session run) because the LLM only fires on freeform text — measured at
**25 of 125 turns (20%)**; the other 80% take the verbatim answer-template
path and never call it.

Extraction quality is genuinely *better* (it recovers `material`,
`feature`, `use_case` from text where regex returns nothing). Suspected
causes of the regression, both unverified:

1. Richer slots trip `classify_track()`'s trigger list sooner, flipping
   sessions to `"buying"` and changing fusion weights — **but the weights
   currently do nothing (Issue 1)**, so this diagnosis is untestable until
   Issue 1 lands. Re-measure then.
2. LLM values are paraphrases, not verbatim copies of product text.
   `evaluator/local_evaluator.py`'s `intent_card()` builds constraints
   directly from the target product's `features`/`details`, so verbatim
   regex values match product text exactly by construction; a paraphrase
   (`"Stainless Steel"` vs the product's own phrasing) does not. Issue 4 was
   one instance of this class. `rank.py`'s `_slot_fit_bonus()` uses
   contiguous-substring matching, so multi-word LLM values like
   `"Long torso camisole"` likely never fire the bonus — token-overlap
   matching would be more robust.

---

## Issue 6 — `_extract_attrs` covers only 5 of 9 attributes 🔵 LOW

**Status:** open
**File:** `starter/retrieval.py`, `_extract_attrs()`
**Owner:** Person A

Populates `material`/`color`/`style`/`brand`/`category` only. `clarify.py`'s
entropy selection can therefore never pick `size`/`budget`/`feature`/
`use_case` — they never clear `MIN_COVERAGE_TO_ASK`. Not a correctness bug
(the coverage gate degrades safely), but it silently narrows the question
space to 5 of 9 possible attributes.

---

## Issue 7 — Override destructively overwrites compound slot values 🟠 HIGH

**Status:** ✅ fixed — found while investigating Intent Override lagging all
other scenarios (70% vs 90%+ after Issues 1/2). Confirmed via a 20-session
Intent Override trace, then full 200-session run:

| | before | after |
|---|---|---|
| TechnicalScore | 0.790345 | **0.796407** |
| Hit Rate — Intent Override | 70% | **76.67%** |
| MRR — Intent Override | 0.540 | **0.546** |
| MTTC — Intent Override | 6.2 | **5.7** |

**Root cause:** `classify_single()` (used for override text) falls back to
a generic `"feature"` bucket for anything that doesn't match a specific
material/color/size/style/use_case word list. Two genuinely different,
both-still-valid facts disclosed in different turns can both land under
that same generic key (e.g. `"Water Resistant"` and `"3 Year Battery"`,
joined as `"Water Resistant; 3 Year Battery"`). `apply_override()` then
unconditionally overwrote `filled_slots[attribute]` with just the
override's new value — a coincidental key collision (not an actual
contradiction of that specific fact) silently destroyed the sibling fact,
permanently narrowing/wrongly-shaping retrieval for the rest of the
session.

**Fix:** `apply_override()` now merges into an existing compound value
instead of overwriting it — and merging correctly handles the value
already being present as a substring of the compound (an earlier attempt
at this fix still downgraded `"X; Y"` to just `"X"` when the override's
value was `"X"`; fixed to keep the fuller existing string in that case).

**Not fully closed:** one traced miss (`public_0002`) never entered the
retrieval pool at all regardless of this fix — likely Issue 3 (retrieval
ceiling), not an override problem. Worth another pass once Issue 3 is
addressed.

---

## Issue 8 — `ask_attribute: None` dead-ends every missed session 🔴 CRITICAL

**Status:** ✅ fixed. 0.8041 → **0.8193** (Boundary 90→100%, Override 80→90%).

Found by reading `transcripts.log`. **All 35 missed sessions ended in a
trailing run of `None`**, mean 4.69 dead turns each, 164 turns total.
`None` appeared 171× in misses vs 24× in hits. The evaluator answers
`None` with *"Those options are not quite right yet..."* — zero
information — so those sessions were over by ~turn 6 but burned to 10.

**Root cause chain:** `_extract_attrs()` populates only 5 of 9 attributes,
so `size`/`budget`/`feature`/`use_case` always score 0.0 coverage and can
never clear `MIN_COVERAGE_TO_ASK`. Once the 5 covered ones are used and
the `other_asked_count` cap of 2 is spent, Rule D's `scored` list is empty
and the function returned `None` permanently.

**Fix:** `None` is now reserved for Rule A (boundary just fired) alone.
Rules B/C/D all fall back to `"other"`, which per
`evaluator/local_evaluator.py:178-181` matches ANY undisclosed fact
(the `attribute == "other"` short-circuit) and so is never worse than a
named ask. Also dropped `HARD_STOP_ASK_TURN`, which forced `None` on turns
8-10. Asking is free — `ask_attribute` and `recommendations` ship in the
same response and MTTC only counts the hit turn.

**Note on Issue 6:** extending `_extract_attrs` coverage is deliberately
NOT the fix here, and would likely be counterproductive — it would let
Rule D select a *named* attribute where it now falls back to `"other"`,
and named attributes are strictly dominated. Left open but downgraded.

Verified directly: trailing-`None` runs went from 35/35 missed sessions to
**0/12**; remaining `None`s are all Rule A, which is correct.

---

## Issue 9 — LLM slot extraction is not worth its cost 🟡 MEDIUM

**Status:** ✅ resolved — now opt-in via `USE_LLM_EXTRACTION`, off by default.

Measured on identical code, only Groq toggled:

| | LLM off | LLM on |
|---|---|---|
| TechnicalScore | **0.8400** | 0.8370 |
| Browsing hit rate | **95%** | 93.75% |
| Tokens (200 sessions) | 0 | 2,172 |

**Two structural reasons it loses here** (verified by diffing extractor
outputs on real turn-1 messages, not inferred):

1. It reads *"but I'm still exploring"* as expressing no preference and
   returns `{}` — correct semantically, but it discards the **category**,
   the one signal every turn-1 message reliably carries. Every Browsing
   session uses that phrasing, which is why Browsing regressed most.
2. It normalizes/truncates: `"watches wrist watches"` → `"Watches"`,
   losing the retrieval specificity the verbatim regex value keeps.

Neither is a capacity problem — a paid tier fixes neither. This evaluator's
messages are template-generated and highly regular, so regex handles them
near-perfectly while the LLM's instinct to normalize destroys the verbatim
signal the pipeline depends on.

**Also fixed a reproducibility trap:** an earlier measurement put this at
"+0.001, noise". That run was invalid — Groq was silently failing on most
calls (5,421 tokens where ~46,000 were expected), so it was mostly
measuring the regex path. Any `except: return None` fallback around a
network call makes runs non-deterministic; compare only against explicit
LLM-off runs.

---

## Issue 10 — Agent re-recommends products already proven wrong 🟠 HIGH

**Status:** ✅ fixed. 0.8193 → **0.8400** (MRR 0.6397 → **0.6867**).

Spotted by reading transcripts: the agent showed the same top-10 turn after
turn. If a product was in a scored top-10 and the session did NOT end, it
is *provably* not the target — the evaluator ends the session the instant
the target appears. Re-showing it spends a slot that can never pay off. Over
10 turns this capped total coverage at ~10 products instead of up to 100.

**Fix:** `SessionState.record_shown()` tracks shown products; `rank()`
demotes them to the back of the list (demote, not drop — the list stays a
full 10 even once the pool is exhausted). Verified: consecutive turns now
share **0** recommendations instead of 10.

**Critical caveat — the override gate.** The "it didn't end, so it's wrong"
deduction is FALSE before an intent override fires, because the evaluator
refuses to score any hit until then (`override_applied`). Two missed
sessions had the target at pool positions **1 and 2** — ranked correctly,
just shown too early to count. Blind exclusion would have discarded the
right answer permanently. `state.clear_shown()` therefore wipes the history
whenever an override is detected.

**Measurement that reframes the remaining work:** of 28 misses probed, only
**6** had the target in the candidate pool ranked too low; **22 never
entered the 50-candidate pool at all**. Ranking is now largely solved —
Issue 3 (retrieval recall) is the binding constraint for further gains.

---

## Fix order

1. ✅ **Issue 1** — fusion. 0.4342 → 0.6084.
2. ✅ **Issue 2** — `"other"` probe + stop returning `None` for free. 0.6084 → 0.7891.
3. ✅ **Issue 5** — re-measured; confirmed neutral now, kept on. 0.7891 → 0.7903.
4. ✅ **Issue 7** — override merge fix (found while investigating Issue 3/
   Intent Override lagging). 0.7903 → 0.7964.
5. **Issue 3** (next) — widen pool, clean the turn-1 query, audit filter
   exclusions. One traced Intent Override miss (`public_0002`) never
   entered the pool at all even after Issue 7 — good concrete case to
   debug against.
6. **Issue 6** — extend attribute coverage.

Re-run `python -m evaluator.local_evaluator` after each item, one at a time,
and record the scenario breakdown — several earlier conclusions were wrong
because two things changed between runs.
