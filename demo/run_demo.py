"""Mini demo pipeline: replays 4 curated sessions from demo_samples.jsonl
(one each of Buying / Browsing / Boundary / Intent Override) with clear,
labeled, turn-by-turn output for a video walkthrough.

Reuses evaluator/local_evaluator.py's own helper functions so behavior
matches the real evaluator exactly — this script does not reimplement any
scoring or conversation logic, it only formats the same replay for the
screen.

Runs with GROQ_API_KEY/OPENAI_API_KEY forced off (see below) so the output
is identical every take, regardless of .env contents or API availability.
"""
from __future__ import annotations

import os

os.environ.pop("GROQ_API_KEY", None)
os.environ.pop("OPENAI_API_KEY", None)

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evaluator.local_evaluator import (
    MAX_TURNS,
    TOP_K,
    catalog_index,
    coarse_category,
    customer_reply,
    initial_message,
    load_jsonl,
    materialize_hidden_fields,
    normalize_recommendations,
)
from starter.agent import Agent

DEMO_DIR = Path(__file__).resolve().parent
SAMPLES_PATH = DEMO_DIR / "demo_samples.jsonl"
CATALOG_PATH = DEMO_DIR.parent / "data" / "catalog.jsonl"
TRANSCRIPT_PATH = DEMO_DIR / "demo_transcript.md"

LABELS = {
    "buying": "BUYING — a quick hit",
    "browsing": "BROWSING — starts vague, converges",
    "boundary": "BOUNDARY — customer has no preference",
    "intent_override": "INTENT OVERRIDE — customer changes their mind",
}


def run_one(agent: Agent, sample: dict, catalog_ids: set, categories: dict, products: dict, out: list[str]) -> dict:
    sample_id = sample["sample_id"]
    scenario = sample["scenario_type"]
    target = str(sample["ground_truth"]["parent_asin"])
    session_id = f"demo_{sample_id}"

    agent.reset(session_id, sample["user_profile"])
    card, behavior = materialize_hidden_fields(sample, products)
    effective_sample = {**sample, "intent_card": card, "behavior": behavior}

    label = LABELS.get(scenario, scenario.upper())
    header = f"[{label}]  sample_id={sample_id}  target={target}"
    out.append("\n" + "=" * len(header))
    out.append(header)
    out.append("=" * len(header))

    disclosed: set = set()
    boundary_used = False
    override_applied = scenario != "intent_override"
    user_message = initial_message(effective_sample, coarse_category(categories.get(target, [])), disclosed)

    hit_turn = None
    hit_rank = None

    for turn in range(1, MAX_TURNS + 1):
        response = agent.respond(session_id, user_message, turn, TOP_K)
        ranked = normalize_recommendations(response.get("recommendations"), catalog_ids)
        hit = target in ranked
        rank = ranked.index(target) + 1 if hit else None

        out.append(f"\nTurn {turn}")
        out.append(f"  Customer : {user_message}")
        out.append(f"  Agent    : {response.get('message')!r}  (ask_attribute={response.get('ask_attribute')!r})")
        out.append(f"  Top-10   : {'HIT, rank ' + str(rank) if hit else 'miss'}")

        if override_applied and hit:
            hit_turn, hit_rank = turn, rank
            break
        if turn == MAX_TURNS:
            break

        override = effective_sample.get("behavior", {}).get("override") or {}
        if not override_applied and turn + 1 == int(override.get("turn", 3)):
            override_applied = True
            new_value = str(override.get("new_value", ""))
            if new_value:
                disclosed.add(new_value)
            user_message = str(override.get("message", "Actually, please ignore my earlier preference."))
        else:
            user_message, boundary_used = customer_reply(
                effective_sample, response.get("ask_attribute"), disclosed, boundary_used
            )

    result_line = f"\nRESULT: {'HIT at turn ' + str(hit_turn) + ', rank ' + str(hit_rank) if hit_turn else 'MISS (10 turns exhausted)'}"
    out.append(result_line)
    return {"sample_id": sample_id, "scenario": scenario, "hit_turn": hit_turn, "hit_rank": hit_rank}


def main() -> None:
    samples = load_jsonl(str(SAMPLES_PATH))
    catalog_ids, categories, products = catalog_index(str(CATALOG_PATH))
    agent = Agent(str(CATALOG_PATH))

    out: list[str] = ["# Demo transcript\n"]
    summary = []
    for sample in samples:
        summary.append(run_one(agent, sample, catalog_ids, categories, products, out))

    out.append("\n" + "=" * 60)
    out.append("SUMMARY")
    out.append("=" * 60)
    for row in summary:
        result = f"turn {row['hit_turn']}, rank {row['hit_rank']}" if row["hit_turn"] else "MISS"
        out.append(f"  {row['scenario']:<16} {row['sample_id']:<14} {result}")

    text = "\n".join(out)
    print(text)
    TRANSCRIPT_PATH.write_text(text + "\n", encoding="utf-8")
    print(f"\n(saved to {TRANSCRIPT_PATH.relative_to(DEMO_DIR.parent)})")


if __name__ == "__main__":
    main()
