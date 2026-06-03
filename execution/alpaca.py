import requests
from config import ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL

HEADERS = {
    "APCA-API-KEY-ID": ALPACA_API_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY
}

BASE = ALPACA_BASE_URL

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