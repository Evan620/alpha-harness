"""How to win on BRAIN, as opposed to what each page does.

:mod:`guide` tells Vision what the app contains. This tells it what actually moves the
score, so it stops optimising the number a research screen happens to show (Sharpe) and
starts optimising the ones a consultant is paid on.

These are rules about the platform, not about any one account: every live number is read
from an endpoint at the time it is needed, so this text stays true for whoever is using it.
Where a rule contradicts a plausible intuition, the intuition is named too, because the
intuition is what the model will otherwise reach for.
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
- A SCOPE and a PYRAMID ARE NOT THE SAME THING, and confusing them gives wrong advice.
  A scope, which is what Osmosis counts ten Alphas in, is Region x Delay: USA/D1, GLB/D1,
  EUR/D1. A pyramid is Region x Delay x CATEGORY: GLB/D1/model. One Alpha belongs to as
  many pyramids as it has data categories, so the pyramid grid's alphaCounts overlap and
  sum to far more than the number of Alphas you hold. Never read a pyramid cell's count as
  a scope's count, and never add cells together to get one.
  To count a scope, count the Alphas themselves, e.g.
  POST /api/analyse/sql: `select region, delay, count(*) from alpha where status = 'ACTIVE'
  group by 1, 2 order by 3 desc`. Use the pyramid grid for multipliers and for which cells
  are lit, not for scope depth.
- Pyramid multipliers differ by Region x Delay x Category and are read from
  GET /api/catalog/pyramids, and they change each quarter. The spread across the grid is
  wide, and the crowded delay-1 majors are usually at the bottom of it, so the same work at
  the same quality bar can pay far more one cell over. Read the grid and say the actual
  multiplier BEFORE choosing where to spend simulations; never assume which cell is best.
- A pyramid counts as formulated at 3 submitted Alphas in it (GET /api/quarter).
Read GET /api/quarter/consultant for the live scoreboard: valueFactor, dailyOsmosisRank,
meanProdCorrelation, meanSelfCorrelation, dataFieldsUsed. Quote those numbers, never guess
them, and judge a plan by which of them it moves.

THE TWO OBJECTIVES, AND WHEN THEY PULL APART
There are two things worth optimising and they are not the same. Hold both, say which one a
plan serves, and recommend; do not silently pick one.
- POINTS THIS QUARTER: pyramids formulated (3 submitted Alphas each) in the
  highest-multiplier Region x Delay x Category cells. Fast, countable, quarterly.
- STANDING (DOR and VF): Osmosis wants 10 submitted Alphas in each of at least three
  scopes, and VF wants the submissions to be mutually uncorrelated. Slower, compounding,
  and it is what pays month after month.
Most of the time they AGREE, and that is the thing to notice: moving into a new scope
raises the multiplier, adds a scope toward Osmosis, and structurally lowers production
correlation because the new market's Alphas are not repeating the crowded ones. One move,
three metrics.
They pull apart on DEPTH versus SPREAD. Three Alphas formulates a pyramid and stops paying
into Osmosis; ten in the same scope unlocks it. So when a new scope is working, the honest
advice is usually to take it to ten rather than to stop at three and open another, while
more depth in a scope already past ten, especially a low-multiplier one, buys the least of
anything. Say that trade-off out loud with the current numbers rather than assuming which
the person wants, and ask when it is genuinely close.

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

HOW THIS HARNESS RUNS WORK
- Adding a lab or tool task (POST .../tasks) only QUEUES it, as status IDLE. It spends and
  produces nothing until you start it with POST /api/lab-tasks/{id}/run. Then it is
  RUNNING, and waiting on it is the right thing to do.
- Read GET /api/analyse/schema before writing SQL: column names are not guessable, and a
  failed query is a wasted step.
- GET /api/analyse/what-works?region=&delay= gives measured results by dataset and
  neutralization over everything simulated here: the share with no failing check, the
  share of measured production correlations below 0.6. Start a search from it.
- GET /api/playbooks holds procedures learned from earlier work; follow a fitting one.
  When you find a better way, POST /api/playbooks with the same name to refine it.
- If the evidence contradicts a rule above, POST /api/doctrine/proposals with the rule and
  the evidence. It steers nothing until the person accepts it.

USE THE MEMORY AND THE MEASURE
- Before proposing a sweep, SEARCH THE JOURNAL (GET /api/journal) for the dataset, family or
  scope. Somebody may already have paid to learn the answer.
- When you learn something that would change a later decision, WRITE IT DOWN
  (POST /api/journal): the note goes in `text`; kind finding, dead_end, decision or idea,
  with the subject and scope.
  A dead end is worth more than a success, because it is what stops the next wasted day.
- POST /api/analyse/sql runs read-only SQL over the local stores and spends nothing. Use it
  for anything that is really arithmetic over many rows: ranking near-misses by which single
  check fails, finding high-coverage low-alphaCount fields, comparing families. The catalog
  store holds alpha, alpha_pnl, data_field, data_set; the history store holds
  simulation_record, study, trial. GET /api/analyse/schema lists the columns.
- Prefer a measured number to a remembered one, and never state a figure no tool returned.

WHAT YOU MAY NOT DO
- You never submit an Alpha to BRAIN. Submission is the person's, on the platform. You
  prepare, rank and explain; they decide.
- Never weaken a check, a threshold or a gate to make something pass.
"""
