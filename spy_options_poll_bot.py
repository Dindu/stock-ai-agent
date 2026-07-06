"""
Index ETF Options Alerts Bot — polling version (SPY / QQQ / IWM by default).

Pulls 5-minute bars from Alpaca REST every POLL_SECONDS for each configured
symbol, runs a Bull/Bear market scorecard, applies a trend-ignition filter,
and posts a Discord alert with a near-the-money 1DTE+ option contract from
Alpaca's live options data when the score ignites into STRONG territory.

No WebSocket -> no Alpaca connection-limit issues.
"""

import os
import re
import sys
import time
import traceback
import uuid
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
from alpaca.data.enums import DataFeed, OptionsFeed
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
_options_feed_env = os.getenv("ALPACA_OPTIONS_FEED", "").strip().lower()
if _options_feed_env in {"opra", "indicative"}:
    OPTIONS_FEED = OptionsFeed(_options_feed_env)
else:
    OPTIONS_FEED = None

# Symbols to scan, in order. Override via env.
ETF_SYMBOLS = {"SPY", "QQQ", "IWM"}
TOP_STOCK_SYMBOLS = {
    "AAPL", "NVDA", "MSFT", "AMZN", "META",
    "TSLA", "AMD", "PLTR", "NFLX", "GOOGL",
    "AVGO", "SMCI", "MU", "COIN", "QCOM",
    "INTC", "CRM", "ORCL", "SHOP", "UBER",
}
AGGRESSIVE_STOCK_SYMBOLS = {"TSLA", "AMD", "PLTR", "SMCI", "COIN", "SHOP", "UBER"}
DEFAULT_SYMBOLS = "SPY,QQQ,IWM,AAPL,NVDA,MSFT,AMZN,META,TSLA,AMD,PLTR,NFLX,GOOGL,AVGO,SMCI,MU,COIN,QCOM,INTC,CRM,ORCL,SHOP,UBER"
SYMBOLS = [s.strip().upper() for s in os.getenv("SYMBOLS", DEFAULT_SYMBOLS).split(",") if s.strip()]
ALPACA_DATA_BASE_URL = os.getenv("ALPACA_DATA_BASE_URL", "https://data.alpaca.markets")
ENABLE_TRENDING_STOCKS = os.getenv("ENABLE_TRENDING_STOCKS", "1") == "1"
TRENDING_STOCK_COUNT = int(os.getenv("TRENDING_STOCK_COUNT", "5"))
TRENDING_REFRESH_SECONDS = int(os.getenv("TRENDING_REFRESH_SECONDS", "300"))
TRENDING_NEWS_HEADLINES = int(os.getenv("TRENDING_NEWS_HEADLINES", "2"))
TRENDING_MIN_BAR_COUNT = int(os.getenv("TRENDING_MIN_BAR_COUNT", "55"))
TRENDING_MIN_LAST_VOLUME = int(os.getenv("TRENDING_MIN_LAST_VOLUME", "100000"))
TRENDING_MIN_PRICE = float(os.getenv("TRENDING_MIN_PRICE", "5"))
TRENDING_EXCLUDE_WARRANTS = os.getenv("TRENDING_EXCLUDE_WARRANTS", "1") == "1"
TRENDING_EXCLUDE_SYMBOLS = {
    s.strip().upper()
    for s in os.getenv("TRENDING_EXCLUDE_SYMBOLS", "BITO").split(",")
    if s.strip()
}
ENABLE_SYMBOL_NEWS_CONTEXT = os.getenv("ENABLE_SYMBOL_NEWS_CONTEXT", "1") == "1"
SYMBOL_NEWS_HEADLINES = int(os.getenv("SYMBOL_NEWS_HEADLINES", "2"))
SYMBOL_NEWS_REFRESH_SECONDS = int(os.getenv("SYMBOL_NEWS_REFRESH_SECONDS", "300"))
BAR_MINUTES = 5
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "30"))  # 30 seconds for index options
OPENING_NO_TRADE_MINUTES = int(os.getenv("OPENING_NO_TRADE_MINUTES", "15"))
CLOSING_NO_TRADE_MINUTES = int(os.getenv("CLOSING_NO_TRADE_MINUTES", "30"))
LOOKBACK_BARS = 120
RECENT_HIGH_LOOKBACK = 20  # bars used for intraday recent high/low (~100 min)
MIN_DTE = int(os.getenv("MIN_DTE", "1"))   # Minimum DTE (exclude 0DTE)
MAX_DTE = int(os.getenv("MAX_DTE", "14"))  # Max DTE window; set <=0 for no upper bound
VOLUME_MULTIPLIER = 1.5

# Scoring thresholds (0-100)
SCORE_STRONG = int(os.getenv("SCORE_STRONG", "80"))   # STRONG CALL/PUT alert
SCORE_SIGNAL = int(os.getenv("SCORE_SIGNAL", "65"))   # CALL/PUT alert
SCORE_WATCH  = int(os.getenv("SCORE_WATCH",  "50"))   # WATCHLIST heads-up
SCORE_DOMINANCE = int(os.getenv("SCORE_DOMINANCE", "20"))  # bull must lead bear by this much (and vice versa)
# Global bypass for all entry gating layers (tier/stock strict/opening/cooldown/ignition/RSI/macro/anti-chase/candle).
NO_GATING_MODE = os.getenv("NO_GATING_MODE", "1") == "1"
ENFORCE_OPENING_WINDOW_IN_NO_GATING = os.getenv("ENFORCE_OPENING_WINDOW_IN_NO_GATING", "1") == "1"
# Stock-only tightening: require a slightly higher effective strong score.
STOCK_STRONG_SCORE_BONUS = int(os.getenv("STOCK_STRONG_SCORE_BONUS", "5"))
# Hard score gate control:
# - When HARD_SCORE_GATE_ENABLED=0, no hard minimum score is enforced.
# - When NO_GATING_MODE=1, the hard gate is bypassed by default unless
#   HARD_SCORE_GATE_IN_NO_GATING_MODE=1.
HARD_SCORE_GATE_ENABLED = os.getenv("HARD_SCORE_GATE_ENABLED", "1") == "1"
HARD_SCORE_GATE_IN_NO_GATING_MODE = os.getenv("HARD_SCORE_GATE_IN_NO_GATING_MODE", "1") == "1"
# Execution behavior controls.
# Default: do not auto-execute WATCHLIST setups (alerts-only quality).
EXECUTE_WATCHLIST_SIGNALS = os.getenv("EXECUTE_WATCHLIST_SIGNALS", "0") == "1"
# Pre-check options buying power before submitting market BUYs.
OPTIONS_BP_BUFFER_PCT = float(os.getenv("OPTIONS_BP_BUFFER_PCT", "0.05"))

# Trend-ignition filter: only fire when the score is *starting* to rise into the threshold.
# CALL example: 5 minutes ago BULL was below IGNITION_PRIOR_MAX, now it has gained at least IGNITION_MIN_DELTA.
# Set IGNITION_REQUIRED=0 in env to disable and revert to absolute-score-only firing.
IGNITION_REQUIRED   = os.getenv("IGNITION_REQUIRED", "1") == "1"
IGNITION_MIN_DELTA  = int(os.getenv("IGNITION_MIN_DELTA",  "25"))  # Raised: require a stronger burst to confirm real ignition
IGNITION_PRIOR_MAX  = int(os.getenv("IGNITION_PRIOR_MAX",  "74"))  # Block only if already at STRONG level (≥75); allows breakouts from 70
IGNITION_LOOKBACK_S = int(os.getenv("IGNITION_LOOKBACK_S", "300"))  # how far back to compare (default 5 min)
# Continuation override: allow one strong alert even if the trend is already hot,
# as long as score remains very strong and has not faded over lookback.
IGNITION_CONTINUATION_ENABLED = os.getenv("IGNITION_CONTINUATION_ENABLED", "1") == "1"
IGNITION_CONTINUATION_MIN_SCORE = int(os.getenv("IGNITION_CONTINUATION_MIN_SCORE", "85"))
IGNITION_CONTINUATION_MIN_DELTA = int(os.getenv("IGNITION_CONTINUATION_MIN_DELTA", "0"))

# RSI exhaustion filter: don't buy CALLs when RSI is overbought or PUTs when oversold.
# Set RSI_FILTER=0 to disable.
RSI_FILTER         = os.getenv("RSI_FILTER", "1") == "1"
RSI_OVERBOUGHT     = int(os.getenv("RSI_OVERBOUGHT", "70"))  # block CALL entries above this
RSI_OVERSOLD       = int(os.getenv("RSI_OVERSOLD",   "30"))  # block PUT entries below this

# Macro alignment for non-SPY symbols.
# Default behavior is confidence adjustment (penalty), not a hard veto.
# Set SPY_MACRO_HARD_BLOCK=1 to restore strict blocking behavior.
SPY_MACRO_ALIGN         = os.getenv("SPY_MACRO_ALIGN", "1") == "1"
SPY_MACRO_SCORE_PENALTY = int(os.getenv("SPY_MACRO_SCORE_PENALTY", "10"))
SPY_MACRO_HARD_BLOCK    = os.getenv("SPY_MACRO_HARD_BLOCK", "0") == "1"

# Anti-chase entry filters: avoid entering when price is too stretched from VWAP,
# or when the latest candle already flipped against the intended side.
ANTI_CHASE_FILTER  = os.getenv("ANTI_CHASE_FILTER", "1") == "1"
MAX_EXT_FROM_VWAP  = float(os.getenv("MAX_EXT_FROM_VWAP", "0.012"))  # 1.2%
CANDLE_CONFIRMATION = os.getenv("CANDLE_CONFIRMATION", "1") == "1"
ALERT_ONLY_COOLDOWN_MINUTES = int(os.getenv("ALERT_ONLY_COOLDOWN_MINUTES", "20"))

# Paper-trading execution. When ENABLE_ALPACA_PAPER_TRADING=1 the bot will
# submit a paper-account market BUY when a STRONG signal fires, then poll the
# option price each cycle and submit a paper-account market SELL at +20% / -20%.
# Set to 0 to keep the bot in pure alert mode (no orders submitted, no tracking).
ENABLE_ALPACA_PAPER_TRADING = os.getenv("ENABLE_ALPACA_PAPER_TRADING", "1") == "1"
PROFIT_TARGET_PCT = float(os.getenv("PROFIT_TARGET_PCT", "0.20"))  # take-profit at +20%
STOP_LOSS_PCT     = float(os.getenv("STOP_LOSS_PCT",     "0.20"))  # stop-loss at -20%
# Legacy profit-protection env vars are retained for backward compatibility,
# but are intentionally not used in exit logic.
PROFIT_PROTECT_ARM_PCT = float(os.getenv("PROFIT_PROTECT_ARM_PCT", "0.04"))
PROFIT_PROTECT_FLOOR_PCT = float(os.getenv("PROFIT_PROTECT_FLOOR_PCT", "0.00"))
PROFIT_PROTECT_DRAWDOWN_PCT = float(os.getenv("PROFIT_PROTECT_DRAWDOWN_PCT", "0.08"))
MAX_OPEN_TRADES   = int(os.getenv("MAX_OPEN_TRADES",   "250"))
# Block stacking multiple strikes on the same symbol+side while one is open.
SINGLE_POSITION_PER_SYMBOL = os.getenv("SINGLE_POSITION_PER_SYMBOL", "1") == "1"
# Rehydrate in-memory trade tracking from Alpaca open positions after restarts.
RECOVER_OPEN_POSITIONS = os.getenv("RECOVER_OPEN_POSITIONS", "1") == "1"
BASE_POSITION_QTY = int(os.getenv("POSITION_QTY",      "1"))
MIN_POSITION_QTY  = int(os.getenv("MIN_POSITION_QTY",  "5"))
MAX_POSITION_QTY  = int(os.getenv("MAX_POSITION_QTY",  "10"))
CONFIDENCE_POSITIONING = os.getenv("CONFIDENCE_POSITIONING", "1") == "1"
CONFIDENCE_STEP_SCORE  = int(os.getenv("CONFIDENCE_STEP_SCORE", "5"))
TRADE_LOG_FILE    = os.getenv("TRADE_LOG_FILE", "trade_results.csv")

# Option quality filters (stock-only tightened defaults; ETFs remain baseline).
ETF_MIN_OPTION_BID = float(os.getenv("ETF_MIN_OPTION_BID", "0.15"))
ETF_MAX_OPTION_SPREAD_PCT = float(os.getenv("ETF_MAX_OPTION_SPREAD_PCT", "0.30"))
STOCK_MIN_OPTION_BID = float(os.getenv("STOCK_MIN_OPTION_BID", "0.20"))
STOCK_MAX_OPTION_SPREAD_PCT = float(os.getenv("STOCK_MAX_OPTION_SPREAD_PCT", "0.25"))
STOCK_MIN_OPTION_VOLUME = int(os.getenv("STOCK_MIN_OPTION_VOLUME", "10"))
STOCK_MIN_OPTION_OPEN_INTEREST = int(os.getenv("STOCK_MIN_OPTION_OPEN_INTEREST", "50"))

# Google Sheets tracking — bot creates/finds a spreadsheet by name automatically.
GOOGLE_SPREADSHEET_NAME   = os.getenv("GOOGLE_SPREADSHEET_NAME", "SPY Options Bot Log")
GOOGLE_SPREADSHEET_ID     = os.getenv("GOOGLE_SPREADSHEET_ID", "")  # paste sheet ID from URL to use existing sheet
FORCE_NEW_GOOGLE_SHEET    = os.getenv("FORCE_NEW_GOOGLE_SHEET", "0") == "1"
GOOGLE_SERVICE_ACCOUNT_EMAIL = os.getenv("GOOGLE_SERVICE_ACCOUNT_EMAIL", "")
GOOGLE_PRIVATE_KEY        = os.getenv("GOOGLE_PRIVATE_KEY", "").replace("\\n", "\n")
OWNER_EMAIL               = os.getenv("OWNER_EMAIL", "")  # your Gmail — sheet is shared to this on startup
GSHEET_RETRY_SECONDS      = int(os.getenv("GSHEET_RETRY_SECONDS", "60"))

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
_last_gsheet_init_attempt: "datetime | None" = None
# Last known SPY VWAP side: 'bull', 'bear', or None (populated by run_symbol each cycle).
_spy_vwap_cache: "dict" = {"side": None, "updated_at": None}
# Cached trending stocks and reasons from Alpaca screener/news.
_trending_cache: "dict" = {"updated_at": None, "symbols": [], "reasons": {}}
# Cached tradability/profile checks for trending candidates.
_trending_asset_cache: "dict" = {}
# Cached symbol news context used by entry alerts.
_symbol_news_cache: "dict" = {"updated_at": {}, "items": {}}

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
    "Trade ID", "Symbol", "Entry Price", "Exit Price", "Strike", "Direction",
    "Entry Time", "Exit Time", "Exit Reason", "Status", "Options Expiration",
    "Alpaca Order ID", "P&L", "P&L %", "Duration", "Created At", "Updated At",
]


# ---------------------------------------------------------------------------
# Google Sheets integration
# ---------------------------------------------------------------------------
def init_google_sheets():
    """Create (or re-open) the bot's spreadsheet by name, then ensure Alerts + Trades tabs exist."""
    global _gsheet, _last_gsheet_init_attempt
    _last_gsheet_init_attempt = datetime.now(central)
    if not GOOGLE_SERVICE_ACCOUNT_EMAIL or not GOOGLE_PRIVATE_KEY:
        log("Google Sheets credentials not configured — sheet logging disabled.")
        return
    step = "build credentials"
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
        step = "authorize gspread client"
        gc = gspread.authorize(creds)

        # Prefer explicit sheet ID when provided (stable target across restarts).
        # This avoids accidental sheet churn if FORCE_NEW_GOOGLE_SHEET is left enabled.
        if GOOGLE_SPREADSHEET_ID:
            step = f"open spreadsheet by id {GOOGLE_SPREADSHEET_ID[:8]}..."
            if FORCE_NEW_GOOGLE_SHEET:
                log("FORCE_NEW_GOOGLE_SHEET=1 ignored because GOOGLE_SPREADSHEET_ID is set.")
            _gsheet = gc.open_by_key(GOOGLE_SPREADSHEET_ID)
            log(f"Opened existing Google Sheet by ID: '{_gsheet.title}'")
        # Force-create mode: always create a fresh sheet with a timestamped name.
        elif FORCE_NEW_GOOGLE_SHEET:
            step = "create new spreadsheet (force mode)"
            fresh_name = f"{GOOGLE_SPREADSHEET_NAME} {datetime.now(central).strftime('%Y-%m-%d %H%M%S')}"
            _gsheet = gc.create(fresh_name)
            _gsheet.share(None, perm_type="anyone", role="writer")
            log(f"✅ Force-created new Google Sheet: '{fresh_name}'")
        else:
            # Find existing spreadsheet by name, or create a new one.
            try:
                step = f"open spreadsheet by name '{GOOGLE_SPREADSHEET_NAME}'"
                _gsheet = gc.open(GOOGLE_SPREADSHEET_NAME)
                log(f"Opened existing Google Sheet: '{GOOGLE_SPREADSHEET_NAME}'")
            except gspread.exceptions.SpreadsheetNotFound:
                step = f"create spreadsheet by name '{GOOGLE_SPREADSHEET_NAME}'"
                _gsheet = gc.create(GOOGLE_SPREADSHEET_NAME)
                # Make it accessible to anyone with the link (read+write).
                _gsheet.share(None, perm_type="anyone", role="writer")
                log(f"✅ Created new Google Sheet: '{GOOGLE_SPREADSHEET_NAME}'")

        log(f"🔗 Sheet URL: https://docs.google.com/spreadsheets/d/{_gsheet.id}")

        # Share to owner's personal Google account so it shows up in their Sheets.
        if OWNER_EMAIL:
            try:
                _gsheet.share(OWNER_EMAIL, perm_type="user", role="writer", notify=False)
                log(f"Sheet shared to {OWNER_EMAIL}")
            except Exception as share_err:
                log(f"Could not share sheet to {OWNER_EMAIL}: {share_err}")

        # Ensure Alerts tab exists with headers.
        try:
            alerts_ws = _gsheet.worksheet("Alerts")
        except gspread.exceptions.WorksheetNotFound:
            alerts_ws = _gsheet.add_worksheet(title="Alerts", rows=5000, cols=len(_ALERTS_HEADERS))
            log("Created 'Alerts' tab in Google Sheets.")
        try:
            existing_alert_headers = alerts_ws.row_values(1)
            if existing_alert_headers != _ALERTS_HEADERS:
                alerts_ws.update(
                    range_name="A1",
                    values=[_ALERTS_HEADERS],
                    value_input_option="USER_ENTERED",
                )
                log("Ensured 'Alerts' header row in Google Sheets.")
        except Exception as header_err:
            log(f"Warning: Could not verify/set Alerts headers: {header_err}")

        # Ensure Trades tab exists with headers.
        try:
            trades_ws = _gsheet.worksheet("Trades")
        except gspread.exceptions.WorksheetNotFound:
            trades_ws = _gsheet.add_worksheet(title="Trades", rows=2000, cols=len(_TRADES_HEADERS))
            log("Created 'Trades' tab in Google Sheets.")
        try:
            existing_trade_headers = trades_ws.row_values(1)
            if existing_trade_headers != _TRADES_HEADERS:
                trades_ws.update(
                    range_name="A1",
                    values=[_TRADES_HEADERS],
                    value_input_option="USER_ENTERED",
                )
                log("Ensured 'Trades' header row in Google Sheets.")
        except Exception as header_err:
            log(f"Warning: Could not verify/set Trades headers: {header_err}")
        
        # Protect header row (row 1) so it cannot be edited.
        try:
            trades_ws = _gsheet.worksheet("Trades")
            trades_ws.protect_range("A1:Q1", editor_users_can_edit=False, warning_only=False)
            log("Protected Trades header row (cannot be edited).")
        except Exception as protect_err:
            log(f"Warning: Could not protect header row: {protect_err}")

    except Exception as e:
        log(
            f"Google Sheets init failed at '{step}': {type(e).__name__}: {e!r} "
            "— sheet logging disabled."
        )
        if GOOGLE_SPREADSHEET_ID:
            log(
                "Sheets hint: ensure this service account has Editor access to the target sheet "
                f"and Drive API is enabled. service_account={GOOGLE_SERVICE_ACCOUNT_EMAIL} "
                f"sheet_id={GOOGLE_SPREADSHEET_ID}"
            )
        _gsheet = None


def ensure_google_sheets_ready():
    """Retry Google Sheets init periodically if it is currently unavailable."""
    if _gsheet is not None:
        return True
    if not GOOGLE_SERVICE_ACCOUNT_EMAIL or not GOOGLE_PRIVATE_KEY:
        return False

    now = datetime.now(central)
    if _last_gsheet_init_attempt is not None:
        elapsed = (now - _last_gsheet_init_attempt).total_seconds()
        if elapsed < GSHEET_RETRY_SECONDS:
            return False

    log("Google Sheets unavailable — retrying initialization.")
    init_google_sheets()
    return _gsheet is not None


def log_alert_to_sheets(symbol, data, option):
    """Append one row to the Alerts tab for every STRONG signal that fires."""
    if _gsheet is None and not ensure_google_sheets_ready():
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


def log_trade_open_to_sheets(trade):
    """Write a row to the Trades tab the moment a paper trade opens."""
    if _gsheet is None and not ensure_google_sheets_ready():
        log(f"[{trade.get('underlying', 'UNKNOWN')}] Trades write skipped: Google Sheets not initialized.")
        return
    try:
        trade_id = str(uuid.uuid4())[:8].upper()
        trade["trade_id"] = trade_id
        opened = trade["opened_at"]
        
        sheet_row = [
            trade_id,                                           # Trade ID
            trade["underlying"],                                # Symbol
            round(trade["entry"], 4),                           # Entry Price
            "",                                                 # Exit Price
            trade.get("strike", ""),                            # Strike
            trade["signal"],                                    # Direction (CALL/PUT)
            opened.strftime("%Y-%m-%d %H:%M:%S") if isinstance(opened, datetime) else str(opened),  # Entry Time
            "",                                                 # Exit Time
            "",                                                 # Exit Reason
            "OPEN",                                             # Status
            trade.get("expiry", ""),                            # Options Expiration
            trade.get("order_id", ""),                          # Alpaca Order ID
            "",                                                 # P&L ($)
            "",                                                 # P&L %
            "",                                                 # Duration
            opened.strftime("%Y-%m-%d %H:%M:%S") if isinstance(opened, datetime) else str(opened),  # Created At
            opened.strftime("%Y-%m-%d %H:%M:%S") if isinstance(opened, datetime) else str(opened),  # Updated At
        ]
        ws = _gsheet.worksheet("Trades")
        ws.append_row(sheet_row, value_input_option="USER_ENTERED")
        # Store the row index so close can update it in-place.
        trade["sheets_row"] = len(ws.get_all_values())
        log(f"[{trade['underlying']}] Trade {trade_id} opened → Google Sheets row {trade['sheets_row']}.")
    except Exception as e:
        log(f"[{trade['underlying']}] Google Sheets open log failed: {e}")


def log_trade_to_sheets(row, trade):
    """Update the existing Trades row (written at open) with exit details."""
    if _gsheet is None and not ensure_google_sheets_ready():
        log(f"[{row.get('underlying', 'UNKNOWN')}] Trades close write skipped: Google Sheets not initialized.")
        return
    try:
        opened = row["opened_at"]
        closed = row["closed_at"]
        duration = (
            round((closed - opened).total_seconds() / 60, 1)
            if isinstance(opened, datetime) and isinstance(closed, datetime)
            else ""
        )
        pnl_dollar = round((row["exit"] - row["entry"]) * 100, 2)
        pnl_pct = round(row["pnl_pct"], 2)
        trade_id = trade.get("trade_id", "")
        now = datetime.now(central).strftime("%Y-%m-%d %H:%M:%S")

        full_row = [
            trade_id,                                           # Trade ID
            row["underlying"],                                  # Symbol
            round(row["entry"], 4),                             # Entry Price
            round(row["exit"],  4),                             # Exit Price
            trade.get("strike", ""),                            # Strike
            row["signal"],                                      # Direction (CALL/PUT)
            opened.strftime("%Y-%m-%d %H:%M:%S") if isinstance(opened, datetime) else str(opened),  # Entry Time
            closed.strftime("%Y-%m-%d %H:%M:%S") if isinstance(closed, datetime) else str(closed),  # Exit Time
            row["reason"],                                      # Exit Reason
            "CLOSED",                                           # Status
            trade.get("expiry", ""),                            # Options Expiration
            trade.get("order_id", ""),                          # Alpaca Order ID
            pnl_dollar,                                         # P&L ($)
            pnl_pct,                                            # P&L %
            duration,                                           # Duration (min)
            opened.strftime("%Y-%m-%d %H:%M:%S") if isinstance(opened, datetime) else str(opened),  # Created At
            now,                                                # Updated At
        ]

        ws = _gsheet.worksheet("Trades")
        sheets_row = trade.get("sheets_row")
        if sheets_row:
            ws.update(
                range_name=f"A{sheets_row}",
                values=[full_row],
                value_input_option="USER_ENTERED",
            )
            log(f"[{trade_id}] Trade closed → Google Sheets row {sheets_row} updated.")
        else:
            # Fallback: no open-row was stored, just append.
            ws.append_row(full_row, value_input_option="USER_ENTERED")
            log(f"[{trade_id}] Trade closed → Google Sheets appended (no open row ref).")
    except Exception as e:
        log(f"[{row['underlying']}] Google Sheets trade close update failed: {e}")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
DISCORD_COLOR_CALL = 0x2ECC71
DISCORD_COLOR_PUT = 0xE74C3C
DISCORD_COLOR_WARN = 0xF1C40F


def send_discord(message, color=None):
    if not DISCORD_WEBHOOK_URL:
        print("Missing Discord webhook.")
        return
    payload = None
    if color is None:
        if len(message) > 1990:
            message = message[:1987] + "..."
        payload = {"content": message}
    else:
        if len(message) > 4000:
            message = message[:3997] + "..."
        payload = {
            "embeds": [
                {
                    "description": message,
                    "color": int(color),
                }
            ]
        }
    try:
        r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
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


def opening_no_trade_minutes_remaining(now=None):
    """Return minutes remaining in the post-open no-trade window."""
    if OPENING_NO_TRADE_MINUTES <= 0:
        return 0

    now = now or datetime.now(central)
    if now.weekday() >= 5:
        return 0

    market_open = now.replace(hour=8, minute=30, second=0, microsecond=0)
    market_resume = market_open + timedelta(minutes=OPENING_NO_TRADE_MINUTES)

    if now < market_open or now >= market_resume:
        return 0

    return max(1, int((market_resume - now).total_seconds() // 60))


def closing_no_trade_minutes_remaining(now=None):
    """Return minutes remaining before session end when new entries are blocked."""
    if CLOSING_NO_TRADE_MINUTES <= 0:
        return 0

    now = now or datetime.now(central)
    if now.weekday() >= 5:
        return 0

    market_close = now.replace(hour=14, minute=55, second=0, microsecond=0)
    block_start = market_close - timedelta(minutes=CLOSING_NO_TRADE_MINUTES)

    if now < block_start or now >= market_close:
        return 0

    return max(1, int((market_close - now).total_seconds() // 60))


def _spy_vwap_side():
    """Return the current SPY macro side: 'bull' if SPY > VWAP, 'bear' if SPY < VWAP.

    Uses the cached value written by run_symbol('SPY', ...) each cycle.
    Returns None if SPY hasn't been evaluated yet this cycle.
    """
    return _spy_vwap_cache.get("side")


def symbol_profile(symbol):
    """Return per-symbol entry tuning so single stocks use stricter filters than ETFs."""
    profile = {
        "ignition_min_delta": IGNITION_MIN_DELTA,
        "rsi_overbought": RSI_OVERBOUGHT,
        "rsi_oversold": RSI_OVERSOLD,
        "max_ext_from_vwap": MAX_EXT_FROM_VWAP,
    }
    if symbol in ETF_SYMBOLS:
        return profile
    if symbol in AGGRESSIVE_STOCK_SYMBOLS:
        profile.update({
            "ignition_min_delta": IGNITION_MIN_DELTA + 10,
            "rsi_overbought": max(55, RSI_OVERBOUGHT - 2),
            "rsi_oversold": min(45, RSI_OVERSOLD + 2),
            "max_ext_from_vwap": min(MAX_EXT_FROM_VWAP, 0.009),
        })
        return profile
    if symbol in TOP_STOCK_SYMBOLS:
        profile.update({
            "ignition_min_delta": IGNITION_MIN_DELTA + 5,
            "rsi_overbought": max(55, RSI_OVERBOUGHT - 1),
            "rsi_oversold": min(45, RSI_OVERSOLD + 1),
            "max_ext_from_vwap": min(MAX_EXT_FROM_VWAP, 0.010),
        })
    return profile


def position_qty_from_score(score, dominance=0):
    """Return position size based on score and confidence dominance."""
    base_qty = max(MIN_POSITION_QTY, min(MAX_POSITION_QTY, BASE_POSITION_QTY))
    if not CONFIDENCE_POSITIONING:
        return base_qty

    step = max(1, CONFIDENCE_STEP_SCORE)
    score_boost = max(0, (int(score) - SCORE_STRONG) // step)
    confidence_boost = max(0, (int(dominance) - SCORE_DOMINANCE) // max(10, step * 2))
    extra_steps = score_boost + confidence_boost
    return max(base_qty, min(MAX_POSITION_QTY, base_qty + extra_steps))


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


def calculate_rsi(series, period=14):
    """Wilder RSI on a price series."""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, float("nan"))
    return 100 - (100 / (1 + rs))


def calculate_indicators(df):
    df = df.copy()
    df["EMA20"] = df["close"].ewm(span=20, adjust=False).mean()
    df["EMA50"] = df["close"].ewm(span=50, adjust=False).mean()
    df["VOL_AVG"] = df["volume"].rolling(20).mean()
    df["RSI14"] = calculate_rsi(df["close"], period=14)

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
    """Compute weighted Bull/Bear scores (0-100) from independent factor groups.

    Factor weights:
        Trend Score       : 25%
        Momentum Score    : 20%
        Volume Score      : 15%
        Market Regime     : 15%
        Relative Strength : 10%
        Pattern Quality   : 10%
        Option Liquidity  : 5% (underlying liquidity proxy pre-contract)
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

    rsi = float(latest["RSI14"]) if not pd.isna(latest["RSI14"]) else 50.0

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

    is_etf = symbol in ETF_SYMBOLS

    if is_etf:
        # ETFs keep the legacy scoring model unchanged.
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

        trend_bull = trend_bear = 0.0
        momentum_bull = momentum_bear = 0.0
        volume_bull = volume_bear = 0.0
        regime_bull = regime_bear = 0.0
        rel_bull = rel_bear = 0.0
        pattern_bull = pattern_bear = 0.0
        option_liq_score = 0.0
    else:
        def _clamp01(x):
            return max(0.0, min(1.0, float(x)))

        def _to_100(x):
            return _clamp01(x) * 100.0

        # Short-term return proxy for momentum.
        close_5 = float(df["close"].iloc[-6]) if len(df) >= 6 else price
        ret_5 = ((price / close_5) - 1.0) if close_5 > 0 else 0.0

        # Underlying liquidity proxy used before contract lookup.
        dollar_vol_now = price * volume
        dv_series = (df["close"] * df["volume"]).rolling(20).mean()
        dollar_vol_avg = float(dv_series.iloc[-1]) if not pd.isna(dv_series.iloc[-1]) else 0.0
        liq_ratio = (dollar_vol_now / dollar_vol_avg) if dollar_vol_avg > 0 else 1.0

        # Market regime from SPY VWAP side cache (or local SPY side when symbol is SPY).
        if symbol == "SPY":
            regime_side = "bull" if price > vwap else "bear"
        else:
            regime_side = _spy_vwap_side()

        # ---------------- Category scores (0-100 each side) ----------------
        trend_bull = _to_100((
            (1.0 if price > vwap else 0.0)
            + (1.0 if price > ema20 else 0.0)
            + (1.0 if price > ema50 else 0.0)
            + (1.0 if ema20_rising else 0.0)
        ) / 4.0)
        trend_bear = _to_100((
            (1.0 if price < vwap else 0.0)
            + (1.0 if price < ema20 else 0.0)
            + (1.0 if price < ema50 else 0.0)
            + (1.0 if ema20_falling else 0.0)
        ) / 4.0)

        momentum_bull = _to_100((
            (_clamp01((rsi - 50.0) / 20.0)) * 0.45
            + (_clamp01(ret_5 / 0.01)) * 0.35
            + ((1.0 if moving_away_bullish else 0.0) * 0.20)
        ))
        momentum_bear = _to_100((
            (_clamp01((50.0 - rsi) / 20.0)) * 0.45
            + (_clamp01((-ret_5) / 0.01)) * 0.35
            + ((1.0 if moving_away_bearish else 0.0) * 0.20)
        ))

        vol_ratio = volume / vol_avg if vol_avg > 0 else 1.0
        volume_bull = _to_100((
            (_clamp01((vol_ratio - 1.0) / 1.0)) * 0.50
            + ((1.0 if bullish_candle else 0.0) * 0.25)
            + ((1.0 if strong_volume else 0.0) * 0.25)
        ))
        volume_bear = _to_100((
            (_clamp01((vol_ratio - 1.0) / 1.0)) * 0.50
            + ((1.0 if bearish_candle else 0.0) * 0.25)
            + ((1.0 if strong_volume else 0.0) * 0.25)
        ))

        if regime_side == "bull":
            regime_bull, regime_bear = 100.0, 0.0
        elif regime_side == "bear":
            regime_bull, regime_bear = 0.0, 100.0
        else:
            regime_bull, regime_bear = 50.0, 50.0

        rel_bull = _to_100((
            ((1.0 if price > pdh else 0.0) * 0.50)
            + ((1.0 if price > recent_high else 0.0) * 0.30)
            + ((1.0 if price > ema50 else 0.0) * 0.20)
        ))
        rel_bear = _to_100((
            ((1.0 if price < pdl else 0.0) * 0.50)
            + ((1.0 if price < recent_low else 0.0) * 0.30)
            + ((1.0 if price < ema50 else 0.0) * 0.20)
        ))

        pattern_bull = _to_100((
            ((1.0 if bullish_candle else 0.0) * 0.35)
            + ((1.0 if price > recent_high else 0.0) * 0.35)
            + ((1.0 if moving_away_bullish else 0.0) * 0.30)
        ))
        pattern_bear = _to_100((
            ((1.0 if bearish_candle else 0.0) * 0.35)
            + ((1.0 if price < recent_low else 0.0) * 0.35)
            + ((1.0 if moving_away_bearish else 0.0) * 0.30)
        ))

        option_liq_score = _to_100(_clamp01((liq_ratio - 0.5) / 1.0))

        # ---------------- Weighted final score (0-100) ----------------
        bull_score_f = (
            trend_bull * 0.25
            + momentum_bull * 0.20
            + volume_bull * 0.15
            + regime_bull * 0.15
            + rel_bull * 0.10
            + pattern_bull * 0.10
            + option_liq_score * 0.05
        )
        bear_score_f = (
            trend_bear * 0.25
            + momentum_bear * 0.20
            + volume_bear * 0.15
            + regime_bear * 0.15
            + rel_bear * 0.10
            + pattern_bear * 0.10
            + option_liq_score * 0.05
        )

        bull_score = int(round(bull_score_f))
        bear_score = int(round(bear_score_f))

        bull_breakdown = {
            "Trend Score (25%)": round(trend_bull * 0.25, 1),
            "Momentum Score (20%)": round(momentum_bull * 0.20, 1),
            "Volume Score (15%)": round(volume_bull * 0.15, 1),
            "Market Regime (15%)": round(regime_bull * 0.15, 1),
            "Relative Strength (10%)": round(rel_bull * 0.10, 1),
            "Pattern Quality (10%)": round(pattern_bull * 0.10, 1),
            "Option Liquidity (5%)": round(option_liq_score * 0.05, 1),
        }
        bear_breakdown = {
            "Trend Score (25%)": round(trend_bear * 0.25, 1),
            "Momentum Score (20%)": round(momentum_bear * 0.20, 1),
            "Volume Score (15%)": round(volume_bear * 0.15, 1),
            "Market Regime (15%)": round(regime_bear * 0.15, 1),
            "Relative Strength (10%)": round(rel_bear * 0.10, 1),
            "Pattern Quality (10%)": round(pattern_bear * 0.10, 1),
            "Option Liquidity (5%)": round(option_liq_score * 0.05, 1),
        }

    # ---------------- Decision ----------------
    # Keep label thresholds aligned with downstream hard-entry gates.
    strong_threshold = SCORE_STRONG if is_etf else (SCORE_STRONG + max(0, STOCK_STRONG_SCORE_BONUS))
    # The dominant side must lead by SCORE_DOMINANCE points; otherwise NO TRADE.
    diff = bull_score - bear_score

    if bull_score >= strong_threshold and diff >= SCORE_DOMINANCE:
        side, score, tier, signal = "CALL", bull_score, "STRONG", "STRONG CALL"
    elif bear_score >= strong_threshold and -diff >= SCORE_DOMINANCE:
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
        "open": open_,
        "vwap": vwap,
        "ema20": ema20,
        "ema50": ema50,
        "rsi": rsi,
        "volume": volume,
        "vol_avg": vol_avg,
        "pdh": pdh,
        "pdl": pdl,
        "recent_high": recent_high,
        "recent_low": recent_low,
        "vwap_distance_now": vwap_distance_now,
        "vwap_distance_prev": vwap_distance_prev,
        "vwap_extension_pct": (abs(vwap_distance_now) / vwap) if vwap else 0.0,
        "bullish_candle": bullish_candle,
        "bearish_candle": bearish_candle,
        "bull_score": bull_score,
        "bear_score": bear_score,
        "bull_breakdown": bull_breakdown,
        "bear_breakdown": bear_breakdown,
        "trend_score_bull": trend_bull,
        "trend_score_bear": trend_bear,
        "momentum_score_bull": momentum_bull,
        "momentum_score_bear": momentum_bear,
        "volume_score_bull": volume_bull,
        "volume_score_bear": volume_bear,
        "market_regime_score_bull": regime_bull,
        "market_regime_score_bear": regime_bear,
        "relative_strength_score_bull": rel_bull,
        "relative_strength_score_bear": rel_bear,
        "pattern_quality_score_bull": pattern_bull,
        "pattern_quality_score_bear": pattern_bear,
        "option_liquidity_score": option_liq_score,
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
    """Fetch the nearest available >=MIN_DTE option contract from Alpaca (live data, no yfinance)."""
    if _option_client is None or _trading_client is None:
        print(f"[{symbol}] Option/trading client not initialised — cannot fetch contracts.", flush=True)
        return None
    try:
        today = date.today()
        min_exp = today + timedelta(days=MIN_DTE)
        max_exp = today + timedelta(days=MAX_DTE) if MAX_DTE > 0 else None
        option_type = "call" if signal == "CALL" else "put"

        req_kwargs = {
            "underlying_symbols": [symbol],
            "expiration_date_gte": min_exp,
            "type": option_type,
            "strike_price_gte": str(round(underlying_price * 0.95, 2)),
            "strike_price_lte": str(round(underlying_price * 1.05, 2)),
            "limit": 50,
        }
        if max_exp is not None:
            req_kwargs["expiration_date_lte"] = max_exp

        req = GetOptionContractsRequest(**req_kwargs)
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
        exp_range = f"{min_exp}–{max_exp}" if max_exp is not None else f">={min_exp}"
        if not contracts:
            print(f"[{symbol}] No option contracts found ({option_type}, {exp_range}).", flush=True)
            return None

        print(f"[{symbol}] {len(contracts)} contract(s) returned by Alpaca for {option_type} {exp_range}.", flush=True)

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

        # ── Quality filters — stock names use tighter option liquidity checks ──
        is_etf = symbol in ETF_SYMBOLS
        min_bid = ETF_MIN_OPTION_BID if is_etf else STOCK_MIN_OPTION_BID
        max_spread_pct = ETF_MAX_OPTION_SPREAD_PCT if is_etf else STOCK_MAX_OPTION_SPREAD_PCT

        rejection_total = 0
        rejection_logged = 0
        max_rejection_logs = 5

        def _reject(msg):
            nonlocal rejection_total, rejection_logged
            rejection_total += 1
            if rejection_logged < max_rejection_logs:
                print(msg, flush=True)
                rejection_logged += 1

        for candidate in contracts:
            contract_sym = candidate.symbol
            exp_date = _exp_date(candidate)

            # Fetch live bid/ask/last/volume for each candidate.
            snap_req = OptionSnapshotRequest(symbol_or_symbols=contract_sym, feed=OPTIONS_FEED)
            snaps = _option_client.get_option_snapshot(snap_req)
            snap = snaps.get(contract_sym) if isinstance(snaps, dict) else snaps

            bid = ask = last = 0.0
            vol = 0
            oi = _safe_int(getattr(candidate, "open_interest", 0), 0)
            if snap:
                q = getattr(snap, "latest_quote", None) or getattr(snap, "quote", None)
                t = getattr(snap, "latest_trade", None) or getattr(snap, "trade", None)

                if q is not None:
                    bid = _safe_float(getattr(q, "bid_price", 0.0), 0.0)
                    ask = _safe_float(getattr(q, "ask_price", 0.0), 0.0)
                if t is not None:
                    last = _safe_float(getattr(t, "price", 0.0), 0.0)
                    # Snapshot trade size is not true daily volume, but we keep it for telemetry.
                    vol = _safe_int(getattr(t, "size", 0), 0)

            dte = (exp_date - today).days

            if bid < min_bid:
                _reject(f"[{symbol}] Contract {contract_sym} rejected — bid ${bid:.2f} < ${min_bid:.2f} minimum.")
                continue

            if not is_etf:
                # In strict mode, accept stock contracts when either intraday volume OR OI passes minimum.
                # In no-gating mode, bypass this liquidity gate and rely on bid/spread checks only.
                if (not NO_GATING_MODE) and vol < STOCK_MIN_OPTION_VOLUME and oi < STOCK_MIN_OPTION_OPEN_INTEREST:
                    _reject(
                        f"[{symbol}] Contract {contract_sym} rejected — volume {vol} < {STOCK_MIN_OPTION_VOLUME} "
                        f"and OI {oi} < {STOCK_MIN_OPTION_OPEN_INTEREST}."
                    )
                    continue
                if NO_GATING_MODE and vol < STOCK_MIN_OPTION_VOLUME and oi < STOCK_MIN_OPTION_OPEN_INTEREST:
                    print(
                        f"[{symbol}] No-gating override: accepting {contract_sym} despite low volume/OI "
                        f"(vol={vol}, oi={oi}).",
                        flush=True,
                    )

            mid = (bid + ask) / 2 if (bid + ask) > 0 else 0.01
            spread_pct = (ask - bid) / mid
            if spread_pct > max_spread_pct:
                _reject(
                    f"[{symbol}] Contract {contract_sym} rejected — spread {spread_pct*100:.1f}% > {max_spread_pct*100:.0f}% max."
                )
                continue

            print(
                f"[{symbol}] Selected contract: {contract_sym}  strike={candidate.strike_price}  "
                f"expiry={exp_date}  bid={bid:.2f} ask={ask:.2f} vol={vol} oi={oi}",
                flush=True,
            )
            return {
                "contract":      contract_sym,
                "expiry":        exp_date.strftime("%Y-%m-%d"),
                "dte":           dte,
                "strike":        float(candidate.strike_price),
                "bid":           bid,
                "ask":           ask,
                "last":          last,
                "volume":        vol,
                "open_interest": oi,
                "side":          signal,  # stored so close_trade can build the alert_key
            }

        if rejection_total > rejection_logged:
            print(f"[{symbol}] ... {rejection_total - rejection_logged} additional contract rejection(s) omitted.", flush=True)
        print(f"[{symbol}] No contracts passed quality filters ({option_type}, {exp_range}).", flush=True)
        return None
    except Exception as e:
        msg = f"[{symbol}] Option contract fetch failed: {type(e).__name__}: {e}"
        print(msg, flush=True)
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


def _alpaca_data_headers():
    return {
        "APCA-API-KEY-ID": ALPACA_API_KEY or "",
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY or "",
    }


def _alpaca_data_get_json(path, params=None, timeout=8):
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        return None
    url = f"{ALPACA_DATA_BASE_URL.rstrip('/')}{path}"
    try:
        r = requests.get(url, headers=_alpaca_data_headers(), params=params or {}, timeout=timeout)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def _safe_float_num(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return default


def _extract_screener_items(payload):
    """Extract a flat list of symbol-like dicts from Alpaca screener payload variants."""
    if payload is None:
        return []
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []

    items = []
    for k in ("gainers", "losers", "most_actives", "actives", "data", "results", "stocks"):
        v = payload.get(k)
        if isinstance(v, list):
            items.extend([x for x in v if isinstance(x, dict)])

    if not items:
        # Some responses may already be a single stock-like object.
        if payload.get("symbol") or payload.get("ticker"):
            items.append(payload)
    return items


def _fetch_trending_news_reason(symbol):
    """Return short headline-based reason from Alpaca news for one symbol."""
    if TRENDING_NEWS_HEADLINES <= 0:
        return "No headline context requested"

    payload = _alpaca_data_get_json(
        "/v1beta1/news",
        params={"symbols": symbol, "limit": TRENDING_NEWS_HEADLINES, "sort": "desc"},
        timeout=8,
    )
    if not payload:
        return "No recent Alpaca news"

    articles = payload.get("news") if isinstance(payload, dict) else None
    if not isinstance(articles, list):
        articles = payload.get("articles") if isinstance(payload, dict) else None
    if not isinstance(articles, list) or not articles:
        return "No recent Alpaca news"

    titles = []
    for art in articles[:TRENDING_NEWS_HEADLINES]:
        title = str((art or {}).get("headline") or (art or {}).get("title") or "").strip()
        if title:
            titles.append(title)
    return " | ".join(titles) if titles else "No recent Alpaca news"


def _classify_news_impact(text):
    """Heuristic impact label from headline text."""
    txt = str(text or "").lower()
    if not txt:
        return "LOW", "no recent headlines"

    high_markers = (
        "earnings", "guidance", "downgrade", "upgrade", "sec", "lawsuit",
        "investigation", "fda", "bankruptcy", "acquisition", "merger",
        "offering", "buyback", "ceo", "cfo", "forecast",
    )
    medium_markers = (
        "analyst", "target", "price target", "partnership", "contract",
        "launch", "product", "supply", "shortage", "beat", "miss",
    )

    for marker in high_markers:
        if marker in txt:
            return "HIGH", marker
    for marker in medium_markers:
        if marker in txt:
            return "MEDIUM", marker
    return "LOW", "headline flow"


def _trim_text(value, max_len=220):
    text = str(value or "").replace("`", "'").strip()
    if len(text) <= max_len:
        return text
    return text[: max(0, max_len - 3)] + "..."


def _get_symbol_news_context(symbol):
    """Return cached latest-news + trending context for one symbol."""
    if not ENABLE_SYMBOL_NEWS_CONTEXT or SYMBOL_NEWS_HEADLINES <= 0:
        return {
            "latest_news": "News context disabled",
            "trending_news": "",
            "impact_label": "LOW",
            "impact_reason": "disabled",
        }

    now = datetime.now(timezone.utc)
    updated = _symbol_news_cache.get("updated_at", {}).get(symbol)
    if updated and (now - updated).total_seconds() < max(30, SYMBOL_NEWS_REFRESH_SECONDS):
        cached = _symbol_news_cache.get("items", {}).get(symbol)
        if cached:
            return dict(cached)

    payload = _alpaca_data_get_json(
        "/v1beta1/news",
        params={"symbols": symbol, "limit": SYMBOL_NEWS_HEADLINES, "sort": "desc"},
        timeout=8,
    )
    latest_news = "No recent Alpaca news"
    if payload:
        articles = payload.get("news") if isinstance(payload, dict) else None
        if not isinstance(articles, list):
            articles = payload.get("articles") if isinstance(payload, dict) else None
        if isinstance(articles, list) and articles:
            titles = []
            for art in articles[:SYMBOL_NEWS_HEADLINES]:
                title = str((art or {}).get("headline") or (art or {}).get("title") or "").strip()
                if title:
                    titles.append(title)
            if titles:
                latest_news = " | ".join(titles)

    trending_news = str(_trending_cache.get("reasons", {}).get(symbol, "") or "")
    combined_text = f"{latest_news} {trending_news}".strip()
    impact_label, impact_reason = _classify_news_impact(combined_text)

    item = {
        "latest_news": latest_news,
        "trending_news": trending_news,
        "impact_label": impact_label,
        "impact_reason": impact_reason,
    }
    _symbol_news_cache.setdefault("updated_at", {})[symbol] = now
    _symbol_news_cache.setdefault("items", {})[symbol] = item
    return dict(item)


def _is_valid_trending_symbol(sym):
    """Basic filter to keep trending universe to common-stock-like tickers."""
    if not re.fullmatch(r"[A-Z]{1,6}", sym or ""):
        return False
    if TRENDING_EXCLUDE_WARRANTS:
        upper = (sym or "").upper()
        # Common warrant/right suffixes from screener feeds.
        suffixes = ("W", "WS", "WT", "WTS", "R", "RT")
        if len(upper) > 1 and upper.endswith(suffixes):
            return False
    return True


def _is_stock_like_trending_candidate(sym):
    """Best-effort stock-only gate for screener symbols."""
    upper = str(sym or "").upper().strip()
    if not upper:
        return False
    if upper in TRENDING_EXCLUDE_SYMBOLS:
        return False

    cached = _trending_asset_cache.get(upper)
    if cached is not None:
        return bool(cached)

    if _trading_client is None:
        return True

    allowed = True
    try:
        asset = _trading_client.get_asset(upper)
        if hasattr(asset, "tradable") and not bool(getattr(asset, "tradable", True)):
            allowed = False
        name = str(getattr(asset, "name", "") or "").lower()
        if name:
            etf_markers = (
                "etf", "exchange traded fund", "fund", "trust", "index", "proshares",
                "ishares", "invesco", "direxion", "vanguard", "spdr",
            )
            if any(marker in name for marker in etf_markers):
                allowed = False
    except Exception:
        allowed = True

    _trending_asset_cache[upper] = allowed
    return allowed


def get_trending_symbols(client, base_symbols):
    """Get trending stock symbols from Alpaca screener, with Alpaca-bars fallback.

    Returns (symbols, reasons) where reasons explains why each symbol is trending.
    """
    if not ENABLE_TRENDING_STOCKS or TRENDING_STOCK_COUNT <= 0:
        return [], {}

    now = datetime.now(timezone.utc)
    updated_at = _trending_cache.get("updated_at")
    if updated_at and (now - updated_at).total_seconds() < max(30, TRENDING_REFRESH_SECONDS):
        return list(_trending_cache.get("symbols", [])), dict(_trending_cache.get("reasons", {}))

    base_set = {s.upper() for s in base_symbols}

    ranked = {}
    screener_sources = [
        ("/v1beta1/screener/stocks/movers", {"top": max(10, TRENDING_STOCK_COUNT * 4)}),
        ("/v1beta1/screener/stocks/most-actives", {"top": max(10, TRENDING_STOCK_COUNT * 4)}),
    ]

    for path, params in screener_sources:
        payload = _alpaca_data_get_json(path, params=params, timeout=8)
        items = _extract_screener_items(payload)
        for idx, item in enumerate(items):
            sym = str(item.get("symbol") or item.get("ticker") or "").upper().strip()
            if not sym or sym in ETF_SYMBOLS or sym in base_set:
                continue
            if not _is_valid_trending_symbol(sym):
                continue
            if not _is_stock_like_trending_candidate(sym):
                continue

            # Validate real-time tradability characteristics via recent bars.
            try:
                bars = fetch_bars(client, sym)
                if len(bars) < TRENDING_MIN_BAR_COUNT:
                    continue
                last_close = float(bars["close"].iloc[-1])
                last_volume = int(float(bars["volume"].iloc[-1]))
                if last_close < TRENDING_MIN_PRICE or last_volume < TRENDING_MIN_LAST_VOLUME:
                    continue
            except Exception:
                continue

            pct = _safe_float_num(
                item.get("percent_change", item.get("change_percent", item.get("change", 0.0))),
                0.0,
            )
            vol = _safe_float_num(item.get("volume", 0.0), 0.0)
            rank_bonus = max(0.0, 40.0 - (idx * 2.0))
            score = (abs(pct) * 6.0) + min(vol / 1_000_000.0, 20.0) + rank_bonus
            reason = f"screener pct={pct:+.2f}% vol={int(vol)}"

            prev = ranked.get(sym)
            if (prev is None) or (score > prev[0]):
                ranked[sym] = (score, reason)

    # Fallback: derive trend candidates from Alpaca bars for top liquid stocks.
    if not ranked:
        for sym in sorted(TOP_STOCK_SYMBOLS):
            if sym in ETF_SYMBOLS or sym in base_set:
                continue
            try:
                bars = fetch_bars(client, sym)
                if len(bars) < 25:
                    continue
                c0 = float(bars["close"].iloc[-1])
                c5 = float(bars["close"].iloc[-6]) if len(bars) >= 6 else c0
                v0 = float(bars["volume"].iloc[-1])
                vavg = float(bars["volume"].tail(20).mean())
                ret5 = ((c0 / c5) - 1.0) * 100.0 if c5 > 0 else 0.0
                vr = (v0 / vavg) if vavg > 0 else 1.0
                score = abs(ret5) * 5.0 + min(max(vr - 1.0, 0.0), 4.0) * 10.0
                ranked[sym] = (score, f"bars 5m={ret5:+.2f}% volx={vr:.2f}")
            except Exception:
                continue

    ordered = sorted(ranked.items(), key=lambda kv: kv[1][0], reverse=True)
    selected = [sym for sym, _ in ordered[:TRENDING_STOCK_COUNT]]

    reasons = {}
    for sym in selected:
        base_reason = ranked.get(sym, (0.0, ""))[1]
        news_reason = _fetch_trending_news_reason(sym)
        reasons[sym] = f"{base_reason}; news: {news_reason}" if news_reason else base_reason

    _trending_cache["updated_at"] = now
    _trending_cache["symbols"] = selected
    _trending_cache["reasons"] = reasons

    if selected:
        log(f"Trending stocks from Alpaca: {', '.join(selected)}")
        for sym in selected:
            why = reasons.get(sym, "")
            if why:
                log(f"[TRENDING] {sym} — {why}")

    return selected, reasons


def log(msg):
    print(f"[{datetime.now(central):%Y-%m-%d %H:%M:%S} CT] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Paper-trading execution + position tracking
# ---------------------------------------------------------------------------
def place_paper_entry(option_contract, qty):
    """Submit a paper BUY and poll up to 5s for the actual fill price.

    Returns (order, fill_price).  fill_price is None if the order did not fill
    within the polling window — the caller must treat this as a failure.
    """
    order_req = MarketOrderRequest(
        symbol=option_contract["contract"],
        qty=qty,
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


def place_paper_exit(contract_symbol, qty):
    """Submit a paper SELL and poll up to 5s for the actual fill price.

    Returns (order, fill_price).  fill_price is None if not filled in time.
    Market SELL fills at the bid (not mid), so this is the real exit price.
    """
    order_req = MarketOrderRequest(
        symbol=contract_symbol,
        qty=qty,
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


def open_trade_record(symbol, signal, option, score, fill_price, qty):
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
        "qty":        int(qty),
        "target":     entry_price * (1 + PROFIT_TARGET_PCT),
        "stop":       entry_price * (1 - STOP_LOSS_PCT),
        "score":      score,
        "max_pnl_pct": 0.0,
        "opened_at":  datetime.now(central),
        "status":     "OPEN",
    }


def _option_side_from_contract(contract_symbol):
    """Infer CALL/PUT from an OCC option symbol; returns None if unknown."""
    m = re.search(r"([CP])\d{8}$", str(contract_symbol or ""))
    if not m:
        return None
    return "CALL" if m.group(1) == "C" else "PUT"


def _underlying_from_contract(contract_symbol):
    """Infer underlying ticker from OCC option symbol; returns None if unknown."""
    m = re.match(r"^([A-Z]+)\d{6}[CP]\d{8}$", str(contract_symbol or ""))
    return m.group(1) if m else None


def _strike_from_contract(contract_symbol):
    """Infer strike from OCC option symbol; returns None if unknown."""
    m = re.match(r"^[A-Z]+\d{6}[CP](\d{8})$", str(contract_symbol or ""))
    if not m:
        return None
    try:
        return int(m.group(1)) / 1000.0
    except Exception:
        return None


def _is_option_asset_class(asset_class_val):
    """Return True when Alpaca asset_class represents an option position."""
    if asset_class_val is None:
        return False

    raw = str(asset_class_val).strip().lower()
    if not raw:
        return False

    # Handles forms like: 'us_option', 'option', 'options', 'AssetClass.US_OPTION'
    token = raw.split(".")[-1]
    return token in ("option", "options", "us_option")


def sync_open_trades_from_alpaca():
    """Mirror _open_trades from live Alpaca option positions.

    Alpaca is the source of truth: each sync rebuilds local tracking from the
    current broker snapshot, preserving only local metadata when available.
    """
    if _trading_client is None:
        return

    recovered = 0
    scanned_total = 0
    scanned_option = 0
    current_positions = []
    previous = dict(_open_trades)
    try:
        for pos in _trading_client.get_all_positions():
            scanned_total += 1
            contract_sym = str(getattr(pos, "symbol", "") or "")
            if not contract_sym:
                continue

            asset_class = getattr(pos, "asset_class", None)
            if not _is_option_asset_class(asset_class):
                continue
            scanned_option += 1

            underlying = _underlying_from_contract(contract_sym)
            side = _option_side_from_contract(contract_sym)
            if not underlying or side not in ("CALL", "PUT"):
                continue

            try:
                qty = abs(int(float(getattr(pos, "qty", 0) or 0)))
            except Exception:
                qty = 0
            if qty <= 0:
                continue

            try:
                entry = float(getattr(pos, "avg_entry_price", 0) or 0)
            except Exception:
                entry = 0.0
            if entry <= 0:
                continue

            try:
                current_px = float(getattr(pos, "current_price", 0) or 0)
            except Exception:
                current_px = 0.0

            try:
                strike_val = float(getattr(pos, "strike_price", 0) or 0)
            except Exception:
                strike_val = 0.0
            strike = int(strike_val) if strike_val else 0

            expiry = str(getattr(pos, "expiration_date", "") or "")

            current_positions.append({
                "contract": contract_sym,
                "underlying": underlying,
                "side": side,
                "expiry": expiry,
                "strike": strike,
                "entry": entry,
                "qty": qty,
                "current_price": current_px,
            })
    except Exception as e:
        log(f"Alpaca position recovery failed: {e}")
        return

    # Rebuild local map from broker snapshot (source of truth), preserving metadata.
    _open_trades.clear()
    for p in current_positions:
        contract_sym = p["contract"]
        prev = previous.get(contract_sym)
        if prev is None:
            recovered += 1
        _open_trades[contract_sym] = {
            "underlying": p["underlying"],
            "signal": prev.get("signal", f"RECOVERED {p['side']}") if prev else f"RECOVERED {p['side']}",
            "side": p["side"],
            "contract": contract_sym,
            "expiry": p["expiry"],
            "strike": p["strike"],
            "entry": p["entry"],
            "qty": p["qty"],
            "current_price": p["current_price"],
            "target": p["entry"] * (1 + PROFIT_TARGET_PCT),
            "stop": p["entry"] * (1 - STOP_LOSS_PCT),
            "score": prev.get("score", 0) if prev else 0,
            "max_pnl_pct": prev.get("max_pnl_pct", 0.0) if prev else 0.0,
            "opened_at": prev.get("opened_at", datetime.now(central)) if prev else datetime.now(central),
            "status": "OPEN",
            "trade_id": prev.get("trade_id") if prev else None,
            "sheets_row": prev.get("sheets_row") if prev else None,
        }

    if recovered:
        log(f"Recovered {recovered} open option position(s) from Alpaca for exit tracking.")
    else:
        log(
            f"Alpaca position sync: scanned {scanned_total} total position(s), "
            f"{scanned_option} option position(s), recovered 0 new trade(s), "
            f"tracking {len(_open_trades)} open trade(s)."
        )


def has_open_underlying_position(symbol, side):
    """Return (True, detail) when an open position already exists for symbol+side."""
    if _trading_client is None:
        return False, ""

    try:
        for pos in _trading_client.get_all_positions():
            pos_symbol = str(getattr(pos, "symbol", "") or "")
            if not pos_symbol.startswith(symbol):
                continue

            asset_class = getattr(pos, "asset_class", None)
            if not _is_option_asset_class(asset_class):
                continue

            pos_side = _option_side_from_contract(pos_symbol)
            # If side cannot be inferred, block conservatively to avoid duplicate stacking.
            if pos_side is None or pos_side == side:
                return True, f"alpaca:{pos_symbol}"
    except Exception as e:
        log(f"[{symbol}] Position guard warning: {e}")

    # Also block when there is a pending option BUY order for this symbol+side.
    # This prevents duplicate entries before the first order becomes a position.
    try:
        orders = _trading_client.get_orders()
        for order in orders:
            ord_symbol = str(getattr(order, "symbol", "") or "")
            if not ord_symbol.startswith(symbol):
                continue

            ord_side = str(getattr(order, "side", "") or "").lower()
            if ord_side != "buy":
                continue

            status = str(getattr(order, "status", "") or "").lower()
            if status in ("filled", "canceled", "expired", "rejected"):
                continue

            opt_side = _option_side_from_contract(ord_symbol)
            if opt_side is None or opt_side == side:
                return True, f"pending-order:{ord_symbol}:{status}"
    except Exception as e:
        log(f"[{symbol}] Order guard warning: {e}")

    return False, ""


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
    # Always re-sync with Alpaca so restart/redeploy never leaves exits unmanaged.
    if RECOVER_OPEN_POSITIONS:
        sync_open_trades_from_alpaca()

    if not _open_trades:
        log("Exit monitor: no open trades to check.")
        return

    for trade in list(_open_trades.values()):
        contract_sym = trade["contract"]

        # ── Primary: pull entry + current price directly from Alpaca position ──
        alpaca_entry   = None
        current_price  = float(trade.get("current_price", 0) or 0)
        if current_price <= 0:
            current_price = None

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
        trade["max_pnl_pct"] = max(float(trade.get("max_pnl_pct", 0.0)), pnl_pct)
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
            _, fill_price = place_paper_exit(
                trade["contract"],
                int(trade.get("qty", max(BASE_POSITION_QTY, MIN_POSITION_QTY))),
            )
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
    outcome_label = "PROFIT" if pnl_pct > 0 else "LOSS"
    closed_at = datetime.now(central)
    duration_min = max(1, int((closed_at - trade["opened_at"]).total_seconds() // 60))
    entry_px = float(trade["entry"])
    exit_px = float(exit_price)
    strike_val = float(trade.get("strike", 0) or 0)
    if strike_val <= 0:
        parsed_strike = _strike_from_contract(trade.get("contract", ""))
        if parsed_strike is not None:
            strike_val = float(parsed_strike)
    strike_text = str(int(strike_val)) if abs(strike_val - int(strike_val)) < 1e-9 else f"{strike_val:.2f}"
    exit_header = f"{trade['underlying']} strike {strike_text}"
    exit_type_label = "Profit" if pnl_pct > 0 else "Loss"
    grade = "A" if pnl_pct >= 0.20 else "B" if pnl_pct >= 0.10 else "C" if pnl_pct >= -0.10 else "D"

    send_discord(
        f"\U0001f534 **{exit_type_label} — {exit_header} | {trade['side']} - {pnl_pct * 100:+.2f}%**\n\n"
        f"------------------------------\n"
        f"\U0001f4b2 **Current Price:** `${exit_px:.2f}`\n"
        f"\U0001f4ca **Trade:** `${entry_px:.2f} -> ${exit_px:.2f}` | `{duration_min}m`\n"
        f"\U0001f4c9 **Result:** `{reason}`\n"
        f"\U0001f9e0 **Summary:** `{trade['underlying']} {trade['side']} closed by rules`\n"
        f"\U0001f3c6 **Grade:** `{grade}`\n\n"
        f"\U0001f4cc **Outcome:** `{outcome_label} (RULES FOLLOWED)`\n"
        f"Opened: `{trade['opened_at']:%Y-%m-%d %H:%M:%S %Z}`\n"
        f"Closed: `{closed_at:%Y-%m-%d %H:%M:%S %Z}`",
        color=DISCORD_COLOR_CALL if pnl_pct > 0 else DISCORD_COLOR_PUT,
    )

    row = {
        "opened_at": trade["opened_at"],
        "closed_at": closed_at,
        "underlying": trade["underlying"],
        "contract":   trade["contract"],
        "signal":     trade["signal"],
        "qty":        int(trade.get("qty", max(BASE_POSITION_QTY, MIN_POSITION_QTY))),
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
    # Clear the alert lock so the symbol can re-alert after cooldown.
    # The ignition gate prevents immediate re-chasing — no need to block all day.
    _alerted_today["keys"].discard(alert_key)

    log(f"[{trade['underlying']}] Closed {trade['contract']} ({reason}, {pnl_pct * 100:+.2f}%)")


def try_open_paper_trade(symbol, side, option, data):
    """Open a paper trade if trading is enabled and we have capacity. Returns True if opened."""
    if not ENABLE_ALPACA_PAPER_TRADING:
        return False

    # Refresh from Alpaca first so capacity/duplicate checks use broker truth.
    sync_open_trades_from_alpaca()

    if len(_open_trades) >= MAX_OPEN_TRADES:
        log(f"[{symbol}] Paper-trade capacity full ({len(_open_trades)}/{MAX_OPEN_TRADES}) — skipping.")
        return False
    if SINGLE_POSITION_PER_SYMBOL:
        already_open, detail = has_open_underlying_position(symbol, side)
        if already_open:
            log(f"[{symbol}] Existing {side} position detected ({detail}) — not opening another strike.")
            return False
    if option["contract"] in _open_trades:
        log(f"[{symbol}] Already long {option['contract']} — not stacking.")
        return False
    if _trading_client is None:
        return False

    score = int(data.get("effective_score", data["bull_score"] if side == "CALL" else data["bear_score"]))
    raw_score = int(data.get("raw_entry_score", score))
    macro_penalty = int(data.get("macro_penalty", 0))
    dominance = abs(int(data.get("bull_score", 0)) - int(data.get("bear_score", 0)))
    qty = position_qty_from_score(score, dominance)
    signal_label = f"STRONG {side}"

    # Broker-side buying power guard to reduce avoidable rejected option orders.
    try:
        account = _trading_client.get_account()
        options_bp = float(getattr(account, "options_buying_power", 0) or 0)
    except Exception:
        options_bp = 0.0

    est_contract_px = max(
        float(option.get("ask", 0.0) or 0.0),
        float(option.get("last", 0.0) or 0.0),
        float(option.get("bid", 0.0) or 0.0),
    )
    est_unit_cost = est_contract_px * 100.0
    bp_multiplier = 1.0 + max(0.0, OPTIONS_BP_BUFFER_PCT)
    est_cost = est_unit_cost * float(qty)
    required_bp = est_cost * bp_multiplier
    if options_bp > 0 and est_unit_cost > 0 and options_bp < required_bp:
        max_affordable_qty = int(options_bp // (est_unit_cost * bp_multiplier))
        if max_affordable_qty >= 1:
            prev_qty = qty
            qty = min(qty, max_affordable_qty)
            est_cost = est_unit_cost * float(qty)
            required_bp = est_cost * bp_multiplier
            log(
                f"[{symbol}] Buying power adjust: qty {prev_qty} -> {qty} "
                f"to fit options BP ${options_bp:,.2f} (est required ${required_bp:,.2f})."
            )
        else:
            log(
                f"[{symbol}] Buying power gate: estimated cost ${est_cost:,.2f} "
                f"(required ${required_bp:,.2f} with buffer) exceeds options BP ${options_bp:,.2f} — skipping entry."
            )
            return False

    try:
        _, fill_price = place_paper_entry(option, qty)
    except Exception as e:
        log(f"[{symbol}] Paper entry submit failed: {e} — not tracking trade.")
        return False

    if fill_price is None:
        log(f"[{symbol}] Order submitted but no fill confirmed within 5s — not tracking trade.")
        return False

    trade = open_trade_record(symbol, signal_label, option, score, fill_price, qty)
    _open_trades[trade["contract"]] = trade

    # Persist both ALERTS and TRADES records as part of the same entry flow.
    log_alert_to_sheets(symbol, data, option)
    log_trade_open_to_sheets(trade)

    setup = "MOMENTUM BREAKOUT" if score >= 90 else "TREND CONTINUATION"
    ai_label = "HIGH" if score >= 90 else "MEDIUM" if score >= 80 else "LOW"
    grade = "A" if score >= 90 else "B" if score >= 80 else "C"
    stop_pct = STOP_LOSS_PCT * 100
    target_1 = trade['entry'] * 1.25
    target_2 = trade['entry'] * 1.50

    score_line = f"{score}/100"
    if macro_penalty > 0:
        score_line = f"{score}/100 (raw {raw_score}, macro -{macro_penalty})"

    strike_val = float(trade.get("strike", 0) or 0)
    strike_text = str(int(strike_val)) if abs(strike_val - int(strike_val)) < 1e-9 else f"{strike_val:.2f}"
    entry_header = f"{trade['underlying']} strike {strike_text}"
    expiry_raw = str(trade.get("expiry", "") or "")
    expiry_mmdd = expiry_raw
    try:
        expiry_mmdd = datetime.strptime(expiry_raw, "%Y-%m-%d").strftime("%m/%d")
    except Exception:
        pass

    news_impact_label = str(data.get("news_impact_label", "LOW") or "LOW")
    news_impact_reason = str(data.get("news_impact_reason", "headline flow") or "headline flow")
    latest_news = _trim_text(data.get("latest_news", "No recent Alpaca news"), max_len=260)
    trending_news = _trim_text(data.get("trending_news", ""), max_len=220)
    news_context_lines = [
        f"\U0001f4f0 **News Impact:** `{news_impact_label}` ({news_impact_reason})",
        f"\U0001f5de **Latest News:** `{latest_news}`",
    ]
    if trending_news:
        news_context_lines.append(f"\U0001f4c8 **Trending Context:** `{trending_news}`")
    news_context_block = "\n".join(news_context_lines)

    send_discord(
        f"\U0001f680 **ENTRY ALERT — {entry_header} | {trade['side']} | ${trade['entry']:.2f} | {expiry_mmdd}**\n\n"
        f"------------------------------\n"
        f"\U0001f4b2 **Current Price:** `${float(data.get('price', 0.0) or 0.0):.2f}`\n"
        f"\U0001f4ca **Setup:** `{setup}` | Score: `{score_line}` ({grade})\n"
        f"{news_context_block}\n"
        f"\U0001f3af **Plan:** Entry `${trade['entry']:.2f}` | Target `${target_1:.2f}` / `${target_2:.2f}` | Stop `-{stop_pct:.0f}%`\n"
        f"\U0001f6d1 **Invalidation:** VWAP loss / hard stop hit\n"
        f"\U0001f916 **AI:** \U0001f7e1 **{ai_label}** — balanced setup with rules-aligned confirmation\n\n"
        f"\U0001f4cc **Action:** ENTERED (`{trade['qty']}` contract{'s' if trade['qty'] != 1 else ''})",
        color=DISCORD_COLOR_CALL if trade['side'] == 'CALL' else DISCORD_COLOR_PUT,
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

    # Entry scan first (symbol loop), then exit management in the same cycle.
    # This keeps the flow aligned with: entry -> check exit -> exit.
    symbols_to_scan = list(SYMBOLS)
    trending_symbols, _ = get_trending_symbols(client, SYMBOLS)
    for sym in trending_symbols:
        if sym not in symbols_to_scan:
            symbols_to_scan.append(sym)

    for symbol in symbols_to_scan:
        try:
            run_symbol(client, symbol)
        except Exception:
            log(f"[{symbol}] Cycle error:")
            traceback.print_exc()
            sys.stdout.flush()

    try:
        track_open_trades()
    except Exception:
        log("track_open_trades error:")
        traceback.print_exc()
        sys.stdout.flush()


def run_symbol(client, symbol):
    bars = fetch_bars(client, symbol)
    log(f"[{symbol}] Fetched {len(bars)} bars.")
    if len(bars) < 55:
        log(f"[{symbol}] Bars: {len(bars)}/55 — warming up.")
        return

    profile = symbol_profile(symbol)
    ignition_min_delta = int(profile["ignition_min_delta"])
    rsi_overbought = int(profile["rsi_overbought"])
    rsi_oversold = int(profile["rsi_oversold"])
    max_ext_from_vwap = float(profile["max_ext_from_vwap"])

    side, data = analyze(bars, client, symbol)
    if data:
        news_context = _get_symbol_news_context(symbol)
        data["latest_news"] = news_context.get("latest_news", "No recent Alpaca news")
        data["trending_news"] = news_context.get("trending_news", "")
        data["news_impact_label"] = news_context.get("impact_label", "LOW")
        data["news_impact_reason"] = news_context.get("impact_reason", "headline flow")

        trend_5m = f" | 5m\u0394 BULL {data['bull_score'] - data['bull_5m']:+d}" if data['bull_5m'] is not None else ""
        log(
            f"[{symbol}] {data['price']:.2f} | {data['signal']} | "
            f"BULL {data['bull_score']} BEAR {data['bear_score']} ({data['sentiment']}){trend_5m}"
        )
        # Keep SPY VWAP macro cache fresh for the alignment filter used by QQQ/IWM.
        if symbol == "SPY":
            _spy_vwap_cache["side"] = "bull" if data["price"] > data["vwap"] else "bear"
            _spy_vwap_cache["updated_at"] = datetime.now(central)

    if side == "NO TRADE":
        return

    closing_block_minutes = closing_no_trade_minutes_remaining()
    if closing_block_minutes > 0:
        log(
            f"[{symbol}] Closing window filter: skipping {data['signal']} during final "
            f"{CLOSING_NO_TRADE_MINUTES}m before close ({closing_block_minutes}m remaining)."
        )
        return

    if data.get("tier") == "WATCH" and not EXECUTE_WATCHLIST_SIGNALS:
        log(f"[{symbol}] Execution gate: WATCHLIST tier is alerts-only (set EXECUTE_WATCHLIST_SIGNALS=1 to enable entries).")
        return

    # Hard minimum score gate with symbol-aware threshold.
    # ETFs keep the base strict gate (> SCORE_STRONG), while single stocks require
    # an extra score buffer (SCORE_STRONG + STOCK_STRONG_SCORE_BONUS).
    enforce_hard_gate = HARD_SCORE_GATE_ENABLED and (
        (not NO_GATING_MODE) or HARD_SCORE_GATE_IN_NO_GATING_MODE
    )
    if enforce_hard_gate:
        side_score = data["bull_score"] if side == "CALL" else data["bear_score"]
        min_required_score = (
            SCORE_STRONG + 1
            if symbol in ETF_SYMBOLS
            else SCORE_STRONG + max(0, STOCK_STRONG_SCORE_BONUS)
        )
        if side_score < min_required_score:
            log(
                f"[{symbol}] Hard score gate: {side} {side_score} < {min_required_score} "
                f"- skipping (requires >= {min_required_score})."
            )
            return

    # Only send Discord alerts for the perfect setup (STRONG tier) unless no-gating mode is enabled.
    if (not NO_GATING_MODE) and data["tier"] != "STRONG":
        log(f"[{symbol}] {data['signal']} (BULL {data['bull_score']} / BEAR {data['bear_score']}) "
            f"\u2014 below STRONG threshold, no Discord alert.")
        return

    enforce_opening_window = (not NO_GATING_MODE) or ENFORCE_OPENING_WINDOW_IN_NO_GATING
    if enforce_opening_window:
        opening_block_minutes = opening_no_trade_minutes_remaining()
        if opening_block_minutes > 0:
            log(
                f"[{symbol}] Opening volatility filter: skipping {data['signal']} during first "
                f"{OPENING_NO_TRADE_MINUTES}m after open ({opening_block_minutes}m remaining)."
            )
            return

    # One STRONG CALL alert and one STRONG PUT alert max per (symbol, side) per day.
    # After a trade closes, a 30-min cooldown allows the same setup to re-trigger.
    alert_key = (symbol, side)
    now_ct = datetime.now(central)
    if not NO_GATING_MODE:
        if alert_key in _alerted_today["keys"]:
            cooldown_until = _alert_cooldowns.get(alert_key)
            if cooldown_until and now_ct < cooldown_until:
                mins_left = max(1, int((cooldown_until - now_ct).total_seconds() // 60))
                log(f"[{symbol}] Already alerted {side} today (cooldown active, {mins_left}m left) — suppressed.")
                return
            if cooldown_until and now_ct >= cooldown_until:
                _alerted_today["keys"].discard(alert_key)
                _alert_cooldowns.pop(alert_key, None)

            # If no live/pending Alpaca trade exists, this is a stale lock — clear it.
            live_exists, live_detail = has_open_underlying_position(symbol, side)
            if not live_exists:
                _alerted_today["keys"].discard(alert_key)
                _alert_cooldowns.pop(alert_key, None)
                log(f"[{symbol}] Cleared stale alert lock for {side} (no live Alpaca trade found).")
            elif alert_key in _alert_cooldowns and now_ct >= _alert_cooldowns[alert_key]:
                # Cooldown expired — clear and allow re-alert.
                _alerted_today["keys"].discard(alert_key)
                _alert_cooldowns.pop(alert_key, None)
            else:
                log(f"[{symbol}] Already alerted {side} today ({live_detail}) — suppressed.")
                return

    # Trend-ignition filter: only fire when the move is *just starting*, not mid- or late-trend.
    if IGNITION_REQUIRED and not NO_GATING_MODE:
        if side == "CALL":
            now_score = data["bull_score"]
            past_score = data["bull_5m"]
        else:
            now_score = data["bear_score"]
            past_score = data["bear_5m"]

        if past_score is None:
            history = score_history.get(symbol, deque())
            if len(history) >= 2:
                # Cold-start fallback: compare against the oldest sampled score since boot
                # so we can still capture fast early-session ignitions.
                _, oldest_bull, oldest_bear = history[0]
                past_score = oldest_bull if side == "CALL" else oldest_bear
                delta = now_score - past_score
                if now_score < 90 and delta < ignition_min_delta:
                    log(
                        f"[{symbol}] Ignition gate: warmup history only ({len(history)} samples), "
                        f"{side} delta +{delta} < +{ignition_min_delta} \u2014 waiting for clearer ignition."
                    )
                    return
                log(
                    f"[{symbol}] Ignition warmup: using partial history ({len(history)} samples) "
                    f"{past_score} \u2192 {now_score} (\u0394 +{delta})."
                )
            elif now_score >= 90:
                # Perfect-score cold start: allow instead of waiting 5 minutes.
                past_score = now_score
                delta = 0
                log(
                    f"[{symbol}] \U0001f525 Perfect-score cold-start override: {side} score {now_score} \u2265 90 "
                    "\u2014 ignition gate bypassed without full lookback history."
                )
            else:
                log(
                    f"[{symbol}] Ignition gate: insufficient history (need {IGNITION_LOOKBACK_S}s) \u2014 "
                    "holding alert until trend can be measured."
                )
                return
        else:
            delta = now_score - past_score
        continuation_override = False
        # Perfect-score override: score ≥ 90 fires regardless of prior level.
        if now_score >= 90:
            log(
                f"[{symbol}] \U0001f525 Perfect-score override: {side} score {now_score} \u2265 90 "
                "\u2014 ignition gate bypassed."
            )
        elif past_score >= IGNITION_PRIOR_MAX:
            if (
                IGNITION_CONTINUATION_ENABLED
                and now_score >= IGNITION_CONTINUATION_MIN_SCORE
                and delta >= IGNITION_CONTINUATION_MIN_DELTA
            ):
                log(
                    f"[{symbol}] Ignition continuation override: {side} remains strong "
                    f"({past_score} -> {now_score}, \u0394 {delta:+d}) \u2014 allowing one continuation entry."
                )
                continuation_override = True
            else:
                log(
                    f"[{symbol}] Ignition gate: {side} 5m ago was already {past_score} "
                    f"(>= {IGNITION_PRIOR_MAX}) \u2014 mid/late trend, no alert."
                )
                return
        if (not continuation_override) and now_score < 90 and delta < ignition_min_delta:
            log(
                f"[{symbol}] Ignition gate: {side} delta only +{delta} (need +{ignition_min_delta}) "
                f"\u2014 trend not igniting, no alert."
            )
            return

        log(
            f"[{symbol}] \U0001f680 Ignition confirmed: {side} score {past_score} \u2192 {now_score} "
            f"(\u0394 +{delta}) over last {IGNITION_LOOKBACK_S}s."
        )

    # ── RSI exhaustion filter ─────────────────────────────────────────────────
    # Don't enter CALLs when RSI is already overbought (move likely exhausted),
    # or PUTs when RSI is already oversold.
    if RSI_FILTER and not NO_GATING_MODE:
        rsi = data.get("rsi", 50.0)
        if side == "CALL" and rsi >= rsi_overbought:
            log(f"[{symbol}] RSI filter: CALL blocked — RSI {rsi:.1f} >= {rsi_overbought} (overbought, late entry).")
            return
        if side == "PUT" and rsi <= rsi_oversold:
            log(f"[{symbol}] RSI filter: PUT blocked — RSI {rsi:.1f} <= {rsi_oversold} (oversold, late entry).")
            return

    # ── Macro alignment context (penalty by default, optional hard block) ───
    macro_penalty = 0
    if SPY_MACRO_ALIGN and (not NO_GATING_MODE) and symbol != "SPY" and "SPY" in SYMBOLS:
        spy_vwap_side = _spy_vwap_side()
        misaligned = (side == "CALL" and spy_vwap_side != "bull") or (side == "PUT" and spy_vwap_side != "bear")
        if misaligned:
            if SPY_MACRO_HARD_BLOCK:
                log(
                    f"[{symbol}] Macro filter: {side} blocked — SPY VWAP side mismatch "
                    f"(spy_side={spy_vwap_side})."
                )
                return
            macro_penalty = max(0, SPY_MACRO_SCORE_PENALTY)
            log(
                f"[{symbol}] Macro context: {side} misaligned with SPY VWAP "
                f"(spy_side={spy_vwap_side}) — applying -{macro_penalty} confidence penalty (no veto)."
            )

    raw_score = data["bull_score"] if side == "CALL" else data["bear_score"]
    effective_score = max(0, raw_score - macro_penalty)
    data["raw_entry_score"] = raw_score
    data["macro_penalty"] = macro_penalty
    data["effective_score"] = effective_score

    # ── Anti-chase filters ───────────────────────────────────────────────────
    # Avoid buying when price is already too extended away from VWAP, and avoid
    # entering when the latest candle already flipped against our side.
    if ANTI_CHASE_FILTER and not NO_GATING_MODE:
        ext_pct = float(data.get("vwap_extension_pct", 0.0))
        if ext_pct > max_ext_from_vwap:
            log(
                f"[{symbol}] Anti-chase: {side} blocked — price is {ext_pct*100:.2f}% from VWAP "
                f"(max {max_ext_from_vwap*100:.2f}%)."
            )
            return

    if CANDLE_CONFIRMATION and not NO_GATING_MODE:
        if side == "CALL" and not data.get("bullish_candle", False):
            log(f"[{symbol}] Candle filter: CALL blocked — latest candle is not bullish.")
            return
        if side == "PUT" and not data.get("bearish_candle", False):
            log(f"[{symbol}] Candle filter: PUT blocked — latest candle is not bearish.")
            return

    # Lock this (symbol, side) NOW — before the option fetch — so a failed or
    # rate-limited fetch doesn't cause ignition to re-fire every 30 seconds.
    if not NO_GATING_MODE:
        _alerted_today["keys"].add(alert_key)

    option = get_option_contract(symbol, side, data["price"])
    if not option:
        if (not NO_GATING_MODE) and ALERT_ONLY_COOLDOWN_MINUTES > 0:
            _alert_cooldowns[alert_key] = now_ct + timedelta(minutes=ALERT_ONLY_COOLDOWN_MINUTES)
        log(f"[{symbol}] {data['signal']} setup detected, but no valid 1DTE+ option found — skipping (real trades only).")
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

Execution required \u2014 real trades only.
"""
    if ENABLE_ALPACA_PAPER_TRADING:
        # Execution path: place order + log sheets + send a single entry alert.
        try:
            opened = try_open_paper_trade(symbol, side, option, data)
        except Exception:
            log(f"[{symbol}] try_open_paper_trade error:")
            traceback.print_exc()
            sys.stdout.flush()
            opened = False

        if not opened:
            if (not NO_GATING_MODE) and ALERT_ONLY_COOLDOWN_MINUTES > 0:
                _alert_cooldowns[alert_key] = now_ct + timedelta(minutes=ALERT_ONLY_COOLDOWN_MINUTES)
            log(f"[{symbol}] Execution unavailable for {data['signal']} {option['contract']} — skipping (real trades only).")
    else:
        # Real-trades-only mode: no alert/sheet output when execution is disabled.
        if (not NO_GATING_MODE) and ALERT_ONLY_COOLDOWN_MINUTES > 0:
            _alert_cooldowns[alert_key] = now_ct + timedelta(minutes=ALERT_ONLY_COOLDOWN_MINUTES)
        log(f"[{symbol}] Paper trading disabled — skipping {data['signal']} setup (real trades only).")


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
        log("Paper trading DISABLED — real-trades-only mode, no Discord/Sheets for setups (TradingClient used for option contract lookup only).")
    if NO_GATING_MODE:
        if HARD_SCORE_GATE_ENABLED and HARD_SCORE_GATE_IN_NO_GATING_MODE:
            log("NO_GATING_MODE enabled - bypassing soft entry gates, hard score gate remains ON.")
        elif HARD_SCORE_GATE_ENABLED:
            log("NO_GATING_MODE enabled - bypassing entry gates, including hard score gate.")
        else:
            log("NO_GATING_MODE enabled - bypassing entry gates (hard score gate disabled globally).")
        if ENFORCE_OPENING_WINDOW_IN_NO_GATING:
            log(f"Opening no-trade window remains enforced for first {OPENING_NO_TRADE_MINUTES}m after open.")
        else:
            log("Opening no-trade window bypassed in NO_GATING_MODE.")

    init_google_sheets()

    if RECOVER_OPEN_POSITIONS:
        sync_open_trades_from_alpaca()

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
