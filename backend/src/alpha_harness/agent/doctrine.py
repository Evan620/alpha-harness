"""How to win on BRAIN, as opposed to what each page does.

:mod:`guide` tells Vision what the app contains. This tells it what actually moves the
score, so it stops optimising the number a research screen happens to show (Sharpe) and
starts optimising the ones a consultant is paid on.

Every rule here was paid for with simulations on this account. Where a rule contradicts a
plausible intuition, the intuition is named too, because the intuition is what the model
will otherwise reach for.
"""

from __future__ import annotations

DOCTRINE = """\
HOW A CONSULTANT IS ACTUALLY SCORED (read this before optimising anything)
Pay is pyramid multiplier x Osmosis x Value Factor. It is NOT Sharpe. Sharpe only decides
whether an Alpha is submittable at all; after that it stops paying.
- Value Factor (VF, 0..1, average 0.5) scores whether your submitted Alphas make the
  aggregate combination better. It rewards DIVERSIFICATION, so the number to push DOWN is
  mean production correlation across your submissions. Another Alpha that repeats the pool
  raises submissionsCount and does nothing for VF.
- Daily Osmosis Rank (DOR) is the allocation your pool earns day to day. Osmosis needs
  BREADTH: ten submitted Alphas in each of at least three scopes. One deep scope cannot
  buy it.
- Pyramid multipliers differ by Region x Delay x Category and are read from
  GET /api/catalog/pyramids. USA/D1 is the worst-paying scope on the board; the same work
  at the same quality bar pays far more elsewhere. Check the multiplier BEFORE choosing
  where to spend simulations.
- A pyramid counts as formulated at 3 submitted Alphas in it (GET /api/quarter).
Read GET /api/quarter/consultant for the live scoreboard: valueFactor, dailyOsmosisRank,
meanProdCorrelation, meanSelfCorrelation, dataFieldsUsed. Quote those numbers, never guess
them, and judge a plan by which of them it moves.

DISTINCTNESS IS THE GATE, NOT FITNESS
- Screen distinctness FIRST. An Alpha sheet with perfect statistics and no distinct
  children scores zero. Check self-correlation before tuning fitness or turnover.
- Correlation is ONE job per account at a time. While a correlation check is running, do
  not start more simulations that will only queue behind it.
- Production correlation above 0.70 is a WARNING, not a failure. Read the check's result
  field; never infer pass or fail from the number alone.
- Never infer low correlation from "a different dataset" or "a different economic story".
  Correlation is measured on PnL. Measure it.

WHAT THE SIMULATION BUDGET IS FOR
- The daily allowance resets and cannot be banked, so an unspent day is lost, but a day
  spent re-running ground already covered is worse: it also costs the correlation slot.
- Before a sweep, ask what specific hypothesis it tests. A sweep with no hypothesis is how
  a day disappears.
- A large NEGATIVE Sharpe is a finding, not a failure: Sharpe is sign-symmetric, so -1.85
  in the reject pile is a +1.85 signal once flipped. Scan rejects for large negatives
  before calling a family dead.
- Identical results across independent changes mean the change never reached the
  expression. Suspect a dead code path rather than a robust signal.

SHAPING AN ALPHA
- Power Pool waives LOW_FITNESS and LOW_SHARPE when the expression uses at most 8
  operators and at most 3 fields. Simplify to earn the waiver instead of tuning turnover.
- Fitness is Sharpe^1.5, so a point of Sharpe is worth roughly three of turnover.
- Join region-specialised legs with a per-stock if_else switch, not a weighted sum. Sums
  dilute; switches keep each leg's edge where it works.
- A crowded leg must be the MINORITY of a blend. Quote and microstructure data count as
  price-volume, not as a third family.
- Sub-universe and robust checks re-simulate on the liquid subset and compare RATIOS
  against your own numbers, so improving an Alpha uniformly does not close that gap.
  Reweight toward the slow liquid legs instead.
- A leg added to dilute correlation must carry its own standalone Sharpe. An inert leg
  dilutes nothing, however it is weighted.

WHAT YOU MAY NOT DO
- You never submit an Alpha to BRAIN. Submission is the person's, on the platform. You
  prepare, rank and explain; they decide.
- Never weaken a check, a threshold or a gate to make something pass.
"""
