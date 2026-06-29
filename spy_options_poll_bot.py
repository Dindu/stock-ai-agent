"""
Index ETF Options Alerts Bot — polling version (SPY / QQQ / IWM by default).

Pulls 5-minute bars from Alpaca REST every POLL_SECONDS for each configured
symbol, runs a Bull/Bear market scorecard, applies a trend-ignition filter,
and posts a Discord alert with a near-the-money 1DTE+ option contract from
Alpaca's live options data when the score ignites into STRONG territory.

No WebSocket -> no Alpaca connection-limit issues.
"""

import os
import sys
import time
import traceback
from collections import deque
from datetime import datetime, date, timedelta, timezone

import gspread
from google.oauth2.service_account import Credentials as GCredentials

import pandas as pd
import pytz
import requests
from dotenv import load_dotenv

from alpaca.data.historical import StockHistoricalDataClient, OptionHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, OptionLatestQuoteRequest, OptionSnapshotRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import DataFeed
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, GetOptionContractsRequest
from alpaca.trading.enums import OrderSide, TimeInForce


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

# Symbols to scan, in order. Override via env: SYMBOLS="SPY,QQQ,IWM"
SYMBOLS = [s.strip().upper() for s in os.getenv("SYMBOLS", "SPY,QQQ,IWM").split(",") if s.strip()]
BAR_MINUTES = 5
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "30"))  # 30 seconds for index options
LOOKBACK_BARS = 120
RECENT_HIGH_LOOKBACK = 20  # bars used for intraday recent high/low (~100 min)
MIN_DTE = 1  # include next-day expiry; theta risk is acceptable for strong signals
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

# Paper-trading execution. When ENABLE_ALPACA_PAPER_TRADING=1 the bot will
# submit a paper-account market BUY when a STRONG signal fires, then poll the
# option price each cycle and submit a paper-account market SELL at +20% / -20%.
# Set to 0 to keep the bot in pure alert mode (no orders submitted, no tracking).
ENABLE_ALPACA_PAPER_TRADING = os.getenv("ENABLE_ALPACA_PAPER_TRADING", "1") == "1"
PROFIT_TARGET_PCT = float(os.getenv("PROFIT_TARGET_PCT", "0.20"))
STOP_LOSS_PCT     = float(os.getenv("STOP_LOSS_PCT",     "0.15"))
MAX_OPEN_TRADES   = int(os.getenv("MAX_OPEN_TRADES",   "1"))
POSITION_QTY      = int(os.getenv("POSITION_QTY",      "1"))
TRADE_LOG_FILE    = os.getenv("TRADE_LOG_FILE", "trade_results.csv")

# Google Sheets tracking — bot creates/finds a spreadsheet by name automatically.
GOOGLE_SPREADSHEET_NAME   = os.getenv("GOOGLE_SPREADSHEET_NAME", "SPY Options Bot Log")
GOOGLE_SERVICE_ACCOUNT_EMAIL = os.getenv("GOOGLE_SERVICE_ACCOUNT_EMAIL", "")
GOOGLE_PRIVATE_KEY        = os.getenv("GOOGLE_PRIVATE_KEY", "").replace("\\n", "\n")

# Per-symbol score-trend history (one reading per cycle).
# At POLL_SECONDS=30s, capacity 24 = 12 minutes of history.
_SCORE_HISTORY_CAP = 24
score_history: "dict[str, deque[tuple[datetime, int, int]]]" = {
    sym: deque(maxlen=_SCORE_HISTORY_CAP) for sym in SYMBOLS
}

central = pytz.timezone("America/Chicago")
# Track which (symbol, side) pairs already alerted today so we never duplicate.
# Reset automatically when the trading date changes.
_alerted_today = {"date": None, "keys": set()}
# Per-symbol prev-day high/low cache.
_pdh_pdl_cache: "dict[str, dict]" = {sym: {"date": None, "pdh": None, "pdl": None} for sym in SYMBOLS}

# Open paper-trade book: contract symbol -> trade record dict.
# Capped by MAX_OPEN_TRADES across all underlyings.
_open_trades: "dict[str, dict]" = {}
# Lazily-initialized Alpaca paper TradingClient (created in main()).
_trading_client: "TradingClient | None" = None
# Lazily-initialized Alpaca OptionHistoricalDataClient (created in main()).
_option_client: "OptionHistoricalDataClient | None" = None
# (symbol, side) -> datetime after which re-alerting is allowed (post-trade cooldown).
_alert_cooldowns: "dict[tuple, datetime]" = {}
# Authorized gspread Spreadsheet object (None if not configured / failed).
_gsheet: "gspread.Spreadsheet | None" = None

# Column headers for the two Google Sheets tabs.
_ALERTS_HEADERS = [
    "Timestamp (CT)", "Symbol", "Signal", "Side", "Price",
    "Bull Score", "Bear Score", "Sentiment", "Ignition Delta",
    "Contract", "Expiry", "DTE", "Strike", "Bid", "Ask", "Last",
    "Option Volume", "Open Interest",
    "Score Components",
    "VWAP", "EMA20", "EMA50", "PDH", "PDL", "Recent High", "Recent Low",
    "Bar Volume", "Bar Vol Avg",
]
_TRADES_HEADERS = [
    "Opened At", "Closed At", "Duration (min)",
    "Symbol", "Contract", "Signal", "Strike", "Expiry",
    "Entry ($)", "Exit ($)", "PnL (%)", "PnL ($)", "Reason", "Score",
]


# ---------------------------------------------------------------------------
# Google Sheets integration
# ---------------------------------------------------------------------------
def init_google_sheets():
    """Create (or re-open) the bot's spreadsheet by name, then ensure Alerts + Trades tabs exist."""
    global _gsheet
    if not GOOGLE_SERVICE_ACCOUNT_EMAIL or not GOOGLE_PRIVATE_KEY:
        log("Google Sheets credentials not configured — sheet logging disabled.")
        return
    try:
        creds = GCredentials.from_service_account_info(
            {
                "type": "service_account",
                "project_id": "linear-catalyst-468901-g0",
                "private_key": GOOGLE_PRIVATE_KEY,
                "client_email": GOOGLE_SERVICE_ACCOUNT_EMAIL,
                "token_uri": "https://oauth2.googleapis.com/token",
            },
            scopes=[
                "https://spreadsheets.google.com/feeds",
                "https://www.googleapis.com/auth/drive",
            ],
        )
        gc = gspread.authorize(creds)

        # Find existing spreadsheet by name, or create a new one.
        try:
            _gsheet = gc.open(GOOGLE_SPREADSHEET_NAME)
            log(f"Opened existing Google Sheet: '{GOOGLE_SPREADSHEET_NAME}'")
        except gspread.exceptions.SpreadsheetNotFound:
            _gsheet = gc.create(GOOGLE_SPREADSHEET_NAME)
            # Make it accessible to anyone with the link (read+write).
            _gsheet.share(None, perm_type="anyone", role="writer")
            log(f"✅ Created new Google Sheet: '{GOOGLE_SPREADSHEET_NAME}'")

        log(f"🔗 Sheet URL: https://docs.google.com/spreadsheets/d/{_gsheet.id}")

        # Ensure Alerts tab exists with headers.
        try:
            _gsheet.worksheet("Alerts")
        except gspread.exceptions.WorksheetNotFound:
            ws = _gsheet.add_worksheet(title="Alerts", rows=5000, cols=len(_ALERTS_HEADERS))
            ws.append_row(_ALERTS_HEADERS, value_input_option="USER_ENTERED")
            log("Created 'Alerts' tab in Google Sheets.")

        # Ensure Trades tab exists with headers.
        try:
            _gsheet.worksheet("Trades")
        except gspread.exceptions.WorksheetNotFound:
            ws = _gsheet.add_worksheet(title="Trades", rows=2000, cols=len(_TRADES_HEADERS))
            ws.append_row(_TRADES_HEADERS, value_input_option="USER_ENTERED")
            log("Created 'Trades' tab in Google Sheets.")

    except Exception as e:
        log(f"Google Sheets init failed: {e} — sheet logging disabled.")
        _gsheet = None


def log_alert_to_sheets(symbol, data, option):
    """Append one row to the Alerts tab for every STRONG signal that fires."""
    if _gsheet is None:
        return
    try:
        breakdown = data["bull_breakdown"] if data["side"] == "CALL" else data["bear_breakdown"]
        components = ", ".join(f"{k} (+{v})" for k, v in breakdown.items())

        if data["side"] == "CALL":
            delta = (data["bull_score"] - data["bull_5m"]) if data["bull_5m"] is not None else ""
        else:
            delta = (data["bear_score"] - data["bear_5m"]) if data["bear_5m"] is not None else ""

        row = [
            datetime.now(central).strftime("%Y-%m-%d %H:%M:%S"),
            symbol,
            data["signal"],
            data["side"],
            round(data["price"], 2),
            data["bull_score"],
            data["bear_score"],
            data["sentiment"],
            delta,
            option["contract"]      if option else "",
            option["expiry"]        if option else "",
            option["dte"]           if option else "",
            option["strike"]        if option else "",
            option["bid"]           if option else "",
            option["ask"]           if option else "",
            option["last"]          if option else "",
            option["volume"]        if option else "",
            option["open_interest"] if option else "",
            components,
            round(data["vwap"],        2),
            round(data["ema20"],       2),
            round(data["ema50"],       2),
            round(data["pdh"],         2),
            round(data["pdl"],         2),
            round(data["recent_high"], 2),
            round(data["recent_low"],  2),
            int(data["volume"]),
            int(data["vol_avg"]),
        ]
        _gsheet.worksheet("Alerts").append_row(row, value_input_option="USER_ENTERED")
        log(f"[{symbol}] Alert logged to Google Sheets.")
    except Exception as e:
        log(f"[{symbol}] Google Sheets alert log failed: {e}")


def log_trade_to_sheets(row, trade):
    """Append one row to the Trades tab when a paper trade closes."""
    if _gsheet is None:
        return
    try:
        opened = row["opened_at"]
        closed = row["closed_at"]
        duration = (
            round((closed - opened).total_seconds() / 60, 1)
            if isinstance(opened, datetime) and isinstance(closed, datetime)
            else ""
        )
        pnl_dollar = round((row["exit"] - row["entry"]) * 100 * POSITION_QTY, 2)

        sheet_row = [
            opened.strftime("%Y-%m-%d %H:%M:%S") if isinstance(opened, datetime) else str(opened),
            closed.strftime("%Y-%m-%d %H:%M:%S") if isinstance(closed, datetime) else str(closed),
            duration,
            row["underlying"],
            row["contract"],
            row["signal"],
            trade.get("strike", ""),
            trade.get("expiry", ""),
            round(row["entry"],   4),
            round(row["exit"],    4),
            round(row["pnl_pct"], 2),
            pnl_dollar,
            row["reason"],
            row["score"],
        ]
        _gsheet.worksheet("Trades").append_row(sheet_row, value_input_option="USER_ENTERED")
        log(f"[{row['underlying']}] Trade logged to Google Sheets.")
    except Exception as e:
        log(f"Google Sheets trade log failed: {e}")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def send_discord(message):
    if not DISCORD_WEBHOOK_URL:
        print("Missing Discord webhook.")
        return
    if len(message) > 1990:
        message = message[:1987] + "..."
    try:
        r = requests.post(DISCORD_WEBHOOK_URL, json={"content": message}, timeout=10)
        if r.status_code not in (200, 204):
            print(f"Discord post returned {r.status_code}: {r.text[:100]}", flush=True)
    except Exception as e:
        print(f"Discord post failed: {e}")


def market_open_now():
    now = datetime.now(central)
    if now.weekday() >= 5:
        return False
    start = now.replace(hour=8, minute=30, second=0, microsecond=0)
    end = now.replace(hour=14, minute=55, second=0, microsecond=0)
    return start <= now <= end


def get_previous_day_levels(client, symbol):
    """Return previous trading day's high/low for ``symbol`` using Alpaca daily bars.

    Cached per session date (per symbol) so we only hit the API once per day.
    """
    today = datetime.now(central).date()
    cache = _pdh_pdl_cache.setdefault(symbol, {"date": None, "pdh": None, "pdl": None})
    if cache["date"] == today and cache["pdh"] is not None:
        return cache["pdh"], cache["pdl"]

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=14)  # buffer for weekends/holidays

    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame(1, TimeFrameUnit.Day),
        start=start,
        end=end,
        feed=DataFeed(FEED),
    )
    daily = client.get_stock_bars(req).df
    if daily is None or daily.empty:
        raise Exception(f"Not enough daily data from Alpaca for {symbol}.")

    if isinstance(daily.index, pd.MultiIndex):
        daily = daily.xs(symbol, level=0)

    daily = daily.dropna()
    if len(daily) < 2:
        raise Exception(f"Not enough daily data from Alpaca for {symbol}.")

    pdh = float(daily["high"].iloc[-2])
    pdl = float(daily["low"].iloc[-2])

    cache["date"] = today
    cache["pdh"] = pdh
    cache["pdl"] = pdl
    return pdh, pdl


def calculate_indicators(df):
    df = df.copy()
    df["EMA20"] = df["close"].ewm(span=20, adjust=False).mean()
    df["EMA50"] = df["close"].ewm(span=50, adjust=False).mean()
    df["VOL_AVG"] = df["volume"].rolling(20).mean()

    typical = (df["high"] + df["low"] + df["close"]) / 3

    # VWAP: reset at today's trading session — never cumsum across the overnight gap.
    eastern = pytz.timezone("America/New_York")
    if hasattr(df.index, "tz") and df.index.tz is not None:
        idx_et = df.index.tz_convert(eastern)
    else:
        idx_et = df.index.tz_localize("UTC").tz_convert(eastern)
    today_et = datetime.now(eastern).date()
    session_mask = pd.Series(
        [ts.date() == today_et for ts in idx_et], index=df.index, dtype=bool
    )
    vwap = pd.Series(float("nan"), index=df.index, dtype=float)
    if session_mask.any():
        t_vol = typical[session_mask] * df.loc[session_mask, "volume"]
        vwap[session_mask] = t_vol.cumsum() / df.loc[session_mask, "volume"].cumsum()
    df["VWAP"] = vwap
    return df


def analyze(df, client, symbol):
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

    pdh, pdl = get_previous_day_levels(client, symbol)
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
    history = score_history.setdefault(symbol, deque(maxlen=_SCORE_HISTORY_CAP))
    history.append((datetime.now(central), bull_score, bear_score))

    def history_at(seconds_ago):
        """Return (bull, bear) closest to N seconds ago, or (None, None)."""
        target = datetime.now(central) - timedelta(seconds=seconds_ago)
        for ts, b, s in reversed(history):
            if ts <= target:
                return b, s
        return (None, None)

    bull_5m, bear_5m = history_at(IGNITION_LOOKBACK_S)
    bull_10m, bear_10m = history_at(IGNITION_LOOKBACK_S * 2)

    print(f"[{symbol}] BULL score: {bull_score:3d} | BEAR score: {bear_score:3d}", flush=True)
    if bull_5m is not None:
        print(f"[{symbol}]   5m ago : BULL {bull_5m:3d} | BEAR {bear_5m:3d}  (Δ BULL {bull_score - bull_5m:+d})", flush=True)
    if bull_10m is not None:
        print(f"[{symbol}]  10m ago : BULL {bull_10m:3d} | BEAR {bear_10m:3d}  (Δ BULL {bull_score - bull_10m:+d})", flush=True)
    print(f"[{symbol}]   Bull components: {bull_breakdown}", flush=True)
    print(f"[{symbol}]   Bear components: {bear_breakdown}", flush=True)

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

def get_option_contract(symbol, signal, underlying_price):
    """Fetch the nearest 1-7 DTE ATM option contract from Alpaca (live data, no yfinance)."""
    if _option_client is None or _trading_client is None:
        print(f"[{symbol}] Option/trading client not initialised — cannot fetch contracts.", flush=True)
        return None
    try:
        today = date.today()
        min_exp = today + timedelta(days=MIN_DTE)
        max_exp = today + timedelta(days=MAX_DTE)
        option_type = "call" if signal == "CALL" else "put"

        req = GetOptionContractsRequest(
            underlying_symbols=[symbol],
            expiration_date_gte=min_exp,
            expiration_date_lte=max_exp,
            type=option_type,
            strike_price_gte=str(round(underlying_price * 0.95, 2)),
            strike_price_lte=str(round(underlying_price * 1.05, 2)),
            limit=50,
        )
        result = _trading_client.get_option_contracts(req)
        # SDK may return a list directly or a wrapper with .option_contracts / .contracts.
        if isinstance(result, list):
            contracts = result
        else:
            contracts = (
                getattr(result, "option_contracts", None)
                or getattr(result, "contracts", None)
                or []
            )
        if not contracts:
            print(f"[{symbol}] No option contracts found ({option_type}, {min_exp}–{max_exp}).", flush=True)
            return None

        print(f"[{symbol}] {len(contracts)} contract(s) returned by Alpaca for {option_type} {min_exp}–{max_exp}.", flush=True)

        def _exp_date(c):
            """Normalise expiration_date to a date object regardless of what Alpaca returns."""
            d = c.expiration_date
            if hasattr(d, "date"):          # datetime → date
                return d.date()
            return d                        # already a date

        # Nearest expiry first, then closest strike.
        contracts = sorted(
            contracts,
            key=lambda c: (
                (_exp_date(c) - today).days,
                abs(float(c.strike_price) - underlying_price),
            ),
        )
        best = contracts[0]
        contract_sym = best.symbol
        exp_date = _exp_date(best)
        print(f"[{symbol}] Best contract: {contract_sym}  strike={best.strike_price}  expiry={exp_date}", flush=True)

        # Fetch live bid/ask/last/volume via a single Alpaca snapshot call.
        snap_req = OptionSnapshotRequest(symbol_or_symbols=contract_sym)
        snaps = _option_client.get_option_snapshot(snap_req)
        snap = snaps.get(contract_sym) if isinstance(snaps, dict) else snaps

        def _safe_float(v, default=0.0):
            try:
                return float(v)
            except Exception:
                return default

        def _safe_int(v, default=0):
            try:
                return int(float(v))
            except Exception:
                return default

        bid = ask = last = 0.0
        vol = oi = 0
        if snap:
            q = getattr(snap, "latest_quote", None) or getattr(snap, "quote", None)
            t = getattr(snap, "latest_trade", None) or getattr(snap, "trade", None)
            bar = (
                getattr(snap, "daily_bar", None)
                or getattr(snap, "day_bar", None)
                or getattr(snap, "day", None)
            )

            if q is not None:
                bid = _safe_float(getattr(q, "bid_price", 0.0), 0.0)
                ask = _safe_float(getattr(q, "ask_price", 0.0), 0.0)
            if t is not None:
                last = _safe_float(getattr(t, "price", 0.0), 0.0)
            if bar is not None:
                vol = _safe_int(getattr(bar, "volume", 0), 0)

            oi = _safe_int(
                getattr(snap, "open_interest", None)
                or getattr(snap, "openInterest", None)
                or 0,
                0,
            )

        dte = (exp_date - today).days
        return {
            "contract":      contract_sym,
            "expiry":        exp_date.strftime("%Y-%m-%d"),
            "dte":           dte,
            "strike":        float(best.strike_price),
            "bid":           bid,
            "ask":           ask,
            "last":          last,
            "volume":        vol,
            "open_interest": oi,
            "side":          signal,  # stored so close_trade can build the alert_key
        }
    except Exception as e:
        msg = f"[{symbol}] Option contract fetch failed: {type(e).__name__}: {e}"
        print(msg, flush=True)
        send_discord(f"⚠️ **Option fetch error** — `{symbol}`\n```{msg}```")
        return None


# ---------------------------------------------------------------------------
# Alpaca REST
# ---------------------------------------------------------------------------
def fetch_bars(client, symbol):
    """Pull the most recent ~LOOKBACK_BARS 5-minute bars for ``symbol`` from Alpaca."""
    end = datetime.now(timezone.utc)
    # 5 days back so a Monday start always captures the previous Friday's bars.
    start = end - timedelta(days=5)

    req = StockBarsRequest(
        symbol_or_symbols=symbol,
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
        bars = bars.xs(symbol, level=0)

    bars = bars[["open", "high", "low", "close", "volume"]].tail(LOOKBACK_BARS)
    return bars


def log(msg):
    print(f"[{datetime.now(central):%Y-%m-%d %H:%M:%S} CT] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Paper-trading execution + position tracking
# ---------------------------------------------------------------------------
def place_paper_entry(option_contract):
    """Submit a paper BUY and poll up to 5s for the actual fill price.

    Returns (order, fill_price).  fill_price is None if the order did not fill
    within the polling window — the caller must treat this as a failure.
    """
    order_req = MarketOrderRequest(
        symbol=option_contract["contract"],
        qty=POSITION_QTY,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
    )
    order = _trading_client.submit_order(order_req)

    fill_price = None
    for _ in range(10):
        time.sleep(0.5)
        try:
            filled = _trading_client.get_order_by_id(str(order.id))
            if filled.status.value in ("filled", "partially_filled") and filled.filled_avg_price:
                fill_price = float(filled.filled_avg_price)
                break
        except Exception:
            pass

    return order, fill_price


def place_paper_exit(contract_symbol):
    """Submit a paper SELL and poll up to 5s for the actual fill price.

    Returns (order, fill_price).  fill_price is None if not filled in time.
    Market SELL fills at the bid (not mid), so this is the real exit price.
    """
    order_req = MarketOrderRequest(
        symbol=contract_symbol,
        qty=POSITION_QTY,
        side=OrderSide.SELL,
        time_in_force=TimeInForce.DAY,
    )
    order = _trading_client.submit_order(order_req)

    fill_price = None
    for _ in range(10):
        time.sleep(0.5)
        try:
            filled = _trading_client.get_order_by_id(str(order.id))
            if filled.status.value in ("filled", "partially_filled") and filled.filled_avg_price:
                fill_price = float(filled.filled_avg_price)
                break
        except Exception:
            pass

    return order, fill_price


def open_trade_record(symbol, signal, option, score, fill_price):
    """Build a trade record using the actual Alpaca fill price (never a yfinance estimate)."""
    entry_price = fill_price
    side = option.get("side", signal.split()[-1])  # "CALL" or "PUT"
    return {
        "underlying": symbol,
        "signal":     signal,
        "side":       side,
        "contract":   option["contract"],
        "expiry":     option["expiry"],
        "strike":     option["strike"],
        "entry":      entry_price,
        "target":     entry_price * (1 + PROFIT_TARGET_PCT),
        "stop":       entry_price * (1 - STOP_LOSS_PCT),
        "score":      score,
        "opened_at":  datetime.now(central),
        "status":     "OPEN",
    }


def get_current_option_price(trade):
    """Get live mid/bid price for an open contract from Alpaca (no yfinance)."""
    if _option_client is None:
        return None
    try:
        contract_sym = trade["contract"]
        req = OptionLatestQuoteRequest(symbol_or_symbols=contract_sym)
        quotes = _option_client.get_option_latest_quote(req)
        q = quotes.get(contract_sym)
        if q is None:
            return None
        bid = float(q.bid_price or 0)
        ask = float(q.ask_price or 0)
        if bid > 0 and ask > 0:
            return (bid + ask) / 2  # mid-price for accurate PnL tracking
        return bid if bid > 0 else None
    except Exception as e:
        log(f"[{trade['underlying']}] Price check failed for {trade['contract']}: {e}")
        return None


def track_open_trades():
    """Walk every open paper trade, mark current PnL, and exit on target/stop.

    Both entry and current price are pulled live from Alpaca positions API.
    This guarantees our internal record matches what the account actually holds.
    Falls back to OptionLatestQuoteRequest only if the position is not yet
    visible in Alpaca (e.g. fill not yet propagated).
    """
    if not _open_trades:
        return

    for trade in list(_open_trades.values()):
        contract_sym = trade["contract"]

        # ── Primary: pull entry + current price directly from Alpaca position ──
        alpaca_entry   = None
        current_price  = None

        if _trading_client is not None:
            try:
                pos = _trading_client.get_open_position(contract_sym)
                if pos.avg_entry_price:
                    alpaca_entry = float(pos.avg_entry_price)
                if pos.current_price:
                    current_price = float(pos.current_price)

                # Sync our record if Alpaca's entry differs (fill slippage, partial fills, etc.)
                if alpaca_entry and abs(alpaca_entry - trade["entry"]) > 0.001:
                    log(f"[{trade['underlying']}] Entry corrected: "
                        f"${trade['entry']:.2f} → ${alpaca_entry:.2f} (Alpaca actual fill)")
                    trade["entry"]  = alpaca_entry
                    trade["target"] = alpaca_entry * (1 + PROFIT_TARGET_PCT)
                    trade["stop"]   = alpaca_entry * (1 - STOP_LOSS_PCT)
            except Exception:
                pass  # position not yet visible or already closed on Alpaca side

        # ── Fallback: use Alpaca option quote if position not found ──
        if current_price is None:
            current_price = get_current_option_price(trade)

        if current_price is None:
            log(f"[{trade['underlying']}] {contract_sym} — no current price from Alpaca, skipping.")
            continue

        entry   = trade["entry"]
        pnl_pct = (current_price - entry) / entry if entry else 0.0
        log(f"[{trade['underlying']}] {contract_sym} | "
            f"entry ${entry:.2f} (Alpaca) | current ${current_price:.2f} (Alpaca) | "
            f"PnL {pnl_pct * 100:+.2f}%")

        if pnl_pct >= PROFIT_TARGET_PCT:
            close_trade(trade, current_price, "TARGET HIT", pnl_pct)
        elif pnl_pct <= -STOP_LOSS_PCT:
            close_trade(trade, current_price, "STOP LOSS",  pnl_pct)


def close_trade(trade, exit_price, reason, pnl_pct):
    """Submit a paper exit (if enabled), Discord the result, and append to CSV.

    exit_price is the mid-price trigger used to detect target/stop.  After
    submitting the market SELL we replace it with the actual Alpaca fill price
    so Discord and the CSV reflect what the account really received (bid-side).
    """
    if ENABLE_ALPACA_PAPER_TRADING and _trading_client is not None:
        try:
            _, fill_price = place_paper_exit(trade["contract"])
            if fill_price is not None:
                # Recalculate PnL using the real fill, not the mid-price estimate.
                actual_exit = fill_price
                actual_pnl  = (actual_exit - trade["entry"]) / trade["entry"]
                log(f"[{trade['underlying']}] Exit fill ${actual_exit:.2f} "
                    f"(mid was ${exit_price:.2f}, diff ${actual_exit - exit_price:+.2f})")
                exit_price = actual_exit
                pnl_pct    = actual_pnl
        except Exception as e:
            log(f"[{trade['underlying']}] Paper exit submit failed: {e}")

    emoji = "\u2705" if pnl_pct > 0 else "\u274c"
    closed_at = datetime.now(central)
    send_discord(
        f"{emoji} **{reason}** \u2014 {trade['underlying']} {trade['signal']}\n\n"
        f"Contract: `{trade['contract']}`\n"
        f"Entry: `{trade['entry']:.2f}`  Exit: `{exit_price:.2f}`\n"
        f"PnL: `{pnl_pct * 100:+.2f}%`  Score: `{trade['score']}`\n"
        f"Opened: `{trade['opened_at']:%Y-%m-%d %H:%M:%S %Z}`\n"
        f"Closed: `{closed_at:%Y-%m-%d %H:%M:%S %Z}`"
    )

    row = {
        "opened_at": trade["opened_at"],
        "closed_at": closed_at,
        "underlying": trade["underlying"],
        "contract":   trade["contract"],
        "signal":     trade["signal"],
        "entry":      trade["entry"],
        "exit":       exit_price,
        "pnl_pct":    pnl_pct * 100,
        "reason":     reason,
        "score":      trade["score"],
    }
    try:
        pd.DataFrame([row]).to_csv(
            TRADE_LOG_FILE,
            mode="a",
            header=not os.path.exists(TRADE_LOG_FILE),
            index=False,
        )
    except Exception as e:
        log(f"CSV write failed: {e}")

    log_trade_to_sheets(row, trade)

    _open_trades.pop(trade["contract"], None)

    # 30-minute cooldown before the same (symbol, side) can re-alert.
    # Prevents same-day whipsaw but allows re-entry after the lockout expires.
    alert_key = (trade["underlying"], trade.get("side", trade["signal"].split()[-1]))
    _alert_cooldowns[alert_key] = datetime.now(central) + timedelta(minutes=30)
    # Keep alert_key in _alerted_today so run_symbol suppresses until cooldown clears.
    _alerted_today["keys"].add(alert_key)

    log(f"[{trade['underlying']}] Closed {trade['contract']} ({reason}, {pnl_pct * 100:+.2f}%)")


def try_open_paper_trade(symbol, side, option, data):
    """Open a paper trade if trading is enabled and we have capacity. Returns True if opened."""
    if not ENABLE_ALPACA_PAPER_TRADING:
        return False

    # No new entries after 13:30 CT (14:30 ET) — theta decay too high on short-DTE options.
    now_ct = datetime.now(central)
    if now_ct.hour > 13 or (now_ct.hour == 13 and now_ct.minute >= 30):
        log(f"[{symbol}] After 13:30 CT — no new paper trade entries (theta risk). Alert only.")
        return False

    if len(_open_trades) >= MAX_OPEN_TRADES:
        log(f"[{symbol}] Paper-trade capacity full ({len(_open_trades)}/{MAX_OPEN_TRADES}) — alert only.")
        return False
    if option["contract"] in _open_trades:
        log(f"[{symbol}] Already long {option['contract']} — not stacking.")
        return False
    if _trading_client is None:
        return False

    score = data["bull_score"] if side == "CALL" else data["bear_score"]
    signal_label = f"STRONG {side}"

    try:
        _, fill_price = place_paper_entry(option)
    except Exception as e:
        log(f"[{symbol}] Paper entry submit failed: {e} — not tracking trade.")
        return False

    if fill_price is None:
        log(f"[{symbol}] Order submitted but no fill confirmed within 5s — not tracking trade.")
        return False

    trade = open_trade_record(symbol, signal_label, option, score, fill_price)
    _open_trades[trade["contract"]] = trade

    send_discord(
        f"💰 **{symbol} {signal_label} — PAPER TRADE OPENED**\n\n"
        f"Contract: `{trade['contract']}`\n"
        f"Expiry: `{trade['expiry']}`  Strike: `{trade['strike']}`\n\n"
        f"Entry (filled): `{trade['entry']:.2f}`\n"
        f"Target: `{trade['target']:.2f}`  (+{PROFIT_TARGET_PCT * 100:.0f}%)\n"
        f"Stop:   `{trade['stop']:.2f}`  (-{STOP_LOSS_PCT * 100:.0f}%)\n\n"
        f"Score: `{score}`  Qty: `{POSITION_QTY}`"
    )
    log(f"[{symbol}] Paper trade opened: {trade['contract']} fill ${trade['entry']:.2f} "
        f"target ${trade['target']:.2f} stop ${trade['stop']:.2f}")
    return True


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

    # Manage any open paper trade first so target/stop can fire even when
    # no new signal is forming this cycle.
    try:
        track_open_trades()
    except Exception:
        log("track_open_trades error:")
        traceback.print_exc()
        sys.stdout.flush()

    for symbol in SYMBOLS:
        try:
            run_symbol(client, symbol)
        except Exception:
            log(f"[{symbol}] Cycle error:")
            traceback.print_exc()
            sys.stdout.flush()


def run_symbol(client, symbol):
    bars = fetch_bars(client, symbol)
    log(f"[{symbol}] Fetched {len(bars)} bars.")
    if len(bars) < 55:
        log(f"[{symbol}] Bars: {len(bars)}/55 — warming up.")
        return

    side, data = analyze(bars, client, symbol)
    if data:
        trend_5m = f" | 5m\u0394 BULL {data['bull_score'] - data['bull_5m']:+d}" if data['bull_5m'] is not None else ""
        log(
            f"[{symbol}] {data['price']:.2f} | {data['signal']} | "
            f"BULL {data['bull_score']} BEAR {data['bear_score']} ({data['sentiment']}){trend_5m}"
        )

    if side == "NO TRADE":
        return

    # Only send Discord alerts for the perfect setup (STRONG tier).
    # SIGNAL/WATCHLIST tiers are logged but not posted.
    if data["tier"] != "STRONG":
        log(f"[{symbol}] {data['signal']} (BULL {data['bull_score']} / BEAR {data['bear_score']}) "
            f"\u2014 below STRONG threshold, no Discord alert.")
        return

    # One STRONG CALL alert and one STRONG PUT alert max per (symbol, side) per day.
    # After a trade closes, a 30-min cooldown allows the same setup to re-trigger.
    alert_key = (symbol, side)
    now_ct = datetime.now(central)
    if alert_key in _alerted_today["keys"]:
        if alert_key in _alert_cooldowns and now_ct >= _alert_cooldowns[alert_key]:
            # Cooldown expired — clear and allow re-alert.
            _alerted_today["keys"].discard(alert_key)
            _alert_cooldowns.pop(alert_key, None)
        else:
            log(f"[{symbol}] Already alerted {side} today — suppressed.")
            return

    # Trend-ignition filter: only fire when the move is *just starting*, not mid- or late-trend.
    if IGNITION_REQUIRED:
        if side == "CALL":
            now_score = data["bull_score"]
            past_score = data["bull_5m"]
        else:
            now_score = data["bear_score"]
            past_score = data["bear_5m"]

        if past_score is None:
            log(
                f"[{symbol}] Ignition gate: insufficient history (need {IGNITION_LOOKBACK_S}s) \u2014 "
                "holding alert until trend can be measured."
            )
            return

        delta = now_score - past_score
        if past_score >= IGNITION_PRIOR_MAX:
            log(
                f"[{symbol}] Ignition gate: {side} 5m ago was already {past_score} "
                f"(>= {IGNITION_PRIOR_MAX}) \u2014 mid/late trend, no alert."
            )
            return
        if delta < IGNITION_MIN_DELTA:
            log(
                f"[{symbol}] Ignition gate: {side} delta only +{delta} (need +{IGNITION_MIN_DELTA}) "
                f"\u2014 trend not igniting, no alert."
            )
            return

        log(
            f"[{symbol}] \U0001f680 Ignition confirmed: {side} score {past_score} \u2192 {now_score} "
            f"(\u0394 +{delta}) over last {IGNITION_LOOKBACK_S}s."
        )

    # Lock this (symbol, side) NOW — before the option fetch — so a failed or
    # rate-limited fetch doesn't cause ignition to re-fire every 30 seconds.
    _alerted_today["keys"].add(alert_key)

    option = get_option_contract(symbol, side, data["price"])
    if not option:
        log(f"[{symbol}] {data['signal']} setup detected, but no valid 1DTE+ option found — sending alert-only Discord.")
        emoji = "\U0001f7e2" if side == "CALL" else "\U0001f534"
        breakdown = data["bull_breakdown"] if side == "CALL" else data["bear_breakdown"]
        checklist = "\n".join(f"\u2705 {k} (+{v})" for k, v in breakdown.items()) or "(no positive components)"
        send_discord(
            f"\U0001f6a8 {emoji} **{symbol} {data['signal']}** _(no option contract found — alert only)_\n\n"
            f"**{symbol}:** `{data['price']:.2f}`\n"
            f"**Bull Score:** `{data['bull_score']}/100`   |   **Bear Score:** `{data['bear_score']}/100`\n"
            f"**Sentiment:** `{data['sentiment']}`\n\n"
            f"**Score Components**\n{checklist}\n\n"
            f"**Levels**\n"
            f"VWAP: `{data['vwap']:.2f}` | EMA20: `{data['ema20']:.2f}` | EMA50: `{data['ema50']:.2f}`\n"
            f"PDH: `{data['pdh']:.2f}` | PDL: `{data['pdl']:.2f}`"
        )
        log_alert_to_sheets(symbol, data, None)
        return

    emoji = "\U0001f7e2" if side == "CALL" else "\U0001f534"
    header = f"\U0001f6a8 {emoji} **{symbol} {data['signal']}**"

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

**{symbol}:** `{data['price']:.2f}`
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
    log(f"[{symbol}] Alert sent: {data['signal']} {option['contract']} (BULL {data['bull_score']} / BEAR {data['bear_score']})")
    log_alert_to_sheets(symbol, data, option)

    # Open a paper trade (if enabled + capacity).
    try:
        try_open_paper_trade(symbol, side, option, data)
    except Exception:
        log(f"[{symbol}] try_open_paper_trade error:")
        traceback.print_exc()
        sys.stdout.flush()


def main():
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        raise Exception("Missing Alpaca API keys in .env")

    client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)

    global _trading_client, _option_client, _gsheet
    _option_client = OptionHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
    log("OptionHistoricalDataClient initialized — live Alpaca option data (no yfinance).")
    # Always init TradingClient — needed for GetOptionContractsRequest even when paper
    # trading is disabled.  Order submission is gated separately by ENABLE_ALPACA_PAPER_TRADING.
    _trading_client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=True)
    if ENABLE_ALPACA_PAPER_TRADING:
        log("Paper trading ENABLED — Alpaca paper TradingClient initialized.")
    else:
        log("Paper trading DISABLED — alerts only, no orders will be submitted (TradingClient used for option contract lookup only).")

    init_google_sheets()

    log(f"Options Alert Bot started. Symbols={','.join(SYMBOLS)} "
        f"Polling every {POLL_SECONDS}s. Feed={FEED}.")

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
