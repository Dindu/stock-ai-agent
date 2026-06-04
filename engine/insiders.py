"""
Insider Buying Scanner — SEC EDGAR Form 4 filings (free, no API key required).
Checks for recent insider transactions. Multiple filings = institutional confidence signal.
Cache: 4 hours per symbol (insider data doesn't change minute to minute).

Uses EDGAR company search by ticker to get the CIK, then fetches actual Form 4 filings
for that specific company — avoids false positives from common-word tickers like T, TECH.
"""

import requests
from datetime import datetime, timedelta

_cache = {}          # {symbol: (timestamp, (score, description))}
_cik_cache = {}      # {symbol: cik_str}
_CACHE_TTL = 14400   # 4 hours


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


def _get_cik(symbol):
    """Resolve ticker → CIK using EDGAR's company tickers JSON (cached)."""
    if symbol in _cik_cache:
        return _cik_cache[symbol]
    try:
        r = requests.get(
            "https://www.sec.gov/files/company_tickers.json",
            headers={"User-Agent": "StockScanBot research@stockscan.com"},
            timeout=8,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        for entry in data.values():
            if entry.get("ticker", "").upper() == symbol.upper():
                cik = str(entry["cik_str"]).zfill(10)
                _cik_cache[symbol] = cik
                return cik
    except Exception:
        pass
    return None


def _fetch(symbol, now):
    """
    Fetch Form 4 filings via EDGAR submissions API using CIK.
    Counts filings in the last 30 days for this exact company.
    """
    try:
        cik = _get_cik(symbol)
        if not cik:
            return (0, "")

        url = f"https://data.sec.gov/submissions/CIK{cik}.json"
        r = requests.get(
            url,
            headers={"User-Agent": "StockScanBot research@stockscan.com"},
            timeout=8,
        )
        if r.status_code != 200:
            return (0, "")

        filings = r.json().get("filings", {}).get("recent", {})
        forms = filings.get("form", [])
        dates = filings.get("filingDate", [])

        cutoff = (now - timedelta(days=30)).strftime("%Y-%m-%d")
        recent_count = sum(
            1 for form, date in zip(forms, dates)
            if form == "4" and date >= cutoff
        )

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
