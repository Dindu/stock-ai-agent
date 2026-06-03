import re
import requests
from config import ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_DATA_URL

HEADERS = {
    "APCA-API-KEY-ID": ALPACA_API_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY
}

FALLBACK_WATCHLIST = ["AAPL","MSFT","NVDA","TSLA","AMZN","AMD","PLTR","SOFI","COIN","META"]

def get_sp500_symbols():
    try:
        r = requests.get(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        # Tickers appear as: href="https://www.nyse.com/quote/XNYS:MMM">MMM</a>
        symbols = re.findall(r'href="https://www\.[^"]+">([A-Z]{1,5})</a>\n</td>', r.text)
        symbols = [s.replace(".", "/") for s in symbols]  # BRK.B -> BRK/B for Alpaca
        if len(symbols) > 100:
            print(f"[SCANNER] Loaded {len(symbols)} S&P 500 symbols from Wikipedia", flush=True)
            return symbols
    except Exception as e:
        print(f"[SCANNER] Could not fetch S&P 500 list: {e}", flush=True)
    print(f"[SCANNER] Using fallback watchlist of {len(FALLBACK_WATCHLIST)} symbols", flush=True)
    return FALLBACK_WATCHLIST

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