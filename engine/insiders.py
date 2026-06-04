"""
Insider Buying Scanner — SEC EDGAR Form 4 filings (free, no API key required).
Checks for recent insider transactions. Multiple filings = institutional confidence signal.
Cache: 4 hours per symbol (insider data doesn't change minute to minute).
"""

import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

_cache = {}  # {symbol: (timestamp, (score, description))}
_CACHE_TTL = 14400  # 4 hours


def get_insider_signal(symbol):
    """
    Pull recent Form 4 filings from SEC EDGAR for the symbol.
    Returns (score 0-20, description)
    """
    now = datetime.now()

    if symbol in _cache:
        cached_time, cached_result = _cache[symbol]
        if (now - cached_time).seconds < _CACHE_TTL:
            return cached_result

    result = _fetch(symbol, now)
    _cache[symbol] = (now, result)
    return result


def _fetch(symbol, now):
    """
    Use SEC EDGAR full-text search API to find recent Form 4 filings.
    This is the reliable free endpoint — returns JSON, no XML parsing needed.
    """
    try:
        url = (
            f"https://efts.sec.gov/LATEST/search-index?q=%22{symbol}%22"
            f"&dateRange=custom&startdt={(now - timedelta(days=30)).strftime('%Y-%m-%d')}"
            f"&enddt={now.strftime('%Y-%m-%d')}&forms=4"
        )
        r = requests.get(
            url,
            headers={"User-Agent": "StockScanBot research@stockscan.com"},
            timeout=8,
        )
        if r.status_code != 200:
            return (0, "")

        hits = r.json().get("hits", {}).get("hits", [])
        recent_count = len(hits)

        print(f"[INSIDERS] {symbol}: {recent_count} Form 4 filing(s) in last 30 days", flush=True)

        if recent_count >= 3:
            return (20, f"{recent_count} insider filings in last 30 days — multiple insiders active")
        elif recent_count == 2:
            return (12, f"2 insider filings in last 30 days")
        elif recent_count == 1:
            return (5, f"1 insider filing in last 30 days")
        else:
            return (0, "")

    except Exception as e:
        print(f"[INSIDERS] {symbol} error: {e}", flush=True)
        return (0, "")
