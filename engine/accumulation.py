"""
Accumulation Scanner — Alpaca 15-day bar analysis (free, uses existing key).
Detects institutional accumulation pattern: price in a tight range while volume quietly rises.
This often precedes a breakout move as institutions build a position before the crowd notices.
Cache: per scan cycle (cleared at start of each cycle).
"""

import requests
from config import ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_DATA_URL

HEADERS = {
    "APCA-API-KEY-ID": ALPACA_API_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
}

_cache = {}  # {symbol: (score, description)} — cleared each scan cycle


def check_accumulation(symbol):
    """
    Detect institutional accumulation: tight price range + volume trending up over 15 days.
    Returns (score 0-15, description)
    """
    if symbol in _cache:
        return _cache[symbol]

    result = _fetch(symbol)
    _cache[symbol] = result
    return result


def _fetch(symbol):
    try:
        url = (
            f"{ALPACA_DATA_URL}/v2/stocks/{symbol}/bars"
            f"?timeframe=1Day&limit=15&adjustment=raw"
        )
        r = requests.get(url, headers=HEADERS, timeout=8)
        bars = r.json().get("bars", [])

        if len(bars) < 8:
            return (0, "")

        closes  = [b["c"] for b in bars]
        volumes = [b["v"] for b in bars]

        avg_price       = sum(closes) / len(closes)
        price_range_pct = (max(closes) - min(closes)) / avg_price * 100

        mid              = len(volumes) // 2
        early_vol_avg    = sum(volumes[:mid]) / mid
        recent_vol_avg   = sum(volumes[mid:]) / (len(volumes) - mid)
        vol_trend        = recent_vol_avg / early_vol_avg if early_vol_avg > 0 else 1.0

        # Classic accumulation: price going sideways while volume quietly increases
        if price_range_pct < 4.0 and vol_trend >= 1.5:
            return (15, f"Strong accumulation: {price_range_pct:.1f}% price range over {len(bars)} days, volume rising {vol_trend:.1f}x")
        elif price_range_pct < 6.0 and vol_trend >= 1.3:
            return (8, f"Mild accumulation: {price_range_pct:.1f}% price range, volume up {vol_trend:.1f}x")
        else:
            return (0, "")

    except Exception:
        return (0, "")


def clear_cache():
    """Call at the start of each scan cycle to ensure fresh data."""
    _cache.clear()
