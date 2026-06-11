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
        print(f"[ALPACA] Failed to fetch positions (HTTP {r.status_code}): {r.text[:200]}", flush=True)
        return []
    raw = r.json()
    print(f"[ALPACA] {len(raw)} open position(s) on Alpaca: {[p.get('symbol') for p in raw]}", flush=True)
    result = []
    for p in raw:
        try:
            symbol = p["symbol"]
            entry = float(p["avg_entry_price"])
            # current_price can be None when market is closed — fall back to last day price
            price = p.get("current_price") or p.get("lastday_price")
            if price is None:
                print(f"[ALPACA] No current price for {symbol}, skipping", flush=True)
                continue
            result.append({
                "symbol": symbol,
                "entry": entry,
                "qty": float(p["qty"]),
                "price": float(price),
                "stop": entry * 0.97,
                "target": entry * 1.08,
            })
        except Exception as e:
            print(f"[ALPACA] Error parsing position {p.get('symbol', '?')}: {e}", flush=True)
    return result


def has_open_position(symbol):
    """Check Alpaca directly for an existing position in this symbol."""
    r = requests.get(f"{BASE}/v2/positions/{symbol}", headers=HEADERS)
    return r.status_code == 200


def buy(symbol, qty):
    if has_open_position(symbol):
        print(f"[ALPACA] Skipping buy {symbol} — position already open on Alpaca", flush=True)
        return None
    r = requests.post(f"{BASE}/v2/orders", json={
        "symbol": symbol,
        "qty": qty,
        "side": "buy",
        "type": "market",
        "time_in_force": "day"
    }, headers=HEADERS)
    data = r.json()
    if "id" in data:
        print(f"[ALPACA] Order placed for {symbol}: id={data['id']} status={data.get('status')}", flush=True)
    else:
        print(f"[ALPACA] Order FAILED for {symbol}: {data}", flush=True)
    return data


def sell(symbol, qty):
    if not has_open_position(symbol):
        print(f"[ALPACA] Skipping sell {symbol} — no open position on Alpaca", flush=True)
        return None
    r = requests.post(f"{BASE}/v2/orders", json={
        "symbol": symbol,
        "qty": qty,
        "side": "sell",
        "type": "market",
        "time_in_force": "day"
    }, headers=HEADERS)
    data = r.json()
    if "id" in data:
        print(f"[ALPACA] Sell order placed for {symbol}: id={data['id']} status={data.get('status')}", flush=True)
    else:
        print(f"[ALPACA] Sell FAILED for {symbol}: {data}", flush=True)
    return data