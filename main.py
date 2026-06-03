import os
import time
import shutil
import subprocess
import requests
from datetime import datetime

LOCK_FILE = "/tmp/stock_ai_agent.lock"

from engine.scanner import fetch_market
from engine.ai import analyze
from engine.news import get_news, get_macro, refresh_macro
from engine.strategy import score_stock
from engine.regime import adjust
from engine.exits import check_exits
from engine.positions import open_positions
from execution.alpaca import buy
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

def run():

    ollama_proc = start_ollama()

    log("AI Trading System Running...")

    while True:

        log("--- New Scan Cycle ---")
        log(f"Open positions: {list(open_positions.keys()) or 'none'}")

        log("Checking exits...")
        check_exits()

        log("Fetching market data...")
        stocks = fetch_market()
        log(f"Fetched {len(stocks)} stocks")

        refresh_macro()
        macro = get_macro()
        if macro:
            log(f"Macro: SPY {macro.get('spy_change_pct', '?')}% | VIX {macro.get('vix', '?')} ({macro.get('fear_level', '?')} fear)")

        # Pre-filter: only analyze stocks with meaningful movement or high volume
        candidates = [s for s in stocks if abs(s["change"]) >= 1.5 or s["volume"] >= 200000]
        log(f"Pre-filtered to {len(candidates)} active stocks (change ≥1.5% or volume ≥200k)")

        for s in candidates:

            log(f"Analyzing {s['symbol']} | Price: ${s['price']:.2f} | Change: {s['change']:.2f}% | Vol: {s['volume']:,}")

            news = get_news(s["symbol"])
            ai = analyze(s, news, macro)
            score, reasons = score_stock(s, ai)
            score = adjust(score, {})

            log(f"  Score: {score:.1f} | Reasons: {reasons}")

            if score >= 75 and s["symbol"] not in open_positions:

                log(f"  *** BUY SIGNAL: {s['symbol']} at ${s['price']:.2f} (score={score:.1f}) ***")
                buy(s["symbol"], 10)

                open_positions[s["symbol"]] = {
                    "entry": s["price"],
                    "qty": 10,
                    "time": time.time(),
                    "stop": s["price"] * 0.97,
                    "target": s["price"] * 1.08
                }

                log(f"  Stop: ${s['price'] * 0.97:.2f} | Target: ${s['price'] * 1.08:.2f}")

                send({
                    **s,
                    "score": score,
                    "reasons": reasons,
                    "qty": 10,
                    "stop": s["price"] * 0.97,
                    "target": s["price"] * 1.08
                })
                log(f"  Discord alert sent for {s['symbol']}")

            elif s["symbol"] in open_positions:
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