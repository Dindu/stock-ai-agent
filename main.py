import os
import time
import shutil
import subprocess
import threading
import requests
from datetime import datetime

LOCK_FILE = "/tmp/stock_ai_agent.lock"

from engine.scanner import fetch_market
from engine.ai import analyze
from engine.news import get_news, get_macro, refresh_macro
from engine.strategy import score_stock, pre_score, detect_scenario
from engine.regime import adjust
from engine.exits import check_exits
from execution.alpaca import buy, get_positions
from output.discord import send
from config import SCAN_INTERVAL, OLLAMA_URL

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

def start_ollama():
    if not shutil.which("ollama"):
        log("WARNING: Ollama not found. Install it from https://ollama.com then run: ollama pull llama3.1")
        return None

    # Check if already running
    try:
        r = requests.get(OLLAMA_URL.replace("/api/generate", ""), timeout=2)
        log("Ollama is already running.")
        return None
    except Exception:
        pass

    log("Starting Ollama...")
    proc = subprocess.Popen(
        ["ollama", "serve"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )

    # Wait for it to be ready
    for _ in range(10):
        time.sleep(1)
        try:
            requests.get(OLLAMA_URL.replace("/api/generate", ""), timeout=2)
            log("Ollama started successfully.")
            return proc
        except Exception:
            pass

    log("WARNING: Ollama did not start in time. AI analysis may fail.")
    return proc

def exit_monitor():
    """Runs in background, checks open positions for stop/target every 5 min."""
    while True:
        time.sleep(300)
        positions = get_positions()
        if positions:
            log(f"[EXIT MONITOR] Checking {len(positions)} open position(s)...")
            check_exits()
        else:
            log("[EXIT MONITOR] No open positions to check.")

def run():

    ollama_proc = start_ollama()

    log("AI Trading System Running...")

    # Start background thread to monitor exits every 5 minutes
    t = threading.Thread(target=exit_monitor, daemon=True)
    t.start()
    log("Exit monitor started (checks every 5 min)")

    while True:

        now = datetime.now()
        if now.hour >= 18:
            log(f"Market closed (after 6:00 PM). Exiting.")
            break

        log("--- New Scan Cycle ---")
        positions = get_positions()
        held_symbols = {p["symbol"] for p in positions}
        log(f"Open positions: {list(held_symbols) or 'none'}")

        log("Fetching market data...")
        stocks = fetch_market()
        log(f"Fetched {len(stocks)} stocks")

        refresh_macro()
        macro = get_macro()
        if macro:
            log(f"Macro: SPY {macro.get('spy_change_pct', '?')}% | VIX {macro.get('vix', '?')} ({macro.get('fear_level', '?')} fear)")

        # Scout for opportunity across all market scenarios
        candidates = []
        for s in stocks:
            scenario, desc = detect_scenario(s)
            if scenario != "none":
                s["scenario"] = scenario
                s["scenario_desc"] = desc
                candidates.append(s)
        log(f"Pre-filtered to {len(candidates)} candidates across scenarios")

        # Tally scenario breakdown for visibility
        from collections import Counter
        sc = Counter(s["scenario"] for s in candidates)
        log(f"  Scenarios: {dict(sc)}")

        for s in candidates:

            # Rule-based pre-score — skip Groq if no chance of reaching 75
            ps = pre_score(s)
            if ps < 10:
                log(f"  [{s['scenario'].upper()}] Pre-score {ps} too low, skipping")
                continue

            log(f"Analyzing {s['symbol']} | {s['scenario'].upper()} | Price: ${s['price']:.2f} | Change: {s['change']:+.2f}% | Gap: {s.get('gap_pct', 0):+.2f}% | Vol: {s['volume']:,} | RelVol: {s.get('rel_volume', 1):.1f}x | Pre-score: {ps}")
            log(f"  Scenario: {s['scenario_desc']}")

            news = get_news(s["symbol"])
            ai = analyze(s, news, macro)
            score, reasons, breakdown, catalyst_summary, hold_period, trade_type = score_stock(s, ai)
            score = adjust(score, {})

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