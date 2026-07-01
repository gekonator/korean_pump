"""Download raw 1m data for Upbit/Binance kimchi-premium backtest, once, into frozen CSVs.

Pairs come from data/universe.csv (token, upbit_market, binance_symbol).
Everything is cached under data/cache/ so a re-run only fetches what's missing.
"""

import csv
import io
import json
import os
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import requests

# ---- period ----
START_DATE = "2025-01-01"
END_DATE = "2026-06-01"  # exclusive upper bound

START_DATETIME = datetime.strptime(START_DATE, "%Y-%m-%d").replace(tzinfo=timezone.utc)
END_DATETIME = datetime.strptime(END_DATE, "%Y-%m-%d").replace(tzinfo=timezone.utc)

# ---- endpoints ----
BINANCE_VISION_URL = "https://data.binance.vision/data/futures/um/daily/klines/{symbol}/1m/{symbol}-1m-{date}.zip"
BINANCE_KLINES_REST_URL = "https://fapi.binance.com/fapi/v1/klines"
BINANCE_FUNDING_URL = "https://fapi.binance.com/fapi/v1/fundingRate"
UPBIT_CANDLES_URL = "https://api.upbit.com/v1/candles/minutes/1"

# ---- output files ----
UNIVERSE_FILE = "data/universe.csv"
BINANCE_KLINES_FILE = "data/binance_1m.csv"
BINANCE_KLINES_HEADER = ["token", "symbol", "timestamp_utc", "open", "high", "low", "close", "volume"]
BINANCE_FUNDING_FILE = "data/binance_funding.csv"
BINANCE_FUNDING_HEADER = ["token", "symbol", "funding_time_utc", "funding_rate"]
UPBIT_KLINES_FILE = "data/upbit_1m.csv"
UPBIT_KLINES_HEADER = ["token", "upbit_market", "timestamp_utc", "open", "high", "low", "close", "volume"]
UPBIT_KRW_USDT_FILE = "data/upbit_krw_usdt_1m.csv"
UPBIT_KRW_USDT_HEADER = ["timestamp_utc", "open", "high", "low", "close"]
GAPS_LOG = "data/gaps.log"

CACHE_DIR = "data/cache"
MANIFEST_DIR = os.path.join(CACHE_DIR, "manifests")

TIMEOUT = 20
BACKOFF_BASE = 1.0
MAX_RETRIES = 4
LOG_EVERY = 10
PROGRESS_FILE = "data/progress.json"

# concurrency: number of pairs processed in parallel per phase
BINANCE_WORKERS = 10
UPBIT_WORKERS = 8

SESSION = requests.Session()
_adapter = requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=20)
SESSION.mount("https://", _adapter)

IO_LOCK = threading.Lock()


class RateLimiter:
    """Caps aggregate request rate to a host across all threads."""

    def __init__(self, max_per_sec):
        self.min_interval = 1.0 / max_per_sec
        self.lock = threading.Lock()
        self.last = 0.0

    def wait(self):
        with self.lock:
            now = time.monotonic()
            elapsed = now - self.last
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)
            self.last = time.monotonic()


BINANCE_LIMITER = RateLimiter(20)  # data.binance.vision + fapi REST
UPBIT_LIMITER = RateLimiter(8)  # Upbit public API is strictly rate-limited


class Counter:
    def __init__(self):
        self.value = 0
        self.lock = threading.Lock()

    def increment(self):
        with self.lock:
            self.value += 1
            return self.value


def format_eta(seconds):
    seconds = max(int(seconds), 0)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def write_progress(phase, done, total, phase_start):
    elapsed = time.time() - phase_start
    rate = elapsed / done if done else 0
    eta = rate * (total - done)
    payload = {
        "phase": phase,
        "done": done,
        "total": total,
        "elapsed_sec": round(elapsed, 1),
        "eta_sec": round(eta, 1),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    os.makedirs("data", exist_ok=True)
    with open(PROGRESS_FILE, "w") as f:
        json.dump(payload, f)
    print(f"  [{phase}] {done}/{total} pairs, elapsed {format_eta(elapsed)}, ETA {format_eta(eta)}", flush=True)


# ---------------------------------------------------------------- helpers ----

def request_with_backoff(url, params=None, limiter=None):
    for attempt in range(MAX_RETRIES + 1):
        if limiter:
            limiter.wait()
        try:
            resp = SESSION.get(url, params=params, timeout=TIMEOUT)
        except requests.RequestException:
            if attempt == MAX_RETRIES:
                return None
            time.sleep(BACKOFF_BASE * (attempt + 1))
            continue
        if resp.status_code in (429, 418):
            if attempt == MAX_RETRIES:
                return resp
            time.sleep(BACKOFF_BASE * (2 ** attempt))
            continue
        return resp
    return None


def log_gap(message):
    with IO_LOCK:
        os.makedirs("data", exist_ok=True)
        with open(GAPS_LOG, "a") as f:
            f.write(f"{datetime.now(timezone.utc).isoformat()} {message}\n")


def append_rows(path, rows, header):
    if not rows:
        return
    with IO_LOCK:
        file_exists = os.path.exists(path) and os.path.getsize(path) > 0
        with open(path, "a", newline="") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(header)
            writer.writerows(rows)


def load_symbol_manifest(source, key):
    path = os.path.join(MANIFEST_DIR, source, f"{key}.json")
    if os.path.exists(path):
        with open(path) as f:
            return set(json.load(f))
    return set()


def save_symbol_manifest(source, key, seen_set):
    path = os.path.join(MANIFEST_DIR, source, f"{key}.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(sorted(seen_set), f)


def daily_date_list(start_dt, end_dt):
    dates = []
    d = start_dt
    while d < end_dt:
        dates.append(d)
        d += timedelta(days=1)
    return dates


def month_list(start_dt, end_dt):
    months = []
    y, m = start_dt.year, start_dt.month
    while datetime(y, m, 1, tzinfo=timezone.utc) < end_dt:
        months.append((y, m))
        m += 1
        if m > 12:
            m = 1
            y += 1
    return months


def count_csv_rows(path):
    if not os.path.exists(path):
        return 0
    with open(path) as f:
        return max(sum(1 for _ in f) - 1, 0)


def count_lines(path):
    if not os.path.exists(path):
        return 0
    with open(path) as f:
        return sum(1 for _ in f)


def read_universe():
    with open(UNIVERSE_FILE) as f:
        return list(csv.DictReader(f))


# ------------------------------------------------------------ binance 1m ----

def download_binance_day_rest(symbol, day_start_ms, day_end_ms):
    rows = []
    cursor = day_start_ms
    while cursor <= day_end_ms:
        params = {"symbol": symbol, "interval": "1m", "startTime": cursor, "endTime": day_end_ms, "limit": 1500}
        resp = request_with_backoff(BINANCE_KLINES_REST_URL, params, limiter=BINANCE_LIMITER)
        if resp is None or resp.status_code != 200:
            log_gap(f"binance_klines {symbol} REST startTime={cursor}: request failed")
            break
        data = resp.json()
        if not data:
            break
        rows.extend([int(d[0]), d[1], d[2], d[3], d[4], d[5]] for d in data)
        last_open = data[-1][0]
        if len(data) < 1500 or last_open >= day_end_ms:
            break
        cursor = last_open + 60_000
    return rows


def download_binance_day(symbol, day_dt, date_str):
    url = BINANCE_VISION_URL.format(symbol=symbol, date=date_str)
    resp = request_with_backoff(url, limiter=BINANCE_LIMITER)
    if resp is not None and resp.status_code == 200:
        try:
            zf = zipfile.ZipFile(io.BytesIO(resp.content))
            inner_name = zf.namelist()[0]
            with zf.open(inner_name) as raw:
                text = io.TextIOWrapper(raw, encoding="utf-8")
                rows = []
                for row in csv.reader(text):
                    if not row or not row[0].isdigit():
                        continue  # skip header row present in some dumps
                    rows.append([int(row[0]), row[1], row[2], row[3], row[4], row[5]])
                if rows:
                    return rows
        except zipfile.BadZipFile:
            pass

    day_start_ms = int(day_dt.timestamp() * 1000)
    day_end_ms = int((day_dt + timedelta(days=1)).timestamp() * 1000) - 1
    return download_binance_day_rest(symbol, day_start_ms, day_end_ms)


def process_binance_klines(token, symbol):
    seen_dates = load_symbol_manifest("binance_klines", symbol)
    cache_dir = os.path.join(CACHE_DIR, "binance_klines", symbol)

    for day_dt in daily_date_list(START_DATETIME, END_DATETIME):
        date_str = day_dt.strftime("%Y-%m-%d")
        if date_str in seen_dates:
            continue

        cache_file = os.path.join(cache_dir, f"{date_str}.csv")
        if os.path.exists(cache_file):
            with open(cache_file, newline="") as f:
                rows = [[int(r[0]), r[1], r[2], r[3], r[4], r[5]] for r in csv.reader(f)]
        else:
            rows = download_binance_day(symbol, day_dt, date_str)
            if rows:
                os.makedirs(cache_dir, exist_ok=True)
                with open(cache_file, "w", newline="") as f:
                    csv.writer(f).writerows(rows)

        if not rows:
            log_gap(f"binance_klines {symbol} {date_str}: no data")
            continue

        output_rows = [(token, symbol, *r) for r in rows]
        append_rows(BINANCE_KLINES_FILE, output_rows, BINANCE_KLINES_HEADER)
        seen_dates.add(date_str)
        save_symbol_manifest("binance_klines", symbol, seen_dates)


# ------------------------------------------------------------- funding ----

def process_binance_funding(token, symbol):
    seen_months = load_symbol_manifest("binance_funding", symbol)
    cache_dir = os.path.join(CACHE_DIR, "binance_funding", symbol)

    for year, month in month_list(START_DATETIME, END_DATETIME):
        month_key = f"{year:04d}-{month:02d}"
        if month_key in seen_months:
            continue

        cache_file = os.path.join(cache_dir, f"{month_key}.json")
        if os.path.exists(cache_file):
            with open(cache_file) as f:
                rows = json.load(f)
        else:
            month_start = datetime(year, month, 1, tzinfo=timezone.utc)
            next_month = (
                datetime(year + 1, 1, 1, tzinfo=timezone.utc)
                if month == 12
                else datetime(year, month + 1, 1, tzinfo=timezone.utc)
            )
            start_ms = max(int(month_start.timestamp() * 1000), int(START_DATETIME.timestamp() * 1000))
            end_ms = min(int(next_month.timestamp() * 1000), int(END_DATETIME.timestamp() * 1000)) - 1

            params = {"symbol": symbol, "startTime": start_ms, "endTime": end_ms, "limit": 1000}
            resp = request_with_backoff(BINANCE_FUNDING_URL, params, limiter=BINANCE_LIMITER)
            if resp is None or resp.status_code != 200:
                log_gap(f"binance_funding {symbol} {month_key}: request failed")
                continue

            data = resp.json()
            rows = [[d["fundingTime"], d["fundingRate"]] for d in data]
            os.makedirs(cache_dir, exist_ok=True)
            with open(cache_file, "w") as f:
                json.dump(rows, f)

        if rows:
            output_rows = [(token, symbol, r[0], r[1]) for r in rows]
            append_rows(BINANCE_FUNDING_FILE, output_rows, BINANCE_FUNDING_HEADER)
        seen_months.add(month_key)
        save_symbol_manifest("binance_funding", symbol, seen_months)


# --------------------------------------------------------------- upbit ----

def download_upbit_chunks(market, cache_subdir, manifest_source):
    """Yield raw Upbit candle chunks (list of dicts, newest first) covering [START_DATETIME, END_DATETIME)."""
    seen_keys = load_symbol_manifest(manifest_source, market)
    cache_dir = os.path.join(CACHE_DIR, cache_subdir, market)
    to_cursor = END_DATETIME

    while to_cursor > START_DATETIME:
        cache_key = to_cursor.strftime("%Y%m%dT%H%M%S")
        cache_file = os.path.join(cache_dir, f"{cache_key}.json")

        if os.path.exists(cache_file):
            with open(cache_file) as f:
                data = json.load(f)
        else:
            params = {"market": market, "to": to_cursor.strftime("%Y-%m-%d %H:%M:%S"), "count": 200}
            resp = request_with_backoff(UPBIT_CANDLES_URL, params, limiter=UPBIT_LIMITER)
            if resp is None or resp.status_code != 200:
                log_gap(f"{manifest_source} {market} before {to_cursor.isoformat()}: request failed")
                break
            data = resp.json()
            if not data:
                log_gap(f"{manifest_source} {market} before {to_cursor.isoformat()}: empty response")
                break
            os.makedirs(cache_dir, exist_ok=True)
            with open(cache_file, "w") as f:
                json.dump(data, f)

        oldest_dt = datetime.strptime(
            data[-1]["candle_date_time_utc"], "%Y-%m-%dT%H:%M:%S"
        ).replace(tzinfo=timezone.utc)

        if cache_key not in seen_keys:
            yield data
            seen_keys.add(cache_key)
            save_symbol_manifest(manifest_source, market, seen_keys)

        to_cursor = oldest_dt - timedelta(minutes=1)


def process_upbit_klines(token, market):
    for chunk in download_upbit_chunks(market, "upbit_klines", "upbit_klines"):
        rows = []
        for c in chunk:
            ts_dt = datetime.strptime(c["candle_date_time_utc"], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
            if not (START_DATETIME <= ts_dt < END_DATETIME):
                continue
            ts_ms = int(ts_dt.timestamp() * 1000)
            rows.append(
                (token, market, ts_ms, c["opening_price"], c["high_price"], c["low_price"],
                 c["trade_price"], c["candle_acc_trade_price"])
            )
        append_rows(UPBIT_KLINES_FILE, rows, UPBIT_KLINES_HEADER)


def process_upbit_krw_usdt():
    for chunk in download_upbit_chunks("KRW-USDT", "upbit_krw_usdt", "upbit_krw_usdt"):
        rows = []
        for c in chunk:
            ts_dt = datetime.strptime(c["candle_date_time_utc"], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
            if not (START_DATETIME <= ts_dt < END_DATETIME):
                continue
            ts_ms = int(ts_dt.timestamp() * 1000)
            rows.append((ts_ms, c["opening_price"], c["high_price"], c["low_price"], c["trade_price"]))
        append_rows(UPBIT_KRW_USDT_FILE, rows, UPBIT_KRW_USDT_HEADER)


# ---------------------------------------------------------------- main ----

def binance_worker(pair):
    process_binance_klines(pair["token"], pair["binance_symbol"])
    process_binance_funding(pair["token"], pair["binance_symbol"])


def upbit_worker(pair):
    process_upbit_klines(pair["token"], pair["upbit_market"])


def run_phase(name, pairs, worker_fn, max_workers):
    phase_start = time.time()
    counter = Counter()
    total = len(pairs)
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(worker_fn, pair): pair for pair in pairs}
        for fut in as_completed(futures):
            pair = futures[fut]
            try:
                fut.result()
            except Exception as exc:
                log_gap(f"{name} {pair.get('token')}: worker crashed: {exc!r}")
            done = counter.increment()
            if done % LOG_EVERY == 0 or done == total:
                write_progress(name, done, total, phase_start)


def main():
    start_time = time.time()
    os.makedirs(CACHE_DIR, exist_ok=True)

    pairs = read_universe()
    print(f"Loaded {len(pairs)} pairs from {UNIVERSE_FILE}")
    print(f"Period: {START_DATE} .. {END_DATE} (UTC, end exclusive)")

    print(f"Phase 1: Binance klines + funding ({BINANCE_WORKERS} workers)", flush=True)
    run_phase("binance", pairs, binance_worker, BINANCE_WORKERS)

    print(f"Phase 2: Upbit per-token klines ({UPBIT_WORKERS} workers)", flush=True)
    run_phase("upbit", pairs, upbit_worker, UPBIT_WORKERS)

    print("Phase 3: Upbit KRW-USDT (once)", flush=True)
    process_upbit_krw_usdt()

    elapsed = time.time() - start_time
    print("\nDone.")
    print(f"Pairs processed: {len(pairs)}")
    print(f"{BINANCE_KLINES_FILE}: {count_csv_rows(BINANCE_KLINES_FILE)} rows")
    print(f"{BINANCE_FUNDING_FILE}: {count_csv_rows(BINANCE_FUNDING_FILE)} rows")
    print(f"{UPBIT_KLINES_FILE}: {count_csv_rows(UPBIT_KLINES_FILE)} rows")
    print(f"{UPBIT_KRW_USDT_FILE}: {count_csv_rows(UPBIT_KRW_USDT_FILE)} rows")
    print(f"Gaps logged: {count_lines(GAPS_LOG)}")
    print(f"Elapsed: {elapsed:.1f}s")


if __name__ == "__main__":
    main()
