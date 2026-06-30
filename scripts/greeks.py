"""
Black-Scholes pricing, implied-volatility solver, and Greeks for European
options (Nifty options are European-style, so BS is appropriate intraday).
"""

import math
from datetime import datetime, time as dtime

N = lambda x: 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))           # CDF
n = lambda x: math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)    # PDF


def bs_price(S, K, T, r, sigma, option_type):
    if T <= 0 or sigma <= 0:
        intrinsic = max(0.0, S - K) if option_type == "CE" else max(0.0, K - S)
        return intrinsic
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if option_type == "CE":
        return S * N(d1) - K * math.exp(-r * T) * N(d2)
    else:
        return K * math.exp(-r * T) * N(-d2) - S * N(-d1)


def implied_vol(price, S, K, T, r, option_type, lo=0.001, hi=5.0, tol=1e-4, max_iter=60):
    """Bisection IV solver -- robust even where vega is tiny (deep ITM/OTM)."""
    if T <= 0:
        return None
    intrinsic = max(0.0, S - K) if option_type == "CE" else max(0.0, K - S)
    if price <= intrinsic + 1e-6:
        return None  # no time value left to invert

    for _ in range(max_iter):
        mid = (lo + hi) / 2
        p = bs_price(S, K, T, r, mid, option_type)
        if abs(p - price) < tol:
            return mid
        if p > price:
            hi = mid
        else:
            lo = mid
    return mid


def greeks(S, K, T, r, sigma, option_type):
    """Returns dict: delta, theta (per calendar day), vega, gamma."""
    if T <= 0 or sigma is None or sigma <= 0:
        return {"delta": 0.0, "theta": 0.0, "vega": 0.0, "gamma": 0.0}

    sqrtT = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * sqrtT)
    d2 = d1 - sigma * sqrtT

    if option_type == "CE":
        delta = N(d1)
        theta_annual = (
            -(S * n(d1) * sigma) / (2 * sqrtT) - r * K * math.exp(-r * T) * N(d2)
        )
    else:
        delta = N(d1) - 1.0
        theta_annual = (
            -(S * n(d1) * sigma) / (2 * sqrtT) + r * K * math.exp(-r * T) * N(-d2)
        )

    vega = S * n(d1) * sqrtT / 100.0       # per 1% vol move
    gamma = n(d1) / (S * sigma * sqrtT)
    theta_per_day = theta_annual / 365.0

    return {"delta": delta, "theta": theta_per_day, "vega": vega, "gamma": gamma}


def time_to_expiry_years(current_ts, expiry_date, expiry_close_time=dtime(15, 30)):
    """current_ts: epoch seconds. expiry_date: datetime.date."""
    now = datetime.fromtimestamp(current_ts)
    expiry_dt = datetime.combine(expiry_date, expiry_close_time)
    seconds = max((expiry_dt - now).total_seconds(), 1.0)
    return seconds / (365.0 * 24 * 3600)
