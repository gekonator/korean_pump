"""Stage B: H1 grid search on IS = 2026-01-01 -> 2026-06-01. No kimchi, no concurrency cap.
Only rule: a token cannot open a new position while its previous one is open (day strategy;
night positions close same day so the rule never binds there).

Expensive part (Upbit 1m->1h aggregation, raw points, Binance minute windows) is computed
once per token; the grid only toggles threshold masks and SL/TP scans on cached results.

Night strategy: entry window 00:10-01:00 UTC, ref = Binance open 00:10, time exit 03:00 UTC.
  Signal minute m: volume_points(live cumulative Upbit hour-0 volume vs prior hourly sums,
  lookback 24h - assumption, thresholds <=12 make any lookback >=12 equivalent) >= V and
  growth_points = floor(Binance close(m)/ref - 1, %) >= G. Fill next minute. 1 entry/token/day.

Day strategy: candidate candle 00:00 UTC (lookback 50, growth_points WITHOUT multiplier per
Stage A), entry strictly 04:00 UTC, reference = Upbit hourly open 00:00 (growth measured on
Upbit; TP anchored at implied Binance ref = binance_open04/(1+growth_upbit)), growth >= 3%.
Time exit D+1 14:00 UTC.

Costs: fee 0.04%/side (entry on notional, exit on qty*exit_px), slippage 0.05% on entry and
time-exit fills (not on SL/TP levels), funding strictly (entry, exit] scaled by mark price.
Same-bar SL+TP tie -> SL. All times UTC.
"""

import math
import time

import duckdb
import numpy as np
import pandas as pd

P = "data/parquet"
IS_START = pd.Timestamp("2026-01-01")
IS_END = pd.Timestamp("2026-06-01")            # exclusive
HOURLY_START = pd.Timestamp("2025-12-27")      # 50h lookback buffer only, no 2025 evaluation
DATA_END = pd.Timestamp("2026-06-01")

FEE_SIDE = 0.0004
SLIP = 0.0005
NOTIONAL = 1000.0
MIN_TRADES = 30
HOUR_MS = 3_600_000
MIN_MS = 60_000
IS_DAYS = (IS_END - IS_START).days  # 151

NIGHT_V = [1, 2, 3, 5, 8, 12]
NIGHT_G = [2, 3, 4, 5]
NIGHT_SL = [8, 9, 10, 11, 12]
NIGHT_TP = [45, 75, 90, 95]

DAY_VP = [20, 25, 30, 35, 40]
DAY_GP = [10, 15, 20, 25]
DAY_PP = [2, 3, 4, 5]
DAY_SL = [10, 11, 12, 13, 14]
DAY_TP = [45, 75, 90, 95]

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
    """Simulate one short from bar entry_i (inclusive) to deadline. Returns trade dict or None."""
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
    net = gross - fees + funding
    return dict(exit_ms=exit_ms, reason=reason, net=net)


# ------------------------------------------------------------------ precompute per token ----

def precompute():
    universe = con.sql(f"SELECT token FROM read_parquet('{P}/universe.parquet') ORDER BY token").df()["token"].tolist()

    hourly = con.sql(f"""
        SELECT token, date_trunc('hour', timestamp_utc) AS hour,
               first(open ORDER BY timestamp_utc) AS open, max(high) AS high, sum(volume) AS volume
        FROM read_parquet('{P}/upbit_1m/*/*.parquet', hive_partitioning=1)
        WHERE timestamp_utc >= TIMESTAMP '{HOURLY_START}' AND timestamp_utc < TIMESTAMP '{DATA_END}'
        GROUP BY token, hour
    """).df()
    hourly["hkey"] = hourly["hour"].values.astype("datetime64[h]").astype(np.int64)
    hourly_by_token = dict(tuple(hourly.groupby("token")))

    funding_df = con.sql(f"SELECT token, funding_time_utc, funding_rate FROM read_parquet('{P}/binance_funding.parquet') ORDER BY token, funding_time_utc").df()
    funding = {t: (g["funding_time_utc"].values.astype("datetime64[ms]").astype(np.int64), g["funding_rate"].values)
               for t, g in funding_df.groupby("token")}

    days = pd.date_range(IS_START, IS_END - pd.Timedelta(days=1), freq="1D")
    day_ms00 = days.values.astype("datetime64[ms]").astype(np.int64)
    day_hkeys = days.values.astype("datetime64[h]").astype(np.int64)
    data_end_ms = int(DATA_END.value // 1_000_000)

    night_signals = {}   # (token, di) -> {(V,G): entry_bar_minute_m}  (m = signal minute idx 10..59)
    night_results = {}   # (token, di, m) -> {(sl,tp): trade}
    day_candidates = []  # dict(token, di, vp, gp, pp)
    day_results = {}     # (token, di) -> {(sl,tp): trade}

    t0 = time.time()
    for n, token in enumerate(universe, 1):
        hdf = hourly_by_token.get(token)
        if hdf is None:
            continue
        hmap = {int(k): (o, h, v) for k, o, h, v in zip(hdf["hkey"], hdf["open"], hdf["high"], hdf["volume"])}
        f_times, f_rates = funding.get(token, (np.array([], dtype=np.int64), np.array([])))

        b = con.sql(f"""
            SELECT timestamp_utc, open, high, low, close FROM read_parquet('{P}/binance_1m/token={token}/*.parquet')
            WHERE timestamp_utc >= TIMESTAMP '{IS_START}' AND timestamp_utc < TIMESTAMP '{DATA_END}'
            ORDER BY timestamp_utc
        """).df()
        if b.empty:
            continue
        bts = b["timestamp_utc"].values.astype("datetime64[ms]").astype(np.int64)
        bo, bh, bl, bc = b["open"].values, b["high"].values, b["low"].values, b["close"].values

        u = con.sql(f"""
            SELECT timestamp_utc, open, close, volume FROM read_parquet('{P}/upbit_1m/token={token}/*.parquet')
            WHERE timestamp_utc >= TIMESTAMP '{IS_START}' AND timestamp_utc < TIMESTAMP '{DATA_END}'
              AND (hour(timestamp_utc) = 0 OR hour(timestamp_utc) = 3 OR
                   (hour(timestamp_utc) = 4 AND minute(timestamp_utc) = 0))
            ORDER BY timestamp_utc
        """).df()
        uts = u["timestamp_utc"].values.astype("datetime64[ms]").astype(np.int64)
        uo, ucl, uv = u["open"].values, u["close"].values, u["volume"].values

        for di in range(len(days)):
            t00 = int(day_ms00[di])
            hk = int(day_hkeys[di])

            # ---------- NIGHT ----------
            t0010 = t00 + 10 * MIN_MS
            iref = np.searchsorted(bts, t0010)
            if iref < len(bts) and bts[iref] == t0010:
                ref_px = bo[iref]
                # prior 24 hourly Upbit volume cumsums
                Pcum = np.zeros(24)
                cum = 0.0
                for k in range(1, 25):
                    r = hmap.get(hk - k)
                    cum += r[2] if r else 0.0
                    Pcum[k - 1] = cum
                # live cumulative Upbit volume per minute m=10..59 (inclusive of m)
                ia_u = np.searchsorted(uts, t00)
                ib_u = np.searchsorted(uts, t00 + HOUR_MS)
                mins_u = ((uts[ia_u:ib_u] - t00) // MIN_MS).astype(int)
                vol_by_min = np.zeros(60)
                np.add.at(vol_by_min, mins_u, uv[ia_u:ib_u])
                live = np.cumsum(vol_by_min)          # live[m] = vol from 00:00..m inclusive
                # vp_live[m]: count of prefix hours with Pcum < live (and Pcum>0 chain)
                mm = np.arange(10, 60)
                lv = live[mm]
                cond = (Pcum[None, :] < lv[:, None]) & (Pcum[None, :] > 0)
                vp_live = np.where(cond.all(axis=1), 24, np.argmin(cond, axis=1))
                # gp_live[m] = floor(binance close(m)/ref - 1, %)  (asof close)
                tmins = t00 + mm * MIN_MS
                ci = np.clip(np.searchsorted(bts, tmins + MIN_MS - 1, side="right") - 1, 0, len(bc) - 1)
                valid_close = bts[ci] >= t00  # ensure the asof close is from today, not stale
                gp_live = np.where(valid_close & (ref_px > 0),
                                   np.floor((bc[ci] / ref_px - 1) * 100), -999).astype(int)

                combos = {}
                for V in NIGHT_V:
                    for G in NIGHT_G:
                        ok = (vp_live >= V) & (gp_live >= G)
                        if ok.any():
                            combos[(V, G)] = int(mm[int(np.argmax(ok))])
                if combos:
                    night_signals[(token, di)] = combos
                    deadline = t00 + 3 * HOUR_MS
                    for m in set(combos.values()):
                        fill_ms = t00 + (m + 1) * MIN_MS
                        fi = np.searchsorted(bts, fill_ms)
                        # next available bar within 5 minutes as point-in-time fill
                        if fi >= len(bts) or bts[fi] > fill_ms + 5 * MIN_MS or bts[fi] >= deadline:
                            continue
                        entry_ms = int(bts[fi])
                        entry_px = bo[fi] * (1 - SLIP)
                        res = {}
                        for sl in NIGHT_SL:
                            for tp in NIGHT_TP:
                                sl_px = entry_px * (1 + sl / 100)
                                tp_px = entry_px - tp / 100 * (entry_px - ref_px)
                                tr = simulate_short(bts, bo, bh, bl, bc, fi, deadline, entry_px,
                                                    sl_px, tp_px, f_times, f_rates, entry_ms)
                                if tr:
                                    tr["entry_ms"] = entry_ms
                                    res[(sl, tp)] = tr
                        if res:
                            night_results[(token, di, m)] = res

            # ---------- DAY ----------
            pts = day_points(hmap, hk)
            if pts is None:
                continue
            vp, gp, pp = pts
            if vp < DAY_VP[0] or gp < DAY_GP[0] or pp < DAY_PP[0]:
                continue
            deadline = t00 + 38 * HOUR_MS
            if deadline > data_end_ms:
                continue
            row = hmap.get(hk)
            upbit_ref = row[0]
            if upbit_ref <= 0:
                continue
            # upbit price at 04:00: exact 04:00 open, else last close in hour 3
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
            res = {}
            for sl in DAY_SL:
                for tp in DAY_TP:
                    sl_px = entry_px * (1 + sl / 100)
                    tp_px = entry_px - tp / 100 * (entry_px - ref_imp)
                    tr = simulate_short(bts, bo, bh, bl, bc, fi, deadline, entry_px,
                                        sl_px, tp_px, f_times, f_rates, int(t04))
                    if tr:
                        tr["entry_ms"] = int(t04)
                        res[(sl, tp)] = tr
            if res:
                day_candidates.append(dict(token=token, di=di, vp=vp, gp=gp, pp=pp))
                day_results[(token, di)] = res

        if n % 40 == 0 or n == len(universe):
            print(f"  precompute {n}/{len(universe)} tokens, {time.time()-t0:.0f}s, "
                  f"night_days={len(night_signals)} day_cands={len(day_candidates)}", flush=True)

    return days, night_signals, night_results, day_candidates, day_results


# ------------------------------------------------------------------------------ metrics ----

def compute_metrics(trades, config_id):
    """trades: list of dicts with entry_ms, exit_ms, net."""
    n = len(trades)
    out = dict(n_trades=n)
    if n == 0:
        return out
    net = np.array([t["net"] for t in trades])
    entry = np.array([t["entry_ms"] for t in trades])
    exit_ = np.array([t["exit_ms"] for t in trades])
    reasons = pd.Series([t["reason"] for t in trades])

    out["expectancy_usdt"] = net.mean()
    out["expectancy_pct"] = net.mean() / NOTIONAL * 100
    rng = np.random.default_rng(1000 + config_id)
    idx = rng.integers(0, n, (1000, n))
    boot = net[idx].mean(axis=1)
    out["ci_low"] = float(np.percentile(boot, 2.5))
    out["ci_high"] = float(np.percentile(boot, 97.5))

    wins, losses = net[net > 0], net[net <= 0]
    out["win_rate"] = len(wins) / n * 100
    out["avg_win"] = wins.mean() if len(wins) else 0.0
    out["avg_loss"] = losses.mean() if len(losses) else 0.0
    out["profit_factor"] = wins.sum() / abs(losses.sum()) if len(losses) and losses.sum() != 0 else np.inf

    # peak concurrency via event sweep
    ev = sorted([(t, 1) for t in entry] + [(t, -1) for t in exit_])
    cur = peak = 0
    for _, dlt in ev:
        cur += dlt
        peak = max(peak, cur)
    base = peak * NOTIONAL
    out["peak_concurrent"] = peak
    out["total_net"] = net.sum()

    order = np.argsort(exit_)
    eq = base + np.cumsum(net[order])
    run_peak = np.maximum.accumulate(np.concatenate([[base], eq]))[1:]
    out["max_dd_pct"] = float(((run_peak - eq) / run_peak).max() * 100)
    out["cagr_pct"] = ((1 + net.sum() / base) ** (365 / IS_DAYS) - 1) * 100 if base > 0 else np.nan
    out["calmar"] = out["cagr_pct"] / out["max_dd_pct"] if out["max_dd_pct"] > 0 else np.inf

    daily = pd.Series(net, index=pd.to_datetime(exit_, unit="ms").date).groupby(level=0).sum()
    r = daily.values / base
    if len(r) > 1 and r.std() > 0:
        out["sharpe"] = r.mean() / r.std() * math.sqrt(365)
        dn = r[r < 0]
        out["sortino"] = r.mean() / dn.std() * math.sqrt(365) if len(dn) > 1 and dn.std() > 0 else np.inf
    else:
        out["sharpe"] = out["sortino"] = np.nan
    out["n_tp"] = int((reasons == "TP").sum())
    out["n_sl"] = int((reasons == "SL").sum())
    out["n_time"] = int((reasons == "TIME").sum())
    return out


def plateau_report(df, dims, score_col="ci_low"):
    """For each row: neighbor scores +-1 step per dimension."""
    lookup = df.set_index(dims)[score_col].to_dict()
    grids = {d: sorted(df[d].unique()) for d in dims}
    mins, means = [], []
    for _, r in df.iterrows():
        vals = []
        for d in dims:
            g = grids[d]
            i = g.index(r[d])
            for j in (i - 1, i + 1):
                if 0 <= j < len(g):
                    key = tuple(r[dd] if dd != d else g[j] for dd in dims)
                    v = lookup.get(key)
                    if v is not None and not np.isnan(v):
                        vals.append(v)
        mins.append(min(vals) if vals else np.nan)
        means.append(np.mean(vals) if vals else np.nan)
    df = df.copy()
    df["nbr_min"] = mins
    df["nbr_mean"] = means
    return df


# --------------------------------------------------------------------------------- main ----

def main():
    t0 = time.time()
    days, night_signals, night_results, day_candidates, day_results = precompute()
    cand_df = pd.DataFrame(day_candidates)
    print(f"\nPrecompute done in {time.time()-t0:.0f}s. Night signal-days: {len(night_signals)}, "
          f"day candidates: {len(cand_df)}")

    # ---- NIGHT grid ----
    rows = []
    cfg_id = 0
    night_trades_store = {}
    for V in NIGHT_V:
        for G in NIGHT_G:
            base_trades = []
            for (token, di), combos in night_signals.items():
                m = combos.get((V, G))
                if m is None:
                    continue
                res = night_results.get((token, di, m))
                if res:
                    base_trades.append(res)
            for sl in NIGHT_SL:
                for tp in NIGHT_TP:
                    trades = [r[(sl, tp)] for r in base_trades if (sl, tp) in r]
                    met = compute_metrics(trades, cfg_id)
                    rows.append(dict(V=V, G=G, sl=sl, tp=tp, **met))
                    night_trades_store[(V, G, sl, tp)] = trades
                    cfg_id += 1
    night_grid = pd.DataFrame(rows)
    night_grid["eligible"] = night_grid["n_trades"] >= MIN_TRADES
    night_grid.to_csv("results/grid_night.csv", index=False)

    # ---- DAY grid ----
    rows = []
    day_trades_store = {}
    cand_sorted = cand_df.sort_values(["token", "di"]).reset_index(drop=True) if not cand_df.empty else cand_df
    for vpm in DAY_VP:
        for gpm in DAY_GP:
            for ppm in DAY_PP:
                sel = cand_sorted[(cand_sorted.vp >= vpm) & (cand_sorted.gp >= gpm) & (cand_sorted.pp >= ppm)]
                for sl in DAY_SL:
                    for tp in DAY_TP:
                        trades = []
                        open_until = {}
                        for r in sel.itertuples():
                            res = day_results[(r.token, r.di)].get((sl, tp))
                            if res is None:
                                continue
                            if res["entry_ms"] < open_until.get(r.token, -1):
                                continue  # token busy
                            open_until[r.token] = res["exit_ms"]
                            trades.append(res)
                        met = compute_metrics(trades, cfg_id)
                        rows.append(dict(vp=vpm, gp=gpm, pp=ppm, sl=sl, tp=tp, **met))
                        day_trades_store[(vpm, gpm, ppm, sl, tp)] = trades
                        cfg_id += 1
    day_grid = pd.DataFrame(rows)
    day_grid["eligible"] = day_grid["n_trades"] >= MIN_TRADES
    day_grid.to_csv("results/grid_day.csv", index=False)

    print(f"\nGrids done in {time.time()-t0:.0f}s. Night configs: {len(night_grid)} "
          f"(eligible {night_grid.eligible.sum()}), Day: {len(day_grid)} (eligible {day_grid.eligible.sum()})")

    # ---- selection with plateau ----
    results = {}
    for name, grid, dims, store in [("night", night_grid, ["V", "G", "sl", "tp"], night_trades_store),
                                    ("day", day_grid, ["vp", "gp", "pp", "sl", "tp"], day_trades_store)]:
        g = plateau_report(grid, dims)
        elig = g[g.eligible & g.ci_low.notna()].sort_values("ci_low", ascending=False)
        top20 = elig.head(20)
        cols = dims + ["n_trades", "expectancy_usdt", "ci_low", "ci_high", "win_rate", "profit_factor",
                       "total_net", "cagr_pct", "max_dd_pct", "sharpe", "n_tp", "n_sl", "n_time",
                       "nbr_min", "nbr_mean", "peak_concurrent"]
        print(f"\n===== {name.upper()} TOP-20 by bootstrap CI lower bound =====")
        print(top20[cols].to_string(index=False))
        top20[cols].to_csv(f"results/top20_{name}.csv", index=False)

        if top20.empty:
            print(f"!! no eligible configs for {name}")
            continue
        # winner: prefer plateau — highest neighbor-min among top-20; tie-break by own score
        w = top20.sort_values(["nbr_min", "ci_low"], ascending=False).iloc[0]
        best = top20.iloc[0]
        print(f"\n{name} winner: {dict((d, w[d]) for d in dims)}  "
              f"ci_low={w.ci_low:.2f} nbr_min={w.nbr_min:.2f} (top-1 was ci_low={best.ci_low:.2f} nbr_min={best.nbr_min:.2f})")
        key = tuple(w[d] for d in dims)
        trades = store[key]
        pd.DataFrame(trades).to_csv(f"results/chosen_trades_{name}.csv", index=False)
        results[name] = dict(config=dict((d, int(w[d]) if isinstance(w[d], (int, np.integer)) else w[d]) for d in dims),
                             ci_low=float(w.ci_low), n=int(w.n_trades))
    print(f"\nTotal elapsed {time.time()-t0:.0f}s")
    return results


if __name__ == "__main__":
    main()
