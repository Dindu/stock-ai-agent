import os
import time
import shutil
import subprocess
import threading
import requests
from collections import Counter
from datetime import datetime, timezone, timedelta

LOCK_FILE = "/tmp/stock_ai_agent.lock"

from engine.scanner import fetch_market
from engine.ai import analyze
from engine.news import get_news, get_macro, refresh_macro
from engine.strategy import score_stock, pre_score, detect_scenario
from engine.accumulation import clear_cache as clear_acc_cache
from engine.regime import adjust
from engine.exits import check_exits
from engine import watchlist, learner
from execution.alpaca import buy, get_positions
from output.discord import send, send_watchlist
from config import SCAN_INTERVAL, OLLAMA_URL

EST = timezone(timedelta(hours=-5))

def log(msg):
    print(f"[{datetime.now(EST).strftime('%H:%M:%S')}] {msg}", flush=True)

def start_ollama():
    if not shutil.which("ollama"):
        log("WARNING: Ollama not found.")
        return None
    try:
        requests.get(OLLAMA_URL.replace("/api/generate", ""), timeout=2)
        log("Ollama already running.")
        return None
    except Exception:
        pass
    log("Starting Ollama...")
    proc = subprocess.Popen(["ollama", "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(10):
        time.sleep(1)
        try:
            requests.get(OLLAMA_URL.replace("/api/generate", ""), timeout=2)
            log("Ollama started.")
            return proc
        except Exception:
            pass
    log("WARNING: Ollama did not start in time.")
    return proc

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
    start_ollama()
    log("=== AI Trading System Starting ===")
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
        log("Fetching 500 stocks...")
        stocks = fetch_market()
        log(f"Fetched {len(stocks)} stocks")
        clear_acc_cache()  # fresh accumulation data each cycle

        refresh_macro()
        macro = get_macro()
        if macro:
            log(f"Macro: SPY {macro.get('spy_change_pct', '?')}% | VIX {macro.get('vix', '?')} ({macro.get('fear_level', '?')} fear)")

        # ── Check watchlist breakouts first ─────────────────────────────────────
        triggered = watchlist.check_triggers(stocks, held_symbols)
        for entry in triggered:
            sym = entry["symbol"]
            if sym in held_symbols:
                continue
            log(f"[WATCHLIST TRIGGER] {sym} broke ${entry['trigger_price']:.2f} — buying now")
            _place_buy(
                entry, entry["score"], entry["breakdown"],
                entry["catalyst_summary"], entry["hold_period"],
                entry["trade_type"], entry["catalyst_type"],
                entry["flags"], entry["reasons"], label="WATCHLIST TRIGGER",
            )
            held_symbols.add(sym)

        # ── Scenario scan ───────────────────────────────────────────────────────
        candidates = []
        for s in stocks:
            scenario, desc = detect_scenario(s)
            if scenario != "none":
                s["scenario"]      = scenario
                s["scenario_desc"] = desc
                candidates.append(s)

        sc = Counter(s["scenario"] for s in candidates)
        log(f"Scenarios found: {dict(sc)} ({len(candidates)} total)")

        for s in candidates:
            sym = s["symbol"]

            if sym in held_symbols:
                continue  # already own it

            ps = pre_score(s)
            if ps < 10:
                continue  # not interesting enough for AI

            log(f"  [{s['scenario'].upper()}] {sym} | ${s['price']:.2f} | {s['change']:+.2f}% | "
                f"Gap: {s.get('gap_pct', 0):+.2f}% | Vol: {s['volume']:,} | RelVol: {s.get('rel_volume', 1):.1f}x | Pre: {ps}")

            news = get_news(sym)
            ai   = analyze(s, news, macro)
            score, reasons, breakdown, catalyst_summary, hold_period, trade_type, catalyst_type, flags = score_stock(s, ai)

            # Apply learning multiplier (adjusts score based on historical win rate)
            multiplier = learner.get_catalyst_multiplier(catalyst_type)
            if multiplier != 1.0:
                log(f"    Learning adjustment: {catalyst_type} multiplier {multiplier:.2f}x")
                score = min(int(score * multiplier), 100)

            score = adjust(score, {})

            # Score breakdown log
            bd = breakdown
            log(f"    Score: {score}/100 | "
                f"Cat:{bd['catalyst']}/30 Fund:{bd['fundamentals']}/15 Mkt:{bd['market']}/10 "
                f"Ins:{bd['insider']}/20 Acc:{bd['accumulation']}/15 Tech:{bd['technicals']}/10")
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
        print("[LOCK] Another instance running. Exiting.", flush=True)
        exit(0)
    try:
        open(LOCK_FILE, 'w').close()
        run()
    except KeyboardInterrupt:
        log("Shutting down...")
    finally:
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)


            # Dynamic target based on conviction score
            target_pct = 1.12 if score >= 85 else 1.10 if score >= 80 else 1.08
            stop   = s["price"] * 0.97
            target = s["price"] * target_pct
            risk   = s["price"] - stop
            reward = target - s["price"]
            rr     = reward / risk if risk > 0 else 0

            log(f"  Score: {score:.0f}/100 | Catalyst:{breakdown['catalyst']}/30 Market:{breakdown['market']}/20 Fundamentals:{breakdown['fundamentals']}/20 Technicals:{breakdown['technicals']}/20 Sentiment:{breakdown['sentiment']}/10")
            log(f"  [{trade_type.upper()}] {catalyst_summary}")
            log(f"  R:R {rr:.1f}:1 | Hold: {hold_period} | Entry: ${s['price']:.2f} | Stop: ${stop:.2f} | Target: ${target:.2f}")

            if score >= 80 and s["symbol"] not in held_symbols:

                log(f"  *** BUY SIGNAL: {s['symbol']} at ${s['price']:.2f} (score={score:.0f}) ***")
                buy(s["symbol"], 10)

                log(f"  Stop: ${stop:.2f} | Target: ${target:.2f}")

                send({
                    **s,
                    "score": score,
                    "reasons": reasons,
                    "breakdown": breakdown,
                    "catalyst_summary": catalyst_summary,
                    "hold_period": hold_period,
                    "trade_type": trade_type,
                    "qty": 10,
                    "stop": stop,
                    "target": target,
                    "rr": rr,
                })
                log(f"  Discord alert sent for {s['symbol']}")

            elif s["symbol"] in held_symbols:
                log(f"  Skipping {s['symbol']} — already in position")
            else:
                log(f"  No signal (score below 75)")

        log(f"Scan complete. Sleeping {SCAN_INTERVAL}s...\n")
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