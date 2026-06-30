# Validation of an Upbit-pump short strategy

A rigorous IS/OOS + walk-forward backtest of the thesis that **Korea-driven altcoin pumps on Upbit mean-revert**, and can be shorted profitably after the move.

## Theorem

A sharp, Korea-driven upward move in an altcoin on Upbit is, on average, **partially reverted** over the following hours — enough that a short opened *after* the move and held to a fixed horizon has **positive net expectancy after costs**.

Mechanism: Korea's capital controls impede cross-border arbitrage, so a local retail surge lets the Upbit price overshoot the global price. Local overshoots tend to collapse back. The trade bets on that collapse.

## Hypotheses

- **H1 — Reversion exists.** Forward return of the token from a post-pump entry to the exit horizon is negative on average.
- **H2 — Tradeable after costs.** A short capturing that reversion has positive net expectancy after fees, funding, slippage, and stop-through.
- **H3 — Korea-specific.** The edge strengthens when the Korean (kimchi) premium is elevated and/or starts deflating, and is weaker without it. This separates the thesis from "short any pump."
- **H4 — Not an artifact.** The edge survives OOS and null/permutation tests, isn't explained by survivorship or look-ahead, and isn't concentrated in a few outlier trades.

The theorem holds only if **H1–H4 hold jointly**. H3 is what makes it specifically a *Korean* phenomenon rather than generic mean reversion.

## Signals & parameters

Every threshold and timing below is a **tuned parameter**, not a fixed assumption — they are exactly what the grid search and IS/OOS evaluate.

**Filters (what flags an event):**
- **Volume** — the event-hour traded value exceeds the cumulative value of the preceding hours.
- **Breakout** — the event-hour high exceeds prior hours' highs by a margin.
- **Pump magnitude** — intra-hour move (open → high) above a base percentage.
- **Close confirmation** — the candle closes up, not just an intrabar wick.
- **Kimchi premium** — `KRW / (USD × KRW-USDT) − 1`, sampled before the event, at entry, at exit, and as a timeline (used to test H3).

**Timing & geometry (grid-searched):** event window time-of-day, entry delay, exit horizon, stop %, take-profit capture (fraction of the run-up), max concurrent positions.
