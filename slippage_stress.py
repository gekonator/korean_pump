"""Slippage stress of the frozen day H1 config (vp>=40, gp>=15, pp>=5, SL 13%, TP 90%,
entry 04:00 UTC, exit D+1 14:00 UTC, no kimchi, no cap). Trade triggers untouched — only
the execution model is stressed. Base 10,000 USDT, flat 1000/trade, realized-only.

Three execution points stressed separately:
  s_entry — short entry fills s_entry% below the theoretical price (selling into the book);
  s_exit  — time-exit buy-back fills s_exit% above;
  s_stop  — stop trigger at the nominal level, FILL s_stop% worse (price running up through
            an illiquid post-pump alt) — the key negative-tail point.
TP is a limit buy-back: fills at target or better, no extra slippage layer (double-count
otherwise). Entry slippage shifts the SL/TP levels too (they are % of the actual fill).

Full grid: s_entry x s_exit in {0.05,0.10,0.15,0.20,0.30}%, s_stop in
{0.05,0.10,0.20,0.30,0.50,0.75,1.0}% -> 175 combos, IS 2026 and OOS 2025 separately.
"""

import time

import numpy as np
import pandas as pd

import engine
from engine import day_points

P = "data/parquet"
FIXED_BASE = 10_000.0
FEE_SIDE = 0.0004
NOTIONAL = 1000.0
HOUR_MS = 3_600_000
MIN_MS = 60_000

S_ENTRY = [0.05, 0.10, 0.15, 0.20, 0.30]
S_EXIT = [0.05, 0.10, 0.15, 0.20, 0.30]
S_STOP = [0.05, 0.10, 0.20, 0.30, 0.50, 0.75, 1.00]

PERIODS = {"IS_2026": ("2026-01-01", "2026-06-01", 151), "OOS_2025": ("2025-01-01", "2026-01-01", 365)}


def collect_candidates(start, end):
    """Frozen-config candidates with cached price windows; execution left parametric."""
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    data_end_ms = int(end.value // 1_000_000)
    universe = engine.sql(f"SELECT token FROM read_parquet('{P}/universe.parquet') ORDER BY token")["token"].tolist()
    hourly = engine.sql(f"""
        SELECT token, date_trunc('hour', timestamp_utc) AS hour,
               first(open ORDER BY timestamp_utc) AS open, max(high) AS high, sum(volume) AS volume
        FROM read_parquet('{P}/upbit_1m/*/*.parquet', hive_partitioning=1)
        WHERE timestamp_utc >= TIMESTAMP '{start}' AND timestamp_utc < TIMESTAMP '{end}'
        GROUP BY token, hour
    """)
    hourly["hkey"] = hourly["hour"].values.astype("datetime64[h]").astype(np.int64)
    hourly_by_token = dict(tuple(hourly.groupby("token")))
    funding_df = engine.sql(f"SELECT token, funding_time_utc, funding_rate FROM read_parquet('{P}/binance_funding.parquet') ORDER BY token, funding_time_utc")
    funding = {t: (g["funding_time_utc"].values.astype("datetime64[ms]").astype(np.int64), g["funding_rate"].values)
               for t, g in funding_df.groupby("token")}

    days = pd.date_range(start, end - pd.Timedelta(days=1), freq="1D")
    day_ms00 = days.values.astype("datetime64[ms]").astype(np.int64)
    day_hkeys = days.values.astype("datetime64[h]").astype(np.int64)

    cands = []
    for token in universe:
        hdf = hourly_by_token.get(token)
        if hdf is None:
            continue
        hmap = {int(k): (o, h, v) for k, o, h, v in zip(hdf["hkey"], hdf["open"], hdf["high"], hdf["volume"])}
        f_times, f_rates = funding.get(token, (np.array([], dtype=np.int64), np.array([])))
        b = engine.sql(f"""
            SELECT timestamp_utc, open, high, low, close FROM read_parquet('{P}/binance_1m/token={token}/*.parquet')
            WHERE timestamp_utc >= TIMESTAMP '{start}' AND timestamp_utc < TIMESTAMP '{end}'
            ORDER BY timestamp_utc
        """)
        if b.empty:
            continue
        bts = b["timestamp_utc"].values.astype("datetime64[ms]").astype(np.int64)
        bo, bh, bl, bc = b["open"].values, b["high"].values, b["low"].values, b["close"].values
        u = engine.sql(f"""
            SELECT timestamp_utc, open, close FROM read_parquet('{P}/upbit_1m/token={token}/*.parquet')
            WHERE timestamp_utc >= TIMESTAMP '{start}' AND timestamp_utc < TIMESTAMP '{end}'
              AND (hour(timestamp_utc) = 3 OR (hour(timestamp_utc) = 4 AND minute(timestamp_utc) = 0))
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
            if not (vp >= 40 and gp >= 15 and pp >= 5):
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
            i_end = np.searchsorted(bts, deadline)
            has_deadline_bar = i_end < len(bts) and bts[i_end] == deadline
            hi_idx = i_end + 1 if has_deadline_bar else i_end
            ia = np.searchsorted(f_times, t04, side="right")
            ib = np.searchsorted(f_times, deadline, side="right")
            cands.append(dict(
                token=token, date=days[di].strftime("%Y-%m-%d"), entry_ms=int(t04), deadline=deadline,
                raw_open=bo[fi], ref_imp=bo[fi] / (1 + growth_up), has_deadline_bar=has_deadline_bar,
                w_ts=bts[fi:hi_idx].copy(), w_o=bo[fi:hi_idx].copy(), w_h=bh[fi:hi_idx].copy(),
                w_l=bl[fi:hi_idx].copy(), w_c=bc[fi:hi_idx].copy(),
                f_t=f_times[ia:ib].copy(), f_r=f_rates[ia:ib].copy(),
            ))
    return cands


def simulate(c, s_entry, s_exit, s_stop):
    entry_px = c["raw_open"] * (1 - s_entry / 100)
    sl_level = entry_px * 1.13
    tp_px = entry_px - 0.90 * (entry_px - c["ref_imp"])
    n_scan = len(c["w_ts"]) - (1 if c["has_deadline_bar"] else 0)
    seg_h, seg_l = c["w_h"][:n_scan], c["w_l"][:n_scan]
    hits = (seg_h >= sl_level) | (seg_l <= tp_px)
    if hits.any():
        j = int(np.argmax(hits))
        exit_ms = int(c["w_ts"][j]) + MIN_MS - 1
        if seg_h[j] >= sl_level:
            exit_px, reason = sl_level * (1 + s_stop / 100), "SL"
        else:
            exit_px, reason = tp_px, "TP"
    elif c["has_deadline_bar"]:
        exit_px, reason, exit_ms = c["w_o"][-1] * (1 + s_exit / 100), "TIME", c["deadline"]
    elif n_scan > 0:
        exit_px, reason, exit_ms = c["w_c"][n_scan - 1] * (1 + s_exit / 100), "TIME", int(c["w_ts"][n_scan - 1]) + MIN_MS - 1
    else:
        return None
    qty = NOTIONAL / entry_px
    gross = qty * (entry_px - exit_px)
    fees = NOTIONAL * FEE_SIDE + qty * exit_px * FEE_SIDE
    m = c["f_t"] <= exit_ms
    if m.any():
        mi = np.clip(np.searchsorted(c["w_ts"], c["f_t"][m], side="right") - 1, 0, len(c["w_c"]) - 1)
        fund = float((c["f_r"][m] * c["w_c"][mi]).sum()) * qty
    else:
        fund = 0.0
    return dict(token=c["token"], entry_ms=c["entry_ms"], exit_ms=exit_ms, reason=reason,
                net=gross - fees + fund)


def run_combo(cands, s_entry, s_exit, s_stop, seed):
    raw = [simulate(c, s_entry, s_exit, s_stop) for c in cands]
    raw = [r for r in raw if r]
    trades = []
    open_until = {}
    for r in sorted(raw, key=lambda x: (x["token"], x["entry_ms"])):
        if r["entry_ms"] < open_until.get(r["token"], -1):
            continue
        open_until[r["token"]] = r["exit_ms"]
        trades.append(r)
    net = np.array([t["net"] for t in trades])
    n = len(net)
    rng = np.random.default_rng(seed)
    boot = net[rng.integers(0, n, (1000, n))].mean(axis=1)
    ci_lo = float(np.percentile(boot, 2.5))
    wins, losses = net[net > 0], net[net <= 0]
    pf = wins.sum() / abs(losses.sum()) if len(losses) and losses.sum() != 0 else np.inf
    exit_ = np.array([t["exit_ms"] for t in trades])
    eq = FIXED_BASE + np.cumsum(net[np.argsort(exit_)])
    run_peak = np.maximum.accumulate(np.concatenate([[FIXED_BASE], eq]))[1:]
    mdd = float(((run_peak - eq) / run_peak).max() * 100)
    return dict(n=n, expectancy=net.mean(), ci_low=ci_lo, win_rate=(net > 0).mean() * 100,
                pf=pf, total_net=net.sum(), mdd_pct=mdd)


def main():
    t0 = time.time()
    rows = []
    for period, (start, end, days_n) in PERIODS.items():
        print(f"collecting candidates {period} ...", flush=True)
        cands = collect_candidates(start, end)
        print(f"  {len(cands)} candidates, running {len(S_ENTRY)*len(S_EXIT)*len(S_STOP)} combos", flush=True)
        seed = 3000
        for se in S_ENTRY:
            for sx in S_EXIT:
                for ss in S_STOP:
                    m = run_combo(cands, se, sx, ss, seed)
                    seed += 1
                    ann = m["total_net"] / FIXED_BASE * (365 / days_n) * 100
                    rows.append(dict(period=period, s_entry=se, s_exit=sx, s_stop=ss,
                                     n_trades=m["n"], expectancy=round(m["expectancy"], 2),
                                     ci_low=round(m["ci_low"], 2), win_rate=round(m["win_rate"], 1),
                                     pf=round(m["pf"], 2), ann_return_pct=round(ann, 2),
                                     mdd_pct=round(m["mdd_pct"], 2)))
    df = pd.DataFrame(rows)
    df.to_csv("results/slippage_stress.csv", index=False)
    print(f"\nsaved results/slippage_stress.csv ({len(df)} rows), {time.time()-t0:.0f}s")

    oos = df[df.period == "OOS_2025"]
    print("\n=== Edge survival on OOS (criterion: bootstrap CI_low > 0) ===")
    base = oos[(oos.s_entry == 0.05) & (oos.s_exit == 0.05)].sort_values("s_stop")
    print("\ns_stop sweep (s_entry=s_exit=0.05):")
    print(base[["s_stop", "expectancy", "ci_low", "win_rate", "ann_return_pct", "mdd_pct"]].to_string(index=False))
    died = base[base.ci_low <= 0]
    print(f"-> s_stop threshold: {'edge survives all tested s_stop' if died.empty else f'CI_low crosses 0 at s_stop={died.s_stop.iloc[0]}%'}")

    sw = oos[(oos.s_exit == 0.05) & (oos.s_stop == 0.05)].sort_values("s_entry")
    print("\ns_entry sweep (s_exit=0.05, s_stop=0.05):")
    print(sw[["s_entry", "expectancy", "ci_low"]].to_string(index=False))
    d2 = sw[sw.ci_low <= 0]
    print(f"-> s_entry threshold: {'survives all' if d2.empty else f'crosses 0 at {d2.s_entry.iloc[0]}%'}")

    sxw = oos[(oos.s_entry == 0.05) & (oos.s_stop == 0.05)].sort_values("s_exit")
    print("\ns_exit sweep (s_entry=0.05, s_stop=0.05):")
    print(sxw[["s_exit", "expectancy", "ci_low"]].to_string(index=False))
    d3 = sxw[sxw.ci_low <= 0]
    print(f"-> s_exit threshold: {'survives all' if d3.empty else f'crosses 0 at {d3.s_exit.iloc[0]}%'}")

    print("\n=== Realistic post-pump scenarios (OOS) ===")
    for se, sx, ss, label in [(0.10, 0.10, 0.30, "optimistic-realistic"),
                              (0.15, 0.15, 0.50, "mid-realistic"),
                              (0.20, 0.20, 0.75, "pessimistic-realistic")]:
        r = oos[(oos.s_entry == se) & (oos.s_exit == sx) & (oos.s_stop == ss)].iloc[0]
        print(f"{label:24} entry/exit {se}/{sx}%, stop {ss}%: expectancy {r.expectancy:+.2f}, "
              f"CI_low {r.ci_low:+.2f} -> {'SURVIVES' if r.ci_low > 0 else 'FAILS'}")


if __name__ == "__main__":
    main()
