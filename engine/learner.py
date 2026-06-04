"""
Learning Layer — trade log + win rate by catalyst type.
After every buy: log the reason and setup.
After every exit: log the result.
After 5+ trades per catalyst type: automatically adjust scoring weight.

This makes the bot smarter over time:
  Government contracts → 71% win rate → boost catalyst score for contract news
  Analyst upgrades     → 43% win rate → mild penalty for upgrade-only plays
"""

import json
import os
import time
from collections import defaultdict

TRADE_LOG_FILE = "/tmp/trade_log.json"


def log_entry(symbol, score, breakdown, catalyst_type, trade_type, entry_price, stop, target):
    """Call immediately after placing a buy order."""
    log = _load()
    log[symbol] = {
        "symbol":       symbol,
        "entry_time":   time.time(),
        "entry_price":  entry_price,
        "score":        score,
        "breakdown":    breakdown,
        "catalyst_type": catalyst_type,
        "trade_type":   trade_type,
        "stop":         stop,
        "target":       target,
        "exit_price":   None,
        "exit_reason":  None,
        "pnl_pct":      None,
        "win":          None,
    }
    _save(log)


def log_exit(symbol, exit_price, exit_reason, pnl_pct):
    """Call immediately after closing a position."""
    log = _load()
    if symbol in log:
        log[symbol].update({
            "exit_price":  exit_price,
            "exit_reason": exit_reason,
            "pnl_pct":     pnl_pct,
            "win":         pnl_pct > 0,
            "exit_time":   time.time(),
        })
        _save(log)


def get_win_rates():
    """Compute win rates grouped by catalyst_type and trade_type."""
    log    = _load()
    closed = [t for t in log.values() if t.get("win") is not None]

    if not closed:
        return {}

    by_catalyst  = defaultdict(lambda: {"wins": 0, "total": 0, "avg_pnl": []})
    by_tradetype = defaultdict(lambda: {"wins": 0, "total": 0})

    for trade in closed:
        ct = trade.get("catalyst_type", "unknown")
        tt = trade.get("trade_type", "unknown")

        by_catalyst[ct]["total"]   += 1
        by_tradetype[tt]["total"]  += 1
        if trade.get("pnl_pct") is not None:
            by_catalyst[ct]["avg_pnl"].append(trade["pnl_pct"])

        if trade["win"]:
            by_catalyst[ct]["wins"]  += 1
            by_tradetype[tt]["wins"] += 1

    result = {
        "total_trades":  len(closed),
        "by_catalyst":   {},
        "by_trade_type": {},
    }
    for k, v in by_catalyst.items():
        avg = sum(v["avg_pnl"]) / len(v["avg_pnl"]) if v["avg_pnl"] else 0
        result["by_catalyst"][k] = {
            "win_rate": round(v["wins"] / v["total"] * 100, 1),
            "trades":   v["total"],
            "avg_pnl":  round(avg, 2),
        }
    for k, v in by_tradetype.items():
        result["by_trade_type"][k] = {
            "win_rate": round(v["wins"] / v["total"] * 100, 1),
            "trades":   v["total"],
        }
    return result


def get_catalyst_multiplier(catalyst_type):
    """
    Return a score multiplier based on historical win rate for this catalyst type.
    Requires 5+ trades before adjusting (not enough data = neutral 1.0).
    Range: 0.75 (bad catalyst, <30% win rate) to 1.25 (great catalyst, >70% win rate)
    """
    rates = get_win_rates()
    data  = rates.get("by_catalyst", {}).get(catalyst_type, {})

    if data.get("trades", 0) < 5:
        return 1.0

    win_rate = data["win_rate"] / 100
    # Linear scale: 0% win = 0.75x, 50% win = 1.0x, 100% win = 1.25x
    return round(0.75 + win_rate * 0.5, 3)


def print_summary():
    """Print win rate summary to logs."""
    rates = get_win_rates()
    if not rates:
        print("[LEARNER] No completed trades yet.", flush=True)
        return
    print(f"[LEARNER] {rates['total_trades']} completed trades:", flush=True)
    for ct, data in rates.get("by_catalyst", {}).items():
        print(f"  {ct}: {data['win_rate']}% win ({data['trades']} trades, avg P&L {data['avg_pnl']:+.1f}%)", flush=True)


def _load():
    if os.path.exists(TRADE_LOG_FILE):
        try:
            with open(TRADE_LOG_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save(log):
    with open(TRADE_LOG_FILE, "w") as f:
        json.dump(log, f)
