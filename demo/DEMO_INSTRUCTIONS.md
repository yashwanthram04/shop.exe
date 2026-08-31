# Demo instructions

One command, run from the repo root:

```
python3 demo/run_demo.py
```

Takes a few seconds. It replays 4 curated sessions from
`demo/demo_samples.jsonl` (one Buying, one Browsing, one Boundary, one
Intent Override) through the real agent, prints a clearly labeled
turn-by-turn transcript for each straight to the terminal, and also saves
the same text to `demo/demo_transcript.md`.

It forces the LLM key off internally, so it's 100% deterministic — same
output every take, no API flakiness risk while recording.

## What each segment shows

| segment | sample_id | what happens |
|---|---|---|
| Buying | `public_0005` | Hits on turn 1, rank 1 — fastest possible |
| Browsing | `public_0087` | Vague opener, 4 rounds of clarification, hits turn 5 |
| Boundary | `public_0180` | Customer says "no preference" right after the first question, still hits turn 5 |
| Intent Override | `public_0068` | Discloses a preference, then turn 3 says "Actually, ignore my earlier preference...", hits turn 7 |

## Screen order for the recording

1. **`demo/demo_samples.jsonl`** — briefly show this is 4 real rows pulled straight from the actual eval dataset, not scripted.
2. **Terminal running `python3 demo/run_demo.py`** — the live walkthrough. Let each of the 4 labeled blocks play out; the `RESULT:` line at the end of each block is the payoff moment.
3. **`demo/demo_transcript.md`** (optional) — the same output saved cleanly, in case you'd rather scroll a file than terminal history.
4. **`logs/eval_history.md`** (latest entry, "Run 2026-08-31 (main, live keys)") — the headline numbers: TechnicalScore 0.843143, HitRate@10 0.985, MRR 0.607478, MTTC 2.58.
5. **`docs/baseline_results.json`** — the starting point to contrast against: TechnicalScore 0.10671. This is your "~8x" claim.

Run it once beforehand to confirm everything's warmed up (embedding cache
already built from earlier runs, so it should be fast), then run it again
for the actual take.
