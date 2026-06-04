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
    try:
        url = (
            f"https://www.sec.gov/cgi-bin/browse-edgar"
            f"?action=getcompany&symbol={symbol}&type=4"
            f"&dateb=&owner=include&count=10&output=atom"
        )
        r = requests.get(
            url,
            headers={"User-Agent": "StockScanBot research@stockscan.com"},
            timeout=8,
        )
        if r.status_code != 200:
            return (0, "")

        root = ET.fromstring(r.text)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        entries = root.findall("atom:entry", ns)

        cutoff = now - timedelta(days=30)
        recent_count = 0

        for entry in entries:
            updated = entry.find("atom:updated", ns)
            if updated is not None:
                try:
                    dt = datetime.fromisoformat(updated.text[:10])
                    if dt >= cutoff:
                        recent_count += 1
                except Exception:
                    pass

        if recent_count >= 3:
            return (20, f"{recent_count} insider filings in last 30 days — multiple insiders active")
        elif recent_count == 2:
            return (12, f"2 insider filings in last 30 days")
        elif recent_count == 1:
            return (5, f"1 insider filing in last 30 days")
        else:
            return (0, "")

    except Exception:
        return (0, "")
