import requests
from datetime import datetime, timedelta
from config import ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_DATA_URL

HEADERS = {
    "APCA-API-KEY-ID": ALPACA_API_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY
}

_macro_cache = {"data": None, "fetched_at": None}


def get_news(symbol, limit=5):
    """Fetch recent news headlines for a symbol from Alpaca."""
    try:
        end = datetime.utcnow()
        start = end - timedelta(days=3)
        r = requests.get(
            f"{ALPACA_DATA_URL}/v1beta1/news",
            headers=HEADERS,
            params={
                "symbols": symbol,
                "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "limit": limit,
                "sort": "desc"
            },
            timeout=10
        )
        if r.status_code != 200:
            return []
        articles = r.json().get("news", [])
        return [a["headline"] for a in articles]
    except Exception:
        return []


def get_macro():
    """
    Fetch macro indicators: SPY price/change and VIX level.
    Cached for the duration of a scan cycle (call refresh_macro() each cycle).
    """
    if _macro_cache["data"]:
        return _macro_cache["data"]

    macro = {}
    try:
        r = requests.get(
            f"{ALPACA_DATA_URL}/v2/stocks/snapshots?symbols=SPY,VIX",
            headers=HEADERS,
            timeout=10
        )
        if r.status_code == 200:
            data = r.json()
            if "SPY" in data:
                bar = data["SPY"].get("dailyBar", {})
                spy_change = ((bar.get("c", 0) - bar.get("o", 1)) / bar.get("o", 1)) * 100
                macro["spy_change_pct"] = round(spy_change, 2)
                macro["market_trend"] = "bullish" if spy_change > 0.3 else "bearish" if spy_change < -0.3 else "neutral"
    except Exception:
        pass

    # VIX via VIXY ETF (tracks VIX, available on Alpaca free tier)
    try:
        r2 = requests.get(
            f"{ALPACA_DATA_URL}/v2/stocks/VIXY/bars?timeframe=1Day&limit=1",
            headers=HEADERS, timeout=8
        )
        if r2.status_code == 200:
            bars = r2.json().get("bars", [])
            if bars:
                vixy = bars[-1]["c"]
                # VIXY ≈ VIX/4.5 rough approximation for fear level
                vix_approx = round(vixy, 1)
                macro["vix"] = vix_approx
                macro["fear_level"] = "high" if vix_approx > 20 else "low" if vix_approx < 12 else "moderate"
    except Exception:
        pass

    _macro_cache["data"] = macro
    return macro


def refresh_macro():
    """Call at the start of each scan cycle to clear the macro cache."""
    _macro_cache["data"] = None
    _macro_cache["fetched_at"] = None
