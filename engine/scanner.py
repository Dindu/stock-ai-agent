import os
import requests
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

def get_sp500_symbols():
    return LOCAL_SYMBOLS

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