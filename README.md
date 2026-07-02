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
configuration.** H2 was then tested on the daily configuration (below).

### H2 — Korea-specificity — **REJECTED**

With the daily H1 configuration frozen, the kimchi premium was tested for
attribution. The Δkimchi band was calibrated on IS **by the shape** of the
expectancy–vs–premium relationship (where expectancy crosses zero), not by
maximizing PnL, and then frozen for a single OOS paired contrast.

**IS shape (2026)** — expectancy by Δkimchi bin, bins fixed before inspecting
PnL:

| Δkimchi (p.p.) | N | Expectancy | Bootstrap CI |
|---|---|---|---|
| < 0 | 27 | +23.7 | [+1.9, +49.5] |
| [0, 0.5) | 54 | +12.6 | [−6.5, +30.1] |
| [0.5, 1) | 36 | +29.9 | [+8.7, +50.8] |
| [1, 1.5) | 21 | +49.2 | [+26.9, +69.4] |
| [1.5, 2) | 9 | +75.2 | [+46.7, +107.6] |
| [2, 2.5) | 8 | +37.2 | [−52.6, +125.3] |
| [2.5, 3) | 7 | −0.8 | [−84.4, +81.4] |
| ≥ 3 | 18 | +26.5 | [−34.7, +91.0] |

The mechanistic prediction (inverted profile — moderate premium good, extreme
premium bad) is only *partially* visible: expectancy peaks at [1.5, 2) and win
rate degrades above 2 p.p. But two facts argue against a Korea-specific story:
the **Δkimchi < 0 bin is solidly positive** (trades profit even when the premium
*fell* into entry), and the extreme tail (≥ 3) stays positive. The shape-derived
band came out as `(−∞, 2.5)` — it only excludes the single failing bin.

**OOS paired contrast (2025), one run:**

| | Blind | Conditioned |
|---|---|---|
| N | 171 | 156 (band removed 15) |
| Expectancy | +16.42 | +17.93 |
| CI | [+4.8, +27.0] | [+7.0, +29.4] |
| Win rate / PF | 71.9 % / 1.72 | 74.4 % / 1.81 |

Difference (conditioned − blind): **+1.51 USDT/trade, paired 95 % CI
[−2.19, +5.07].** Pre-registered criterion required the CI lower bound > 0 — it
is not. **H0 not rejected.**

**Interpretation.** The kimchi gate produces no statistically distinguishable
improvement. Combined with the positive Δkimchi < 0 bin on IS, the evidence
points to the edge being **generic post-pump mean reversion** — reversion driven
by the Upbit pump event itself (detected via volume/breakout/magnitude on Upbit
activity), *not* by the cross-exchange price premium. Binance is the execution
venue for the short; the predictive signal lives entirely on the Upbit side.

Two caveats, so the conclusion is not overstated:

- The shape-derived band was weakly discriminating (removed only 15/171 trades),
  so the two branches overlap heavily and the contrast had little room to
  separate. This follows from the honest "by shape" rule, not a protocol defect.
- *Reference only, not pre-registered, does not change the verdict:* the original
  author's band [0.2, 1.7) gives diff +10.96, CI [−1.11, +23.37] on N = 71 — a
  signal on the edge of significance. A moderate-premium zone *may* carry
  information, but it could not be proven on this data volume. Registered as a
  hypothesis for a future fresh period (e.g. IS 2026-H2), **not** re-tested on
  this sample.

---

## Reproducibility

- Grid search & metrics: `grid_day.csv`, `grid_night.csv`,
  `top20_day.csv`, `top20_night.csv`
- Frozen chosen configs & their trades: `chosen_trades_day.csv`,
  `chosen_trades_night.csv`
- OOS run: `stage_oos.py`, `oos_trades_day.csv`, `oos_trades_night.csv`
- Engine verification against reference (1:1 price/exit match on shared trades):
  `legacy_dayshort_diff.py`, `legacy_diff_matched.csv`
- H2 kimchi contrast: `stage_h2.py`, `results/h2_kimchi_bins_is.csv`,
  `results/h2_paired_oos.csv`

---

## Summary of findings

| Hypothesis | Claim | Verdict |
|---|---|---|
| **H1** (daily) | Qualifying Upbit pumps revert profitably, net of costs, OOS | **Confirmed** — OOS expectancy +16.42, CI [+5.20, +27.77] |
| **H1** (night) | Same, for the post-midnight regime | **Rejected** — OOS CI crosses zero |
| **H2** | The edge is Korea-specific (kimchi premium carries information) | **Rejected** — kimchi gate gives no distinguishable improvement |

The strategy captures a real, cost-surviving, out-of-sample edge in shorting
post-pump altcoins — but the "Korean premium" framing is **not** what drives it.
The mechanism is generic post-pump mean reversion, signalled by Upbit pump
activity, with Binance as the execution venue.

## Next steps

These separate *research validity* (done) from *trading readiness* (not done):

- **Trading-readiness gaps, before any real capital:** slippage stress (current
  0.05 % is optimistic for illiquid post-pump alts — find where the edge dies),
  Monte-Carlo on the trade distribution for drawdown / risk-of-ruin, capacity
  estimation, and forward paper-trading to reconcile assumed vs real fills.
- **Non-stationarity:** signal density differed sharply between years; forward
  frequency and return are uncertain.
- **Open registered hypothesis:** the moderate-kimchi zone (author's prior band),
  to be tested only on a fresh future period — never re-run on this data.