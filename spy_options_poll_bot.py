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
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "30"))  # 30 seconds for SPY options
LOOKBACK_BARS = 120
RECENT_HIGH_LOOKBACK = 20  # bars used for intraday recent high/low (~100 min)
MIN_DTE = 1
MAX_DTE = 7
VOLUME_MULTIPLIER = 1.5

# Scoring thresholds (0-100)
SCORE_STRONG = int(os.getenv("SCORE_STRONG", "80"))   # STRONG CALL/PUT alert
SCORE_SIGNAL = int(os.getenv("SCORE_SIGNAL", "65"))   # CALL/PUT alert
SCORE_WATCH  = int(os.getenv("SCORE_WATCH",  "50"))   # WATCHLIST heads-up

central = pytz.timezone("America/Chicago")
# Track contracts already alerted today so we never duplicate.
# Keyed on (side, contract); reset automatically when the trading date changes.
_alerted_today = {"date": None, "keys": set()}
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

    # Intraday recent high/low (exclude the current bar so a break is meaningful)
    recent_window = df.iloc[-(RECENT_HIGH_LOOKBACK + 1):-1]
    recent_high = float(recent_window["high"].max()) if len(recent_window) else price
    recent_low = float(recent_window["low"].min()) if len(recent_window) else price

    above_pdh = price > pdh
    below_pdl = price < pdl
    above_recent_high = price > recent_high
    below_recent_low = price < recent_low

    # ---- Score (CALL side) ----
    call_score = 0
    if bullish:
        call_score += 30
    if strong_volume:
        call_score += 20
    if moving_away_bullish:
        call_score += 20
    if above_pdh:
        call_score += 15
    if above_recent_high:
        call_score += 15

    # ---- Score (PUT side) ----
    put_score = 0
    if bearish:
        put_score += 30
    if strong_volume:
        put_score += 20
    if moving_away_bearish:
        put_score += 20
    if below_pdl:
        put_score += 15
    if below_recent_low:
        put_score += 15

    print("Bullish:", bullish, flush=True)
    print("Strong Volume:", strong_volume, flush=True)
    print("Above PDH:", above_pdh, flush=True)
    print("Above Recent High:", above_recent_high, flush=True)
    print("VWAP Direction (bull):", moving_away_bullish, flush=True)
    print(f"CALL score: {call_score} | PUT score: {put_score}", flush=True)

    if call_score >= put_score and call_score >= SCORE_WATCH:
        side = "CALL"
        score = call_score
    elif put_score > call_score and put_score >= SCORE_WATCH:
        side = "PUT"
        score = put_score
    else:
        side = "NO TRADE"
        score = max(call_score, put_score)

    if side == "NO TRADE":
        tier = "NONE"
        signal = "NO TRADE"
    elif score >= SCORE_STRONG:
        tier = "STRONG"
        signal = f"STRONG {side}"
    elif score >= SCORE_SIGNAL:
        tier = "SIGNAL"
        signal = side
    else:  # >= SCORE_WATCH
        tier = "WATCH"
        signal = "WATCHLIST"

    data = {
        "price": price,
        "vwap": vwap,
        "ema20": ema20,
        "ema50": ema50,
        "volume": volume,
        "vol_avg": vol_avg,
        "pdh": pdh,
        "pdl": pdl,
        "recent_high": recent_high,
        "recent_low": recent_low,
        "vwap_distance_now": vwap_distance_now,
        "vwap_distance_prev": vwap_distance_prev,
        "call_score": call_score,
        "put_score": put_score,
        "score": score,
        "tier": tier,
        "side": side,
        "signal": signal,
        "checks": {
            "bullish": bullish,
            "bearish": bearish,
            "strong_volume": strong_volume,
            "vwap_direction": moving_away_bullish if side == "CALL" else moving_away_bearish,
            "above_pdh": above_pdh,
            "below_pdl": below_pdl,
            "above_recent_high": above_recent_high,
            "below_recent_low": below_recent_low,
        },
    }

    return side, data

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
    if not market_open_now():
        log("Market closed — skipping.")
        return

    # Reset the per-day dedupe set when the date rolls over.
    today = datetime.now(central).date()
    if _alerted_today["date"] != today:
        _alerted_today["date"] = today
        _alerted_today["keys"] = set()

    bars = fetch_bars(client)
    log(f"Fetched {len(bars)} bars.")
    if len(bars) < 55:
        log(f"Bars: {len(bars)}/55 — warming up.")
        return

    side, data = analyze(bars, client)
    if data:
        log(
            f"SPY {data['price']:.2f} | {data['signal']} ({data['score']}) | "
            f"VWAP {data['vwap']:.2f} | EMA20 {data['ema20']:.2f} | "
            f"EMA50 {data['ema50']:.2f} | RecentHigh {data['recent_high']:.2f}"
        )

    if side == "NO TRADE":
        return

    option = get_option_contract(side, data["price"])
    if not option:
        send_discord(f"⚠️ {data['signal']} setup detected, but no valid 1DTE+ option found.")
        return

    alert_key = (side, option["contract"])
    if alert_key in _alerted_today["keys"]:
        log(f"Duplicate {side} alert for {option['contract']} — suppressed.")
        return

    tier = data["tier"]
    if tier == "STRONG":
        emoji = "🟢" if side == "CALL" else "🔴"
        header = f"{emoji} **SPY {data['signal']} SETUP** (score {data['score']}/100)"
        footer = "Strong setup — all major confirmations aligned."
    elif tier == "SIGNAL":
        emoji = "🟢" if side == "CALL" else "🔴"
        header = f"{emoji} **SPY {data['signal']} SETUP** (score {data['score']}/100)"
        footer = "Confirmed setup — trend + one breakout confirmation."
    else:  # WATCH
        emoji = "🟡"
        header = f"{emoji} **SPY {side} WATCHLIST** (score {data['score']}/100)"
        footer = "Watchlist — wait for volume / breakout confirmation before entering."

    checks = data["checks"]
    if side == "CALL":
        checklist = (
            f"Bullish trend: `{checks['bullish']}`\n"
            f"Strong volume: `{checks['strong_volume']}`\n"
            f"VWAP direction: `{checks['vwap_direction']}`\n"
            f"Above PDH: `{checks['above_pdh']}`\n"
            f"Above recent high: `{checks['above_recent_high']}`"
        )
    else:
        checklist = (
            f"Bearish trend: `{checks['bearish']}`\n"
            f"Strong volume: `{checks['strong_volume']}`\n"
            f"VWAP direction: `{checks['vwap_direction']}`\n"
            f"Below PDL: `{checks['below_pdl']}`\n"
            f"Below recent low: `{checks['below_recent_low']}`"
        )

    message = f"""
{header}

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

**Score Breakdown**
{checklist}

**SPY Levels**
Price: `{data['price']:.2f}`
VWAP: `{data['vwap']:.2f}`
EMA20: `{data['ema20']:.2f}`
EMA50: `{data['ema50']:.2f}`
PDH: `{data['pdh']:.2f}` | PDL: `{data['pdl']:.2f}`
Recent High: `{data['recent_high']:.2f}` | Recent Low: `{data['recent_low']:.2f}`
Current Volume: `{int(data['volume'])}` | Avg Volume: `{int(data['vol_avg'])}`

**VWAP Direction**
Now: `{data['vwap_distance_now']:.2f}` | Previous: `{data['vwap_distance_prev']:.2f}`

**Rule**
Minimum 1DTE. Near-the-money only.
{footer}
Alert only — verify chart before taking play.
"""
    send_discord(message)
    _alerted_today["keys"].add(alert_key)
    log(f"Alert sent: {data['signal']} {option['contract']} (score {data['score']})")


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
