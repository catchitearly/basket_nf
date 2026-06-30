#!/usr/bin/env python3
"""
For each date, each 5-min candle, and each strike in the configured range:
  - builds the near-CE/far-CE combo (and near-PE/far-PE combo)
  - solves the integer ratio (lots_far : lots_near) that gets net premium
    closest to zero (cost-free spread)
  - computes IV + Greeks for both legs via Black-Scholes
  - flags it as a candidate entry if net delta is inside the neutral band
    and net theta is positive
  - for the best candidate of the day, simulates holding from that entry
    to the configured square-off time and produces a P&L curve

Output: data/scan_results.json (one entry per date) and a copy into
docs/scan_results.json so the dashboard can read it.
"""

import json
import os
from datetime import datetime, date as ddate
from fractions import Fraction

import yaml

from greeks import bs_price, implied_vol, greeks, time_to_expiry_years

CONFIG_PATH = os.environ.get("SCAN_CONFIG", "config/scan_config.yaml")
CACHE_PATH = os.environ.get("CACHE_PATH", "data/candles_cache.json")
OUTPUT_PATH = os.environ.get("SCAN_OUTPUT", "data/scan_results.json")
DOCS_OUTPUT_PATH = os.environ.get("DOCS_SCAN_OUTPUT", "docs/scan_results.json")


def load_cfg():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def load_cache():
    with open(CACHE_PATH) as f:
        return json.load(f)


def index_by_ts(candles):
    return {c["ts"]: c for c in candles}


def solve_zero_cost_ratio(near_price, far_price, max_leg=10, tolerance=0.05):
    """Find integers (far_lots, near_lots) <= max_leg minimizing
    abs(far_lots*far_price - near_lots*near_price) / (far_lots*far_price + near_lots*near_price).
    Returns (far_lots, near_lots, imbalance_pct) or None if nothing within tolerance."""
    if near_price <= 0 or far_price <= 0:
        return None

    best = None
    for near_lots in range(1, max_leg + 1):
        for far_lots in range(1, max_leg + 1):
            cost_far = far_lots * far_price
            cost_near = near_lots * near_price
            gross = cost_far + cost_near
            imbalance = abs(cost_far - cost_near) / gross
            if best is None or imbalance < best[2]:
                best = (far_lots, near_lots, imbalance)

    if best and best[2] <= tolerance:
        return best
    return best  # return best-effort even if outside tolerance; caller checks


def analyze_leg(price, S, K, expiry_date, current_ts, r, option_type):
    T = time_to_expiry_years(current_ts, expiry_date)
    iv = implied_vol(price, S, K, T, r, option_type)
    if iv is None:
        return None
    g = greeks(S, K, T, r, iv, option_type)
    g["iv"] = iv
    g["T"] = T
    return g


def scan_date(date_str, day_data, cfg):
    r = cfg["risk_free_rate"]
    strikes = list(range(cfg["strikes"]["start"], cfg["strikes"]["end"] + 1, cfg["strikes"]["step"]))
    near_expiry_date = ddate.fromisoformat(cfg["expiries"]["near"]["date"])
    far_expiry_date = ddate.fromisoformat(cfg["expiries"]["far"]["date"])
    lot_size = cfg["lot_size"]
    delta_band = cfg["delta_neutral_band"]
    require_pos_theta = cfg["require_positive_theta"]
    max_leg = cfg["max_ratio_leg"]
    tolerance = cfg["ratio_tolerance"]

    underlying = index_by_ts(day_data["underlying"]["candles"])
    if not underlying:
        return {"date": date_str, "error": "no underlying data"}

    timestamps = sorted(underlying.keys())
    candidates = []

    for opt_type in ("CE", "PE"):
        for strike in strikes:
            near_key = f"near_{strike}_{opt_type}"
            far_key = f"far_{strike}_{opt_type}"
            if near_key not in day_data or far_key not in day_data:
                continue
            near_candles = index_by_ts(day_data[near_key]["candles"])
            far_candles = index_by_ts(day_data[far_key]["candles"])

            for ts in timestamps:
                if ts not in near_candles or ts not in far_candles:
                    continue
                S = underlying[ts]["close"]
                near_price = near_candles[ts]["close"]
                far_price = far_candles[ts]["close"]
                if near_price <= 0 or far_price <= 0:
                    continue

                ratio = solve_zero_cost_ratio(near_price, far_price, max_leg, tolerance)
                if ratio is None:
                    continue
                far_lots, near_lots, imbalance = ratio
                if imbalance > tolerance:
                    continue

                near_g = analyze_leg(near_price, S, strike, near_expiry_date, ts, r, opt_type)
                far_g = analyze_leg(far_price, S, strike, far_expiry_date, ts, r, opt_type)
                if near_g is None or far_g is None:
                    continue

                # Buy far, sell near
                net_delta = far_lots * far_g["delta"] - near_lots * near_g["delta"]
                net_theta = far_lots * far_g["theta"] - near_lots * near_g["theta"]
                net_vega = far_lots * far_g["vega"] - near_lots * near_g["vega"]

                delta_neutral = abs(net_delta) <= delta_band
                theta_ok = (net_theta > 0) if require_pos_theta else True

                if delta_neutral and theta_ok:
                    candidates.append({
                        "ts": ts,
                        "strike": strike,
                        "option_type": opt_type,
                        "far_lots": far_lots,
                        "near_lots": near_lots,
                        "near_price": near_price,
                        "far_price": far_price,
                        "imbalance_pct": round(imbalance * 100, 2),
                        "net_delta": round(net_delta, 4),
                        "net_theta": round(net_theta, 2),
                        "net_vega": round(net_vega, 2),
                        "near_iv": round(near_g["iv"] * 100, 2),
                        "far_iv": round(far_g["iv"] * 100, 2),
                    })

    if not candidates:
        return {"date": date_str, "candidates": [], "best": None, "pnl_curve": []}

    # rank: highest net_theta per unit net_vega (more decay per unit of vol risk)
    for c in candidates:
        c["score"] = c["net_theta"] / (abs(c["net_vega"]) + 1e-6)
    candidates.sort(key=lambda c: c["score"], reverse=True)
    best = candidates[0]

    # simulate holding the best candidate from its entry ts to square-off
    pnl_curve = simulate_pnl(best, day_data, cfg, timestamps)

    return {
        "date": date_str,
        "candidates": candidates[:20],   # cap for output size
        "best": best,
        "pnl_curve": pnl_curve,
    }


def simulate_pnl(best, day_data, cfg, timestamps):
    strike = best["strike"]
    opt_type = best["option_type"]
    near_key = f"near_{strike}_{opt_type}"
    far_key = f"far_{strike}_{opt_type}"
    near_candles = index_by_ts(day_data[near_key]["candles"])
    far_candles = index_by_ts(day_data[far_key]["candles"])
    lot_size = cfg["lot_size"]

    square_off_h, square_off_m = [int(x) for x in cfg["square_off_time"].split(":")]
    entry_ts = best["ts"]
    near_entry = best["near_price"]
    far_entry = best["far_price"]
    far_lots = best["far_lots"]
    near_lots = best["near_lots"]

    curve = []
    for ts in timestamps:
        if ts < entry_ts:
            continue
        dt = datetime.fromtimestamp(ts)
        if (dt.hour, dt.minute) > (square_off_h, square_off_m):
            break
        if ts not in near_candles or ts not in far_candles:
            continue
        near_now = near_candles[ts]["close"]
        far_now = far_candles[ts]["close"]

        far_pnl = far_lots * (far_now - far_entry) * lot_size           # long far
        near_pnl = near_lots * (near_entry - near_now) * lot_size       # short near
        total = round(far_pnl + near_pnl, 2)
        curve.append({"ts": ts, "far_pnl": round(far_pnl, 2), "near_pnl": round(near_pnl, 2), "total": total})

    return curve


def main():
    cfg = load_cfg()
    cache = load_cache()

    results = []
    for date_str in cfg["dates"]:
        if date_str not in cache:
            print(f"WARN: no cached data for {date_str}, run fetch_chain.py first")
            continue
        print(f"Scanning {date_str} ...")
        result = scan_date(date_str, cache[date_str], cfg)
        results.append(result)
        n = len(result.get("candidates", []))
        best = result.get("best")
        if best:
            print(f"  {n} candidates. Best: strike {best['strike']}{best['option_type']} "
                  f"buy{best['far_lots']}far:sell{best['near_lots']}near score={best['score']:.3f}")
        else:
            print(f"  {n} candidates. No qualifying combo found.")

    output = {"generated_at": datetime.utcnow().isoformat() + "Z", "config": cfg, "results": results}

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(output, f, indent=2, default=str)

    os.makedirs(os.path.dirname(DOCS_OUTPUT_PATH), exist_ok=True)
    with open(DOCS_OUTPUT_PATH, "w") as f:
        json.dump(output, f, indent=2, default=str)

    print(f"Wrote results to {OUTPUT_PATH} and {DOCS_OUTPUT_PATH}")


if __name__ == "__main__":
    main()
