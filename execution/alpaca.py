import requests
from config import ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL

HEADERS = {
    "APCA-API-KEY-ID": ALPACA_API_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY
}

BASE = ALPACA_BASE_URL

def get_positions():
    """Fetch all open positions from Alpaca. Returns list of dicts with symbol, entry, qty, current price, stop, target."""
    r = requests.get(f"{BASE}/v2/positions", headers=HEADERS)
    if r.status_code != 200:
        print(f"[ALPACA] Failed to fetch positions (HTTP {r.status_code})", flush=True)
        return []
    result = []
    for p in r.json():
        try:
            entry = float(p["avg_entry_price"])
            result.append({
                "symbol": p["symbol"],
                "entry": entry,
                "qty": float(p["qty"]),
                "price": float(p["current_price"]),
                "stop": entry * 0.97,
                "target": entry * 1.08,
            })
        except:
            continue
    return result


def has_open_position(symbol):
    """Check Alpaca directly for an existing position in this symbol."""
    r = requests.get(f"{BASE}/v2/positions/{symbol}", headers=HEADERS)
    return r.status_code == 200


def buy(symbol, qty):
    if has_open_position(symbol):
        print(f"[ALPACA] Skipping buy {symbol} — position already open on Alpaca", flush=True)
        return None
    return requests.post(f"{BASE}/v2/orders", json={
        "symbol": symbol,
        "qty": qty,
        "side": "buy",
        "type": "market",
        "time_in_force": "day"
    }, headers=HEADERS).json()


def sell(symbol, qty):
    return requests.post(f"{BASE}/v2/orders", json={
        "symbol": symbol,
        "qty": qty,
        "side": "sell",
        "type": "market",
        "time_in_force": "day"
    }, headers=HEADERS).json()