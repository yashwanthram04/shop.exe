# Shop.exe — Conversational Shopping Copilot

An AI shopping agent that asks useful follow-up questions and finds a customer's hidden target product within at most 10 turns, built against the TechJam conversational-search challenge harness.

## Solution Overview

The agent is a multi-turn dialogue pipeline, not a single-shot recommender. Each turn runs through a fixed sequence orchestrated by `Agent._respond()` in [starter/agent.py](starter/agent.py):

**Understand → Route → Retrieve → Clarify → Rank → Respond**

| Module | Role |
|---|---|
| [starter/agent.py](starter/agent.py) | Orchestrator. Implements the evaluator's required `reset()` / `respond()` contract and fails safe to an empty response on any internal crash, rather than raising. |
| [starter/state.py](starter/state.py) | `SessionState` — per-session memory: filled slots across a 9-attribute vocabulary, slot provenance/decay, boundary/override tracking, and shown-product history. |
| [starter/router.py](starter/router.py) | Extracts slots from free text, classifies the session as "buying" or "browsing", and detects boundary/override signals (e.g. the customer changing their mind mid-conversation). |
| [starter/retrieval.py](starter/retrieval.py) | Hybrid retrieval: BM25 (SQLite FTS5) + dense embeddings (local `sentence-transformers` by default, optional OpenAI embeddings), fused with Reciprocal Rank Fusion and then hard-constraint filtered. |
| [starter/clarify.py](starter/clarify.py) | Deterministic, entropy/coverage-based choice of which attribute to ask about next — or to stop asking and answer. |
| [starter/rank.py](starter/rank.py) | Final scoring: blends normalized retrieval score, slot-fit, rating, and popularity into a top-10 ranked list of `parent_asin`s. |

The default pipeline is **fully deterministic and runs offline** — local embeddings, regex-based slot extraction, no API calls. Three additional LLM-augmented roles (slot extraction, search-query synthesis, and result reranking, all via Groq) exist as opt-in features, but were measured to be net-neutral-to-negative on the eval set during development, so they ship **off by default**.

The four pipeline areas (routing/state, retrieval, clarification/ranking, and orchestration) were each owned by a different team member, as documented in `agent.py`'s module docstring.

## Setup and Installation

**Prerequisites:** Python 3.10+

1. Install dependencies:
   ```
   pip install -r requirements.txt
   ```
2. Download the product catalog (gitignored, required at `data/catalog.jsonl`):
   ```
   gzip -dk catalog.jsonl.gz
   mv catalog.jsonl data/catalog.jsonl
   ```
   Verify the download against the checksums in `SHA256SUMS`.
3. (Optional) Copy the environment template if you want to try the opt-in LLM features:
   ```
   cp .env.example .env
   ```
   Every flag in `.env` (`OPENAI_API_KEY`, `GROQ_API_KEY`, `USE_LLM_EXTRACTION`, `USE_LLM_QUERY_SYNTHESIS`, `USE_LLM_RERANK`) is optional. With none of them set, the agent runs fully offline and deterministically — this is the configuration the results below were measured on.

## Steps to Reproduce Results

Run the evaluator against the 200-session public dev set:

```
python -m evaluator.local_evaluator --catalog data/catalog.jsonl --dataset data/public_set.jsonl --output results.json
```

This drives `starter.agent.Agent` through every labeled session in `data/public_set.jsonl`, computes `hit_rate_at_10`, `mrr`, `mttc`, and `recommended_technical_score`, writes full per-session results to `results.json`, and prints the aggregate to stdout.

| Metric | Baseline (weak BM25) | Current agent |
|---|---|---|
| Technical Score | 0.10671 | 0.8445 |
| Hit Rate@10 | 12.5% | 98.5% (197/200) |
| MRR | 0.068034 | 0.6056 |
| MTTC | 9.81 | 2.485 |

Baseline numbers are from `docs/baseline_results.json`; current-agent numbers and the full turn-by-turn debugging history behind them (buying/browsing/boundary/intent-override scenario breakdowns included) are documented in `ISSUES.md` and `logs/eval_history.md`.

Other ways to exercise the agent:

- **Demo replay:** `python demo/run_demo.py` replays 4 curated sessions (one per scenario type — buying, browsing, boundary, intent override) with LLM flags forced off for determinism, and writes a transcript to `demo/demo_transcript.md`.
- **Unit tests:** `python -m unittest discover tests` covers the router, session state, and evaluator harness.
- **Smoke checks:** `python scripts/test_smoke.py` (happy-path) and `python scripts/test_bad_path.py` (adversarial edge cases — missing reset, empty messages, oversized input) confirm the agent never crashes.

## Limitations and What We'd Improve With More Time

- **3 of 200 public sessions still miss (1.5%).** The last remaining batch (see Issue 19 in `ISSUES.md`) came from candidates whose keyword rank exceeded the retrieval pool cutoff; widening the pool further produced flat returns, so these misses likely need a different fix rather than a bigger candidate pool.
- **Slot-fit matching is lexical, not semantic.** `rank.py` and `clarify.py` match slots against product text via substring/token overlap, so paraphrased attributes (e.g. "sneakers" vs. "athletic shoes") can be missed.
- **The LLM-augmented roles underperform the deterministic pipeline.** Slot extraction, query synthesis, and reranking (all via Groq) were each tested and measured net-neutral-to-negative on this eval distribution, so they're opt-in rather than integrated. This likely reflects insufficient prompt/signal tuning rather than a hard ceiling — worth revisiting with better prompts now that the deterministic baseline is strong.
- **Ranking weights are hand-tuned, not learned.** The blend of retrieval score, slot-fit, rating, and popularity in `rank.py` uses fixed constants rather than weights fit against a held-out split.
- **Track classification is a fixed rule.** Buying-vs-browsing is decided from a hard-coded slot list, not adapted per product category.
- **Only validated on the public 200-session dev set.** The private judging set (800 sessions) may expose distribution shift not visible here.

With more time, we'd prioritize: revisiting LLM reranking with better-tuned prompts and signals, learning the ranking blend weights instead of hand-tuning them, moving slot-fit matching to embedding similarity instead of token overlap, making the retrieval pool size adaptive per-query instead of a fixed cutoff, and adding a single automated step to log evaluator output after each run (the repo currently has three slightly different "latest" score snapshots across `results.json`, `ISSUES.md`, and `logs/eval_history.md` that would benefit from reconciling).
