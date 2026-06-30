# Validation of an Upbit-pump short strategy

A rigorous IS/OOS + walk-forward backtest of the thesis that **Korea-driven altcoin pumps on Upbit mean-revert**, and can be shorted profitably after the move.

## Theorem

A sharp, Korea-driven upward move in an altcoin on Upbit is, on average, **partially reverted** over the following hours — enough that a short opened *after* the move and held to a fixed horizon has **positive net expectancy after costs**.

Mechanism: Korea's capital controls impede cross-border arbitrage, so a local retail surge lets the Upbit price overshoot the global price. Local overshoots tend to collapse back. The trade bets on that collapse.

Consequence: the edge is a **high win rate with negative skew** — many small wins (reversion) against rare large losses (pump continues). Positive expectancy is necessary but not sufficient; the thesis must also survive the left tail.

## Hypotheses

- **H1 — Reversion exists.** Forward return of the token from a post-pump entry to the exit horizon is negative on average (kimchi-agnostic).
- **H2 — Tradeable after costs.** A short capturing that reversion has positive net expectancy after fees, funding, slippage, and stop-through.
- **H3 — Korea-specific.** The edge strengthens when the Korean (kimchi) premium is elevated and/or starts deflating, and is weaker without it. This separates the thesis from "short any pump."
- **H4 — Not an artifact.** The edge survives OOS and null/permutation tests, isn't explained by survivorship or look-ahead, and isn't concentrated in a few outlier trades.

The theorem holds only if **H1–H4 hold jointly**. H3 is what makes it specifically a *Korean* phenomenon rather than generic mean reversion.

## Signals

An **event** (Korean pump) is a token-day flagged by abnormal upward activity on Upbit:

- **Volume spike** — the event-hour traded value exceeds the cumulative value of the preceding *N* hours.
- **Breakout** — the event-hour high exceeds prior *N*-hour highs by a margin.
- **Pump magnitude** — intra-hour move (open → high) above a base percentage.
- **Close confirmation** — the candle closes up by a threshold, not just an intrabar wick.
- **Entry gate** — the move is still above threshold at entry time (the run-up persisted).

**Kimchi premium** (measured per trade, used to test H3): `KRW_price / (USD_price × KRW/USDT) − 1`, sampled as a pre-event baseline, at entry, at exit, and as a 5-minute timeline.

Trade: short after the move, protective stop and take-profit (take captures a fraction of the run-up), otherwise time-exit at the next session.

## Validation methodology

- **IS/OOS split** — chronological; the last ~30–40% is held out and untouched until a single final run.
- **Pre-registered parameter grid** — small and fixed in advance (event thresholds, trade geometry, kimchi overlay on/off). The kimchi overlay vs. the no-overlay baseline *is* the test of H3.
- **Walk-forward** — rolling train→test windows; the edge must hold in the majority of windows, not one stretch.
- **Cost & execution stress** — sweeps over fees, slippage, and **stop-through** (worse-than-nominal stop fills on illiquid continued pumps), plus funding scenarios.
- **Null tests** — randomized entry direction/timing must make the edge vanish; event→date permutation.
- **Tail & concentration** — bootstrap confidence intervals (returns are skewed, non-normal), worst-N trades, and share of PnL from the top trades.
- **Honest accounting** — symmetric fees, funding, and mark-to-market drawdown with a concurrent-position cap.

**Pass (on OOS, jointly):** H1 reversion significant (CI excludes zero) · H2 positive net expectancy after costs · H3 kimchi overlay beats baseline · H4 edge in most walk-forward windows, survives stop-through, fails null tests, not outlier-driven · drawdown within risk budget.

## Status

Design stage. Theorem and validation protocol fixed; implementation and backtest to follow.
