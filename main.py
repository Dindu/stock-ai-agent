import os
import time
import threading
from collections import Counter
from datetime import datetime, timezone, timedelta

LOCK_FILE = "/tmp/stock_ai_agent.lock"

from engine.scanner import fetch_market
from engine.strategy import score_stock, detect_scenario
from engine.confluence import clear_cache as clear_confluence_cache
from engine.exits import check_exits
from engine import watchlist, learner
from execution.alpaca import buy, get_positions
from output.discord import send, send_watchlist
from config import SCAN_INTERVAL

EST = timezone(timedelta(hours=-5))

def log(msg):
    print(f"[{datetime.now(EST).strftime('%H:%M:%S')}] {msg}", flush=True)

def exit_monitor():
    """Background thread: check exits every 5 min and log learning data."""
    while True:
        time.sleep(300)
        positions = get_positions()
        if positions:
            log(f"[EXIT MONITOR] Checking {len(positions)} open position(s)...")
            check_exits()
        else:
            log("[EXIT MONITOR] No open positions.")


def _place_buy(s, score, breakdown, catalyst_summary, hold_period, trade_type, catalyst_type, flags, reasons, label="BUY"):
    """Place a buy order and log everything."""
    target_pct = 1.12 if score >= 85 else 1.10 if score >= 80 else 1.08
    stop       = round(s["price"] * 0.97, 4)
    target     = round(s["price"] * target_pct, 4)
    risk       = s["price"] - stop
    reward     = target - s["price"]
    rr         = round(reward / risk, 2) if risk > 0 else 0

    log(f"  *** {label}: {s['symbol']} at ${s['price']:.2f} (score={score}) ***")
    result = buy(s["symbol"], 10)

    if result and "id" in result:
        learner.log_entry(
            symbol=s["symbol"], score=score, breakdown=breakdown,
            catalyst_type=catalyst_type, trade_type=trade_type,
            entry_price=s["price"], stop=stop, target=target,
        )
        send({
            **s,
            "score": score, "reasons": reasons, "breakdown": breakdown,
            "catalyst_summary": catalyst_summary, "hold_period": hold_period,
            "trade_type": trade_type, "flags": flags,
            "qty": 10, "stop": stop, "target": target, "rr": rr,
        })
        log(f"  Discord alert sent | Stop: ${stop} | Target: ${target} | R:R {rr}:1")
    return result


def run():
    log("=== 7-Indicator Trading System Starting ===")
    learner.print_summary()

    t = threading.Thread(target=exit_monitor, daemon=True)
    t.start()
    log("Exit monitor started (every 5 min)")

    while True:

        now = datetime.now(EST)
        if now.hour >= 18:
            log("Market closed (after 6:00 PM EST). Exiting.")
            break

        log("─── New Scan Cycle ───")
        positions    = get_positions()
        held_symbols = {p["symbol"] for p in positions}
        wl           = watchlist.get_all()
        log(f"Positions: {list(held_symbols) or 'none'} | Watchlist: {list(wl.keys()) or 'none'}")

        # ── Fetch market data ───────────────────────────────────────────────────
        log("Fetching local symbols and Stocktwits trending stocks...")
        stocks = fetch_market()
        log(f"Fetched {len(stocks)} stocks")
        clear_confluence_cache()  # fresh 7-indicator confluence data each cycle

        # ── Seven-indicator scan ────────────────────────────────────────────────
        candidates = stocks
        for s in stocks:
            scenario, desc = detect_scenario(s)
            s["scenario"] = scenario
            s["scenario_desc"] = desc

        sc = Counter(s["scenario"] for s in candidates)
        log(f"Scanning {len(candidates)} symbols with seven indicators | Scenarios: {dict(sc)}")

        for s in candidates:
            sym = s["symbol"]

            if sym in held_symbols:
                continue  # already own it

            log(f"  [{s['scenario'].upper()}] {sym} | ${s['price']:.2f} | {s['change']:+.2f}% | "
                f"Gap: {s.get('gap_pct', 0):+.2f}% | Vol: {s['volume']:,} | RelVol: {s.get('rel_volume', 1):.1f}x")

            score, reasons, breakdown, catalyst_summary, hold_period, trade_type, catalyst_type, flags = score_stock(s)

            # Score breakdown log
            bd = breakdown
            log(f"    7-indicator score: {score}/100 | Bull votes: {bd['bull_votes']}/7 | Bear votes: {bd['bear_votes']}/7")
            log(f"    [{trade_type.upper()}] {catalyst_summary}")
            if flags:
                for flag in flags:
                    log(f"    {flag}")

            target_pct    = 1.12 if score >= 85 else 1.10 if score >= 80 else 1.08
            stop          = round(s["price"] * 0.97, 4)
            target        = round(s["price"] * target_pct, 4)
            trigger_price = round(s.get("high", s["price"]) * 1.005, 4)  # just above today's high

            if score >= 80:
                # High conviction — buy immediately
                _place_buy(s, score, breakdown, catalyst_summary, hold_period,
                           trade_type, catalyst_type, flags, reasons)
                held_symbols.add(sym)

            elif score >= 65 and trade_type != "avoid":
                # Good setup — add to watchlist, wait for breakout confirmation
                if sym not in wl:
                    entry = watchlist.add(
                        symbol=sym, score=score, breakdown=breakdown,
                        catalyst_summary=catalyst_summary, catalyst_type=catalyst_type,
                        trade_type=trade_type, trigger_price=trigger_price,
                        stop=stop, target=target, hold_period=hold_period,
                        flags=flags, reasons=reasons,
                    )
                    log(f"    *** WATCHLIST: {sym} | trigger ${trigger_price:.2f} | score {score} ***")
                    send_watchlist({
                        **s, "score": score, "breakdown": breakdown,
                        "catalyst_summary": catalyst_summary, "trade_type": trade_type,
                        "trigger_price": trigger_price, "stop": stop, "target": target,
                        "hold_period": hold_period, "flags": flags, "reasons": reasons,
                    })
                else:
                    log(f"    Already on watchlist, skipping")
            else:
                log(f"    No signal (score {score} below 65)")

        log(f"Cycle complete. Sleeping {SCAN_INTERVAL}s...\n")
        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    if os.path.exists(LOCK_FILE):
        print("[LOCK] Another instance is already running. Exiting.", flush=True)
        exit(0)
    try:
        open(LOCK_FILE, 'w').close()
        run()
    except KeyboardInterrupt:
        log("Shutting down...")
    finally:
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)