import requests
from datetime import datetime
from engine.positions import open_positions
from execution.alpaca import sell
from output.discord import send_exit
from config import ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_DATA_URL

HEADERS = {
    "APCA-API-KEY-ID": ALPACA_API_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY
}

_fetch_failures = {}  # tracks consecutive price fetch failures per symbol

def get_price(symbol):
    url = f"{ALPACA_DATA_URL}/v2/stocks/trades/latest?symbols={symbol}"
    r = requests.get(url, headers=HEADERS)

    try:
        return r.json()["trades"][symbol]["p"]
    except:
        return None


def check_exits():

    for sym in list(open_positions.keys()):

        pos = open_positions[sym]
        price = get_price(sym)

        if not price:
            _fetch_failures[sym] = _fetch_failures.get(sym, 0) + 1
            if _fetch_failures[sym] >= 3:
                print(f"[EXITS] Removing ghost position {sym} — price unavailable after 3 attempts", flush=True)
                del open_positions[sym]
                _fetch_failures.pop(sym, None)
            else:
                print(f"[EXITS] Could not fetch price for {sym} (attempt {_fetch_failures[sym]}/3)", flush=True)
            continue

        _fetch_failures.pop(sym, None)  # reset on success

        pnl = ((price - pos["entry"]) / pos["entry"]) * 100
        print(f"[EXITS] {sym} | Current: ${price:.2f} | Entry: ${pos['entry']:.2f} | PnL: {pnl:.2f}%", flush=True)

        if price <= pos["stop"]:
            print(f"[EXITS] STOP HIT on {sym} — selling at ${price:.2f} (PnL: {pnl:.2f}%)", flush=True)
            sell(sym, pos["qty"])
            send_exit(sym, "🛑 Stop Loss Hit", pos["entry"], price, pos["qty"], pnl)
            del open_positions[sym]

        elif price >= pos["target"]:
            print(f"[EXITS] TARGET HIT on {sym} — selling at ${price:.2f} (PnL: {pnl:.2f}%)", flush=True)
            sell(sym, pos["qty"])
            send_exit(sym, "🎯 Take Profit Hit", pos["entry"], price, pos["qty"], pnl)
            del open_positions[sym]

        elif (datetime.now() - pos["time"]).seconds > 86400:
            print(f"[EXITS] TIME EXIT on {sym} — held >24h, selling at ${price:.2f} (PnL: {pnl:.2f}%)", flush=True)
            sell(sym, pos["qty"])
            send_exit(sym, "⏰ Time Exit (>24h)", pos["entry"], price, pos["qty"], pnl)
            del open_positions[sym]