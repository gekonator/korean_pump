"""H2: is the edge Korea-specific? H0: expectancy is the same with and without a kimchi gate.

Frozen H1 day config (NOT touched here): vp>=40, gp>=15, pp>=5, SL 13%, TP 90%-capture,
entry 04:00 UTC, exit D+1 14:00 UTC, ref = Upbit hourly open 00:00, growth >= 3% (Upbit),
lookback 50, gp without multiplier, busy rule, no cap, fee 0.04%/side, slippage 0.05%
on entry/time-exit, funding (entry, exit] x mark.

Kimchi delta definition (fixed):
  premium(t) = upbit_close(t) / (binance_close(t) * krwusdt_close(t)) - 1, 1m data,
  each leg forward-filled as-of t with a 60-minute staleness cap (micro-gap fill only).
  baseline  = premium as-of the last minute strictly BEFORE 00:00 UTC of the event candle.
  at entry  = premium as-of the last minute strictly BEFORE 04:00 UTC (point-in-time).
  dkimchi   = (premium_entry - premium_baseline) * 100  [percentage points]

Stage 1 (IS 2026-01-01 -> 2026-06-01): bin dkimchi with bins FIXED BEFORE looking at PnL:
  (-inf,0), [0,0.5), [0.5,1), [1,1.5), [1.5,2), [2,2.5), [2.5,3), [3,inf)  (p.p.)
  Band is derived from FORM: the contiguous run of positive-expectancy bins with the
  largest total N (edges where bin expectancy crosses zero). Author's prior 0.2-1.7 p.p.
  is printed alongside for comparison only.

Stage 2 (OOS 2025-01-01 -> 2026-01-01), pre-registered criterion:
  PASS iff (a) expectancy(cond) - expectancy(blind) > 0 AND
           (b) paired bootstrap (1000 resamples) 95% CI lower bound of the difference > 0.
  If N(conditioned) < 40 -> flag as low-power regardless of sign.
"""

import math
import time

import duckdb
import numpy as np
import pandas as pd

P = "data/parquet"
FEE_SIDE = 0.0004
SLIP = 0.0005
NOTIONAL = 1000.0
HOUR_MS = 3_600_000
MIN_MS = 60_000
STALE_MS = 60 * MIN_MS  # forward-fill staleness cap for premium legs

DAY_CFG = dict(vp=40, gp=15, pp=5, sl=13, tp=90)  # frozen H1
BIN_EDGES = [-np.inf, 0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, np.inf]  # fixed before PnL

con = duckdb.connect()
con.execute("PRAGMA threads=8")

KRW = con.sql(f"SELECT timestamp_utc, close FROM read_parquet('{P}/upbit_krw_usdt_1m.parquet') ORDER BY timestamp_utc").df()
K_TS = KRW["timestamp_utc"].values.astype("datetime64[ms]").astype(np.int64)
K_CL = KRW["close"].values


def asof(ts_arr, val_arr, t_ms):
    """Last value strictly before t_ms, None if absent or staler than STALE_MS."""
    i = np.searchsorted(ts_arr, t_ms) - 1
    if i < 0 or t_ms - ts_arr[i] > STALE_MS:
        return None
    return val_arr[i]


def day_points(hmap, hk, lookback=50):
    row = hmap.get(hk)
    if row is None:
        return None
    o, hi, v = row
    pp = 0 if o <= 0 or (hi - o) / o * 100 <= 3 else math.ceil((hi - o) / o * 100 - 3)
    cum = 0.0
    vp = 0
    for k in range(1, lookback + 1):
        r = hmap.get(hk - k)
        cum += r[2] if r else 0.0
        if cum <= 0 or v <= cum:
            break
        vp = k
    rm = 0.0
    gp = 0
    for k in range(1, lookback + 1):
        r = hmap.get(hk - k)
        rm = max(rm, r[1] if r else 0.0)
        if rm <= 0 or hi <= rm:
            break
        gp = k
    return vp, gp, pp


def run_day_pool(start, end):
    """Frozen H1 day-strategy trades for [start, end), with dkimchi per trade."""
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    data_end_ms = int(end.value // 1_000_000)
    universe = con.sql(f"SELECT token FROM read_parquet('{P}/universe.parquet') ORDER BY token").df()["token"].tolist()
    hourly = con.sql(f"""
        SELECT token, date_trunc('hour', timestamp_utc) AS hour,
               first(open ORDER BY timestamp_utc) AS open, max(high) AS high, sum(volume) AS volume
        FROM read_parquet('{P}/upbit_1m/*/*.parquet', hive_partitioning=1)
        WHERE timestamp_utc >= TIMESTAMP '{start}' AND timestamp_utc < TIMESTAMP '{end}'
        GROUP BY token, hour
    """).df()
    hourly["hkey"] = hourly["hour"].values.astype("datetime64[h]").astype(np.int64)
    hourly_by_token = dict(tuple(hourly.groupby("token")))
    funding_df = con.sql(f"SELECT token, funding_time_utc, funding_rate FROM read_parquet('{P}/binance_funding.parquet') ORDER BY token, funding_time_utc").df()
    funding = {t: (g["funding_time_utc"].values.astype("datetime64[ms]").astype(np.int64), g["funding_rate"].values)
               for t, g in funding_df.groupby("token")}

    days = pd.date_range(start, end - pd.Timedelta(days=1), freq="1D")
    day_ms00 = days.values.astype("datetime64[ms]").astype(np.int64)
    day_hkeys = days.values.astype("datetime64[h]").astype(np.int64)

    raw = []
    for token in universe:
        hdf = hourly_by_token.get(token)
        if hdf is None:
            continue
        hmap = {int(k): (o, h, v) for k, o, h, v in zip(hdf["hkey"], hdf["open"], hdf["high"], hdf["volume"])}
        f_times, f_rates = funding.get(token, (np.array([], dtype=np.int64), np.array([])))
        b = con.sql(f"""
            SELECT timestamp_utc, open, high, low, close FROM read_parquet('{P}/binance_1m/token={token}/*.parquet')
            WHERE timestamp_utc >= TIMESTAMP '{start}' AND timestamp_utc < TIMESTAMP '{end}'
            ORDER BY timestamp_utc
        """).df()
        if b.empty:
            continue
        bts = b["timestamp_utc"].values.astype("datetime64[ms]").astype(np.int64)
        bo, bh, bl, bc = b["open"].values, b["high"].values, b["low"].values, b["close"].values
        u = con.sql(f"""
            SELECT timestamp_utc, open, close, volume FROM read_parquet('{P}/upbit_1m/token={token}/*.parquet')
            WHERE timestamp_utc >= TIMESTAMP '{start - pd.Timedelta(hours=2)}' AND timestamp_utc < TIMESTAMP '{end}'
              AND (hour(timestamp_utc) IN (0, 3, 23) OR (hour(timestamp_utc) = 4 AND minute(timestamp_utc) = 0))
            ORDER BY timestamp_utc
        """).df()
        uts = u["timestamp_utc"].values.astype("datetime64[ms]").astype(np.int64)
        uo, ucl = u["open"].values, u["close"].values

        for di in range(len(days)):
            t00 = int(day_ms00[di])
            hk = int(day_hkeys[di])
            pts = day_points(hmap, hk)
            if pts is None:
                continue
            vp, gp, pp = pts
            if not (vp >= DAY_CFG["vp"] and gp >= DAY_CFG["gp"] and pp >= DAY_CFG["pp"]):
                continue
            deadline = t00 + 38 * HOUR_MS
            if deadline > data_end_ms:
                continue
            upbit_ref = hmap[hk][0]
            if upbit_ref <= 0:
                continue
            t04 = t00 + 4 * HOUR_MS
            iu = np.searchsorted(uts, t04)
            if iu < len(uts) and uts[iu] == t04:
                p04 = uo[iu]
            else:
                ja = np.searchsorted(uts, t00 + 3 * HOUR_MS)
                jb = np.searchsorted(uts, t04)
                if jb > ja:
                    p04 = ucl[jb - 1]
                else:
                    continue
            growth_up = p04 / upbit_ref - 1
            if growth_up < 0.03:
                continue
            fi = np.searchsorted(bts, t04)
            if fi >= len(bts) or bts[fi] != t04:
                continue
            raw_open = bo[fi]
            entry_px = raw_open * (1 - SLIP)
            ref_imp = raw_open / (1 + growth_up)
            sl_px = entry_px * (1 + DAY_CFG["sl"] / 100)
            tp_px = entry_px - DAY_CFG["tp"] / 100 * (entry_px - ref_imp)

            i_end = np.searchsorted(bts, deadline)
            seg_h, seg_l = bh[fi:i_end], bl[fi:i_end]
            hits = (seg_h >= sl_px) | (seg_l <= tp_px)
            if hits.any():
                j = int(np.argmax(hits))
                exit_ms = int(bts[fi + j]) + MIN_MS - 1
                exit_px, reason = (sl_px, "SL") if seg_h[j] >= sl_px else (tp_px, "TP")
            elif i_end < len(bts) and bts[i_end] == deadline:
                exit_px, reason, exit_ms = bo[i_end] * (1 + SLIP), "TIME", deadline
            elif i_end > fi:
                exit_px, reason, exit_ms = bc[i_end - 1] * (1 + SLIP), "TIME", int(bts[i_end - 1]) + MIN_MS - 1
            else:
                continue
            qty = NOTIONAL / entry_px
            gross = qty * (entry_px - exit_px)
            fees = NOTIONAL * FEE_SIDE + qty * exit_px * FEE_SIDE
            ia = np.searchsorted(f_times, t04, side="right")
            ib = np.searchsorted(f_times, exit_ms, side="right")
            ft, fr = f_times[ia:ib], f_rates[ia:ib]
            funding_pnl = float((fr * bc[np.clip(np.searchsorted(bts, ft, side='right') - 1, 0, len(bc) - 1)]).sum()) * qty if len(ft) else 0.0
            net = gross - fees + funding_pnl

            # ---- dkimchi (does not affect the trade; measured for H2 only) ----
            def prem(t_ms):
                uu = asof(uts, ucl, t_ms)
                bb = asof(bts, bc, t_ms)
                kk = asof(K_TS, K_CL, t_ms)
                if uu is None or bb is None or kk is None or bb <= 0 or kk <= 0:
                    return None
                return uu / (bb * kk) - 1

            p_base = prem(t00)      # strictly before 00:00
            p_entry = prem(t04)     # strictly before 04:00
            dk = (p_entry - p_base) * 100 if (p_base is not None and p_entry is not None) else np.nan

            raw.append(dict(token=token, date=days[di].strftime("%Y-%m-%d"), entry_ms=int(t04),
                            exit_ms=exit_ms, reason=reason, net=net, dkimchi=dk))

    # busy rule
    trades = []
    open_until = {}
    for r in sorted(raw, key=lambda x: (x["token"], x["entry_ms"])):
        if r["entry_ms"] < open_until.get(r["token"], -1):
            continue
        open_until[r["token"]] = r["exit_ms"]
        trades.append(r)
    return pd.DataFrame(trades)


def boot_ci(net, seed, n_boot=1000):
    n = len(net)
    if n == 0:
        return np.nan, np.nan
    rng = np.random.default_rng(seed)
    means = net[rng.integers(0, n, (n_boot, n))].mean(axis=1)
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def mdd_pct(df):
    if df.empty:
        return np.nan
    entry, exit_, net = df["entry_ms"].values, df["exit_ms"].values, df["net"].values
    ev = sorted([(t, 1) for t in entry] + [(t, -1) for t in exit_])
    cur = peak = 0
    for _, d in ev:
        cur += d
        peak = max(peak, cur)
    base = peak * NOTIONAL
    order = np.argsort(exit_)
    eq = base + np.cumsum(net[order])
    run_peak = np.maximum.accumulate(np.concatenate([[base], eq]))[1:]
    return float(((run_peak - eq) / run_peak).max() * 100)


def main():
    t0 = time.time()

    # ================= STAGE 1: IS form =================
    print("Stage 1: building frozen day pool on IS 2026-01-01 -> 2026-06-01 ...", flush=True)
    is_tr = run_day_pool("2026-01-01", "2026-06-01")
    n_nan = int(is_tr.dkimchi.isna().sum())
    print(f"IS trades: {len(is_tr)} (dkimchi NaN: {n_nan})  total net {is_tr.net.sum():+.2f} "
          f"expectancy {is_tr.net.mean():+.2f}")

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

    # band from FORM: contiguous run of positive-expectancy bins with the largest total N
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
    inverted = (bins_df.expectancy.iloc[-1] < bins_df.expectancy.iloc[i0:j0 + 1].mean()) if bins_df.n.iloc[-1] > 0 else None
    print(f"\nBand from IS form: [{band_lo:g}, {band_hi:g}) p.p.   (author prior: [0.2, 1.7])")
    print(f"Extreme-bin check (inverted profile): top bin expectancy "
          f"{bins_df.expectancy.iloc[-1]:+.2f} (n={bins_df.n.iloc[-1]}) vs band mean "
          f"{bins_df.expectancy.iloc[i0:j0+1].mean():+.2f} -> {'inverted OK' if inverted else 'NOT inverted' if inverted is not None else 'no data in top bin'}")

    # ================= STAGE 2: OOS paired contrast =================
    print("\nStage 2: OOS 2025-01-01 -> 2026-01-01, single run ...", flush=True)
    oos = run_day_pool("2025-01-01", "2026-01-01")
    oos["in_band"] = (oos.dkimchi >= band_lo) & (oos.dkimchi < band_hi)
    oos.to_csv("results/h2_paired_oos.csv", index=False)
    blind = oos
    cond = oos[oos.in_band & oos.dkimchi.notna()]

    e_b, e_c = blind.net.mean(), cond.net.mean()
    lo_b, hi_b = boot_ci(blind.net.values, seed=900)
    lo_c, hi_c = boot_ci(cond.net.values, seed=901)

    # paired bootstrap of the difference: resample blind rows, recompute both means
    rng = np.random.default_rng(902)
    net = blind.net.values
    mask = blind.in_band.values & blind.dkimchi.notna().values
    n = len(net)
    diffs = []
    for _ in range(1000):
        idx = rng.integers(0, n, n)
        m = mask[idx]
        if m.sum() == 0:
            diffs.append(np.nan)
            continue
        diffs.append(net[idx][m].mean() - net[idx].mean())
    diffs = np.array(diffs)
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
    print(f"{'MDD %':24}{mdd_pct(blind):>14.2f}{mdd_pct(cond):>14.2f}")
    print(f"\nband filtered out {len(blind)-len(cond)} of {len(blind)} blind trades "
          f"(dkimchi NaN: {int(blind.dkimchi.isna().sum())})")
    print(f"difference (cond - blind): {diff:+.2f} USDT/trade, paired bootstrap CI [{d_lo:+.2f}, {d_hi:+.2f}]")

    crit_a = diff > 0
    crit_b = d_lo > 0
    print(f"\ncriterion (a) diff > 0:        {'PASS' if crit_a else 'FAIL'}")
    print(f"criterion (b) CI lower > 0:    {'PASS' if crit_b else 'FAIL'}")
    verdict = "PASS" if (crit_a and crit_b) else "FAIL"
    power_flag = " [LOW POWER: N(cond) < 40]" if len(cond) < 40 else ""
    print(f"H2 VERDICT: {verdict}{power_flag}")
    print(f"\nElapsed {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
