import os
import requests
import time
from config import ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_DATA_URL

HEADERS = {
    "APCA-API-KEY-ID": ALPACA_API_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY
}

LOCAL_SYMBOLS = [
    symbol.strip().upper()
    for symbol in os.getenv(
        "SYMBOLS",
        "SPY,QQQ,IWM,AAPL,NVDA,MSFT,AMZN,TSLA,AMD,PLTR,GOOGL,AVGO,ADBE,HOOD,ORCL",
    ).split(",")
    if symbol.strip()
]
ENABLE_STOCKTWITS_TRENDING = os.getenv("ENABLE_STOCKTWITS_TRENDING", "1") == "1"
STOCKTWITS_TRENDING_URL = os.getenv(
    "STOCKTWITS_TRENDING_URL",
    "https://api.stocktwits.com/api/2/trending/symbols.json",
)
STOCKTWITS_TIMEOUT_SECONDS = int(os.getenv("STOCKTWITS_TIMEOUT_SECONDS", "6"))
TRENDING_STOCK_COUNT = int(os.getenv("TRENDING_STOCK_COUNT", "10"))
TRENDING_REFRESH_SECONDS = int(os.getenv("TRENDING_REFRESH_SECONDS", "1800"))
TRENDING_EXCLUDE_SYMBOLS = {
    symbol.strip().upper()
    for symbol in os.getenv("TRENDING_EXCLUDE_SYMBOLS", "BITO").split(",")
    if symbol.strip()
}
_trending_cache = {"updated_at": 0.0, "symbols": []}

def get_sp500_symbols():
    symbols = list(LOCAL_SYMBOLS)
    if not ENABLE_STOCKTWITS_TRENDING or TRENDING_STOCK_COUNT <= 0:
        return symbols

    now = time.monotonic()
    if now - _trending_cache["updated_at"] >= max(30, TRENDING_REFRESH_SECONDS):
        try:
            response = requests.get(
                STOCKTWITS_TRENDING_URL,
                timeout=max(2, STOCKTWITS_TIMEOUT_SECONDS),
                headers={"Accept": "application/json", "User-Agent": "stock-ai-agent/1.0"},
            )
            if response.status_code != 200:
                print(f"[TRENDING] Stocktwits fetch failed: HTTP {response.status_code}", flush=True)
                trending = []
            else:
                payload = response.json() or {}
                trending = []
                for item in payload.get("symbols", []) if isinstance(payload, dict) else []:
                    symbol = str(item.get("symbol") if isinstance(item, dict) else item).upper().strip()
                    if symbol and symbol not in TRENDING_EXCLUDE_SYMBOLS and symbol.isalpha():
                        trending.append(symbol)
                trending = list(dict.fromkeys(trending))[:TRENDING_STOCK_COUNT]
                print(
                    f"[TRENDING] Stocktwits returned {len(trending)} symbol(s): {', '.join(trending)}",
                    flush=True,
                )
        except Exception as exc:
            print(f"[TRENDING] Stocktwits fetch exception: {type(exc).__name__}: {exc}", flush=True)
            trending = []
        _trending_cache.update({"updated_at": now, "symbols": trending})

    for symbol in _trending_cache["symbols"]:
        if symbol not in symbols:
            symbols.append(symbol)
    return symbols

def fetch_market():
    symbols = get_sp500_symbols()
    results = []
    batch_size = 100

    for i in range(0, len(symbols), batch_size):
        batch = symbols[i:i + batch_size]
        url = f"{ALPACA_DATA_URL}/v2/stocks/snapshots?symbols={','.join(batch)}"
        r = requests.get(url, headers=HEADERS)

        if r.status_code != 200:
            print(f"[SCANNER] Batch {i // batch_size + 1} failed (HTTP {r.status_code})", flush=True)
            continue

        for symbol, d in r.json().items():
            try:
                bar      = d["dailyBar"]
                prev_bar = d.get("prevDailyBar", {})
                trade    = d["latestTrade"]

                open_price  = bar["o"]
                close_price = bar["c"]
                high_price  = bar["h"]
                low_price   = bar["l"]
                volume      = bar["v"]
                prev_close  = prev_bar.get("c", open_price)
                prev_volume = prev_bar.get("v", volume) or volume

                # Intraday change (open → current)
                change = ((trade["p"] - open_price) / open_price) * 100

                # Overnight gap (prev close → today open)
                gap_pct = ((open_price - prev_close) / prev_close) * 100

                # Relative volume vs yesterday
                rel_volume = volume / prev_volume

                # Intraday range as % of price (volatility proxy)
                range_pct = ((high_price - low_price) / low_price) * 100

                results.append({
                    "symbol":     symbol,
                    "price":      trade["p"],
                    "open":       open_price,
                    "high":       high_price,
                    "low":        low_price,
                    "prev_close": prev_close,
                    "change":     change,
                    "gap_pct":    gap_pct,
                    "volume":     volume,
                    "rel_volume": rel_volume,
                    "range_pct":  range_pct,
                })
            except:
                continue

    return results