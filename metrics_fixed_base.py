"""Recompute return/risk metrics of the chosen configs on a FIXED capital base.

Fixed base: deposit 10,000 USDT set in advance (not derived from history), flat 1000 USDT
notional per trade, no compounding, no slot limit (leverage covers exposure; liquidation
unreachable because SL 13% fires before liquidation at the leverage used).
Equity is realized-only: equity(t) = 10000 + cumulative net PnL of closed trades by exit time.

Annualized return here = total_net / 10000 scaled to 365 days — a SIMPLE annualized return
on fixed capital WITHOUT compounding (deliberately not a compound-CAGR formula, since the
PnL stream itself is non-compounding flat-size).

Trades and H1/H2 verdicts are untouched — this is a metrics-only recompute. Old dynamic-base
(peak x 1000) numbers are recomputed alongside for comparison.
"""

import math

import numpy as np
import pandas as pd

FIXED_BASE = 10_000.0
NOTIONAL = 1000.0

SETS = [
    ("day", "IS 2026 (151d)", "results/chosen_trades_day.csv", 151),
    ("day", "OOS 2025 (365d)", "results/oos_trades_day.csv", 365),
    ("night", "IS 2026 (151d)", "results/chosen_trades_night.csv", 151),
    ("night", "OOS 2025 (365d)", "results/oos_trades_night.csv", 365),
]


def peak_concurrent(entry, exit_):
    ev = sorted([(t, 1) for t in entry] + [(t, -1) for t in exit_])
    cur = peak = 0
    for _, d in ev:
        cur += d
        peak = max(peak, cur)
    return peak


def curve_stats(net_sorted_by_exit, base):
    eq = base + np.cumsum(net_sorted_by_exit)
    run_peak = np.maximum.accumulate(np.concatenate([[base], eq]))[1:]
    mdd_pct = float(((run_peak - eq) / run_peak).max() * 100)
    mdd_usdt = float((run_peak - eq).max())
    return mdd_pct, mdd_usdt


def sharpe_sortino(net, exit_, base):
    daily = pd.Series(net, index=pd.to_datetime(exit_, unit="ms").date).groupby(level=0).sum()
    r = daily.values / base
    if len(r) < 2 or r.std() == 0:
        return np.nan, np.nan
    sharpe = r.mean() / r.std() * math.sqrt(365)
    dn = r[r < 0]
    sortino = r.mean() / dn.std() * math.sqrt(365) if len(dn) > 1 and dn.std() > 0 else np.inf
    return sharpe, sortino


rows = []
for strat, period, path, days in SETS:
    tr = pd.read_csv(path)
    net = tr["net"].values
    entry = tr["entry_ms"].values
    exit_ = tr["exit_ms"].values
    order = np.argsort(exit_)
    net_o = net[order]
    total = net.sum()
    peak = peak_concurrent(entry, exit_)

    # old dynamic base (peak x 1000, compound formula) — for comparison
    old_base = peak * NOTIONAL
    old_mdd_pct, _ = curve_stats(net_o, old_base)
    old_cagr = ((1 + total / old_base) ** (365 / days) - 1) * 100
    old_sharpe, _ = sharpe_sortino(net, exit_, old_base)

    # new fixed base
    ann = total / FIXED_BASE * (365 / days) * 100  # simple annualized, no compounding
    mdd_pct, mdd_usdt = curve_stats(net_o, FIXED_BASE)
    calmar = ann / mdd_pct if mdd_pct > 0 else np.inf
    sharpe, sortino = sharpe_sortino(net, exit_, FIXED_BASE)

    rows.append(dict(
        strategy=strat, period=period, n_trades=len(tr), total_net=round(total, 2),
        peak_concurrent=peak, peak_exposure_usdt=peak * NOTIONAL,
        leverage_vs_10k=round(peak * NOTIONAL / FIXED_BASE, 2),
        ann_return_fixed_pct=round(ann, 2), mdd_fixed_pct=round(mdd_pct, 2),
        mdd_fixed_usdt=round(mdd_usdt, 2), calmar_fixed=round(calmar, 2),
        sharpe_fixed=round(sharpe, 2), sortino_fixed=round(sortino, 2) if np.isfinite(sortino) else np.inf,
        old_base_usdt=old_base, old_cagr_pct=round(old_cagr, 2),
        old_mdd_pct=round(old_mdd_pct, 2), old_sharpe=round(old_sharpe, 2),
    ))

df = pd.DataFrame(rows)
df.to_csv("results/metrics_fixed_base.csv", index=False)

pd.set_option("display.width", 220)
print("=== Fixed base 10,000 USDT, flat 1000/trade, no compounding, realized-only equity ===\n")
show = df[["strategy", "period", "n_trades", "total_net", "peak_concurrent", "leverage_vs_10k",
           "ann_return_fixed_pct", "mdd_fixed_pct", "mdd_fixed_usdt", "calmar_fixed",
           "sharpe_fixed", "sortino_fixed"]]
print(show.to_string(index=False))
print("\n=== Comparison: old dynamic base (peak x 1000, compound CAGR) vs new fixed 10k ===\n")
cmp = df[["strategy", "period", "old_base_usdt", "old_cagr_pct", "ann_return_fixed_pct",
          "old_mdd_pct", "mdd_fixed_pct", "old_sharpe", "sharpe_fixed"]]
cmp.columns = ["strategy", "period", "old_base", "old_CAGR%", "new_annret%", "old_MDD%", "new_MDD%",
               "old_Sharpe", "new_Sharpe"]
print(cmp.to_string(index=False))
