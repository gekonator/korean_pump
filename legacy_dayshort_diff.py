"""Replay the legacy day-short strategy (single fixed 04:00 UTC entry, no kimchi trigger)
on data/parquet/ and diff row-by-row against the author's reference trades.csv.

Price source note (verified against the data, see run output): reference start_price /
entry_price match Binance futures 1m opens at 00:00 / 04:00 UTC exactly, so Binance is
used for both despite the spec text saying "Upbit hourly candle open". The candidate
filter (volume/growth/pump points) stays on Upbit hourly candles per the spec.
"""

import math
import time

import duckdb
import numpy as np
import pandas as pd

P = "data/parquet"
PERIOD_START = pd.Timestamp("2025-06-01")
PERIOD_END = pd.Timestamp("2026-06-01")      # exclusive: candidate days
HOURLY_START = pd.Timestamp("2025-05-28")    # >40h lookback buffer
DATA_END = pd.Timestamp("2026-06-01")

FEE = 0.0008          # flat 0.08% of notional per round trip (matches reference fee_ret)
NOTIONAL = 1000.0
LOOKBACK = 40
VP_MIN, GP_MIN, PP_MIN = 30, 20, 3
GROWTH_MIN = 0.03
SL_MULT = 1.13
TP_CAPTURE = 0.90

HOUR_MS = 3_600_000
MIN_MS = 60_000

con = duckdb.connect()
con.execute("PRAGMA threads=8")


def load_upbit_hourly():
    df = con.sql(f"""
        SELECT token, date_trunc('hour', timestamp_utc) AS hour,
               first(open ORDER BY timestamp_utc) AS open,
               max(high) AS high,
               sum(volume) AS volume
        FROM read_parquet('{P}/upbit_1m/*/*.parquet', hive_partitioning=1)
        WHERE timestamp_utc >= TIMESTAMP '{HOURLY_START}' AND timestamp_utc < TIMESTAMP '{DATA_END}'
        GROUP BY token, hour
    """).df()
    df["hkey"] = df["hour"].values.astype("datetime64[h]").astype(np.int64)
    return df


def compute_points(hmap, target_hkey):
    row = hmap.get(target_hkey)
    if row is None:
        return None
    o, hi, v = row

    if o <= 0:
        pp = 0
    else:
        pct = (hi - o) / o * 100
        pp = 0 if pct <= 3 else math.ceil(pct - 3)

    cum = 0.0
    vp = 0
    for k in range(1, LOOKBACK + 1):
        r = hmap.get(target_hkey - k)
        cum += r[2] if r else 0.0
        if cum <= 0 or v <= cum:
            break
        vp = k

    rm = 0.0
    gp = 0
    for k in range(1, LOOKBACK + 1):
        r = hmap.get(target_hkey - k)
        rm = max(rm, r[1] if r else 0.0)
        if rm <= 0 or hi <= rm * 1.05:
            break
        gp = k
    return vp, gp, pp


def load_binance_arrays(token):
    df = con.sql(f"""
        SELECT timestamp_utc, open, high, low, close
        FROM read_parquet('{P}/binance_1m/token={token}/*.parquet')
        WHERE timestamp_utc >= TIMESTAMP '{PERIOD_START}' AND timestamp_utc < TIMESTAMP '{DATA_END}'
        ORDER BY timestamp_utc
    """).df()
    if df.empty:
        return None
    ts = df["timestamp_utc"].values.astype("datetime64[ms]").astype(np.int64)
    return ts, df["open"].values, df["high"].values, df["low"].values, df["close"].values


def open_at(b, ts_ms):
    ts, o = b[0], b[1]
    i = np.searchsorted(ts, ts_ms)
    if i < len(ts) and ts[i] == ts_ms:
        return o[i]
    return None


def simulate(b, entry_ms, exit_deadline_ms, entry_px, ref_px):
    ts, o, h, l, c = b
    i0 = np.searchsorted(ts, entry_ms)
    if i0 >= len(ts) or ts[i0] != entry_ms:
        return None
    i_end = np.searchsorted(ts, exit_deadline_ms)

    sl_px = entry_px * SL_MULT
    tp_px = entry_px - TP_CAPTURE * (entry_px - ref_px)

    seg_h = h[i0:i_end]
    seg_l = l[i0:i_end]
    hits = (seg_h >= sl_px) | (seg_l <= tp_px)
    if hits.any():
        j = int(np.argmax(hits))
        bar_ms = int(ts[i0 + j])
        if seg_h[j] >= sl_px:  # same-bar tie resolves to SL
            return sl_px, "SL", bar_ms + MIN_MS - 1, sl_px, tp_px
        return tp_px, "TP", bar_ms + MIN_MS - 1, sl_px, tp_px

    if i_end < len(ts) and ts[i_end] == exit_deadline_ms:
        return o[i_end], "TIME_EXIT", exit_deadline_ms, sl_px, tp_px
    if i_end > i0:
        return c[i_end - 1], "TIME_EXIT", int(ts[i_end - 1]) + MIN_MS - 1, sl_px, tp_px
    return None


def main():
    t0 = time.time()

    universe = con.sql(f"SELECT * FROM read_parquet('{P}/universe.parquet')").df()
    print(f"{len(universe)} tokens in universe")

    print("Building Upbit hourly aggregates...", flush=True)
    hourly = load_upbit_hourly()

    funding_df = con.sql(f"SELECT token, funding_time_utc, funding_rate FROM read_parquet('{P}/binance_funding.parquet') ORDER BY token, funding_time_utc").df()
    funding = {}
    for tok, g in funding_df.groupby("token"):
        funding[tok] = (g["funding_time_utc"].values.astype("datetime64[ms]").astype(np.int64), g["funding_rate"].values)

    days = pd.date_range(PERIOD_START, PERIOD_END - pd.Timedelta(days=1), freq="1D")
    day_hkeys = days.values.astype("datetime64[h]").astype(np.int64)
    day_ms00 = days.values.astype("datetime64[ms]").astype(np.int64)

    trades = []
    reasons = {}   # (symbol, date_str) -> info dict

    hourly_by_token = dict(tuple(hourly.groupby("token")))

    for n, urow in enumerate(universe.itertuples(), 1):
        token, symbol = urow.token, urow.binance_symbol

        hdf = hourly_by_token.get(token)
        if hdf is None:
            for d in days:
                reasons[symbol + "|" + d.strftime("%Y-%m-%d")] = {"reason": "no_upbit_data"}
            continue
        hmap = {int(k): (o, h, v) for k, o, h, v in zip(hdf["hkey"], hdf["open"], hdf["high"], hdf["volume"])}

        b = load_binance_arrays(token)
        f_times, f_rates = funding.get(token, (np.array([], dtype=np.int64), np.array([])))

        open_until = -1
        for di, d in enumerate(days):
            key = symbol + "|" + d.strftime("%Y-%m-%d")
            pts = compute_points(hmap, int(day_hkeys[di]))
            if pts is None:
                reasons[key] = {"reason": "no_upbit_candle"}
                continue
            vp, gp, pp = pts
            info = {"vp": vp, "gp": gp, "pp": pp}
            if not (vp >= VP_MIN and gp >= GP_MIN and pp >= PP_MIN):
                reasons[key] = {"reason": "filter_fail", **info}
                continue

            t00 = int(day_ms00[di])
            t04 = t00 + 4 * HOUR_MS
            exit_deadline = t00 + 38 * HOUR_MS  # next day 14:00 UTC
            if exit_deadline > int(DATA_END.value // 1_000_000):
                reasons[key] = {"reason": "beyond_data", **info}
                continue
            if b is None:
                reasons[key] = {"reason": "no_binance_data", **info}
                continue

            ref_px = open_at(b, t00)
            ent_px = open_at(b, t04)
            if ref_px is None or ent_px is None or ref_px <= 0:
                reasons[key] = {"reason": "no_binance_bar", **info}
                continue

            growth = ent_px / ref_px - 1
            if growth < GROWTH_MIN:
                reasons[key] = {"reason": "growth_fail", "growth": growth, **info}
                continue
            if t04 < open_until:
                reasons[key] = {"reason": "token_busy", "growth": growth, **info}
                continue

            sim = simulate(b, t04, exit_deadline, ent_px, ref_px)
            if sim is None:
                reasons[key] = {"reason": "no_entry_bar", **info}
                continue
            exit_px, exit_reason, exit_ms, sl_px, tp_px = sim
            open_until = exit_ms

            gross_ret = (ent_px - exit_px) / ent_px
            ia = np.searchsorted(f_times, t04, side="left")
            ib = np.searchsorted(f_times, exit_ms, side="right")
            funding_ret = float(f_rates[ia:ib].sum())
            n_funding = int(ib - ia)
            net_ret = gross_ret - FEE + funding_ret

            trades.append(dict(
                symbol=symbol, token=token, date=key.split("|")[1],
                vp=vp, gp=gp, pp=pp,
                start_price=ref_px, entry_price=ent_px, exit_price=exit_px,
                sl_price=sl_px, tp_price=tp_px, actual_growth=growth,
                exit_reason=exit_reason, exit_ms=exit_ms,
                gross_ret=gross_ret, funding_ret=funding_ret, funding_events=n_funding,
                net_ret=net_ret, net_ret_usdt=net_ret * NOTIONAL,
            ))
            reasons[key] = {"reason": "traded", **info}

        if n % 40 == 0 or n == len(universe):
            print(f"  {n}/{len(universe)} tokens, trades so far {len(trades)}, {time.time()-t0:.0f}s", flush=True)

    ours = pd.DataFrame(trades)
    ours.to_csv("results/our_legacy_trades.csv", index=False)
    print(f"\nOur trades: {len(ours)}, net {ours.net_ret_usdt.sum():.2f} USDT, "
          f"winrate {(ours.net_ret_usdt > 0).mean()*100:.1f}%, "
          f"exits {ours.exit_reason.value_counts().to_dict()}")

    # ---------------- diff vs reference ----------------
    ref = pd.read_csv("data/reference/trades.csv")
    ref["key"] = ref["symbol"] + "|" + ref["date"]
    n_dupes = int(ref["key"].duplicated().sum())
    ref_d = ref.drop_duplicates("key", keep="first").set_index("key")
    ours["key"] = ours["symbol"] + "|" + ours["date"]
    ours_d = ours.set_index("key")

    ref_keys = set(ref_d.index)
    our_keys = set(ours_d.index)
    missing = sorted(ref_keys - our_keys)
    extra = sorted(our_keys - ref_keys)
    matched = sorted(ref_keys & our_keys)

    print(f"\nReference rows: {len(ref)} ({n_dupes} duplicate key(s) collapsed -> {len(ref_d)})")
    print(f"Matched: {len(matched)}  Missing (ref only): {len(missing)}  Extra (ours only): {len(extra)}")

    print("\n--- MISSING (first 20): ref points [capped at 40 for fair compare] vs our recomputed ---")
    rows = []
    for k in missing[:20]:
        r = ref_d.loc[k]
        info = reasons.get(k, {"reason": "day_not_evaluated"})
        rows.append(dict(
            symbol=k.split("|")[0], date=k.split("|")[1], ref_market=r["token"],
            ref_vp=min(r["volume_points"], 40), ref_gp=min(r["growth_points"], 40), ref_pp=r["pump_points"],
            our_reason=info.get("reason"),
            our_vp=info.get("vp"), our_gp=info.get("gp"), our_pp=info.get("pp"),
            our_growth=info.get("growth"),
        ))
    print(pd.DataFrame(rows).to_string(index=False))

    reason_counts = {}
    for k in missing:
        rsn = reasons.get(k, {"reason": "day_not_evaluated"})["reason"]
        reason_counts[rsn] = reason_counts.get(rsn, 0) + 1
    print(f"\nMissing-trade reason breakdown: {reason_counts}")

    if extra:
        print("\n--- EXTRA (ours, not in ref) ---")
        print(ours_d.loc[extra, ["vp", "gp", "pp", "actual_growth", "exit_reason", "net_ret_usdt"]].to_string())

    # matched row-by-row compare
    cmp_rows = []
    for k in matched:
        r, o = ref_d.loc[k], ours_d.loc[k]
        e_ok = abs(o["entry_price"] - r["entry_price"]) / r["entry_price"] <= 0.001
        x_ok = abs(o["exit_price"] - r["exit_price"]) / r["exit_price"] <= 0.001
        rsn_ok = o["exit_reason"] == r["exit_reason"]
        cmp_rows.append(dict(
            symbol=k.split("|")[0], date=k.split("|")[1],
            ref_entry=r["entry_price"], our_entry=o["entry_price"], entry_ok=e_ok,
            ref_exit=r["exit_price"], our_exit=o["exit_price"], exit_ok=x_ok,
            ref_reason=r["exit_reason"], our_reason=o["exit_reason"], reason_ok=rsn_ok,
            ref_net=r["net_ret_usdt"], our_net=o["net_ret_usdt"],
            net_diff=o["net_ret_usdt"] - r["net_ret_usdt"],
        ))
    cmp = pd.DataFrame(cmp_rows)
    cmp.to_csv("results/legacy_diff_matched.csv", index=False)

    full_ok = cmp["entry_ok"] & cmp["exit_ok"] & cmp["reason_ok"]
    print(f"\n--- MATCHED comparison ({len(cmp)} trades) ---")
    print(f"entry_price within 0.1%: {cmp.entry_ok.mean()*100:.1f}%")
    print(f"exit_price within 0.1%:  {cmp.exit_ok.mean()*100:.1f}%")
    print(f"exit_reason equal:       {cmp.reason_ok.mean()*100:.1f}%")
    print(f"all three:               {full_ok.mean()*100:.1f}%")
    print(f"net pnl on matched: ours {cmp.our_net.sum():.2f} vs ref {cmp.ref_net.sum():.2f} USDT")

    print("\n--- 10 worst matched discrepancies by |net diff| ---")
    worst = cmp.reindex(cmp.net_diff.abs().sort_values(ascending=False).index).head(10)
    print(worst.to_string(index=False))

    print("\n=== AGGREGATE ===")
    print(f"{'':16}{'ours':>12}{'reference':>12}")
    print(f"{'trades':16}{len(ours):>12}{len(ref):>12}")
    print(f"{'net pnl USDT':16}{ours.net_ret_usdt.sum():>12.2f}{ref.net_ret_usdt.sum():>12.2f}")
    print(f"{'winrate %':16}{(ours.net_ret_usdt > 0).mean()*100:>12.1f}{(ref.net_ret_usdt > 0).mean()*100:>12.1f}")
    print(f"\n(a) filter/selection: recall {len(matched)/len(ref_d)*100:.1f}% of ref trades reproduced, "
          f"{len(extra)} extra")
    print(f"(b) price accuracy on matched: {full_ok.mean()*100:.1f}% fully matching rows")
    print(f"\nElapsed: {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
