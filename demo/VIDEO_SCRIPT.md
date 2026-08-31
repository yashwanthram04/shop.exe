# Video script (talking points, not word-for-word)

## Intro
- This is a conversational shopping agent — it retrieves products across
  turns, asks clarifying questions, and adapts as the customer's intent
  becomes clearer.
- I'll walk through 4 real conversations pulled straight from the
  evaluation dataset, then show the numbers.

## 1. Buying — quick hit (`public_0005`)
- Customer states a clear requirement immediately: "leather," category
  disclosed up front.
- Point out: the agent finds the right product on the very first turn,
  ranked #1. When intent is clear, there's no wasted back-and-forth.

## 2. Browsing — starts vague, converges (`public_0087`)
- Customer opens with "I'm looking for shirts... but I'm still exploring" —
  no hard constraint given.
- Point out: the agent asks a sequence of clarifying questions (material,
  brand, color, style), narrowing the candidate pool each turn, until it
  converges on the right product by turn 5.
- This is the core "browsing" behavior — the agent earns the information it
  needs instead of guessing.

## 3. Boundary — no preference (`public_0180`)
- Right after the first question, the customer says "I don't have a
  preference for that, use your judgment."
- Point out: the agent doesn't get stuck — it stops asking about that
  attribute, moves to the next one, and still converges to a hit by turn 5.
  It handles genuinely uncertain customers gracefully.

## 4. Intent override — customer changes their mind (`public_0068`)
- Customer discloses one preference on turn 1, then on turn 3 says
  "Actually, ignore my earlier preference. What I need is: Imported."
- Point out: the agent correctly drops the stale preference and re-focuses
  on the new one, still finding the right product by turn 7. This is the
  hardest scenario type — the agent has to un-learn something it already
  believed.

## Outro — the numbers
- Across the full 200-session evaluation set: TechnicalScore went from
  0.107 on the provided baseline to 0.8431 — about an 8x improvement.
- Hit rate: 98.5% (197 of 200 sessions find the right product in the
  top 10). MRR 0.6075, mean turns to conversion 2.58.
- These 4 conversations were chosen to clearly show each behavior on
  camera, but the behaviors themselves — converging through clarification,
  handling "no preference," recovering from a changed mind — are the same
  patterns that drive the 98.5% hit rate across all 200 sessions, not
  one-off exceptions.
