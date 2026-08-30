"""One-off tool: run the real Agent through all 200 public dev sessions and
write a full human-readable transcript (every turn's customer message,
agent message/ask_attribute/recommendations, and the final outcome) to
transcripts.log. Uses the evaluator's own session-simulation helpers so the
replayed conversations are byte-identical to what local_evaluator.py itself
produces — this is NOT a reimplementation, it drives the same Agent class.
Not part of the submission; a debugging aid only.
"""
import json
from evaluator.local_evaluator import (
    catalog_index, load_jsonl, coarse_category, initial_message,
    customer_reply, materialize_hidden_fields, normalize_recommendations,
    MAX_TURNS, TOP_K,
)
from starter.agent import Agent

catalog_ids, categories, products = catalog_index("data/catalog.jsonl")
samples = load_jsonl("data/public_set.jsonl")
agent = Agent("data/catalog.jsonl")


def title_of(asin: str) -> str:
    p = products.get(asin)
    if not p:
        return f"{asin} (unknown)"
    return f"{asin} — {str(p.get('title', ''))[:70]}"


lines: list[str] = []
hits = 0

for i, sample in enumerate(samples, 1):
    session_id = f"log_{sample['sample_id']}"
    target = str(sample["ground_truth"]["parent_asin"])
    effective_intent_card, effective_behavior = materialize_hidden_fields(sample, products)
    effective_sample = {**sample, "intent_card": effective_intent_card, "behavior": effective_behavior}
    disclosed: set[str] = set()
    boundary_used = False
    override_applied = sample["scenario_type"] != "intent_override"

    agent.reset(session_id, sample["user_profile"])
    user_message = initial_message(effective_sample, coarse_category(categories.get(target, [])), disclosed)

    lines.append("=" * 100)
    lines.append(f"SESSION {i}/{len(samples)}  id={sample['sample_id']}  scenario={sample['scenario_type']}")
    lines.append(f"TARGET (hidden answer): {title_of(target)}")
    lines.append("-" * 100)

    hit_turn = None
    best_rank = None

    for turn in range(1, MAX_TURNS + 1):
        try:
            response = agent.respond(session_id, user_message, turn, TOP_K)
        except Exception as exc:
            response = {"message": "", "ask_attribute": None, "recommendations": []}
            lines.append(f"  [turn {turn}] AGENT CRASHED: {exc!r}")

        ranked = normalize_recommendations(response.get("recommendations"), catalog_ids)
        is_hit = override_applied and target in ranked

        lines.append(f"[Turn {turn}] CUSTOMER SAYS: {user_message!r}")
        lines.append(f"           AGENT ask_attribute: {response.get('ask_attribute')!r}")
        lines.append(f"           AGENT message: {response.get('message')!r}")
        lines.append(f"           AGENT recommends (top 5 of {len(ranked)}):")
        for rank_i, asin in enumerate(ranked[:5], 1):
            marker = "  <== TARGET" if asin == target else ""
            lines.append(f"             {rank_i}. {title_of(asin)}{marker}")
        usage = response.get("usage") or {}
        if usage.get("prompt_tokens") or usage.get("completion_tokens"):
            lines.append(f"           usage: {usage}")

        if is_hit:
            hit_turn = turn
            best_rank = ranked.index(target) + 1
            lines.append(f"           >>> HIT at rank {best_rank} <<<")
            break

        if turn == MAX_TURNS:
            lines.append("           >>> MISS (turn limit reached) <<<")
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

    lines.append("-" * 100)
    if hit_turn is not None:
        hits += 1
        lines.append(f"OUTCOME: HIT — turn {hit_turn}, rank {best_rank}, reciprocal_rank={1.0/best_rank:.3f}")
    else:
        lines.append("OUTCOME: MISS")
    lines.append("")

lines.insert(0, f"Hit rate: {hits}/{len(samples)} ({hits/len(samples):.1%})\n")

with open("transcripts.log", "w", encoding="utf-8") as f:
    f.write("\n".join(lines))

print(f"Wrote transcripts.log — {hits}/{len(samples)} hits ({hits/len(samples):.1%})")
