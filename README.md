# Validation of an Upbit-pump short strategy

A IS/OOS + walk-forward backtest of the thesis that **Korea-driven altcoin pumps on Upbit mean-revert**, and can be shorted profitably after the move.

## Theorem

A sharp, Korea-driven upward move in an altcoin on Upbit is, on average, **partially reverted** over the following hours or days — enough that a short opened **after** the move and held to a fixed horizon has **positive net expectancy after costs**.

## Hypotheses

**H1 — Reversion exists.**
Shorting a qualifying pump (see Signals below) yields positive net-of-costs expectancy, on data not used to calibrate the filter thresholds.
H0: expectancy ≤ 0. Tested via IS/OOS split — thresholds tuned only on IS, expectancy (with bootstrap CI) measured only on OOS.

**H2 — The edge is Korea-specific.**
Among candidates that pass H1's filters, expectancy is significantly higher when the kimchi premium rose within a specific range around the event than when it stayed flat or rose sharply.
H0: expectancy(kimchi in range) = expectancy(kimchi outside range) — i.e. kimchi carries no information.
Tested via **paired contrast**: all other parameters held fixed at their H1-calibrated values, only the kimchi filter is toggled on/off on the same candidate pool. The kimchi range itself must be fixed on IS and confirmed on OOS — never grid-searched and reported on the same sample.

H1 without H2 would mean "pumps revert regardless of cause" (kimchi irrelevant). H2 only matters once H1 is confirmed — it specifies *which* pumps.

## Signals & parameters

Every threshold and timing below is a **tuned parameter**, not a fixed assumption — exactly what the grid search and IS/OOS evaluate.

**Filters (what flags an event):**
- **Volume** — event-hour traded value exceeds the cumulative value of preceding hours.
- **Breakout** — event-hour high exceeds prior hours' highs by a margin.
- **Pump magnitude** — intra-hour move (open → high) above a base percentage.
- **Close confirmation** — the candle closes up, not just an intrabar wick.
- **Kimchi premium** — `KRW / (USD × KRW-USDT) − 1`, sampled before the event, at entry, at exit, and as a timeline (used to test H2).

**Timing & geometry (grid-searched):** event window time-of-day, entry delay, exit horizon, stop %, take-profit capture (fraction of the run-up), max concurrent positions.

## Validation protocol

- IS/OOS split, OOS untouched until a single final run.
- No hardcoded parameters — everything above is grid-searched on IS only.
- Point-in-time fills (next bar's open), no lookahead.
- Minimum trade count gate before any config is eligible (prevents thin-sample overfitting).
- IS scoring uses bootstrap CI lower bound on expectancy, not point estimates.
- Selected config must sit on a stable plateau in the grid, not an isolated peak.
