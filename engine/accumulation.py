"""
Accumulation Scanner — yfinance 20-day bar analysis (free, no API key needed).
Detects institutional accumulation pattern: price in a tight range while volume quietly rises.
This often precedes a breakout move as institutions build a position before the crowd notices.
Cache: per scan cycle (cleared at start of each cycle).
"""

import yfinance as yf

_cache = {}  # {symbol: (score, description)} — cleared each scan cycle


def check_accumulation(symbol):
    """
    Detect institutional accumulation: tight price range + volume trending up over 20 days.
    Returns (score 0-15, description)
    """
    if symbol in _cache:
        return _cache[symbol]

    result = _fetch(symbol)
    _cache[symbol] = result
    return result


def _fetch(symbol):
    try:
        df = yf.download(symbol, period="20d", interval="1d", progress=False, auto_adjust=False)
        if df is None or len(df) < 8:
            return (0, "")

        # yfinance returns multi-level columns when auto_adjust=False: (field, ticker)
        close_col  = ("Close",  symbol)
        open_col   = ("Open",   symbol)
        volume_col = ("Volume", symbol)
        if close_col not in df.columns:
            # Fallback for single-ticker simple column names
            close_col  = "Close"
            open_col   = "Open"
            volume_col = "Volume"

        closes  = df[close_col].dropna().tolist()
        opens   = df[open_col].dropna().tolist()
        volumes = df[volume_col].dropna().tolist()

        n = min(len(closes), len(opens), len(volumes))
        closes, opens, volumes = closes[:n], opens[:n], volumes[:n]

        avg_price       = sum(closes) / len(closes)
        price_range_pct = (max(closes) - min(closes)) / avg_price * 100

        mid              = len(volumes) // 2
        early_vol_avg    = sum(volumes[:mid]) / mid
        recent_vol_avg   = sum(volumes[mid:]) / (len(volumes) - mid)
        vol_trend        = recent_vol_avg / early_vol_avg if early_vol_avg > 0 else 1.0

        # Signal 1: Classic accumulation — tight range + rising volume
        # Thresholds adjusted for volatile market conditions
        if price_range_pct < 6.0 and vol_trend >= 1.4:
            return (15, f"Strong accumulation: {price_range_pct:.1f}% range, volume up {vol_trend:.1f}x over {n} days")
        if price_range_pct < 12.0 and vol_trend >= 1.2:
            return (8, f"Mild accumulation: {price_range_pct:.1f}% range, volume up {vol_trend:.1f}x")

        # Signal 2: On-balance volume pressure — more buying days with big volume than selling days
        avg_vol = sum(volumes) / len(volumes)
        up_vol_days   = sum(1 for i in range(n) if closes[i] > opens[i] and volumes[i] > avg_vol)
        down_vol_days = sum(1 for i in range(n) if closes[i] < opens[i] and volumes[i] > avg_vol)
        total_high_vol_days = up_vol_days + down_vol_days

        if total_high_vol_days >= 4 and up_vol_days / total_high_vol_days >= 0.65:
            pct = round(up_vol_days / total_high_vol_days * 100)
            return (8, f"Volume pressure: {pct}% of high-vol days were up days ({up_vol_days}/{total_high_vol_days})")

        return (0, "")

    except Exception:
        return (0, "")

    except Exception:
        return (0, "")


def clear_cache():
    """Call at the start of each scan cycle to ensure fresh data."""
    _cache.clear()
