"""Stage C: shuffle robustness of the chosen configs. For each strategy's chosen trades,
shuffle the within-day order 500 times, each time apply a hypothetical cap of 3 entries/day
(first 3 in shuffled order), recompute realized MDD and CAGR on the capped subset
(real entry/exit times kept — the shuffle only decides WHICH trades a capped runner takes).
"""

import math

import numpy as np
import pandas as pd

NOTIONAL = 1000.0
IS_DAYS = 151
N_SHUFFLES = 500
CAP = 3


def mdd_cagr(trades):
    if len(trades) == 0:
        return np.nan, np.nan
    entry = trades["entry_ms"].values
    exit_ = trades["exit_ms"].values
    net = trades["net"].values
    ev = sorted([(t, 1) for t in entry] + [(t, -1) for t in exit_])
    cur = peak = 0
    for _, d in ev:
        cur += d
        peak = max(peak, cur)
    base = peak * NOTIONAL
    order = np.argsort(exit_)
    eq = base + np.cumsum(net[order])
    run_peak = np.maximum.accumulate(np.concatenate([[base], eq]))[1:]
    mdd = float(((run_peak - eq) / run_peak).max() * 100)
    cagr = ((1 + net.sum() / base) ** (365 / IS_DAYS) - 1) * 100
    return mdd, cagr


def run(name):
    tr = pd.read_csv(f"results/chosen_trades_{name}.csv")
    tr["day"] = pd.to_datetime(tr["entry_ms"], unit="ms").dt.date
    per_day = tr.groupby("day").size()
    mdd0, cagr0 = mdd_cagr(tr)
    print(f"\n===== {name.upper()} =====")
    print(f"chosen-config trades: {len(tr)} over {per_day.index.nunique()} active days; "
          f"days with >{CAP} entries: {(per_day > CAP).sum()} "
          f"(max {per_day.max()}/day, mean {per_day.mean():.2f})")
    print(f"uncapped baseline: MDD {mdd0:.2f}%, CAGR {cagr0:.1f}%")

    rng = np.random.default_rng(7)
    mdds, cagrs = [], []
    for _ in range(N_SHUFFLES):
        picks = []
        for _, g in tr.groupby("day"):
            idx = rng.permutation(len(g))[:CAP]
            picks.append(g.iloc[idx])
        sel = pd.concat(picks)
        m, c = mdd_cagr(sel)
        mdds.append(m)
        cagrs.append(c)
    mdds, cagrs = np.array(mdds), np.array(cagrs)
    print(f"cap={CAP} shuffled x{N_SHUFFLES}:")
    print(f"  MDD  %: median {np.median(mdds):.2f}  p5 {np.percentile(mdds,5):.2f}  p95 {np.percentile(mdds,95):.2f}"
          f"  (spread p95/p5 = {np.percentile(mdds,95)/max(np.percentile(mdds,5),1e-9):.2f}x)")
    print(f"  CAGR %: median {np.median(cagrs):.1f}  p5 {np.percentile(cagrs,5):.1f}  p95 {np.percentile(cagrs,95):.1f}")
    return dict(name=name, mdd0=mdd0, cagr0=cagr0, mdds=mdds, cagrs=cagrs)


if __name__ == "__main__":
    run("night")
    run("day")
