import requests
from config import ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL

HEADERS = {
    "APCA-API-KEY-ID": ALPACA_API_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY
}

BASE = ALPACA_BASE_URL

def buy(symbol, qty):
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