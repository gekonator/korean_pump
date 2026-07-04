"""OOS run: 2025-01-01 -> 2026-01-01, both configs frozen from the IS grid (stage B).
Pre-registered pass criteria (fixed BEFORE this run):
  1) OOS bootstrap CI lower bound (2.5th pct of 1000 resamples of expectancy) > 0
  2) OOS expectancy >= 50% of IS expectancy
     IS expectancy: day +26.79 USDT/trade -> threshold 13.39; night +3.47 -> threshold 1.73.

Day strategy comes from engine.py (the canonical frozen implementation). The night
strategy is implemented inline: it was rejected on this run and archived, so it is
deliberately not part of the shared engine. All times UTC.

Note: OOS data starts exactly 2025-01-01, so the first ~2 days have truncated lookback
(missing prior hours contribute zero volume); this slightly deflates early-January points.
"""

import time

import numpy as np
import pandas as pd

import engine
from engine import HOUR_MS, MIN_MS, SLIP, simulate_short

OOS_START = pd.Timestamp("2025-01-01")
OOS_END = pd.Timestamp("2026-01-01")   # exclusive
PERIOD_DAYS = (OOS_END - OOS_START).days

NIGHT_CFG = dict(V=5, G=3, sl=8, tp=90)
IS_EXPECTANCY = dict(night=3.465188, day=26.788527)


def run_night():
    """Night strategy (V>=5, G>=3, SL 8%, TP 90%): rejected on this run, kept for the record."""
    hourly_maps = engine.load_hourly_maps(OOS_START, OOS_END)
    funding = engine.load_funding()
    days = pd.date_range(OOS_START, OOS_END - pd.Timedelta(days=1), freq="1D")
    day_ms00 = days.values.astype("datetime64[ms]").astype(np.int64)
    day_hkeys = days.values.astype("datetime64[h]").astype(np.int64)

    trades = []
    for token in engine.load_universe():
        hmap = hourly_maps.get(token)
        if hmap is None:
            continue
        b = engine.load_binance_1m(token, OOS_START, OOS_END)
        if b is None:
            continue
        bts, bo, bh, bl, bc = b
        f_times, f_rates = funding.get(token, (np.array([], dtype=np.int64), np.array([])))
        u = engine.sql(f"""
            SELECT timestamp_utc, volume FROM read_parquet('{engine.PARQUET}/upbit_1m/token={token}/*.parquet')
            WHERE timestamp_utc >= TIMESTAMP '{OOS_START}' AND timestamp_utc < TIMESTAMP '{OOS_END}'
              AND hour(timestamp_utc) = 0
            ORDER BY timestamp_utc
        """)
        uts = u["timestamp_utc"].values.astype("datetime64[ms]").astype(np.int64)
        uv = u["volume"].values

        for di in range(len(days)):
            t00 = int(day_ms00[di])
            hk = int(day_hkeys[di])
            t0010 = t00 + 10 * MIN_MS
            iref = np.searchsorted(bts, t0010)
            if iref >= len(bts) or bts[iref] != t0010:
                continue
            ref_px = bo[iref]
            cum = 0.0
            Pcum = np.zeros(24)
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
            cond = (Pcum[None, :] < live[mm][:, None]) & (Pcum[None, :] > 0)
            vp_live = np.where(cond.all(axis=1), 24, np.argmin(cond, axis=1))
            tmins = t00 + mm * MIN_MS
            ci = np.clip(np.searchsorted(bts, tmins + MIN_MS - 1, side="right") - 1, 0, len(bc) - 1)
            valid_close = bts[ci] >= t00
            gp_live = np.where(valid_close & (ref_px > 0),
                               np.floor((bc[ci] / ref_px - 1) * 100), -999).astype(int)
            ok = (vp_live >= NIGHT_CFG["V"]) & (gp_live >= NIGHT_CFG["G"])
            if not ok.any():
                continue
            m = int(mm[int(np.argmax(ok))])
            fill_ms = t00 + (m + 1) * MIN_MS
            fi = np.searchsorted(bts, fill_ms)
            deadline = t00 + 3 * HOUR_MS
            if fi >= len(bts) or bts[fi] > fill_ms + 5 * MIN_MS or bts[fi] >= deadline:
                continue
            entry_ms = int(bts[fi])
            entry_px = bo[fi] * (1 - SLIP)
            sl_px = entry_px * (1 + NIGHT_CFG["sl"] / 100)
            tp_px = entry_px - NIGHT_CFG["tp"] / 100 * (entry_px - ref_px)
            tr = simulate_short(bts, bo, bh, bl, bc, fi, deadline, entry_px, sl_px, tp_px,
                                f_times, f_rates, entry_ms)
            if tr:
                trades.append(tr)
    return pd.DataFrame(trades)


def report(name, df, seed):
    net = df["net"].values
    n = len(net)
    rng = np.random.default_rng(seed)
    boot = net[rng.integers(0, n, (1000, n))].mean(axis=1)
    ci_low, ci_high = np.percentile(boot, [2.5, 97.5])
    exp = net.mean()
    thr = IS_EXPECTANCY[name] * 0.5
    reasons = df["reason"]
    wins, losses = net[net > 0], net[net <= 0]
    pf = wins.sum() / abs(losses.sum()) if losses.sum() != 0 else np.inf
    p1, p2 = ci_low > 0, exp >= thr

    print(f"\n===== {name.upper()} OOS 2025 =====")
    print(f"trades {n} | exits TP {int((reasons=='TP').sum())} SL {int((reasons=='SL').sum())} "
          f"TIME {int((reasons=='TIME').sum())}")
    print(f"expectancy {exp:+.2f} USDT/trade  bootstrap CI [{ci_low:+.2f}, {ci_high:+.2f}]")
    print(f"win rate {len(wins)/n*100:.1f}%  PF {pf:.2f}  total net {net.sum():+.2f}")
    print(f"criterion 1 (CI low > 0):        {'PASS' if p1 else 'FAIL'}  (ci_low {ci_low:+.2f})")
    print(f"criterion 2 (exp >= 50% of IS):  {'PASS' if p2 else 'FAIL'}  (exp {exp:+.2f} vs {thr:+.2f})")
    print(f"VERDICT: {'PASS' if (p1 and p2) else 'FAIL'}")
    df.to_csv(f"results/oos_trades_{name}.csv", index=False)


def main():
    t0 = time.time()
    night = run_night()
    day = engine.day_signals(OOS_START, OOS_END)
    report("night", night, 101)
    report("day", day, 202)
    print(f"\nElapsed {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
