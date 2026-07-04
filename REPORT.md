# Report: Day Short Strategy on Upbit Pumps (korean_pump)

Date: 2026-07-02. All times UTC. Data: own frozen 1m dataset
(Upbit spot + Binance USDT-M perp + funding), 2025-01-01 → 2026-06-01,
245 pairs, `data/parquet/`.

---

## 1. Strategy (frozen configuration)

Short a pump on the Binance perp after anomalous volume/growth is detected on Upbit.

| Parameter | Value |
|---|---|
| Candidate candle | Upbit hourly 00:00–01:00 UTC |
| volume_points | ≥ 40 (lookback 50 h) |
| growth_points | ≥ 15 (lookback 50 h) |
| pump_points | ≥ 5 |
| Entry condition | Upbit price growth from open 00:00 to 04:00 ≥ 3% |
| Entry | short Binance perp, market at 04:00 UTC open |
| Stop-loss | 13% from entry price |
| Take-profit | limit, 90%-capture of the move (entry − implied ref) |
| Time-exit | D+1 14:00 UTC |
| Kimchi filter | **none** (H2 rejected, see §4) |
| Position cap | none; one open position per token at a time |
| Sizing | flat 1,000 USDT notional per trade, no compounding |
| Cost model | fee 0.04%/side; funding (entry, exit] × mark price; slippage 0.05% on market fills |
| Capital base | 10,000 USDT (fixed); peak exposure 7,000 → no leverage required |

## 2. Validation protocol and results


1. **Grid search strictly on IS 2026-01→2026-06** (1,600 configurations),
   selection metric — lower bound of the bootstrap CI of expectancy; the
   winner was chosen by plateau, not peak (ci_low 15.63, neighbors ≥13.7).
   2025 was not used in the selection.
2. **OOS 2025 (full year), single run, criteria pre-registered before the run:**
   CI_low > 0 and expectancy ≥ 50% of IS. **PASS**: expectancy +16.42 vs
   threshold +13.39; CI [+5.2, +27.8]. (The night-strategy candidate failed
   the criterion as expected and was discarded — the protocol filtered out noise.)
3. **H2 (kimchi specificity): FAIL** — the paired contrast of the kimchi gate
   on OOS is not significant (+1.51, CI [−2.19, +5.07]); moreover, trades with
   a falling kimchi premium were profitable. Conclusion: the edge is generic
   pump-reversal; no kimchi filter is included in the configuration.
4. **Shuffle robustness**: the cap almost never binds (7 of 110 days with >3
   entries); dropping the cap is methodologically confirmed.


## 3. Metrics (base 10,000 USDT, realized-only equity)

| | IS 2026 (151 d) | **OOS 2025 (365 d)** |
|---|---|---|
| Trades | 181 | 171 |
| Expectancy | +26.79 USDT (2.68%) | **+16.42 USDT (1.64%)** |
| Bootstrap CI | [+15.4, +39.1] | [+5.2, +27.8] |
| Win rate / PF | 76.8% / 2.36 | 71.9% / 1.72 |
| Exits TP/SL/TIME | 88/21/72 | 91/23/57 |
| Ann. return (simple annualized, no compounding) | +117.2% | **+28.1%** |
| MDD realized | 2.08% (288 USDT) | 3.91% (410 USDT) |
| Calmar / Sharpe / Sortino | 56.5 / 6.85 / 16.7 | 7.2 / 4.73 / 6.9 |
| Peak concurrent positions | 6 (0.6x of base) | 7 (0.7x) |

![Equity](results/fig_equity.png)

The honest forward-looking expectation is the OOS row (~28% annualized on 10k),
not IS: signal density is non-stationary (2026 produced 181 trades in 5 months
vs 171 for all of 2025).

## 4. Monte Carlo (10,000 bootstrap paths, OOS trades)

| | p5 | p50 | p95 |
|---|---|---|---|
| Annual PnL, USDT | +1,171 | +2,819 | +4,428 |
| Annual return | +11.7% | +28.2% | +44.3% |
| MDD | 2.6% | 4.5% | 8.4% (p99 = 11.1%) |

P(losing year) = **0.16%**; P(MDD>10%) = 2.0%.
Caveat: i.i.d. resampling destroys trade clustering — the tails are
indicative, not exact.

![Monte Carlo](results/fig_montecarlo.png)

## 5. Slippage stress (5×5×7 grid, entry/exit/stop stressed separately)

TP is not stressed (limit buy-back — fills at target or better);
market executions are stressed: entry, time-exit, and the **stop fill**
(the key negative tail — a stop firing while the pump keeps running on
an illiquid alt).

**Main result: the edge survives on OOS across the entire realistic zone.**

| Scenario (entry/exit, stop) | OOS expectancy | OOS CI_low | Verdict |
|---|---|---|---|
| Baseline (0.05/0.05, 0.05) | +16.34 | +4.59 | ✅ |
| Optimistic-realistic (0.1/0.1, 0.3) | +15.41 | +4.18 | ✅ |
| Mid-realistic (0.15/0.15, 0.5) | +14.36 | +2.23 | ✅ |
| Pessimistic-realistic (0.2/0.2, 0.75) | +13.42 | +1.68 | ✅ |
| Extreme corner (0.3/0.3, 1.0) | +11.93 | **−0.40** | ❌ |

Per-component survival thresholds (OOS, others held at 0.05%): neither
s_stop up to 1.0%, nor s_entry up to 0.3%, nor s_exit up to 0.3% pushes
CI_low below zero on its own. The only failing point of the whole grid is
all three components at their extremes simultaneously. Why the edge is
robust to stop slippage: SL is only 13% of trades, and a 1% slip on the
stop moves expectancy by only ~−1.4 USDT/trade.

**Answer to the key question: yes, the strategy survives realistic slippage
levels for post-pump alts (entry/exit 0.1–0.2%, stop 0.3–0.75%) with margin;
no red flag.** The margin is not infinite, however: at the
pessimistic-realistic point CI_low is only +1.68 — monitoring actual
slippage in live trading is mandatory.

![Slippage](results/fig_slippage.png)

## 6. Risks and limitations

- **Capacity/liquidity not modeled**: flat 1,000 USDT is a small-size
  calculation; the slippage stress only roughly approximates size scaling.
- **Realized-only equity**: floating drawdown of open positions
  (theoretically up to ~13% × positions) is invisible in MDD; a margin call
  is unreachable at ≤0.7x leverage.
- **vp≥40 is the edge of the IS grid**; higher values were not explored.
- **Regime non-stationarity**: signal density and pump profiles change year
  to year; MC and OOS partially reflect this but do not guarantee it.
- Delisting risk of the shorted alt and Upbit/Binance rule changes are
  outside the model.
- 2025 has been used twice (H1 OOS + exploratory) — any new configuration
  changes require a fresh OOS (2026-H2+).

## 7. Files

| Artifact | Path |
|---|---|
| Data | `data/parquet/` (+ `data/reference/trades.csv` — author's reference) |
| Engine/stages | `engine.py` (single source of truth), `stage_a_verify.py`, `stage_b_grid.py`, `stage_c_shuffle.py`, `stage_oos.py`, `stage_h2.py`, `make_parity_reference.py` |
| Metrics/stress | `metrics_fixed_base.py`, `monte_carlo.py`, `slippage_stress.py`, `exploratory_baseline.py` |
| Trades | `results/chosen_trades_day.csv` (IS), `results/oos_trades_day.csv` (OOS) |
| Grids/validation | `results/grid_day.csv`, `results/top20_day.csv`, `results/h2_*.csv` |
| Stress/MC | `results/slippage_stress.csv`, `results/monte_carlo.csv`, `results/metrics_fixed_base.csv` |
| Charts | `results/fig_equity.png`, `results/fig_slippage.png`, `results/fig_montecarlo.png` |
