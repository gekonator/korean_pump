"""OOS run: 2025-01-01 -> 2026-01-01, both configs frozen from the IS grid (stage B).
Pre-registered pass criteria (fixed BEFORE this run):
  1) OOS bootstrap CI lower bound (2.5th pct of 1000 resamples of expectancy) > 0
  2) OOS expectancy >= 50% of IS expectancy
     IS expectancy: day +26.79 USDT/trade -> threshold 13.39; night +3.47 -> threshold 1.73.

Engine identical to stage B (same cost model, same signal machinery). All times UTC.
Note: OOS data starts exactly 2025-01-01, so the first ~2 days have truncated lookback
(missing prior hours contribute zero volume); this slightly deflates early-January points.
"""

import math
import time

import duckdb
import numpy as np
import pandas as pd

P = "data/parquet"
OOS_START = pd.Timestamp("2025-01-01")
OOS_END = pd.Timestamp("2026-01-01")   # exclusive
DATA_END = pd.Timestamp("2026-01-01")  # OOS never reads beyond this
PERIOD_DAYS = (OOS_END - OOS_START).days

FEE_SIDE = 0.0004
SLIP = 0.0005
NOTIONAL = 1000.0
HOUR_MS = 3_600_000
MIN_MS = 60_000

NIGHT_CFG = dict(V=5, G=3, sl=8, tp=90)
DAY_CFG = dict(vp=40, gp=15, pp=5, sl=13, tp=90)
IS_EXPECTANCY = dict(night=3.465188, day=26.788527)

con = duckdb.connect()
con.execute("PRAGMA threads=8")


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


def simulate_short(bts, bo, bh, bl, bc, entry_i, deadline_ms, entry_px, sl_px, tp_px,
                   f_times, f_rates, entry_ms):
    i_end = np.searchsorted(bts, deadline_ms)
    seg_h, seg_l = bh[entry_i:i_end], bl[entry_i:i_end]
    hits = (seg_h >= sl_px) | (seg_l <= tp_px)
    if hits.any():
        j = int(np.argmax(hits))
        exit_ms = int(bts[entry_i + j]) + MIN_MS - 1
        if seg_h[j] >= sl_px:
            exit_px, reason = sl_px, "SL"
        else:
            exit_px, reason = tp_px, "TP"
    elif i_end < len(bts) and bts[i_end] == deadline_ms:
        exit_px, reason, exit_ms = bo[i_end] * (1 + SLIP), "TIME", deadline_ms
    elif i_end > entry_i:
        exit_px, reason, exit_ms = bc[i_end - 1] * (1 + SLIP), "TIME", int(bts[i_end - 1]) + MIN_MS - 1
    else:
        return None
    qty = NOTIONAL / entry_px
    gross = qty * (entry_px - exit_px)
    fees = NOTIONAL * FEE_SIDE + qty * exit_px * FEE_SIDE
    ia = np.searchsorted(f_times, entry_ms, side="right")
    ib = np.searchsorted(f_times, exit_ms, side="right")
    ft, fr = f_times[ia:ib], f_rates[ia:ib]
    if len(ft):
        mi = np.clip(np.searchsorted(bts, ft, side="right") - 1, 0, len(bc) - 1)
        funding = float((fr * bc[mi]).sum()) * qty
    else:
        funding = 0.0
    return dict(entry_ms=entry_ms, exit_ms=exit_ms, reason=reason, net=gross - fees + funding)


def main():
    t0 = time.time()
    universe = con.sql(f"SELECT token FROM read_parquet('{P}/universe.parquet') ORDER BY token").df()["token"].tolist()
    hourly = con.sql(f"""
        SELECT token, date_trunc('hour', timestamp_utc) AS hour,
               first(open ORDER BY timestamp_utc) AS open, max(high) AS high, sum(volume) AS volume
        FROM read_parquet('{P}/upbit_1m/*/*.parquet', hive_partitioning=1)
        WHERE timestamp_utc >= TIMESTAMP '{OOS_START}' AND timestamp_utc < TIMESTAMP '{DATA_END}'
        GROUP BY token, hour
    """).df()
    hourly["hkey"] = hourly["hour"].values.astype("datetime64[h]").astype(np.int64)
    hourly_by_token = dict(tuple(hourly.groupby("token")))
    funding_df = con.sql(f"SELECT token, funding_time_utc, funding_rate FROM read_parquet('{P}/binance_funding.parquet') ORDER BY token, funding_time_utc").df()
    funding = {t: (g["funding_time_utc"].values.astype("datetime64[ms]").astype(np.int64), g["funding_rate"].values)
               for t, g in funding_df.groupby("token")}

    days = pd.date_range(OOS_START, OOS_END - pd.Timedelta(days=1), freq="1D")
    day_ms00 = days.values.astype("datetime64[ms]").astype(np.int64)
    day_hkeys = days.values.astype("datetime64[h]").astype(np.int64)
    data_end_ms = int(DATA_END.value // 1_000_000)

    night_trades = []
    day_raw = []  # (token, di, trade)

    for n, token in enumerate(universe, 1):
        hdf = hourly_by_token.get(token)
        if hdf is None:
            continue
        hmap = {int(k): (o, h, v) for k, o, h, v in zip(hdf["hkey"], hdf["open"], hdf["high"], hdf["volume"])}
        f_times, f_rates = funding.get(token, (np.array([], dtype=np.int64), np.array([])))
        b = con.sql(f"""
            SELECT timestamp_utc, open, high, low, close FROM read_parquet('{P}/binance_1m/token={token}/*.parquet')
            WHERE timestamp_utc >= TIMESTAMP '{OOS_START}' AND timestamp_utc < TIMESTAMP '{DATA_END}'
            ORDER BY timestamp_utc
        """).df()
        if b.empty:
            continue
        bts = b["timestamp_utc"].values.astype("datetime64[ms]").astype(np.int64)
        bo, bh, bl, bc = b["open"].values, b["high"].values, b["low"].values, b["close"].values
        u = con.sql(f"""
            SELECT timestamp_utc, open, close, volume FROM read_parquet('{P}/upbit_1m/token={token}/*.parquet')
            WHERE timestamp_utc >= TIMESTAMP '{OOS_START}' AND timestamp_utc < TIMESTAMP '{DATA_END}'
              AND (hour(timestamp_utc) = 0 OR hour(timestamp_utc) = 3 OR
                   (hour(timestamp_utc) = 4 AND minute(timestamp_utc) = 0))
            ORDER BY timestamp_utc
        """).df()
        uts = u["timestamp_utc"].values.astype("datetime64[ms]").astype(np.int64)
        uo, ucl, uv = u["open"].values, u["close"].values, u["volume"].values

        for di in range(len(days)):
            t00 = int(day_ms00[di])
            hk = int(day_hkeys[di])

            # ---- NIGHT (V=5, G=3, sl=8, tp=90) ----
            t0010 = t00 + 10 * MIN_MS
            iref = np.searchsorted(bts, t0010)
            if iref < len(bts) and bts[iref] == t0010:
                ref_px = bo[iref]
                Pcum = np.zeros(24)
                cum = 0.0
                for k in range(1, 25):
                    r = hmap.get(hk - k)
                    cum += r[2] if r else 0.0
                    Pcum[k - 1] = cum
                ia_u = np.searchsorted(uts, t00)
                ib_u = np.searchsorted(uts, t00 + HOUR_MS)
                mins_u = ((uts[ia_u:ib_u] - t00) // MIN_MS).astype(int)
                vol_by_min = np.zeros(60)
                np.add.at(vol_by_min, mins_u, uv[ia_u:ib_u])
                live = np.cumsum(vol_by_min)
                mm = np.arange(10, 60)
                lv = live[mm]
                cond = (Pcum[None, :] < lv[:, None]) & (Pcum[None, :] > 0)
                vp_live = np.where(cond.all(axis=1), 24, np.argmin(cond, axis=1))
                tmins = t00 + mm * MIN_MS
                ci = np.clip(np.searchsorted(bts, tmins + MIN_MS - 1, side="right") - 1, 0, len(bc) - 1)
                valid_close = bts[ci] >= t00
                gp_live = np.where(valid_close & (ref_px > 0),
                                   np.floor((bc[ci] / ref_px - 1) * 100), -999).astype(int)
                ok = (vp_live >= NIGHT_CFG["V"]) & (gp_live >= NIGHT_CFG["G"])
                if ok.any():
                    m = int(mm[int(np.argmax(ok))])
                    fill_ms = t00 + (m + 1) * MIN_MS
                    fi = np.searchsorted(bts, fill_ms)
                    deadline = t00 + 3 * HOUR_MS
                    if fi < len(bts) and bts[fi] <= fill_ms + 5 * MIN_MS and bts[fi] < deadline:
                        entry_ms = int(bts[fi])
                        entry_px = bo[fi] * (1 - SLIP)
                        sl_px = entry_px * (1 + NIGHT_CFG["sl"] / 100)
                        tp_px = entry_px - NIGHT_CFG["tp"] / 100 * (entry_px - ref_px)
                        tr = simulate_short(bts, bo, bh, bl, bc, fi, deadline, entry_px, sl_px, tp_px,
                                            f_times, f_rates, entry_ms)
                        if tr:
                            night_trades.append(tr)

            # ---- DAY (vp>=40, gp>=15, pp>=5, sl=13, tp=90) ----
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
            tr = simulate_short(bts, bo, bh, bl, bc, fi, deadline, entry_px, sl_px, tp_px,
                                f_times, f_rates, int(t04))
            if tr:
                day_raw.append((token, di, tr))

        if n % 60 == 0 or n == len(universe):
            print(f"  {n}/{len(universe)} tokens, night={len(night_trades)} day_raw={len(day_raw)}, "
                  f"{time.time()-t0:.0f}s", flush=True)

    # day: busy rule per token
    day_trades = []
    open_until = {}
    for token, di, tr in sorted(day_raw, key=lambda x: (x[0], x[1])):
        if tr["entry_ms"] < open_until.get(token, -1):
            continue
        open_until[token] = tr["exit_ms"]
        day_trades.append(tr)

    def report(name, trades, seed):
        net = np.array([t["net"] for t in trades])
        entry = np.array([t["entry_ms"] for t in trades])
        exit_ = np.array([t["exit_ms"] for t in trades])
        reasons = pd.Series([t["reason"] for t in trades])
        n = len(net)
        rng = np.random.default_rng(seed)
        boot = net[rng.integers(0, n, (1000, n))].mean(axis=1)
        ci_low, ci_high = np.percentile(boot, [2.5, 97.5])
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
        cagr = ((1 + net.sum() / base) ** (365 / PERIOD_DAYS) - 1) * 100
        exp = net.mean()
        thr = IS_EXPECTANCY[name] * 0.5
        p1 = ci_low > 0
        p2 = exp >= thr
        print(f"\n===== {name.upper()} OOS 2025 =====")
        print(f"trades {n} | exits TP {int((reasons=='TP').sum())} SL {int((reasons=='SL').sum())} TIME {int((reasons=='TIME').sum())}")
        print(f"expectancy {exp:+.2f} USDT/trade ({exp/10:.2f}%)  bootstrap CI [{ci_low:+.2f}, {ci_high:+.2f}]")
        wins = net[net > 0]
        losses = net[net <= 0]
        pf = wins.sum() / abs(losses.sum()) if losses.sum() != 0 else np.inf
        print(f"win rate {len(wins)/n*100:.1f}%  avg win {wins.mean():.2f}  avg loss {losses.mean():.2f}  PF {pf:.2f}")
        print(f"total net {net.sum():+.2f} USDT  peak concurrent {peak}  CAGR {cagr:.1f}%  MDD {mdd:.2f}%")
        print(f"IS expectancy {IS_EXPECTANCY[name]:+.2f} -> 50% threshold {thr:+.2f}")
        print(f"criterion 1 (CI low > 0):        {'PASS' if p1 else 'FAIL'}  (ci_low {ci_low:+.2f})")
        print(f"criterion 2 (exp >= 50% of IS):  {'PASS' if p2 else 'FAIL'}  (exp {exp:+.2f} vs {thr:+.2f})")
        print(f"VERDICT: {'PASS' if (p1 and p2) else 'FAIL'}")
        pd.DataFrame(trades).to_csv(f"results/oos_trades_{name}.csv", index=False)

    report("night", night_trades, 101)
    report("day", day_trades, 202)
    print(f"\nElapsed {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
