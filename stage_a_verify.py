"""Stage A: verify the day-short engine 1:1 against reference trades.csv after 3 fixes:
1. growth_points: no 1.05 multiplier (break when high <= running_max), lookback 50h (vp too).
2. Universe = unique tokens from reference trades.csv (removes survivorship diffs).
3. Funding: strictly (entry, exit], scaled by mark price (rate * mark_t / entry).

Reference params: vp>=30, gp>=20, pp>=3, entry 04:00 UTC, exit D+1 14:00 UTC,
SL 13%, TP 90%-capture, flat fee 0.0008, no slippage (matching the reference cost model).
Period 2025-06-01 -> 2026-06-01. All times UTC.
"""

import math
import time

import duckdb
import numpy as np
import pandas as pd

P = "data/parquet"
PERIOD_START = pd.Timestamp("2025-06-01")
PERIOD_END = pd.Timestamp("2026-06-01")
HOURLY_START = pd.Timestamp("2025-05-25")
DATA_END = pd.Timestamp("2026-06-01")

FEE = 0.0008
LOOKBACK = 50
VP_MIN, GP_MIN, PP_MIN = 30, 20, 3
GROWTH_MIN = 0.03
SL_MULT = 1.13
TP_CAPTURE = 0.90
HOUR_MS = 3_600_000
MIN_MS = 60_000

con = duckdb.connect()
con.execute("PRAGMA threads=8")


def compute_points(hmap, hk):
    row = hmap.get(hk)
    if row is None:
        return None
    o, hi, v = row
    pp = 0 if o <= 0 or (hi - o) / o * 100 <= 3 else math.ceil((hi - o) / o * 100 - 3)
    cum = 0.0
    vp = 0
    for k in range(1, LOOKBACK + 1):
        r = hmap.get(hk - k)
        cum += r[2] if r else 0.0
        if cum <= 0 or v <= cum:
            break
        vp = k
    rm = 0.0
    gp = 0
    for k in range(1, LOOKBACK + 1):
        r = hmap.get(hk - k)
        rm = max(rm, r[1] if r else 0.0)
        if rm <= 0 or hi <= rm:  # fix 1: no multiplier
            break
        gp = k
    return vp, gp, pp


def main():
    t0 = time.time()
    ref = pd.read_csv("data/reference/trades.csv")
    tokens = sorted({s[:-4] for s in ref["symbol"]})  # strip USDT
    print(f"Universe from reference: {len(tokens)} tokens")

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

    days = pd.date_range(PERIOD_START, PERIOD_END - pd.Timedelta(days=1), freq="1D")
    day_hkeys = days.values.astype("datetime64[h]").astype(np.int64)
    day_ms00 = days.values.astype("datetime64[ms]").astype(np.int64)
    data_end_ms = int(DATA_END.value // 1_000_000)

    trades = []
    no_data_tokens = []
    for token in tokens:
        hdf = hourly_by_token.get(token)
        b = con.sql(f"""
            SELECT timestamp_utc, open, high, low, close FROM read_parquet('{P}/binance_1m/token={token}/*.parquet')
            WHERE timestamp_utc >= TIMESTAMP '{PERIOD_START}' AND timestamp_utc < TIMESTAMP '{DATA_END}'
            ORDER BY timestamp_utc
        """).df() if hdf is not None else None
        if hdf is None or b is None or b.empty:
            no_data_tokens.append(token)
            continue
        hmap = {int(k): (o, h, v) for k, o, h, v in zip(hdf["hkey"], hdf["open"], hdf["high"], hdf["volume"])}
        bts = b["timestamp_utc"].values.astype("datetime64[ms]").astype(np.int64)
        bo, bh, bl, bc = b["open"].values, b["high"].values, b["low"].values, b["close"].values
        f_times, f_rates = funding.get(token, (np.array([], dtype=np.int64), np.array([])))

        open_until = -1
        for di in range(len(days)):
            pts = compute_points(hmap, int(day_hkeys[di]))
            if pts is None:
                continue
            vp, gp, pp = pts
            if not (vp >= VP_MIN and gp >= GP_MIN and pp >= PP_MIN):
                continue
            t00 = int(day_ms00[di])
            t04 = t00 + 4 * HOUR_MS
            deadline = t00 + 38 * HOUR_MS
            if deadline > data_end_ms:
                continue

            i00 = np.searchsorted(bts, t00)
            i04 = np.searchsorted(bts, t04)
            if i00 >= len(bts) or bts[i00] != t00 or i04 >= len(bts) or bts[i04] != t04:
                continue
            ref_px, ent_px = bo[i00], bo[i04]
            if ref_px <= 0 or ent_px / ref_px - 1 < GROWTH_MIN:
                continue
            # reference engine allowed overlapping same-token positions (SONIC 2026-02-18/19 overlap)

            i_end = np.searchsorted(bts, deadline)
            sl_px = ent_px * SL_MULT
            tp_px = ent_px - TP_CAPTURE * (ent_px - ref_px)
            seg_h, seg_l = bh[i04:i_end], bl[i04:i_end]
            hits = (seg_h >= sl_px) | (seg_l <= tp_px)
            if hits.any():
                j = int(np.argmax(hits))
                exit_ms = int(bts[i04 + j]) + MIN_MS - 1
                if seg_h[j] >= sl_px:
                    exit_px, reason = sl_px, "SL"
                else:
                    exit_px, reason = tp_px, "TP"
            elif i_end < len(bts) and bts[i_end] == deadline:
                exit_px, reason, exit_ms = bo[i_end], "TIME_EXIT", deadline
            elif i_end > i04:
                exit_px, reason, exit_ms = bc[i_end - 1], "TIME_EXIT", int(bts[i_end - 1]) + MIN_MS - 1
            else:
                continue
            open_until = exit_ms

            gross = (ent_px - exit_px) / ent_px
            # fix 3: funding strictly (entry, exit], scaled by mark price
            ia = np.searchsorted(f_times, t04, side="right")
            ib = np.searchsorted(f_times, exit_ms, side="right")
            ft, fr = f_times[ia:ib], f_rates[ia:ib]
            if len(ft):
                mi = np.clip(np.searchsorted(bts, ft, side="right") - 1, 0, len(bc) - 1)
                fund = float((fr * bc[mi] / ent_px).sum())
            else:
                fund = 0.0
            net = gross - FEE + fund
            trades.append(dict(symbol=token + "USDT", date=days[di].strftime("%Y-%m-%d"),
                               vp=vp, gp=gp, pp=pp, entry_price=ent_px, exit_price=exit_px,
                               exit_reason=reason, funding_ret=fund, funding_events=len(ft),
                               net_ret_usdt=net * 1000.0))

    ours = pd.DataFrame(trades)
    ours.to_csv("results/stage_a_trades.csv", index=False)

    # ---- diff ----
    ref["key"] = ref["symbol"] + "|" + ref["date"]
    ours["key"] = ours["symbol"] + "|" + ours["date"]
    ref_d = ref.drop_duplicates("key")
    matched = set(ref_d.key) & set(ours.key)
    missing = sorted(set(ref_d.key) - set(ours.key))
    extra = sorted(set(ours.key) - set(ref_d.key))

    print(f"\nno-data tokens (in ref universe, absent in our parquet): {no_data_tokens}")
    print(f"\n{'':22}{'ours':>12}{'reference':>12}")
    print(f"{'trades':22}{len(ours):>12}{len(ref):>12}")
    ec_o = ours.exit_reason.value_counts()
    ec_r = ref.exit_reason.value_counts()
    for r in ["TP", "SL", "TIME_EXIT"]:
        print(f"{'exit '+r:22}{ec_o.get(r,0):>12}{ec_r.get(r,0):>12}")
    print(f"{'net PnL USDT':22}{ours.net_ret_usdt.sum():>12.2f}{ref.net_ret_usdt.sum():>12.2f}")
    pnl_diff = abs(ours.net_ret_usdt.sum() - ref.net_ret_usdt.sum()) / abs(ref.net_ret_usdt.sum()) * 100
    cnt_diff = abs(len(ours) - len(ref)) / len(ref) * 100
    print(f"\nresidual: trades {cnt_diff:.2f}%  pnl {pnl_diff:.2f}%")
    print(f"matched keys {len(matched)}/{len(ref_d)}  missing {len(missing)}  extra {len(extra)}")

    if missing:
        print("\nmissing:", missing[:25])
    if extra:
        print("extra:", extra[:25])

    # per-matched funding accuracy
    m = ours.merge(ref_d[["key", "net_ret_usdt", "funding_ret", "funding_events"]], on="key", suffixes=("", "_ref"))
    print(f"\nmatched net pnl: ours {m.net_ret_usdt.sum():.2f} vs ref {m.net_ret_usdt_ref.sum():.2f} "
          f"({abs(m.net_ret_usdt.sum()-m.net_ret_usdt_ref.sum())/abs(m.net_ret_usdt_ref.sum())*100:.2f}% off)")
    print(f"funding events equal: {(m.funding_events == m.funding_events_ref).mean()*100:.1f}%")
    print(f"net within 1 USDT: {((m.net_ret_usdt - m.net_ret_usdt_ref).abs() < 1).mean()*100:.1f}%")
    print(f"\nElapsed {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
