"""
Watchlist Pipeline — two-stage entry system.
Stage 1: Stock scores 65-79 → goes on WATCHLIST with a trigger price.
Stage 2: Next scan, if price crosses trigger + volume confirms → BUY.

This stops us from chasing stocks mid-move. We identify the gem early,
set a trigger (e.g. breakout above today's high), and wait for confirmation.

Persisted to /tmp/stock_watchlist.json so it survives cron restarts.
"""

import json
import os
import time

WATCHLIST_FILE = "/tmp/stock_watchlist.json"
WATCHLIST_EXPIRE_DAYS = 5  # remove if not triggered within 5 days


def _load():
    if os.path.exists(WATCHLIST_FILE):
        try:
            with open(WATCHLIST_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save(wl):
    with open(WATCHLIST_FILE, "w") as f:
        json.dump(wl, f)


def add(symbol, score, breakdown, catalyst_summary, catalyst_type, trade_type,
        trigger_price, stop, target, hold_period, flags, reasons):
    """Add a stock to the watchlist."""
    wl = _load()
    wl[symbol] = {
        "score":            score,
        "breakdown":        breakdown,
        "catalyst_summary": catalyst_summary,
        "catalyst_type":    catalyst_type,
        "trade_type":       trade_type,
        "trigger_price":    round(trigger_price, 4),
        "stop":             round(stop, 4),
        "target":           round(target, 4),
        "hold_period":      hold_period,
        "flags":            flags,
        "reasons":          reasons,
        "added":            time.time(),
    }
    _save(wl)
    return wl[symbol]


def get_all():
    return _load()


def remove(symbol):
    wl = _load()
    if symbol in wl:
        del wl[symbol]
        _save(wl)


def check_triggers(stocks, held_symbols):
    """
    Compare live prices against watchlist trigger prices.
    Returns list of triggered entries ready to buy.
    Automatically expires stale entries.
    """
    wl = _load()
    if not wl:
        return []

    price_map  = {s["symbol"]: s for s in stocks}
    triggered  = []
    to_remove  = []
    expire_cut = time.time() - (WATCHLIST_EXPIRE_DAYS * 86400)

    for symbol, entry in wl.items():
        if symbol in held_symbols:
            to_remove.append(symbol)
            continue

        # Expire old entries
        if entry["added"] < expire_cut:
            print(f"[WATCHLIST] Expiring {symbol} — no trigger in {WATCHLIST_EXPIRE_DAYS} days", flush=True)
            to_remove.append(symbol)
            continue

        stock = price_map.get(symbol)
        if not stock:
            continue

        # Trigger condition: price crosses trigger AND volume confirms (1.2x+ normal)
        price_ok  = stock["price"] >= entry["trigger_price"]
        volume_ok = stock.get("rel_volume", 1.0) >= 1.2

        if price_ok and volume_ok:
            print(f"[WATCHLIST] TRIGGERED: {symbol} at ${stock['price']:.2f} "
                  f"(trigger: ${entry['trigger_price']:.2f}, rel_vol: {stock.get('rel_volume', 1):.1f}x)", flush=True)
            triggered.append({**stock, **entry})
            to_remove.append(symbol)

    for sym in to_remove:
        remove(sym)

    return triggered
