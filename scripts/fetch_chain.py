#!/usr/bin/env python3
"""
Downloads 5-min candles for:
  - the underlying index
  - every CE/PE at every strike in the configured range
  - for both the near and far expiry
  - for every date in config.dates

Caches everything to data/candles_cache.json so scan_combos.py can run
repeatedly without re-hitting the API.
"""

import json
import os
import time

import requests
import yaml

FYERS_HISTORY_URL = "https://api-t1.fyers.in/data/history"
CONFIG_PATH = os.environ.get("SCAN_CONFIG", "config/scan_config.yaml")
CACHE_PATH = os.environ.get("CACHE_PATH", "data/candles_cache.json")


def get_auth_header():
    token = os.environ.get("FYERS_TOKEN")
    if not token or ":" not in token:
        raise SystemExit("ERROR: FYERS_TOKEN env var missing or malformed.")
    return token


def build_option_symbol(expiry_code, strike, option_type):
    return f"NSE:NIFTY{expiry_code}{strike}{option_type}"


def fetch_candles(symbol, resolution, date_from, date_to, auth_header):
    params = {
        "symbol": symbol,
        "resolution": resolution,
        "date_format": "1",
        "range_from": date_from,
        "range_to": date_to,
        "cont_flag": "1",
    }
    headers = {"Authorization": auth_header}
    resp = requests.get(FYERS_HISTORY_URL, params=params, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if data.get("s") != "ok":
        print(f"  WARN: no data for {symbol} on {date_from}: {data}")
        return []
    return [
        {"ts": int(c[0]), "open": c[1], "high": c[2], "low": c[3], "close": c[4], "volume": c[5]}
        for c in data.get("candles", [])
    ]


def main():
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)

    auth_header = get_auth_header()
    resolution = str(cfg["resolution"])
    strikes = range(cfg["strikes"]["start"], cfg["strikes"]["end"] + 1, cfg["strikes"]["step"])

    near_code = cfg["expiries"]["near"]["code"]
    far_code = cfg["expiries"]["far"]["code"]

    symbols = {"underlying": cfg["underlying_symbol"]}
    for strike in strikes:
        for opt in ("CE", "PE"):
            symbols[f"near_{strike}_{opt}"] = build_option_symbol(near_code, strike, opt)
            symbols[f"far_{strike}_{opt}"] = build_option_symbol(far_code, strike, opt)

    cache = {}
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH) as f:
            cache = json.load(f)

    for date in cfg["dates"]:
        cache.setdefault(date, {})
        print(f"=== {date} ===")
        for key, symbol in symbols.items():
            if key in cache[date]:
                continue  # already cached
            print(f"  fetching {key} -> {symbol}")
            try:
                candles = fetch_candles(symbol, resolution, date, date, auth_header)
            except Exception as e:
                print(f"  ERROR fetching {symbol}: {e}")
                candles = []
            cache[date][key] = {"symbol": symbol, "candles": candles}
            time.sleep(0.25)

        # save progressively so a failed run doesn't lose earlier work
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        with open(CACHE_PATH, "w") as f:
            json.dump(cache, f)

    print(f"Done. Cache written to {CACHE_PATH}")


if __name__ == "__main__":
    main()
