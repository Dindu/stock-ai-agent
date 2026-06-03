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
                bar = d["dailyBar"]
                trade = d["latestTrade"]
                change = ((bar["c"] - bar["o"]) / bar["o"]) * 100
                results.append({
                    "symbol": symbol,
                    "price": trade["p"],
                    "change": change,
                    "volume": bar["v"]
                })
            except:
                continue

    return results