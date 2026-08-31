# Shopping Copilot — Full Demo Script

*Aligned to the final 5-slide deck (shopping.pdf) plus the live `demo/run_demo.py` walkthrough. Slides ~3:20, demo ~1:55, total ~5:15 at 150 wpm. Trim notes at the bottom.*

---

# PART 1 — SLIDES

## Slide 1 — Title card
### *"Shopping Copilot — AI Conversational Search and Recommendations"*
**[0:00–0:20]**

> Imagine chatting with a shopping assistant that actually understands what you're looking for, even when you don't say it perfectly. That's what we built for TechJam this year: a conversational agent that can take a shopper from a vague idea to the exact product they'd buy, in just a few messages.

## Slide 2 — Pipeline diagram
### *Understand → Route → Retrieve → Clarify/Answer → Rank → Respond*
**[0:20–1:38]**

> Every message moves through the same six-step pipeline, and it starts by turning what the shopper typed into structured facts, things like size, color, or budget. From there, the agent decides how to route the conversation. If the shopper has already given us something specific, we treat it as a focused buying conversation and filter hard around that constraint. If they're still exploring, we open things up and search broadly instead.
>
> Either way, retrieval runs two searches at once, one on keywords and one on meaning, and combines them by rank rather than raw score, because the two aren't measured on the same scale. Then the agent decides whether to ask or answer: if the pool of matching products is still too broad, it asks one more clarifying question, and if it's narrow enough, it just answers. From there we rank what's left by relevance, rating, and fit, and respond with a ranked list and the next question together.
>
> Running underneath the whole conversation is memory: everything the shopper tells us carries forward, fades if it goes unused, and gets rewritten the moment they change their mind.

## Slide 3 — "Four Sessions, Every Turn Type"
**[1:38–2:16]**

> To show that off, we're running four real sessions from the dev set, one for each kind of conversation the agent has to handle. A buying session, where the shopper states a hard constraint right away and we move fast. A browsing session, which starts vague and narrows down gradually as we learn more. A boundary session, where the shopper genuinely doesn't have a preference and just wants us to use our judgment. And an intent override session, where they change their mind partway through, and the agent has to erase what it assumed and rebuild from there.

## Slide 4 — "Same Pipeline. Every Outcome."
**[2:16–2:54]**

> Here's how those four sessions actually played out. The buying session resolved almost immediately, on the very first turn. The browsing and boundary sessions both took five turns to converge, since there was less to go on early. The toughest one, the intent override, still resolved by turn seven, even after the shopper changed direction midway through. All four landed well inside our ten-turn limit, which is really the point: the same pipeline handles the easy case and the hard case without needing separate logic for either.

## Slide 5 — "7.9× the Technical Score"
**[2:54–3:30]**

> And the results back that up. Against the same two hundred evaluation sessions, our tuned pipeline scores seven point nine times higher than the baseline BM25 agent on the competition's technical score. Hit rate at ten went from twelve and a half percent to ninety-eight and a half. Mean reciprocal rank went from point zero seven to point six one. On average, we now find the right product in under three turns, down from nearly ten. Same catalog, same scoring formula, a completely different outcome.

---

# PART 2 — LIVE DEMO

*Every beat below is: what's on your screen, then what you say over it. Times are relative to the start of the demo section.*

## Demo 1 — The test set
**[0:00–0:20]** · **ON SCREEN:** cut from slides to your editor with **`demo/demo_samples.jsonl`** open — 4 rows, scroll slowly so the `scenario_type` and `ground_truth` fields are visible

> But rather than take my word for it, let me show you the agent actually running. These four rows are the demo set: one buying, one browsing, one boundary, one intent override, pulled straight out of the evaluation dataset with their ground-truth product IDs attached. Nothing here was written for the video.

## Demo 2 — Run it
**[0:20–0:35]** · **ON SCREEN:** terminal at the repo root — type `python3 demo/run_demo.py` and hit enter

> One command replays all four through the real agent. It reuses the evaluator's own conversation logic rather than reimplementing it, and it forces the LLM keys off internally, so this output is identical every single time it runs.

## Demo 3 — Buying
**[0:35–0:53]** · **ON SCREEN:** scroll to the `[BUYING — a quick hit]` block; pause on `RESULT: HIT at turn 1, rank 1`

> First is the buying case. The customer names a category and one hard requirement, leather, and the agent returns the exact target product at rank one, on turn one. When the intent is already clear, there's no back-and-forth worth having.

## Demo 4 — Browsing
**[0:53–1:13]** · **ON SCREEN:** scroll through the `[BROWSING]` block, turns 1 through 5 — let the `ask_attribute=` values be readable

> The browsing case starts with almost nothing: shirts, but I'm still exploring. Watch the ask_attribute on each turn. The agent works through material, then brand, then colour, then style, narrowing the candidate pool every round, and lands the target on turn five.

## Demo 5 — Boundary
**[1:13–1:31]** · **ON SCREEN:** scroll to the `[BOUNDARY]` block; pause on the turn 2 customer line

> The boundary case is the awkward one. Right after the first question, the customer says they have no preference and to just use our judgment. The agent doesn't stall on it. It closes that attribute off, moves to the next one, and still converges by turn five.

## Demo 6 — Intent override
**[1:31–1:50]** · **ON SCREEN:** scroll to the `[INTENT OVERRIDE]` block; pause on the turn 3 line *"Actually, ignore my earlier preference"*

> And the hardest one. On turn three the customer reverses course: actually, ignore my earlier preference. The agent drops what it had assumed, re-anchors on the new constraint, and recovers to a hit on turn seven, still inside the ten-turn budget.

## Demo 7 — Summary
**[1:50–2:05]** · **ON SCREEN:** scroll to the `SUMMARY` block at the bottom of the output

> Four scenarios, four hits: turns one, five, five and seven. Same pipeline, same code path, no special-casing for any of them. That's the behaviour behind the ninety-eight point five percent hit rate across the full two hundred sessions.

---

## Screen checklist (in order)

1. `demo/demo_samples.jsonl` — the four real test rows
2. Terminal at repo root → `python3 demo/run_demo.py`
3. Scroll: `[BUYING]` → `[BROWSING]` → `[BOUNDARY]` → `[INTENT OVERRIDE]` → `SUMMARY`
4. *Optional receipts, if you want them on camera:* `logs/eval_history.md` (latest entry — TechnicalScore 0.843143, HR@10 0.985, MRR 0.607478, MTTC 2.58) and `docs/baseline_results.json` (0.10671)

## Production notes

- **Run it once before recording.** The embedding cache is already built from earlier runs, so the take itself will be fast — but a cold run is the one thing that could stall on camera.
- **The whole transcript prints in a few seconds, not turn by turn.** So the four beats above are narrated *while you scroll*, not while it runs. If your terminal history looks messy, pre-run it and scroll `demo/demo_transcript.md` instead — identical text, cleaner to read.
- **`demo/VIDEO_SCRIPT.md` in the repo has stale numbers** (TechnicalScore 0.8445, MRR 0.6056, MTTC 2.485 — from an earlier run). The slides and this script use the current run: 0.843143 / 0.607478 / 2.58. Worth updating that file so the repo doesn't contradict the video.

## Trim options

- **To ~4:30 total:** cut Slide 2's middle paragraph to one sentence — *"Retrieval blends keyword and semantic search, and the agent either asks one more question or answers, depending on how broad the results still are."*
- **To ~1:20 of demo:** drop Demo 5 (boundary) and let Demo 4 carry the clarification story. Keep the override — it's the one judges will care most about.
