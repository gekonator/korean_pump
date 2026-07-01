"""Build list of tokens traded both on Upbit and Binance USDT-M perpetual futures."""

import csv
import os
import time

import requests

UPBIT_URL = "https://api.upbit.com/v1/market/all?isDetails=false"
BINANCE_URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"

TIMEOUT = 20
QUOTE_PRIORITY = {"KRW": 0, "USDT": 1, "BTC": 2}


def fetch_json(url):
    try:
        response = requests.get(url, timeout=TIMEOUT)
        response.raise_for_status()
        return response.json()
    except requests.RequestException:
        time.sleep(1)
        response = requests.get(url, timeout=TIMEOUT)
        response.raise_for_status()
        return response.json()


def get_upbit_markets():
    data = fetch_json(UPBIT_URL)

    best = {}
    for item in data:
        market = item["market"]
        quote, _, base = market.partition("-")
        if quote not in QUOTE_PRIORITY:
            continue

        current = best.get(base)
        if current is None or QUOTE_PRIORITY[quote] < QUOTE_PRIORITY[current[0]]:
            best[base] = (quote, market)

    return {base: market for base, (quote, market) in best.items()}, len(data)


def get_binance_perpetuals():
    data = fetch_json(BINANCE_URL)

    result = {}
    for item in data["symbols"]:
        if (
            item.get("contractType") == "PERPETUAL"
            and item.get("quoteAsset") == "USDT"
            and item.get("status") == "TRADING"
        ):
            result[item["baseAsset"]] = item["symbol"]

    return result, len(data["symbols"])


def main():
    upbit_markets, upbit_count = get_upbit_markets()
    binance_perps, binance_count = get_binance_perpetuals()

    common_tokens = sorted(set(upbit_markets) & set(binance_perps))

    os.makedirs("data", exist_ok=True)
    with open("data/universe.csv", "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["token", "upbit_market", "binance_symbol"])
        for token in common_tokens:
            writer.writerow([token, upbit_markets[token], binance_perps[token]])

    print(f"Upbit markets: {upbit_count}")
    print(f"Binance perpetuals: {binance_count}")
    print(f"Common tokens: {len(common_tokens)}")


if __name__ == "__main__":
    main()
