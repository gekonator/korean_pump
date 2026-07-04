"""EXPLORATORY — NOT validation. 2025 was already consumed as H1 OOS; results here are
directional estimates only and must not be claimed as validated.

Decompose the difference between the frozen day config and the original baseline
configuration on 2025-01-01 -> 2026-01-01 (full year), fixed base 10,000 USDT,
flat 1000/trade, realized-only equity, full cost model (fee 0.04%/side, funding
(entry,exit] x mark, slippage 0.05% on entry/time-exit fills).

V1  frozen config:            vp>=40 gp>=15 pp>=5, entry fixed 04:00 UTC (Upbit growth>=3%
                              vs Upbit open 00:00), SL 13%, TP 90%, exit D+1 14:00, no kimchi, no cap.
V2  baseline thresholds only: vp>=14 gp>=10 pp>=4, everything else identical to V1.
V3  baseline full config:     vp>=14 gp>=10 pp>=4, entry window 04:00-06:00 first minute with
                              Binance growth>=3% (ref = Binance open 04:00) AND kimchi drop from
                              window max (tracked from 04:00) >= 1.7 p.p., fill next minute,
                              cap=4/day (priority gp -> vp -> pp), SL 13%, TP 90%, exit D+1 14:00.

(1)vs(2) = pure threshold effect. (2)vs(3) = kimchi-gate+cap effect on soft thresholds
(an additional independent look at H2 on the baseline thresholds). Differences use day-cluster
bootstrap (resample calendar days, 1000x) since V3 geometry differs from V2 trade-by-trade.
"""

import numpy as np
import pandas as pd

import engine
from engine import day_points, simulate_short

P = "data/parquet"
START = pd.Timestamp("2025-01-01")
END = pd.Timestamp("2026-01-01")
PERIOD_DAYS = 365
FIXED_BASE = 10_000.0
FEE_SIDE = 0.0004
SLIP = 0.0005
NOTIONAL = 1000.0
HOUR_MS = 3_600_000
MIN_MS = 60_000
STALE_MS = 60 * MIN_MS

KRW = engine.sql(f"SELECT timestamp_utc, close FROM read_parquet('{P}/upbit_krw_usdt_1m.parquet') ORDER BY timestamp_utc")
K_TS = KRW["timestamp_utc"].values.astype("datetime64[ms]").astype(np.int64)
K_CL = KRW["close"].values


def asof(ts, vals, t):
    i = np.searchsorted(ts, t, side="right") - 1
    if i < 0 or t - ts[i] > STALE_MS:
        return None
    return vals[i]


def build_pools():
    universe = engine.sql(f"SELECT token FROM read_parquet('{P}/universe.parquet') ORDER BY token")["token"].tolist()
    hourly = engine.sql(f"""
        SELECT token, date_trunc('hour', timestamp_utc) AS hour,
               first(open ORDER BY timestamp_utc) AS open, max(high) AS high, sum(volume) AS volume
        FROM read_parquet('{P}/upbit_1m/*/*.parquet', hive_partitioning=1)
        WHERE timestamp_utc >= TIMESTAMP '{START}' AND timestamp_utc < TIMESTAMP '{END}'
        GROUP BY token, hour
    """)
    hourly["hkey"] = hourly["hour"].values.astype("datetime64[h]").astype(np.int64)
    hourly_by_token = dict(tuple(hourly.groupby("token")))
    funding_df = engine.sql(f"SELECT token, funding_time_utc, funding_rate FROM read_parquet('{P}/binance_funding.parquet') ORDER BY token, funding_time_utc")
    funding = {t: (g["funding_time_utc"].values.astype("datetime64[ms]").astype(np.int64), g["funding_rate"].values)
               for t, g in funding_df.groupby("token")}

    days = pd.date_range(START, END - pd.Timedelta(days=1), freq="1D")
    day_ms00 = days.values.astype("datetime64[ms]").astype(np.int64)
    day_hkeys = days.values.astype("datetime64[h]").astype(np.int64)
    data_end_ms = int(END.value // 1_000_000)

    fixed_raw = []   # candidates for V1/V2 (fixed 04:00 entry), tagged with points
    baseline_raw = []  # candidates for V3 (baseline window entry with kimchi drop)

    for token in universe:
        hdf = hourly_by_token.get(token)
        if hdf is None:
            continue
        hmap = {int(k): (o, h, v) for k, o, h, v in zip(hdf["hkey"], hdf["open"], hdf["high"], hdf["volume"])}
        f_times, f_rates = funding.get(token, (np.array([], dtype=np.int64), np.array([])))
        b = engine.sql(f"""
            SELECT timestamp_utc, open, high, low, close FROM read_parquet('{P}/binance_1m/token={token}/*.parquet')
            WHERE timestamp_utc >= TIMESTAMP '{START}' AND timestamp_utc < TIMESTAMP '{END}'
            ORDER BY timestamp_utc
        """)
        if b.empty:
            continue
        bts = b["timestamp_utc"].values.astype("datetime64[ms]").astype(np.int64)
        bo, bh, bl, bc = b["open"].values, b["high"].values, b["low"].values, b["close"].values
        u = engine.sql(f"""
            SELECT timestamp_utc, open, close FROM read_parquet('{P}/upbit_1m/token={token}/*.parquet')
            WHERE timestamp_utc >= TIMESTAMP '{START}' AND timestamp_utc < TIMESTAMP '{END}'
              AND hour(timestamp_utc) IN (3, 4, 5)
            ORDER BY timestamp_utc
        """)
        uts = u["timestamp_utc"].values.astype("datetime64[ms]").astype(np.int64)
        uo, ucl = u["open"].values, u["close"].values

        for di in range(len(days)):
            t00 = int(day_ms00[di])
            hk = int(day_hkeys[di])
            pts = day_points(hmap, hk)
            if pts is None:
                continue
            vp, gp, pp = pts
            if not (vp >= 14 and gp >= 10 and pp >= 4):  # union filter (V2/V3 superset; V1 masked later)
                continue
            deadline = t00 + 38 * HOUR_MS
            if deadline > data_end_ms:
                continue
            t04 = t00 + 4 * HOUR_MS

            # ---- V1/V2 candidate: fixed 04:00 entry, Upbit growth gate ----
            upbit_ref = hmap[hk][0]
            if upbit_ref > 0:
                iu = np.searchsorted(uts, t04)
                if iu < len(uts) and uts[iu] == t04:
                    p04 = uo[iu]
                else:
                    ja = np.searchsorted(uts, t00 + 3 * HOUR_MS)
                    jb = np.searchsorted(uts, t04)
                    p04 = ucl[jb - 1] if jb > ja else None
                if p04 is not None and p04 / upbit_ref - 1 >= 0.03:
                    fi = np.searchsorted(bts, t04)
                    if fi < len(bts) and bts[fi] == t04:
                        raw_open = bo[fi]
                        entry_px = raw_open * (1 - SLIP)
                        ref_imp = raw_open / (p04 / upbit_ref)
                        tr = simulate_short(bts, bo, bh, bl, bc, fi, deadline, entry_px,
                                 entry_px * 1.13, entry_px - 0.90 * (entry_px - ref_imp),
                                 f_times, f_rates, int(t04))
                        if tr:
                            fixed_raw.append(dict(token=token, di=di, vp=vp, gp=gp, pp=pp, **tr))

            # ---- V3 candidate: window 04:00-06:00, Binance growth + kimchi drop ----
            fi04 = np.searchsorted(bts, t04)
            if fi04 >= len(bts) or bts[fi04] != t04:
                continue
            ref_b = bo[fi04]
            run_max = None
            signal_m = None
            for m in range(0, 120):
                t_m = t04 + m * MIN_MS
                uu = asof(uts, ucl, t_m)
                bb = asof(bts, bc, t_m)
                kk = asof(K_TS, K_CL, t_m)
                if uu is None or bb is None or kk is None or bb <= 0 or kk <= 0:
                    continue
                prem = uu / (bb * kk) - 1
                run_max = prem if run_max is None else max(run_max, prem)
                growth = bb / ref_b - 1
                if growth >= 0.03 and (run_max - prem) * 100 >= 1.7:
                    signal_m = m
                    break
            if signal_m is None:
                continue
            fill_ms = t04 + (signal_m + 1) * MIN_MS
            fj = np.searchsorted(bts, fill_ms)
            if fj >= len(bts) or bts[fj] > fill_ms + 5 * MIN_MS:
                continue
            entry_ms = int(bts[fj])
            entry_px = bo[fj] * (1 - SLIP)
            tr = simulate_short(bts, bo, bh, bl, bc, fj, deadline, entry_px,
                     entry_px * 1.13, entry_px - 0.90 * (entry_px - ref_b),
                     f_times, f_rates, entry_ms)
            if tr:
                baseline_raw.append(dict(token=token, di=di, vp=vp, gp=gp, pp=pp, **tr))

    return days, fixed_raw, baseline_raw


def apply_busy(raw):
    out = []
    open_until = {}
    for r in sorted(raw, key=lambda x: (x["token"], x["entry_ms"])):
        if r["entry_ms"] < open_until.get(r["token"], -1):
            continue
        open_until[r["token"]] = r["exit_ms"]
        out.append(r)
    return out


def metrics(trades, name, seed):
    df = pd.DataFrame(trades)
    net = df.net.values
    n = len(net)
    rng = np.random.default_rng(seed)
    boot = net[rng.integers(0, n, (1000, n))].mean(axis=1)
    ci_lo, ci_hi = np.percentile(boot, [2.5, 97.5])
    wins, losses = net[net > 0], net[net <= 0]
    pf = wins.sum() / abs(losses.sum()) if len(losses) and losses.sum() != 0 else np.inf
    ev = sorted([(t, 1) for t in df.entry_ms] + [(t, -1) for t in df.exit_ms])
    cur = peak = 0
    for _, d in ev:
        cur += d
        peak = max(peak, cur)
    order = np.argsort(df.exit_ms.values)
    eq = FIXED_BASE + np.cumsum(net[order])
    run_peak = np.maximum.accumulate(np.concatenate([[FIXED_BASE], eq]))[1:]
    mdd = float(((run_peak - eq) / run_peak).max() * 100)
    ann = net.sum() / FIXED_BASE * (365 / PERIOD_DAYS) * 100
    return dict(variant=name, n_trades=n, total_net=round(net.sum(), 2),
                expectancy=round(net.mean(), 2), ci_low=round(ci_lo, 2), ci_high=round(ci_hi, 2),
                win_rate=round((net > 0).mean() * 100, 1), profit_factor=round(pf, 2),
                ann_return_pct=round(ann, 2), mdd_pct=round(mdd, 2),
                calmar=round(ann / mdd, 2) if mdd > 0 else np.inf, peak_concurrent=peak)


def day_cluster_diff(trades_a, trades_b, seed):
    """day-cluster bootstrap CI of expectancy(A) - expectancy(B)."""
    da = pd.DataFrame(trades_a).groupby("di")["net"].agg(list)
    db = pd.DataFrame(trades_b).groupby("di")["net"].agg(list)
    all_days = sorted(set(da.index) | set(db.index))
    rng = np.random.default_rng(seed)
    diffs = []
    for _ in range(1000):
        pick = rng.choice(all_days, len(all_days), replace=True)
        na = [x for d in pick for x in da.get(d, [])]
        nb = [x for d in pick for x in db.get(d, [])]
        if not na or not nb:
            diffs.append(np.nan)
            continue
        diffs.append(np.mean(na) - np.mean(nb))
    return float(np.nanpercentile(diffs, 2.5)), float(np.nanpercentile(diffs, 97.5))


def main():
    print("EXPLORATORY - NOT validation (2025 already consumed as H1 OOS)\n", flush=True)
    days, fixed_raw, baseline_raw = build_pools()

    v1 = apply_busy([r for r in fixed_raw if r["vp"] >= 40 and r["gp"] >= 15 and r["pp"] >= 5])
    v2 = apply_busy(fixed_raw)
    # V3: cap=4/day by priority gp -> vp -> pp, then busy rule
    v3_pool = []
    for di, g in pd.DataFrame(baseline_raw).groupby("di"):
        g = g.sort_values(["gp", "vp", "pp"], ascending=False)
        v3_pool.extend(g.head(4).to_dict("records"))
    v3 = apply_busy(v3_pool)

    rows = [metrics(v1, "V1 frozen config (40/15/5, fixed 04:00)", 11),
            metrics(v2, "V2 baseline thresholds no kimchi (14/10/4)", 12),
            metrics(v3, "V3 baseline full (14/10/4 + kimchi drop 1.7 + cap4)", 13)]
    res = pd.DataFrame(rows)
    res.insert(0, "note", "EXPLORATORY_not_validation_2025_spent_on_H1_OOS")
    res.to_csv("results/exploratory_baseline.csv", index=False)
    pd.set_option("display.width", 220)
    print(res.drop(columns=["note"]).to_string(index=False))

    e1 = np.mean([t["net"] for t in v1])
    e2 = np.mean([t["net"] for t in v2])
    e3 = np.mean([t["net"] for t in v3])
    lo12, hi12 = day_cluster_diff(v1, v2, 21)
    lo32, hi32 = day_cluster_diff(v3, v2, 22)
    print(f"\n(1)-(2) threshold effect:      {e1-e2:+.2f} USDT/trade, day-cluster bootstrap CI [{lo12:+.2f}, {hi12:+.2f}]")
    print(f"(3)-(2) kimchi+cap effect:     {e3-e2:+.2f} USDT/trade, day-cluster bootstrap CI [{lo32:+.2f}, {hi32:+.2f}]")
    print("\nEXPLORATORY - directional estimates only.")


if __name__ == "__main__":
    main()
