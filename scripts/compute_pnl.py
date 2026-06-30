#!/usr/bin/env python3
"""
Computes the historical P&L-vs-time for a multi-leg Nifty options basket
using the Fyers "history" (candle) API, and writes a JSON file consumable
by the docs/index.html dashboard.

Auth:
  Expects an env var FYERS_TOKEN in the form:
      "<APP_ID>:<ACCESS_TOKEN>"
  e.g. "ABC123-100:eyJhbGciOiJIUzI1NiIs..."

  APP_ID is the Fyers app id you created the access token with
  (looks like "XXXXXX-100"). ACCESS_TOKEN is the daily token you already
  generate. Store the combined string as a GitHub Actions secret called
  FYERS_TOKEN.
"""

import json
import os
import sys
import time
from datetime import datetime

import requests
import yaml

FYERS_HISTORY_URL = "https://api-t1.fyers.in/data/history"

CONFIG_PATH = os.environ.get("BASKET_CONFIG", "config/basket.yaml")
OUTPUT_PATH = os.environ.get("OUTPUT_PATH", "docs/pnl_timeseries.json")


def load_config(path):
    with open(path, "r") as f:
        return yaml.safe_load(f)


def get_auth_header():
    token = os.environ.get("FYERS_TOKEN")
    if not token or ":" not in token:
        sys.exit(
            "ERROR: FYERS_TOKEN env var missing or malformed. "
            "Expected '<APP_ID>:<ACCESS_TOKEN>'."
        )
    return token  # Fyers expects this exact string as the Authorization header


def fetch_candles(symbol, resolution, date_from, date_to, auth_header):
    params = {
        "symbol": symbol,
        "resolution": resolution,
        "date_format": "1",      # 1 = yyyy-mm-dd strings
        "range_from": date_from,
        "range_to": date_to,
        "cont_flag": "1",
    }
    headers = {"Authorization": auth_header}

    resp = requests.get(FYERS_HISTORY_URL, params=params, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    if data.get("s") != "ok":
        raise RuntimeError(f"Fyers API error for {symbol}: {data}")

    candles = data.get("candles", [])
    # Each candle: [epoch_ts, open, high, low, close, volume]
    return [
        {
            "ts": int(c[0]),
            "open": c[1],
            "high": c[2],
            "low": c[3],
            "close": c[4],
            "volume": c[5],
        }
        for c in candles
    ]


def compute_leg_pnl(leg, candles):
    if not candles:
        return [], None

    entry_price = leg.get("entry_price")
    if entry_price is None:
        entry_price = candles[0]["close"]

    side = leg["side"].upper()
    qty = leg["lots"] * leg["lot_size"]
    sign = 1 if side == "BUY" else -1

    series = []
    for c in candles:
        pnl = sign * (c["close"] - entry_price) * qty
        series.append({"ts": c["ts"], "pnl": round(pnl, 2), "ltp": c["close"]})

    return series, entry_price


def merge_series(per_leg_series):
    """Align all legs on the union of timestamps, forward-filling each
    leg's last-known P&L for timestamps where that leg has no candle."""
    all_ts = sorted({pt["ts"] for series in per_leg_series.values() for pt in series})

    last_known = {leg_name: 0.0 for leg_name in per_leg_series}
    leg_index = {leg_name: 0 for leg_name in per_leg_series}

    merged = []
    for ts in all_ts:
        row = {"ts": ts, "legs": {}, "total": 0.0}
        for leg_name, series in per_leg_series.items():
            idx = leg_index[leg_name]
            while idx < len(series) and series[idx]["ts"] <= ts:
                last_known[leg_name] = series[idx]["pnl"]
                idx += 1
            leg_index[leg_name] = idx
            row["legs"][leg_name] = last_known[leg_name]
            row["total"] += last_known[leg_name]
        row["total"] = round(row["total"], 2)
        merged.append(row)

    return merged


def main():
    cfg = load_config(CONFIG_PATH)
    auth_header = get_auth_header()

    date_from = cfg["date_range"]["from"]
    date_to = cfg["date_range"]["to"]
    resolution = str(cfg["resolution"])

    per_leg_series = {}
    leg_meta = []

    for leg in cfg["legs"]:
        print(f"Fetching candles for {leg['name']} ({leg['fyers_symbol']})...")
        candles = fetch_candles(
            leg["fyers_symbol"], resolution, date_from, date_to, auth_header
        )
        series, entry_price = compute_leg_pnl(leg, candles)
        per_leg_series[leg["name"]] = series
        leg_meta.append(
            {
                "name": leg["name"],
                "symbol": leg["fyers_symbol"],
                "side": leg["side"],
                "lots": leg["lots"],
                "lot_size": leg["lot_size"],
                "entry_price": entry_price,
            }
        )
        time.sleep(0.3)  # gentle on rate limits

    merged = merge_series(per_leg_series)

    output = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "date_range": cfg["date_range"],
        "resolution": resolution,
        "legs": leg_meta,
        "timeseries": merged,
    }

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(output, f, indent=2)

    print(f"Wrote {len(merged)} points to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
