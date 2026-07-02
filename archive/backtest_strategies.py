"""SUPERSEDED (Phase 0). Kept for provenance only.

This was the first replication attempt, written before the engine was verified
against the reference trade log. Several assumptions here were later corrected
(growth_points multiplier, lookback, funding convention, busy rule). The
validated engine lives in stage_a_verify.py / stage_b_grid.py / stage_oos.py.
"""

"""Backtest the morning-short and daytime-short strategies on our own frozen data/parquet/.

This is a parameter-replay run (not a fresh H1 tune) meant to sanity-check the
author's tuned parameters against reference annualized return / max drawdown figures.
All timestamps are UTC throughout, no other timezone is introduced anywhere.

See the printed "ASSUMPTIONS" section at the end for every place the spec was
ambiguous and how it was resolved.
"""

import math
import os
import time

import duckdb
import numpy as np
import pandas as pd

PARQUET_DIR = "data/parquet"

PERIOD_START = pd.Timestamp("2025-06-01 00:00:00")
PERIOD_END = pd.Timestamp("2026-06-01 00:00:00")  # exclusive
DATA_START = pd.Timestamp("2025-01-01 00:00:00")  # gives >40h lookback buffer before period start
DATA_END_LIMIT = pd.Timestamp("2026-06-01 00:00:00")  # exclusive upper bound of collected data

FEE_RATE = 0.0004     # 0.04% per side
SLIPPAGE = 0.0005      # 0.05%, explicit assumption

TOKEN_LIMIT = int(os.environ.get("BACKTEST_TOKEN_LIMIT", "0")) or None

con = duckdb.connect()
con.execute("PRAGMA threads=8")


# ---------------------------------------------------------------- general signal formulas (Upbit hourly) ----

def volume_points(hourly_map, target_hour, lookback_n, live_volume=None):
    if live_volume is None:
        row = hourly_map.get(target_hour)
        if row is None:
            return 0
        vol_target = row["volume"]
    else:
        vol_target = live_volume
    cumsum = 0.0
    points = 0
    for h in range(1, lookback_n + 1):
        hh = target_hour - pd.Timedelta(hours=h)
        vol_h = hourly_map.get(hh, {}).get("volume", 0.0)
        cumsum += vol_h
        if cumsum <= 0 or vol_target <= cumsum:
            break
        points = h
    return points


def growth_points_general(hourly_map, target_hour, lookback_n, breakout_multiplier=1.05):
    row = hourly_map.get(target_hour)
    if row is None:
        return 0
    high_target = row["high"]
    running_max = 0.0
    points = 0
    for h in range(1, lookback_n + 1):
        hh = target_hour - pd.Timedelta(hours=h)
        high_h = hourly_map.get(hh, {}).get("high", 0.0)
        running_max = max(running_max, high_h)
        if running_max <= 0 or high_target <= running_max * breakout_multiplier:
            break
        points = h
    return points


def pump_points(hourly_map, target_hour):
    row = hourly_map.get(target_hour)
    if row is None:
        return 0
    if row["open"] <= 0:
        return 0
    pump_pct = (row["high"] - row["open"]) / row["open"] * 100
    if pump_pct <= 3:
        return 0
    return math.ceil(pump_pct - 3)


# ---------------------------------------------------------------------------------- data loading ----

def load_token_minutes(token):
    upbit = con.sql(f"""
        SELECT timestamp_utc, open, high, low, close, volume
        FROM read_parquet('{PARQUET_DIR}/upbit_1m/token={token}/*.parquet')
        WHERE timestamp_utc >= TIMESTAMP '{DATA_START}' AND timestamp_utc < TIMESTAMP '{DATA_END_LIMIT}'
        ORDER BY timestamp_utc
    """).df()
    binance = con.sql(f"""
        SELECT timestamp_utc, open, high, low, close, volume
        FROM read_parquet('{PARQUET_DIR}/binance_1m/token={token}/*.parquet')
        WHERE timestamp_utc >= TIMESTAMP '{DATA_START}' AND timestamp_utc < TIMESTAMP '{DATA_END_LIMIT}'
        ORDER BY timestamp_utc
    """).df()
    return upbit, binance


def compute_premium(upbit, binance, krwusdt):
    """premium(t) = upbit_close(t) / (binance_close_asof(t) * krwusdt_close_asof(t)) - 1, asof = last known <= t."""
    df = pd.merge_asof(
        upbit[["timestamp_utc", "close"]].rename(columns={"close": "upbit_close"}).sort_values("timestamp_utc"),
        binance[["timestamp_utc", "close"]].rename(columns={"close": "binance_close"}).sort_values("timestamp_utc"),
        on="timestamp_utc",
    )
    df = pd.merge_asof(
        df,
        krwusdt.reset_index().rename(columns={"close": "krwusdt_close"}).sort_values("timestamp_utc"),
        on="timestamp_utc",
    )
    df["premium"] = df["upbit_close"] / (df["binance_close"] * df["krwusdt_close"]) - 1
    return df.dropna(subset=["premium"]).set_index("timestamp_utc")["premium"].sort_index()


def premium_asof(premium_series, t):
    pos = premium_series.index.searchsorted(t, side="right")
    if pos == 0:
        return None
    return premium_series.iloc[pos - 1]


# ------------------------------------------------------------------------------------- morning strategy: phase A ----

def find_morning_signals(token, upbit, binance, hourly_map, premium_series):
    binance_map = binance.set_index("timestamp_utc")[["open", "close"]].to_dict("index")
    upbit_vol = upbit.set_index("timestamp_utc")["volume"].sort_index()

    candidates = []
    for day in pd.date_range(PERIOD_START, PERIOD_END - pd.Timedelta(days=1), freq="1D"):
        target_hour = day
        exit_deadline = target_hour + pd.Timedelta(hours=3)
        if exit_deadline > DATA_END_LIMIT:
            continue

        ref_row = binance_map.get(target_hour)
        if ref_row is None or ref_row["open"] <= 0:
            continue
        reference_price = ref_row["open"]

        kimchi_baseline = premium_asof(premium_series, target_hour - pd.Timedelta(minutes=1))
        if kimchi_baseline is None:
            continue

        hour_minutes = upbit_vol[(upbit_vol.index >= target_hour) & (upbit_vol.index < target_hour + pd.Timedelta(hours=1))]
        cum_vol = hour_minutes.cumsum()

        found = None
        for m in pd.date_range(target_hour + pd.Timedelta(minutes=10), target_hour + pd.Timedelta(minutes=59), freq="1min"):
            b = binance_map.get(m)
            if b is None:
                continue
            growth_pct = (b["close"] - reference_price) / reference_price * 100
            if math.floor(growth_pct) < 3:
                continue

            pos = cum_vol.index.searchsorted(m, side="right")
            if pos == 0:
                continue
            live_vol = cum_vol.iloc[pos - 1]
            if volume_points(hourly_map, target_hour, 5, live_volume=live_vol) < 5:
                continue

            prem_m = premium_asof(premium_series, m)
            if prem_m is None:
                continue
            kimchi_growth_pp = (prem_m - kimchi_baseline) * 100
            if not (0.2 <= kimchi_growth_pp <= 1.7):
                continue

            found = dict(signal_minute=m, growth_pct=growth_pct, kimchi_growth_pp=kimchi_growth_pp)
            break

        if found:
            candidates.append(dict(
                token=token, day=day, target_hour=target_hour,
                reference_price=reference_price, exit_deadline=exit_deadline,
                **found,
            ))
    return candidates


# --------------------------------------------------------------------------------------- daily strategy: phase A ----

def find_daily_signals(token, upbit, binance, hourly_map, premium_series):
    binance_map = binance.set_index("timestamp_utc")[["open", "close"]].to_dict("index")

    candidates = []
    for day in pd.date_range(PERIOD_START, PERIOD_END - pd.Timedelta(days=1), freq="1D"):
        target_hour = day + pd.Timedelta(hours=4)
        exit_deadline = day + pd.Timedelta(days=1) + pd.Timedelta(hours=14)
        if exit_deadline > DATA_END_LIMIT:
            continue

        vp = volume_points(hourly_map, target_hour, 40)
        if vp < 14:
            continue
        gp = growth_points_general(hourly_map, target_hour, 40, breakout_multiplier=1.05)
        if gp < 10:
            continue
        pp = pump_points(hourly_map, target_hour)
        if pp < 4:
            continue

        ref_row = binance_map.get(target_hour)
        if ref_row is None or ref_row["open"] <= 0:
            continue
        reference_price = ref_row["open"]

        running_max = None
        found = None
        for m in pd.date_range(target_hour, target_hour + pd.Timedelta(minutes=119), freq="1min"):
            prem_m = premium_asof(premium_series, m)
            if prem_m is None:
                continue
            running_max = prem_m if running_max is None else max(running_max, prem_m)

            b = binance_map.get(m)
            if b is None:
                continue
            growth_pct = (b["close"] - reference_price) / reference_price * 100
            drawdown_pp = (running_max - prem_m) * 100

            if growth_pct >= 3.0 and drawdown_pp >= 1.7:
                found = dict(signal_minute=m, growth_pct=growth_pct, drawdown_pp=drawdown_pp)
                break

        if found:
            candidates.append(dict(
                token=token, day=day, target_hour=target_hour,
                reference_price=reference_price, exit_deadline=exit_deadline,
                volume_points=vp, growth_points=gp, pump_points=pp,
                **found,
            ))
    return candidates


# -------------------------------------------------------------------------------------------- trade simulation ----

def simulate_trade(token, entry_minute, time_exit, reference_price, sl_pct, funding_by_token, notional=1000.0):
    window = con.sql(f"""
        SELECT timestamp_utc, open, high, low, close
        FROM read_parquet('{PARQUET_DIR}/binance_1m/token={token}/*.parquet')
        WHERE timestamp_utc >= TIMESTAMP '{entry_minute}' AND timestamp_utc <= TIMESTAMP '{time_exit}'
        ORDER BY timestamp_utc
    """).df()
    if window.empty or window.iloc[0]["timestamp_utc"] != entry_minute:
        return None  # no clean point-in-time fill available

    raw_entry = window.iloc[0]["open"]
    if raw_entry <= 0:
        return None
    entry_price = raw_entry * (1 - SLIPPAGE)
    stop_price = entry_price * (1 + sl_pct)
    tp_price = entry_price - 0.90 * (entry_price - reference_price)

    exit_price = None
    exit_reason = None
    exit_time = None
    for _, row in window.iterrows():
        hit_sl = row["high"] >= stop_price
        hit_tp = row["low"] <= tp_price
        if hit_sl and hit_tp:
            exit_price, exit_reason, exit_time = stop_price, "SL", row["timestamp_utc"]
            break
        if hit_sl:
            exit_price, exit_reason, exit_time = stop_price, "SL", row["timestamp_utc"]
            break
        if hit_tp:
            exit_price, exit_reason, exit_time = tp_price, "TP", row["timestamp_utc"]
            break

    if exit_price is None:
        last_row = window.iloc[-1]
        raw_exit = last_row["open"] if last_row["timestamp_utc"] == time_exit else last_row["close"]
        exit_price = raw_exit * (1 + SLIPPAGE)
        exit_reason = "TIME"
        exit_time = last_row["timestamp_utc"]

    qty = notional / entry_price
    gross_pnl = qty * (entry_price - exit_price)
    entry_fee = notional * FEE_RATE
    exit_fee = qty * exit_price * FEE_RATE

    times, rates = funding_by_token.get(token, (np.array([]), np.array([])))
    if len(times):
        mask = (times >= entry_minute) & (times < exit_time)
        funding_pnl = float(rates[mask].sum()) * notional
        n_funding = int(mask.sum())
    else:
        funding_pnl, n_funding = 0.0, 0

    net_pnl = gross_pnl - entry_fee - exit_fee + funding_pnl

    return dict(
        token=token, entry_time=entry_minute, exit_time=exit_time, exit_reason=exit_reason,
        entry_price=entry_price, exit_price=exit_price, qty=qty,
        gross_pnl=gross_pnl, fees=entry_fee + exit_fee, funding_pnl=funding_pnl, n_funding=n_funding,
        net_pnl=net_pnl, return_pct=net_pnl / notional,
    )


# --------------------------------------------------------------------------------------------------- metrics ----

def compute_metrics(trades, base_capital, period_days):
    if not trades:
        return None
    df = pd.DataFrame(trades).sort_values("exit_time").reset_index(drop=True)
    df["equity"] = base_capital + df["net_pnl"].cumsum()
    df["peak"] = df["equity"].cummax()
    df["drawdown_pct"] = (df["peak"] - df["equity"]) / df["peak"] * 100

    total_pnl = df["net_pnl"].sum()
    total_return_pct = total_pnl / base_capital * 100
    annualized_return_pct = total_return_pct * (365.0 / period_days)

    win_rate = (df["net_pnl"] > 0).mean() * 100
    expectancy_usdt = df["net_pnl"].mean()
    expectancy_pct = df["return_pct"].mean() * 100
    max_dd = df["drawdown_pct"].max()

    return dict(
        n_trades=len(df), win_rate=win_rate, expectancy_usdt=expectancy_usdt,
        expectancy_pct=expectancy_pct, total_pnl=total_pnl, total_return_pct=total_return_pct,
        annualized_return_pct=annualized_return_pct, max_drawdown_pct=max_dd,
        n_sl=(df["exit_reason"] == "SL").sum(), n_tp=(df["exit_reason"] == "TP").sum(),
        n_time=(df["exit_reason"] == "TIME").sum(),
        trades_df=df,
    )


# ------------------------------------------------------------------------------------------------------- main ----

def main():
    t0 = time.time()

    tokens = con.sql(f"""
        SELECT DISTINCT token FROM read_parquet('{PARQUET_DIR}/upbit_1m/*/*.parquet', hive_partitioning=1)
        WHERE token IN (SELECT DISTINCT token FROM read_parquet('{PARQUET_DIR}/binance_1m/*/*.parquet', hive_partitioning=1))
        ORDER BY token
    """).df()["token"].tolist()
    if TOKEN_LIMIT:
        tokens = tokens[:TOKEN_LIMIT]
    print(f"{len(tokens)} tokens with data on both exchanges", flush=True)

    print("Loading KRW-USDT series...", flush=True)
    krwusdt = con.sql(f"SELECT timestamp_utc, close FROM read_parquet('{PARQUET_DIR}/upbit_krw_usdt_1m.parquet') ORDER BY timestamp_utc").df()
    krwusdt = krwusdt.set_index("timestamp_utc")["close"]

    print("Loading funding data...", flush=True)
    funding_df = con.sql(f"SELECT token, funding_time_utc, funding_rate FROM read_parquet('{PARQUET_DIR}/binance_funding.parquet') ORDER BY token, funding_time_utc").df()
    funding_by_token = {}
    for tok, g in funding_df.groupby("token"):
        funding_by_token[tok] = (g["funding_time_utc"].values, g["funding_rate"].values)

    print("Building Upbit hourly aggregates for all tokens...", flush=True)
    upbit_hourly_all = con.sql(f"""
        SELECT token, date_trunc('hour', timestamp_utc) AS hour,
            first(open ORDER BY timestamp_utc) AS open,
            max(high) AS high, min(low) AS low,
            last(close ORDER BY timestamp_utc) AS close,
            sum(volume) AS volume
        FROM read_parquet('{PARQUET_DIR}/upbit_1m/*/*.parquet', hive_partitioning=1)
        WHERE timestamp_utc >= TIMESTAMP '{DATA_START}' AND timestamp_utc < TIMESTAMP '{DATA_END_LIMIT}'
        GROUP BY token, hour
    """).df()
    upbit_hourly_all = upbit_hourly_all.set_index(["token", "hour"]).sort_index()

    morning_candidates = []
    daily_candidates = []

    for i, token in enumerate(tokens, 1):
        upbit, binance = load_token_minutes(token)
        if upbit.empty or binance.empty:
            continue

        try:
            hourly_df = upbit_hourly_all.loc[token]
        except KeyError:
            continue
        hourly_map = hourly_df[["open", "high", "low", "close", "volume"]].to_dict("index")

        premium_series = compute_premium(upbit, binance, krwusdt)
        if premium_series.empty:
            continue

        morning_candidates.extend(find_morning_signals(token, upbit, binance, hourly_map, premium_series))
        daily_candidates.extend(find_daily_signals(token, upbit, binance, hourly_map, premium_series))

        if i % 20 == 0 or i == len(tokens):
            print(f"  scanned {i}/{len(tokens)} tokens, elapsed {time.time()-t0:.1f}s, "
                  f"morning_candidates={len(morning_candidates)} daily_candidates={len(daily_candidates)}", flush=True)

    print(f"\nPhase A done in {time.time()-t0:.1f}s. "
          f"Morning raw candidates: {len(morning_candidates)}, Daily raw candidates: {len(daily_candidates)}", flush=True)

    # --- phase B: concurrency + priority selection per day ---
    morning_df = pd.DataFrame(morning_candidates)
    daily_df = pd.DataFrame(daily_candidates)

    morning_selected = []
    if not morning_df.empty:
        for day, group in morning_df.groupby("day"):
            g = group.sort_values(["growth_pct", "kimchi_growth_pp"], ascending=[False, True])
            morning_selected.extend(g.head(3).to_dict("records"))

    daily_selected = []
    if not daily_df.empty:
        for day, group in daily_df.groupby("day"):
            g = group.sort_values(["growth_points", "volume_points", "pump_points"], ascending=[False, False, False])
            daily_selected.extend(g.head(4).to_dict("records"))

    print(f"Selected after concurrency cap: morning={len(morning_selected)} daily={len(daily_selected)}", flush=True)

    # --- phase C: simulate trades ---
    print("\nSimulating morning trades...", flush=True)
    morning_trades = []
    for c in morning_selected:
        entry_minute = c["signal_minute"] + pd.Timedelta(minutes=1)
        t = simulate_trade(c["token"], entry_minute, c["exit_deadline"], c["reference_price"], sl_pct=0.10, funding_by_token=funding_by_token)
        if t:
            morning_trades.append(t)

    print("Simulating daily trades...", flush=True)
    daily_trades = []
    for c in daily_selected:
        entry_minute = c["signal_minute"] + pd.Timedelta(minutes=1)
        t = simulate_trade(c["token"], entry_minute, c["exit_deadline"], c["reference_price"], sl_pct=0.13, funding_by_token=funding_by_token)
        if t:
            daily_trades.append(t)

    period_days = (PERIOD_END - PERIOD_START).days

    morning_metrics = compute_metrics(morning_trades, base_capital=3000.0, period_days=period_days)
    daily_metrics = compute_metrics(daily_trades, base_capital=4000.0, period_days=period_days)

    print(f"\nTotal elapsed: {time.time()-t0:.1f}s")

    return morning_metrics, daily_metrics, len(morning_candidates), len(daily_candidates)


def print_report(name, metrics, n_raw_candidates, ref_annual_pct, ref_dd_pct):
    print(f"\n{'='*60}\n{name}\n{'='*60}")
    if metrics is None:
        print("No trades generated.")
        return
    print(f"Raw candidates found (pre-concurrency-cap): {n_raw_candidates}")
    print(f"Trades executed:      {metrics['n_trades']}")
    print(f"  exits: SL={metrics['n_sl']} TP={metrics['n_tp']} TIME={metrics['n_time']}")
    print(f"Win rate:              {metrics['win_rate']:.1f}%")
    print(f"Expectancy (net):      {metrics['expectancy_usdt']:.2f} USDT/trade ({metrics['expectancy_pct']:.2f}% of notional)")
    print(f"Total net P&L:         {metrics['total_pnl']:.2f} USDT")
    print(f"Total return:          {metrics['total_return_pct']:.1f}%")
    print(f"Annualized return:     {metrics['annualized_return_pct']:.1f}%   (reference: ~{ref_annual_pct}%)")
    print(f"Max drawdown (realized): {metrics['max_drawdown_pct']:.1f}%   (reference: ~{ref_dd_pct}%)")


if __name__ == "__main__":
    morning_metrics, daily_metrics, n_morning_raw, n_daily_raw = main()
    print_report("STRATEGY 1 -- Morning shorts", morning_metrics, n_morning_raw, 460, 36)
    print_report("STRATEGY 2 -- Daily shorts", daily_metrics, n_daily_raw, 830, 37)
