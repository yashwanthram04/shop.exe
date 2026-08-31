# Evaluation Log

Use this file to record each local evaluator run. Keep one entry per run with the score, scenario breakdown, and a short note about what changed.

## Template

### Run YYYY-MM-DD HH:MM
- TechnicalScore:
- HitRate@10:
- MRR:
- MTTC:
- Efficiency:
- Buying:
- Browsing:
- Intent Override:
- Boundary:
- Note:

---

## Example

### Run 2026-08-29 18:40
- TechnicalScore: 0.112
- HitRate@10: 0.14
- MRR: 0.07
- MTTC: 9.7
- Efficiency: 0.13
- Buying: 0.18
- Browsing: 0.09
- Intent Override: 0.00
- Boundary: 0.00
- Note: Baseline run; retrieval still single-turn and browsing is weak.

---

### Run 2026-08-29 current baseline
- TechnicalScore: 0.10671
- HitRate@10: 0.125
- MRR: 0.068034
- MTTC: 9.81
- Efficiency: 0.119
- Buying: 0.2375
- Browsing: 0.025
- Intent Override: 0.133333
- Boundary: 0.0
- Note: Fresh baseline run from python -m evaluator.local_evaluator. Browsing is the major weakness, buying is much stronger, boundary is zero, and the overall score matches the documented baseline reference for the starter agent.

---

### Run 2026-08-31 (live keys, final confirmation) 
- TechnicalScore: 0.809543
- MRR: 0.65781
- MTTC: 3.14
- Efficiency: 0.786
- Buying: 0.925
- Browsing: 0.925
- Intent Override: 0.8
- Boundary: 1.0
- Token usage: 12224 prompt + 2061 completion = 14285 total (200 sessions) 
**Final state for this session: 0.809543 live / 0.816993 Groq-off**, up from the 0.10671 starting baseline and the 0.7964 mid-session documented baseline.

---

### Run 2026-08-31 (memory branch, live keys, full end-to-end)
- TechnicalScore: 0.817693
- HitRate@10: 0.92
- MRR: 0.662643
- MTTC: 3.055
- Efficiency: 0.7945
- Buying: 0.925
- Browsing: 0.9375
- Intent Override: 0.833333
- Boundary: 1.0
- Note: memory branch = main's 0.809543 run + the edge-case-audit branch's `clear_freeform_override` fix (also clears `filled_null`/`asked_categories`, not just `filled_slots`). +0.008 over main, concentrated in Browsing (+0.0125) and Intent Override (+0.033) — the scenarios that touch override/boundary-clearing state.

---

