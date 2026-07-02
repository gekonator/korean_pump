# Validation of an Upbit-pump short strategy

An out-of-sample backtest of the thesis that **Korea-driven altcoin pumps on
Upbit mean-revert**, and can be shorted profitably after the move.

The project is deliberately structured as a *falsification exercise*, not a
performance showcase: every claim is a pre-registered, testable hypothesis with
a stated null, an in-sample / out-of-sample split, and a pass/fail criterion
fixed **before** the out-of-sample data is touched.

---

## Theorem

A sharp, Korea-driven upward move in an altcoin on Upbit is, on average,
**partially reverted** over the following hours or days — enough that a short
opened *after* the move and held to a fixed horizon has **positive net
expectancy after costs**.

The edge is expected to have a characteristic shape: a **high win rate with
negative skew** — many small wins (reversion) against rarer large losses (the
pump continues). Positive expectancy is therefore necessary but not sufficient;
the thesis must also survive the left tail, which is why confidence intervals,
not point estimates, drive every decision.

---

## Hypotheses

The two hypotheses are nested in importance: **H2 is only meaningful once H1
holds.** H1 asks *whether* qualifying pumps revert profitably; H2 asks *whether
the Korean premium is what makes them do so*, rather than generic mean
reversion.

### H1 — Reversion exists, net of costs

Shorting a qualifying pump (see *Signals*) yields **positive net-of-costs
expectancy** on data not used to calibrate the filter thresholds.

- **H0:** expectancy ≤ 0.
- **Test:** chronological IS/OOS split. Thresholds and trade geometry are
  selected only on IS; expectancy — with a bootstrap confidence interval — is
  measured only once on OOS.
- **Pass criterion (pre-registered):** OOS bootstrap CI lower bound > 0 **and**
  OOS expectancy ≥ 50 % of the IS expectancy.

### H2 — The edge is Korea-specific

Among candidates that pass H1's filters, expectancy is **conditional on the
kimchi-premium state** in a pre-registered direction — higher when the premium
behaves as the mechanism predicts than when it is flat or blind to it.

- **H0:** expectancy(kimchi-conditioned) = expectancy(kimchi-blind) — i.e. the
  premium carries no information.
- **Test:** a **paired contrast**. All other parameters are held fixed at their
  H1-calibrated values; only the kimchi filter is toggled on/off on the *same*
  candidate pool, so the comparison isolates the premium from confounding
  parameter combinations.
- **Constraint:** the kimchi range itself is fixed on IS and confirmed on OOS —
  never grid-searched and reported on the same sample. Folding kimchi into the
  main grid would *not* test H2: the optimizer could silently discard it, and we
  would learn nothing about attribution.

> If H1 holds but H2 fails, the strategy may still be tradeable — but the
> "Korean" story is decorative, and it is really just generic post-pump
> reversion.

---

## Mechanism

Why the overshoot forms and why it collapses are **two independent processes** —
this asymmetry is what the trade exploits.

- **Formation.** Korea's capital controls impede cross-border arbitrage. A local
  retail surge can push the Upbit price above the global price, and arbitrageurs
  cannot immediately flatten the gap.
- **Collapse.** The reversion is *not* driven by arbitrage arriving late. It is
  driven by exhaustion of the transient retail buying pressure that caused the
  spike. The overshoot fades on its own.

This is why the entry timing is **fixed to the Korean session**, not
grid-searched: the mechanism predicts a specific window, so optimizing the
hour freely would both risk overfitting to a spurious time and weaken the
mechanistic claim.

---

## Signals & parameters

**Event filters (what flags a Korean pump on an Upbit hourly candle):**

- **Volume** — event-hour traded value exceeds the cumulative value of the
  preceding hours (scored as `volume_points`).
- **Breakout** — event-hour high exceeds prior hours' highs by a margin (scored
  as `growth_points`).
- **Pump magnitude** — intra-hour move (open → high) above a base percentage
  (scored as `pump_points`).

**Trade geometry (calibrated on IS):** stop-loss %, take-profit capture
(fraction of the run-up from reference to entry).

**Fixed by mechanism (not grid-searched):** entry window (Korean session),
exit horizon.

**Kimchi premium** — `KRW / (USDT × KRW-USDT) − 1`, computed on-the-fly from
1-minute data and sampled as a pre-event baseline, at entry, at exit, and as a
timeline. Reserved exclusively for the H2 paired contrast.

---

## Validation protocol

- **Data:** frozen 1-minute Parquet datasets (Binance USDT-M perps + funding,
  Upbit KRW markets, Upbit KRW-USDT), fetched once. No live API calls during
  backtesting.
- **IS/OOS:** chronological split. IS = 2026-01-01 → 2026-06-01; OOS =
  2025-01-01 → 2026-01-01. OOS untouched until a single final run per config.
- **No hardcoded parameters:** filter thresholds and trade geometry are
  grid-searched on IS only.
- **Point-in-time execution:** fills on the next bar; no look-ahead. Universe
  includes tokens that later delisted (survivorship-aware).
- **Minimum trade gate:** a config is eligible only above a minimum IS trade
  count, to prevent thin-sample overfitting.
- **Selection metric:** bootstrap CI *lower bound* on expectancy — not the point
  estimate.
- **Plateau requirement:** the selected config must sit on a stable plateau in
  the grid (neighbours by ±1 grid step do not collapse), not an isolated peak.
- **Position-cap handling:** a maximum-concurrent-positions cap is deliberately
  **excluded** from the grid. A cap selects trades by their order within a day,
  not by signal quality, which biases the sample and confounds the edge test. A
  shuffle test (re-ordering same-day trades) confirmed that any cap-dependent
  result would be an artifact of historical sequence, not a property of the
  edge. A cap belongs to risk management, applied afterward — not to the
  hypothesis test.

**Cost model:** taker fee 0.04 %/side, real funding over the holding period
(`(entry, exit]`, scaled by mark price), slippage 0.05 % on entry and forced
time-exit. Same-bar stop+take tie resolves to the stop (conservative).

---

## Results

### H1 — tested on two independent configurations

H1 was evaluated on **two entry regimes** derived from the same thesis. The
result is reported per-configuration, because they behaved differently — and the
divergence is itself the point.

#### Daily short — **PASS**

Entry at the Korean session open, held to the next day's session close.
Selected config: `volume_points ≥ 40, growth_points ≥ 15, pump_points ≥ 5,
SL 13 %, TP 90 %-capture`.

| Metric | In-sample (2026) | Out-of-sample (2025) |
|---|---|---|
| Trades | 181 | 171 |
| Expectancy / trade | +26.79 USDT (+2.68 %) | +16.42 USDT (+1.64 %) |
| Bootstrap CI (95 %) lower | +15.63 | **+5.20** |
| Win rate | 76.8 % | 71.9 % |
| Profit factor | 2.36 | 1.72 |
| Max drawdown (realized) | 3.0 % | 5.6 % |

Both pre-registered criteria met on OOS: CI lower bound +5.20 > 0, and OOS
expectancy (+16.42) exceeds 50 % of IS expectancy (+13.39 threshold). The IS→OOS
decay of −39 % is a normal magnitude; the edge remained statistically
significant with margin.

#### Night short — **FAIL**

Entry in a short post-midnight-UTC window, closed same session. Selected config:
`volume_points ≥ 5, growth_points ≥ 3, SL 8 %, TP 90 %-capture`.

| Metric | In-sample (2026) | Out-of-sample (2025) |
|---|---|---|
| Trades | 704 | 992 |
| Expectancy / trade | +3.47 USDT (+0.35 %) | +2.19 USDT |
| Bootstrap CI (95 %) lower | +0.58 | **−0.27** |

The night edge was flagged as weak already on IS — no configuration in the top
20 formed a plateau (every neighbour dropped the CI lower bound below zero). On
OOS the expectancy stayed mildly positive but the bootstrap CI **crossed zero**:
the edge is statistically indistinguishable from noise. Per protocol, this
configuration is archived, not carried forward.

### Interpretation

The split outcome **confirms the protocol works**, rather than indicating a flaw
in it. A signal that never formed a plateau on IS and sat on the edge of
significance failed OOS exactly as predicted — it did not "break into the
negative", it simply revealed itself to be what it looked like: noise with a
slight positive drift, insufficient to trade. A weak signal being caught and
discarded by a pre-registered filter is the filter doing its job.

**Honest caveats on the daily PASS:**

- OOS CAGR (~40 %) is far below IS CAGR (~318 %). This is not only expectancy
  decay: 2026 was unusually rich in qualifying events (181 trades in 5 months vs
  171 in 12 months). **Signal density is non-stationary.**
- `volume_points ≥ 40` sits at the **edge of the grid** — the true optimum may
  lie beyond it and was not probed. This is a candidate for a follow-up IS grid
  extension, not something the current OOS validated.

**Status: H1 confirmed for the daily configuration; rejected for the night
configuration.** Proceeding to H2 (kimchi attribution) on the daily
configuration only.

---

## Reproducibility

- Grid search & metrics: `grid_day.csv`, `grid_night.csv`,
  `top20_day.csv`, `top20_night.csv`
- Frozen chosen configs & their trades: `chosen_trades_day.csv`,
  `chosen_trades_night.csv`
- OOS run: `stage_oos.py`, `oos_trades_day.csv`, `oos_trades_night.csv`
- Engine verification against reference (1:1 price/exit match on shared trades):
  `legacy_dayshort_diff.py`, `legacy_diff_matched.csv`

---

## Next step — H2

Take the frozen daily H1 configuration, hold every parameter fixed, and run the
kimchi paired contrast (premium-conditioned vs premium-blind entries on the same
candidate pool). The kimchi range is fixed on IS and confirmed once on OOS.