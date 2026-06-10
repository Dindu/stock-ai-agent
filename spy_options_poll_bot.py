"""
SPY Options Alerts Bot — polling version.

Pulls SPY 5-minute bars from Alpaca REST every POLL_SECONDS, runs the same
VWAP / EMA20 / EMA50 / volume / PDH / PDL + VWAP-direction logic, and posts
a Discord alert with a near-the-money 1DTE+ option contract from yfinance.

No WebSocket -> no Alpaca connection-limit issues.
"""

import os
import sys
import time
import traceback
from datetime import datetime, date, timedelta, timezone

import pandas as pd
import pytz
import requests
import yfinance as yf
from dotenv import load_dotenv

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import DataFeed


# Force line-buffered stdout so logs appear in real time on Render / Docker.
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

load_dotenv()

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL") or os.getenv("DISCORD_WEBHOOK")
FEED = os.getenv("ALPACA_FEED", "iex").lower()

SYMBOL = "SPY"
BAR_MINUTES = 5
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "120"))  # 2 minutes
LOOKBACK_BARS = 120
MIN_DTE = 1
MAX_DTE = 7
VOLUME_MULTIPLIER = 1.5
REQUIRE_VWAP_DIRECTION = True

central = pytz.timezone("America/Chicago")
last_alert_contract = None
_pdh_pdl_cache = {"date": None, "pdh": None, "pdl": None}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def send_discord(message):
    if not DISCORD_WEBHOOK_URL:
        print("Missing Discord webhook.")
        return
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"content": message}, timeout=10)
    except Exception as e:
        print(f"Discord post failed: {e}")


def market_open_now():
    now = datetime.now(central)
    if now.weekday() >= 5:
        return False
    start = now.replace(hour=8, minute=30, second=0, microsecond=0)
    end = now.replace(hour=14, minute=55, second=0, microsecond=0)
    return start <= now <= end


def get_previous_day_levels(client):
    """Return previous trading day's high/low using Alpaca daily bars.

    Cached per session date so we only hit the API once per day, avoiding
    yfinance rate limits that affect cron environments.
    """
    today = datetime.now(central).date()
    if _pdh_pdl_cache["date"] == today and _pdh_pdl_cache["pdh"] is not None:
        return _pdh_pdl_cache["pdh"], _pdh_pdl_cache["pdl"]

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=14)  # buffer for weekends/holidays

    req = StockBarsRequest(
        symbol_or_symbols=SYMBOL,
        timeframe=TimeFrame(1, TimeFrameUnit.Day),
        start=start,
        end=end,
        feed=DataFeed(FEED),
    )
    daily = client.get_stock_bars(req).df
    if daily is None or daily.empty:
        raise Exception("Not enough daily data from Alpaca.")

    if isinstance(daily.index, pd.MultiIndex):
        daily = daily.xs(SYMBOL, level=0)

    daily = daily.dropna()
    if len(daily) < 2:
        raise Exception("Not enough daily data from Alpaca.")

    pdh = float(daily["high"].iloc[-2])
    pdl = float(daily["low"].iloc[-2])

    _pdh_pdl_cache["date"] = today
    _pdh_pdl_cache["pdh"] = pdh
    _pdh_pdl_cache["pdl"] = pdl
    return pdh, pdl


def calculate_indicators(df):
    df = df.copy()
    df["EMA20"] = df["close"].ewm(span=20, adjust=False).mean()
    df["EMA50"] = df["close"].ewm(span=50, adjust=False).mean()
    df["VOL_AVG"] = df["volume"].rolling(20).mean()

    typical = (df["high"] + df["low"] + df["close"]) / 3
    df["VWAP"] = (typical * df["volume"]).cumsum() / df["volume"].cumsum()
    return df


def analyze(df, client):
    if len(df) < 55:
        return "NO TRADE", None

    pdh, pdl = get_previous_day_levels(client)
    df = calculate_indicators(df)

    latest = df.iloc[-1]
    previous = df.iloc[-2]

    price = float(latest["close"])
    vwap = float(latest["VWAP"])
    ema20 = float(latest["EMA20"])
    ema50 = float(latest["EMA50"])
    volume = float(latest["volume"])
    vol_avg = float(latest["VOL_AVG"])

    if pd.isna(vol_avg):
        return "NO TRADE", None

    bullish = price > vwap and price > ema20 and ema20 > ema50
    bearish = price < vwap and price < ema20 and ema20 < ema50
    strong_volume = volume > vol_avg * VOLUME_MULTIPLIER

    vwap_distance_now = float(latest["close"] - latest["VWAP"])
    vwap_distance_prev = float(previous["close"] - previous["VWAP"])

    moving_away_bullish = vwap_distance_now > vwap_distance_prev
    moving_away_bearish = vwap_distance_now < vwap_distance_prev

    print("Bullish:", bullish, flush=True)
    print("Strong Volume:", strong_volume, flush=True)
    print("Above PDH:", price > pdh, flush=True)
    print("VWAP Direction:", moving_away_bullish, flush=True)

    call_signal = bullish and strong_volume and price > pdh
    put_signal = bearish and strong_volume and price < pdl

    if REQUIRE_VWAP_DIRECTION:
        call_signal = call_signal and moving_away_bullish
        put_signal = put_signal and moving_away_bearish

    data = {
        "price": price,
        "vwap": vwap,
        "ema20": ema20,
        "ema50": ema50,
        "volume": volume,
        "vol_avg": vol_avg,
        "pdh": pdh,
        "pdl": pdl,
        "vwap_distance_now": vwap_distance_now,
        "vwap_distance_prev": vwap_distance_prev,
    }

    if call_signal:
        return "CALL", data
    if put_signal:
        return "PUT", data
    return "NO TRADE", data


def get_valid_expiry(ticker):
    today = date.today()
    for expiry in ticker.options:
        exp_date = datetime.strptime(expiry, "%Y-%m-%d").date()
        dte = (exp_date - today).days
        if MIN_DTE <= dte <= MAX_DTE:
            return expiry, dte
    return None, None


def get_option_contract(signal, spy_price):
    try:
        ticker = yf.Ticker(SYMBOL)
        expiry, dte = get_valid_expiry(ticker)
        if not expiry:
            return None
        chain = ticker.option_chain(expiry)
    except Exception as e:
        print(f"Option chain fetch failed: {e}")
        return None
    if signal == "CALL":
        options = chain.calls.copy()
        options = options[options["strike"] >= spy_price]
    elif signal == "PUT":
        options = chain.puts.copy()
        options = options[options["strike"] <= spy_price]
    else:
        return None

    if options.empty:
        return None

    options["distance"] = abs(options["strike"] - spy_price)
    option = options.sort_values("distance").iloc[0]

    return {
        "contract": option["contractSymbol"],
        "expiry": expiry,
        "dte": dte,
        "strike": float(option["strike"]),
        "bid": float(option["bid"]) if not pd.isna(option["bid"]) else 0,
        "ask": float(option["ask"]) if not pd.isna(option["ask"]) else 0,
        "last": float(option["lastPrice"]) if not pd.isna(option["lastPrice"]) else 0,
        "volume": int(option["volume"]) if not pd.isna(option["volume"]) else 0,
        "open_interest": int(option["openInterest"]) if not pd.isna(option["openInterest"]) else 0,
    }


# ---------------------------------------------------------------------------
# Alpaca REST
# ---------------------------------------------------------------------------
def fetch_bars(client):
    """Pull the most recent ~LOOKBACK_BARS 5-minute SPY bars from Alpaca."""
    end = datetime.now(timezone.utc)
    # 2 days back is plenty of buffer for ~120 5-min bars + overnight gap.
    start = end - timedelta(days=2)

    req = StockBarsRequest(
        symbol_or_symbols=SYMBOL,
        timeframe=TimeFrame(BAR_MINUTES, TimeFrameUnit.Minute),
        start=start,
        end=end,
        feed=DataFeed(FEED),
    )

    bars = client.get_stock_bars(req).df
    if bars is None or bars.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    # When a single symbol is requested the result has a MultiIndex (symbol, ts).
    if isinstance(bars.index, pd.MultiIndex):
        bars = bars.xs(SYMBOL, level=0)

    bars = bars[["open", "high", "low", "close", "volume"]].tail(LOOKBACK_BARS)
    return bars


def log(msg):
    print(f"[{datetime.now(central):%Y-%m-%d %H:%M:%S} CT] {msg}", flush=True)


# ---------------------------------------------------------------------------
# main loop
# ---------------------------------------------------------------------------
def run_cycle(client):
    global last_alert_contract

    if not market_open_now():
        log("Market closed — skipping.")
        return

    bars = fetch_bars(client)
    log(f"Fetched {len(bars)} bars.")
    if len(bars) < 55:
        log(f"Bars: {len(bars)}/55 — warming up.")
        return

    signal, data = analyze(bars, client)
    if data:
        log(
            f"SPY {data['price']:.2f} | Signal {signal} | "
            f"VWAP {data['vwap']:.2f} | EMA20 {data['ema20']:.2f} | "
            f"EMA50 {data['ema50']:.2f}"
        )

    if signal == "NO TRADE":
        return

    option = get_option_contract(signal, data["price"])
    if not option:
        send_discord(f"⚠️ {signal} setup detected, but no valid 1DTE+ option found.")
        return

    if option["contract"] == last_alert_contract:
        return

    emoji = "🟢" if signal == "CALL" else "🔴"
    message = f"""
{emoji} **SPY {signal} SETUP**

**Suggested Option**
Contract: `{option['contract']}`
Expiry: `{option['expiry']}`
DTE: `{option['dte']}`
Strike: `{option['strike']}`
Bid: `{option['bid']}`
Ask: `{option['ask']}`
Last: `{option['last']}`
Volume: `{option['volume']}`
Open Interest: `{option['open_interest']}`

**SPY Levels**
Price: `{data['price']:.2f}`
VWAP: `{data['vwap']:.2f}`
EMA20: `{data['ema20']:.2f}`
EMA50: `{data['ema50']:.2f}`
PDH: `{data['pdh']:.2f}`
PDL: `{data['pdl']:.2f}`
Current Volume: `{int(data['volume'])}`
Average Volume: `{int(data['vol_avg'])}`

**VWAP Direction**
Now: `{data['vwap_distance_now']:.2f}`
Previous: `{data['vwap_distance_prev']:.2f}`

**Rule**
Minimum 1DTE.
Near-the-money only.
VWAP direction filter enabled.
Alert only — verify chart before taking play.
"""
    send_discord(message)
    last_alert_contract = option["contract"]


def main():
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        raise Exception("Missing Alpaca API keys in .env")

    client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)

    send_discord(
        f"✅ SPY Options Alert Bot (polling every {POLL_SECONDS}s) started. "
        "Minimum 1DTE. Alerts only."
    )
    log(f"Polling every {POLL_SECONDS}s. Feed={FEED}.")

    while True:
        try:
            run_cycle(client)
        except Exception:
            log("Cycle error:")
            traceback.print_exc()
            sys.stdout.flush()
        log(f"Sleeping {POLL_SECONDS}s...")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
