"""Monte Carlo simulation of the frozen day config on the fixed 10k base.

Method: bootstrap paths — resample N trades with replacement from the realized trade pool
(order randomized), rebuild the realized equity curve from 10,000 USDT, repeat 10,000 times.
Reported per period (IS 2026, OOS 2025): distribution of total PnL, annualized return,
max drawdown; probability of a losing period and of MDD exceeding thresholds.
This treats trades as i.i.d. — it destroys any clustering/serial dependence, so tails are
indicative, not exact.
"""

import numpy as np
import pandas as pd

FIXED_BASE = 10_000.0
N_PATHS = 10_000

SETS = [
    ("day IS 2026", "results/chosen_trades_day.csv", 151),
    ("day OOS 2025", "results/oos_trades_day.csv", 365),
]

rng = np.random.default_rng(2026)
summary = []
for name, path, days in SETS:
    net = pd.read_csv(path)["net"].values
    n = len(net)
    idx = rng.integers(0, n, (N_PATHS, n))
    paths = net[idx]                      # (paths, n) trade PnLs in random order
    totals = paths.sum(axis=1)
    eq = FIXED_BASE + np.cumsum(paths, axis=1)
    run_peak = np.maximum.accumulate(np.concatenate([np.full((N_PATHS, 1), FIXED_BASE), eq], axis=1), axis=1)[:, 1:]
    mdd = ((run_peak - eq) / run_peak).max(axis=1) * 100
    ann = totals / FIXED_BASE * (365 / days) * 100

    row = dict(
        set=name, n_trades=n, n_paths=N_PATHS,
        total_p5=np.percentile(totals, 5), total_p50=np.percentile(totals, 50),
        total_p95=np.percentile(totals, 95),
        ann_p5=np.percentile(ann, 5), ann_p50=np.percentile(ann, 50), ann_p95=np.percentile(ann, 95),
        mdd_p5=np.percentile(mdd, 5), mdd_p50=np.percentile(mdd, 50), mdd_p95=np.percentile(mdd, 95),
        mdd_p99=np.percentile(mdd, 99),
        p_loss=float((totals < 0).mean() * 100),
        p_mdd_gt5=float((mdd > 5).mean() * 100),
        p_mdd_gt10=float((mdd > 10).mean() * 100),
    )
    summary.append(row)
    np.save(f"results/mc_{'is' if 'IS' in name else 'oos'}_totals.npy", totals)
    np.save(f"results/mc_{'is' if 'IS' in name else 'oos'}_mdd.npy", mdd)

    print(f"\n===== {name} (N={n}, {N_PATHS} paths) =====")
    print(f"total PnL USDT   p5 {row['total_p5']:+8.0f}   p50 {row['total_p50']:+8.0f}   p95 {row['total_p95']:+8.0f}")
    print(f"ann.return %     p5 {row['ann_p5']:+8.1f}   p50 {row['ann_p50']:+8.1f}   p95 {row['ann_p95']:+8.1f}")
    print(f"MDD %            p5 {row['mdd_p5']:8.2f}   p50 {row['mdd_p50']:8.2f}   p95 {row['mdd_p95']:8.2f}   p99 {row['mdd_p99']:8.2f}")
    print(f"P(period loss) {row['p_loss']:.2f}%   P(MDD>5%) {row['p_mdd_gt5']:.1f}%   P(MDD>10%) {row['p_mdd_gt10']:.2f}%")

pd.DataFrame(summary).round(2).to_csv("results/monte_carlo.csv", index=False)
print("\nsaved results/monte_carlo.csv")
