"""H2: is the edge Korea-specific? H0: expectancy is the same with and without a kimchi gate.

The frozen H1 day config comes from engine.py and is not modified here; the kimchi
premium is measured on top of its trades.

Kimchi delta definition (fixed):
  premium(t) = upbit_close(t) / (binance_close(t) * krwusdt_close(t)) - 1, 1m data,
  each leg taken as-of strictly before t with a 60-minute staleness cap.
  baseline  = premium as-of the last minute strictly BEFORE 00:00 UTC of the event candle.
  at entry  = premium as-of the last minute strictly BEFORE 04:00 UTC (point-in-time).
  dkimchi   = (premium_entry - premium_baseline) * 100  [percentage points]

Stage 1 (IS 2026-01-01 -> 2026-06-01, hourly lookback buffer from 2025-12-27):
  bins fixed BEFORE looking at PnL: (-inf,0), [0,0.5), ..., [2.5,3), [3,inf) p.p.
  The band is derived from FORM (the contiguous run of positive-expectancy bins with
  the largest total N); the prior hypothesis band [0.2, 1.7] is printed for comparison.

Stage 2 (OOS 2025-01-01 -> 2026-01-01), pre-registered criterion:
  PASS iff (a) expectancy(cond) - expectancy(blind) > 0 AND
           (b) paired bootstrap (1000 resamples) 95% CI lower bound of the difference > 0.
  N(conditioned) < 40 -> flag as low-power regardless of sign.
"""

import time

import numpy as np
import pandas as pd

import engine
from engine import HOUR_MS, MIN_MS

STALE_MS = 60 * MIN_MS
BIN_EDGES = [-np.inf, 0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, np.inf]

KRW = engine.sql(f"SELECT timestamp_utc, close FROM read_parquet('{engine.PARQUET}/upbit_krw_usdt_1m.parquet') ORDER BY timestamp_utc")
K_TS = KRW["timestamp_utc"].values.astype("datetime64[ms]").astype(np.int64)
K_CL = KRW["close"].values


def asof_strict(ts, vals, t):
    """Last value strictly before t, None if absent or staler than STALE_MS."""
    i = np.searchsorted(ts, t) - 1
    if i < 0 or t - ts[i] > STALE_MS:
        return None
    return vals[i]


def add_dkimchi(trades, start, end):
    """Attach dkimchi to each trade of the frozen pool (does not affect the trades)."""
    out = trades.copy()
    out["dkimchi"] = np.nan
    for token, g in trades.groupby("token"):
        u = engine.sql(f"""
            SELECT timestamp_utc, close FROM read_parquet('{engine.PARQUET}/upbit_1m/token={token}/*.parquet')
            WHERE timestamp_utc >= TIMESTAMP '{start}' - INTERVAL 2 HOUR AND timestamp_utc < TIMESTAMP '{end}'
              AND hour(timestamp_utc) IN (23, 3)
            ORDER BY timestamp_utc
        """)
        uts = u["timestamp_utc"].values.astype("datetime64[ms]").astype(np.int64)
        ucl = u["close"].values
        b = engine.sql(f"""
            SELECT timestamp_utc, close FROM read_parquet('{engine.PARQUET}/binance_1m/token={token}/*.parquet')
            WHERE timestamp_utc >= TIMESTAMP '{start}' - INTERVAL 2 HOUR AND timestamp_utc < TIMESTAMP '{end}'
              AND hour(timestamp_utc) IN (23, 3)
            ORDER BY timestamp_utc
        """)
        bts = b["timestamp_utc"].values.astype("datetime64[ms]").astype(np.int64)
        bcl = b["close"].values

        def prem(t_ms):
            uu = asof_strict(uts, ucl, t_ms)
            bb = asof_strict(bts, bcl, t_ms)
            kk = asof_strict(K_TS, K_CL, t_ms)
            if uu is None or bb is None or kk is None or bb <= 0 or kk <= 0:
                return None
            return uu / (bb * kk) - 1

        for idx, r in g.iterrows():
            t04 = int(r.entry_ms)
            t00 = t04 - 4 * HOUR_MS
            p_base, p_entry = prem(t00), prem(t04)
            if p_base is not None and p_entry is not None:
                out.loc[idx, "dkimchi"] = (p_entry - p_base) * 100
    return out


def boot_ci(net, seed, n_boot=1000):
    n = len(net)
    if n == 0:
        return np.nan, np.nan
    rng = np.random.default_rng(seed)
    means = net[rng.integers(0, n, (n_boot, n))].mean(axis=1)
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def main():
    t0 = time.time()

    # ================= STAGE 1: IS form =================
    print("Stage 1: frozen day pool on IS 2026 (hourly buffer from 2025-12-27) ...", flush=True)
    is_tr = engine.day_signals("2026-01-01", "2026-06-01", hourly_start="2025-12-27")
    is_tr = add_dkimchi(is_tr, "2026-01-01", "2026-06-01")
    print(f"IS trades: {len(is_tr)} (dkimchi NaN: {int(is_tr.dkimchi.isna().sum())})  "
          f"total net {is_tr.net.sum():+.2f}  expectancy {is_tr.net.mean():+.2f}")

    lab = [f"[{BIN_EDGES[i]:g},{BIN_EDGES[i+1]:g})" for i in range(len(BIN_EDGES) - 1)]
    is_v = is_tr.dropna(subset=["dkimchi"]).copy()
    is_v["bin"] = pd.cut(is_v.dkimchi, BIN_EDGES, right=False, labels=lab)
    rows = []
    for i, L in enumerate(lab):
        g = is_v[is_v.bin == L]
        lo, hi = boot_ci(g.net.values, seed=500 + i)
        rows.append(dict(bin=L, left=BIN_EDGES[i], right=BIN_EDGES[i + 1], n=len(g),
                         expectancy=g.net.mean() if len(g) else np.nan, ci_low=lo, ci_high=hi,
                         win_rate=(g.net > 0).mean() * 100 if len(g) else np.nan))
    bins_df = pd.DataFrame(rows)
    bins_df.to_csv("results/h2_kimchi_bins_is.csv", index=False)
    print("\n--- IS dkimchi bins (fixed 0.5 p.p. grid) ---")
    print(bins_df.to_string(index=False))

    pos = (bins_df.expectancy > 0).values
    runs = []
    i = 0
    while i < len(pos):
        if pos[i] and bins_df.n.iloc[i] > 0:
            j = i
            while j + 1 < len(pos) and pos[j + 1] and bins_df.n.iloc[j + 1] > 0:
                j += 1
            runs.append((i, j, bins_df.n.iloc[i:j + 1].sum()))
            i = j + 1
        else:
            i += 1
    if not runs:
        print("\n!! No positive-expectancy region: mechanistic prediction NOT supported on IS.")
        return
    i0, j0, _ = max(runs, key=lambda r: r[2])
    band_lo, band_hi = bins_df.left.iloc[i0], bins_df.right.iloc[j0]
    print(f"\nBand from IS form: [{band_lo:g}, {band_hi:g}) p.p.   (prior hypothesis: [0.2, 1.7])")

    # ================= STAGE 2: OOS paired contrast =================
    print("\nStage 2: OOS 2025, single run ...", flush=True)
    oos = engine.day_signals("2025-01-01", "2026-01-01")
    oos = add_dkimchi(oos, "2025-01-01", "2026-01-01")
    oos["in_band"] = (oos.dkimchi >= band_lo) & (oos.dkimchi < band_hi)
    oos.to_csv("results/h2_paired_oos.csv", index=False)
    blind = oos
    cond = oos[oos.in_band & oos.dkimchi.notna()]

    e_b, e_c = blind.net.mean(), cond.net.mean()
    lo_b, hi_b = boot_ci(blind.net.values, seed=900)
    lo_c, hi_c = boot_ci(cond.net.values, seed=901)

    rng = np.random.default_rng(902)
    net = blind.net.values
    mask = blind.in_band.values & blind.dkimchi.notna().values
    n = len(net)
    diffs = []
    for _ in range(1000):
        idx = rng.integers(0, n, n)
        m = mask[idx]
        diffs.append(net[idx][m].mean() - net[idx].mean() if m.sum() else np.nan)
    d_lo, d_hi = np.nanpercentile(diffs, [2.5, 97.5])
    diff = e_c - e_b

    def pf(x):
        w, l = x[x > 0], x[x <= 0]
        return w.sum() / abs(l.sum()) if len(l) and l.sum() != 0 else np.inf

    print(f"\n{'':24}{'BLIND':>14}{'CONDITIONED':>14}")
    print(f"{'N trades':24}{len(blind):>14}{len(cond):>14}")
    print(f"{'expectancy USDT':24}{e_b:>+14.2f}{e_c:>+14.2f}")
    print(f"{'bootstrap CI':24}{f'[{lo_b:+.1f},{hi_b:+.1f}]':>14}{f'[{lo_c:+.1f},{hi_c:+.1f}]':>14}")
    print(f"{'win rate %':24}{(blind.net>0).mean()*100:>14.1f}{(cond.net>0).mean()*100:>14.1f}")
    print(f"{'profit factor':24}{pf(blind.net.values):>14.2f}{pf(cond.net.values):>14.2f}")
    print(f"\nband filtered out {len(blind)-len(cond)} of {len(blind)} blind trades")
    print(f"difference (cond - blind): {diff:+.2f} USDT/trade, paired bootstrap CI [{d_lo:+.2f}, {d_hi:+.2f}]")

    crit_a, crit_b = diff > 0, d_lo > 0
    print(f"\ncriterion (a) diff > 0:        {'PASS' if crit_a else 'FAIL'}")
    print(f"criterion (b) CI lower > 0:    {'PASS' if crit_b else 'FAIL'}")
    power_flag = " [LOW POWER: N(cond) < 40]" if len(cond) < 40 else ""
    print(f"H2 VERDICT: {'PASS' if (crit_a and crit_b) else 'FAIL'}{power_flag}")
    print(f"\nElapsed {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
