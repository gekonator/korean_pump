"""Canonical engine for the frozen day-short strategy. Single source of truth.

Every research script imports the signal formulas, the trade simulator and the
data loaders from here; no other file may redefine them. The frozen H1-validated
configuration is DAY_CFG. All times UTC.

Frozen day config: volume_points >= 40, growth_points >= 15, pump_points >= 5
(hourly Upbit candle 00:00-01:00 UTC, lookback 50 h, growth_points without a
breakout multiplier), Upbit growth open00 -> 04:00 >= 3%, short Binance perp at
the 04:00 UTC open, SL 13%, TP 90%-capture of the implied move, time exit
D+1 14:00 UTC, one open position per token, no concurrency cap.

Cost model: taker fee 0.04%/side, slippage 0.05% on market fills (entry and
time exit; SL/TP levels are the modeled fills), funding accrued over
(entry, exit] scaled by the mark price.
"""

import math

import duckdb
import numpy as np
import pandas as pd

PARQUET = "data/parquet"
FEE_SIDE = 0.0004
SLIP = 0.0005
NOTIONAL = 1000.0
HOUR_MS = 3_600_000
MIN_MS = 60_000
LOOKBACK = 50

DAY_CFG = dict(vp=40, gp=15, pp=5, sl=13, tp=90)

_con = duckdb.connect()
_con.execute("PRAGMA threads=8")


def sql(query):
    return _con.sql(query).df()


# ------------------------------------------------------------------- signal formulas ----

def day_points(hmap, hk, lookback=LOOKBACK):
    """(volume_points, growth_points, pump_points) for the hourly candle at hour-key hk.

    hmap: {epoch_hour: (open, high, quote_volume)} built from Upbit 1m data.
    Missing hours contribute zero volume / zero high. Strict inequalities:
    the target hour must strictly exceed the cumulative volume / running max
    of prior hours for a point to be scored.
    """
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


# ---------------------------------------------------------------------- simulation ----

def simulate_short(bts, bo, bh, bl, bc, entry_i, deadline_ms, entry_px, sl_px, tp_px,
                   f_times, f_rates, entry_ms):
    """Simulate one short from bar entry_i (inclusive) to the time-exit deadline.

    Same-bar SL+TP tie resolves to SL (conservative). Funding is accrued over
    (entry, exit], each event scaled by the as-of mark (1m close). Returns a
    trade dict or None when no bars are available.
    """
    i_end = np.searchsorted(bts, deadline_ms)
    seg_h, seg_l = bh[entry_i:i_end], bl[entry_i:i_end]
    hits = (seg_h >= sl_px) | (seg_l <= tp_px)
    if hits.any():
        j = int(np.argmax(hits))
        exit_ms = int(bts[entry_i + j]) + MIN_MS - 1
        exit_px, reason = (sl_px, "SL") if seg_h[j] >= sl_px else (tp_px, "TP")
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


# --------------------------------------------------------------------- data loaders ----

def load_universe():
    return sql(f"SELECT token FROM read_parquet('{PARQUET}/universe.parquet') ORDER BY token")["token"].tolist()


def load_hourly_maps(start, end):
    """Per-token {epoch_hour: (open, high, quote_volume)} from Upbit 1m data.

    Hourly open = open of the first traded minute of the hour; volume is the
    KRW quote volume. `start` may include a lookback buffer before the
    evaluation period.
    """
    df = sql(f"""
        SELECT token, date_trunc('hour', timestamp_utc) AS hour,
               first(open ORDER BY timestamp_utc) AS open, max(high) AS high, sum(volume) AS volume
        FROM read_parquet('{PARQUET}/upbit_1m/*/*.parquet', hive_partitioning=1)
        WHERE timestamp_utc >= TIMESTAMP '{start}' AND timestamp_utc < TIMESTAMP '{end}'
        GROUP BY token, hour
    """)
    df["hkey"] = df["hour"].values.astype("datetime64[h]").astype(np.int64)
    return {t: {int(k): (o, h, v) for k, o, h, v in zip(g.hkey, g.open, g.high, g.volume)}
            for t, g in df.groupby("token")}


def load_funding():
    df = sql(f"SELECT token, funding_time_utc, funding_rate FROM read_parquet('{PARQUET}/binance_funding.parquet') ORDER BY token, funding_time_utc")
    return {t: (g["funding_time_utc"].values.astype("datetime64[ms]").astype(np.int64), g["funding_rate"].values)
            for t, g in df.groupby("token")}


def load_binance_1m(token, start, end):
    df = sql(f"""
        SELECT timestamp_utc, open, high, low, close FROM read_parquet('{PARQUET}/binance_1m/token={token}/*.parquet')
        WHERE timestamp_utc >= TIMESTAMP '{start}' AND timestamp_utc < TIMESTAMP '{end}'
        ORDER BY timestamp_utc
    """)
    if df.empty:
        return None
    return (df["timestamp_utc"].values.astype("datetime64[ms]").astype(np.int64),
            df["open"].values, df["high"].values, df["low"].values, df["close"].values)


def load_upbit_gate_minutes(token, start, end):
    """Upbit 1m bars needed for the 3% entry gate: hour 03 plus the 04:00 bar."""
    df = sql(f"""
        SELECT timestamp_utc, open, close FROM read_parquet('{PARQUET}/upbit_1m/token={token}/*.parquet')
        WHERE timestamp_utc >= TIMESTAMP '{start}' AND timestamp_utc < TIMESTAMP '{end}'
          AND (hour(timestamp_utc) = 3 OR (hour(timestamp_utc) = 4 AND minute(timestamp_utc) = 0))
        ORDER BY timestamp_utc
    """)
    return (df["timestamp_utc"].values.astype("datetime64[ms]").astype(np.int64),
            df["open"].values, df["close"].values)


def upbit_price_at_0400(uts, uo, ucl, t00):
    """Upbit price for the entry gate: the 04:00 bar open, else the last 1m close
    within 03:00-04:00, else None (no signal). Returns (price, source_tag)."""
    t04 = t00 + 4 * HOUR_MS
    iu = np.searchsorted(uts, t04)
    if iu < len(uts) and uts[iu] == t04:
        return uo[iu], "open@04:00"
    ja = np.searchsorted(uts, t00 + 3 * HOUR_MS)
    jb = np.searchsorted(uts, t04)
    if jb > ja:
        return ucl[jb - 1], "last_close_03xx"
    return None, None


# ------------------------------------------------------------------ signal pipeline ----

def day_signals(start, end, hourly_start=None, cfg=DAY_CFG, keep_details=False):
    """All frozen-config day signals in [start, end) with the busy rule applied.

    hourly_start: start of the hourly aggregation, allowing a >= 50h lookback
    buffer before the evaluation period (pass None when the data begins exactly
    at `start` and no buffer exists). keep_details adds every intermediate
    detector value plus busy-skipped rows (for parity checks).
    """
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    hourly_start = pd.Timestamp(hourly_start) if hourly_start else start
    data_end_ms = int(end.value // 1_000_000)

    hourly_maps = load_hourly_maps(hourly_start, end)
    funding = load_funding()
    days = pd.date_range(start, end - pd.Timedelta(days=1), freq="1D")
    day_ms00 = days.values.astype("datetime64[ms]").astype(np.int64)
    day_hkeys = days.values.astype("datetime64[h]").astype(np.int64)

    raw = []
    for token in load_universe():
        hmap = hourly_maps.get(token)
        if hmap is None:
            continue
        b = load_binance_1m(token, start, end)
        if b is None:
            continue
        bts, bo, bh, bl, bc = b
        f_times, f_rates = funding.get(token, (np.array([], dtype=np.int64), np.array([])))
        uts, uo, ucl = load_upbit_gate_minutes(token, start, end)

        for di in range(len(days)):
            t00 = int(day_ms00[di])
            pts = day_points(hmap, int(day_hkeys[di]))
            if pts is None:
                continue
            vp, gp, pp = pts
            if not (vp >= cfg["vp"] and gp >= cfg["gp"] and pp >= cfg["pp"]):
                continue
            deadline = t00 + 38 * HOUR_MS
            if deadline > data_end_ms:
                continue
            upbit_ref = hmap[int(day_hkeys[di])][0]
            if upbit_ref <= 0:
                continue
            p04, p04_src = upbit_price_at_0400(uts, uo, ucl, t00)
            if p04 is None:
                continue
            growth_up = p04 / upbit_ref - 1
            if growth_up < 0.03:
                continue
            t04 = t00 + 4 * HOUR_MS
            fi = np.searchsorted(bts, t04)
            if fi >= len(bts) or bts[fi] != t04:
                continue
            raw_open = bo[fi]
            entry_px = raw_open * (1 - SLIP)
            ref_imp = raw_open / (1 + growth_up)  # Upbit move projected onto the Binance scale
            sl_px = entry_px * (1 + cfg["sl"] / 100)
            tp_px = entry_px - cfg["tp"] / 100 * (entry_px - ref_imp)
            tr = simulate_short(bts, bo, bh, bl, bc, fi, deadline, entry_px, sl_px, tp_px,
                                f_times, f_rates, int(t04))
            if tr is None:
                continue
            row = dict(token=token, date=days[di].strftime("%Y-%m-%d"), vp=vp, gp=gp, pp=pp, **tr)
            if keep_details:
                row.update(upbit_ref_open00=upbit_ref, upbit_p04=p04, p04_source=p04_src,
                           growth_up=growth_up, binance_open04=raw_open, entry_px=entry_px,
                           ref_implied=ref_imp, sl_px=sl_px, tp_px=tp_px)
            raw.append(row)

    out, open_until = [], {}
    for r in sorted(raw, key=lambda x: (x["token"], x["entry_ms"])):
        busy = r["entry_ms"] < open_until.get(r["token"], -1)
        if not busy:
            open_until[r["token"]] = r["exit_ms"]
        if keep_details:
            r = dict(r, skipped_busy=busy)
            out.append(r)
        elif not busy:
            out.append(r)
    return pd.DataFrame(out)
