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
from collections import deque
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
SCORE_DOMINANCE = int(os.getenv("SCORE_DOMINANCE", "20"))  # bull must lead bear by this much (and vice versa)

# Trend-ignition filter: only fire when the score is *starting* to rise into the threshold.
# CALL example: 5 minutes ago BULL was below IGNITION_PRIOR_MAX, now it has gained at least IGNITION_MIN_DELTA.
# Set IGNITION_REQUIRED=0 in env to disable and revert to absolute-score-only firing.
IGNITION_REQUIRED   = os.getenv("IGNITION_REQUIRED", "1") == "1"
IGNITION_MIN_DELTA  = int(os.getenv("IGNITION_MIN_DELTA",  "20"))  # BULL/BEAR must have risen at least this much in 5 min
IGNITION_PRIOR_MAX  = int(os.getenv("IGNITION_PRIOR_MAX",  "65"))  # 5 min ago BULL/BEAR must have been below this
IGNITION_LOOKBACK_S = int(os.getenv("IGNITION_LOOKBACK_S", "300"))  # how far back to compare (default 5 min)

# Score-trend history (one reading per cycle).
# At POLL_SECONDS=30s, capacity 24 = 12 minutes of history.
_SCORE_HISTORY_CAP = 24
score_history: "deque[tuple[datetime, int, int]]" = deque(maxlen=_SCORE_HISTORY_CAP)

central = pytz.timezone("America/Chicago")
# Track which side already alerted today so we never duplicate.
# Reset automatically when the trading date changes.
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
    """Compute independent Bull and Bear scores (0-100) from the latest bars.

    Components (each side, max 100):
      Price vs VWAP   : 20
      Price vs EMA20  : 15
      Price vs EMA50  : 15
      EMA20 slope     : 10
      Volume + candle : 15  (only if volume > 1.5x avg AND candle agrees)
      Higher high / lower low : 15
      VWAP direction  : 10
    """
    if len(df) < 55:
        return "NO TRADE", None

    pdh, pdl = get_previous_day_levels(client)
    df = calculate_indicators(df)

    latest = df.iloc[-1]
    previous = df.iloc[-2]

    price = float(latest["close"])
    open_ = float(latest["open"])
    vwap = float(latest["VWAP"])
    ema20 = float(latest["EMA20"])
    ema50 = float(latest["EMA50"])
    volume = float(latest["volume"])
    vol_avg = float(latest["VOL_AVG"])

    if pd.isna(vol_avg):
        return "NO TRADE", None

    # EMA20 slope: compare current EMA20 to EMA20 a few bars back.
    ema20_back = float(df["EMA20"].iloc[-5]) if len(df) >= 5 else float(df["EMA20"].iloc[0])
    ema20_rising = ema20 > ema20_back
    ema20_falling = ema20 < ema20_back

    strong_volume = volume > vol_avg * VOLUME_MULTIPLIER
    bullish_candle = price > open_
    bearish_candle = price < open_

    vwap_distance_now = price - vwap
    vwap_distance_prev = float(previous["close"]) - float(previous["VWAP"])
    moving_away_bullish = vwap_distance_now > vwap_distance_prev
    moving_away_bearish = vwap_distance_now < vwap_distance_prev

    # Intraday recent high/low (exclude current bar so a break is meaningful).
    recent_window = df.iloc[-(RECENT_HIGH_LOOKBACK + 1):-1]
    recent_high = float(recent_window["high"].max()) if len(recent_window) else price
    recent_low = float(recent_window["low"].min()) if len(recent_window) else price

    # ---------------- Bull score ----------------
    bull_breakdown = {}
    bull_score = 0
    if price > vwap:
        bull_score += 20; bull_breakdown["Price > VWAP"] = 20
    if price > ema20:
        bull_score += 15; bull_breakdown["Price > EMA20"] = 15
    if price > ema50:
        bull_score += 15; bull_breakdown["Price > EMA50"] = 15
    if ema20_rising:
        bull_score += 10; bull_breakdown["EMA20 rising"] = 10
    if strong_volume and bullish_candle:
        bull_score += 15; bull_breakdown["Strong volume + bull candle"] = 15
    if price > recent_high:
        bull_score += 15; bull_breakdown["Higher high"] = 15
    if moving_away_bullish:
        bull_score += 10; bull_breakdown["VWAP direction bullish"] = 10

    # ---------------- Bear score ----------------
    bear_breakdown = {}
    bear_score = 0
    if price < vwap:
        bear_score += 20; bear_breakdown["Price < VWAP"] = 20
    if price < ema20:
        bear_score += 15; bear_breakdown["Price < EMA20"] = 15
    if price < ema50:
        bear_score += 15; bear_breakdown["Price < EMA50"] = 15
    if ema20_falling:
        bear_score += 10; bear_breakdown["EMA20 falling"] = 10
    if strong_volume and bearish_candle:
        bear_score += 15; bear_breakdown["Strong volume + bear candle"] = 15
    if price < recent_low:
        bear_score += 15; bear_breakdown["Lower low"] = 15
    if moving_away_bearish:
        bear_score += 10; bear_breakdown["VWAP direction bearish"] = 10

    # ---------------- Decision ----------------
    # The dominant side must lead by SCORE_DOMINANCE points; otherwise NO TRADE.
    diff = bull_score - bear_score

    if bull_score >= SCORE_STRONG and diff >= SCORE_DOMINANCE:
        side, score, tier, signal = "CALL", bull_score, "STRONG", "STRONG CALL"
    elif bear_score >= SCORE_STRONG and -diff >= SCORE_DOMINANCE:
        side, score, tier, signal = "PUT", bear_score, "STRONG", "STRONG PUT"
    elif bull_score >= SCORE_SIGNAL and diff >= SCORE_DOMINANCE:
        side, score, tier, signal = "CALL", bull_score, "SIGNAL", "CALL"
    elif bear_score >= SCORE_SIGNAL and -diff >= SCORE_DOMINANCE:
        side, score, tier, signal = "PUT", bear_score, "SIGNAL", "PUT"
    elif bull_score >= SCORE_WATCH and bull_score > bear_score:
        side, score, tier, signal = "CALL", bull_score, "WATCH", "WATCHLIST"
    elif bear_score >= SCORE_WATCH and bear_score > bull_score:
        side, score, tier, signal = "PUT", bear_score, "WATCH", "WATCHLIST"
    else:
        side, score, tier, signal = "NO TRADE", max(bull_score, bear_score), "NONE", "NO TRADE"

    # ---------------- Trend ----------------
    score_history.append((datetime.now(central), bull_score, bear_score))

    def history_at(seconds_ago):
        """Return (bull, bear) closest to N seconds ago, or (None, None)."""
        target = datetime.now(central) - timedelta(seconds=seconds_ago)
        for ts, b, s in reversed(score_history):
            if ts <= target:
                return b, s
        return (None, None)

    bull_5m, bear_5m = history_at(IGNITION_LOOKBACK_S)
    bull_10m, bear_10m = history_at(IGNITION_LOOKBACK_S * 2)

    print(f"BULL score: {bull_score:3d} | BEAR score: {bear_score:3d}", flush=True)
    if bull_5m is not None:
        print(f"  5m ago : BULL {bull_5m:3d} | BEAR {bear_5m:3d}  (Δ BULL {bull_score - bull_5m:+d})", flush=True)
    if bull_10m is not None:
        print(f" 10m ago : BULL {bull_10m:3d} | BEAR {bear_10m:3d}  (Δ BULL {bull_score - bull_10m:+d})", flush=True)
    print(f"  Bull components: {bull_breakdown}", flush=True)
    print(f"  Bear components: {bear_breakdown}", flush=True)

    # Sentiment summary line for the human glance.
    if diff >= 30:
        sentiment = "BULL DOMINANT"
    elif diff <= -30:
        sentiment = "BEAR DOMINANT"
    elif abs(diff) <= 10:
        sentiment = "BALANCED"
    elif diff > 0:
        sentiment = "Bull lean"
    else:
        sentiment = "Bear lean"

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
        "bull_score": bull_score,
        "bear_score": bear_score,
        "bull_breakdown": bull_breakdown,
        "bear_breakdown": bear_breakdown,
        "bull_5m": bull_5m, "bear_5m": bear_5m,
        "bull_10m": bull_10m, "bear_10m": bear_10m,
        "score": score,
        "tier": tier,
        "side": side,
        "signal": signal,
        "sentiment": sentiment,
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
        trend_5m = f" | 5m\u0394 BULL {data['bull_score'] - data['bull_5m']:+d}" if data['bull_5m'] is not None else ""
        log(
            f"SPY {data['price']:.2f} | {data['signal']} | "
            f"BULL {data['bull_score']} BEAR {data['bear_score']} ({data['sentiment']}){trend_5m}"
        )

    if side == "NO TRADE":
        return

    # Only send Discord alerts for the perfect setup (STRONG tier).
    # SIGNAL/WATCHLIST tiers are logged but not posted.
    if data["tier"] != "STRONG":
        log(f"{data['signal']} (BULL {data['bull_score']} / BEAR {data['bear_score']}) "
            f"\u2014 below STRONG threshold, no Discord alert.")
        return

    # One STRONG CALL alert and one STRONG PUT alert max per trading day.
    if side in _alerted_today["keys"]:
        log(f"Already alerted {side} today \u2014 suppressed.")
        return

    # Trend-ignition filter: only fire when the move is *just starting*, not mid- or late-trend.
    # For CALL: 5 min ago BULL was relatively low, and BULL has surged by at least IGNITION_MIN_DELTA.
    # For PUT : same logic on BEAR.
    if IGNITION_REQUIRED:
        if side == "CALL":
            now_score = data["bull_score"]
            past_score = data["bull_5m"]
        else:
            now_score = data["bear_score"]
            past_score = data["bear_5m"]

        if past_score is None:
            log(
                f"Ignition gate: insufficient history (need {IGNITION_LOOKBACK_S}s) \u2014 "
                "holding alert until trend can be measured."
            )
            return

        delta = now_score - past_score
        if past_score >= IGNITION_PRIOR_MAX:
            log(
                f"Ignition gate: {side} 5m ago was already {past_score} "
                f"(>= {IGNITION_PRIOR_MAX}) \u2014 mid/late trend, no alert."
            )
            return
        if delta < IGNITION_MIN_DELTA:
            log(
                f"Ignition gate: {side} delta only +{delta} (need +{IGNITION_MIN_DELTA}) "
                f"\u2014 trend not igniting, no alert."
            )
            return

        log(
            f"\U0001f680 Ignition confirmed: {side} score {past_score} \u2192 {now_score} "
            f"(\u0394 +{delta}) over last {IGNITION_LOOKBACK_S}s."
        )

    option = get_option_contract(side, data["price"])
    if not option:
        log(f"{data['signal']} setup detected, but no valid 1DTE+ option found.")
        return

    emoji = "\U0001f7e2" if side == "CALL" else "\U0001f534"
    header = f"\U0001f6a8 {emoji} **SPY {data['signal']}**"

    breakdown = data["bull_breakdown"] if side == "CALL" else data["bear_breakdown"]
    checklist = "\n".join(f"\u2705 {k} (+{v})" for k, v in breakdown.items()) or "(no positive components)"

    # Trend lines
    trend_lines = []
    if data["bull_5m"] is not None:
        trend_lines.append(
            f"5m ago : BULL `{data['bull_5m']}` / BEAR `{data['bear_5m']}` "
            f"(\u0394 BULL {data['bull_score'] - data['bull_5m']:+d})"
        )
    if data["bull_10m"] is not None:
        trend_lines.append(
            f"10m ago: BULL `{data['bull_10m']}` / BEAR `{data['bear_10m']}` "
            f"(\u0394 BULL {data['bull_score'] - data['bull_10m']:+d})"
        )
    trend_block = "\n".join(trend_lines) if trend_lines else "(insufficient history)"

    message = f"""
{header}

**SPY:** `{data['price']:.2f}`
**Bull Score:** `{data['bull_score']}/100`   |   **Bear Score:** `{data['bear_score']}/100`
**Sentiment:** `{data['sentiment']}`

**Suggested Option**
Contract: `{option['contract']}`
Expiry: `{option['expiry']}` (DTE {option['dte']})
Strike: `{option['strike']}`
Bid/Ask/Last: `{option['bid']}` / `{option['ask']}` / `{option['last']}`
Volume / OI: `{option['volume']}` / `{option['open_interest']}`

**Score Components**
{checklist}

**Score Trend**
{trend_block}

**Levels**
VWAP: `{data['vwap']:.2f}` | EMA20: `{data['ema20']:.2f}` | EMA50: `{data['ema50']:.2f}`
PDH: `{data['pdh']:.2f}` | PDL: `{data['pdl']:.2f}`
Recent High: `{data['recent_high']:.2f}` | Recent Low: `{data['recent_low']:.2f}`
Volume: `{int(data['volume'])}` | Avg: `{int(data['vol_avg'])}`

Alert only \u2014 verify chart before taking play.
"""
    send_discord(message)
    _alerted_today["keys"].add(side)
    log(f"Alert sent: {data['signal']} {option['contract']} (BULL {data['bull_score']} / BEAR {data['bear_score']})")


def main():
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        raise Exception("Missing Alpaca API keys in .env")

    client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)

    log(f"SPY Options Alert Bot started. Polling every {POLL_SECONDS}s. Feed={FEED}.")

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
