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

### Run 2026-08-31 (main, live keys)
- TechnicalScore: 0.843143
- HitRate@10: 0.985
- MRR: 0.607478
- MTTC: 2.58
- Efficiency: 0.842
- Buying: 0.975
- Browsing: 1.0
- Intent Override: 0.966667
- Boundary: 1.0