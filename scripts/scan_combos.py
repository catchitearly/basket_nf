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


def find_atm_strike(spot, strikes):
    return min(strikes, key=lambda k: abs(k - spot))


def find_itm1_strike(spot, strikes, option_type):
    """One strike in-the-money relative to spot.
    CE is ITM below spot -> next strike down from ATM.
    PE is ITM above spot -> next strike up from ATM."""
    atm = find_atm_strike(spot, strikes)
    step = strikes[1] - strikes[0] if len(strikes) > 1 else 0
    candidate = atm - step if option_type == "CE" else atm + step
    return candidate if candidate in strikes else atm


def search_theta_max_ratio(near_g, far_g, max_leg, delta_band, gamma_band):
    """Search (far_lots, near_lots) pairs up to max_leg; among those satisfying
    the delta and gamma caps, return the one with the highest net theta."""
    best = None
    for far_lots in range(1, max_leg + 1):
        for near_lots in range(1, max_leg + 1):
            net_delta = far_lots * far_g["delta"] - near_lots * near_g["delta"]
            net_gamma = far_lots * far_g["gamma"] - near_lots * near_g["gamma"]
            net_theta = far_lots * far_g["theta"] - near_lots * near_g["theta"]
            net_vega = far_lots * far_g["vega"] - near_lots * near_g["vega"]

            if abs(net_delta) > delta_band or abs(net_gamma) > gamma_band:
                continue
            if best is None or net_theta > best["net_theta"]:
                best = {
                    "far_lots": far_lots, "near_lots": near_lots,
                    "net_delta": net_delta, "net_gamma": net_gamma,
                    "net_theta": net_theta, "net_vega": net_vega,
                }
    return best


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
    gamma_band = cfg["gamma_band"]
    require_pos_theta = cfg["require_positive_theta"]
    max_leg = cfg["max_ratio_leg"]
    strike_modes = cfg["strike_modes"]
    option_types = cfg["option_types"]

    underlying = index_by_ts(day_data["underlying"]["candles"])
    if not underlying:
        return {"date": date_str, "error": "no underlying data"}

    timestamps = sorted(underlying.keys())
    candidates = []

    for ts in timestamps:
        S = underlying[ts]["close"]

        for opt_type in option_types:
            for mode in strike_modes:
                strike = find_atm_strike(S, strikes) if mode == "ATM" else find_itm1_strike(S, strikes, opt_type)

                near_key = f"near_{strike}_{opt_type}"
                far_key = f"far_{strike}_{opt_type}"
                if near_key not in day_data or far_key not in day_data:
                    continue
                near_candles = index_by_ts(day_data[near_key]["candles"])
                far_candles = index_by_ts(day_data[far_key]["candles"])
                if ts not in near_candles or ts not in far_candles:
                    continue

                near_price = near_candles[ts]["close"]
                far_price = far_candles[ts]["close"]
                if near_price <= 0 or far_price <= 0:
                    continue

                near_g = analyze_leg(near_price, S, strike, near_expiry_date, ts, r, opt_type)
                far_g = analyze_leg(far_price, S, strike, far_expiry_date, ts, r, opt_type)
                if near_g is None or far_g is None:
                    continue

                ratio = search_theta_max_ratio(near_g, far_g, max_leg, delta_band, gamma_band)
                if ratio is None:
                    continue
                if require_pos_theta and ratio["net_theta"] <= 0:
                    continue

                far_lots, near_lots = ratio["far_lots"], ratio["near_lots"]
                net_cost = far_lots * far_price - near_lots * near_price  # +debit / -credit

                candidates.append({
                    "ts": ts,
                    "strike": strike,
                    "strike_mode": mode,
                    "option_type": opt_type,
                    "spot": round(S, 2),
                    "far_lots": far_lots,
                    "near_lots": near_lots,
                    "near_price": near_price,
                    "far_price": far_price,
                    "net_cost": round(net_cost, 2),
                    "net_delta": round(ratio["net_delta"], 4),
                    "net_gamma": round(ratio["net_gamma"], 5),
                    "net_theta": round(ratio["net_theta"], 2),
                    "net_vega": round(ratio["net_vega"], 2),
                    "near_iv": round(near_g["iv"] * 100, 2),
                    "far_iv": round(far_g["iv"] * 100, 2),
                })

    if not candidates:
        return {"date": date_str, "candidates": [], "best": None, "pnl_curve": []}

    # rank: highest net theta per unit net vega (more decay per unit of vol risk)
    for c in candidates:
        c["score"] = c["net_theta"] / (abs(c["net_vega"]) + 1e-6)
    candidates.sort(key=lambda c: c["score"], reverse=True)
    best = candidates[0]

    pnl_curve = simulate_pnl(best, day_data, cfg, timestamps)

    return {
        "date": date_str,
        "candidates": candidates[:20],
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
