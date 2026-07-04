"""Generate the canonical detector-parity reference for live execution.

For every frozen-config signal on IS 2026 and OOS 2025 this dumps ALL intermediate
detector values (points, Upbit reference and gate price, Binance decision price,
implied reference, SL/TP levels) so a live detector can be diffed field-by-field.
Sanity gate: trade counts and net totals must reproduce the published
181 / +4848.72 (IS) and 171 / +2807.70 (OOS) exactly.

Note the IS hourly aggregation needs a lookback buffer (2025-12-27) so early-January
candles see a full 50h history; OOS has no buffer because the dataset begins exactly
at 2025-01-01 (documented truncated-lookback caveat).
"""

import pandas as pd

import engine

is_df = engine.day_signals("2026-01-01", "2026-06-01", hourly_start="2025-12-27", keep_details=True)
is_df.insert(0, "period", "IS_2026")
oos_df = engine.day_signals("2025-01-01", "2026-01-01", keep_details=True)
oos_df.insert(0, "period", "OOS_2025")

full = pd.concat([is_df, oos_df])
full.to_csv("results/detector_parity_reference.csv", index=False)

for name, df, want_n, want_net in [("IS", is_df, 181, 4848.72), ("OOS", oos_df, 171, 2807.70)]:
    taken = df[~df.skipped_busy]
    ok_n = len(taken) == want_n
    ok_net = abs(taken.net.sum() - want_net) < 0.01
    print(f"{name}: signals {len(df)} (taken {len(taken)}, busy-skipped {int(df.skipped_busy.sum())}), "
          f"net {taken.net.sum():+.2f} | count {'OK' if ok_n else 'MISMATCH'} net {'OK' if ok_net else 'MISMATCH'}")
    assert ok_n and ok_net, f"{name} parity reference does not reproduce published results"
print("saved results/detector_parity_reference.csv")
