# AGENTS.md — Rules for any AI/agent working on this repo

This file is for any LLM/coding agent (Claude, Copilot, etc.) helping build the
shopping agent for the TechJam Conversational E-Commerce Search Challenge.
Read this before touching any file in this repo.

## 🚫 NEVER MODIFY THESE FILES (very important)

These are official, frozen, organizer-provided files. Editing any of them
invalidates local scoring and is an explicit submission-rules violation
(`docs/submission_rules.md`: "Do not include... code that modifies evaluator
files"). Official final scoring re-runs the same evaluator logic against a
private session set, so any local tampering just produces a fake number that
won't hold up — never do it to "make the score look better."

- `evaluator/local_evaluator.py` — the scoring engine. Do not edit the hit
  condition, MRR/MTTC formulas, `normalize_recommendations`, or anything else
  in this file.
- `docs/agent_api_contract.json` — the required `Agent` interface schema.
- `docs/evaluation_config.json` — scoring configuration.
- `docs/competition_specification.md`, `docs/submission_rules.md`,
  `docs/baseline_results.json` — official rules/reference docs.
- `data/public_set.jsonl` — the 200 labeled dev sessions (ground truth
  labels). Read-only. Never edit, filter, or "helpfully" clean this data.
- `data/catalog.jsonl` (60k+ products, gitignored, not committed) — the
  frozen product catalog. Read-only, no structural mutations, no injecting
  mock/fake ASINs, per competition scope.
- `tests/test_evaluator.py` — do not weaken/rewrite these tests to make a
  broken agent pass.

**The only files meant to be edited are under `starter/`**: `agent.py`
(orchestration only — Person D), `state.py` + `router.py` (slots/decay/
routing — Person B), `retrieval.py` (hybrid search — Person A), `clarify.py`
+ `rank.py` (clarification + final ranking — Person C). If a task seems to
require changing one of the frozen files above, stop and flag it instead of
editing it.

**Known open gap (found by direct testing, not yet fixed):** `retrieve()`
in `retrieval.py` currently only searches using the raw current-turn
message — it does not yet combine `state.slots`/`state.summary()` into the
query. Until Person A and Person B agree on how retrieval should consume
accumulated state, the pipeline is still functionally single-turn search
even though slot tracking works correctly. This is why an early end-to-end
run scored identically to the old BM25-only baseline.

## Team roles

Four people, four files, working in parallel against frozen interfaces —
nobody is blocked waiting on anyone else's implementation to exist.

- **Person A — `starter/retrieval.py`.** Hybrid search: real hard-constraint
  filtering in `filter_candidates`, and the embedding similarity search in
  `semantic_candidates` (currently a stub returning nothing). This is the
  highest-leverage file — it's the fix for Browsing's ~2.5% hit rate.
- **Person B — `starter/state.py` + `starter/router.py`.** Slot extraction,
  decay, override handling, Buying/Browsing classification. Co-owns, with
  Person A, deciding how accumulated state actually feeds into retrieval's
  query (see "known open gap" below — not settled yet).
- **Person C — `starter/clarify.py` + `starter/rank.py`.** Over-generality
  detection, info-gain-style attribute selection, turn-budget pressure, and
  final candidate ranking.
- **Person D — `starter/agent.py` (orchestration only) + everything
  non-code.** Continuous integration (re-run the evaluator after every
  change, watch the scenario breakdown for regressions), defensive
  robustness, README/reproduction steps, latency/token/cost disclosure,
  Devpost write-up, demo video. Not blocked/sequential — this role runs
  from hour one in parallel with A/B/C, not after them.

## Where things live

- `starter/agent.py` — orchestrates the pipeline (Person D). Along with
  `state.py`, `router.py`, `retrieval.py`, `clarify.py`, `rank.py`, these
  five files together are the required deliverable (per
  `docs/submission_rules.md`: "one Python agent entry file... any required
  local helper modules").
- `evaluator/local_evaluator.py` — run via `python -m evaluator.local_evaluator`.
  Writes `results.json` (gitignored, safe to regenerate/overwrite).
- `data/catalog.jsonl` — must exist at this exact path for the evaluator to
  run (`--catalog` defaults to `data/catalog.jsonl`). It is NOT committed to
  git (60MB+, gitignored) — if missing, copy/extract the downloaded catalog
  file into place before running the evaluator.
- `data/public_set.jsonl` — 200 dev sessions used by the local evaluator.
- `docs/agent_api_contract.json` — source of truth for the exact
  request/response shape `Agent.respond()` must return.

## Required `Agent` interface

```python
class Agent:
    def reset(self, session_id: str, user_profile: dict) -> None: ...
    def respond(self, session_id: str, user_message: str, turn: int, top_k: int) -> dict:
        return {
            "message": "...",                       # required, str
            "ask_attribute": "material" | None,      # required, one of the enum below or null
            "recommendations": [{"parent_asin": "B000..."}],  # required, ordered best->worst
            "usage": {"prompt_tokens": 0, "completion_tokens": 0},  # optional
        }
```

`ask_attribute` allowed values: `category, material, color, size, style,
brand, budget, feature, use_case, other`, or `null`.

## Scoring mechanics an agent MUST understand before writing ranking logic

These are load-bearing facts about how `local_evaluator.py` actually scores
sessions — get these wrong and the agent's strategy will be misaligned with
the metrics, even if the code runs fine.

- **Single ground-truth target per session.** Exactly one `parent_asin` is
  correct. Only exact string equality counts — no partial/fuzzy credit.
- **Hit = target appears anywhere in your (deduped, catalog-valid) top 10
  recommendations for that turn.** Position does not matter for whether it's
  a hit — `target in ranked`, not `ranked[0] == target`.
- **The instant a hit occurs, the session ends immediately** (`break` in the
  eval loop). There is no way to include the correct item in your output
  "without" triggering the hit, and no way to revise its rank in a later
  turn once it's already appeared. Whatever rank it has the first time it
  shows up is locked in forever for that session.
- **Only the first 10 valid, unique, catalog-real `parent_asin` values in
  your `recommendations` list are ever looked at** — the schema allows up to
  100 items, but sending more than 10 clean IDs has zero scoring benefit.
  Dedup/validate your own output; don't rely on padding.
- **MRR rewards rank, hit rate doesn't.** `reciprocal_rank = 1/rank` if hit,
  `0` if miss. So getting the target into position 10 scores a hit but only
  0.1 MRR, vs. 1.0 MRR at position 1. There's a real tradeoff between
  "include it now at a mediocre rank" (locks in a fast, guaranteed hit) vs.
  "wait one more turn to be able to rank it #1" (better MRR, but risks
  losing the hit entirely if it drops out of contention). Weights:
  `TechnicalScore = 0.50*HitRate@10 + 0.30*MRR + 0.20*Efficiency`.
- **MTTC/Efficiency penalize turns.** `Efficiency = clip((11-MTTC)/10, 0, 1)`,
  miss = turn 11 assigned. Max 10 turns per session (hard cutoff, zero score
  if exceeded — actually capped/ended by the evaluator's `MAX_TURNS`).
- **Intent Override sessions**: a hit before the customer's override message
  arrives does NOT count (`override_applied` gates the hit check). Don't be
  confused if an early correct-looking guess doesn't register in these
  sessions — that's expected, not a bug.
- **`ask_attribute: "other"` is the broadest information-reveal query** — it
  matches any undisclosed hidden fact regardless of category, unlike a
  specific attribute name which only matches facts of that type. Each ask
  reveals up to 2 previously-undisclosed facts; there are at most 4 hidden
  facts total (2 `hard_constraints` + 2 `soft_preferences`) per product, so
  in general ~2 "other" asks can fully drain the hidden fact pool (fewer for
  Buying, which pre-discloses one constraint in turn 1; one extra ask needed
  for Boundary, which burns the first ask on a non-answer).
- **Response validation is lenient at runtime** — the evaluator only hard
  fails on `message` not being a string (treats the whole response as a miss
  for that turn). Missing/malformed `ask_attribute`/`recommendations` won't
  crash, they just default to no-question/no-candidates. Still, always
  return the full contract shape — don't rely on this leniency.
- **Exceptions/timeouts count as a miss for that session.** Wrap retrieval/
  ranking logic defensively; a crash costs the same as a wrong answer.

## Architecture plan (what's actually built, and what was tried and rejected)

Four pieces, per turn, in this order:
1. **Slot extraction + intent routing** (`router.py`) — parse the customer's
   message into structured slots (category/material/color/size/style/brand/
   budget/feature/use_case), classify Buying vs Browsing, detect
   intent-override (slot erasure/rewrite) and boundary/no-preference
   signals. Genuine free-text understanding now runs here via Groq
   (`_classify_unprompted_llm`), LLM-first with the original regex/keyword
   matching (`_classify_unprompted`) as the fallback on any failure (no
   key, network error, timeout, malformed response) — this is deliberately
   the ONLY place an LLM is used for understanding, not for every turn:
   it only fires on genuinely freeform text (turn-1 disclosures, etc.),
   never on direct answers to our own `ask_attribute` question, since
   those already match the evaluator's fixed reply templates with 100%
   certainty for free (see the scoring-mechanics section above).
2. **Retrieval** (`retrieval.py`) — Buying: hard-constraint filter +
   keyword/vector search. Browsing: dense embedding similarity (local
   `sentence-transformers`, always-on; optional OpenAI upgrade with
   automatic fallback) — this was the biggest gap vs. the BM25-only
   starter, which scored ~2.5% hit rate on Browsing vs ~24% on Buying (see
   `docs/baseline_results.json`).
3. **Over-generality check** (`clarify.py`) — entropy/coverage-based
   selection of which open attribute would most split the current
   candidate pool, with turn-budget pressure (stop asking, just recommend,
   as turns run low).
4. **Ranking** (`rank.py`) — a weighted formula: retrieval score + product
   rating + review-volume popularity + a decayed-slot text-match bonus.
   **LLM reranking of this formula's own output was tried and rejected**:
   an A/B run (Groq, `openai/gpt-oss-20b`, reranking the top 20) moved
   TechnicalScore by +0.003 — noise-level — while adding real latency and
   ~207K tokens of cost across a 200-session run. The formula alone already
   captures most of the useful signal; don't re-add LLM reranking here
   without a real reason to expect it'll help this time.

Session state (`state.py`, `SessionState`) persists across turns within a
session: `filled_slots` (accumulation + override/erase-and-rewrite),
`filled_null` (boundary), decay (older unconfirmed slots count for less
than recently confirmed ones via `decayed_slots()`), and `turn_usage`
(reset every turn, real LLM token counts get written into it in place —
this is what makes the contract's `usage` field non-zero when Groq
extraction actually runs). Sessions are isolated single-user interactions —
no cross-session persistence needed or expected.

## Model/API keys (optional, both have automatic fallbacks)

- `OPENAI_API_KEY` — upgrades `retrieval.py`'s embedding search from the
  local model to `text-embedding-3-small`. Falls back to local on any
  failure, per-call (not just at startup).
- `GROQ_API_KEY` — enables real LLM understanding of freeform customer
  messages in `router.py` (NOT used in `rank.py` — see above). Falls back
  to regex/keyword extraction on any failure.
- Both load from a local `.env` file (via `python-dotenv`, see
  `.env.example`) — `.env` is gitignored, never commit real keys. The
  agent runs fully offline with neither set.

## Local dev commands

```bash
python -m evaluator.local_evaluator      # runs starter/Agent against data/public_set.jsonl
```

Baseline (weak BM25 starter) reference score: Hit Rate@10 = 0.125,
MRR = 0.068, MTTC = 9.81, TechnicalScore = 0.107 (see
`docs/baseline_results.json`). Any real change should be compared against
this, broken down by scenario (`buying`/`browsing`/`intent_override`/
`boundary`) since they behave very differently.