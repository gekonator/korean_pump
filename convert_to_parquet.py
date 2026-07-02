"""Convert data/*.csv into data/parquet/, streaming from disk to disk via DuckDB.

Large per-minute files are hive-partitioned by token so a backtest can point-query
one pair without scanning the whole dataset. Small files become single parquet files.
Source CSVs are left untouched.
"""

import os
import time

import duckdb

DATA_DIR = "data"
PARQUET_DIR = "data/parquet"

con = duckdb.connect()

# (csv path, output path, select expression, partition column or None)
JOBS = [
    (
        "data/binance_1m.csv",
        "data/parquet/binance_1m",
        "token, symbol, epoch_ms(timestamp_utc) AS timestamp_utc, open, high, low, close, volume",
        "token",
    ),
    (
        "data/upbit_1m.csv",
        "data/parquet/upbit_1m",
        "token, upbit_market, epoch_ms(timestamp_utc) AS timestamp_utc, open, high, low, close, volume",
        "token",
    ),
    (
        "data/binance_funding.csv",
        "data/parquet/binance_funding.parquet",
        "token, symbol, epoch_ms(funding_time_utc) AS funding_time_utc, funding_rate",
        None,
    ),
    (
        "data/upbit_krw_usdt_1m.csv",
        "data/parquet/upbit_krw_usdt_1m.parquet",
        "epoch_ms(timestamp_utc) AS timestamp_utc, open, high, low, close",
        None,
    ),
    (
        "data/universe.csv",
        "data/parquet/universe.parquet",
        "token, upbit_market, binance_symbol",
        None,
    ),
]


def dir_size(path):
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            total += os.path.getsize(os.path.join(root, name))
    return total


def human_mb(num_bytes):
    return f"{num_bytes / (1024 * 1024):,.1f} MB"


def main():
    os.makedirs(PARQUET_DIR, exist_ok=True)
    results = []

    for csv_path, out_path, select_expr, partition_col in JOBS:
        print(f"=== {csv_path} -> {out_path} ===", flush=True)
        t0 = time.time()

        csv_rows = con.sql(f"SELECT count(*) FROM read_csv_auto('{csv_path}')").fetchone()[0]

        if partition_col:
            copy_opts = f"(FORMAT PARQUET, COMPRESSION ZSTD, PARTITION_BY ({partition_col}), OVERWRITE_OR_IGNORE true)"
        else:
            copy_opts = "(FORMAT PARQUET, COMPRESSION ZSTD)"

        con.execute(
            f"COPY (SELECT {select_expr} FROM read_csv_auto('{csv_path}')) TO '{out_path}' {copy_opts}"
        )

        if partition_col:
            parquet_rows = con.sql(
                f"SELECT count(*) FROM read_parquet('{out_path}/**/*.parquet', hive_partitioning=1)"
            ).fetchone()[0]
            out_size = dir_size(out_path)
        else:
            parquet_rows = con.sql(f"SELECT count(*) FROM read_parquet('{out_path}')").fetchone()[0]
            out_size = os.path.getsize(out_path)

        elapsed = time.time() - t0
        csv_size = os.path.getsize(csv_path)
        ok = csv_rows == parquet_rows
        status = "OK" if ok else "MISMATCH"
        print(
            f"  rows csv={csv_rows} parquet={parquet_rows} [{status}] "
            f"size {human_mb(csv_size)} -> {human_mb(out_size)} in {elapsed:.1f}s",
            flush=True,
        )
        if not ok:
            raise RuntimeError(f"Row count mismatch for {csv_path}: csv={csv_rows} parquet={parquet_rows}")

        results.append((csv_path, out_path, csv_rows, csv_size, out_size))

    print("\n=== Summary ===")
    header = f"{'file':<28}{'rows':>14}{'csv size':>14}{'parquet size':>16}{'ratio':>10}"
    print(header)
    print("-" * len(header))
    for csv_path, out_path, rows, csv_size, out_size in results:
        ratio = csv_size / out_size if out_size else float("inf")
        name = os.path.basename(csv_path)
        print(f"{name:<28}{rows:>14,}{human_mb(csv_size):>14}{human_mb(out_size):>16}{ratio:>9.1f}x")


if __name__ == "__main__":
    main()
