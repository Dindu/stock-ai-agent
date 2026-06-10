import os
import requests
import pandas as pd
import yfinance as yf
from datetime import datetime, date
import pytz
from dotenv import load_dotenv
from alpaca.data.live import StockDataStream
from alpaca.data.enums import DataFeed

load_dotenv()

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
# Accept either DISCORD_WEBHOOK_URL or the existing DISCORD_WEBHOOK from .env
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL") or os.getenv("DISCORD_WEBHOOK")
FEED = os.getenv("ALPACA_FEED", "iex")

SYMBOL = "SPY"
BAR_MINUTES = 5
MIN_DTE = 1
MAX_DTE = 7
VOLUME_MULTIPLIER = 1.5
REQUIRE_VWAP_DIRECTION = True

central = pytz.timezone("America/Chicago")

bars = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
current_bucket = None
current_bar = None
last_alert_contract = None


def send_discord(message):
    if not DISCORD_WEBHOOK_URL:
        print("Missing Discord webhook.")
        return
    requests.post(DISCORD_WEBHOOK_URL, json={"content": message}, timeout=10)


def market_open_now():
    now = datetime.now(central)
    if now.weekday() >= 5:
        return False

    start = now.replace(hour=8, minute=30, second=0, microsecond=0)
    end = now.replace(hour=14, minute=55, second=0, microsecond=0)
    return start <= now <= end


def get_previous_day_levels():
    daily = yf.download(
        SYMBOL,
        period="10d",
        interval="1d",
        progress=False,
        auto_adjust=True
    ).dropna()

    if len(daily) < 2:
        raise Exception("Not enough daily data.")

    pdh = float(daily["High"].iloc[-2])
    pdl = float(daily["Low"].iloc[-2])
    return pdh, pdl


def calculate_indicators(df):
    df = df.copy()

    df["EMA20"] = df["close"].ewm(span=20, adjust=False).mean()
    df["EMA50"] = df["close"].ewm(span=50, adjust=False).mean()
    df["VOL_AVG"] = df["volume"].rolling(20).mean()

    typical = (df["high"] + df["low"] + df["close"]) / 3
    df["VWAP"] = (typical * df["volume"]).cumsum() / df["volume"].cumsum()

    return df


def analyze(df):
    if len(df) < 55:
        return "NO TRADE", None

    pdh, pdl = get_previous_day_levels()
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
    ticker = yf.Ticker(SYMBOL)

    expiry, dte = get_valid_expiry(ticker)
    if not expiry:
        return None

    chain = ticker.option_chain(expiry)

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


def close_completed_bar():
    global bars, current_bar

    if current_bar is None:
        return False

    new_row = pd.DataFrame(
        [current_bar],
        index=[current_bar["bucket"]]
    ).drop(columns=["bucket"])

    bars = pd.concat([bars, new_row])
    bars = bars.tail(120)

    current_bar = None
    return True


def update_bar(price, size, timestamp):
    global current_bucket, current_bar

    ts = pd.Timestamp(timestamp)

    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")

    ts = ts.tz_convert("America/Chicago")
    bucket = ts.floor(f"{BAR_MINUTES}min")

    if current_bucket is None:
        current_bucket = bucket
        current_bar = {
            "bucket": bucket,
            "open": price,
            "high": price,
            "low": price,
            "close": price,
            "volume": size,
        }
        return False

    if bucket != current_bucket:
        closed = close_completed_bar()

        current_bucket = bucket
        current_bar = {
            "bucket": bucket,
            "open": price,
            "high": price,
            "low": price,
            "close": price,
            "volume": size,
        }

        return closed

    current_bar["high"] = max(current_bar["high"], price)
    current_bar["low"] = min(current_bar["low"], price)
    current_bar["close"] = price
    current_bar["volume"] += size

    return False


async def on_trade(trade):
    global last_alert_contract

    if not market_open_now():
        return

    price = float(trade.price)
    size = int(trade.size)
    timestamp = trade.timestamp

    bar_closed = update_bar(price, size, timestamp)

    if not bar_closed:
        return

    if len(bars) < 55:
        print(f"Collecting bars: {len(bars)}/55")
        return

    signal, data = analyze(bars)

    if data:
        print(
            f"SPY {data['price']:.2f} | "
            f"Signal {signal} | "
            f"VWAP {data['vwap']:.2f} | "
            f"EMA20 {data['ema20']:.2f} | "
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

    send_discord("✅ SPY WebSocket Options Alert Bot started. Minimum 1DTE. Alerts only.")

    stream = StockDataStream(
        ALPACA_API_KEY,
        ALPACA_SECRET_KEY,
        feed=DataFeed(FEED.lower())
    )

    stream.subscribe_trades(on_trade, SYMBOL)
    stream.run()


if __name__ == "__main__":
    main()
