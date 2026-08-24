"""
Index ETF Options Alerts Bot — polling version (SPY / QQQ / IWM by default).

Pulls 5-minute bars from Alpaca REST every POLL_SECONDS for each configured
symbol, runs a Bull/Bear market scorecard, applies a trend-ignition filter,
and posts a Discord alert with a near-the-money 1DTE+ option contract from
Alpaca's live options data when the score ignites into STRONG territory.

No WebSocket -> no Alpaca connection-limit issues.
"""

import os
import json
import re
import sys
import time
from math import exp
import subprocess
import threading
import traceback
import uuid
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import deque
from datetime import datetime, date, timedelta, timezone

import gspread
from google.oauth2.service_account import Credentials as GCredentials

import pandas as pd
import pytz
import requests
from dotenv import load_dotenv

from alpaca.data.historical import StockHistoricalDataClient, OptionHistoricalDataClient
from alpaca.data.live import StockDataStream
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
DISCORD_WEBHOOK_LIVE_TRADES_URL = os.getenv("DISCORD_WEBHOOK_LIVE_TRADES") or DISCORD_WEBHOOK_URL
DISCORD_WEBHOOK_AI_STATUS_URL = os.getenv("DISCORD_WEBHOOK_AI_STATUS", "").strip()
DISCORD_WEBHOOK_MORNING_BRIEFING_URL = os.getenv("DISCORD_WEBHOOK_MORNING_BRIEFING", "").strip()
DISCORD_WEBHOOK_EOD_SUMMARY_URL = os.getenv("DISCORD_WEBHOOK_EOD_SUMMARY", "").strip()
DISCORD_WEBHOOK_ERRORS_URL = os.getenv("DISCORD_WEBHOOK_ERRORS", "").strip()
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
DISCORD_CHANNEL_ID = (
    os.getenv("DISCORD_CHANNEL_ID")
    or os.getenv("DISCORD_LIVE_TRADES_CHANNEL_ID")
    or ""
).strip()
FEED = os.getenv("ALPACA_FEED", "iex").lower()
_options_feed_env = os.getenv("ALPACA_OPTIONS_FEED", "").strip().lower()
if _options_feed_env in {"opra", "indicative"}:
    OPTIONS_FEED = OptionsFeed(_options_feed_env)
else:
    OPTIONS_FEED = None

# Symbols to scan, in order. Override via env.
ETF_SYMBOLS = {"SPY", "QQQ", "IWM"}
TOP_STOCK_SYMBOLS = {
    "AAPL", "NVDA", "MSFT", "AMZN",
    "TSLA", "AMD", "PLTR", "GOOGL",
    "AVGO", "INTC", "SPCX",
    "ADBE", "HOOD", "ORCL",
}
AGGRESSIVE_STOCK_SYMBOLS = {"TSLA", "AMD", "PLTR", "GOOGL"}
# Shadow-only liquidity tier classification (see _shadow_tier_liquidity_check) — logging only,
# does not gate contract selection until validated against real fills/outcomes.
MEGA_LIQUID_SYMBOLS = {"AAPL", "NVDA", "MSFT", "AMZN", "TSLA", "GOOGL", "AMD"}
TIER_LIQUIDITY_THRESHOLDS = {
    "ETF":          {"min_oi": 2000, "min_volume": 200, "max_spread_pct": 0.03},
    "MEGA_LIQUID":  {"min_oi": 500,  "min_volume": 50,  "max_spread_pct": 0.06},
    "STANDARD":     {"min_oi": 100,  "min_volume": 10,  "max_spread_pct": 0.10},
}
DEFAULT_SYMBOLS = "SPY,QQQ,IWM,AAPL,NVDA,MSFT,AMZN,TSLA,AMD,PLTR,GOOGL,AVGO,ADBE,HOOD,ORCL"
SYMBOLS = [s.strip().upper() for s in os.getenv("SYMBOLS", DEFAULT_SYMBOLS).split(",") if s.strip()]
ALPACA_DATA_BASE_URL = os.getenv("ALPACA_DATA_BASE_URL", "https://data.alpaca.markets")
ALPACA_TRADING_BASE_URL = os.getenv("ALPACA_TRADING_BASE_URL", "https://paper-api.alpaca.markets")
ENABLE_TRENDING_STOCKS = os.getenv("ENABLE_TRENDING_STOCKS", "1") == "1"
TRENDING_STOCK_COUNT = int(os.getenv("TRENDING_STOCK_COUNT", "10"))
TRENDING_REFRESH_SECONDS = int(os.getenv("TRENDING_REFRESH_SECONDS", "300"))
TRENDING_NEWS_HEADLINES = int(os.getenv("TRENDING_NEWS_HEADLINES", "2"))
ENABLE_STOCKTWITS_TRENDING = os.getenv("ENABLE_STOCKTWITS_TRENDING", "1") == "1"
STOCKTWITS_TRENDING_URL = os.getenv("STOCKTWITS_TRENDING_URL", "https://api.stocktwits.com/api/2/trending/symbols.json")
STOCKTWITS_TIMEOUT_SECONDS = int(os.getenv("STOCKTWITS_TIMEOUT_SECONDS", "6"))
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
WS_SYMBOL_MIN_EVAL_SECONDS = int(os.getenv("WS_SYMBOL_MIN_EVAL_SECONDS", "5"))
WS_EXIT_CHECK_SECONDS = int(os.getenv("WS_EXIT_CHECK_SECONDS", "5"))
EXIT_REVIEW_ENABLED = os.getenv("EXIT_REVIEW_ENABLED", "1") == "1"
EXIT_REVIEW_DELAY_MINUTES = int(os.getenv("EXIT_REVIEW_DELAY_MINUTES", "30"))
EXIT_REVIEW_CHECK_SECONDS = int(os.getenv("EXIT_REVIEW_CHECK_SECONDS", "60"))
WS_LOOP_SLEEP_SECONDS = float(os.getenv("WS_LOOP_SLEEP_SECONDS", "0.5"))
WS_FULL_SCAN_INTERVAL_SECONDS = int(os.getenv("WS_FULL_SCAN_INTERVAL_SECONDS", "30"))
SCAN_PREFETCH_PARALLEL_ENABLED = os.getenv("SCAN_PREFETCH_PARALLEL_ENABLED", "1") == "1"
SCAN_PREFETCH_MAX_WORKERS = max(1, int(os.getenv("SCAN_PREFETCH_MAX_WORKERS", "8")))
ENABLE_SCAN_TIMING_LOGS = os.getenv("ENABLE_SCAN_TIMING_LOGS", "1") == "1"
SCAN_SYMBOL_LOG_THRESHOLD_MS = float(os.getenv("SCAN_SYMBOL_LOG_THRESHOLD_MS", "250"))
OPENING_NO_TRADE_MINUTES = int(os.getenv("OPENING_NO_TRADE_MINUTES", "15"))
OPENING_EXCEPTION_ENABLED = os.getenv("OPENING_EXCEPTION_ENABLED", "1") == "1"
OPENING_EXCEPTION_MIN_SCORE = int(os.getenv("OPENING_EXCEPTION_MIN_SCORE", "85"))
OPENING_EXCEPTION_MIN_DOMINANCE = int(os.getenv("OPENING_EXCEPTION_MIN_DOMINANCE", "50"))
OPENING_EXCEPTION_MIN_SIDE_DELTA_5M = int(os.getenv("OPENING_EXCEPTION_MIN_SIDE_DELTA_5M", "8"))
OPENING_EXCEPTION_MIN_VOL_RATIO = float(os.getenv("OPENING_EXCEPTION_MIN_VOL_RATIO", "1.20"))
CLOSING_NO_TRADE_MINUTES = int(os.getenv("CLOSING_NO_TRADE_MINUTES", "30"))
LOOKBACK_BARS = 120
RECENT_HIGH_LOOKBACK = 20  # bars used for intraday recent high/low (~100 min)
MIN_DTE = int(os.getenv("MIN_DTE", "1"))   # Minimum DTE (exclude 0DTE)
MAX_DTE = int(os.getenv("MAX_DTE", "5"))  # Max DTE window; set <=0 for no upper bound
VOLUME_MULTIPLIER = 1.5

# Scoring thresholds (0-100)
SCORE_STRONG = int(os.getenv("SCORE_STRONG", "80"))   # STRONG CALL/PUT alert
SCORE_SIGNAL = int(os.getenv("SCORE_SIGNAL", "65"))   # CALL/PUT alert
SCORE_WATCH  = int(os.getenv("SCORE_WATCH",  "50"))   # WATCHLIST heads-up
SCORE_DOMINANCE = int(os.getenv("SCORE_DOMINANCE", "20"))  # bull must lead bear by this much (and vice versa)
# Entry decisions use one of two named technical playbooks. Set to 0 only to
# retain the legacy stacked ignition/watchlist/RSI entry chain.
TWO_PLAYBOOK_ENTRY_MODE = os.getenv("TWO_PLAYBOOK_ENTRY_MODE", "1") == "1"
BREAKOUT_MIN_SCORE = int(os.getenv("BREAKOUT_MIN_SCORE", str(SCORE_SIGNAL)))
PULLBACK_MIN_SCORE = int(os.getenv("PULLBACK_MIN_SCORE", str(SCORE_SIGNAL)))
PLAYBOOK_MIN_DOMINANCE = int(os.getenv("PLAYBOOK_MIN_DOMINANCE", str(SCORE_DOMINANCE)))
PLAYBOOK_MAX_VWAP_EXTENSION = float(os.getenv("PLAYBOOK_MAX_VWAP_EXTENSION", "0.012"))
# Tiered VWAP extension (replaces the single static cutoff above as the actual gate).
# NORMAL: <= PLAYBOOK_MAX_VWAP_EXTENSION. EXTENDED: allowed only with strong confirmation.
# VERY_EXTENDED: always blocked — not overridden by any confirmation (frozen risk guard).
PLAYBOOK_VWAP_EXTENDED_MAX = float(os.getenv("PLAYBOOK_VWAP_EXTENDED_MAX", "0.016"))
PLAYBOOK_VWAP_EXTENDED_OVERRIDE_MIN_SCORE = int(os.getenv("PLAYBOOK_VWAP_EXTENDED_OVERRIDE_MIN_SCORE", "70"))
PLAYBOOK_VWAP_EXTENDED_OVERRIDE_MIN_DOMINANCE = int(os.getenv("PLAYBOOK_VWAP_EXTENDED_OVERRIDE_MIN_DOMINANCE", "55"))
PLAYBOOK_VWAP_EXTENDED_OVERRIDE_MIN_VOLX = float(os.getenv("PLAYBOOK_VWAP_EXTENDED_OVERRIDE_MIN_VOLX", "1.50"))
PLAYBOOK_VWAP_EXTENDED_OVERRIDE_MIN_PATTERN = float(os.getenv("PLAYBOOK_VWAP_EXTENDED_OVERRIDE_MIN_PATTERN", "80.0"))
# Shadow telemetry only — logs what the decision would be at these caps, never gates.
VWAP_SHADOW_CAPS_PCT = [0.0120, 0.0140, 0.0160, 0.0180, 0.0200]
# Armed breakout setups: patience window (in completed 5m bars) before a pending
# breakout/pullback expires without confirmation.
ARMED_SETUP_MAX_BARS = int(os.getenv("ARMED_SETUP_MAX_BARS", "3"))
PULLBACK_RECLAIM_TOLERANCE = float(os.getenv("PULLBACK_RECLAIM_TOLERANCE", "0.003"))
PULLBACK_MIN_SCORE_DELTA = int(os.getenv("PULLBACK_MIN_SCORE_DELTA", "0"))
BREAKOUT_HOLD_CONFIRMATION_ENABLED = os.getenv("BREAKOUT_HOLD_CONFIRMATION_ENABLED", "1") == "1"
BREAKOUT_CONFIRMATION_MIN_BUFFER_PCT = float(os.getenv("BREAKOUT_CONFIRMATION_MIN_BUFFER_PCT", "0.001"))
BREAKOUT_CONFIRMATION_MIN_SCORE_DELTA = int(os.getenv("BREAKOUT_CONFIRMATION_MIN_SCORE_DELTA", "1"))
BREAKOUT_CONFIRMATION_MIN_VOLUME_RATIO = float(os.getenv("BREAKOUT_CONFIRMATION_MIN_VOLUME_RATIO", "1.0"))
BREAKOUT_CONTINUATION_ENABLED = os.getenv("BREAKOUT_CONTINUATION_ENABLED", "1") == "1"
BREAKOUT_CONTINUATION_WINDOW_MINUTES = int(os.getenv("BREAKOUT_CONTINUATION_WINDOW_MINUTES", "15"))
BREAKOUT_CONTINUATION_MIN_SCORE = int(os.getenv("BREAKOUT_CONTINUATION_MIN_SCORE", "75"))
PLAYBOOK_EXTREME_PROXIMITY_PCT = float(os.getenv("PLAYBOOK_EXTREME_PROXIMITY_PCT", "0.0015"))
BREAKOUT_PUT_ENTRIES_ENABLED = os.getenv("BREAKOUT_PUT_ENTRIES_ENABLED", "0") == "1"
MAX_NEW_ENTRIES_PER_CYCLE = max(1, int(os.getenv("MAX_NEW_ENTRIES_PER_CYCLE", "10")))
# Global bypass for all entry gating layers (tier/stock strict/opening/cooldown/ignition/RSI/macro/anti-chase/candle).
NO_GATING_MODE = os.getenv("NO_GATING_MODE", "0") == "1"
ENFORCE_OPENING_WINDOW_IN_NO_GATING = os.getenv("ENFORCE_OPENING_WINDOW_IN_NO_GATING", "1") == "1"
# Recall-first mode: bias the bot toward taking strong setups rather than filtering them away.
RECALL_FIRST_MODE = os.getenv("RECALL_FIRST_MODE", "1") == "1"
# Stock-only tightening: require a slightly higher effective strong score.
STOCK_STRONG_SCORE_BONUS = int(os.getenv("STOCK_STRONG_SCORE_BONUS", "0"))
# Hard score gate control:
# - When HARD_SCORE_GATE_ENABLED=0, no hard minimum score is enforced.
# - When NO_GATING_MODE=1, the hard gate is bypassed by default unless
#   HARD_SCORE_GATE_IN_NO_GATING_MODE=1.
HARD_SCORE_GATE_ENABLED = os.getenv("HARD_SCORE_GATE_ENABLED", "1") == "1"
HARD_SCORE_GATE_IN_NO_GATING_MODE = os.getenv("HARD_SCORE_GATE_IN_NO_GATING_MODE", "1") == "1"
# Dynamic hard-gate tuning: adjust required score by real-time regime/momentum quality.
DYNAMIC_HARD_GATE_ENABLED = os.getenv("DYNAMIC_HARD_GATE_ENABLED", "0") == "1"
DYNAMIC_HARD_GATE_MAX_RELIEF = int(os.getenv("DYNAMIC_HARD_GATE_MAX_RELIEF", "0"))
DYNAMIC_HARD_GATE_MAX_PENALTY = int(os.getenv("DYNAMIC_HARD_GATE_MAX_PENALTY", "0"))
DYNAMIC_HARD_GATE_MIN_FLOOR_ETF = int(os.getenv("DYNAMIC_HARD_GATE_MIN_FLOOR_ETF", "67"))
DYNAMIC_HARD_GATE_MIN_FLOOR_STOCK = int(os.getenv("DYNAMIC_HARD_GATE_MIN_FLOOR_STOCK", "64"))
TOP_STOCK_HARD_GATE_MIN_FLOOR = int(os.getenv("TOP_STOCK_HARD_GATE_MIN_FLOOR", "62"))
# Execution behavior controls.
# Default: do not auto-execute WATCHLIST setups (alerts-only quality).
EXECUTE_WATCHLIST_SIGNALS = os.getenv("EXECUTE_WATCHLIST_SIGNALS", "0") == "1"
# Selective watchlist execution: allow entries only when continuation confirmation passes.
SELECTIVE_WATCHLIST_EXECUTION_ENABLED = os.getenv("SELECTIVE_WATCHLIST_EXECUTION_ENABLED", "1") == "1"
ETF_SIMPLE_ENTRY_MODE = os.getenv("ETF_SIMPLE_ENTRY_MODE", "1") == "1"
STOCK_SIMPLE_ENTRY_MODE = os.getenv("STOCK_SIMPLE_ENTRY_MODE", "1") == "1"
WATCHLIST_PROMOTION_MIN_SCORE = int(os.getenv("WATCHLIST_PROMOTION_MIN_SCORE", "56"))
WATCHLIST_PROMOTION_MIN_DOMINANCE = int(os.getenv("WATCHLIST_PROMOTION_MIN_DOMINANCE", "12"))
WATCHLIST_PROMOTION_MIN_MOMENTUM = int(os.getenv("WATCHLIST_PROMOTION_MIN_MOMENTUM", "35"))
WATCHLIST_PROMOTION_MIN_VOLUME = int(os.getenv("WATCHLIST_PROMOTION_MIN_VOLUME", "30"))
WATCHLIST_PROMOTION_MIN_DELTA_5M = int(os.getenv("WATCHLIST_PROMOTION_MIN_DELTA_5M", "2"))
TOP_STOCK_WATCHLIST_REL_STRENGTH_MIN = float(os.getenv("TOP_STOCK_WATCHLIST_REL_STRENGTH_MIN", "5.0"))
TOP_STOCK_WATCHLIST_PATTERN_MIN = float(os.getenv("TOP_STOCK_WATCHLIST_PATTERN_MIN", "60.0"))
# Pre-check options buying power before submitting market BUYs.
OPTIONS_BP_BUFFER_PCT = float(os.getenv("OPTIONS_BP_BUFFER_PCT", "0.05"))

# Trend-ignition filter: only fire when the score is *starting* to rise into the threshold.
# CALL example: 5 minutes ago BULL was below IGNITION_PRIOR_MAX, now it has gained at least IGNITION_MIN_DELTA.
# Set IGNITION_REQUIRED=0 in env to disable and revert to absolute-score-only firing.
IGNITION_REQUIRED   = os.getenv("IGNITION_REQUIRED", "1") == "1"
IGNITION_MIN_DELTA  = int(os.getenv("IGNITION_MIN_DELTA",  "15"))  # Relaxed from 25 (was too tight, failing at +15/+25)
IGNITION_PRIOR_MAX  = int(os.getenv("IGNITION_PRIOR_MAX",  "74"))  # Block only if already at STRONG level (≥75); allows breakouts from 70
IGNITION_LOOKBACK_S = int(os.getenv("IGNITION_LOOKBACK_S", "300"))  # how far back to compare (default 5 min)
IGNITION_DELTA_80_89 = int(os.getenv("IGNITION_DELTA_80_89", "20"))
IGNITION_DELTA_90_94 = int(os.getenv("IGNITION_DELTA_90_94", "14"))
IGNITION_DELTA_95_PLUS = int(os.getenv("IGNITION_DELTA_95_PLUS", "10"))
# Continuation override: allow one strong alert even if the trend is already hot,
# as long as score remains very strong and has not faded over lookback.
IGNITION_CONTINUATION_ENABLED = os.getenv("IGNITION_CONTINUATION_ENABLED", "1") == "1"
IGNITION_CONTINUATION_MIN_SCORE = int(os.getenv("IGNITION_CONTINUATION_MIN_SCORE", "85"))
IGNITION_CONTINUATION_MIN_DELTA = int(os.getenv("IGNITION_CONTINUATION_MIN_DELTA", "10"))
STRICT_IGNITION_NO_BYPASS = os.getenv("STRICT_IGNITION_NO_BYPASS", "1") == "1"

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
# Bearish tape CALL penalty: if SPY+QQQ are both bearish at entry time, add an
# extra score penalty to CALLs so only truly high-conviction bullish entries pass.
BEARISH_TAPE_CALL_PENALTY_ENABLED = os.getenv("BEARISH_TAPE_CALL_PENALTY_ENABLED", "1") == "1"
BEARISH_TAPE_CALL_SCORE_PENALTY   = int(os.getenv("BEARISH_TAPE_CALL_SCORE_PENALTY", "8"))
# Marginal score tighter stop: for borderline setups (score < MARGINAL_SCORE_CEIL),
# apply a tighter stop so weak entries don't bleed out to full stop.
MARGINAL_SCORE_STOP_ENABLED = os.getenv("MARGINAL_SCORE_STOP_ENABLED", "1") == "1"
MARGINAL_SCORE_FLOOR  = int(os.getenv("MARGINAL_SCORE_FLOOR", "64"))
MARGINAL_SCORE_CEIL   = int(os.getenv("MARGINAL_SCORE_CEIL",  "77"))
MARGINAL_STOP_PCT     = float(os.getenv("MARGINAL_STOP_PCT",  "0.08"))
# Recovered position market alignment: when a recovered position direction is
# opposite to current market bias, apply a tighter stop and a score penalty so it
# exits quickly if the market continues against it.
RECOVERED_MARKET_ALIGN_ENABLED      = os.getenv("RECOVERED_MARKET_ALIGN_ENABLED", "1") == "1"
RECOVERED_OPPOSING_STOP_PCT         = float(os.getenv("RECOVERED_OPPOSING_STOP_PCT", "0.05"))
RECOVERED_OPPOSING_SCORE_PENALTY    = int(os.getenv("RECOVERED_OPPOSING_SCORE_PENALTY", "12"))

# Anti-chase entry filters: avoid entering when price is too stretched from VWAP,
# or when the latest candle already flipped against the intended side.
ANTI_CHASE_FILTER  = os.getenv("ANTI_CHASE_FILTER", "1") == "1"
MAX_EXT_FROM_VWAP  = float(os.getenv("MAX_EXT_FROM_VWAP", "0.012"))  # 1.2%
CANDLE_CONFIRMATION = os.getenv("CANDLE_CONFIRMATION", "1") == "1"
# Momentum continuation pre-entry filter: block weak/late continuation even after ignition.
ENTRY_MOMENTUM_CONTINUATION_FILTER = os.getenv("ENTRY_MOMENTUM_CONTINUATION_FILTER", "1") == "1"
ENTRY_CONT_MIN_MOMENTUM_SCORE = float(os.getenv("ENTRY_CONT_MIN_MOMENTUM_SCORE", "52"))
ENTRY_CONT_MIN_DELTA_5M = int(os.getenv("ENTRY_CONT_MIN_DELTA_5M", "1"))
ENTRY_CONT_MIN_EMA20_SLOPE_PCT = float(os.getenv("ENTRY_CONT_MIN_EMA20_SLOPE_PCT", "0.0001"))  # Relaxed from 0.00015 (was too tight)
ENTRY_CONT_MIN_MOMENTUM_QUALITY = float(os.getenv("ENTRY_CONT_MIN_MOMENTUM_QUALITY", "8.0"))
ENTRY_MAX_TREND_AGE_BARS = int(os.getenv("ENTRY_MAX_TREND_AGE_BARS", "4"))
ENTRY_1DTE_MIN_DELTA = float(os.getenv("ENTRY_1DTE_MIN_DELTA", "0.40"))
# ML entry gate (optional): model reads current rule-engine features and returns win probability.
ML_GATE_ENABLED = os.getenv("ML_GATE_ENABLED", "0") == "1"  # Disabled: enable only after 100+ trades
ML_GATE_MODEL_PATH = os.getenv("ML_GATE_MODEL_PATH", "models/entry_model.json")
ML_GATE_REQUIRE_MODEL = os.getenv("ML_GATE_REQUIRE_MODEL", "0") == "1"
ML_MIN_PROBABILITY = float(os.getenv("ML_MIN_PROBABILITY", "0.62"))
ML_MIN_EXPECTED_RETURN = float(os.getenv("ML_MIN_EXPECTED_RETURN", "0.00"))
# Auto-retrain: refresh ML model after enough closed trades (non-blocking background job).
AUTO_RETRAIN_MODEL_ENABLED = os.getenv("AUTO_RETRAIN_MODEL_ENABLED", "0") == "1"
AUTO_RETRAIN_MIN_NEW_CLOSED_TRADES = int(os.getenv("AUTO_RETRAIN_MIN_NEW_CLOSED_TRADES", "25"))
AUTO_RETRAIN_COOLDOWN_MINUTES = int(os.getenv("AUTO_RETRAIN_COOLDOWN_MINUTES", "60"))
AUTO_RETRAIN_SOURCE = os.getenv("AUTO_RETRAIN_SOURCE", "csv").strip().lower()
AUTO_RETRAIN_INPUT_PATH = os.getenv("AUTO_RETRAIN_INPUT_PATH", "trade_results.csv")
AUTO_RETRAIN_SHEET_TAB = os.getenv("AUTO_RETRAIN_SHEET_TAB", "Trades")
AUTO_RETRAIN_OUTPUT_PATH = os.getenv("AUTO_RETRAIN_OUTPUT_PATH", "models/entry_model.json")
AUTO_RETRAIN_MIN_SAMPLES = int(os.getenv("AUTO_RETRAIN_MIN_SAMPLES", "20"))
ALERT_ONLY_COOLDOWN_MINUTES = int(os.getenv("ALERT_ONLY_COOLDOWN_MINUTES", "20"))
ENABLE_HOURLY_DISCORD_PERF_REPORT = os.getenv("ENABLE_HOURLY_DISCORD_PERF_REPORT", "1") == "1"
HOURLY_REPORT_MINUTE_WINDOW = int(os.getenv("HOURLY_REPORT_MINUTE_WINDOW", "2"))
ENABLE_MORNING_BRIEFING = os.getenv("ENABLE_MORNING_BRIEFING", "1") == "1"
MORNING_BRIEFING_HOUR_CT = int(os.getenv("MORNING_BRIEFING_HOUR_CT", "8"))
MORNING_BRIEFING_MINUTE_CT = int(os.getenv("MORNING_BRIEFING_MINUTE_CT", "35"))
ENABLE_MIDDAY_BRIEFING = os.getenv("ENABLE_MIDDAY_BRIEFING", "1") == "1"
MIDDAY_BRIEFING_HOUR_CT = int(os.getenv("MIDDAY_BRIEFING_HOUR_CT", "12"))
MIDDAY_BRIEFING_MINUTE_CT = int(os.getenv("MIDDAY_BRIEFING_MINUTE_CT", "5"))
BRIEFING_STATE_FILE = os.getenv("BRIEFING_STATE_FILE", "briefing_sent_state.json")
MORNING_BRIEFING_NEWS_LIMIT = int(os.getenv("MORNING_BRIEFING_NEWS_LIMIT", "40"))
MORNING_BRIEFING_HEADLINES_PER_SECTION = int(os.getenv("MORNING_BRIEFING_HEADLINES_PER_SECTION", "3"))
MORNING_BRIEFING_NEWS_SYMBOLS = os.getenv(
    "MORNING_BRIEFING_NEWS_SYMBOLS",
    "SPY,QQQ,IWM,DIA,TLT,GLD,USO,AAPL,NVDA,MSFT,AMZN,META,TSLA",
)
ENABLE_STATE_TRANSITION_ALERTS = os.getenv("ENABLE_STATE_TRANSITION_ALERTS", "0") == "1"
TRANSITION_ALERT_MIN_TIER = os.getenv("TRANSITION_ALERT_MIN_TIER", "SIGNAL").strip().upper()
TRANSITION_ALERT_COOLDOWN_SECONDS = int(os.getenv("TRANSITION_ALERT_COOLDOWN_SECONDS", "90"))
ENABLE_PRIORITY_SCANNING = os.getenv("ENABLE_PRIORITY_SCANNING", "1") == "1"

# Paper-trading execution. When ENABLE_ALPACA_PAPER_TRADING=1 the bot will
# submit a paper-account market BUY when a STRONG signal fires, then poll the
# option price each cycle and submit a paper-account market SELL at +20% / -20%.
# Set to 0 to keep the bot in pure alert mode (no orders submitted, no tracking).
ENABLE_ALPACA_PAPER_TRADING = os.getenv("ENABLE_ALPACA_PAPER_TRADING", "1") == "1"
PROFIT_TARGET_PCT = float(os.getenv("PROFIT_TARGET_PCT", "0.20"))  # take-profit at +20%
STOP_LOSS_PCT     = float(os.getenv("STOP_LOSS_PCT",     "0.25"))  # emergency option-premium stop; thesis invalidation exits earlier
# Adaptive exit profile (expectancy-focused, not trade-count suppression).
PARTIAL_TP_PCT = float(os.getenv("PARTIAL_TP_PCT", "0.12"))
PARTIAL_CLOSE_FRACTION = float(os.getenv("PARTIAL_CLOSE_FRACTION", "0.70"))
TRAILING_STOP_GIVEBACK_PCT = float(os.getenv("TRAILING_STOP_GIVEBACK_PCT", "0.10"))
THESIS_STRUCTURE_TOLERANCE_PCT = float(os.getenv("THESIS_STRUCTURE_TOLERANCE_PCT", "0.0010"))
THESIS_SWING_BREAK_TOLERANCE_PCT = float(os.getenv("THESIS_SWING_BREAK_TOLERANCE_PCT", "0.0008"))
THESIS_LEVEL_TOLERANCE_PCT = float(os.getenv("THESIS_LEVEL_TOLERANCE_PCT", "0.0012"))
THESIS_REVERSAL_SLOPE_PCT = float(os.getenv("THESIS_REVERSAL_SLOPE_PCT", "0.0005"))
EMERGENCY_DATA_FAIL_EXIT_ENABLED = os.getenv("EMERGENCY_DATA_FAIL_EXIT_ENABLED", "1") == "1"
EMERGENCY_DATA_FAIL_CYCLES = int(os.getenv("EMERGENCY_DATA_FAIL_CYCLES", "3"))
RUNNER_UNDERLYING_TRAIL_PCT = float(os.getenv("RUNNER_UNDERLYING_TRAIL_PCT", "0.0035"))
# Second, independent guard alongside the underlying runner trail: protects the
# option's own P&L peak once a runner has shown strong gains, since delta/gamma/IV
# decay can erode option profit well before the underlying thesis actually breaks.
RUNNER_PNL_TRAIL_ACTIVATE = float(os.getenv("RUNNER_PNL_TRAIL_ACTIVATE", "0.15"))
RUNNER_PNL_MAX_GIVEBACK = float(os.getenv("RUNNER_PNL_MAX_GIVEBACK", "0.08"))
# High-conviction runner profile (applies only when entry quality is strong).
RUNNER_EXIT_PROFILE_ENABLED = os.getenv("RUNNER_EXIT_PROFILE_ENABLED", "1") == "1"
RUNNER_MIN_SCORE = int(os.getenv("RUNNER_MIN_SCORE", "75"))
RUNNER_MIN_MOMENTUM_QUALITY = float(os.getenv("RUNNER_MIN_MOMENTUM_QUALITY", "8.5"))
RUNNER_MIN_IGNITION_DELTA = int(os.getenv("RUNNER_MIN_IGNITION_DELTA", "16"))
RUNNER_TARGET_PCT = float(os.getenv("RUNNER_TARGET_PCT", "0.24"))
RUNNER_PARTIAL_TP_PCT = float(os.getenv("RUNNER_PARTIAL_TP_PCT", "0.14"))
RUNNER_PARTIAL_CLOSE_FRACTION = float(os.getenv("RUNNER_PARTIAL_CLOSE_FRACTION", "0.35"))
RUNNER_TRAILING_GIVEBACK_PCT = float(os.getenv("RUNNER_TRAILING_GIVEBACK_PCT", "0.08"))
MAX_TRADE_HOLD_MINUTES = int(os.getenv("MAX_TRADE_HOLD_MINUTES", "90"))
# Regime-aware target/stop profile.
ADAPTIVE_EXIT_PROFILE_ENABLED = os.getenv("ADAPTIVE_EXIT_PROFILE_ENABLED", "1") == "1"
HIGH_VOL_RATIO = float(os.getenv("HIGH_VOL_RATIO", "1.50"))
LOW_VOL_RATIO = float(os.getenv("LOW_VOL_RATIO", "0.90"))
HIGH_VOL_TARGET_PCT = float(os.getenv("HIGH_VOL_TARGET_PCT", "0.24"))
HIGH_VOL_STOP_PCT = float(os.getenv("HIGH_VOL_STOP_PCT", "0.14"))
LOW_VOL_TARGET_PCT = float(os.getenv("LOW_VOL_TARGET_PCT", "0.16"))
LOW_VOL_STOP_PCT = float(os.getenv("LOW_VOL_STOP_PCT", "0.14"))
# Option contract ranking preferences.
TARGET_OPTION_DELTA_MIN = float(os.getenv("TARGET_OPTION_DELTA_MIN", "0.45"))
TARGET_OPTION_DELTA_MAX = float(os.getenv("TARGET_OPTION_DELTA_MAX", "0.65"))
# Hard floor/ceiling — contracts outside this band are never selected, regardless of OI/spread.
OPTION_ACCEPTABLE_DELTA_MIN = float(os.getenv("OPTION_ACCEPTABLE_DELTA_MIN", "0.35"))
OPTION_ACCEPTABLE_DELTA_MAX = float(os.getenv("OPTION_ACCEPTABLE_DELTA_MAX", "0.75"))
# Below the acceptable band, only allow contracts with excellent liquidity — never on OI alone.
OPTION_FALLBACK_DELTA_MIN = float(os.getenv("OPTION_FALLBACK_DELTA_MIN", "0.30"))
OPTION_FALLBACK_MIN_VOLUME = float(os.getenv("OPTION_FALLBACK_MIN_VOLUME", "500"))
OPTION_FALLBACK_MIN_OI = float(os.getenv("OPTION_FALLBACK_MIN_OI", "5000"))
OPTION_FALLBACK_MAX_SPREAD_PCT = float(os.getenv("OPTION_FALLBACK_MAX_SPREAD_PCT", "0.10"))
OPTION_RANK_SPREAD_WEIGHT = float(os.getenv("OPTION_RANK_SPREAD_WEIGHT", "0.50"))
OPTION_RANK_LIQUIDITY_WEIGHT = float(os.getenv("OPTION_RANK_LIQUIDITY_WEIGHT", "0.35"))
OPTION_RANK_DELTA_WEIGHT = float(os.getenv("OPTION_RANK_DELTA_WEIGHT", "0.15"))
OPTION_RANK_STRIKE_DISTANCE_WEIGHT = float(os.getenv("OPTION_RANK_STRIKE_DISTANCE_WEIGHT", "0.45"))
OPTION_MAX_STRIKE_DISTANCE_PCT = float(os.getenv("OPTION_MAX_STRIKE_DISTANCE_PCT", "0.012"))
OPTION_RELAXED_FALLBACK_ALERT_ONLY = os.getenv("OPTION_RELAXED_FALLBACK_ALERT_ONLY", "1") == "1"
# Entry-quality confluence: compensating multi-factor check (not a single pass/fail score).
ENTRY_CONFLUENCE_ENABLED = os.getenv("ENTRY_CONFLUENCE_ENABLED", "1") == "1"
ENTRY_CONFLUENCE_MIN_POINTS = float(os.getenv("ENTRY_CONFLUENCE_MIN_POINTS", "7.0"))  # out of 12
V2_ENTRY_QUALITY_ENABLED = os.getenv("V2_ENTRY_QUALITY_ENABLED", "1") == "1"
V2_ENTRY_QUALITY_MIN_SCORE = float(os.getenv("V2_ENTRY_QUALITY_MIN_SCORE", "70.0"))
V2_ENTRY_WEIGHT_TREND = float(os.getenv("V2_ENTRY_WEIGHT_TREND", "25.0"))
V2_ENTRY_WEIGHT_LOCATION = float(os.getenv("V2_ENTRY_WEIGHT_LOCATION", "20.0"))
V2_ENTRY_WEIGHT_SETUP = float(os.getenv("V2_ENTRY_WEIGHT_SETUP", "20.0"))
V2_ENTRY_WEIGHT_TRIGGER = float(os.getenv("V2_ENTRY_WEIGHT_TRIGGER", "15.0"))
V2_ENTRY_WEIGHT_CONTEXT_RS = float(os.getenv("V2_ENTRY_WEIGHT_CONTEXT_RS", "10.0"))
V2_ENTRY_WEIGHT_CONTRACT = float(os.getenv("V2_ENTRY_WEIGHT_CONTRACT", "10.0"))
# Shadow measurement only — never gates or blocks a trade. Classifies each V2-evaluated
# candidate into: BOTH_PASS, V2_ONLY (V2 admits, legacy would have blocked), or
# LEGACY_ONLY (legacy would have taken it, V2 rejects it).
V2_ATTRIBUTION_LOG_ENABLED = os.getenv("V2_ATTRIBUTION_LOG_ENABLED", "1") == "1"
V2_ATTRIBUTION_LOG_FILE = os.getenv("V2_ATTRIBUTION_LOG_FILE", "v2_entry_attribution_log.csv")
# Measurement only — logs Trend/Location/Setup/Trigger/Context before contract search,
# so a rejected contract search doesn't hide what V2 saw in the underlying setup.
V2_PRE_CONTRACT_LOG_ENABLED = os.getenv("V2_PRE_CONTRACT_LOG_ENABLED", "1") == "1"
# Contract-search cache: avoids re-running the full Alpaca chain/snapshot query
# (which can take 10-20s) for the same symbol/side/price within a short window.
# Pure performance measure — does not change which contracts are selected/rejected.
OPTION_SEARCH_CACHE_ENABLED = os.getenv("OPTION_SEARCH_CACHE_ENABLED", "1") == "1"
OPTION_SEARCH_CACHE_TTL_SECONDS = int(os.getenv("OPTION_SEARCH_CACHE_TTL_SECONDS", "20"))
# A "no valid contract" result must expire much faster than a successful selection —
# option quotes/spreads can turn tradable again within seconds, and an entry-ready
# candidate (playbook already passed) should never be killed by a stale negative.
OPTION_SEARCH_NEGATIVE_CACHE_TTL_SECONDS = int(os.getenv("OPTION_SEARCH_NEGATIVE_CACHE_TTL_SECONDS", "4"))
# Fresh-setup-after-loss: require new evidence before re-entering the same (symbol, side)
# after a stop-out, instead of a blanket block or an unconditional re-entry.
FRESH_SETUP_AFTER_LOSS_ENABLED = os.getenv("FRESH_SETUP_AFTER_LOSS_ENABLED", "1") == "1"
FRESH_SETUP_LOOKBACK_MINUTES = int(os.getenv("FRESH_SETUP_LOOKBACK_MINUTES", "240"))
FRESH_SETUP_MIN_IGNITION_DELTA = int(os.getenv("FRESH_SETUP_MIN_IGNITION_DELTA", "20"))
FRESH_SETUP_MIN_REQUIRED_SIGNALS = int(os.getenv("FRESH_SETUP_MIN_REQUIRED_SIGNALS", "2"))
FRESH_SETUP_PRICE_RESET_PCT = float(os.getenv("FRESH_SETUP_PRICE_RESET_PCT", "0.0015"))  # 0.15%
# Legacy profit-protection env vars are retained for backward compatibility,
# but are intentionally not used in exit logic.
PROFIT_PROTECT_ARM_PCT = float(os.getenv("PROFIT_PROTECT_ARM_PCT", "0.04"))
PROFIT_PROTECT_FLOOR_PCT = float(os.getenv("PROFIT_PROTECT_FLOOR_PCT", "0.00"))
PROFIT_PROTECT_DRAWDOWN_PCT = float(os.getenv("PROFIT_PROTECT_DRAWDOWN_PCT", "0.08"))
MAX_OPEN_TRADES   = int(os.getenv("MAX_OPEN_TRADES",   "250"))
MAX_SAME_DIRECTION_POSITIONS = int(os.getenv("MAX_SAME_DIRECTION_POSITIONS", "2"))
MAX_SAME_DIRECTION_ETF_POSITIONS = int(os.getenv("MAX_SAME_DIRECTION_ETF_POSITIONS", "1"))
MAX_SAME_DIRECTION_STOCK_POSITIONS = int(os.getenv("MAX_SAME_DIRECTION_STOCK_POSITIONS", "1"))
# Block stacking multiple strikes on the same symbol+side while one is open.
SINGLE_POSITION_PER_SYMBOL = os.getenv("SINGLE_POSITION_PER_SYMBOL", "1") == "1"
# Rehydrate in-memory trade tracking from Alpaca open positions after restarts.
RECOVER_OPEN_POSITIONS = os.getenv("RECOVER_OPEN_POSITIONS", "1") == "1"
BASE_POSITION_QTY = int(os.getenv("POSITION_QTY",      "1"))
MIN_POSITION_QTY  = int(os.getenv("MIN_POSITION_QTY",  "5"))
MAX_POSITION_QTY  = int(os.getenv("MAX_POSITION_QTY",  "10"))
CONFIDENCE_POSITIONING = os.getenv("CONFIDENCE_POSITIONING", "1") == "1"
CONFIDENCE_STEP_SCORE  = int(os.getenv("CONFIDENCE_STEP_SCORE", "5"))
# Grade-based position sizing: qty scales A(6) → B(4) → C(3) → D(2) by entry score.
GRADE_BASED_QTY   = os.getenv("GRADE_BASED_QTY",   "1") == "1"
GRADE_QTY_A       = int(os.getenv("GRADE_QTY_A",    "6"))   # highest confidence
GRADE_QTY_B       = int(os.getenv("GRADE_QTY_B",    "4"))
GRADE_QTY_C       = int(os.getenv("GRADE_QTY_C",    "3"))
GRADE_QTY_D       = int(os.getenv("GRADE_QTY_D",    "2"))   # lowest confidence
GRADE_SCORE_A     = int(os.getenv("GRADE_SCORE_A",  "95"))  # score >= 95 → A
GRADE_SCORE_B     = int(os.getenv("GRADE_SCORE_B",  "88"))  # score >= 88 → B
GRADE_SCORE_C     = int(os.getenv("GRADE_SCORE_C",  "83"))  # score >= 83 → C  (typical gate)
TRADE_LOG_FILE    = os.getenv("TRADE_LOG_FILE", "trade_results.csv")

# Option quality filters (stock-only tightened defaults; ETFs remain baseline).
ETF_MIN_OPTION_BID = float(os.getenv("ETF_MIN_OPTION_BID", "0.15"))
ETF_MAX_OPTION_SPREAD_PCT = float(os.getenv("ETF_MAX_OPTION_SPREAD_PCT", "0.05"))
ETF_MAX_OPTION_BID = float(os.getenv("ETF_MAX_OPTION_BID", "8.00"))
STOCK_MIN_OPTION_BID = float(os.getenv("STOCK_MIN_OPTION_BID", "0.20"))
STOCK_MAX_OPTION_SPREAD_PCT = float(os.getenv("STOCK_MAX_OPTION_SPREAD_PCT", "0.05"))
STOCK_MAX_OPTION_BID = float(os.getenv("STOCK_MAX_OPTION_BID", "8.00"))
STOCK_MIN_OPTION_VOLUME = int(os.getenv("STOCK_MIN_OPTION_VOLUME", "10"))
STOCK_MIN_OPTION_OPEN_INTEREST = int(os.getenv("STOCK_MIN_OPTION_OPEN_INTEREST", "50"))
ENTRY_EXPENSIVE_EXTENSION_FILTER = os.getenv("ENTRY_EXPENSIVE_EXTENSION_FILTER", "1") == "1"
ENTRY_EXPENSIVE_EXTENSION_BID_FLOOR = float(os.getenv("ENTRY_EXPENSIVE_EXTENSION_BID_FLOOR", "4.00"))
ENTRY_EXPENSIVE_EXTENSION_VWAP_RATIO = float(os.getenv("ENTRY_EXPENSIVE_EXTENSION_VWAP_RATIO", "0.80"))
ENTRY_PREMIUM_EXCEPTION_SCORE = int(os.getenv("ENTRY_PREMIUM_EXCEPTION_SCORE", "95"))
ENTRY_PREMIUM_EXCEPTION_MIN_IGNITION_DELTA = int(os.getenv("ENTRY_PREMIUM_EXCEPTION_MIN_IGNITION_DELTA", "20"))
EARLY_IGNITION_PHASE_FILTER = os.getenv("EARLY_IGNITION_PHASE_FILTER", "1") == "1"
EARLY_IGNITION_MIN_DELTA_5M = int(os.getenv("EARLY_IGNITION_MIN_DELTA_5M", "2"))
ENTRY_MIN_IGNITION_DELTA = int(os.getenv("ENTRY_MIN_IGNITION_DELTA", "15"))  # Relaxed from 18 (was blocking +15 deltas)
ETF_ENTRY_MIN_OPTION_OI = int(os.getenv("ETF_ENTRY_MIN_OPTION_OI", "5000"))  # Relaxed from 8000 (SPY has 4487 OI)
STOCK_ENTRY_MIN_OPTION_OI = int(os.getenv("STOCK_ENTRY_MIN_OPTION_OI", "4000"))
PROMOTED_ETF_ENTRY_MIN_OPTION_OI = int(os.getenv("PROMOTED_ETF_ENTRY_MIN_OPTION_OI", "1500"))
PROMOTED_STOCK_ENTRY_MIN_OPTION_OI = int(os.getenv("PROMOTED_STOCK_ENTRY_MIN_OPTION_OI", "100"))
# OCC open interest is prior-session settlement, so short-dated strikes look illiquid even when actively traded.
ENTRY_VOLUME_SUBSTITUTES_OI = os.getenv("ENTRY_VOLUME_SUBSTITUTES_OI", "1") == "1"
ENTRY_MIN_OPTION_DAY_VOLUME = int(os.getenv("ENTRY_MIN_OPTION_DAY_VOLUME", "1000"))
# When OI/day-volume metadata is stale or missing (see _classify_liquidity_data_state),
# a live bid/ask this tight is accepted as evidence of real tradability instead of an
# automatic veto on stale metadata alone.
LIQUIDITY_SUBSTITUTE_MAX_SPREAD_PCT = float(os.getenv("LIQUIDITY_SUBSTITUTE_MAX_SPREAD_PCT", "0.06"))
# A tight spread only counts as evidence if the quote itself was pulled recently —
# otherwise a stale-but-narrow quote could pass by coincidence, not real liquidity.
LIQUIDITY_SUBSTITUTE_MAX_QUOTE_AGE_SECONDS = float(os.getenv("LIQUIDITY_SUBSTITUTE_MAX_QUOTE_AGE_SECONDS", "20.0"))
PROMOTED_CONT_MIN_MOMENTUM_SCORE = float(os.getenv("PROMOTED_CONT_MIN_MOMENTUM_SCORE", "36"))
PROMOTED_MIN_DOMINANCE = int(os.getenv("PROMOTED_MIN_DOMINANCE", "20"))
PROMOTED_MIN_DELTA_5M = int(os.getenv("PROMOTED_MIN_DELTA_5M", "2"))
PROMOTED_MIN_MOMENTUM_QUALITY = float(os.getenv("PROMOTED_MIN_MOMENTUM_QUALITY", "7.0"))
ENTRY_ALLOWED_SETUPS = {
    s.strip().upper()
    for s in os.getenv(
        "ENTRY_ALLOWED_SETUPS",
        "EARLY_BREAKOUT,FIRST_PULLBACK,VWAP_RECLAIM,HIGHER_LOW_RECLAIM,MOMENTUM_CONTINUATION,BREAKDOWN,BREAKDOWN_RETEST,LOWER_HIGH_FAILURE,VWAP_REJECT",
    ).split(",")
    if s.strip()
}

# Real 1-minute sniper timing engine: 5m finds the setup, 1m times the entry.
ONE_MINUTE_ENTRY_ENABLED = os.getenv("ONE_MINUTE_ENTRY_ENABLED", "1") == "1"
ONE_MINUTE_LOOKBACK_BARS = int(os.getenv("ONE_MINUTE_LOOKBACK_BARS", "60"))
ONE_MINUTE_EMA9_TOUCH_TOLERANCE = float(os.getenv("ONE_MINUTE_EMA9_TOUCH_TOLERANCE", "0.0015"))
ONE_MINUTE_LEVEL_RETEST_TOLERANCE = float(os.getenv("ONE_MINUTE_LEVEL_RETEST_TOLERANCE", "0.0015"))
ONE_MINUTE_MIN_VOLUME_RATIO = float(os.getenv("ONE_MINUTE_MIN_VOLUME_RATIO", "1.20"))
# Stronger FIRST_PULLBACK confirmation: require the 5m directional score to still be building.
FIRST_PULLBACK_MIN_SCORE_DELTA = int(os.getenv("FIRST_PULLBACK_MIN_SCORE_DELTA", "2"))
# Minimum 1m trigger-candle follow-through as a fraction of price.
ONE_MINUTE_MIN_TRIGGER_MOVE_PCT = float(os.getenv("ONE_MINUTE_MIN_TRIGGER_MOVE_PCT", "0.0005"))
ONE_MINUTE_COMPRESSION_PCT = float(os.getenv("ONE_MINUTE_COMPRESSION_PCT", "0.0035"))
ONE_MINUTE_MAX_TRIGGER_AGE_BARS = int(os.getenv("ONE_MINUTE_MAX_TRIGGER_AGE_BARS", "6"))
# Reclaim confirmation window: look for a bullish/bearish reclaim across the last N closed
# 1m bars instead of requiring the single latest bar to beat the immediately prior bar's
# high/low (audit showed that single-bar rule was the dominant 1m-sniper blocker).
ONE_MINUTE_RECLAIM_WINDOW_BARS = int(os.getenv("ONE_MINUTE_RECLAIM_WINDOW_BARS", "3"))
ONE_MINUTE_REQUIRE_CLOSED_BAR = os.getenv("ONE_MINUTE_REQUIRE_CLOSED_BAR", "1") == "1"
ONE_MINUTE_BYPASS_5M_BREAKOUT_HOLD = os.getenv("ONE_MINUTE_BYPASS_5M_BREAKOUT_HOLD", "1") == "1"
SNIPER_WATCH_ALERT_COOLDOWN_MINUTES = int(os.getenv("SNIPER_WATCH_ALERT_COOLDOWN_MINUTES", "30"))

# Stricter confirmation for recovery-style CALL entries to reduce weak bounce losses.
RECOVERY_CALL_STRICT_ENABLED = os.getenv("RECOVERY_CALL_STRICT_ENABLED", "1") == "1"
RECOVERY_CALL_MIN_MOMENTUM_SCORE = float(os.getenv("RECOVERY_CALL_MIN_MOMENTUM_SCORE", "50"))
RECOVERY_CALL_MIN_MOMENTUM_QUALITY = float(os.getenv("RECOVERY_CALL_MIN_MOMENTUM_QUALITY", "8.5"))
RECOVERY_CALL_MIN_DELTA_5M = int(os.getenv("RECOVERY_CALL_MIN_DELTA_5M", "3"))
RECOVERY_CALL_MIN_RSI = float(os.getenv("RECOVERY_CALL_MIN_RSI", "30"))
RECOVERY_CALL_MIN_VWAP_DISTANCE = float(os.getenv("RECOVERY_CALL_MIN_VWAP_DISTANCE", "-0.008"))

# Scores near the entry floor need stronger confirmation than high-conviction calls.
MARGINAL_CALL_GATE_ENABLED = os.getenv("MARGINAL_CALL_GATE_ENABLED", "1") == "1"
MARGINAL_CALL_SCORE_CEIL = int(os.getenv("MARGINAL_CALL_SCORE_CEIL", "69"))
MARGINAL_CALL_MIN_RELATIVE_STRENGTH = float(os.getenv("MARGINAL_CALL_MIN_RELATIVE_STRENGTH", "5.0"))
MARGINAL_CALL_MIN_MOMENTUM = float(os.getenv("MARGINAL_CALL_MIN_MOMENTUM", "10.0"))

# Google Sheets tracking — bot creates/finds a spreadsheet by name automatically.
GOOGLE_SPREADSHEET_NAME   = os.getenv("GOOGLE_SPREADSHEET_NAME", "SPY Options Bot Log")
GOOGLE_SPREADSHEET_ID     = os.getenv("GOOGLE_SPREADSHEET_ID", "")  # paste sheet ID from URL to use existing sheet
FORCE_NEW_GOOGLE_SHEET    = os.getenv("FORCE_NEW_GOOGLE_SHEET", "0") == "1"
GOOGLE_SERVICE_ACCOUNT_EMAIL = os.getenv("GOOGLE_SERVICE_ACCOUNT_EMAIL", "")
GOOGLE_PRIVATE_KEY        = os.getenv("GOOGLE_PRIVATE_KEY", "").replace("\\n", "\n")
OWNER_EMAIL               = os.getenv("OWNER_EMAIL", "")  # your Gmail — sheet is shared to this on startup
GSHEET_RETRY_SECONDS      = int(os.getenv("GSHEET_RETRY_SECONDS", "60"))

# Per-symbol score-trend history (one reading per cycle).
# Use a larger default in websocket mode so 5m/10m deltas are reliably available.
_SCORE_HISTORY_CAP = int(os.getenv("SCORE_HISTORY_CAP", "120"))
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
# (symbol, side) -> datetime after which another near-entry sniper watch alert is allowed.
_sniper_watch_cooldowns: "dict[tuple, datetime]" = {}
# (symbol, side) -> reference state from the most recent losing trade, used to require
# a genuinely fresh setup (not the same chop) before re-entering the same side same day.
_post_loss_setup: "dict[tuple, dict]" = {}
# Authorized gspread Spreadsheet object (None if not configured / failed).
_gsheet: "gspread.Spreadsheet | None" = None
_last_gsheet_init_attempt: "datetime | None" = None
# Last known SPY VWAP side: 'bull', 'bear', or None (populated by run_symbol each cycle).
_spy_vwap_cache: "dict" = {"side": None, "updated_at": None}
# Last known QQQ VWAP side: 'bull', 'bear', or None (populated by run_symbol each cycle).
_qqq_vwap_cache: "dict" = {"side": None, "updated_at": None}
# Ignition confirmation buffer: (symbol, side) -> {"score": int, "delta": int, "at": datetime}
# An ignition must be seen twice (two consecutive scans) before an entry is allowed.
_ignition_pending: "dict[tuple, dict]" = {}
# Initial breakout levels awaiting the next completed five-minute bar to hold.
_breakout_hold_pending: "dict[tuple, dict]" = {}
_breakout_continuations: "dict[tuple, dict]" = {}
# Cached trending stocks and reasons from Alpaca screener/news.
_trending_cache: "dict" = {"updated_at": None, "symbols": [], "reasons": {}}
# Cached tradability/profile checks for trending candidates.
_trending_asset_cache: "dict" = {}
# Cached symbol news context used by entry alerts.
_symbol_news_cache: "dict" = {"updated_at": {}, "items": {}}
# Cached ML model metadata for low-overhead per-symbol gating.
_ml_gate_cache: "dict" = {"path": None, "mtime": None, "model": None, "checked": None}
_auto_retrain_state: "dict" = {
    "running": False,
    "last_run_at": None,
    "last_status": "",
    "last_closed_count": 0,
    "closed_count": 0,
}
_auto_retrain_lock = threading.Lock()
# Last observed state per symbol for transition-only alerting.
_last_symbol_state: "dict[str, dict]" = {}
# Per-symbol timestamp of the most recent transition alert.
_last_transition_alert_at: "dict[str, datetime]" = {}
# Per-symbol opportunity cache used by priority scan ordering.
_symbol_opportunity_cache: "dict[str, dict]" = {}
# WebSocket-driven symbol scheduling state.
_ws_pending_symbols: "set[str]" = set()
_ws_last_eval_at: "dict[str, datetime]" = {}
_ws_pending_lock = threading.Lock()
# Daily performance tracker for hourly Discord summaries (resets by CT date).
_perf_stats: "dict" = {
    "date": None,
    "entries": 0,
    "closed": 0,
    "wins": 0,
    "losses": 0,
    "realized_pnl_dollar": 0.0,
    "realized_pnl_pct_sum": 0.0,
    "gross_win_dollar": 0.0,
    "gross_loss_dollar": 0.0,
    "win_pnl_pct_sum": 0.0,
    "loss_pnl_pct_sum": 0.0,
    "best_win_dollar": 0.0,
    "best_win_symbol": "",
    "worst_loss_dollar": 0.0,
    "worst_loss_symbol": "",
    "last_report_entries": 0,
    "last_report_closed": 0,
    "last_report_wins": 0,
    "last_report_losses": 0,
    "last_report_realized_pnl_dollar": 0.0,
    "last_sent_hour": None,
    "hydrated_for": None,
    "hydrated_source": "",
    "last_hydrate_attempt": None,
}
# Last date for which morning briefing was already sent.
_morning_briefing_sent_date: "date | None" = None
# Last date for which midday briefing was already sent.
_midday_briefing_sent_date: "date | None" = None
_briefing_dispatch_lock = threading.Lock()

# Column headers for the two Google Sheets tabs.
_ALERTS_HEADERS = [
    "Timestamp (CT)", "Symbol", "Signal", "Side", "Price",
    "Bull Score", "Bear Score", "Sentiment", "Ignition Delta",
    "Contract", "Expiry", "DTE", "Strike", "Bid", "Ask", "Last",
    "Option Volume", "Open Interest",
    "Score Components",
    "VWAP", "EMA20", "EMA50", "PDH", "PDL", "Recent High", "Recent Low",
    "Bar Volume", "Bar Vol Avg",
    "1m Trigger", "Trade Status", "P&L ($)", "P&L %", "Exit Reason", "Closed At (CT)", "Updated At (CT)", "Setup Type",
]
_TRADES_HEADERS = [
    "Trade ID", "Symbol", "Entry Price", "Exit Price", "Strike", "Direction",
    "Entry Time", "Exit Time", "Exit Reason", "Status", "Options Expiration",
    "Alpaca Order ID", "P&L", "P&L %", "Duration", "Created At", "Updated At", "Setup Type",
]
_EXIT_REVIEWS_HEADERS = [
    "Review ID", "Status", "Review Due CT", "Reviewed At CT", "Trade ID", "Symbol", "Contract", "Side",
    "Entry Time CT", "Exit Time CT", "Exit Trigger", "Entry Option", "Exit Option", "Option at Review",
    "P&L at Exit %", "Option Move After Exit %", "Underlying at Exit", "Underlying at Review",
    "VWAP at Review", "EMA20 at Review", "2-Bar Move %", "5-Bar Move %", "Assessment", "Notes", "Discord Message ID",
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

        # Exit Reviews stores one observation-only follow-up for each final exit.
        try:
            exit_reviews_ws = _gsheet.worksheet("Exit Reviews")
        except gspread.exceptions.WorksheetNotFound:
            exit_reviews_ws = _gsheet.add_worksheet(title="Exit Reviews", rows=2000, cols=len(_EXIT_REVIEWS_HEADERS))
            log("Created 'Exit Reviews' tab in Google Sheets.")
        try:
            existing_review_headers = exit_reviews_ws.row_values(1)
            if existing_review_headers != _EXIT_REVIEWS_HEADERS:
                exit_reviews_ws.update(
                    range_name="A1",
                    values=[_EXIT_REVIEWS_HEADERS],
                    value_input_option="USER_ENTERED",
                )
                log("Ensured 'Exit Reviews' header row in Google Sheets.")
        except Exception as header_err:
            log(f"Warning: Could not verify/set Exit Reviews headers: {header_err}")
        
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
    """Append one row to the Alerts tab for every STRONG signal that fires.

    Returns the 1-based sheet row index when available, else None.
    """
    if _gsheet is None and not ensure_google_sheets_ready():
        return None
    try:
        breakdown = data["bull_breakdown"] if data["side"] == "CALL" else data["bear_breakdown"]
        components = ", ".join(f"{k} (+{v})" for k, v in breakdown.items())

        if data["side"] == "CALL":
            delta = (data["bull_score"] - data["bull_5m"]) if data["bull_5m"] is not None else ""
        else:
            delta = (data["bear_score"] - data["bear_5m"]) if data["bear_5m"] is not None else ""

        now_ct = datetime.now(central).strftime("%Y-%m-%d %H:%M:%S")
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
            str(data.get("one_minute_trigger", "") or ""),
            "OPEN",
            "",
            "",
            "",
            "",
            now_ct,
            str(data.get("setup_type", _classify_entry_timing(data, data.get("side", ""))) or "UNKNOWN"),
        ]
        ws = _gsheet.worksheet("Alerts")
        ws.append_row(row, value_input_option="USER_ENTERED")
        alert_row = len(ws.col_values(1))
        oi = _safe_int_num((option or {}).get("open_interest", 0), 0)
        log(f"[{symbol}] Alert logged to Google Sheets (ignition={delta:+d} OI={oi:,}).")
        return alert_row
    except Exception as e:
        log(f"[{symbol}] Google Sheets alert log failed: {e}")
        return None


def _find_latest_open_alert_row(contract):
    """Best-effort lookup for the latest OPEN alert row by contract symbol."""
    if _gsheet is None and not ensure_google_sheets_ready():
        return None
    try:
        ws = _gsheet.worksheet("Alerts")
        rows = ws.get_all_values()
        if len(rows) <= 1:
            return None

        headers = rows[0]
        contract_idx = headers.index("Contract")
        status_idx = headers.index("Trade Status") if "Trade Status" in headers else None

        for i in range(len(rows) - 1, 0, -1):
            row = rows[i]
            row_contract = str(row[contract_idx]).strip() if contract_idx < len(row) else ""
            if row_contract != str(contract):
                continue
            row_status = str(row[status_idx]).strip().upper() if (status_idx is not None and status_idx < len(row)) else ""
            if row_status in ("", "OPEN", "RECOVERED"):
                return i + 1
        return None
    except Exception:
        return None


def update_alert_close_to_sheets(row, trade):
    """Mark the corresponding Alerts row as CLOSED and fill realized P&L fields."""
    if _gsheet is None and not ensure_google_sheets_ready():
        return
    try:
        ws = _gsheet.worksheet("Alerts")
        headers = ws.row_values(1)
        if not headers:
            return

        row_idx = trade.get("alerts_row")
        if not row_idx:
            row_idx = _find_latest_open_alert_row(trade.get("contract", ""))
        if not row_idx:
            log(f"[{trade.get('underlying', 'UNKNOWN')}] Alerts close update skipped: no matching OPEN alert row.")
            return

        existing = ws.row_values(int(row_idx))
        needed = len(headers)
        if len(existing) < needed:
            existing.extend([""] * (needed - len(existing)))

        qty = int(row.get("qty", 0) or 0)
        if "_combined_pnl_dollar" in row:
            pnl_dollar = round(float(row["_combined_pnl_dollar"]), 2)
        else:
            pnl_dollar = round((float(row["exit"]) - float(row["entry"])) * float(qty) * 100.0, 2)
        pnl_pct = round(float(row["pnl_pct"]), 2)
        closed_at = row.get("closed_at")
        closed_text = (
            closed_at.strftime("%Y-%m-%d %H:%M:%S")
            if isinstance(closed_at, datetime)
            else str(closed_at or "")
        )
        now_ct = datetime.now(central).strftime("%Y-%m-%d %H:%M:%S")

        index_map = {name: idx for idx, name in enumerate(headers)}
        if "Trade Status" in index_map:
            existing[index_map["Trade Status"]] = "CLOSED"
        if "P&L ($)" in index_map:
            existing[index_map["P&L ($)"]] = pnl_dollar
        if "P&L %" in index_map:
            existing[index_map["P&L %"]] = pnl_pct
        if "Exit Reason" in index_map:
            existing[index_map["Exit Reason"]] = row.get("reason", "")
        if "Closed At (CT)" in index_map:
            existing[index_map["Closed At (CT)"]] = closed_text
        if "Updated At (CT)" in index_map:
            existing[index_map["Updated At (CT)"]] = now_ct

        ws.update(
            range_name=f"A{int(row_idx)}",
            values=[existing],
            value_input_option="USER_ENTERED",
        )
        trade["alerts_row"] = int(row_idx)
        log(f"[{trade.get('underlying', 'UNKNOWN')}] Alerts row {row_idx} marked CLOSED with P&L.")
    except Exception as e:
        log(f"[{trade.get('underlying', 'UNKNOWN')}] Alerts close update failed: {e}")


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
            trade.get("setup_type", trade.get("entry_timing", "UNKNOWN")),
        ]
        # Store entry_ignition_delta and entry_option_oi for future analysis
        trade["sheets_entry_ignition"] = trade.get("entry_ignition_delta", 0)
        trade["sheets_entry_oi"] = trade.get("entry_option_oi", 0)
        ws = _gsheet.worksheet("Trades")
        ws.append_row(sheet_row, value_input_option="USER_ENTERED")
        # Store the row index so close can update it in-place.
        trade["sheets_row"] = len(ws.get_all_values())
        log(f"[{trade['underlying']}] Trade {trade_id} opened → Google Sheets row {trade['sheets_row']} (ignition={trade.get('entry_ignition_delta', 0)} OI={trade.get('entry_option_oi', 0)}).")
    except Exception as e:
        log(f"[{trade['underlying']}] Google Sheets open log failed: {e}")


def log_trade_to_sheets(row, trade, final_close=True):
    """Write trade exit details to Trades sheet.

    - final_close=True: update the original OPEN row in-place to CLOSED.
    - final_close=False: append a PARTIAL audit row and keep OPEN row intact.
    """
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
        qty = int(row.get("qty", 1) or 1)
        if "_combined_pnl_dollar" in row:
            pnl_dollar = round(float(row["_combined_pnl_dollar"]), 2)
        else:
            pnl_dollar = round((row["exit"] - row["entry"]) * 100 * float(max(1, qty)), 2)
        pnl_pct = round(row["pnl_pct"], 2)
        trade_id = str(trade.get("trade_id", "") or "").strip()
        if not trade_id:
            trade_id = str(uuid.uuid4())[:8].upper()
            trade["trade_id"] = trade_id
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
            "CLOSED" if final_close else "PARTIAL",             # Status
            trade.get("expiry", ""),                            # Options Expiration
            trade.get("order_id", ""),                          # Alpaca Order ID
            pnl_dollar,                                         # P&L ($)
            pnl_pct,                                            # P&L %
            duration,                                           # Duration (min)
            opened.strftime("%Y-%m-%d %H:%M:%S") if isinstance(opened, datetime) else str(opened),  # Created At
            now,                                                # Updated At
            trade.get("setup_type", trade.get("entry_timing", "UNKNOWN")),
        ]

        ws = _gsheet.worksheet("Trades")
        sheets_row = trade.get("sheets_row")
        if final_close and sheets_row:
            ws.update(
                range_name=f"A{sheets_row}",
                values=[full_row],
                value_input_option="USER_ENTERED",
            )
            log(f"[{trade_id}] Trade closed → Google Sheets row {sheets_row} updated.")
        else:
            # Partial closes (or missing open row ref) append an audit row.
            ws.append_row(full_row, value_input_option="USER_ENTERED")
            suffix = "partial" if not final_close else "close"
            log(f"[{trade_id}] Trade {suffix} → Google Sheets appended.")
    except Exception as e:
        log(f"[{row['underlying']}] Google Sheets trade close update failed: {e}")


def _partial_totals_from_trades_sheet(trade):
    """Return cumulative partial-leg dollar/cost totals for a trade_id from Trades sheet.

    This is a restart-safe fallback when in-memory partial fields are missing.
    """
    trade_id = str(trade.get("trade_id", "") or "").strip()
    if not trade_id:
        return 0, 0.0, 0.0
    if _gsheet is None and not ensure_google_sheets_ready():
        return 0, 0.0, 0.0

    try:
        ws = _gsheet.worksheet("Trades")
        values = ws.get_all_values()
        if not values:
            return 0, 0.0, 0.0

        headers = values[0]
        idx = {name: i for i, name in enumerate(headers)}
        tid_idx = idx.get("Trade ID")
        status_idx = idx.get("Status")
        pnl_d_idx = idx.get("P&L")
        pnl_pct_idx = idx.get("P&L %")
        if None in (tid_idx, status_idx, pnl_d_idx, pnl_pct_idx):
            return 0, 0.0, 0.0

        partial_rows = 0
        total_dollar = 0.0
        total_cost = 0.0
        for row in values[1:]:
            if tid_idx >= len(row) or status_idx >= len(row):
                continue
            if str(row[tid_idx]).strip() != trade_id:
                continue
            if str(row[status_idx]).strip().upper() != "PARTIAL":
                continue

            pnl_d = _safe_float_num(row[pnl_d_idx] if pnl_d_idx < len(row) else 0.0, 0.0)
            pnl_pct = _safe_float_num(row[pnl_pct_idx] if pnl_pct_idx < len(row) else 0.0, 0.0)
            total_dollar += float(pnl_d)
            if abs(float(pnl_pct)) > 1e-9:
                total_cost += float(pnl_d) / (float(pnl_pct) / 100.0)
            partial_rows += 1

        return partial_rows, float(total_dollar), float(total_cost)
    except Exception:
        return 0, 0.0, 0.0


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
DISCORD_COLOR_CALL = 0x2ECC71
DISCORD_COLOR_PUT = 0xE74C3C
DISCORD_COLOR_WARN = 0xF1C40F
_TIER_RANK = {"NONE": 0, "WATCH": 1, "SIGNAL": 2, "STRONG": 3}


def send_discord(message, color=None, reply_to_message_id=None, wait_for_response=False, webhook_url=None):
    webhook_url = webhook_url or DISCORD_WEBHOOK_URL
    if not webhook_url:
        print("Missing Discord webhook.")
        return None
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

    if reply_to_message_id:
        payload["message_reference"] = {
            "message_id": str(reply_to_message_id),
            "fail_if_not_exists": False,
        }

    try:
        if wait_for_response and "wait=true" not in webhook_url:
            sep = "&" if "?" in webhook_url else "?"
            webhook_url = f"{webhook_url}{sep}wait=true"

        r = requests.post(webhook_url, json=payload, timeout=10)
        if r.status_code not in (200, 204):
            print(f"Discord post returned {r.status_code}: {r.text[:100]}", flush=True)
            return None

        # When webhook is called with wait=true, Discord returns the message object.
        if wait_for_response:
            try:
                return r.json()
            except Exception:
                return None
        return None
    except Exception as e:
        print(f"Discord post failed: {e}")
        return None


def append_discord_entry_update(entry_message_id, update, color=None, webhook_url=None):
    """Append a trade status update to the original Discord entry embed."""
    webhook_url = webhook_url or DISCORD_WEBHOOK_URL
    if not webhook_url or not entry_message_id:
        return False

    message_url = f"{webhook_url.split('?', 1)[0]}/messages/{entry_message_id}"
    try:
        response = requests.get(message_url, timeout=10)
        if response.status_code != 200:
            print(f"Discord entry fetch returned {response.status_code}: {response.text[:100]}", flush=True)
            return False

        original = response.json()
        embeds = original.get("embeds") or []
        if embeds:
            description = str(embeds[0].get("description", "") or "")
        else:
            description = str(original.get("content", "") or "")
        description = f"{description}\n\n------------------------------\n{update}".strip()
        if len(description) > 4000:
            description = description[-4000:]

        payload = {"embeds": [{"description": description, "color": int(color or DISCORD_COLOR_WARN)}]}
        response = requests.patch(message_url, json=payload, timeout=10)
        if response.status_code not in (200, 204):
            print(f"Discord entry update returned {response.status_code}: {response.text[:100]}", flush=True)
            return False
        return True
    except Exception as e:
        print(f"Discord entry update failed: {e}")
        return False


def send_discord_bot_reply(entry_message_id, message, color=None):
    """Send a native Discord bot reply to an original webhook entry alert."""
    if not DISCORD_BOT_TOKEN or not entry_message_id:
        return False

    channel_id = DISCORD_CHANNEL_ID
    if not channel_id:
        try:
            message_url = f"{DISCORD_WEBHOOK_URL.split('?', 1)[0]}/messages/{entry_message_id}"
            original = requests.get(message_url, timeout=10)
            if original.status_code == 200:
                channel_id = str((original.json() or {}).get("channel_id", "") or "")
        except Exception as e:
            print(f"Discord reply channel lookup failed: {e}", flush=True)
    if not channel_id:
        print("Discord bot reply skipped: original entry channel is unavailable.", flush=True)
        return False

    payload = {
        "embeds": [{"description": message[:4000], "color": int(color or DISCORD_COLOR_WARN)}],
        "message_reference": {
            "message_id": str(entry_message_id),
            "channel_id": channel_id,
            "fail_if_not_exists": False,
        },
    }
    try:
        response = requests.post(
            f"https://discord.com/api/v10/channels/{channel_id}/messages",
            json=payload,
            headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
            timeout=10,
        )
        if response.status_code not in (200, 201):
            print(f"Discord bot reply returned {response.status_code}: {response.text[:100]}", flush=True)
            return False
        return True
    except Exception as e:
        print(f"Discord bot reply failed: {e}")
        return False


def send_discord_chart_reply(entry_message_id, chart_bytes, filename, caption):
    """Attach a chart image as a native reply to the original entry alert."""
    if not DISCORD_BOT_TOKEN or not entry_message_id or not chart_bytes:
        return False
    channel_id = DISCORD_CHANNEL_ID
    if not channel_id:
        return False
    payload = {
        "content": caption[:2000],
        "message_reference": {
            "message_id": str(entry_message_id),
            "channel_id": channel_id,
            "fail_if_not_exists": False,
        },
    }
    try:
        response = requests.post(
            f"https://discord.com/api/v10/channels/{channel_id}/messages",
            data={"payload_json": json.dumps(payload)},
            files={"files[0]": (filename, chart_bytes, "image/png")},
            headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
            timeout=15,
        )
        if response.status_code not in (200, 201):
            print(f"Discord chart reply returned {response.status_code}: {response.text[:100]}", flush=True)
            return False
        return True
    except Exception as e:
        print(f"Discord chart reply failed: {e}")
        return False


def _trade_progress_reason(trade, current_price, pnl_pct):
    """Build a concise progress reason string for milestone alerts."""
    entry = float(trade.get("entry", 0.0) or 0.0)
    target = float(trade.get("target", 0.0) or 0.0)
    stop = float(trade.get("stop", 0.0) or 0.0)
    max_pnl = float(trade.get("max_pnl_pct", pnl_pct) or pnl_pct)

    if target > 0:
        to_target = ((target - float(current_price)) / target) * 100.0
    else:
        to_target = 0.0

    if entry > 0:
        cushion = ((float(current_price) - stop) / entry) * 100.0
    else:
        cushion = 0.0

    return (
        f"Momentum follow-through: trade is +{pnl_pct * 100:.2f}% from entry, "
        f"max seen +{max_pnl * 100:.2f}%, "
        f"target gap {to_target:+.2f}%, stop cushion {cushion:+.2f}%."
    )


def maybe_send_trade_progress_alert(trade, current_price, pnl_pct):
    """Send a one-time +10% progress update, preferably as a reply to entry alert."""
    sent = bool(trade.get("milestone_10_sent", False))
    if sent:
        return
    if pnl_pct < 0.10:
        return

    side = str(trade.get("side", "")).upper()
    color = DISCORD_COLOR_CALL if side == "CALL" else DISCORD_COLOR_PUT
    strike_val = float(trade.get("strike", 0) or 0)
    if strike_val <= 0:
        parsed_strike = _strike_from_contract(trade.get("contract", ""))
        if parsed_strike is not None:
            strike_val = float(parsed_strike)
    strike_text = str(int(strike_val)) if abs(strike_val - int(strike_val)) < 1e-9 else f"{strike_val:.2f}"
    header = f"{trade.get('underlying', 'UNKNOWN')} strike {strike_text}"
    reason = _trade_progress_reason(trade, current_price, pnl_pct)
    opened = trade.get("opened_at")
    opened_text = opened.strftime("%Y-%m-%d %H:%M:%S %Z") if isinstance(opened, datetime) else str(opened)

    milestone_message = (
        f"\U0001f4c8 **MILESTONE — {header} | {side} | +10%**\n\n"
        f"\U0001f4b2 **Current Price:** `${float(current_price):.2f}`\n"
        f"\U0001f4ca **PnL:** `{pnl_pct * 100:+.2f}%`\n"
        f"\U0001f9e0 **Reason:** `{reason}`\n"
        f"\U0001f4cc **Opened:** `{opened_text}`"
    )
    sent = send_discord_bot_reply(trade.get("entry_message_id"), milestone_message, color=color)
    if not sent:
        sent = append_discord_entry_update(
            trade.get("entry_message_id"),
            milestone_message,
            color=color,
            webhook_url=DISCORD_WEBHOOK_LIVE_TRADES_URL,
        )
    trade["milestone_10_sent"] = True
    log(f"[{trade.get('underlying', 'UNKNOWN')}] Milestone {'sent' if sent else 'not sent'} (+10%): {trade.get('contract', '')}")


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


def _qqq_vwap_side():
    """Return the current QQQ macro side: 'bull' if QQQ > VWAP, 'bear' if QQQ < VWAP."""
    return _qqq_vwap_cache.get("side")


def _market_regime_scores(symbol, price, vwap, ema20, ema50, ema20_rising, ema20_falling):
    """Blend local trend with SPY/QQQ context into bull and bear regime scores."""
    def _clamp01(x):
        return max(0.0, min(1.0, float(x)))

    def _to_100(x):
        return _clamp01(x) * 100.0

    local_bull = (
        (1.0 if price > vwap else 0.0)
        + (1.0 if price > ema20 else 0.0)
        + (1.0 if price > ema50 else 0.0)
        + (1.0 if ema20_rising else 0.0)
    ) / 4.0
    local_bear = (
        (1.0 if price < vwap else 0.0)
        + (1.0 if price < ema20 else 0.0)
        + (1.0 if price < ema50 else 0.0)
        + (1.0 if ema20_falling else 0.0)
    ) / 4.0

    macro_bull_votes = 0.0
    macro_bear_votes = 0.0
    for side in (_spy_vwap_side(), _qqq_vwap_side()):
        if side == "bull":
            macro_bull_votes += 1.0
        elif side == "bear":
            macro_bear_votes += 1.0

    macro_bull = macro_bull_votes / 2.0
    macro_bear = macro_bear_votes / 2.0

    if symbol in ETF_SYMBOLS:
        bull = _to_100((local_bull * 0.65) + (macro_bull * 0.35))
        bear = _to_100((local_bear * 0.65) + (macro_bear * 0.35))
    else:
        bull = _to_100((local_bull * 0.50) + (macro_bull * 0.35) + (0.15 if _spy_vwap_side() == "bull" else 0.0))
        bear = _to_100((local_bear * 0.50) + (macro_bear * 0.35) + (0.15 if _spy_vwap_side() == "bear" else 0.0))

    return bull, bear


def symbol_profile(symbol):
    """Return per-symbol entry tuning so single stocks use stricter filters than ETFs."""
    profile = {
        "ignition_min_delta": IGNITION_MIN_DELTA,
        "rsi_overbought": RSI_OVERBOUGHT,
        "rsi_oversold": RSI_OVERSOLD,
        "max_ext_from_vwap": MAX_EXT_FROM_VWAP,
    }
    if symbol in ETF_SYMBOLS:
        profile.update({
            "ignition_min_delta": max(10, IGNITION_MIN_DELTA - 5),
            "rsi_overbought": min(80, RSI_OVERBOUGHT + 5),
            "rsi_oversold": max(20, RSI_OVERSOLD - 5),
            "max_ext_from_vwap": min(0.015, MAX_EXT_FROM_VWAP + 0.003),
        })
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


def ignition_delta_required(now_score, base_delta):
    """Return adaptive ignition delta requirement from score band."""
    try:
        score = int(now_score)
    except Exception:
        score = 0

    if score >= 95:
        return max(0, int(IGNITION_DELTA_95_PLUS))
    if score >= 90:
        return max(0, int(IGNITION_DELTA_90_94))
    if score >= 80:
        return max(0, int(IGNITION_DELTA_80_89))
    return max(0, int(base_delta))


def _side_metric_bundle(data, side):
    """Return normalized side-specific metrics used by dynamic entry gates."""
    side = str(side or "").upper()
    if side == "CALL":
        side_score = _safe_float_num((data or {}).get("bull_score", 0.0), 0.0)
        momentum = _safe_float_num((data or {}).get("momentum_score_bull", 0.0), 0.0)
        volume = _safe_float_num((data or {}).get("volume_score_bull", 0.0), 0.0)
        regime = _safe_float_num((data or {}).get("market_regime_score_bull", 0.0), 0.0)
        pattern = _safe_float_num((data or {}).get("pattern_quality_score_bull", 0.0), 0.0)
        now_score = _safe_float_num((data or {}).get("bull_score", 0.0), 0.0)
        prev_score = (data or {}).get("bull_5m")
        opposite = _safe_float_num((data or {}).get("bear_score", 0.0), 0.0)
    else:
        side_score = _safe_float_num((data or {}).get("bear_score", 0.0), 0.0)
        momentum = _safe_float_num((data or {}).get("momentum_score_bear", 0.0), 0.0)
        volume = _safe_float_num((data or {}).get("volume_score_bear", 0.0), 0.0)
        regime = _safe_float_num((data or {}).get("market_regime_score_bear", 0.0), 0.0)
        pattern = _safe_float_num((data or {}).get("pattern_quality_score_bear", 0.0), 0.0)
        now_score = _safe_float_num((data or {}).get("bear_score", 0.0), 0.0)
        prev_score = (data or {}).get("bear_5m")
        opposite = _safe_float_num((data or {}).get("bull_score", 0.0), 0.0)

    delta_5m = None if prev_score is None else int(round(now_score - _safe_float_num(prev_score, now_score)))
    ema20_slope_pct = _safe_float_num((data or {}).get("ema20_slope_pct", 0.0), 0.0)
    momentum_quality = _safe_float_num((data or {}).get("momentum_quality", 0.0), 0.0)
    dominance = side_score - opposite

    return {
        "side_score": side_score,
        "momentum": momentum,
        "volume": volume,
        "regime": regime,
        "pattern": pattern,
        "delta_5m": delta_5m,
        "ema20_slope_pct": ema20_slope_pct,
        "momentum_quality": momentum_quality,
        "dominance": dominance,
    }


def dynamic_min_required_score(symbol, side, data):
    """Return a dynamic hard minimum score using regime and continuation quality."""
    is_top_stock = str(symbol or "").upper() in TOP_STOCK_SYMBOLS
    if not DYNAMIC_HARD_GATE_ENABLED:
        # Use the well-tuned floor constants directly (ETF=67, TopStock=62, Stock=64)
        if symbol in ETF_SYMBOLS:
            return DYNAMIC_HARD_GATE_MIN_FLOOR_ETF
        elif is_top_stock:
            return TOP_STOCK_HARD_GATE_MIN_FLOOR
        else:
            return DYNAMIC_HARD_GATE_MIN_FLOOR_STOCK

    m = _side_metric_bundle(data, side)
    entry_timing = _classify_entry_timing(data or {}, side)
    relief = 0
    penalty = 0

    if entry_timing in {"FIRST_PULLBACK", "VWAP_RECLAIM", "HIGHER_LOW_RECLAIM", "BREAKDOWN_RETEST", "LOWER_HIGH_FAILURE", "VWAP_REJECT", "MOMENTUM_CONTINUATION"}:
        relief += 4

    if entry_timing in {"HIGHER_LOW_RECLAIM", "LOWER_HIGH_FAILURE"}:
        relief += 3

    if RECALL_FIRST_MODE:
        relief += 2
        if m["delta_5m"] is not None and m["delta_5m"] >= 0:
            relief += 1
        if m["regime"] >= 60:
            relief += 1

    if symbol in ETF_SYMBOLS:
        if m["momentum"] >= 55:
            relief += 1
        if m["delta_5m"] is not None and m["delta_5m"] >= 0:
            relief += 1

    if m["regime"] >= 80:
        relief += 3
    if m["momentum"] >= 70:
        relief += 3
    elif m["momentum"] < 45:
        penalty += 2
    if m["volume"] >= 60:
        relief += 1
    elif m["volume"] < 25:
        penalty += 1
    if m["pattern"] >= 65:
        relief += 1
    if m["delta_5m"] is not None:
        if m["delta_5m"] >= 5:
            relief += 1
        elif m["delta_5m"] <= 0:
            penalty += 1

    is_top_stock = str(symbol or "").upper() in TOP_STOCK_SYMBOLS
    if is_top_stock:
        if m["dominance"] >= 18:
            relief += 1
        if m["delta_5m"] is not None and m["delta_5m"] >= 12:
            relief += 2
        if m["momentum"] >= 60 and m["pattern"] >= 60:
            relief += 1
        if m["delta_5m"] is not None and m["delta_5m"] >= 18:
            relief += 2

    slope = m["ema20_slope_pct"]
    if (side == "CALL" and slope <= 0) or (side == "PUT" and slope >= 0):
        penalty += 1

    adjusted = int(base - min(DYNAMIC_HARD_GATE_MAX_RELIEF, relief) + min(DYNAMIC_HARD_GATE_MAX_PENALTY, penalty))
    if symbol in ETF_SYMBOLS:
        floor = max(58, DYNAMIC_HARD_GATE_MIN_FLOOR_ETF - (7 if RECALL_FIRST_MODE else 5))
    elif is_top_stock:
        floor = max(56, TOP_STOCK_HARD_GATE_MIN_FLOOR - (6 if RECALL_FIRST_MODE else 0))
    else:
        floor = max(58, DYNAMIC_HARD_GATE_MIN_FLOOR_STOCK - (4 if RECALL_FIRST_MODE else 0))
    return max(int(floor), adjusted)


def opening_window_exception_ok(symbol, side, data):
    """Allow rare opening-window entries only for extreme, liquid opening moves."""
    if not OPENING_EXCEPTION_ENABLED:
        return False, "disabled"
    if symbol not in ETF_SYMBOLS and symbol not in TOP_STOCK_SYMBOLS:
        return False, "symbol not in ETF/top-stock allowlist"

    m = _side_metric_bundle(data, side)
    entry_timing = _classify_entry_timing(data or {}, side)
    side_delta_5m, _ = _score_trend_deltas(data or {}, side)
    vol_ratio = _safe_float_num((data or {}).get("vol_ratio", 1.0), 1.0)
    price = _safe_float_num((data or {}).get("price", 0.0), 0.0)
    vwap = _safe_float_num((data or {}).get("vwap", 0.0), 0.0)

    if str((data or {}).get("tier", "")).upper() != "STRONG":
        early_structure_ok = (
            entry_timing in {"HIGHER_LOW_RECLAIM", "LOWER_HIGH_FAILURE"}
            and bool((data or {}).get("watchlist_promoted", False))
            and m["side_score"] >= max(SCORE_WATCH, OPENING_EXCEPTION_MIN_SCORE - 25)
            and m["dominance"] >= max(8, OPENING_EXCEPTION_MIN_DOMINANCE - 30)
        )
        if not early_structure_ok:
            return False, "tier not STRONG"

    if m["side_score"] < OPENING_EXCEPTION_MIN_SCORE:
        return False, f"score {m['side_score']:.0f} < {OPENING_EXCEPTION_MIN_SCORE}"
    if m["dominance"] < OPENING_EXCEPTION_MIN_DOMINANCE:
        return False, f"dominance {m['dominance']:.0f} < {OPENING_EXCEPTION_MIN_DOMINANCE}"
    if side_delta_5m is None or side_delta_5m < OPENING_EXCEPTION_MIN_SIDE_DELTA_5M:
        return False, f"side 5m delta {side_delta_5m} < {OPENING_EXCEPTION_MIN_SIDE_DELTA_5M}"
    if vol_ratio < OPENING_EXCEPTION_MIN_VOL_RATIO:
        return False, f"vol_ratio {vol_ratio:.2f} < {OPENING_EXCEPTION_MIN_VOL_RATIO:.2f}"
    if side == "CALL" and price <= vwap:
        return False, "price not above VWAP"
    if side == "PUT" and price >= vwap:
        return False, "price not below VWAP"

    return True, (
        f"score={m['side_score']:.0f}, dom={m['dominance']:.0f}, "
        f"d5={side_delta_5m}, volx={vol_ratio:.2f}"
    )


def watchlist_execution_confirmed(symbol, side, data):
    """Promote WATCH tier to executable only when continuation quality is strong."""
    if not SELECTIVE_WATCHLIST_EXECUTION_ENABLED:
        return False, "selective watchlist execution disabled"

    m = _side_metric_bundle(data, side)
    has_delta = m["delta_5m"] is not None
    entry_timing = _classify_entry_timing(data or {}, side)
    is_top_stock = str(symbol or "").upper() in TOP_STOCK_SYMBOLS
    rel_strength = _safe_float_num(
        (data or {}).get("relative_strength_score_bull", 0.0) if side == "CALL" else (data or {}).get("relative_strength_score_bear", 0.0),
        0.0,
    )

    if RECALL_FIRST_MODE and not has_delta:
        startup_override = (
            m["side_score"] >= max(SCORE_WATCH + 8, WATCHLIST_PROMOTION_MIN_SCORE)
            and m["dominance"] >= max(10, WATCHLIST_PROMOTION_MIN_DOMINANCE)
            and m["regime"] >= 45
            and (m["momentum"] >= max(22.0, WATCHLIST_PROMOTION_MIN_MOMENTUM - 13) or entry_timing in {"HIGHER_LOW_RECLAIM", "LOWER_HIGH_FAILURE"})
        )
        if startup_override:
            return True, (
                f"startup override: score={m['side_score']:.0f}, dom={m['dominance']:.0f}, "
                f"mom={m['momentum']:.1f}, vol={m['volume']:.1f}, regime={m['regime']:.1f}"
            )

    if symbol in ETF_SYMBOLS and has_delta:
        etf_override = (
            m["side_score"] >= max(50, SCORE_WATCH - 2)
            and m["dominance"] >= max(8, WATCHLIST_PROMOTION_MIN_DOMINANCE - 4)
            and m["delta_5m"] >= -1
            and m["momentum"] >= max(22.0, WATCHLIST_PROMOTION_MIN_MOMENTUM - 12)
            and m["volume"] >= max(10.0, WATCHLIST_PROMOTION_MIN_VOLUME - 20)
            and m["regime"] >= 45
        )
        if etf_override:
            return True, (
                f"ETF override: score={m['side_score']:.0f}, dom={m['dominance']:.0f}, "
                f"mom={m['momentum']:.1f}, vol={m['volume']:.1f}, d5={m['delta_5m']}"
            )

    if STOCK_SIMPLE_ENTRY_MODE and symbol not in ETF_SYMBOLS and has_delta:
        stock_override = (
            m["side_score"] >= max(48, SCORE_WATCH - 4)
            and m["dominance"] >= max(8, WATCHLIST_PROMOTION_MIN_DOMINANCE - 4)
            and m["delta_5m"] >= -1
            and m["momentum"] >= max(20.0, WATCHLIST_PROMOTION_MIN_MOMENTUM - 15)
            and m["regime"] >= 45
        )
        if stock_override:
            return True, (
                f"stock simplified mode: score={m['side_score']:.0f}, dom={m['dominance']:.0f}, "
                f"mom={m['momentum']:.1f}, vol={m['volume']:.1f}, d5={m['delta_5m']}"
            )

    if RECALL_FIRST_MODE and has_delta:
        recall_override = (
            m["side_score"] >= max(45, SCORE_WATCH - 5)
            and m["dominance"] >= max(8, WATCHLIST_PROMOTION_MIN_DOMINANCE - 4)
            and m["delta_5m"] >= -1
            and m["momentum"] >= max(18.0, WATCHLIST_PROMOTION_MIN_MOMENTUM - 12)
            and m["regime"] >= 40
        )
        if recall_override:
            return True, (
                f"recall-first override: score={m['side_score']:.0f}, dom={m['dominance']:.0f}, "
                f"mom={m['momentum']:.1f}, vol={m['volume']:.1f}, d5={m['delta_5m']}"
            )

    if entry_timing in {"FIRST_PULLBACK", "VWAP_RECLAIM", "HIGHER_LOW_RECLAIM", "BREAKDOWN_RETEST", "LOWER_HIGH_FAILURE", "VWAP_REJECT", "MOMENTUM_CONTINUATION"} and has_delta:
        pullback_override = (
            m["side_score"] >= max(SCORE_WATCH, WATCHLIST_PROMOTION_MIN_SCORE - 6)
            and m["dominance"] >= max(10, WATCHLIST_PROMOTION_MIN_DOMINANCE - 2)
            and m["delta_5m"] >= 0
            and m["momentum"] >= max(25.0, WATCHLIST_PROMOTION_MIN_MOMENTUM - 10)
            and m["volume"] >= max(15.0, WATCHLIST_PROMOTION_MIN_VOLUME - 15)
            and m["regime"] >= 50
        )
        if pullback_override:
            return True, (
                f"pullback/reclaim override ({entry_timing.lower()}): score={m['side_score']:.0f}, dom={m['dominance']:.0f}, "
                f"mom={m['momentum']:.1f}, vol={m['volume']:.1f}, d5={m['delta_5m']}"
            )

    if entry_timing in {"HIGHER_LOW_RECLAIM", "LOWER_HIGH_FAILURE"} and has_delta:
        structure_override = (
            m["side_score"] >= max(44, SCORE_WATCH - 6)
            and m["dominance"] >= max(6, WATCHLIST_PROMOTION_MIN_DOMINANCE - 6)
            and m["delta_5m"] >= -1
            and m["momentum"] >= max(16.0, WATCHLIST_PROMOTION_MIN_MOMENTUM - 18)
            and m["regime"] >= 40
        )
        if structure_override:
            return True, (
                f"early-structure override ({entry_timing.lower()}): score={m['side_score']:.0f}, dom={m['dominance']:.0f}, "
                f"mom={m['momentum']:.1f}, vol={m['volume']:.1f}, d5={m['delta_5m']}"
            )

    # Fast-moving megacaps can show valid continuation before momentum/volume sub-scores fully normalize.
    if is_top_stock and m["delta_5m"] is not None:
        accel_override = (
            m["side_score"] >= max(WATCHLIST_PROMOTION_MIN_SCORE, SCORE_WATCH + 3)
            and m["dominance"] >= max(10, WATCHLIST_PROMOTION_MIN_DOMINANCE - 2)
            and m["delta_5m"] >= max(6, WATCHLIST_PROMOTION_MIN_DELTA_5M + 2)
            and m["momentum"] >= max(30.0, WATCHLIST_PROMOTION_MIN_MOMENTUM - 12)
            and m["regime"] >= 60
        )
        if accel_override:
            return True, (
                f"top-stock acceleration override: score={m['side_score']:.0f}, dom={m['dominance']:.0f}, "
                f"mom={m['momentum']:.1f}, vol={m['volume']:.1f}, d5={m['delta_5m']}"
            )

        regime_light_override = (
            m["side_score"] >= max(WATCHLIST_PROMOTION_MIN_SCORE - 1, SCORE_WATCH + 2)
            and m["dominance"] >= max(10, WATCHLIST_PROMOTION_MIN_DOMINANCE - 2)
            and m["delta_5m"] >= max(4, WATCHLIST_PROMOTION_MIN_DELTA_5M + 1)
            and m["momentum"] >= max(30.0, WATCHLIST_PROMOTION_MIN_MOMENTUM - 12)
            and m["volume"] >= max(25.0, WATCHLIST_PROMOTION_MIN_VOLUME - 5)
            and rel_strength >= TOP_STOCK_WATCHLIST_REL_STRENGTH_MIN
            and m["pattern"] >= TOP_STOCK_WATCHLIST_PATTERN_MIN
        )
        if regime_light_override:
            return True, (
                f"top-stock regime-light override: score={m['side_score']:.0f}, dom={m['dominance']:.0f}, "
                f"mom={m['momentum']:.1f}, vol={m['volume']:.1f}, rel={rel_strength:.1f}, "
                f"pattern={m['pattern']:.1f}, d5={m['delta_5m']}"
            )

    if m["side_score"] < WATCHLIST_PROMOTION_MIN_SCORE:
        return False, f"score {m['side_score']:.0f} < {WATCHLIST_PROMOTION_MIN_SCORE}"
    if m["dominance"] < WATCHLIST_PROMOTION_MIN_DOMINANCE:
        return False, f"dominance {m['dominance']:.0f} < {WATCHLIST_PROMOTION_MIN_DOMINANCE}"
    if m["momentum"] < WATCHLIST_PROMOTION_MIN_MOMENTUM:
        return False, f"momentum {m['momentum']:.1f} < {WATCHLIST_PROMOTION_MIN_MOMENTUM}"
    if m["volume"] < WATCHLIST_PROMOTION_MIN_VOLUME and not (RECALL_FIRST_MODE and m["side_score"] >= max(SCORE_WATCH + 6, WATCHLIST_PROMOTION_MIN_SCORE) and m["dominance"] >= max(10, WATCHLIST_PROMOTION_MIN_DOMINANCE)):
        return False, f"volume {m['volume']:.1f} < {WATCHLIST_PROMOTION_MIN_VOLUME}"
    if m["regime"] < 60:
        return False, f"regime alignment {m['regime']:.1f} < 60"
    if m["delta_5m"] is None and m["side_score"] < 90 and not (RECALL_FIRST_MODE and m["side_score"] >= max(SCORE_WATCH + 6, WATCHLIST_PROMOTION_MIN_SCORE) and m["dominance"] >= max(10, WATCHLIST_PROMOTION_MIN_DOMINANCE)):
        return False, "insufficient 5m history for continuation"
    if m["delta_5m"] is not None and m["delta_5m"] < WATCHLIST_PROMOTION_MIN_DELTA_5M:
        return False, f"5m delta {m['delta_5m']} < {WATCHLIST_PROMOTION_MIN_DELTA_5M}"

    slope = m["ema20_slope_pct"]
    if side == "CALL" and slope <= 0:
        return False, f"ema20 slope {slope:.5f} not bullish"
    if side == "PUT" and slope >= 0:
        return False, f"ema20 slope {slope:.5f} not bearish"
    if m["momentum_quality"] < ENTRY_CONT_MIN_MOMENTUM_QUALITY:
        return False, f"momentum quality {m['momentum_quality']:.1f} < {ENTRY_CONT_MIN_MOMENTUM_QUALITY}"

    return True, (
        f"score={m['side_score']:.0f}, dom={m['dominance']:.0f}, "
        f"mom={m['momentum']:.1f}, vol={m['volume']:.1f}, "
        f"regime={m['regime']:.1f}, d5={m['delta_5m']}"
    )


def entry_momentum_continuation_ok(symbol, side, data):
    """Require continuation structure to avoid weak/late entries."""
    if not ENTRY_MOMENTUM_CONTINUATION_FILTER:
        return True, "disabled"

    m = _side_metric_bundle(data, side)
    is_etf = str(symbol or "").upper() in ETF_SYMBOLS
    watchlist_promoted = bool((data or {}).get("watchlist_promoted", False))
    ignition_confirmed = bool((data or {}).get("ignition_confirmed", False))
    ignition_delta = int((data or {}).get("ignition_delta", 0))

    min_momentum_required = float(ENTRY_CONT_MIN_MOMENTUM_SCORE)
    if (
        (not is_etf)
        and RECALL_FIRST_MODE
        and watchlist_promoted
        and bool((data or {}).get("promoted_quality_ok", False))
        and ignition_confirmed
        and ignition_delta >= max(10, ENTRY_MIN_IGNITION_DELTA - 2)
        and m["dominance"] >= max(20.0, float(SCORE_DOMINANCE))
    ):
        min_momentum_required = min(min_momentum_required, float(PROMOTED_CONT_MIN_MOMENTUM_SCORE))

    # ETF scoring uses the legacy model and does not populate momentum category fields,
    # so ETF continuation should rely on score-delta + EMA slope instead.
    if (not is_etf) and m["momentum"] < min_momentum_required:
        return False, f"momentum {m['momentum']:.1f} < {min_momentum_required:.1f}"
    # For top stocks where ignition was freshly confirmed with a large delta,
    # skip the momentum quality check — the ignition burst itself proves momentum.
    # MQ lags on fast movers because EMA20 slope takes bars to reflect the move.
    is_top_stock = str(symbol or "").upper() in TOP_STOCK_SYMBOLS
    skip_mq = is_top_stock and ignition_confirmed and ignition_delta >= IGNITION_MIN_DELTA
    if (not is_etf) and (not skip_mq) and m["momentum_quality"] < ENTRY_CONT_MIN_MOMENTUM_QUALITY:
        return False, f"momentum quality {m['momentum_quality']:.1f} < {ENTRY_CONT_MIN_MOMENTUM_QUALITY}"
    if m["delta_5m"] is None and m["side_score"] < 90:
        return False, "insufficient 5m history"
    if m["delta_5m"] is not None and m["delta_5m"] < ENTRY_CONT_MIN_DELTA_5M:
        return False, f"5m delta {m['delta_5m']} < {ENTRY_CONT_MIN_DELTA_5M}"

    slope = m["ema20_slope_pct"]
    min_slope = abs(ENTRY_CONT_MIN_EMA20_SLOPE_PCT)
    if side == "CALL" and slope < min_slope:
        return False, f"ema20 slope {slope:.5f} < +{min_slope:.5f}"
    if side == "PUT" and slope > -min_slope:
        return False, f"ema20 slope {slope:.5f} > -{min_slope:.5f}"

    if is_etf:
        return True, f"etf continuation d5={m['delta_5m']}, slope={slope:.5f}"

    return True, (
        f"mom={m['momentum']:.1f}, mq={m['momentum_quality']:.1f}, "
        f"d5={m['delta_5m']}, slope={slope:.5f}"
    )


def adaptive_target_stop_pcts(data, score=None):
    """Return target/stop percentages tuned to current volatility regime and conviction."""
    base_target = float(PROFIT_TARGET_PCT)
    base_stop = float(STOP_LOSS_PCT)
    if not ADAPTIVE_EXIT_PROFILE_ENABLED:
        # Still apply marginal score tighter stop even when adaptive profile is off.
        if MARGINAL_SCORE_STOP_ENABLED and score is not None:
            sc = int(score or 0)
            if MARGINAL_SCORE_FLOOR <= sc < MARGINAL_SCORE_CEIL:
                base_stop = min(base_stop, max(0.01, MARGINAL_STOP_PCT))
        return base_target, base_stop

    vol_ratio = _safe_float_num((data or {}).get("vol_ratio", 1.0), 1.0)
    if vol_ratio >= HIGH_VOL_RATIO:
        stop = max(0.01, HIGH_VOL_STOP_PCT)
    elif vol_ratio <= LOW_VOL_RATIO:
        stop = max(0.01, LOW_VOL_STOP_PCT)
        base_target = max(0.01, LOW_VOL_TARGET_PCT)
        return base_target, stop
    else:
        stop = base_stop

    target = base_target
    if vol_ratio >= HIGH_VOL_RATIO:
        target = max(0.01, HIGH_VOL_TARGET_PCT)

    # Marginal score tighter stop overrides vol regime stop downward.
    if MARGINAL_SCORE_STOP_ENABLED and score is not None:
        sc = int(score or 0)
        if MARGINAL_SCORE_FLOOR <= sc < MARGINAL_SCORE_CEIL:
            stop = min(stop, max(0.01, MARGINAL_STOP_PCT))

    return target, stop


def runner_exit_profile_for_trade(signal, score, data, base_target_pct):
    """Return per-trade exit settings that let high-conviction winners run longer."""
    default_partial_tp = max(0.01, PARTIAL_TP_PCT)
    default_partial_fraction = max(0.1, min(0.9, PARTIAL_CLOSE_FRACTION))
    default_trailing = max(0.02, TRAILING_STOP_GIVEBACK_PCT)

    if not RUNNER_EXIT_PROFILE_ENABLED:
        return {
            "runner_profile": False,
            "target_pct": float(base_target_pct),
            "partial_tp_pct": default_partial_tp,
            "partial_close_fraction": default_partial_fraction,
            "trailing_giveback_pct": default_trailing,
        }

    signal_txt = str(signal or "").upper()
    entry_mq = _safe_float_num((data or {}).get("momentum_quality", 0.0), 0.0)
    ignition_delta = _safe_int_num((data or {}).get("ignition_delta", 0), 0)
    is_strong = signal_txt.startswith("STRONG ")

    qualifies = (
        is_strong
        and int(score or 0) >= RUNNER_MIN_SCORE
        and entry_mq >= RUNNER_MIN_MOMENTUM_QUALITY
        and ignition_delta >= RUNNER_MIN_IGNITION_DELTA
    )

    if not qualifies:
        return {
            "runner_profile": False,
            "target_pct": float(base_target_pct),
            "partial_tp_pct": default_partial_tp,
            "partial_close_fraction": default_partial_fraction,
            "trailing_giveback_pct": default_trailing,
        }

    return {
        "runner_profile": True,
        "target_pct": max(float(base_target_pct), max(0.01, RUNNER_TARGET_PCT)),
        "partial_tp_pct": max(default_partial_tp, max(0.01, RUNNER_PARTIAL_TP_PCT)),
        "partial_close_fraction": min(default_partial_fraction, max(0.1, min(0.9, RUNNER_PARTIAL_CLOSE_FRACTION))),
        "trailing_giveback_pct": min(default_trailing, max(0.02, RUNNER_TRAILING_GIVEBACK_PCT)),
    }


def option_candidate_rank(candidate):
    """Rank option candidates by spread, liquidity and delta alignment."""
    spread_pct = _safe_float_num(candidate.get("spread_pct", 1.0), 1.0)
    volume = max(0.0, _safe_float_num(candidate.get("volume", 0), 0.0))
    oi = max(0.0, _safe_float_num(candidate.get("open_interest", 0), 0.0))
    delta_abs = abs(_safe_float_num(candidate.get("delta", 0.0), 0.0))
    strike_distance_pct = max(0.0, _safe_float_num(candidate.get("strike_distance_pct", 1.0), 1.0))

    # Lower spread is better.
    spread_score = max(0.0, 1.0 - min(1.0, spread_pct / 0.35))
    # Larger volume+OI is better, saturating scale.
    liq_raw = min(1.0, (volume / 1000.0)) * 0.6 + min(1.0, (oi / 5000.0)) * 0.4
    # Prefer delta inside target band.
    if delta_abs <= 0:
        delta_score = 0.0
    elif TARGET_OPTION_DELTA_MIN <= delta_abs <= TARGET_OPTION_DELTA_MAX:
        delta_score = 1.0
    elif delta_abs < TARGET_OPTION_DELTA_MIN:
        delta_score = max(0.0, 1.0 - ((TARGET_OPTION_DELTA_MIN - delta_abs) / max(0.05, TARGET_OPTION_DELTA_MIN)))
    else:
        delta_score = max(0.0, 1.0 - ((delta_abs - TARGET_OPTION_DELTA_MAX) / max(0.05, 1.0 - TARGET_OPTION_DELTA_MAX)))

    # Prefer strikes close to underlying; beyond max distance decays to 0.
    strike_distance_score = max(
        0.0,
        1.0 - min(1.0, strike_distance_pct / max(0.0001, OPTION_MAX_STRIKE_DISTANCE_PCT)),
    )

    score = (
        spread_score * OPTION_RANK_SPREAD_WEIGHT
        + liq_raw * OPTION_RANK_LIQUIDITY_WEIGHT
        + delta_score * OPTION_RANK_DELTA_WEIGHT
        + strike_distance_score * OPTION_RANK_STRIKE_DISTANCE_WEIGHT
    )
    return score


def _score_trend_deltas(data, side):
    """Return 5m/10m score deltas for the intended side."""
    side = str(side or "").upper()
    now_score = int(data.get("bull_score", 0)) if side == "CALL" else int(data.get("bear_score", 0))
    past_5m = data.get("bull_5m") if side == "CALL" else data.get("bear_5m")
    past_10m = data.get("bull_10m") if side == "CALL" else data.get("bear_10m")
    delta_5m = None if past_5m is None else int(now_score - int(past_5m))
    delta_10m = None if past_10m is None else int(now_score - int(past_10m))
    return delta_5m, delta_10m


def classify_entry_playbook(side, data):
    """Classify a valid directional setup as a breakout or pullback continuation."""
    side = str(side or "").upper()
    call_side = side == "CALL"
    fresh_break = bool((data or {}).get("fresh_breakout" if call_side else "fresh_breakdown", False))
    delta_5m, _ = _score_trend_deltas(data or {}, side)
    price = _safe_float_num((data or {}).get("price", 0.0), 0.0)
    vwap = _safe_float_num((data or {}).get("vwap", 0.0), 0.0)
    ema20 = _safe_float_num((data or {}).get("ema20", 0.0), 0.0)
    ema50 = _safe_float_num((data or {}).get("ema50", 0.0), 0.0)
    micro_high = _safe_float_num((data or {}).get("micro_high", price), price)
    micro_low = _safe_float_num((data or {}).get("micro_low", price), price)
    prior_bar_high = _safe_float_num((data or {}).get("prior_bar_high", price), price)
    prior_bar_low = _safe_float_num((data or {}).get("prior_bar_low", price), price)
    candle_ok = bool((data or {}).get("bullish_candle" if call_side else "bearish_candle", False))
    aligned = (price > vwap and price > ema20 and price > ema50) if call_side else (price < vwap and price < ema20 and price < ema50)

    if fresh_break and candle_ok and aligned and (delta_5m is None or delta_5m >= 0):
        return "BREAKOUT"
    pullback_reclaim = (
        micro_low <= min(vwap, ema20) * (1.0 + PULLBACK_RECLAIM_TOLERANCE)
        and price > prior_bar_high
    ) if call_side else (
        micro_high >= max(vwap, ema20) * (1.0 - PULLBACK_RECLAIM_TOLERANCE)
        and price < prior_bar_low
    )
    if aligned and candle_ok and pullback_reclaim and delta_5m is not None and delta_5m >= PULLBACK_MIN_SCORE_DELTA:
        return "PULLBACK_CONTINUATION"
    return None


def classify_entry_confluence(symbol, side, data, option):
    """Score DIRECTION/LOCATION/STRUCTURE/TIMING/CONTEXT/CONTRACT (0-2 each, 12 max).

    This is a compensating check, not six independent gates: a very strong
    category (e.g. a clean breakout-retest) can offset one mediocre category.
    Only STRUCTURE (a valid playbook) and CONTRACT (a tradeable option) are
    mandatory — everything else can trade off against everything else.
    """
    side = str(side or "").upper()
    call_side = side == "CALL"
    data = data or {}
    option = option or {}
    points = {}

    # DIRECTION — score dominance and 5m trend both point the same way.
    score = _safe_float_num(data.get("bull_score" if call_side else "bear_score", 0.0), 0.0)
    opposite = _safe_float_num(data.get("bear_score" if call_side else "bull_score", 0.0), 0.0)
    delta_5m, _ = _score_trend_deltas(data, side)
    dominance = score - opposite
    if dominance >= 30 and delta_5m is not None and delta_5m >= 0:
        points["direction"] = 2.0
    elif dominance >= 10 or (delta_5m is not None and delta_5m >= 0):
        points["direction"] = 1.0
    else:
        points["direction"] = 0.0

    # LOCATION — reasonable distance from VWAP/EMA20, room before opposing S/R.
    ext_pct = abs(_safe_float_num(data.get("vwap_extension_pct", 0.0), 0.0))
    opposing_level = data.get("resistance_level") if call_side else data.get("support_level")
    opposing_room_atr = _safe_float_num((opposing_level or {}).get("distance_atr", 999.0), 999.0)
    if ext_pct <= 0.006 and opposing_room_atr >= 0.5:
        points["location"] = 2.0
    elif ext_pct <= 0.012 and opposing_room_atr >= 0.25:
        points["location"] = 1.0
    else:
        points["location"] = 0.0

    # STRUCTURE — mandatory: must be a named, valid playbook.
    playbook = str(data.get("entry_playbook", "") or "")
    points["structure"] = 2.0 if playbook in {"BREAKOUT", "PULLBACK_CONTINUATION", "BREAKOUT_CONTINUATION"} else 0.0

    # TIMING — 1m sniper trigger quality.
    trigger = str(data.get("one_minute_trigger", "") or "").upper()
    if trigger == "BREAKOUT_RETEST":
        points["timing"] = 2.0
    elif trigger in {"FIRST_PULLBACK", "MOMENTUM_COMPRESSION"}:
        points["timing"] = 1.0
    else:
        points["timing"] = 0.0

    # CONTEXT — SPY/QQQ tape not strongly contradictory.
    macro_penalty = _safe_float_num(data.get("macro_penalty", 0.0), 0.0)
    if macro_penalty <= 0:
        points["context"] = 2.0
    elif macro_penalty <= 12:
        points["context"] = 1.0
    else:
        points["context"] = 0.0

    # CONTRACT — mandatory: delta must fall in the preferred/acceptable band.
    opt_delta = abs(_safe_float_num(option.get("delta", 0.0), 0.0))
    if TARGET_OPTION_DELTA_MIN <= opt_delta <= TARGET_OPTION_DELTA_MAX:
        points["contract"] = 2.0
    elif OPTION_ACCEPTABLE_DELTA_MIN <= opt_delta <= OPTION_ACCEPTABLE_DELTA_MAX:
        points["contract"] = 1.0
    else:
        points["contract"] = 0.0

    total = sum(points.values())
    detail = ", ".join(f"{k}={v:.0f}" for k, v in points.items())

    if points["structure"] <= 0.0:
        return False, total, f"no valid structure ({detail})"
    if points["contract"] <= 0.0:
        return False, total, f"contract outside tradeable delta band ({detail})"
    if total < ENTRY_CONFLUENCE_MIN_POINTS:
        return False, total, f"confluence {total:.1f} < {ENTRY_CONFLUENCE_MIN_POINTS:.1f} ({detail})"
    return True, total, detail


def _v2_pre_contract_scores(symbol, side, data):
    """Compute Trend/Location/Setup/Trigger/Context+RS — everything V2 can score
    before a contract is even found. Pure evidence computation, no gating.
    """
    side = str(side or "").upper()
    call_side = side == "CALL"
    data = data or {}

    side_score = _safe_float_num(data.get("bull_score" if call_side else "bear_score", 0.0), 0.0)
    opp_score = _safe_float_num(data.get("bear_score" if call_side else "bull_score", 0.0), 0.0)
    delta_5m, _ = _score_trend_deltas(data, side)
    dominance = side_score - opp_score
    momentum_quality = _safe_float_num(data.get("momentum_quality", 0.0), 0.0)

    if dominance >= 30 and (delta_5m is None or delta_5m >= 0):
        trend_score = min(100.0, 0.65 * side_score + 0.35 * max(0.0, momentum_quality))
    elif dominance >= 15:
        trend_score = min(100.0, 0.55 * side_score + 0.25 * max(0.0, momentum_quality) + 10.0)
    else:
        trend_score = min(100.0, 0.45 * side_score + 0.20 * max(0.0, momentum_quality))

    ext_pct = abs(_safe_float_num(data.get("vwap_extension_pct", 0.0), 0.0))
    opposing_level = data.get("resistance_level") if call_side else data.get("support_level")
    room_atr = _safe_float_num((opposing_level or {}).get("distance_atr", 0.0), 0.0)
    if ext_pct <= 0.006 and room_atr >= 0.5:
        location_score = 100.0
    elif ext_pct <= 0.012 and room_atr >= 0.25:
        location_score = 70.0
    elif ext_pct <= 0.018 and room_atr >= 0.15:
        location_score = 45.0
    else:
        location_score = 20.0

    playbook = str(data.get("entry_playbook", "") or "").upper()
    if playbook in {"PULLBACK_CONTINUATION", "BREAKOUT_CONTINUATION", "BREAKOUT"}:
        setup_score = 100.0
    elif playbook:
        setup_score = 55.0
    else:
        setup_score = 0.0

    trigger = str(data.get("one_minute_trigger", "") or "").upper()
    if trigger == "BREAKOUT_RETEST":
        trigger_score = 100.0
    elif trigger == "FIRST_PULLBACK":
        trigger_score = 85.0
    elif trigger == "MOMENTUM_COMPRESSION":
        trigger_score = 75.0
    elif trigger in {"DISABLED", ""}:
        trigger_score = 50.0 if not ONE_MINUTE_ENTRY_ENABLED else 20.0
    else:
        trigger_score = 35.0

    macro_penalty = _safe_float_num(data.get("macro_penalty", 0.0), 0.0)
    regime_score = _safe_float_num(
        data.get("market_regime_score_bull" if call_side else "market_regime_score_bear", 0.0),
        0.0,
    )
    rel_strength = _safe_float_num(
        data.get("relative_strength_score_bull" if call_side else "relative_strength_score_bear", 0.0),
        0.0,
    )
    # An invalid market-context check (short-term move not yet aligned) is evidence of
    # weak conviction, not an automatic veto — it discounts Context, it doesn't zero it.
    context_invalid_penalty = 0.0 if bool(data.get("context_valid", True)) else 25.0
    context_rs_score = max(
        0.0,
        min(100.0, 0.55 * regime_score + 0.45 * rel_strength - macro_penalty - context_invalid_penalty),
    )

    return {
        "trend": trend_score,
        "location": location_score,
        "setup": setup_score,
        "trigger": trigger_score,
        "context_rs": context_rs_score,
    }


def log_v2_pre_contract_components(symbol, side, data):
    """Measurement only: log what V2 sees before contract search, so a rejected
    contract search doesn't hide Trend/Location/Setup/Trigger/Context evidence.
    """
    if not (V2_ENTRY_QUALITY_ENABLED and V2_PRE_CONTRACT_LOG_ENABLED):
        return
    try:
        scores = _v2_pre_contract_scores(symbol, side, data)
        log(
            f"[{symbol}] [V2 PRE-CONTRACT] {side} — trend={scores['trend']:.0f}, "
            f"location={scores['location']:.0f}, setup={scores['setup']:.0f}, "
            f"trigger={scores['trigger']:.0f}, context+rs={scores['context_rs']:.0f}, contract=PENDING"
        )
    except Exception as e:
        log(f"[{symbol}] V2 pre-contract logging failed (non-blocking): {e}")


def unified_entry_quality_v2(symbol, side, data, option):
    """Unified V2 entry decision using weighted evidence instead of chained vetoes."""
    side = str(side or "").upper()
    option = option or {}
    scores = _v2_pre_contract_scores(symbol, side, data)
    trend_score = scores["trend"]
    location_score = scores["location"]
    setup_score = scores["setup"]
    trigger_score = scores["trigger"]
    context_rs_score = scores["context_rs"]

    delta_abs = abs(_safe_float_num(option.get("delta", 0.0), 0.0))
    spread_pct = _safe_float_num(option.get("spread_pct", 1.0), 1.0)
    vol = max(0.0, _safe_float_num(option.get("volume", 0.0), 0.0))
    oi = max(0.0, _safe_float_num(option.get("open_interest", 0.0), 0.0))
    if TARGET_OPTION_DELTA_MIN <= delta_abs <= TARGET_OPTION_DELTA_MAX:
        delta_score = 100.0
    elif OPTION_ACCEPTABLE_DELTA_MIN <= delta_abs <= OPTION_ACCEPTABLE_DELTA_MAX:
        delta_score = 75.0
    elif OPTION_FALLBACK_DELTA_MIN <= delta_abs < OPTION_ACCEPTABLE_DELTA_MIN:
        delta_score = 50.0
    else:
        delta_score = 20.0
    spread_score = 100.0 if spread_pct <= 0.04 else 75.0 if spread_pct <= 0.08 else 45.0 if spread_pct <= 0.12 else 20.0
    liquidity_score = 100.0 if (vol >= 500 and oi >= 5000) else 75.0 if (vol >= 250 and oi >= 2500) else 45.0 if (vol >= 100 and oi >= 1000) else 20.0
    contract_score = max(0.0, min(100.0, 0.45 * delta_score + 0.30 * spread_score + 0.25 * liquidity_score))

    weight_sum = max(
        1.0,
        V2_ENTRY_WEIGHT_TREND
        + V2_ENTRY_WEIGHT_LOCATION
        + V2_ENTRY_WEIGHT_SETUP
        + V2_ENTRY_WEIGHT_TRIGGER
        + V2_ENTRY_WEIGHT_CONTEXT_RS
        + V2_ENTRY_WEIGHT_CONTRACT,
    )
    weighted_total = (
        trend_score * V2_ENTRY_WEIGHT_TREND
        + location_score * V2_ENTRY_WEIGHT_LOCATION
        + setup_score * V2_ENTRY_WEIGHT_SETUP
        + trigger_score * V2_ENTRY_WEIGHT_TRIGGER
        + context_rs_score * V2_ENTRY_WEIGHT_CONTEXT_RS
        + contract_score * V2_ENTRY_WEIGHT_CONTRACT
    ) / weight_sum

    detail = (
        f"trend={trend_score:.0f}, location={location_score:.0f}, setup={setup_score:.0f}, "
        f"trigger={trigger_score:.0f}, context+rs={context_rs_score:.0f}, contract={contract_score:.0f}"
    )
    if weighted_total < V2_ENTRY_QUALITY_MIN_SCORE:
        return False, weighted_total, f"entry quality {weighted_total:.1f} < {V2_ENTRY_QUALITY_MIN_SCORE:.1f} ({detail})"
    return True, weighted_total, detail


def _legacy_shadow_verdict(symbol, side, data, option):
    """Shadow-only: would the pre-V2 pipeline (hard score + confirmed sniper + confluence)
    have taken this same candidate? Never blocks anything — used solely for attribution.
    """
    side_score = _safe_float_num((data or {}).get("bull_score" if side == "CALL" else "bear_score", 0.0), 0.0)
    min_required_score = min(BREAKOUT_MIN_SCORE, PULLBACK_MIN_SCORE)
    score_ok = side_score >= min_required_score

    sniper_required = bool(ONE_MINUTE_ENTRY_ENABLED)
    sniper_ok = bool((data or {}).get("one_minute_entry_confirmed", False)) if sniper_required else True

    confluence_ok, confluence_points, confluence_detail = classify_entry_confluence(symbol, side, data, option)

    legacy_ok = score_ok and sniper_ok and confluence_ok
    detail = (
        f"score {side_score:.0f}{'>=' if score_ok else '<'}{min_required_score:.0f}, "
        f"sniper_confirmed={sniper_ok}, confluence={confluence_points:.1f}/12 ({confluence_detail})"
    )
    return legacy_ok, detail


def _log_entry_attribution(symbol, side, data, v2_ok, v2_score, legacy_ok, legacy_detail):
    """Best-effort shadow attribution log — measurement only, never raises or blocks."""
    if not V2_ATTRIBUTION_LOG_ENABLED:
        return
    try:
        if v2_ok and legacy_ok:
            bucket = "BOTH_PASS"
        elif v2_ok and not legacy_ok:
            bucket = "V2_ONLY"
        elif (not v2_ok) and legacy_ok:
            bucket = "LEGACY_ONLY"
        else:
            bucket = "BOTH_BLOCK"
        row = {
            "timestamp": datetime.now(central).strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": symbol,
            "side": side,
            "bucket": bucket,
            "v2_pass": v2_ok,
            "v2_score": round(float(v2_score), 1),
            "legacy_pass": legacy_ok,
            "legacy_detail": legacy_detail,
            "setup_type": str((data or {}).get("entry_playbook", "") or ""),
            "one_minute_trigger": str((data or {}).get("one_minute_trigger", "") or ""),
        }
        pd.DataFrame([row]).to_csv(
            V2_ATTRIBUTION_LOG_FILE,
            mode="a",
            header=not os.path.exists(V2_ATTRIBUTION_LOG_FILE),
            index=False,
        )
        log(f"[{symbol}] Shadow attribution: bucket={bucket} (v2={v2_ok}/{v2_score:.1f}, legacy={legacy_ok}: {legacy_detail}).")
    except Exception as e:
        log(f"[{symbol}] Shadow attribution logging failed (non-blocking): {e}")



def _record_post_loss_setup(symbol, side, exit_market_context, ignition_delta):
    """Remember where a losing trade failed so the next same-side entry must show fresh evidence."""
    symbol = str(symbol or "").upper()
    side = str(side or "").upper()
    if not symbol or side not in ("CALL", "PUT"):
        return
    _post_loss_setup[(symbol, side)] = {
        "loss_at": datetime.now(central),
        "reference_price": _safe_float_num((exit_market_context or {}).get("price", 0.0), 0.0),
        "entry_ignition_delta": _safe_int_num(ignition_delta, 0),
    }


def fresh_setup_confirmed(symbol, side, data):
    """After a same-side loss, require new evidence (not the same chop) before re-entering."""
    if not FRESH_SETUP_AFTER_LOSS_ENABLED:
        return True, "fresh-setup check disabled"
    symbol = str(symbol or "").upper()
    side = str(side or "").upper()
    record = _post_loss_setup.get((symbol, side))
    if not record:
        return True, "no prior loss on record"

    loss_at = record.get("loss_at")
    if not isinstance(loss_at, datetime) or (datetime.now(central) - loss_at) > timedelta(minutes=FRESH_SETUP_LOOKBACK_MINUTES):
        _post_loss_setup.pop((symbol, side), None)
        return True, "prior loss outside lookback window"

    data = data or {}
    call_side = side == "CALL"
    fresh_break = bool(data.get("fresh_breakout" if call_side else "fresh_breakdown", False))
    ignition_delta = _safe_int_num(data.get("ignition_delta", 0), 0)
    trigger = str(data.get("one_minute_trigger", "") or "").upper()
    price = _safe_float_num(data.get("price", 0.0), 0.0)
    reference_price = _safe_float_num(record.get("reference_price", 0.0), 0.0)

    signals = []
    if fresh_break:
        signals.append("fresh breakout/breakdown")
    if ignition_delta >= FRESH_SETUP_MIN_IGNITION_DELTA:
        signals.append(f"renewed ignition {ignition_delta:+d}")
    if trigger == "BREAKOUT_RETEST":
        signals.append("retest-confirmed 1m trigger")
    if reference_price > 0:
        reset_pct = (price - reference_price) / reference_price
        if (call_side and reset_pct >= FRESH_SETUP_PRICE_RESET_PCT) or ((not call_side) and reset_pct <= -FRESH_SETUP_PRICE_RESET_PCT):
            signals.append(f"structure reset {reset_pct * 100:+.2f}% vs prior stop-out")

    if len(signals) >= FRESH_SETUP_MIN_REQUIRED_SIGNALS:
        return True, f"fresh setup confirmed: {', '.join(signals)}"
    return False, (
        f"waiting for fresh setup after prior {side} stop-out "
        f"(have {len(signals)}/{FRESH_SETUP_MIN_REQUIRED_SIGNALS}: {', '.join(signals) or 'none'})"
    )


def _breakout_hold_confirmation(symbol, side, data):
    """Armed breakout state: gives a fresh break up to ARMED_SETUP_MAX_BARS completed
    5m bars to hold before expiring, instead of a single all-or-nothing next bar.
    Cancels early on score collapse, dominance disappearing, or structure loss.
    """
    if not BREAKOUT_HOLD_CONFIRMATION_ENABLED or not symbol:
        return None, "disabled"

    side = str(side or "").upper()
    call_side = side == "CALL"
    key = (str(symbol).upper(), side)
    bar_time = (data or {}).get("bar_time")
    price = _safe_float_num((data or {}).get("price", 0.0), 0.0)
    vwap = _safe_float_num((data or {}).get("vwap", 0.0), 0.0)
    ema20 = _safe_float_num((data or {}).get("ema20", 0.0), 0.0)
    score = _safe_float_num((data or {}).get("bull_score" if call_side else "bear_score", 0.0), 0.0)
    opposite = _safe_float_num((data or {}).get("bear_score" if call_side else "bull_score", 0.0), 0.0)
    fresh_break = bool((data or {}).get("fresh_breakout", False)) if side == "CALL" else bool((data or {}).get("fresh_breakdown", False))
    pending = _breakout_hold_pending.get(key)

    if pending:
        if bar_time == pending["bar_time"]:
            return False, f"ARMED_BREAKOUT_{side}: awaiting next completed bar (bar 1/{ARMED_SETUP_MAX_BARS})"

        bars_elapsed = int(pending.get("bars_elapsed", 0)) + 1
        if bars_elapsed > ARMED_SETUP_MAX_BARS:
            _breakout_hold_pending.pop(key, None)
            return False, f"ARMED SETUP CANCELLED: expired after {ARMED_SETUP_MAX_BARS} bars"

        level = float(pending["level"])
        buffer = level * BREAKOUT_CONFIRMATION_MIN_BUFFER_PCT
        held = price >= level + buffer if side == "CALL" else price <= level - buffer

        if not held:
            dominance_now = score - opposite
            armed_score = pending.get("armed_score", score)
            if score < PULLBACK_MIN_SCORE or score < (armed_score * 0.6):
                _breakout_hold_pending.pop(key, None)
                return False, f"ARMED SETUP CANCELLED: score collapsed to {score:.0f}"
            if dominance_now < PLAYBOOK_MIN_DOMINANCE:
                _breakout_hold_pending.pop(key, None)
                return False, f"ARMED SETUP CANCELLED: dominance disappeared ({dominance_now:.0f})"
            structure_lost = (price < min(vwap, ema20)) if call_side else (price > max(vwap, ema20))
            if structure_lost:
                _breakout_hold_pending.pop(key, None)
                return False, "ARMED SETUP CANCELLED: lost VWAP/EMA20 structure"
            pending["bar_time"] = bar_time
            pending["bars_elapsed"] = bars_elapsed
            return False, f"ARMED_BREAKOUT_{side}: still waiting (bar {bars_elapsed + 1}/{ARMED_SETUP_MAX_BARS})"

        _breakout_hold_pending.pop(key, None)

        delta_5m, _ = _score_trend_deltas(data or {}, side)
        if delta_5m is None or delta_5m < BREAKOUT_CONFIRMATION_MIN_SCORE_DELTA:
            return False, (
                f"breakout confirmation 5m score delta {delta_5m} < "
                f"{BREAKOUT_CONFIRMATION_MIN_SCORE_DELTA:+d}"
            )

        volume_ratio = _safe_float_num((data or {}).get("vol_ratio", 0.0), 0.0)
        if volume_ratio < BREAKOUT_CONFIRMATION_MIN_VOLUME_RATIO:
            return False, (
                f"breakout confirmation volume ratio {volume_ratio:.2f} < "
                f"{BREAKOUT_CONFIRMATION_MIN_VOLUME_RATIO:.2f}"
            )

        opposing_indices = [
            name for name, index_side in (("SPY", _spy_vwap_side()), ("QQQ", _qqq_vwap_side()))
            if index_side == ("bear" if side == "CALL" else "bull")
        ]
        if opposing_indices:
            return False, f"breakout confirmation blocked by {'/'.join(opposing_indices)} macro alignment"
        if held:
            return True, f"breakout held {level:.2f} on bar {bars_elapsed}/{ARMED_SETUP_MAX_BARS}"
        return False, f"breakout failed to hold {level:.2f}"

    if fresh_break:
        level = _safe_float_num((data or {}).get("recent_high" if side == "CALL" else "recent_low", price), price)
        _breakout_hold_pending[key] = {
            "bar_time": bar_time,
            "level": level,
            "bars_elapsed": 0,
            "armed_score": score,
        }
        return False, f"ARMED_BREAKOUT_{side}: fresh breakout at {level:.2f}; awaiting hold confirmation"
    return None, "no pending breakout"


def _breakout_continuation_confirmation(symbol, side, data):
    """Keep a confirmed breakout eligible for one healthy continuation window."""
    if not BREAKOUT_CONTINUATION_ENABLED or not symbol:
        return None, "disabled"

    side = str(side or "").upper()
    key = (str(symbol).upper(), side)
    continuation = _breakout_continuations.get(key)
    if not continuation:
        return None, "no confirmed breakout continuation"

    bar_time = (data or {}).get("bar_time")
    price = _safe_float_num((data or {}).get("price", 0.0), 0.0)
    level = float(continuation["level"])
    expires_at = continuation.get("expires_at")
    if expires_at is not None and bar_time is not None and bar_time > expires_at:
        _breakout_continuations.pop(key, None)
        return False, "confirmed breakout continuation expired"

    held = price > level if side == "CALL" else price < level
    if not held:
        _breakout_continuations.pop(key, None)
        return False, f"confirmed breakout lost {level:.2f}"
    return True, f"confirmed breakout continuation above {level:.2f}"


def _remember_breakout_continuation(symbol, side, data):
    if not BREAKOUT_CONTINUATION_ENABLED or not symbol:
        return

    side = str(side or "").upper()
    key = (str(symbol).upper(), side)
    bar_time = (data or {}).get("bar_time")
    price = _safe_float_num((data or {}).get("price", 0.0), 0.0)
    level = _safe_float_num((data or {}).get("recent_high" if side == "CALL" else "recent_low", price), price)
    try:
        expires_at = bar_time + timedelta(minutes=BREAKOUT_CONTINUATION_WINDOW_MINUTES)
    except (TypeError, ValueError):
        expires_at = None
    _breakout_continuations[key] = {"level": level, "expires_at": expires_at}


def marginal_call_entry_ok(score, data):
    """Require stronger local and index alignment for 65-69 score CALLs."""
    if not MARGINAL_CALL_GATE_ENABLED or score > MARGINAL_CALL_SCORE_CEIL:
        return True, "not a marginal CALL"

    price = _safe_float_num((data or {}).get("price", 0.0), 0.0)
    vwap = _safe_float_num((data or {}).get("vwap", 0.0), 0.0)
    ema20 = _safe_float_num((data or {}).get("ema20", 0.0), 0.0)
    ema50 = _safe_float_num((data or {}).get("ema50", 0.0), 0.0)
    if not (price > vwap and price > ema20 and price > ema50):
        return False, "marginal CALL requires VWAP/EMA20/EMA50 alignment"

    relative_strength = _safe_float_num((data or {}).get("relative_strength_score_bull", 0.0), 0.0)
    if relative_strength < MARGINAL_CALL_MIN_RELATIVE_STRENGTH:
        return False, (
            f"marginal CALL relative strength {relative_strength:.1f} "
            f"< {MARGINAL_CALL_MIN_RELATIVE_STRENGTH:.1f}"
        )

    momentum = _safe_float_num((data or {}).get("momentum_score_bull", 0.0), 0.0)
    if momentum < MARGINAL_CALL_MIN_MOMENTUM:
        return False, f"marginal CALL momentum {momentum:.1f} < {MARGINAL_CALL_MIN_MOMENTUM:.1f}"

    bearish_indices = [
        name for name, side in (("SPY", _spy_vwap_side()), ("QQQ", _qqq_vwap_side()))
        if side == "bear"
    ]
    if bearish_indices:
        return False, f"marginal CALL blocked by bearish {'/'.join(bearish_indices)}"
    return True, "marginal CALL quality confirmed"


def _log_vwap_shadow_telemetry(symbol, side, extension, playbook, one_minute_trigger, score, dominance_val, volume_ratio, pattern_quality):
    """Shadow-only: what would the tiered VWAP decision be at other caps? Never gates
    anything — pure evidence for calibrating PLAYBOOK_MAX_VWAP_EXTENSION later.
    """
    try:
        would_pass = {}
        for cap in VWAP_SHADOW_CAPS_PCT:
            if extension <= cap:
                would_pass[f"{cap*100:.2f}%"] = True
            else:
                extended_override = (
                    playbook == "PULLBACK_CONTINUATION"
                    and one_minute_trigger == "FIRST_PULLBACK"
                    and score >= PLAYBOOK_VWAP_EXTENDED_OVERRIDE_MIN_SCORE
                    and dominance_val >= PLAYBOOK_VWAP_EXTENDED_OVERRIDE_MIN_DOMINANCE
                    and volume_ratio >= PLAYBOOK_VWAP_EXTENDED_OVERRIDE_MIN_VOLX
                    and pattern_quality >= PLAYBOOK_VWAP_EXTENDED_OVERRIDE_MIN_PATTERN
                    and extension <= PLAYBOOK_VWAP_EXTENDED_MAX
                )
                would_pass[f"{cap*100:.2f}%"] = bool(extended_override)
        log(
            f"[{symbol}] [VWAP SHADOW] {side} extension={extension*100:.2f}% playbook={playbook} "
            f"would_pass_at={would_pass}"
        )
    except Exception as e:
        log(f"[{symbol}] VWAP shadow telemetry failed (non-blocking): {e}")


def playbook_entry_ok(side, data, symbol=None):
    """Apply the minimum technical conditions for a named entry playbook."""
    side = str(side or "").upper()
    call_side = side == "CALL"
    score = _safe_float_num((data or {}).get("bull_score" if call_side else "bear_score", 0.0), 0.0)
    opposite = _safe_float_num((data or {}).get("bear_score" if call_side else "bull_score", 0.0), 0.0)
    price = _safe_float_num((data or {}).get("price", 0.0), 0.0)
    vwap = _safe_float_num((data or {}).get("vwap", 0.0), 0.0)
    ema20 = _safe_float_num((data or {}).get("ema20", 0.0), 0.0)
    recent_high = _safe_float_num((data or {}).get("recent_high", price), price)
    recent_low = _safe_float_num((data or {}).get("recent_low", price), price)
    pdh = _safe_float_num((data or {}).get("pdh", price), price)
    pdl = _safe_float_num((data or {}).get("pdl", price), price)
    support_level = (data or {}).get("support_level")
    resistance_level = (data or {}).get("resistance_level")
    extension = abs(_safe_float_num((data or {}).get("vwap_extension_pct", 0.0), 0.0))
    volume_ratio = _safe_float_num((data or {}).get("vol_ratio", 0.0), 0.0)
    delta_5m, _ = _score_trend_deltas(data or {}, side)
    one_minute_engine_active = bool(ONE_MINUTE_ENTRY_ENABLED)
    one_minute_enabled = bool(one_minute_engine_active and data.get("one_minute_entry_confirmed", False))
    one_minute_trigger = str(data.get("one_minute_trigger", "") or "").upper()
    # V2: an unconfirmed 1m trigger is evidence (weak Trigger score), not an automatic
    # veto. Fall through to structural playbook/hold-confirmation checks below instead
    # of terminating the candidate outright. Genuinely invalid structure still blocks.
    playbook = classify_entry_playbook(side, data)
    if one_minute_enabled and one_minute_trigger in {"FIRST_PULLBACK", "BREAKOUT_RETEST", "MOMENTUM_COMPRESSION"}:
        if one_minute_trigger == "FIRST_PULLBACK":
            playbook = "PULLBACK_CONTINUATION"
        elif one_minute_trigger == "BREAKOUT_RETEST":
            playbook = "BREAKOUT"
        else:
            playbook = "BREAKOUT"
        hold_confirmed, hold_reason = True, f"1m sniper trigger: {one_minute_trigger}"
        continuation_confirmed, continuation_reason = False, hold_reason
    else:
        hold_confirmed, hold_reason = _breakout_hold_confirmation(symbol, side, data)
        continuation_confirmed, continuation_reason = _breakout_continuation_confirmation(symbol, side, data)
    if hold_confirmed and not one_minute_enabled:
        _remember_breakout_continuation(symbol, side, data)
        playbook = "BREAKOUT"
        continuation_confirmed = True
        continuation_reason = hold_reason
    elif continuation_confirmed:
        playbook = "BREAKOUT_CONTINUATION"

    if playbook is None:
        return False, None, "no breakout or pullback-continuation structure"
    if playbook == "BREAKOUT" and hold_confirmed is False:
        return False, playbook, hold_reason
    if call_side:
        resistance_levels = [level for level in (recent_high, pdh) if level >= price]
        extreme_level = min(resistance_levels) if resistance_levels else max(recent_high, pdh)
    else:
        support_levels = [level for level in (recent_low, pdl) if level <= price]
        extreme_level = max(support_levels) if support_levels else min(recent_low, pdl)
    distance_to_extreme = (
        ((extreme_level - price) / price) if call_side else ((price - extreme_level) / price)
    ) if price > 0 else 0.0
    if playbook not in ("BREAKOUT", "BREAKOUT_CONTINUATION") and distance_to_extreme <= PLAYBOOK_EXTREME_PROXIMITY_PCT:
        direction = "high/resistance" if call_side else "low/support"
        return False, playbook, (
            f"{side} is within {distance_to_extreme * 100:+.2f}% of {direction} "
            f"${extreme_level:.2f}; requires breakout hold confirmation"
        )

    # Strong horizontal S/R guard for non-breakout entries. Breakout entries are
    # allowed to challenge and clear the level because they have explicit hold
    # confirmation logic elsewhere in this function.
    if playbook not in ("BREAKOUT", "BREAKOUT_CONTINUATION"):
        if call_side and isinstance(resistance_level, dict):
            resistance_touches = _safe_int_num(resistance_level.get("touches", 0), 0)
            resistance_strength = _safe_float_num(resistance_level.get("strength", 0.0), 0.0)
            resistance_distance_atr = _safe_float_num(resistance_level.get("distance_atr", 999.0), 999.0)
            if (
                resistance_touches >= 2
                and resistance_strength >= 75.0
                and resistance_distance_atr <= 0.35
            ):
                return False, playbook, (
                    f"CALL blocked -- strong horizontal resistance "
                    f"${_safe_float_num(resistance_level.get('level', 0.0), 0.0):.2f} is "
                    f"{resistance_distance_atr:.2f} ATR away "
                    f"(touches={resistance_touches}, strength={resistance_strength:.0f}/100)"
                )

        if (not call_side) and isinstance(support_level, dict):
            support_touches = _safe_int_num(support_level.get("touches", 0), 0)
            support_strength = _safe_float_num(support_level.get("strength", 0.0), 0.0)
            support_distance_atr = _safe_float_num(support_level.get("distance_atr", 999.0), 999.0)
            if (
                support_touches >= 2
                and support_strength >= 75.0
                and support_distance_atr <= 0.35
            ):
                return False, playbook, (
                    f"PUT blocked -- strong horizontal support "
                    f"${_safe_float_num(support_level.get('level', 0.0), 0.0):.2f} is "
                    f"{support_distance_atr:.2f} ATR away "
                    f"(touches={support_touches}, strength={support_strength:.0f}/100)"
                )

    # FIRST_PULLBACK is a trigger, not sufficient proof by itself. Require the 5m
    # directional score to still be building so stale pullbacks do not re-enter a fading move.
    # delta_5m is None when 5m-ago history isn't available yet (cold start/warmup gap) —
    # that's "unknown", not "failed freshness", so it must not be treated as a block.
    if (
        playbook not in ("BREAKOUT", "BREAKOUT_CONTINUATION")
        and one_minute_enabled
        and one_minute_trigger == "FIRST_PULLBACK"
        and delta_5m is not None
        and delta_5m < FIRST_PULLBACK_MIN_SCORE_DELTA
    ):
        return False, playbook, (
            f"FIRST_PULLBACK blocked -- 5m directional score delta {delta_5m} "
            f"< +{FIRST_PULLBACK_MIN_SCORE_DELTA}; waiting for fresh continuation"
        )

    is_breakout_continuation = continuation_confirmed is True
    min_score = (
        BREAKOUT_CONTINUATION_MIN_SCORE if is_breakout_continuation
        else BREAKOUT_MIN_SCORE if playbook == "BREAKOUT"
        else PULLBACK_MIN_SCORE
    )
    # V2: raw score is Trend evidence for Unified Entry Quality, not a second hard
    # veto here — the outer hard-score gate is already advisory-only under V2.
    score_gate_authority = not (V2_ENTRY_QUALITY_ENABLED and TWO_PLAYBOOK_ENTRY_MODE)
    if score_gate_authority and score < min_score:
        return False, playbook, f"score {score:.0f} < {min_score}"
    if is_breakout_continuation:
        pass
    elif score < SCORE_STRONG:
        # V2: early-entry volume ratio is Volume/Trigger evidence, not a binary kill
        # switch — the 1m sniper (e.g. BREAKOUT_RETEST) already measures its own volume.
        if score_gate_authority and volume_ratio < VOLUME_MULTIPLIER:
            return False, playbook, (
                f"early-entry volume ratio {volume_ratio:.2f} < {VOLUME_MULTIPLIER:.2f}"
            )
        if score_gate_authority and (delta_5m is None or delta_5m <= 0):
            return False, playbook, (
                f"early-entry 5m score delta {delta_5m} is not rising"
            )
    elif score_gate_authority and volume_ratio < 1.0:
        return False, playbook, (
            f"confirmed-entry volume ratio {volume_ratio:.2f} < 1.00"
        )
    if (score - opposite) < PLAYBOOK_MIN_DOMINANCE:
        return False, playbook, f"dominance {score - opposite:.0f} < {PLAYBOOK_MIN_DOMINANCE}"
    if call_side and not (price > vwap and price > ema20):
        return False, playbook, "CALL lacks VWAP/EMA20 alignment"
    if not call_side and not (price < vwap and price < ema20):
        return False, playbook, "PUT lacks VWAP/EMA20 alignment"
    if call_side and score <= MARGINAL_CALL_SCORE_CEIL:
        marginal_call_ok, marginal_call_reason = marginal_call_entry_ok(score, data)
        if not marginal_call_ok:
            return False, playbook, marginal_call_reason

    # Tiered VWAP extension: NORMAL passes freely, EXTENDED requires strong
    # confirmation, VERY_EXTENDED is never overridden (proven risk guard, frozen).
    dominance_val = score - opposite
    pattern_quality = _safe_float_num(
        (data or {}).get("pattern_quality_score_bull" if call_side else "pattern_quality_score_bear", 0.0), 0.0
    )
    _log_vwap_shadow_telemetry(symbol, side, extension, playbook, one_minute_trigger, score, dominance_val, volume_ratio, pattern_quality)
    if extension > PLAYBOOK_VWAP_EXTENDED_MAX:
        return False, playbook, (
            f"VWAP extension {extension * 100:.2f}% > {PLAYBOOK_VWAP_EXTENDED_MAX * 100:.2f}% (VERY_EXTENDED)"
        )
    if extension > PLAYBOOK_MAX_VWAP_EXTENSION:
        extended_override = (
            playbook == "PULLBACK_CONTINUATION"
            and one_minute_trigger == "FIRST_PULLBACK"
            and score >= PLAYBOOK_VWAP_EXTENDED_OVERRIDE_MIN_SCORE
            and dominance_val >= PLAYBOOK_VWAP_EXTENDED_OVERRIDE_MIN_DOMINANCE
            and volume_ratio >= PLAYBOOK_VWAP_EXTENDED_OVERRIDE_MIN_VOLX
            and pattern_quality >= PLAYBOOK_VWAP_EXTENDED_OVERRIDE_MIN_PATTERN
        )
        if not extended_override:
            return False, playbook, (
                f"VWAP extension {extension * 100:.2f}% > {PLAYBOOK_MAX_VWAP_EXTENSION * 100:.2f}% (EXTENDED, "
                f"confirmation insufficient for override)"
            )
    if delta_5m is not None and delta_5m < -4:
        return False, playbook, f"directional score fading ({delta_5m:+d} over 5m)"
    if is_breakout_continuation:
        return True, playbook, (
            f"{continuation_reason}; score={score:.0f}, "
            f"dominance={score - opposite:.0f}, 5m_delta={delta_5m}"
        )
    return True, playbook, f"score={score:.0f}, dominance={score - opposite:.0f}, 5m_delta={delta_5m}"


def promoted_entry_quality_ok(symbol, side, data):
    """Unified quality gate for promoted watchlist entries used by all relaxed paths."""
    m = _side_metric_bundle(data or {}, side)
    side_delta_5m, side_delta_10m = _score_trend_deltas(data or {}, side)
    entry_timing = _classify_entry_timing(data or {}, side)
    is_etf = str(symbol or "").upper() in ETF_SYMBOLS

    allowed_timings = {
        "FIRST_PULLBACK",
        "VWAP_RECLAIM",
        "HIGHER_LOW_RECLAIM",
        "BREAKDOWN_RETEST",
        "LOWER_HIGH_FAILURE",
        "VWAP_REJECT",
        "MOMENTUM_CONTINUATION",
        "EARLY_BREAKOUT",
        "BREAKDOWN",
    }
    if entry_timing not in allowed_timings:
        return False, f"timing {entry_timing} not in promoted-quality allowlist"

    min_dom = max(int(PROMOTED_MIN_DOMINANCE), int(SCORE_DOMINANCE))
    if m["dominance"] < min_dom:
        return False, f"dominance {m['dominance']:.0f} < {min_dom}"

    min_d5 = max(int(PROMOTED_MIN_DELTA_5M), int(EARLY_IGNITION_MIN_DELTA_5M))
    if side_delta_5m is None or side_delta_5m < min_d5:
        return False, f"side 5m delta {side_delta_5m} < {min_d5}"

    min_mq = max(float(PROMOTED_MIN_MOMENTUM_QUALITY), float(ENTRY_CONT_MIN_MOMENTUM_QUALITY) - 1.0)
    if m["momentum_quality"] < min_mq:
        return False, f"momentum quality {m['momentum_quality']:.1f} < {min_mq:.1f}"

    min_momentum = 26.0 if is_etf else 32.0
    if m["momentum"] < min_momentum:
        return False, f"momentum {m['momentum']:.1f} < {min_momentum:.1f}"

    if side_delta_10m is not None and side_delta_10m < 0:
        return False, f"side 10m delta {side_delta_10m:+d} < +0 (fading)"

    return True, (
        f"timing={entry_timing}, dom={m['dominance']:.0f}, d5={side_delta_5m}, "
        f"d10={side_delta_10m}, mom={m['momentum']:.1f}, mq={m['momentum_quality']:.1f}"
    )


def _why_now_line(data, side):
    """Plain-language explanation of why this side is actionable now."""
    side = str(side or "").upper()
    dominant = int(data.get("bull_score", 0)) if side == "CALL" else int(data.get("bear_score", 0))
    opposite = int(data.get("bear_score", 0)) if side == "CALL" else int(data.get("bull_score", 0))
    delta_5m, delta_10m = _score_trend_deltas(data, side)
    direction = "buyers" if side == "CALL" else "sellers"
    pieces = [
        f"{direction} are in control ({dominant} vs {opposite})",
        f"{side} edge +{max(0, dominant - opposite)}",
    ]
    if delta_5m is not None:
        momentum = "building" if delta_5m > 0 else "steady" if delta_5m == 0 else "cooling"
        pieces.append(f"momentum {momentum} over 5m ({delta_5m:+d})")
    if delta_10m is not None:
        pieces.append(f"10m trend {delta_10m:+d}")
    return " | ".join(pieces)


def _classify_entry_timing(data, side):
    """Classify the setup type using score trend and directional structure."""
    side = str(side or "").upper()
    delta_5m, delta_10m = _score_trend_deltas(data or {}, side)
    side_score = int((data or {}).get("bull_score", 0)) if side == "CALL" else int((data or {}).get("bear_score", 0))
    rsi = _safe_float_num((data or {}).get("rsi", 50.0), 50.0)
    price = _safe_float_num((data or {}).get("price", 0.0), 0.0)
    vwap = _safe_float_num((data or {}).get("vwap", 0.0), 0.0)
    ema20 = _safe_float_num((data or {}).get("ema20", 0.0), 0.0)
    recent_high = _safe_float_num((data or {}).get("recent_high", 0.0), 0.0)
    recent_low = _safe_float_num((data or {}).get("recent_low", 0.0), 0.0)
    micro_high = _safe_float_num((data or {}).get("micro_high", recent_high), recent_high)
    micro_low = _safe_float_num((data or {}).get("micro_low", recent_low), recent_low)
    prior_bar_high = _safe_float_num((data or {}).get("prior_bar_high", price), price)
    prior_bar_low = _safe_float_num((data or {}).get("prior_bar_low", price), price)
    ext_pct = _safe_float_num((data or {}).get("vwap_extension_pct", 0.0), 0.0)
    vwap_distance_prev = _safe_float_num((data or {}).get("vwap_distance_prev", 0.0), 0.0)
    bullish_candle = bool((data or {}).get("bullish_candle", False))
    bearish_candle = bool((data or {}).get("bearish_candle", False))
    fresh_breakout = bool((data or {}).get("fresh_breakout", False))
    fresh_breakdown = bool((data or {}).get("fresh_breakdown", False))
    trend_age_bars = _safe_int_num(
        (data or {}).get("bull_trend_age_bars", 0) if side == "CALL" else (data or {}).get("bear_trend_age_bars", 0),
        0,
    )
    moving_away_bullish = bool(price > vwap and (price - vwap) > vwap_distance_prev)
    moving_away_bearish = bool(price < vwap and (price - vwap) < vwap_distance_prev)
    reclaim_ready_bull = delta_5m is not None and delta_5m >= 1 and (bullish_candle or moving_away_bullish)
    reclaim_ready_bear = delta_5m is not None and delta_5m >= 1 and (bearish_candle or moving_away_bearish)
    pullback_ready = delta_5m is not None and 1 <= delta_5m <= 7 and ext_pct <= (MAX_EXT_FROM_VWAP * 0.85)
    early_reclaim_bull = (
        side == "CALL"
        and price > vwap
        and price > ema20
        and bullish_candle
        and price > prior_bar_high
        and micro_low >= min(vwap, ema20) * 0.997
        and (delta_5m is None or delta_5m >= 0)
        and ext_pct <= (MAX_EXT_FROM_VWAP * 0.75)
    )
    early_failure_bear = (
        side == "PUT"
        and price < vwap
        and price < ema20
        and bearish_candle
        and price < prior_bar_low
        and micro_high <= max(vwap, ema20) * 1.003
        and (delta_5m is None or delta_5m >= 0)
        and ext_pct <= (MAX_EXT_FROM_VWAP * 0.75)
    )

    if side == "CALL" and delta_5m is not None and delta_10m is not None and delta_5m >= 8 and delta_10m >= 10 and fresh_breakout and bullish_candle and moving_away_bullish:
        return "EARLY_BREAKOUT"
    if side == "PUT" and delta_5m is not None and delta_10m is not None and delta_5m >= 8 and delta_10m >= 10 and fresh_breakdown and bearish_candle and moving_away_bearish:
        return "BREAKDOWN"

    if early_reclaim_bull:
        return "HIGHER_LOW_RECLAIM"
    if early_failure_bear:
        return "LOWER_HIGH_FAILURE"

    if side == "CALL" and price > vwap and vwap_distance_prev <= 0 and reclaim_ready_bull:
        return "VWAP_RECLAIM"
    if side == "PUT" and price < vwap and vwap_distance_prev >= 0 and reclaim_ready_bear:
        return "VWAP_REJECT"

    if side == "CALL" and price > ema20 and price > vwap and pullback_ready and (bullish_candle or moving_away_bullish or trend_age_bars <= ENTRY_MAX_TREND_AGE_BARS):
        return "FIRST_PULLBACK"
    if side == "PUT" and price < ema20 and price < vwap and pullback_ready and (bearish_candle or moving_away_bearish or trend_age_bars <= ENTRY_MAX_TREND_AGE_BARS):
        return "BREAKDOWN_RETEST"

    if side == "CALL" and rsi >= 70 and (delta_5m is None or delta_5m <= 0):
        return "LATE_CHASE"
    if side == "PUT" and rsi <= 30 and (delta_5m is None or delta_5m <= 0):
        return "LATE_CHASE"

    if trend_age_bars > ENTRY_MAX_TREND_AGE_BARS and delta_5m is not None and delta_5m <= 0:
        return "LATE_CHASE"

    if side_score >= 80 and delta_5m is not None and delta_5m <= -4:
        return "LATE_CHASE"

    if delta_5m is not None and delta_5m >= 2:
        return "MOMENTUM_CONTINUATION"

    return "MEAN_REVERSION_RISK"


def _tier_rank(tier):
    return _TIER_RANK.get(str(tier or "").upper(), 0)


def _opportunity_score_from_data(data):
    """Estimate opportunity strength for scan prioritization."""
    tier = str(data.get("tier", "NONE") or "NONE").upper()
    side = str(data.get("side", "NO TRADE") or "NO TRADE").upper()
    bull = int(data.get("bull_score", 0))
    bear = int(data.get("bear_score", 0))
    dominant = max(bull, bear)
    dominance = abs(bull - bear)
    delta_5m, _ = _score_trend_deltas(data, "CALL" if side == "CALL" else "PUT")
    delta_boost = max(0, int(delta_5m or 0))
    tier_boost = {"STRONG": 30, "SIGNAL": 15, "WATCH": 5}.get(tier, 0)
    news_boost = {"HIGH": 8, "MEDIUM": 4, "LOW": 0}.get(str(data.get("news_impact_label", "LOW") or "LOW").upper(), 0)
    return float(dominant + (0.8 * dominance) + (1.2 * delta_boost) + tier_boost + news_boost)


def entry_candidate_rank(candidate):
    """Rank executable candidates from one scan before committing capital."""
    data = candidate.get("data", {})
    side = candidate.get("side", "")
    raw_score = _safe_float_num(data.get("effective_score", 0.0), 0.0)
    opposite_score = _safe_float_num(data.get("bear_score" if side == "CALL" else "bull_score", 0.0), 0.0)
    dominance = max(0.0, raw_score - opposite_score)
    delta_5m, _ = _score_trend_deltas(data, side)
    extension = abs(_safe_float_num(data.get("vwap_extension_pct", 0.0), 0.0))
    option_score = option_candidate_rank(candidate.get("option", {}))
    playbook_bonus = 5.0 if candidate.get("playbook") == "BREAKOUT" else 0.0
    return raw_score + (0.5 * dominance) + max(0.0, float(delta_5m or 0)) + playbook_bonus + (10.0 * option_score) - (1000.0 * extension)


def execute_ranked_candidates(candidates):
    """Execute only the highest-ranked valid candidates from a shared scan."""
    if not candidates:
        return

    ranked = sorted(candidates, key=entry_candidate_rank, reverse=True)
    for candidate in ranked[:MAX_NEW_ENTRIES_PER_CYCLE]:
        symbol = candidate["symbol"]
        side = candidate["side"]
        rank = entry_candidate_rank(candidate)
        if not NO_GATING_MODE:
            _alerted_today["keys"].add(candidate["alert_key"])
        log(
            f"[{symbol}] Candidate selected: {candidate['playbook']} {side} "
            f"rank={rank:.1f} ({len(ranked)} eligible, top {MAX_NEW_ENTRIES_PER_CYCLE} executable)."
        )
        try:
            opened = try_open_paper_trade(symbol, side, candidate["option"], candidate["data"])
        except Exception:
            log(f"[{symbol}] Selected candidate execution error:")
            traceback.print_exc()
            sys.stdout.flush()
            opened = False
        if not opened and (not NO_GATING_MODE) and ALERT_ONLY_COOLDOWN_MINUTES > 0:
            _alert_cooldowns[candidate["alert_key"]] = datetime.now(central) + timedelta(minutes=ALERT_ONLY_COOLDOWN_MINUTES)


def _update_symbol_opportunity_cache(symbol, data):
    """Store latest signal strength snapshot for priority scheduling."""
    if not symbol or not data:
        return
    _symbol_opportunity_cache[symbol] = {
        "updated_at": datetime.now(central),
        "score": _opportunity_score_from_data(data),
        "tier": str(data.get("tier", "NONE") or "NONE").upper(),
        "side": str(data.get("side", "NO TRADE") or "NO TRADE").upper(),
    }


def _order_symbols_by_priority(symbols):
    """Sort symbols by current opportunity score while preserving deterministic fallback order."""
    if not symbols:
        return []

    ordered_unique = list(dict.fromkeys(symbols))
    if not ENABLE_PRIORITY_SCANNING:
        return ordered_unique

    def _key(sym):
        rec = _symbol_opportunity_cache.get(sym, {})
        return (
            float(rec.get("score", -1.0)),
            _tier_rank(rec.get("tier", "NONE")),
            -(ordered_unique.index(sym)),
        )

    return sorted(ordered_unique, key=_key, reverse=True)


def _maybe_send_transition_alert(symbol, data):
    """Send a low-noise Discord update only when signal state transitions."""
    if not data:
        return

    now_ct = datetime.now(central)
    current_tier = str(data.get("tier", "NONE") or "NONE").upper()
    current_side = str(data.get("side", "NO TRADE") or "NO TRADE").upper()
    current_signal = str(data.get("signal", "NO TRADE") or "NO TRADE")
    current = {
        "tier": current_tier,
        "side": current_side,
        "signal": current_signal,
        "score": int(data.get("bull_score", 0)) if current_side == "CALL" else int(data.get("bear_score", 0)),
    }
    previous = _last_symbol_state.get(symbol)
    _last_symbol_state[symbol] = {**current, "at": now_ct}

    if not ENABLE_STATE_TRANSITION_ALERTS:
        return
    if current_tier == "NONE" or current_side not in ("CALL", "PUT"):
        return
    min_rank = _tier_rank(TRANSITION_ALERT_MIN_TIER)
    if _tier_rank(current_tier) < min_rank:
        return
    if previous and previous.get("tier") == current_tier and previous.get("side") == current_side:
        return

    last_sent = _last_transition_alert_at.get(symbol)
    if last_sent and (now_ct - last_sent).total_seconds() < max(0, TRANSITION_ALERT_COOLDOWN_SECONDS):
        return

    from_state = "NONE"
    if previous:
        from_state = f"{previous.get('tier', 'NONE')} {previous.get('side', '')}".strip()
    to_state = f"{current_tier} {current_side}".strip()
    why_now = _why_now_line(data, current_side)
    news_impact = str(data.get("news_impact_label", "LOW") or "LOW").upper()
    send_discord(
        f"🔁 **STATE CHANGE — {symbol}**\n"
        f"`{from_state}` → `{to_state}`\n"
        f"Signal: `{current_signal}`\n"
        f"Why now: `{why_now}`\n"
        f"News impact: `{news_impact}`",
        color=DISCORD_COLOR_WARN,
        webhook_url=DISCORD_WEBHOOK_AI_STATUS_URL or DISCORD_WEBHOOK_URL,
    )
    _last_transition_alert_at[symbol] = now_ct


def position_qty_from_score(score, dominance=0):
    """Return position size based on entry score grade.

    Grade A (score >= GRADE_SCORE_A) -> GRADE_QTY_A  (e.g. 6 contracts)
    Grade B (score >= GRADE_SCORE_B) -> GRADE_QTY_B  (e.g. 4 contracts)
    Grade C (score >= GRADE_SCORE_C) -> GRADE_QTY_C  (e.g. 3 contracts)
    Grade D (below C threshold)      -> GRADE_QTY_D  (e.g. 2 contracts)
    """
    if GRADE_BASED_QTY:
        score_int = int(score)
        if score_int >= GRADE_SCORE_A:
            grade, qty = "A", GRADE_QTY_A
        elif score_int >= GRADE_SCORE_B:
            grade, qty = "B", GRADE_QTY_B
        elif score_int >= GRADE_SCORE_C:
            grade, qty = "C", GRADE_QTY_C
        else:
            grade, qty = "D", GRADE_QTY_D
        log(f"[SIZING] score={score_int} → Grade {grade} → {qty} contracts")
        return qty

    # Legacy: confidence-boost sizing off score delta.
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
    # Wilder ATR(14): volatility diagnostic only. It does NOT change trade decisions.
    prev_close = df["close"].shift(1)
    true_range = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)

    df["ATR14"] = true_range.ewm(
        alpha=1 / 14,
        min_periods=14,
        adjust=False
    ).mean()

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
def detect_horizontal_levels(df, price, atr14, lookback=60):
    """
    Diagnostic-only horizontal support/resistance detector.

    Finds local swing highs/lows, clusters nearby levels, and reports
    the strongest levels near the current price.

    DOES NOT approve or reject trades.
    """
    if df is None or len(df) < 15 or not atr14 or atr14 <= 0:
        return {
            "support": None,
            "resistance": None,
        }

    window = df.iloc[-min(lookback, len(df)) - 1:-1].copy()

    if len(window) < 10:
        return {
            "support": None,
            "resistance": None,
        }

    highs = []
    lows = []

    # 2-bar pivot detection.
    for i in range(2, len(window) - 2):
        row = window.iloc[i]

        high_val = float(row["high"])
        low_val = float(row["low"])

        left_highs = window["high"].iloc[i - 2:i]
        right_highs = window["high"].iloc[i + 1:i + 3]

        left_lows = window["low"].iloc[i - 2:i]
        right_lows = window["low"].iloc[i + 1:i + 3]

        if high_val >= float(left_highs.max()) and high_val >= float(right_highs.max()):
            highs.append(high_val)

        if low_val <= float(left_lows.min()) and low_val <= float(right_lows.min()):
            lows.append(low_val)

    # Cluster tolerance scales with volatility.
    cluster_tolerance = max(
        atr14 * 0.30,
        price * 0.0010,
    )

    def cluster_levels(levels):
        if not levels:
            return []

        levels = sorted(levels)
        clusters = []

        for level in levels:
            if not clusters:
                clusters.append([level])
                continue

            cluster_mean = sum(clusters[-1]) / len(clusters[-1])

            if abs(level - cluster_mean) <= cluster_tolerance:
                clusters[-1].append(level)
            else:
                clusters.append([level])

        results = []

        for cluster in clusters:
            center = sum(cluster) / len(cluster)
            touches = len(cluster)

            distance_atr = abs(price - center) / atr14

            # Simple diagnostic strength score.
            strength = min(
                100.0,
                45.0
                + (touches - 1) * 18.0
                + max(0.0, 20.0 - distance_atr * 8.0),
            )

            results.append({
                "level": center,
                "touches": touches,
                "strength": strength,
                "distance_atr": distance_atr,
            })

        return results

    support_clusters = cluster_levels(
        [level for level in lows if level <= price]
    )

    resistance_clusters = cluster_levels(
        [level for level in highs if level >= price]
    )

    support = (
        max(
            support_clusters,
            key=lambda x: (x["touches"], x["strength"]),
        )
        if support_clusters
        else None
    )

    resistance = (
        max(
            resistance_clusters,
            key=lambda x: (x["touches"], x["strength"]),
        )
        if resistance_clusters
        else None
    )

    return {
        "support": support,
        "resistance": resistance,
    }

def analyze(df, client, symbol):
    """Compute weighted Bull/Bear scores (0-100) from independent factor groups.

    Factor weights:
        Trend Score       : 30%
        Momentum Score    : 25%
        Volume Score      : 5%
        Market Regime     : 15%
        Relative Strength : 5%
        Pattern Quality   : 15%
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
    prev_close = float(previous["close"])

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
    vol_ratio = volume / vol_avg if vol_avg > 0 else 1.0

    ema20_prev = float(previous["EMA20"]) if not pd.isna(previous["EMA20"]) else ema20
    ema20_slope_pct = ((ema20 - ema20_prev) / ema20_prev) if ema20_prev else 0.0
    prev_vol = float(previous["volume"]) if not pd.isna(previous["volume"]) else volume
    volume_accel = ((volume - prev_vol) / prev_vol) if prev_vol > 0 else 0.0
    momentum_quality = max(0.0, min(100.0, (abs(ema20_slope_pct) * 10000.0 * 0.6) + (max(0.0, volume_accel) * 100.0 * 0.4)))

    vwap_distance_now = price - vwap
    vwap_distance_prev = float(previous["close"]) - float(previous["VWAP"])
    moving_away_bullish = vwap_distance_now > vwap_distance_prev
    moving_away_bearish = vwap_distance_now < vwap_distance_prev

       # Intraday recent high/low (exclude current bar so a break is meaningful).
    recent_window = df.iloc[-(RECENT_HIGH_LOOKBACK + 1):-1]
    recent_high = float(recent_window["high"].max()) if len(recent_window) else price
    recent_low = float(recent_window["low"].min()) if len(recent_window) else price

    # Horizontal support/resistance diagnostics only.
    atr14 = float(latest["ATR14"]) if not pd.isna(latest.get("ATR14")) else 0.0

    horizontal_levels = detect_horizontal_levels(
        df=df,
        price=price,
        atr14=atr14,
        lookback=60,
    )

    support_level = horizontal_levels.get("support")
    resistance_level = horizontal_levels.get("resistance")

    fresh_breakout = prev_close <= recent_high and price > recent_high
    fresh_breakdown = prev_close >= recent_low and price < recent_low

    micro_window = df.iloc[-4:-1]
    micro_high = float(micro_window["high"].max()) if len(micro_window) else recent_high
    micro_low = float(micro_window["low"].min()) if len(micro_window) else recent_low
    prior_bar_high = float(previous["high"]) if not pd.isna(previous["high"]) else price
    prior_bar_low = float(previous["low"]) if not pd.isna(previous["low"]) else price

    bull_trend_mask = (df["close"] > df["VWAP"]) & (df["close"] > df["EMA20"]) & (df["close"] > df["EMA50"])
    bear_trend_mask = (df["close"] < df["VWAP"]) & (df["close"] < df["EMA20"]) & (df["close"] < df["EMA50"])

    def _consecutive_true_count(mask):
        count = 0
        for value in reversed(mask.tolist()):
            if bool(value):
                count += 1
            else:
                break
        return count

    bull_trend_age_bars = _consecutive_true_count(bull_trend_mask)
    bear_trend_age_bars = _consecutive_true_count(bear_trend_mask)

    is_etf = symbol in ETF_SYMBOLS

    # Momentum calculation applies to both ETFs and stocks (moved outside if/else).
    def _clamp01(x):
        return max(0.0, min(1.0, float(x)))

    def _to_100(x):
        return _clamp01(x) * 100.0

    # Short-term return proxy for momentum.
    close_5 = float(df["close"].iloc[-6]) if len(df) >= 6 else price
    ret_5 = ((price / close_5) - 1.0) if close_5 > 0 else 0.0

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
        regime_bull, regime_bear = _market_regime_scores(symbol, price, vwap, ema20, ema50, ema20_rising, ema20_falling)
        rel_bull = _to_100(((1.0 if price > recent_high else 0.0) * 0.60) + ((1.0 if price > ema50 else 0.0) * 0.40))
        rel_bear = _to_100(((1.0 if price < recent_low else 0.0) * 0.60) + ((1.0 if price < ema50 else 0.0) * 0.40))
        pattern_bull = _to_100(((1.0 if bullish_candle else 0.0) * 0.35) + ((1.0 if price > recent_high else 0.0) * 0.35) + ((1.0 if moving_away_bullish else 0.0) * 0.30))
        pattern_bear = _to_100(((1.0 if bearish_candle else 0.0) * 0.35) + ((1.0 if price < recent_low else 0.0) * 0.35) + ((1.0 if moving_away_bearish else 0.0) * 0.30))
        option_liq_score = 0.0
    else:

        # Median baseline: a rolling mean is dominated by the opening volume spike,
        # which scores every normal mid-session bar as zero liquidity.
        dollar_vol_now = price * volume
        dv_series = (df["close"] * df["volume"]).tail(20)
        dollar_vol_base = float(dv_series.median()) if len(dv_series) else 0.0
        liq_ratio = (dollar_vol_now / dollar_vol_base) if dollar_vol_base > 0 else 1.0
        vol_series = df["volume"].tail(20)
        vol_base = float(vol_series.median()) if len(vol_series) else 0.0
        rel_volume = (volume / vol_base) if vol_base > 0 else 1.0

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

        volume_bull = _to_100((
            (_clamp01(rel_volume / 1.5)) * 0.50
            + ((1.0 if bullish_candle else 0.0) * 0.25)
            + ((1.0 if strong_volume else 0.0) * 0.25)
        ))
        volume_bear = _to_100((
            (_clamp01(rel_volume / 1.5)) * 0.50
            + ((1.0 if bearish_candle else 0.0) * 0.25)
            + ((1.0 if strong_volume else 0.0) * 0.25)
        ))

        regime_bull, regime_bear = _market_regime_scores(symbol, price, vwap, ema20, ema50, ema20_rising, ema20_falling)

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

        option_liq_score = _to_100(_clamp01(liq_ratio / 1.5))

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

    # ========== MARKET CONTEXT VALIDATION ==========
    # Don't take CALLs in downtrends or PUTs in uptrends just because score is high
    # Validate that market structure SUPPORTS the proposed direction
    # Note: price, vwap, rsi are already computed above from latest/calculate_indicators
    
    # Distance to VWAP (%)
    price_to_vwap_pct = (price - vwap) / vwap * 100 if vwap > 0 else 0

    # Momentum: price change over last 5 bars (direction of move)
    if len(df) >= 6:
        close_5 = float(df["close"].iloc[-6])
        momentum_pct = (price - close_5) / close_5 * 100 if close_5 > 0 else 0
    else:
        momentum_pct = 0

    # Short-term momentum: last 2 bars (is move TURNING?)
    if len(df) >= 3:
        close_2 = float(df["close"].iloc[-3])
        momentum_2bar = (price - close_2) / close_2 * 100 if close_2 > 0 else 0
    else:
        momentum_2bar = 0

    def validate_call_context():
        """
        CALL valid in two scenarios:
        1. Uptrend: price above VWAP with positive momentum (breakout/continuation)
        2. Recovery: price below VWAP but momentum is TURNING positive (V-shaped rebound)
           - Must be recovering (5-bar momentum turning), not just one green bar
        Reject only when price is falling AND showing no reversal signs.
        """
        # HARD REJECT: momentum still clearly negative (move hasn't reversed)
        if momentum_pct < -1.5 and momentum_2bar < -0.3:
            return False, f"CALL rejected: still falling (5bar {momentum_pct:.1f}%, 2bar {momentum_2bar:.1f}%)"
        # HARD REJECT: price deep below VWAP AND momentum not recovering
        if price_to_vwap_pct < -3.0 and momentum_pct < 0:
            return False, f"CALL rejected: deep downtrend {price_to_vwap_pct:.1f}% below VWAP, no recovery"
        # ALLOW: Recovery trade - oversold + momentum turning positive with follow-through
        if rsi <= 35 and momentum_2bar >= 0.2 and momentum_pct >= -0.5:
            return True, (
                f"CALL allowed: recovery trade RSI {rsi:.0f}, "
                f"2bar {momentum_2bar:.2f}%, 5bar {momentum_pct:.2f}%"
            )
        # ALLOW: Trend/breakout trade - near/above VWAP and momentum aligned
        if price_to_vwap_pct >= -0.8 and momentum_2bar >= 0.05 and momentum_pct >= 0.0:
            return True, "CALL context valid: trend aligned"
        # ALLOW: Controlled pullback inside an intact uptrend — a negative 2-bar print alone
        # doesn't mean low conviction here; hand off to the playbook/1m sniper (reclaim +
        # volume confirmation) instead of killing the candidate before it gets evaluated.
        if (
            TWO_PLAYBOOK_ENTRY_MODE
            and ema20_rising
            and rsi >= 45
            and price_to_vwap_pct >= -1.0
            and momentum_pct >= -0.3
            and -1.5 <= momentum_2bar < 0.05
        ):
            return True, (
                f"CALL allowed: controlled pullback in intact uptrend (VWAP {price_to_vwap_pct:.1f}%, "
                f"2bar {momentum_2bar:.2f}%, 5bar {momentum_pct:.2f}%) — handing to playbook/1m sniper"
            )
        # REJECT: no clear bullish structure yet
        return False, (
            f"CALL rejected: low conviction (VWAP {price_to_vwap_pct:.1f}%, "
            f"2bar {momentum_2bar:.2f}%, 5bar {momentum_pct:.2f}%, RSI {rsi:.0f})"
        )

    def validate_put_context():
        """
        PUT valid in two scenarios:
        1. Downtrend: price below VWAP with negative momentum (breakdown/continuation)
        2. Reversal: price above VWAP but momentum is TURNING negative (distribution top)
           - Must be reversing, not just one red bar
        Reject only when price is rising AND showing no reversal signs.
        """
        # HARD REJECT: momentum still clearly positive (move hasn't reversed)
        if momentum_pct > 1.5 and momentum_2bar > 0.3:
            return False, f"PUT rejected: still rising (5bar {momentum_pct:.1f}%, 2bar {momentum_2bar:.1f}%)"
        # HARD REJECT: price deep above VWAP AND momentum not reversing
        if price_to_vwap_pct > 3.0 and momentum_pct > 0:
            return False, f"PUT rejected: deep uptrend {price_to_vwap_pct:.1f}% above VWAP, no reversal"
        # ALLOW: Reversal trade - overbought + momentum turning negative with follow-through
        if rsi >= 65 and momentum_2bar <= -0.2 and momentum_pct <= 0.5:
            return True, (
                f"PUT allowed: reversal trade RSI {rsi:.0f}, "
                f"2bar {momentum_2bar:.2f}%, 5bar {momentum_pct:.2f}%"
            )
        # ALLOW: Trend/breakdown trade - near/below VWAP and momentum aligned
        if price_to_vwap_pct <= 0.8 and momentum_2bar <= -0.05 and momentum_pct <= 0.0:
            return True, "PUT context valid: trend aligned"
        # ALLOW: Controlled bounce inside an intact downtrend — mirror of the CALL case;
        # hand off to the playbook/1m sniper instead of killing the candidate outright.
        if (
            TWO_PLAYBOOK_ENTRY_MODE
            and ema20_falling
            and rsi <= 55
            and price_to_vwap_pct <= 1.0
            and momentum_pct <= 0.3
            and -0.05 < momentum_2bar <= 1.5
        ):
            return True, (
                f"PUT allowed: controlled bounce in intact downtrend (VWAP {price_to_vwap_pct:.1f}%, "
                f"2bar {momentum_2bar:.2f}%, 5bar {momentum_pct:.2f}%) — handing to playbook/1m sniper"
            )
        # REJECT: no clear bearish structure yet
        return False, (
            f"PUT rejected: low conviction (VWAP {price_to_vwap_pct:.1f}%, "
            f"2bar {momentum_2bar:.2f}%, 5bar {momentum_pct:.2f}%, RSI {rsi:.0f})"
        )
    
    # ========== Decision with Context Validation ==========
    # Keep label thresholds aligned with downstream hard-entry gates.
    # Use the same well-tuned floors: ETF=67, TopStock=62, Stock=64
    _is_top_stock = symbol in TOP_STOCK_SYMBOLS
    if is_etf:
        strong_threshold = DYNAMIC_HARD_GATE_MIN_FLOOR_ETF
    elif _is_top_stock:
        strong_threshold = TOP_STOCK_HARD_GATE_MIN_FLOOR
    else:
        strong_threshold = DYNAMIC_HARD_GATE_MIN_FLOOR_STOCK
    # The dominant side must lead by SCORE_DOMINANCE points; otherwise NO TRADE.
    diff = bull_score - bear_score

    # V2: market-context validation is Context evidence, not an automatic veto —
    # the legacy path (below) still enforces it as a hard NO TRADE.
    context_gate_authority = not (TWO_PLAYBOOK_ENTRY_MODE and V2_ENTRY_QUALITY_ENABLED)
    context_valid, context_reason = True, ""
    if bull_score >= strong_threshold and diff >= SCORE_DOMINANCE:
        call_valid, call_reason = validate_call_context()
        context_valid, context_reason = call_valid, call_reason
        if call_valid or not context_gate_authority:
            side, score, tier, signal = "CALL", bull_score, "STRONG", "STRONG CALL"
            if not call_valid:
                print(f"[{symbol}] {call_reason} | Score was {bull_score} (above threshold) — advisory under V2, not blocking", flush=True)
        else:
            side, score, tier, signal = "NO TRADE", bull_score, "REJECTED", f"STRONG_CALL_CONTEXT_FAIL"
            print(f"[{symbol}] {call_reason} | Score was {bull_score} (above threshold)", flush=True)
    elif bear_score >= strong_threshold and -diff >= SCORE_DOMINANCE:
        put_valid, put_reason = validate_put_context()
        context_valid, context_reason = put_valid, put_reason
        if put_valid or not context_gate_authority:
            side, score, tier, signal = "PUT", bear_score, "STRONG", "STRONG PUT"
            if not put_valid:
                print(f"[{symbol}] {put_reason} | Score was {bear_score} (above threshold) — advisory under V2, not blocking", flush=True)
        else:
            side, score, tier, signal = "NO TRADE", bear_score, "REJECTED", f"STRONG_PUT_CONTEXT_FAIL"
            print(f"[{symbol}] {put_reason} | Score was {bear_score} (above threshold)", flush=True)
    elif bull_score >= SCORE_SIGNAL and diff >= SCORE_DOMINANCE:
        call_valid, call_reason = validate_call_context()
        context_valid, context_reason = call_valid, call_reason
        if call_valid or not context_gate_authority:
            side, score, tier, signal = "CALL", bull_score, "SIGNAL", "CALL"
        else:
            side, score, tier, signal = "NO TRADE", bull_score, "REJECTED", "SIGNAL_CALL_CONTEXT_FAIL"
    elif bear_score >= SCORE_SIGNAL and -diff >= SCORE_DOMINANCE:
        put_valid, put_reason = validate_put_context()
        context_valid, context_reason = put_valid, put_reason
        if put_valid or not context_gate_authority:
            side, score, tier, signal = "PUT", bear_score, "SIGNAL", "PUT"
        else:
            side, score, tier, signal = "NO TRADE", bear_score, "REJECTED", "SIGNAL_PUT_CONTEXT_FAIL"
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
        # ATR diagnostics only - does NOT block or approve trades.
    atr14 = float(latest.get("ATR14", float("nan")))
    vwap_dist_atr = float("nan")
    ema20_dist_atr = float("nan")

    if pd.notna(atr14) and atr14 > 0:
        vwap_dist_atr = abs(price - vwap) / atr14 if vwap else float("nan")
        ema20_dist_atr = abs(price - ema20) / atr14 if ema20 else float("nan")

        structure_level = None
        structure_label = "nearest-structure"

        if side == "CALL":
            structure_level = recent_high
            structure_label = "resistance"
        elif side == "PUT":
            structure_level = recent_low
            structure_label = "support"
        else:
            if abs(price - recent_low) <= abs(recent_high - price):
                structure_level = recent_low
            else:
                structure_level = recent_high

        if structure_level is not None:
            structure_dist_atr = abs(price - structure_level) / atr14
        else:
            structure_dist_atr = float("nan")

        print(
            f"[{symbol}]   ATR14(5m)={atr14:.4f} | "
            f"VWAP dist={vwap_dist_atr:.2f} ATR | "
            f"EMA20 dist={ema20_dist_atr:.2f} ATR | "
            f"{structure_label} dist={structure_dist_atr:.2f} ATR",
            flush=True,
        )

        # Horizontal S/R diagnostics only.
        if support_level:
            print(
                f"[{symbol}]   H-SUPPORT ${support_level['level']:.2f} | "
                f"touches={support_level['touches']} | "
                f"strength={support_level['strength']:.0f}/100 | "
                f"distance={support_level['distance_atr']:.2f} ATR",
                flush=True,
            )
        else:
            print(f"[{symbol}]   H-SUPPORT none", flush=True)

        if resistance_level:
            print(
                f"[{symbol}]   H-RESISTANCE ${resistance_level['level']:.2f} | "
                f"touches={resistance_level['touches']} | "
                f"strength={resistance_level['strength']:.0f}/100 | "
                f"distance={resistance_level['distance_atr']:.2f} ATR",
                flush=True,
            )
        else:
            print(f"[{symbol}]   H-RESISTANCE none", flush=True)

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
        "bar_time": latest.name,
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
        "support_level": support_level,
        "resistance_level": resistance_level,
        "micro_high": micro_high,
        "micro_low": micro_low,
        "prior_bar_high": prior_bar_high,
        "prior_bar_low": prior_bar_low,
        "fresh_breakout": fresh_breakout,
        "fresh_breakdown": fresh_breakdown,
        "bull_trend_age_bars": bull_trend_age_bars,
        "bear_trend_age_bars": bear_trend_age_bars,
        "vwap_distance_now": vwap_distance_now,
        "vwap_distance_prev": vwap_distance_prev,
        "vwap_extension_pct": (abs(vwap_distance_now) / vwap) if vwap else 0.0,
        "vol_ratio": vol_ratio,
        "ema20_slope_pct": ema20_slope_pct,
        "volume_accel": volume_accel,
        "momentum_quality": momentum_quality,
        "atr14": atr14,
        "vwap_dist_atr": vwap_dist_atr,
        "ema20_dist_atr": ema20_dist_atr,
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
        "context_valid": context_valid,
        "context_reason": context_reason,
    }

    return side, data

_option_search_cache: "dict[tuple, dict]" = {}


def _option_search_price_bucket(price):
    """Quantize price into a fixed-width bucket so the cache tolerates small ticks
    but still refreshes once the underlying has moved meaningfully.
    """
    price = float(price or 0.0)
    if price >= 500:
        step = 0.50
    elif price >= 100:
        step = 0.10
    else:
        step = 0.02
    return round(price / step) if step > 0 else 0


def _liquidity_tier(symbol):
    """Shadow classification only — does not gate anything yet. Lets us log what
    tier-based thresholds would decide before committing to new hard numbers.
    """
    symbol = str(symbol or "").upper()
    if symbol in ETF_SYMBOLS:
        return "ETF"
    if symbol in MEGA_LIQUID_SYMBOLS:
        return "MEGA_LIQUID"
    return "STANDARD"


def _shadow_tier_liquidity_check(symbol, oi, vol, spread_pct):
    """Log-only: what a symbol-class-aware liquidity rule would decide, so real
    thresholds can eventually be set from evidence instead of intuition.
    """
    tier = _liquidity_tier(symbol)
    thresholds = TIER_LIQUIDITY_THRESHOLDS.get(tier, TIER_LIQUIDITY_THRESHOLDS["STANDARD"])
    would_pass = (
        spread_pct <= thresholds["max_spread_pct"]
        and (oi >= thresholds["min_oi"] or vol >= thresholds["min_volume"])
    )
    log(
        f"[{symbol}] [SHADOW TIER={tier}] would_pass={would_pass} "
        f"(oi={oi}>={thresholds['min_oi']} or vol={vol}>={thresholds['min_volume']}, "
        f"spread={spread_pct*100:.1f}%<={thresholds['max_spread_pct']*100:.0f}%)"
    )


def _log_contract_data_quality(symbol, candidate):
    """Measurement only: distinguish live quote/trade data from stale OI metadata
    so a rejection can be attributed to genuine illiquidity vs. a data-freshness gap.
    """
    try:
        bid = float(candidate.get("bid", 0.0) or 0.0)
        ask = float(candidate.get("ask", 0.0) or 0.0)
        bid_size = int(candidate.get("bid_size", 0) or 0)
        ask_size = int(candidate.get("ask_size", 0) or 0)
        mid = (bid + ask) / 2 if (bid + ask) > 0 else 0.0
        spread_pct = float(candidate.get("spread_pct", 0.0) or 0.0)
        vol = int(candidate.get("volume", 0) or 0)
        oi = int(candidate.get("open_interest", 0) or 0)
        oi_date_raw = str(candidate.get("open_interest_date", "") or "")
        oi_age_days = "unknown"
        try:
            if oi_date_raw:
                oi_age_days = (date.today() - date.fromisoformat(oi_date_raw[:10])).days
        except Exception:
            oi_age_days = "unknown"
        quote_ts = candidate.get("quote_timestamp")
        trade_ts = candidate.get("trade_timestamp")
        log(
            f"[{symbol}] Contract data check: {candidate.get('contract', '')} "
            f"bid=${bid:.2f}({bid_size}) ask=${ask:.2f}({ask_size}) mid=${mid:.2f} spread={spread_pct*100:.1f}% "
            f"quote_ts={quote_ts or 'unavailable'} trade_ts={trade_ts or 'unavailable'} "
            f"day_vol={vol} oi={oi} oi_asof={oi_date_raw or 'unknown'} oi_age_days={oi_age_days}"
        )
        _shadow_tier_liquidity_check(symbol, oi, vol, spread_pct)
    except Exception as e:
        log(f"[{symbol}] Contract data quality logging failed (non-blocking): {e}")


def get_option_contract(symbol, signal, underlying_price, data=None, max_ext_from_vwap=None):
    """Cached wrapper around ``_get_option_contract_uncached``.

    Short-lived cache only — never changes which contract is selected or rejected,
    it just avoids re-running the same expensive Alpaca chain/snapshot query within
    a few seconds of an identical prior search.
    """
    if not OPTION_SEARCH_CACHE_ENABLED:
        return _get_option_contract_uncached(symbol, signal, underlying_price, data=data, max_ext_from_vwap=max_ext_from_vwap)

    setup_type = str((data or {}).get("entry_playbook", "") or (data or {}).get("setup_type", "") or "")
    bar_time = (data or {}).get("bar_time")
    if setup_type and bar_time is not None:
        cache_key = (symbol, signal, setup_type, bar_time)
    else:
        cache_key = (symbol, signal, _option_search_price_bucket(underlying_price))
    now = datetime.now(central)
    cached = _option_search_cache.get(cache_key)
    if cached and cached["expires_at"] > now:
        log(f"[{symbol}] Contract search cache hit ({signal}) — reusing result from {(now - cached['cached_at']).total_seconds():.0f}s ago.")
        return cached["result"]

    result = _get_option_contract_uncached(symbol, signal, underlying_price, data=data, max_ext_from_vwap=max_ext_from_vwap)
    is_negative = result is None
    ttl_seconds = OPTION_SEARCH_NEGATIVE_CACHE_TTL_SECONDS if is_negative else OPTION_SEARCH_CACHE_TTL_SECONDS
    if is_negative:
        log(f"[{symbol}] Contract search found no valid contract — caching negative result for only {ttl_seconds}s (not the full {OPTION_SEARCH_CACHE_TTL_SECONDS}s).")
    _option_search_cache[cache_key] = {
        "result": result,
        "cached_at": now,
        "expires_at": now + timedelta(seconds=max(1, ttl_seconds)),
    }
    return result


def _get_option_contract_uncached(symbol, signal, underlying_price, data=None, max_ext_from_vwap=None):
    """Fetch the best available >=MIN_DTE option contract from Alpaca.

    When ``data`` and ``max_ext_from_vwap`` are provided, run entry-quality prechecks
    during candidate selection so we rank only contracts that are actually tradeable.
    """
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
            strike_lo = round(underlying_price * 0.95, 2)
            strike_hi = round(underlying_price * 1.05, 2)
            print(
                f"[{symbol}] No option contracts found ({option_type}, expiry {exp_range}, "
                f"strike [{strike_lo:.2f}, {strike_hi:.2f}]) — either NO_EXPIRATION_IN_WINDOW "
                f"or NO_STRIKES_IN_WINDOW for this chain.",
                flush=True,
            )
            # Diagnostic-only: same expiry window with no strike bound, to isolate whether
            # the narrow +/-5% strike band (vs a genuine expiry-availability gap) is at fault.
            # Never used for selection — logging only.
            try:
                diag_kwargs = dict(req_kwargs)
                diag_kwargs.pop("strike_price_gte", None)
                diag_kwargs.pop("strike_price_lte", None)
                diag_result = _trading_client.get_option_contracts(GetOptionContractsRequest(**diag_kwargs))
                diag_contracts = (
                    diag_result if isinstance(diag_result, list)
                    else (getattr(diag_result, "option_contracts", None) or getattr(diag_result, "contracts", None) or [])
                )
                if not diag_contracts:
                    print(
                        f"[{symbol}] DIAGNOSTIC: 0 contracts for {option_type} even with no strike bound "
                        f"in expiry {exp_range} — this expiry window has no chain at all for this name.",
                        flush=True,
                    )
                else:
                    strikes = sorted({float(c.strike_price) for c in diag_contracts})
                    print(
                        f"[{symbol}] DIAGNOSTIC: {len(diag_contracts)} contract(s) exist for {option_type} in "
                        f"expiry {exp_range} once strike bound is removed — available strikes "
                        f"{strikes[:3]}...{strikes[-3:]} (band [{strike_lo:.2f}, {strike_hi:.2f}] excluded all of them).",
                        flush=True,
                    )
            except Exception as diag_err:
                print(f"[{symbol}] DIAGNOSTIC query failed (non-blocking): {diag_err}", flush=True)
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

        passed_candidates = []
        relaxed_fallback_candidates = []

        for candidate in contracts:
            contract_sym = candidate.symbol
            exp_date = _exp_date(candidate)

            # Fetch live bid/ask/last/volume for each candidate.
            snap_req = OptionSnapshotRequest(symbol_or_symbols=contract_sym, feed=OPTIONS_FEED)
            snaps = _option_client.get_option_snapshot(snap_req)
            snap = snaps.get(contract_sym) if isinstance(snaps, dict) else snaps

            bid = ask = last = 0.0
            vol = 0
            last_trade_size = 0
            bid_size = ask_size = 0
            quote_ts = trade_ts = None
            oi = _safe_int(getattr(candidate, "open_interest", 0), 0)
            oi_date = str(getattr(candidate, "open_interest_date", "") or "")
            if snap:
                q = getattr(snap, "latest_quote", None) or getattr(snap, "quote", None)
                t = getattr(snap, "latest_trade", None) or getattr(snap, "trade", None)
                greeks = getattr(snap, "greeks", None)
                daily = getattr(snap, "daily_bar", None)

                if q is not None:
                    bid = _safe_float(getattr(q, "bid_price", 0.0), 0.0)
                    ask = _safe_float(getattr(q, "ask_price", 0.0), 0.0)
                    bid_size = _safe_int(getattr(q, "bid_size", 0), 0)
                    ask_size = _safe_int(getattr(q, "ask_size", 0), 0)
                    quote_ts = getattr(q, "timestamp", None)
                if t is not None:
                    last = _safe_float(getattr(t, "price", 0.0), 0.0)
                    last_trade_size = _safe_int(getattr(t, "size", 0), 0)
                    trade_ts = getattr(t, "timestamp", None)
                # Daily bar volume is today's cumulative flow; last-trade size is a single print.
                if daily is not None:
                    vol = _safe_int(getattr(daily, "volume", 0), 0)
                delta_val = _safe_float(getattr(greeks, "delta", 0.0), 0.0) if greeks is not None else 0.0
            else:
                delta_val = 0.0

            dte = (exp_date - today).days

            # NO_QUOTE is a data-integrity reject, distinct from a genuine thin-liquidity
            # rejection: there's nothing to trade against at all, live or stale.
            if bid <= 0.0 and ask <= 0.0:
                _reject(f"[{symbol}] Contract {contract_sym} rejected — NO_QUOTE (no live bid/ask returned).")
                continue

            if bid < min_bid:
                _reject(f"[{symbol}] Contract {contract_sym} rejected — bid ${bid:.2f} < ${min_bid:.2f} minimum.")
                continue

            mid = (bid + ask) / 2 if (bid + ask) > 0 else 0.01
            spread_pct = (ask - bid) / mid

            if not is_etf:
                # In strict mode, accept stock contracts when either intraday volume OR OI passes minimum.
                # In no-gating mode, bypass this liquidity gate and rely on bid/spread checks only.
                if (not NO_GATING_MODE) and vol < STOCK_MIN_OPTION_VOLUME and oi < STOCK_MIN_OPTION_OPEN_INTEREST:
                    liquidity_state, oi_age_days = _classify_liquidity_data_state(oi_date, vol)
                    tight_live_market = bid > 0 and ask > 0 and spread_pct <= LIQUIDITY_SUBSTITUTE_MAX_SPREAD_PCT
                    quote_age_seconds = None
                    if quote_ts is not None:
                        try:
                            now_utc = datetime.now(timezone.utc)
                            ts = quote_ts if quote_ts.tzinfo else quote_ts.replace(tzinfo=timezone.utc)
                            quote_age_seconds = (now_utc - ts).total_seconds()
                        except Exception:
                            quote_age_seconds = None
                    quote_is_fresh = quote_age_seconds is not None and quote_age_seconds <= LIQUIDITY_SUBSTITUTE_MAX_QUOTE_AGE_SECONDS

                    if liquidity_state in ("LIQUIDITY_DATA_STALE", "LIQUIDITY_DATA_MISSING") and tight_live_market and quote_is_fresh:
                        print(
                            f"[{symbol}] LIQUIDITY SUBSTITUTE (discovery stage): {contract_sym} "
                            f"metadata={liquidity_state} oi={oi} oi_date={oi_date or 'unknown'} volume={vol} "
                            f"bid={bid:.2f} ask={ask:.2f} spread={spread_pct*100:.2f}% quote_age="
                            f"{'unknown' if quote_age_seconds is None else f'{quote_age_seconds:.1f}s'}",
                            flush=True,
                        )
                    else:
                        _reject(
                            f"[{symbol}] Contract {contract_sym} rejected — volume {vol} < {STOCK_MIN_OPTION_VOLUME} "
                            f"and OI {oi} < {STOCK_MIN_OPTION_OPEN_INTEREST} [{liquidity_state}, "
                            f"quote_age={quote_age_seconds if quote_age_seconds is not None else 'unknown'}]."
                        )
                        continue
                if NO_GATING_MODE and vol < STOCK_MIN_OPTION_VOLUME and oi < STOCK_MIN_OPTION_OPEN_INTEREST:
                    print(
                        f"[{symbol}] No-gating override: accepting {contract_sym} despite low volume/OI "
                        f"(vol={vol}, oi={oi}).",
                        flush=True,
                    )

            if spread_pct > max_spread_pct:
                _reject(
                    f"[{symbol}] Contract {contract_sym} rejected — spread {spread_pct*100:.1f}% > {max_spread_pct*100:.0f}% max."
                )
                continue

            slippage_est = max(0.0, ask - bid)
            option_candidate = {
                "contract":      contract_sym,
                "expiry":        exp_date.strftime("%Y-%m-%d"),
                "dte":           dte,
                "strike":        float(candidate.strike_price),
                "bid":           bid,
                "ask":           ask,
                "bid_size":      bid_size,
                "ask_size":      ask_size,
                "last":          last,
                "volume":        vol,
                "last_trade_size": last_trade_size,
                "open_interest": oi,
                "open_interest_date": oi_date,
                "quote_timestamp": quote_ts,
                "trade_timestamp": trade_ts,
                "delta":         delta_val,
                "spread_pct":    spread_pct,
                "slippage_est":  slippage_est,
                "side":          signal,  # stored so close_trade can build the alert_key
            }

            strike_distance_pct = (
                abs(float(option_candidate["strike"]) - float(underlying_price)) / float(underlying_price)
            ) if float(underlying_price) > 0 else 1.0
            option_candidate["strike_distance_pct"] = strike_distance_pct

            if data is not None and max_ext_from_vwap is not None:
                precheck_ok, precheck_reason = entry_contract_quality_ok(
                    symbol,
                    signal,
                    data,
                    option_candidate,
                    max_ext_from_vwap,
                )
                if not precheck_ok:
                    option_candidate["_entry_precheck_reason"] = str(precheck_reason)
                    relaxed_fallback_candidates.append(option_candidate)
                    _reject(
                        f"[{symbol}] Contract {contract_sym} rejected — entry-quality precheck failed: "
                        f"{precheck_reason}."
                    )
                    continue

            passed_candidates.append(option_candidate)

        # Hard delta filter — never select a contract outside the acceptable delta band
        # (e.g. a 0.13-delta lottery ticket) just because it has better OI/spread.
        # Rejecting the contract only skips this expiry/type search, not the underlying setup —
        # the caller still searches other expiries/strikes before giving up on the trade.
        if passed_candidates:
            in_delta_band = [
                c for c in passed_candidates
                if OPTION_ACCEPTABLE_DELTA_MIN <= abs(_safe_float(c.get("delta", 0.0), 0.0)) <= OPTION_ACCEPTABLE_DELTA_MAX
            ]
            if in_delta_band:
                passed_candidates = in_delta_band
            else:
                # Below the acceptable band, allow through only with excellent liquidity —
                # never on OI alone (this is exactly how the SPY 0.13-delta contract got picked).
                fallback_band = [
                    c for c in passed_candidates
                    if OPTION_FALLBACK_DELTA_MIN <= abs(_safe_float(c.get("delta", 0.0), 0.0)) < OPTION_ACCEPTABLE_DELTA_MIN
                    and _safe_float(c.get("volume", 0.0), 0.0) >= OPTION_FALLBACK_MIN_VOLUME
                    and _safe_float(c.get("open_interest", 0.0), 0.0) >= OPTION_FALLBACK_MIN_OI
                    and _safe_float(c.get("spread_pct", 1.0), 1.0) <= OPTION_FALLBACK_MAX_SPREAD_PCT
                ]
                if fallback_band:
                    print(
                        f"[{symbol}] No contracts in preferred delta band — using fallback delta "
                        f"[{OPTION_FALLBACK_DELTA_MIN:.2f}, {OPTION_ACCEPTABLE_DELTA_MIN:.2f}) contract with "
                        f"excellent liquidity.",
                        flush=True,
                    )
                    passed_candidates = fallback_band
                else:
                    print(
                        f"[{symbol}] No contracts within acceptable delta band "
                        f"[{OPTION_ACCEPTABLE_DELTA_MIN:.2f}, {OPTION_ACCEPTABLE_DELTA_MAX:.2f}] or fallback band "
                        f"({len(passed_candidates)} candidate(s) rejected on delta) — skipping this contract search.",
                        flush=True,
                    )
                    passed_candidates = []

        if passed_candidates:
            # Prefer near-ATM contracts when available; fall back to full pool if not.
            near_atm_candidates = [
                c for c in passed_candidates
                if _safe_float(c.get("strike_distance_pct", 1.0), 1.0) <= OPTION_MAX_STRIKE_DISTANCE_PCT
            ]
            pool = near_atm_candidates if near_atm_candidates else passed_candidates

            # Prefer directional near-spot strikes first:
            # - CALL: ATM or slightly OTM (strike >= underlying)
            # - PUT:  ATM or slightly OTM (strike <= underlying)
            directional_near_spot = []
            for c in pool:
                strike = _safe_float(c.get("strike", 0.0), 0.0)
                if signal == "CALL" and strike >= float(underlying_price):
                    directional_near_spot.append(c)
                elif signal == "PUT" and strike <= float(underlying_price):
                    directional_near_spot.append(c)
            if directional_near_spot:
                pool = directional_near_spot

            ranked = sorted(pool, key=option_candidate_rank, reverse=True)
            best = ranked[0]
            print(
                f"[{symbol}] Selected contract: {best['contract']} strike={best['strike']} "
                f"expiry={best['expiry']} bid={best['bid']:.2f} ask={best['ask']:.2f} "
                f"day_vol={best['volume']} oi={best['open_interest']} "
                f"oi_asof={best.get('open_interest_date') or 'unknown'} delta={best['delta']:.2f} "
                f"dist={_safe_float(best.get('strike_distance_pct', 0.0), 0.0)*100:.2f}% "
                f"rank={option_candidate_rank(best):.3f}",
                flush=True,
            )
            _log_contract_data_quality(symbol, best)
            return best

        if (
            OPTION_RELAXED_FALLBACK_ALERT_ONLY
            and data is not None
            and max_ext_from_vwap is not None
            and relaxed_fallback_candidates
        ):
            ranked_relaxed = sorted(relaxed_fallback_candidates, key=option_candidate_rank, reverse=True)
            best_relaxed = dict(ranked_relaxed[0])
            best_relaxed["_selection_mode"] = "RELAXED_FALLBACK"
            print(
                f"[{symbol}] Relaxed fallback contract selected for alert-only path: "
                f"{best_relaxed['contract']} strike={best_relaxed['strike']} expiry={best_relaxed['expiry']} "
                f"reason={best_relaxed.get('_entry_precheck_reason', 'entry precheck failed')}",
                flush=True,
            )
            _log_contract_data_quality(symbol, best_relaxed)
            return best_relaxed

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


def fetch_1m_bars(client, symbol):
    """Fetch recent 1-minute bars for entry timing only.

    The 5-minute engine remains the setup detector. This function supplies the
    lower-timeframe confirmation used to time the actual option entry.
    """
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=4)
    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame(1, TimeFrameUnit.Minute),
        start=start,
        end=end,
        feed=DataFeed(FEED),
    )
    bars = client.get_stock_bars(req).df
    if bars is None or bars.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    if isinstance(bars.index, pd.MultiIndex):
        bars = bars.xs(symbol, level=0)
    bars = bars[["open", "high", "low", "close", "volume"]].tail(ONE_MINUTE_LOOKBACK_BARS + 5)

    # Never use a still-forming 1-minute candle for the trigger when requested.
    if ONE_MINUTE_REQUIRE_CLOSED_BAR and len(bars):
        now_utc = pd.Timestamp.now(tz="UTC")
        last_ts = pd.Timestamp(bars.index[-1])
        if last_ts.tzinfo is None:
            last_ts = last_ts.tz_localize("UTC")
        if last_ts >= now_utc.floor("min"):
            bars = bars.iloc[:-1]
    return bars.tail(ONE_MINUTE_LOOKBACK_BARS)


def one_minute_entry_timing(symbol, side, bars_1m, five_min_data):
    """Return a concrete 1m trigger for a qualified 5m directional setup.

    Trigger types: FIRST_PULLBACK, BREAKOUT_RETEST, MOMENTUM_COMPRESSION.
    No 1m trigger means the 5m setup remains a candidate but is not entered.
    """
    if not ONE_MINUTE_ENTRY_ENABLED:
        return "DISABLED", "1m sniper engine disabled"
    if bars_1m is None or len(bars_1m) < 25:
        return None, f"insufficient 1m history ({0 if bars_1m is None else len(bars_1m)}/25)"

    df = bars_1m.copy()
    df["EMA9"] = df["close"].ewm(span=9, adjust=False).mean()
    df["EMA20"] = df["close"].ewm(span=20, adjust=False).mean()
    df["VOL_AVG20"] = df["volume"].rolling(20).mean()

    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    idx = df.index
    eastern = pytz.timezone("America/New_York")
    if getattr(idx, "tz", None) is not None:
        idx_et = idx.tz_convert(eastern)
    else:
        idx_et = idx.tz_localize("UTC").tz_convert(eastern)
    today_et = datetime.now(eastern).date()
    session_mask = pd.Series([ts.date() == today_et for ts in idx_et], index=df.index, dtype=bool)
    vwap = pd.Series(float("nan"), index=df.index, dtype=float)
    if session_mask.any():
        tv = typical[session_mask] * df.loc[session_mask, "volume"]
        vwap[session_mask] = tv.cumsum() / df.loc[session_mask, "volume"].cumsum()
    df["VWAP"] = vwap

    latest = df.iloc[-1]
    prev = df.iloc[-2]
    recent = df.iloc[-(ONE_MINUTE_MAX_TRIGGER_AGE_BARS + 1):]
    avg_vol = float(latest["VOL_AVG20"]) if pd.notna(latest["VOL_AVG20"]) else 0.0
    vol_ratio = float(latest["volume"] / avg_vol) if avg_vol > 0 else 1.0

    price = float(latest["close"])
    ema9 = float(latest["EMA9"])
    ema20 = float(latest["EMA20"])
    one_vwap = float(latest["VWAP"]) if pd.notna(latest["VWAP"]) else price
    ema9_slope = float(latest["EMA9"] - df["EMA9"].iloc[-4]) if len(df) >= 4 else 0.0

    five_price = float(five_min_data.get("price", price))
    five_vwap = float(five_min_data.get("vwap", price))
    five_ema20 = float(five_min_data.get("ema20", price))
    five_ema50 = float(five_min_data.get("ema50", price))

    bullish = price > float(latest["open"])
    bearish = price < float(latest["open"])

    if side == "CALL":
        five_aligned = five_price > five_vwap and five_price > five_ema20 and five_price > five_ema50
        micro_high = float(recent["high"].iloc[:-1].max()) if len(recent) > 1 else float(prev["high"])
        pullback_touch = float(recent["low"].min()) <= ema9 * (1.0 + ONE_MINUTE_EMA9_TOUCH_TOLERANCE)
        # Reclaim within the last N closed bars (not just the single latest bar vs. the
        # immediately prior bar) — a bullish candle closing back above the pre-pullback
        # high, with price still holding above EMA9 now.
        reclaim_window = df.iloc[-ONE_MINUTE_RECLAIM_WINDOW_BARS:]
        reclaim = bool(
            ((reclaim_window["close"] > reclaim_window["open"]) & (reclaim_window["close"] > micro_high)).any()
        ) and price > ema9
        near_vwap = abs(price - one_vwap) / max(price, 0.01) <= ONE_MINUTE_LEVEL_RETEST_TOLERANCE
        level_retest = float(latest["low"]) <= micro_high * (1.0 + ONE_MINUTE_LEVEL_RETEST_TOLERANCE) and price > micro_high and bullish
        compression_window = df.iloc[-4:-1]
        compression = False
        if len(compression_window) == 3:
            rng = (float(compression_window["high"].max()) - float(compression_window["low"].min())) / max(float(latest["close"]), 0.01)
            compression = rng <= ONE_MINUTE_COMPRESSION_PCT
        momentum_break = compression and price > float(compression_window["high"].max()) and bullish and vol_ratio >= ONE_MINUTE_MIN_VOLUME_RATIO

        trigger_move_pct = abs(price - float(prev["close"])) / max(abs(float(prev["close"])), 0.01) if float(prev["close"]) else 0.0
        if five_aligned and ema9_slope > 0 and pullback_touch and reclaim and (near_vwap or price > five_vwap) and vol_ratio >= ONE_MINUTE_MIN_VOLUME_RATIO and trigger_move_pct >= ONE_MINUTE_MIN_TRIGGER_MOVE_PCT:
            return "FIRST_PULLBACK", f"1m EMA9 pullback/reclaim confirmed; volx={vol_ratio:.2f}"
        if five_aligned and level_retest and vol_ratio >= ONE_MINUTE_MIN_VOLUME_RATIO:
            return "BREAKOUT_RETEST", f"1m breakout retest confirmed; volx={vol_ratio:.2f}"
        if five_aligned and momentum_break:
            return "MOMENTUM_COMPRESSION", f"1m compression expansion confirmed; volx={vol_ratio:.2f}"

        return None, (
            f"waiting 1m CALL trigger; price={price:.2f}, EMA9={ema9:.2f}, VWAP1m={one_vwap:.2f}, "
            f"volx={vol_ratio:.2f}, ema9_slope={ema9_slope:+.4f}"
        )

    five_aligned = five_price < five_vwap and five_price < five_ema20 and five_price < five_ema50
    micro_low = float(recent["low"].iloc[:-1].min()) if len(recent) > 1 else float(prev["low"])
    pullback_touch = float(recent["high"].max()) >= ema9 * (1.0 - ONE_MINUTE_EMA9_TOUCH_TOLERANCE)
    # Mirror of the CALL-side reclaim window: bearish candle closing back below the
    # pre-bounce low within the last N bars, with price still holding below EMA9 now.
    reclaim_window = df.iloc[-ONE_MINUTE_RECLAIM_WINDOW_BARS:]
    reclaim = bool(
        ((reclaim_window["close"] < reclaim_window["open"]) & (reclaim_window["close"] < micro_low)).any()
    ) and price < ema9
    near_vwap = abs(price - one_vwap) / max(price, 0.01) <= ONE_MINUTE_LEVEL_RETEST_TOLERANCE
    level_retest = float(latest["high"]) >= micro_low * (1.0 - ONE_MINUTE_LEVEL_RETEST_TOLERANCE) and price < micro_low and bearish
    compression_window = df.iloc[-4:-1]
    compression = False
    if len(compression_window) == 3:
        rng = (float(compression_window["high"].max()) - float(compression_window["low"].min())) / max(float(latest["close"]), 0.01)
        compression = rng <= ONE_MINUTE_COMPRESSION_PCT
    momentum_break = compression and price < float(compression_window["low"].min()) and bearish and vol_ratio >= ONE_MINUTE_MIN_VOLUME_RATIO

    trigger_move_pct = abs(price - float(prev["close"])) / max(abs(float(prev["close"])), 0.01) if float(prev["close"]) else 0.0
    if five_aligned and ema9_slope < 0 and pullback_touch and reclaim and (near_vwap or price < five_vwap) and vol_ratio >= ONE_MINUTE_MIN_VOLUME_RATIO and trigger_move_pct >= ONE_MINUTE_MIN_TRIGGER_MOVE_PCT:
        return "FIRST_PULLBACK", f"1m EMA9 pullback/reclaim confirmed; volx={vol_ratio:.2f}"
    if five_aligned and level_retest and vol_ratio >= ONE_MINUTE_MIN_VOLUME_RATIO:
        return "BREAKOUT_RETEST", f"1m breakdown retest confirmed; volx={vol_ratio:.2f}"
    if five_aligned and momentum_break:
        return "MOMENTUM_COMPRESSION", f"1m compression expansion confirmed; volx={vol_ratio:.2f}"

    return None, (
        f"waiting 1m PUT trigger; price={price:.2f}, EMA9={ema9:.2f}, VWAP1m={one_vwap:.2f}, "
        f"volx={vol_ratio:.2f}, ema9_slope={ema9_slope:+.4f}"
    )


def prefetch_bars_parallel(client, symbols):
    """Fetch bars for multiple symbols concurrently to reduce scan latency."""
    ordered_symbols = list(dict.fromkeys(symbols or []))
    if not ordered_symbols:
        return {}
    started_at = time.perf_counter()
    if (not SCAN_PREFETCH_PARALLEL_ENABLED) or len(ordered_symbols) == 1:
        bars_by_symbol = {sym: fetch_bars(client, sym) for sym in ordered_symbols}
        if ENABLE_SCAN_TIMING_LOGS:
            elapsed_ms = (time.perf_counter() - started_at) * 1000.0
            log(f"[SCAN] Prefetched {len(ordered_symbols)} symbol(s) serially in {elapsed_ms:.1f}ms.")
        return bars_by_symbol

    bars_by_symbol = {}
    max_workers = min(SCAN_PREFETCH_MAX_WORKERS, len(ordered_symbols))
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="bars") as executor:
        future_map = {executor.submit(fetch_bars, client, sym): sym for sym in ordered_symbols}
        for future in as_completed(future_map):
            sym = future_map[future]
            try:
                bars_by_symbol[sym] = future.result()
            except Exception as e:
                log(f"[{sym}] Parallel bars fetch failed: {type(e).__name__}: {e}")
                bars_by_symbol[sym] = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    if ENABLE_SCAN_TIMING_LOGS:
        elapsed_ms = (time.perf_counter() - started_at) * 1000.0
        log(
            f"[SCAN] Prefetched {len(ordered_symbols)} symbol(s) in {elapsed_ms:.1f}ms "
            f"with {max_workers} worker(s)."
        )
    return bars_by_symbol


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


def _alpaca_trading_get_json(path, params=None, timeout=10):
    """GET JSON from Alpaca trading API (paper/live based on configured base URL)."""
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        return None
    url = f"{ALPACA_TRADING_BASE_URL.rstrip('/')}{path}"
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


def _safe_int_num(v, default=0):
    try:
        return int(float(v))
    except Exception:
        return default


def _classify_liquidity_data_state(oi_date_raw, day_volume):
    """Distinguish stale/missing OI+volume metadata from genuinely bad liquidity.

    Returns (state, oi_age_days). A stale/missing classification lets a tight live
    bid/ask stand in as evidence of real tradability instead of an absolute veto.
    """
    oi_age_days = None
    try:
        if oi_date_raw:
            oi_age_days = (date.today() - date.fromisoformat(str(oi_date_raw)[:10])).days
    except Exception:
        oi_age_days = None

    if not oi_date_raw and day_volume <= 0:
        return "LIQUIDITY_DATA_MISSING", oi_age_days
    if oi_age_days is not None and oi_age_days >= 1 and day_volume <= 0:
        return "LIQUIDITY_DATA_STALE", oi_age_days
    return "LIQUIDITY_ACTUALLY_BAD", oi_age_days


def entry_contract_quality_ok(symbol, side, data, option, max_ext_from_vwap):
    """Final must-pass checklist after an option contract has been selected."""
    if not option:
        return False, "missing option contract"

    if (
        not TWO_PLAYBOOK_ENTRY_MODE
        and STRICT_IGNITION_NO_BYPASS
        and IGNITION_REQUIRED
        and not bool((data or {}).get("ignition_confirmed", False))
    ):
        return False, "ignition not freshly confirmed"

    entry_timing = _classify_entry_timing(data or {}, side)
    delta_5m, _ = _score_trend_deltas(data or {}, side)
    ignition_delta = _safe_int_num((data or {}).get("ignition_delta", 0), 0)
    effective_score = _safe_int_num((data or {}).get("effective_score", (data or {}).get("score", 0)), 0)
    trend_age_bars = _safe_int_num(
        (data or {}).get("bull_trend_age_bars", 0) if side == "CALL" else (data or {}).get("bear_trend_age_bars", 0),
        0,
    )
    fresh_break = bool((data or {}).get("fresh_breakout", False)) if side == "CALL" else bool((data or {}).get("fresh_breakdown", False))

    if not TWO_PLAYBOOK_ENTRY_MODE and ENTRY_ALLOWED_SETUPS and entry_timing not in ENTRY_ALLOWED_SETUPS:
        if not (RECALL_FIRST_MODE and effective_score >= 82 and delta_5m is not None and delta_5m >= 0):
            return False, f"setup {entry_timing} not in allowed setups"

    if not TWO_PLAYBOOK_ENTRY_MODE and ignition_delta < ENTRY_MIN_IGNITION_DELTA:
        return False, f"ignition delta {ignition_delta} < {ENTRY_MIN_IGNITION_DELTA}"

    if not TWO_PLAYBOOK_ENTRY_MODE and EARLY_IGNITION_PHASE_FILTER:
        if entry_timing == "LATE_CHASE":
            return False, "late-chase timing"
        if delta_5m is not None and delta_5m < EARLY_IGNITION_MIN_DELTA_5M:
            return False, f"5m score delta {delta_5m:+d} < +{EARLY_IGNITION_MIN_DELTA_5M}"
        if entry_timing in {"EARLY_BREAKOUT", "BREAKDOWN"} and not fresh_break:
            return False, "stale break: move already broke earlier bars"
        if entry_timing == "MOMENTUM_CONTINUATION" and trend_age_bars > ENTRY_MAX_TREND_AGE_BARS:
            return False, f"continuation too mature ({trend_age_bars} trend bars)"

    if (
        not TWO_PLAYBOOK_ENTRY_MODE
        and RECOVERY_CALL_STRICT_ENABLED
        and side == "CALL"
        and entry_timing in {"FIRST_PULLBACK", "VWAP_RECLAIM", "HIGHER_LOW_RECLAIM"}
    ):
        m = _side_metric_bundle(data or {}, side)
        rsi_now = _safe_float_num((data or {}).get("rsi", 50.0), 50.0)
        vwap_now = _safe_float_num((data or {}).get("vwap", 0.0), 0.0)
        price_now = _safe_float_num((data or {}).get("price", 0.0), 0.0)
        vwap_dist = ((price_now - vwap_now) / vwap_now) if vwap_now > 0 else 0.0

        if m["momentum"] < RECOVERY_CALL_MIN_MOMENTUM_SCORE:
            return False, (
                f"recovery-call momentum {m['momentum']:.1f} < {RECOVERY_CALL_MIN_MOMENTUM_SCORE:.1f}"
            )
        if m["momentum_quality"] < RECOVERY_CALL_MIN_MOMENTUM_QUALITY:
            return False, (
                f"recovery-call momentum quality {m['momentum_quality']:.1f} < {RECOVERY_CALL_MIN_MOMENTUM_QUALITY:.1f}"
            )
        if m["delta_5m"] is None or m["delta_5m"] < RECOVERY_CALL_MIN_DELTA_5M:
            return False, (
                f"recovery-call 5m score delta {m['delta_5m']} < +{RECOVERY_CALL_MIN_DELTA_5M}"
            )
        if rsi_now < RECOVERY_CALL_MIN_RSI:
            return False, f"recovery-call RSI {rsi_now:.1f} < {RECOVERY_CALL_MIN_RSI:.1f}"
        if vwap_dist < RECOVERY_CALL_MIN_VWAP_DISTANCE:
            return False, (
                f"recovery-call too far below VWAP ({vwap_dist*100:.2f}% < {RECOVERY_CALL_MIN_VWAP_DISTANCE*100:.2f}%)"
            )

    dte = _safe_int_num((option or {}).get("dte", 0), 0)
    if MAX_DTE > 0 and dte > MAX_DTE:
        return False, f"DTE {dte} > max {MAX_DTE}"

    oi = _safe_int_num((option or {}).get("open_interest", 0), 0)
    min_oi = ETF_ENTRY_MIN_OPTION_OI if symbol in ETF_SYMBOLS else STOCK_ENTRY_MIN_OPTION_OI
    if (
        symbol in ETF_SYMBOLS
        and RECALL_FIRST_MODE
        and bool((data or {}).get("watchlist_promoted", False))
        and bool((data or {}).get("promoted_quality_ok", False))
        and bool((data or {}).get("ignition_confirmed", False))
        and _safe_int_num((data or {}).get("ignition_delta", 0), 0) >= max(10, ENTRY_MIN_IGNITION_DELTA - 2)
    ):
        min_oi = min(min_oi, int(PROMOTED_ETF_ENTRY_MIN_OPTION_OI))
    elif (
        symbol not in ETF_SYMBOLS
        and RECALL_FIRST_MODE
        and bool((data or {}).get("watchlist_promoted", False))
        and bool((data or {}).get("promoted_quality_ok", False))
        and bool((data or {}).get("ignition_confirmed", False))
        and _safe_int_num((data or {}).get("ignition_delta", 0), 0) >= max(10, ENTRY_MIN_IGNITION_DELTA - 2)
        and _safe_int_num((data or {}).get("effective_score", 0), 0) >= max(55, SCORE_WATCH)
        and entry_timing in {"FIRST_PULLBACK", "VWAP_RECLAIM", "HIGHER_LOW_RECLAIM", "BREAKDOWN_RETEST", "LOWER_HIGH_FAILURE", "VWAP_REJECT", "MOMENTUM_CONTINUATION"}
        and delta_5m is not None and delta_5m >= max(2, EARLY_IGNITION_MIN_DELTA_5M)
        and _safe_float_num((data or {}).get("momentum_quality", 0.0), 0.0) >= max(7.0, ENTRY_CONT_MIN_MOMENTUM_QUALITY - 1.0)
    ):
        min_oi = min(min_oi, int(PROMOTED_STOCK_ENTRY_MIN_OPTION_OI))
    if oi < min_oi:
        day_volume = _safe_int_num((option or {}).get("volume", 0), 0)
        if not (ENTRY_VOLUME_SUBSTITUTES_OI and day_volume >= ENTRY_MIN_OPTION_DAY_VOLUME):
            oi_date_raw = str((option or {}).get("open_interest_date", "") or "")
            liquidity_state, oi_age_days = _classify_liquidity_data_state(oi_date_raw, day_volume)
            bid_now = _safe_float_num((option or {}).get("bid", 0.0), 0.0)
            ask_now = _safe_float_num((option or {}).get("ask", 0.0), 0.0)
            mid_now = (bid_now + ask_now) / 2.0 if (bid_now + ask_now) > 0 else 0.0
            live_spread_pct = ((ask_now - bid_now) / mid_now) if mid_now > 0 else float("inf")
            tight_live_market = bid_now > 0 and ask_now > 0 and live_spread_pct <= LIQUIDITY_SUBSTITUTE_MAX_SPREAD_PCT

            # A tight spread on a stale quote is meaningless — the substitute only counts
            # if the quote itself was pulled recently. Missing timestamp fails closed.
            quote_ts = (option or {}).get("quote_timestamp")
            quote_age_seconds = None
            if quote_ts is not None:
                try:
                    now_utc = datetime.now(timezone.utc)
                    ts = quote_ts if quote_ts.tzinfo else quote_ts.replace(tzinfo=timezone.utc)
                    quote_age_seconds = (now_utc - ts).total_seconds()
                except Exception:
                    quote_age_seconds = None
            quote_is_fresh = quote_age_seconds is not None and quote_age_seconds <= LIQUIDITY_SUBSTITUTE_MAX_QUOTE_AGE_SECONDS

            if liquidity_state in ("LIQUIDITY_DATA_STALE", "LIQUIDITY_DATA_MISSING") and tight_live_market and quote_is_fresh:
                log(
                    f"[{symbol}] LIQUIDITY SUBSTITUTE: metadata={liquidity_state} oi={oi} "
                    f"oi_date={oi_date_raw or 'unknown'} volume={day_volume} bid={bid_now:.2f} "
                    f"ask={ask_now:.2f} spread={live_spread_pct*100:.2f}% quote_age={quote_age_seconds:.1f}s"
                )
                if isinstance(option, dict):
                    option["_liquidity_substitute_used"] = True
                    option["_liquidity_substitute_mid"] = mid_now
            else:
                fail_reason = "quote too stale" if (tight_live_market and not quote_is_fresh) else liquidity_state
                return False, (
                    f"open interest {oi} (as of {oi_date_raw or 'unknown'}) < {min_oi} "
                    f"and day volume {day_volume} < {ENTRY_MIN_OPTION_DAY_VOLUME} "
                    f"[{fail_reason}, quote_age={quote_age_seconds if quote_age_seconds is not None else 'unknown'}]"
                )

    bid = _safe_float_num((option or {}).get("bid", 0.0), 0.0)
    ask = _safe_float_num((option or {}).get("ask", 0.0), 0.0)
    mid = (bid + ask) / 2.0
    spread_pct = ((ask - bid) / mid) if mid > 0 else float("inf")
    max_spread_pct = ETF_MAX_OPTION_SPREAD_PCT if symbol in ETF_SYMBOLS else STOCK_MAX_OPTION_SPREAD_PCT
    if spread_pct > max_spread_pct:
        return False, (
            f"option spread {spread_pct * 100:.1f}% > {max_spread_pct * 100:.1f}% max"
        )
    opt_delta = abs(_safe_float_num((option or {}).get("delta", 0.0), 0.0))
    max_bid = ETF_MAX_OPTION_BID if symbol in ETF_SYMBOLS else STOCK_MAX_OPTION_BID
    if bid > max_bid:
        exceptional_setup = (
            effective_score >= ENTRY_PREMIUM_EXCEPTION_SCORE
            and ignition_delta >= ENTRY_PREMIUM_EXCEPTION_MIN_IGNITION_DELTA
            and entry_timing == "EARLY_BREAKOUT"
        )
        if not exceptional_setup:
            return False, f"bid ${bid:.2f} > ${max_bid:.2f} max without exceptional early ignition"

    if dte == 1 and opt_delta < ENTRY_1DTE_MIN_DELTA:
        return False, f"1DTE delta {opt_delta:.2f} < {ENTRY_1DTE_MIN_DELTA:.2f}"

    if not TWO_PLAYBOOK_ENTRY_MODE and ENTRY_EXPENSIVE_EXTENSION_FILTER:
        ext_pct = _safe_float_num((data or {}).get("vwap_extension_pct", 0.0), 0.0)
        ext_limit = max(0.0, float(max_ext_from_vwap) * max(0.0, ENTRY_EXPENSIVE_EXTENSION_VWAP_RATIO))
        if bid >= ENTRY_EXPENSIVE_EXTENSION_BID_FLOOR and ext_pct >= ext_limit:
            return False, (
                f"expensive late entry: bid ${bid:.2f} with VWAP extension {ext_pct*100:.2f}% "
                f">= {ext_limit*100:.2f}%"
            )

    return True, "entry checklist passed"


def _ml_sigmoid(x):
    """Numerically stable sigmoid for logistic-style probability outputs."""
    z = max(-50.0, min(50.0, _safe_float_num(x, 0.0)))
    return 1.0 / (1.0 + exp(-z))


def _extract_ml_entry_features(symbol, side, data):
    """Build model features from the existing rule-engine state."""
    side_u = str(side or "").upper()
    call_side = side_u == "CALL"
    m = _side_metric_bundle(data or {}, side_u)
    delta_5m, delta_10m = _score_trend_deltas(data or {}, side_u)

    side_score = _safe_float_num((data or {}).get("bull_score", 0.0), 0.0) if call_side else _safe_float_num((data or {}).get("bear_score", 0.0), 0.0)
    opposite_score = _safe_float_num((data or {}).get("bear_score", 0.0), 0.0) if call_side else _safe_float_num((data or {}).get("bull_score", 0.0), 0.0)
    dominance = side_score - opposite_score

    feats = {
        "is_call": 1.0 if call_side else 0.0,
        "is_put": 0.0 if call_side else 1.0,
        "is_etf": 1.0 if symbol in ETF_SYMBOLS else 0.0,
        "is_top_stock": 1.0 if symbol in TOP_STOCK_SYMBOLS else 0.0,
        "is_aggressive_stock": 1.0 if symbol in AGGRESSIVE_STOCK_SYMBOLS else 0.0,
        "effective_score": _safe_float_num((data or {}).get("effective_score", side_score), side_score),
        "raw_score": _safe_float_num((data or {}).get("raw_entry_score", side_score), side_score),
        "macro_penalty": _safe_float_num((data or {}).get("macro_penalty", 0.0), 0.0),
        "side_score": side_score,
        "opposite_score": opposite_score,
        "dominance": dominance,
        "delta_5m": _safe_float_num(delta_5m, 0.0),
        "delta_10m": _safe_float_num(delta_10m, 0.0),
        "momentum": _safe_float_num(m.get("momentum", 0.0), 0.0),
        "volume": _safe_float_num(m.get("volume", 0.0), 0.0),
        "regime": _safe_float_num(m.get("regime", 0.0), 0.0),
        "pattern": _safe_float_num(m.get("pattern", 0.0), 0.0),
        "momentum_quality": _safe_float_num(m.get("momentum_quality", 0.0), 0.0),
        "ema20_slope_pct": _safe_float_num((data or {}).get("ema20_slope_pct", 0.0), 0.0),
        "rsi": _safe_float_num((data or {}).get("rsi", 50.0), 50.0),
        "vol_ratio": _safe_float_num((data or {}).get("vol_ratio", 1.0), 1.0),
        "vwap_extension_pct": _safe_float_num((data or {}).get("vwap_extension_pct", 0.0), 0.0),
        "watchlist_promoted": 1.0 if bool((data or {}).get("watchlist_promoted", False)) else 0.0,
    }
    return feats


def _load_ml_gate_model():
    """Load and cache a lightweight JSON logistic model for entry gating."""
    path = str(ML_GATE_MODEL_PATH or "").strip()
    if not path:
        return None

    try:
        mtime = os.path.getmtime(path)
    except Exception:
        mtime = None

    cached = _ml_gate_cache
    if cached.get("path") == path and cached.get("mtime") == mtime:
        return cached.get("model")

    model = None
    if mtime is not None:
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            if isinstance(payload, dict):
                feature_order = payload.get("feature_order") or []
                weights = payload.get("weights") or []
                bias = _safe_float_num(payload.get("bias", 0.0), 0.0)
                threshold = _safe_float_num(payload.get("threshold", ML_MIN_PROBABILITY), ML_MIN_PROBABILITY)
                feature_stats = payload.get("feature_stats") if isinstance(payload.get("feature_stats"), dict) else {}
                expected_return = payload.get("expected_return") if isinstance(payload.get("expected_return"), dict) else None

                if isinstance(feature_order, list) and isinstance(weights, list) and len(feature_order) == len(weights) and len(feature_order) > 0:
                    model = {
                        "feature_order": [str(x) for x in feature_order],
                        "weights": [_safe_float_num(x, 0.0) for x in weights],
                        "bias": bias,
                        "threshold": threshold,
                        "feature_stats": feature_stats,
                        "expected_return": expected_return,
                    }
        except Exception:
            model = None

    _ml_gate_cache["path"] = path
    _ml_gate_cache["mtime"] = mtime
    _ml_gate_cache["model"] = model
    _ml_gate_cache["checked"] = datetime.now(central)
    return model


def _predict_ml_entry(symbol, side, data):
    """Return (probability, expected_return, source_label) for the current setup."""
    model = _load_ml_gate_model()
    if not model:
        return None, None, "none"

    feats = _extract_ml_entry_features(symbol, side, data)
    order = list(model.get("feature_order") or [])
    weights = list(model.get("weights") or [])
    stats = model.get("feature_stats") or {}

    linear = _safe_float_num(model.get("bias", 0.0), 0.0)
    for idx, name in enumerate(order):
        val = _safe_float_num(feats.get(name, 0.0), 0.0)
        st = stats.get(name) if isinstance(stats, dict) else None
        if isinstance(st, dict):
            mean = _safe_float_num(st.get("mean", 0.0), 0.0)
            std = abs(_safe_float_num(st.get("std", 1.0), 1.0))
            if std > 1e-9:
                val = (val - mean) / std
        linear += _safe_float_num(weights[idx], 0.0) * val

    probability = _ml_sigmoid(linear)

    expected_return = None
    er = model.get("expected_return")
    if isinstance(er, dict):
        er_order = er.get("feature_order") if isinstance(er.get("feature_order"), list) else order
        er_weights = er.get("weights") if isinstance(er.get("weights"), list) else []
        if len(er_order) == len(er_weights) and len(er_order) > 0:
            er_linear = _safe_float_num(er.get("bias", 0.0), 0.0)
            for idx, name in enumerate(er_order):
                er_linear += _safe_float_num(er_weights[idx], 0.0) * _safe_float_num(feats.get(name, 0.0), 0.0)
            expected_return = er_linear

    return probability, expected_return, "json_model"


def _run_auto_retrain_job(trigger_reason=""):
    """Run model training in background without blocking scan/execution loops."""
    cmd = [
        sys.executable,
        "train_entry_model.py",
        "--source", str(AUTO_RETRAIN_SOURCE or "sheets"),
        "--input", str(AUTO_RETRAIN_INPUT_PATH or "trade_results.csv"),
        "--sheet-tab", str(AUTO_RETRAIN_SHEET_TAB or "Trades"),
        "--output", str(AUTO_RETRAIN_OUTPUT_PATH or "models/entry_model.json"),
        "--min-samples", str(max(5, int(AUTO_RETRAIN_MIN_SAMPLES))),
    ]

    started = datetime.now(central)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=os.getcwd(),
            timeout=300,
        )
        ok = proc.returncode == 0
        stdout_tail = (proc.stdout or "").strip().splitlines()[-1:] if proc.stdout else []
        stderr_tail = (proc.stderr or "").strip().splitlines()[-1:] if proc.stderr else []
        summary = " ".join(stdout_tail + stderr_tail).strip()
        if ok:
            log(
                f"[AUTO-RETRAIN] Model refresh succeeded"
                f" ({trigger_reason or 'periodic'}) in "
                f"{int((datetime.now(central) - started).total_seconds())}s."
                f" {summary}"
            )
        else:
            log(
                f"[AUTO-RETRAIN] Model refresh failed (code={proc.returncode})"
                f" ({trigger_reason or 'periodic'}). {summary}"
            )
    except Exception as e:
        ok = False
        summary = str(e)
        log(f"[AUTO-RETRAIN] Model refresh exception: {e}")

    with _auto_retrain_lock:
        _auto_retrain_state["running"] = False
        _auto_retrain_state["last_run_at"] = datetime.now(central)
        _auto_retrain_state["last_status"] = "ok" if ok else f"error: {summary}"
        if ok:
            _auto_retrain_state["last_closed_count"] = int(_auto_retrain_state.get("closed_count", 0))


def maybe_trigger_auto_retrain(trigger_reason=""):
    """Schedule non-blocking model retraining when enough new closed trades accumulate."""
    if not AUTO_RETRAIN_MODEL_ENABLED:
        return

    now_ct = datetime.now(central)
    with _auto_retrain_lock:
        if _auto_retrain_state.get("running", False):
            return

        last_run_at = _auto_retrain_state.get("last_run_at")
        if isinstance(last_run_at, datetime):
            elapsed = (now_ct - last_run_at).total_seconds()
            if elapsed < max(60, AUTO_RETRAIN_COOLDOWN_MINUTES * 60):
                return

        closed_count = int(_auto_retrain_state.get("closed_count", 0))
        last_closed_count = int(_auto_retrain_state.get("last_closed_count", 0))
        need = max(1, int(AUTO_RETRAIN_MIN_NEW_CLOSED_TRADES))
        if (closed_count - last_closed_count) < need:
            return

        _auto_retrain_state["running"] = True

    thread = threading.Thread(
        target=_run_auto_retrain_job,
        kwargs={"trigger_reason": trigger_reason},
        daemon=True,
        name="auto-ml-retrain",
    )
    thread.start()


def _parse_datetime_any(value):
    """Parse common date/datetime strings into central timezone, or None."""
    raw = str(value or "").strip()
    if not raw:
        return None

    candidates = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
    ]
    for fmt in candidates:
        try:
            dt = datetime.strptime(raw, fmt)
            return central.localize(dt)
        except Exception:
            pass

    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return central.localize(dt)
        return dt.astimezone(central)
    except Exception:
        return None


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


def _fetch_stocktwits_trending_symbols():
    """Fetch trending symbols from Stocktwits public endpoint."""
    if not ENABLE_STOCKTWITS_TRENDING:
        return []

    try:
        resp = requests.get(
            STOCKTWITS_TRENDING_URL,
            timeout=max(2, STOCKTWITS_TIMEOUT_SECONDS),
            headers={"Accept": "application/json", "User-Agent": "stock-ai-agent/1.0"},
        )
        if resp.status_code != 200:
            log(f"[TRENDING] Stocktwits fetch failed: HTTP {resp.status_code} — {resp.text[:150]}")
            return []
        payload = resp.json() or {}
        raw_items = payload.get("symbols") if isinstance(payload, dict) else []
        out = []
        if isinstance(raw_items, list):
            for item in raw_items:
                if isinstance(item, dict):
                    sym = str(item.get("symbol") or "").upper().strip()
                else:
                    sym = str(item or "").upper().strip()
                if sym:
                    out.append(sym)
        if not out:
            log(f"[TRENDING] Stocktwits returned HTTP 200 but 0 usable symbols (payload keys: {list(payload.keys()) if isinstance(payload, dict) else type(payload)}).")
        else:
            log(f"[TRENDING] Stocktwits returned {len(out)} symbol(s): {', '.join(out[:10])}")
        return out
    except Exception as e:
        log(f"[TRENDING] Stocktwits fetch exception: {type(e).__name__}: {e}")
        return []


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
        if payload is None:
            log(f"[TRENDING] Alpaca screener {path} returned no payload (request failed or non-200).")
        elif not items:
            log(f"[TRENDING] Alpaca screener {path} returned 0 usable items (payload keys: {list(payload.keys()) if isinstance(payload, dict) else type(payload)}).")
        else:
            log(f"[TRENDING] Alpaca screener {path} returned {len(items)} raw item(s).")
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

    # Blend in social momentum from Stocktwits trending feed.
    stocktwits_syms = _fetch_stocktwits_trending_symbols()
    for idx, sym in enumerate(stocktwits_syms):
        if not sym or sym in ETF_SYMBOLS or sym in base_set:
            continue
        if not _is_valid_trending_symbol(sym):
            continue
        if not _is_stock_like_trending_candidate(sym):
            continue

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

        rank_bonus = max(0.0, 26.0 - (idx * 1.5))
        score = 30.0 + rank_bonus
        reason = "stocktwits trending"

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
        log(f"Trending stocks from Alpaca/Stocktwits: {', '.join(selected)}")
        for sym in selected:
            why = reasons.get(sym, "")
            if why:
                log(f"[TRENDING] {sym} — {why}")

    return selected, reasons


def log(msg):
    print(f"[{datetime.now(central):%Y-%m-%d %H:%M:%S} CT] {msg}", flush=True)


def _reset_perf_stats_if_new_day(now_ct=None):
    """Reset in-memory daily performance counters on date rollover."""
    now_ct = now_ct or datetime.now(central)
    today = now_ct.date()
    if _perf_stats.get("date") == today:
        return

    _perf_stats["date"] = today
    _perf_stats["entries"] = 0
    _perf_stats["closed"] = 0
    _perf_stats["wins"] = 0
    _perf_stats["losses"] = 0
    _perf_stats["realized_pnl_dollar"] = 0.0
    _perf_stats["realized_pnl_pct_sum"] = 0.0
    _perf_stats["gross_win_dollar"] = 0.0
    _perf_stats["gross_loss_dollar"] = 0.0
    _perf_stats["win_pnl_pct_sum"] = 0.0
    _perf_stats["loss_pnl_pct_sum"] = 0.0
    _perf_stats["best_win_dollar"] = 0.0
    _perf_stats["best_win_symbol"] = ""
    _perf_stats["worst_loss_dollar"] = 0.0
    _perf_stats["worst_loss_symbol"] = ""
    _perf_stats["last_report_entries"] = 0
    _perf_stats["last_report_closed"] = 0
    _perf_stats["last_report_wins"] = 0
    _perf_stats["last_report_losses"] = 0
    _perf_stats["last_report_realized_pnl_dollar"] = 0.0
    _perf_stats["last_sent_hour"] = None
    _perf_stats["hydrated_for"] = None
    _perf_stats["hydrated_source"] = ""
    _perf_stats["last_hydrate_attempt"] = None


def _blank_perf_snapshot(today):
    return {
        "date": today,
        "entries": 0,
        "closed": 0,
        "wins": 0,
        "losses": 0,
        "realized_pnl_dollar": 0.0,
        "realized_pnl_pct_sum": 0.0,
        "gross_win_dollar": 0.0,
        "gross_loss_dollar": 0.0,
        "win_pnl_pct_sum": 0.0,
        "loss_pnl_pct_sum": 0.0,
        "best_win_dollar": 0.0,
        "best_win_symbol": "",
        "worst_loss_dollar": 0.0,
        "worst_loss_symbol": "",
    }


def _accumulate_closed_trade_stats(snapshot, symbol, pnl_dollar, pnl_pct):
    """Update aggregate counters with one closed trade outcome."""
    snapshot["closed"] = int(snapshot.get("closed", 0)) + 1
    snapshot["realized_pnl_dollar"] = float(snapshot.get("realized_pnl_dollar", 0.0)) + float(pnl_dollar)
    snapshot["realized_pnl_pct_sum"] = float(snapshot.get("realized_pnl_pct_sum", 0.0)) + float(pnl_pct)

    if pnl_dollar > 0:
        snapshot["wins"] = int(snapshot.get("wins", 0)) + 1
        snapshot["gross_win_dollar"] = float(snapshot.get("gross_win_dollar", 0.0)) + float(pnl_dollar)
        snapshot["win_pnl_pct_sum"] = float(snapshot.get("win_pnl_pct_sum", 0.0)) + float(pnl_pct)
        if float(pnl_dollar) > float(snapshot.get("best_win_dollar", 0.0)):
            snapshot["best_win_dollar"] = float(pnl_dollar)
            snapshot["best_win_symbol"] = str(symbol or "")
    else:
        snapshot["losses"] = int(snapshot.get("losses", 0)) + 1
        snapshot["gross_loss_dollar"] = float(snapshot.get("gross_loss_dollar", 0.0)) + abs(float(pnl_dollar))
        snapshot["loss_pnl_pct_sum"] = float(snapshot.get("loss_pnl_pct_sum", 0.0)) + float(pnl_pct)
        if float(pnl_dollar) < float(snapshot.get("worst_loss_dollar", 0.0)):
            snapshot["worst_loss_dollar"] = float(pnl_dollar)
            snapshot["worst_loss_symbol"] = str(symbol or "")


def _rehydrate_perf_from_sheets(today):
    """Rebuild today's counters from Google Sheets Trades tab (if available)."""
    if _gsheet is None and not ensure_google_sheets_ready():
        return None
    if _gsheet is None:
        return None

    try:
        ws = _gsheet.worksheet("Trades")
        rows = ws.get_all_values()
    except Exception as e:
        log(f"Perf rehydrate (Sheets) failed: {e}")
        return None

    if not rows or len(rows) <= 1:
        return None

    snap = _blank_perf_snapshot(today)
    for row in rows[1:]:
        if len(row) < 14:
            continue

        symbol = str(row[1] if len(row) > 1 else "").strip().upper()
        entry_dt = _parse_datetime_any(row[6] if len(row) > 6 else "")
        exit_dt = _parse_datetime_any(row[7] if len(row) > 7 else "")
        status = str(row[9] if len(row) > 9 else "").strip().upper()

        if entry_dt and entry_dt.date() == today and status != "PARTIAL":
            snap["entries"] = int(snap.get("entries", 0)) + 1

        if status != "CLOSED" or not exit_dt or exit_dt.date() != today:
            continue

        pnl_dollar = _safe_float_num(str(row[12]).replace("$", "").replace(",", ""), 0.0)
        pnl_pct = _safe_float_num(str(row[13]).replace("%", "").replace(",", ""), 0.0)
        _accumulate_closed_trade_stats(snap, symbol, pnl_dollar, pnl_pct)

    return snap


def _rehydrate_perf_from_alpaca_orders(today):
    """Rebuild today's counters from filled Alpaca option orders using FIFO matching."""
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        return None

    snap = _blank_perf_snapshot(today)
    day_start_ct = central.localize(datetime(today.year, today.month, today.day, 0, 0, 0))
    day_end_ct = day_start_ct + timedelta(days=1)
    after_iso = day_start_ct.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    until_iso = day_end_ct.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    params = {
        "status": "all",
        "direction": "asc",
        "limit": 500,
        "nested": "false",
        "after": after_iso,
        "until": until_iso,
    }
    orders = _alpaca_trading_get_json("/v2/orders", params=params, timeout=10)
    if not orders:
        return None
    if not isinstance(orders, list):
        return None

    lots = {}
    realized_by_contract = {}
    for o in orders:
        if str(o.get("status", "")).lower() != "filled":
            continue

        contract = str(o.get("symbol", "") or "").strip().upper()
        if not re.match(r"^[A-Z]+\d{6}[CP]\d{8}$", contract):
            continue

        side = str(o.get("side", "") or "").strip().upper()
        qty = _safe_float_num(o.get("filled_qty", o.get("qty", 0)), 0.0)
        price = _safe_float_num(o.get("filled_avg_price", 0), 0.0)
        if qty <= 0 or price <= 0:
            continue

        filled_dt = _parse_datetime_any(o.get("filled_at") or o.get("updated_at") or o.get("submitted_at"))
        if not filled_dt or filled_dt.date() != today:
            continue

        underlying = _underlying_from_contract(contract) or contract
        if side == "BUY":
            snap["entries"] = int(snap.get("entries", 0)) + 1
            lots.setdefault(contract, []).append([qty, price])
            realized_by_contract.setdefault(contract, {
                "underlying": underlying,
                "pnl_dollar": 0.0,
                "buy_cost": 0.0,
            })
            continue

        if side != "SELL":
            continue

        remaining = qty
        matched_qty = 0.0
        buy_cost = 0.0
        pnl_dollar = 0.0
        queue = lots.setdefault(contract, [])
        while remaining > 1e-9 and queue:
            lot_qty, lot_price = queue[0]
            use_qty = min(remaining, lot_qty)
            pnl_dollar += (price - lot_price) * use_qty * 100.0
            buy_cost += lot_price * use_qty * 100.0
            matched_qty += use_qty
            lot_qty -= use_qty
            remaining -= use_qty
            if lot_qty <= 1e-9:
                queue.pop(0)
            else:
                queue[0][0] = lot_qty

        if matched_qty <= 0:
            continue

        realized = realized_by_contract.setdefault(contract, {
            "underlying": underlying,
            "pnl_dollar": 0.0,
            "buy_cost": 0.0,
        })
        realized["underlying"] = underlying
        realized["pnl_dollar"] = float(realized.get("pnl_dollar", 0.0)) + pnl_dollar
        realized["buy_cost"] = float(realized.get("buy_cost", 0.0)) + buy_cost
        snap["realized_pnl_dollar"] = float(snap.get("realized_pnl_dollar", 0.0)) + pnl_dollar

        # Count the trade as closed only once the remaining FIFO lots are gone.
        if queue:
            continue

        total_pnl_dollar = float(realized.get("pnl_dollar", 0.0))
        total_buy_cost = float(realized.get("buy_cost", 0.0))
        pnl_pct = ((total_pnl_dollar / total_buy_cost) * 100.0) if total_buy_cost > 0 else 0.0
        _accumulate_closed_trade_stats(snap, realized.get("underlying", underlying), total_pnl_dollar, pnl_pct)
        realized_by_contract.pop(contract, None)

    return snap


def _rehydrate_perf_stats_if_needed(now_ct=None):
    """Hydrate daily perf counters after restart using Sheets/Alpaca history."""
    now_ct = now_ct or datetime.now(central)
    today = now_ct.date()
    if _perf_stats.get("hydrated_for") == today:
        return

    _perf_stats["last_hydrate_attempt"] = now_ct

    candidates = []
    sheet_snap = _rehydrate_perf_from_sheets(today)
    if sheet_snap:
        candidates.append(("google_sheets", sheet_snap))

    alpaca_snap = _rehydrate_perf_from_alpaca_orders(today)
    if alpaca_snap:
        candidates.append(("alpaca_orders", alpaca_snap))

    if candidates:
        source, chosen = max(
            candidates,
            key=lambda item: (
                int(item[1].get("closed", 0)),
                int(item[1].get("entries", 0)),
                abs(float(item[1].get("realized_pnl_dollar", 0.0))),
            ),
        )
        for k, v in chosen.items():
            _perf_stats[k] = v
        _perf_stats["hydrated_source"] = source
        log(
            f"Hourly perf rehydrated from {source}: entries={_perf_stats['entries']} "
            f"closed={_perf_stats['closed']} wins={_perf_stats['wins']} losses={_perf_stats['losses']} "
            f"realized=${float(_perf_stats['realized_pnl_dollar']):+,.2f}"
        )
    else:
        _perf_stats["hydrated_source"] = "none"
        log("Hourly perf rehydrate: no historical trades found for today (starting from zero).")

    _perf_stats["hydrated_for"] = today


def _record_trade_open_for_perf(now_ct=None):
    """Record one opened trade in today's in-memory performance counters."""
    _reset_perf_stats_if_new_day(now_ct)
    _perf_stats["entries"] = int(_perf_stats.get("entries", 0)) + 1


def _record_trade_close_for_perf(trade, exit_price, pnl_pct, now_ct=None, final_close=True):
    """Record one closed trade in today's in-memory performance counters.

    final_close=False: partial exit — contributes to realized dollar P&L but does
    not increment closed/wins/losses (those are counted once on the final leg).
    pnl_pct for the final close should be the combined net pnl across all legs.
    """
    _reset_perf_stats_if_new_day(now_ct)

    qty = int(trade.get("qty", 0) or 0)
    entry_px = float(trade.get("entry", 0.0) or 0.0)
    exit_px = float(exit_price or 0.0)
    pnl_dollar = (exit_px - entry_px) * float(max(0, qty)) * 100.0

    # Always accumulate realized dollar P&L (partial exits generate real cash).
    _perf_stats["realized_pnl_dollar"] = float(_perf_stats.get("realized_pnl_dollar", 0.0)) + pnl_dollar

    if not final_close:
        # Partial exit: contribute to realized dollar P&L but don't count as a
        # completed trade; closed/wins/losses are tallied once on the final leg.
        return

    # Final close: count the trade and use combined pnl_pct for win/loss outcome.
    _perf_stats["realized_pnl_pct_sum"] = float(_perf_stats.get("realized_pnl_pct_sum", 0.0)) + float(pnl_pct * 100.0)
    _perf_stats["closed"] = int(_perf_stats.get("closed", 0)) + 1
    if pnl_pct > 0:
        _perf_stats["wins"] = int(_perf_stats.get("wins", 0)) + 1
        _perf_stats["gross_win_dollar"] = float(_perf_stats.get("gross_win_dollar", 0.0)) + pnl_dollar
        _perf_stats["win_pnl_pct_sum"] = float(_perf_stats.get("win_pnl_pct_sum", 0.0)) + float(pnl_pct * 100.0)
        if pnl_dollar > float(_perf_stats.get("best_win_dollar", 0.0)):
            _perf_stats["best_win_dollar"] = pnl_dollar
            _perf_stats["best_win_symbol"] = str(trade.get("underlying", "") or "")
    else:
        _perf_stats["losses"] = int(_perf_stats.get("losses", 0)) + 1
        _perf_stats["gross_loss_dollar"] = float(_perf_stats.get("gross_loss_dollar", 0.0)) + abs(pnl_dollar)
        _perf_stats["loss_pnl_pct_sum"] = float(_perf_stats.get("loss_pnl_pct_sum", 0.0)) + float(pnl_pct * 100.0)
        if pnl_dollar < float(_perf_stats.get("worst_loss_dollar", 0.0)):
            _perf_stats["worst_loss_dollar"] = pnl_dollar
            _perf_stats["worst_loss_symbol"] = str(trade.get("underlying", "") or "")


def maybe_send_hourly_perf_report(now_ct=None):
    """Send one hourly Discord performance summary for today's trades."""
    if not ENABLE_HOURLY_DISCORD_PERF_REPORT:
        return
    if not DISCORD_WEBHOOK_URL:
        return

    now_ct = now_ct or datetime.now(central)
    _reset_perf_stats_if_new_day(now_ct)
    _rehydrate_perf_stats_if_needed(now_ct)

    # Post once per hour near the top of the hour to avoid noisy timing drift.
    minute_window = max(1, min(15, HOURLY_REPORT_MINUTE_WINDOW))
    if now_ct.minute >= minute_window:
        return

    hour_key = (now_ct.date(), now_ct.hour)
    if _perf_stats.get("last_sent_hour") == hour_key:
        return

    entries = int(_perf_stats.get("entries", 0))
    closed = int(_perf_stats.get("closed", 0))
    wins = int(_perf_stats.get("wins", 0))
    losses = int(_perf_stats.get("losses", 0))
    open_positions = len(_open_trades)
    realized_pnl_dollar = float(_perf_stats.get("realized_pnl_dollar", 0.0))
    avg_closed_pnl_pct = (float(_perf_stats.get("realized_pnl_pct_sum", 0.0)) / closed) if closed > 0 else 0.0
    win_rate = (wins / closed * 100.0) if closed > 0 else 0.0
    gross_win_dollar = float(_perf_stats.get("gross_win_dollar", 0.0))
    gross_loss_dollar = float(_perf_stats.get("gross_loss_dollar", 0.0))
    profit_factor = (gross_win_dollar / gross_loss_dollar) if gross_loss_dollar > 0 else (float("inf") if gross_win_dollar > 0 else 0.0)
    avg_win_pct = (float(_perf_stats.get("win_pnl_pct_sum", 0.0)) / wins) if wins > 0 else 0.0
    avg_loss_pct = (float(_perf_stats.get("loss_pnl_pct_sum", 0.0)) / losses) if losses > 0 else 0.0
    best_win_dollar = float(_perf_stats.get("best_win_dollar", 0.0))
    best_win_symbol = str(_perf_stats.get("best_win_symbol", "") or "")
    worst_loss_dollar = float(_perf_stats.get("worst_loss_dollar", 0.0))
    worst_loss_symbol = str(_perf_stats.get("worst_loss_symbol", "") or "")
    last_entries = int(_perf_stats.get("last_report_entries", 0))
    last_closed = int(_perf_stats.get("last_report_closed", 0))
    last_wins = int(_perf_stats.get("last_report_wins", 0))
    last_losses = int(_perf_stats.get("last_report_losses", 0))
    last_realized_pnl = float(_perf_stats.get("last_report_realized_pnl_dollar", 0.0))
    delta_entries = entries - last_entries
    delta_closed = closed - last_closed
    delta_wins = wins - last_wins
    delta_losses = losses - last_losses
    delta_realized_pnl = realized_pnl_dollar - last_realized_pnl
    delta_win_rate = (delta_wins / delta_closed * 100.0) if delta_closed > 0 else 0.0
    pf_text = "N/A"
    if profit_factor == float("inf"):
        pf_text = "INF"
    elif gross_loss_dollar > 0:
        pf_text = f"{profit_factor:.2f}"

    color = DISCORD_COLOR_WARN
    if realized_pnl_dollar > 0:
        color = DISCORD_COLOR_CALL
    elif realized_pnl_dollar < 0:
        color = DISCORD_COLOR_PUT

    send_discord(
        f"📊 **Hourly Trade Summary ({now_ct:%Y-%m-%d %H:%M} CT)**\n\n"
        f"**Today Entries:** `{entries}`\n"
        f"**Closed Trades:** `{closed}`\n"
        f"**Wins / Losses:** `{wins}` / `{losses}`\n"
        f"**Win Rate:** `{win_rate:.1f}%`\n"
        f"**Open Positions:** `{open_positions}`\n"
        f"**Realized P&L ($):** `{realized_pnl_dollar:+,.2f}`\n"
        f"**Avg Closed P&L (%):** `{avg_closed_pnl_pct:+.2f}%`\n"
        f"**Gross Wins / Losses ($):** `{gross_win_dollar:,.2f}` / `{gross_loss_dollar:,.2f}`\n"
        f"**Profit Factor:** `{pf_text}`\n"
        f"**Avg Win / Loss (%):** `{avg_win_pct:+.2f}%` / `{avg_loss_pct:+.2f}%`\n"
        f"**Best / Worst Trade ($):** `{best_win_symbol or '-'} {best_win_dollar:+,.2f}` / `{worst_loss_symbol or '-'} {worst_loss_dollar:+,.2f}`\n"
        f"**Since Last Report:** Entries `{delta_entries:+d}` | Closed `{delta_closed:+d}` | "
        f"W/L `{delta_wins:+d}/{delta_losses:+d}` | WinRate `{delta_win_rate:.1f}%` | "
        f"P&L `{delta_realized_pnl:+,.2f}`",
        color=color,
        webhook_url=DISCORD_WEBHOOK_AI_STATUS_URL or DISCORD_WEBHOOK_LIVE_TRADES_URL,
    )
    _perf_stats["last_report_entries"] = entries
    _perf_stats["last_report_closed"] = closed
    _perf_stats["last_report_wins"] = wins
    _perf_stats["last_report_losses"] = losses
    _perf_stats["last_report_realized_pnl_dollar"] = realized_pnl_dollar
    _perf_stats["last_sent_hour"] = hour_key
    log(
        f"Hourly perf report sent: entries={entries} closed={closed} wins={wins} "
        f"losses={losses} realized=${realized_pnl_dollar:+,.2f}"
    )


def _extract_news_articles(payload):
    """Normalize Alpaca news payloads into a simple list of article dicts."""
    if not isinstance(payload, dict):
        return []
    articles = payload.get("news")
    if not isinstance(articles, list):
        articles = payload.get("articles")
    if not isinstance(articles, list):
        return []
    return [a for a in articles if isinstance(a, dict)]


def _fetch_daily_change_summary(client, symbol):
    """Return latest daily close move versus previous close for one symbol."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=10)

    try:
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame(1, TimeFrameUnit.Day),
            start=start,
            end=end,
            feed=DataFeed(FEED),
        )
        bars = client.get_stock_bars(req).df
        if bars is None or bars.empty:
            return None
        if isinstance(bars.index, pd.MultiIndex):
            bars = bars.xs(symbol, level=0)
        bars = bars[["close"]].dropna().tail(2)
        if len(bars) < 2:
            return None

        prev_close = float(bars["close"].iloc[-2])
        last_close = float(bars["close"].iloc[-1])
        if prev_close <= 0:
            return None
        pct = ((last_close - prev_close) / prev_close) * 100.0
        return {
            "symbol": symbol,
            "last_close": last_close,
            "prev_close": prev_close,
            "pct": pct,
        }
    except Exception:
        return None


def _categorize_market_headlines(articles, now_ct):
    """Split headlines into FED/FOMC, earnings, geopolitics, and broad market buckets."""
    fed_keys = (
        "fomc", "federal reserve", "fed ", "powell", "rate cut", "rate hike",
        "inflation", "cpi", "ppi", "pce", "jobs", "payroll", "treasury yield",
    )
    earnings_keys = (
        "earnings", "guidance", "eps", "revenue", "beat", "miss", "forecast", "outlook",
    )
    geopolitics_keys = (
        "iran", "israel", "ukraine", "russia", "china", "taiwan", "gaza",
        "middle east", "red sea", "strait of hormuz", "south china sea",
        "missile", "drone strike", "airstrike", "ceasefire", "sanctions", "tariff",
        "nato", "pentagon", "opec", "oil supply", "shipping route", "embassy",
    )

    fed, earnings, geopolitics, market = [], [], [], []
    seen = set()

    for art in articles:
        title = str(art.get("headline") or art.get("title") or "").strip()
        if not title:
            continue
        key = title.lower()
        if key in seen:
            continue

        # Prefer today's headlines for the morning briefing; allow recent fallback.
        created = _parse_datetime_any(art.get("created_at") or art.get("updated_at") or "")
        if created is not None:
            if created.astimezone(central).date() != now_ct.date():
                continue

        seen.add(key)
        blob = f"{title} {str(art.get('summary') or '')}".lower()

        if any(k in blob for k in fed_keys):
            fed.append(title)
        elif any(k in blob for k in earnings_keys):
            earnings.append(title)
        elif any(k in blob for k in geopolitics_keys):
            geopolitics.append(title)
        else:
            market.append(title)

    # If no same-day headlines were found, use recent headlines as fallback.
    if not (fed or earnings or geopolitics or market):
        for art in articles:
            title = str(art.get("headline") or art.get("title") or "").strip()
            if not title:
                continue
            key = title.lower()
            if key in seen:
                continue
            seen.add(key)
            blob = f"{title} {str(art.get('summary') or '')}".lower()
            if any(k in blob for k in fed_keys):
                fed.append(title)
            elif any(k in blob for k in earnings_keys):
                earnings.append(title)
            elif any(k in blob for k in geopolitics_keys):
                geopolitics.append(title)
            else:
                market.append(title)

    return fed, earnings, geopolitics, market


def _load_briefing_state():
    """Load persisted briefing sent-state from disk."""
    try:
        if not os.path.exists(BRIEFING_STATE_FILE):
            return {"sent": {}}
        with open(BRIEFING_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"sent": {}}
        sent = data.get("sent")
        if not isinstance(sent, dict):
            data["sent"] = {}
        return data
    except Exception:
        return {"sent": {}}


def _save_briefing_state(state):
    """Persist briefing sent-state to disk."""
    try:
        folder = os.path.dirname(BRIEFING_STATE_FILE)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(BRIEFING_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, sort_keys=True)
    except Exception as e:
        log(f"Warning: failed to save briefing state: {e}")


def _briefing_already_sent(sent_key, date_str):
    state = _load_briefing_state()
    sent = state.get("sent", {}) if isinstance(state, dict) else {}
    if not isinstance(sent, dict):
        return False
    return str(sent.get(sent_key, "")) == str(date_str)


def _mark_briefing_sent(sent_key, date_str, now_ct):
    state = _load_briefing_state()
    sent = state.get("sent")
    if not isinstance(sent, dict):
        sent = {}
    sent[sent_key] = str(date_str)
    state["sent"] = sent
    state["updated_at_ct"] = now_ct.strftime("%Y-%m-%d %H:%M:%S")
    _save_briefing_state(state)


def _maybe_send_scheduled_briefing(client, now_ct, title, target_hour, target_minute, sent_date, sent_key):
    """Send one daily scheduled market briefing after target CT time."""
    if not DISCORD_WEBHOOK_URL:
        return sent_date

    with _briefing_dispatch_lock:
        target_time = now_ct.replace(
            hour=max(0, min(23, int(target_hour))),
            minute=max(0, min(59, int(target_minute))),
            second=0,
            microsecond=0,
        )
        if now_ct < target_time:
            return sent_date

        today_str = now_ct.strftime("%Y-%m-%d")
        if sent_date == now_ct.date():
            return sent_date
        if _briefing_already_sent(sent_key, today_str):
            log(f"{title} skipped: already sent today (persistent state).")
            return now_ct.date()

        index_rows = []
        for sym in ("SPY", "QQQ", "IWM"):
            snap = _fetch_daily_change_summary(client, sym)
            if not snap:
                continue
            index_rows.append(
                f"- `{sym}` close `{snap['last_close']:.2f}` ({snap['pct']:+.2f}% vs prior close)"
            )
        if not index_rows:
            index_rows.append("- `Index snapshot unavailable from Alpaca right now`")

        symbols_csv = ",".join(
            [s.strip().upper() for s in str(MORNING_BRIEFING_NEWS_SYMBOLS).split(",") if s.strip()]
        )
        payload = _alpaca_data_get_json(
            "/v1beta1/news",
            params={"symbols": symbols_csv, "limit": max(10, MORNING_BRIEFING_NEWS_LIMIT), "sort": "desc"},
            timeout=10,
        )
        articles = _extract_news_articles(payload)
        fed_news, earnings_news, geopolitics_news, market_news = _categorize_market_headlines(articles, now_ct)

        n = max(1, MORNING_BRIEFING_HEADLINES_PER_SECTION)
        fed_lines = [f"- {h}" for h in fed_news[:n]] or ["- No major Fed/FOMC macro headline detected yet"]
        earn_lines = [f"- {h}" for h in earnings_news[:n]] or ["- No major earnings headline detected yet"]
        geopolitics_lines = [f"- {h}" for h in geopolitics_news[:n]] or ["- No major geopolitics headline detected yet"]
        market_lines = [f"- {h}" for h in market_news[:n]] or ["- No broad market-moving headline detected yet"]
        index_block = "\n".join(index_rows)
        fed_block = "\n".join(fed_lines)
        earn_block = "\n".join(earn_lines)
        geopolitics_block = "\n".join(geopolitics_lines)
        market_block = "\n".join(market_lines)

        send_discord(
            f"\U0001f305 **{title} ({now_ct:%Y-%m-%d %H:%M} CT)**\n\n"
            f"**Market Snapshot**\n"
            f"{index_block}\n\n"
            f"**Fed / FOMC / Macro Watch**\n"
            f"{fed_block}\n\n"
            f"**Earnings Watch**\n"
            f"{earn_block}\n\n"
            f"**Geopolitics Watch**\n"
            f"{geopolitics_block}\n\n"
            f"**Other Market Drivers**\n"
            f"{market_block}\n\n"
            f"\U0001f4cc **Note:** headline scan is news-based; always verify exact event times on your economic/earnings calendar.",
            color=DISCORD_COLOR_WARN,
            webhook_url=DISCORD_WEBHOOK_MORNING_BRIEFING_URL or DISCORD_WEBHOOK_URL,
        )
        _mark_briefing_sent(sent_key, today_str, now_ct)
        log(f"{title} sent to Discord.")
        return now_ct.date()


def maybe_send_morning_briefing(client, now_ct=None):
    """Send one morning market briefing to Discord with macro + catalyst context."""
    global _morning_briefing_sent_date

    if not ENABLE_MORNING_BRIEFING:
        return

    now_ct = now_ct or datetime.now(central)
    _morning_briefing_sent_date = _maybe_send_scheduled_briefing(
        client,
        now_ct,
        "Morning Market Briefing",
        MORNING_BRIEFING_HOUR_CT,
        MORNING_BRIEFING_MINUTE_CT,
        _morning_briefing_sent_date,
        "morning",
    )


def maybe_send_midday_briefing(client, now_ct=None):
    """Send one midday market briefing to Discord with refreshed catalysts."""
    global _midday_briefing_sent_date

    if not ENABLE_MIDDAY_BRIEFING:
        return

    now_ct = now_ct or datetime.now(central)
    _midday_briefing_sent_date = _maybe_send_scheduled_briefing(
        client,
        now_ct,
        "Midday Market Briefing",
        MIDDAY_BRIEFING_HOUR_CT,
        MIDDAY_BRIEFING_MINUTE_CT,
        _midday_briefing_sent_date,
        "midday",
    )


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
    if _trading_client is None:
        raise Exception("Trading client not initialized")

    # Determine close direction from broker position side first.
    # Alpaca may report qty as positive even for short positions, so relying
    # only on qty sign can choose the wrong close side.
    pos = _trading_client.get_open_position(contract_symbol)
    pos_qty_raw = float(getattr(pos, "qty", 0) or 0)
    if pos_qty_raw == 0:
        raise Exception(f"No open qty to close for {contract_symbol}")

    pos_side_raw = str(getattr(pos, "side", "") or "").strip().lower()
    if "short" in pos_side_raw:
        close_side = OrderSide.BUY
    elif "long" in pos_side_raw:
        close_side = OrderSide.SELL
    else:
        close_side = OrderSide.SELL if pos_qty_raw > 0 else OrderSide.BUY

    close_qty = max(1, min(abs(int(pos_qty_raw)), int(max(1, qty))))
    log(
        f"[{_underlying_from_contract(contract_symbol) or contract_symbol}] Exit intent: "
        f"contract={contract_symbol} pos_side={pos_side_raw or 'unknown'} qty={pos_qty_raw} "
        f"-> side={close_side.name} qty={close_qty}"
    )

    order_req = MarketOrderRequest(
        symbol=contract_symbol,
        qty=close_qty,
        side=close_side,
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


def open_trade_record(symbol, signal, option, score, fill_price, qty, data=None):
    """Build a trade record using the actual Alpaca fill price (never a yfinance estimate)."""
    entry_price = fill_price
    side = option.get("side", signal.split()[-1])  # "CALL" or "PUT"
    target_pct, stop_pct = adaptive_target_stop_pcts(data or {}, score=score)
    runner_profile = runner_exit_profile_for_trade(signal, score, data or {}, target_pct)
    target_pct = float(runner_profile.get("target_pct", target_pct))
    partial_tp_pct = float(runner_profile.get("partial_tp_pct", PARTIAL_TP_PCT))
    partial_close_fraction = float(runner_profile.get("partial_close_fraction", PARTIAL_CLOSE_FRACTION))
    trailing_giveback_pct = float(runner_profile.get("trailing_giveback_pct", TRAILING_STOP_GIVEBACK_PCT))
    underlying_entry_price = _safe_float_num((data or {}).get("price", 0.0), 0.0)
    delta_5m, delta_10m = _score_trend_deltas(data or {}, side)
    entry_timing = _classify_entry_timing(data or {}, side)
    setup_type = str((data or {}).get("entry_playbook") or (data or {}).get("setup_type") or entry_timing or "UNKNOWN")
    ignition_delta = _safe_int_num((data or {}).get("ignition_delta", 0), 0)
    option_oi = _safe_int_num((option or {}).get("open_interest", 0), 0)
    return {
        "underlying": symbol,
        "signal":     signal,
        "side":       side,
        "contract":   option["contract"],
        "expiry":     option["expiry"],
        "strike":     option["strike"],
        "entry":      entry_price,
        "qty":        int(qty),
        "target":     entry_price * (1 + target_pct),
        "stop":       entry_price * (1 - stop_pct),
        "target_pct": target_pct,
        "stop_pct":   stop_pct,
        "score":      score,
        "max_pnl_pct": 0.0,
        "runner_profile": bool(runner_profile.get("runner_profile", False)),
        "partial_tp_pct": partial_tp_pct,
        "partial_close_fraction": partial_close_fraction,
        "trailing_giveback_pct": trailing_giveback_pct,
        "partial_taken": False,
        "partial_target": entry_price * (1 + max(0.01, partial_tp_pct)),
        "partial_close_qty": 0,
        "partial_exit_px": 0.0,
        "partial_pnl_pct": 0.0,
        "partial_realized_dollar": 0.0,
        "partial_realized_cost": 0.0,
        "entry_momentum_quality": _safe_float_num((data or {}).get("momentum_quality", 0.0), 0.0),
        "entry_vol_ratio": _safe_float_num((data or {}).get("vol_ratio", 1.0), 1.0),
        "underlying_entry_price": underlying_entry_price,
        "entry_bull_score": int((data or {}).get("bull_score", 0) or 0),
        "entry_bear_score": int((data or {}).get("bear_score", 0) or 0),
        "entry_delta_5m": delta_5m,
        "entry_delta_10m": delta_10m,
        "entry_rsi": _safe_float_num((data or {}).get("rsi", 50.0), 50.0),
        "entry_timing": entry_timing,
        "one_minute_trigger": str((data or {}).get("one_minute_trigger", "") or ""),
        "setup_type": setup_type,
        "entry_ignition_delta": ignition_delta,
        "entry_option_oi": option_oi,
        "entry_atr14": _safe_float_num((data or {}).get("atr14", 0.0), 0.0),
        "entry_support_level": ((data or {}).get("support_level") or {}).get("level") if isinstance((data or {}).get("support_level"), dict) else None,
        "entry_support_touches": ((data or {}).get("support_level") or {}).get("touches") if isinstance((data or {}).get("support_level"), dict) else None,
        "entry_support_strength": ((data or {}).get("support_level") or {}).get("strength") if isinstance((data or {}).get("support_level"), dict) else None,
        "entry_support_distance_atr": ((data or {}).get("support_level") or {}).get("distance_atr") if isinstance((data or {}).get("support_level"), dict) else None,
        "entry_resistance_level": ((data or {}).get("resistance_level") or {}).get("level") if isinstance((data or {}).get("resistance_level"), dict) else None,
        "entry_resistance_touches": ((data or {}).get("resistance_level") or {}).get("touches") if isinstance((data or {}).get("resistance_level"), dict) else None,
        "entry_resistance_strength": ((data or {}).get("resistance_level") or {}).get("strength") if isinstance((data or {}).get("resistance_level"), dict) else None,
        "entry_resistance_distance_atr": ((data or {}).get("resistance_level") or {}).get("distance_atr") if isinstance((data or {}).get("resistance_level"), dict) else None,
        "opened_at":  datetime.now(central),
        "status":     "OPEN",
        "entry_message_id": None,
        "milestone_10_sent": False,
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
            if strike_val <= 0:
                parsed_strike = _strike_from_contract(contract_sym)
                strike_val = float(parsed_strike) if parsed_strike is not None else 0.0
            strike = strike_val

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
        # Decide stop pct for this recovered/re-synced position.
        # For truly new recoveries (prev=None), apply tighter stop.
        # If market bias is opposing the position direction, tighten further.
        _rec_stop_pct = STOP_LOSS_PCT
        if prev is None and RECOVERED_MARKET_ALIGN_ENABLED:
            spy_side = _spy_vwap_side()
            qqq_side = _qqq_vwap_cache.get("side")
            pos_side = p["side"]
            spy_opposing  = (pos_side == "CALL" and spy_side == "bear") or (pos_side == "PUT" and spy_side == "bull")
            qqq_opposing  = (pos_side == "CALL" and qqq_side == "bear") or (pos_side == "PUT" and qqq_side == "bull")
            if spy_opposing or qqq_opposing:
                _rec_stop_pct = min(STOP_LOSS_PCT, max(0.01, RECOVERED_OPPOSING_STOP_PCT))
                log(
                    f"[{p['underlying']}] Recovered {pos_side} opposing market bias "
                    f"(spy={spy_side}, qqq={qqq_side}) — tighter stop {_rec_stop_pct*100:.0f}%."
                )
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
            "stop": p["entry"] * (1 - (float(prev.get("stop_pct", STOP_LOSS_PCT) or STOP_LOSS_PCT) if prev else _rec_stop_pct)),
            "target_pct": PROFIT_TARGET_PCT,
            "stop_pct": float(prev.get("stop_pct", STOP_LOSS_PCT) or STOP_LOSS_PCT) if prev else _rec_stop_pct,
            "score": prev.get("score", 0) if prev else 0,
            "max_pnl_pct": prev.get("max_pnl_pct", 0.0) if prev else 0.0,
            "runner_profile": bool(prev.get("runner_profile", False)) if prev else False,
            "partial_tp_pct": float(prev.get("partial_tp_pct", PARTIAL_TP_PCT) or PARTIAL_TP_PCT) if prev else PARTIAL_TP_PCT,
            "partial_close_fraction": float(prev.get("partial_close_fraction", PARTIAL_CLOSE_FRACTION) or PARTIAL_CLOSE_FRACTION) if prev else PARTIAL_CLOSE_FRACTION,
            "trailing_giveback_pct": float(prev.get("trailing_giveback_pct", TRAILING_STOP_GIVEBACK_PCT) or TRAILING_STOP_GIVEBACK_PCT) if prev else TRAILING_STOP_GIVEBACK_PCT,
            "partial_taken": bool(prev.get("partial_taken", False)) if prev else False,
            "partial_target": prev.get("partial_target", p["entry"] * (1 + max(0.01, float(prev.get("partial_tp_pct", PARTIAL_TP_PCT) if prev else PARTIAL_TP_PCT)))) if prev else p["entry"] * (1 + max(0.01, PARTIAL_TP_PCT)),
            "partial_close_qty": int(prev.get("partial_close_qty", 0) or 0) if prev else 0,
            "partial_exit_px": float(prev.get("partial_exit_px", 0.0) or 0.0) if prev else 0.0,
            "partial_pnl_pct": float(prev.get("partial_pnl_pct", 0.0) or 0.0) if prev else 0.0,
            "partial_realized_dollar": float(prev.get("partial_realized_dollar", 0.0) or 0.0) if prev else 0.0,
            "partial_realized_cost": float(prev.get("partial_realized_cost", 0.0) or 0.0) if prev else 0.0,
            "entry_momentum_quality": prev.get("entry_momentum_quality", 0.0) if prev else 0.0,
            "entry_vol_ratio": prev.get("entry_vol_ratio", 1.0) if prev else 1.0,
            "underlying_entry_price": prev.get("underlying_entry_price", 0.0) if prev else 0.0,
            "entry_bull_score": prev.get("entry_bull_score", 0) if prev else 0,
            "entry_bear_score": prev.get("entry_bear_score", 0) if prev else 0,
            "entry_delta_5m": prev.get("entry_delta_5m") if prev else None,
            "entry_delta_10m": prev.get("entry_delta_10m") if prev else None,
            "entry_rsi": prev.get("entry_rsi", 50.0) if prev else 50.0,
            "entry_timing": prev.get("entry_timing", "UNKNOWN") if prev else "UNKNOWN",
            "setup_type": prev.get("setup_type", "UNKNOWN") if prev else "UNKNOWN",
            "entry_ignition_delta": prev.get("entry_ignition_delta", 0) if prev else 0,
            "entry_option_oi": prev.get("entry_option_oi", 0) if prev else 0,
            "opened_at": prev.get("opened_at", datetime.now(central)) if prev else datetime.now(central),
            "status": "OPEN",
            "trade_id": prev.get("trade_id") if prev else None,
            "sheets_row": prev.get("sheets_row") if prev else None,
            "alerts_row": prev.get("alerts_row") if prev else None,
            "entry_message_id": prev.get("entry_message_id") if prev else None,
            "milestone_10_sent": prev.get("milestone_10_sent", False) if prev else False,
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


def same_direction_position_capacity_ok(symbol, side):
    """Limit correlated ETF/stock exposure in the same option direction."""
    side = str(side or "").upper()
    symbol = str(symbol or "").upper()
    matching = [
        trade for trade in _open_trades.values()
        if str(trade.get("side", "")).upper() == side
    ]
    if MAX_SAME_DIRECTION_POSITIONS > 0 and len(matching) >= MAX_SAME_DIRECTION_POSITIONS:
        return False, f"{side} exposure cap {len(matching)}/{MAX_SAME_DIRECTION_POSITIONS}"

    category_cap = (
        MAX_SAME_DIRECTION_ETF_POSITIONS if symbol in ETF_SYMBOLS
        else MAX_SAME_DIRECTION_STOCK_POSITIONS
    )
    category_name = "ETF" if symbol in ETF_SYMBOLS else "stock"
    category_count = sum(
        1 for trade in matching
        if (str(trade.get("underlying", "")).upper() in ETF_SYMBOLS) == (symbol in ETF_SYMBOLS)
    )
    if category_cap > 0 and category_count >= category_cap:
        return False, f"{side} {category_name} exposure cap {category_count}/{category_cap}"
    return True, ""


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


def _underlying_thesis_state(trade):
    """Return thesis status from underlying structure, independent of option premium PnL."""
    try:
        symbol = str(trade.get("underlying", "") or "").upper()
        side = str(trade.get("side", "") or "").upper()
        if not symbol or side not in ("CALL", "PUT"):
            return {"ready": False, "invalid": False, "reason": "MISSING TRADE CONTEXT", "price": 0.0, "vwap": 0.0, "ema20": 0.0}

        client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
        bars = fetch_bars(client, symbol)
        if bars is None or bars.empty or len(bars) < 30:
            return {"ready": False, "invalid": False, "reason": "NO UNDERLYING BARS", "price": 0.0, "vwap": 0.0, "ema20": 0.0}

        df = calculate_indicators(bars)
        if df is None or df.empty or len(df) < 8:
            return {"ready": False, "invalid": False, "reason": "INSUFFICIENT INDICATORS", "price": 0.0, "vwap": 0.0, "ema20": 0.0}

        latest = df.iloc[-1]
        price = _safe_float_num(latest.get("close"), 0.0)
        vwap = _safe_float_num(latest.get("VWAP"), price)
        ema20 = _safe_float_num(latest.get("EMA20"), price)
        if price <= 0 or vwap <= 0 or ema20 <= 0:
            return {"ready": False, "invalid": False, "reason": "INVALID INDICATOR VALUES", "price": price, "vwap": vwap, "ema20": ema20}

        ema20_back = _safe_float_num(df["EMA20"].iloc[-4], ema20)
        ema20_slope_pct = ((ema20 - ema20_back) / ema20_back) if ema20_back else 0.0
        recent_swing_low = _safe_float_num(df["low"].iloc[-6:-1].min(), price)
        recent_swing_high = _safe_float_num(df["high"].iloc[-6:-1].max(), price)
        setup = str(trade.get("setup_type", trade.get("entry_timing", "")) or "").upper()
        entry_support = _safe_float_num(trade.get("entry_support_level"), 0.0)
        entry_resistance = _safe_float_num(trade.get("entry_resistance_level"), 0.0)

        if side == "CALL":
            below_structure = price < (min(vwap, ema20) * (1.0 - max(0.0, THESIS_STRUCTURE_TOLERANCE_PCT)))
            higher_low_broken = price < (recent_swing_low * (1.0 - max(0.0, THESIS_SWING_BREAK_TOLERANCE_PCT)))
            breakout_fail = (
                ("BREAKOUT" in setup or "CONTINUATION" in setup)
                and entry_resistance > 0
                and price < (entry_resistance * (1.0 - max(0.0, THESIS_LEVEL_TOLERANCE_PCT)))
            )
            regime_reversal = ema20_slope_pct <= -abs(THESIS_REVERSAL_SLOPE_PCT) and price < ema20

            if breakout_fail:
                return {"ready": True, "invalid": True, "reason": "BREAKOUT/RETEST FAILED", "price": price, "vwap": vwap, "ema20": ema20}
            if higher_low_broken and below_structure:
                return {"ready": True, "invalid": True, "reason": "HIGHER-LOW STRUCTURE BROKE", "price": price, "vwap": vwap, "ema20": ema20}
            if below_structure and regime_reversal:
                return {"ready": True, "invalid": True, "reason": "LOST VWAP/EMA20 WITH REVERSAL", "price": price, "vwap": vwap, "ema20": ema20}
            return {"ready": True, "invalid": False, "reason": "CALL THESIS INTACT", "price": price, "vwap": vwap, "ema20": ema20}

        above_structure = price > (max(vwap, ema20) * (1.0 + max(0.0, THESIS_STRUCTURE_TOLERANCE_PCT)))
        lower_high_broken = price > (recent_swing_high * (1.0 + max(0.0, THESIS_SWING_BREAK_TOLERANCE_PCT)))
        breakdown_fail = (
            ("BREAKDOWN" in setup or "REJECT" in setup)
            and entry_support > 0
            and price > (entry_support * (1.0 + max(0.0, THESIS_LEVEL_TOLERANCE_PCT)))
        )
        regime_reversal = ema20_slope_pct >= abs(THESIS_REVERSAL_SLOPE_PCT) and price > ema20

        if breakdown_fail:
            return {"ready": True, "invalid": True, "reason": "BREAKDOWN/RETEST FAILED", "price": price, "vwap": vwap, "ema20": ema20}
        if lower_high_broken and above_structure:
            return {"ready": True, "invalid": True, "reason": "LOWER-HIGH STRUCTURE BROKE", "price": price, "vwap": vwap, "ema20": ema20}
        if above_structure and regime_reversal:
            return {"ready": True, "invalid": True, "reason": "RECLAIMED VWAP/EMA20 WITH REVERSAL", "price": price, "vwap": vwap, "ema20": ema20}
        return {"ready": True, "invalid": False, "reason": "PUT THESIS INTACT", "price": price, "vwap": vwap, "ema20": ema20}
    except Exception as e:
        return {"ready": False, "invalid": False, "reason": f"THESIS CHECK ERROR: {e}", "price": 0.0, "vwap": 0.0, "ema20": 0.0}


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
                    target_pct = float(trade.get("target_pct", PROFIT_TARGET_PCT) or PROFIT_TARGET_PCT)
                    stop_pct = float(trade.get("stop_pct", STOP_LOSS_PCT) or STOP_LOSS_PCT)
                    partial_tp_pct = float(trade.get("partial_tp_pct", PARTIAL_TP_PCT) or PARTIAL_TP_PCT)
                    trade["target"] = alpaca_entry * (1 + target_pct)
                    trade["stop"] = alpaca_entry * (1 - stop_pct)
                    trade["partial_target"] = alpaca_entry * (1 + max(0.01, partial_tp_pct))
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

        maybe_send_trade_progress_alert(trade, current_price, pnl_pct)

        target_pct = float(trade.get("target_pct", PROFIT_TARGET_PCT) or PROFIT_TARGET_PCT)
        stop_pct = float(trade.get("stop_pct", STOP_LOSS_PCT) or STOP_LOSS_PCT)
        partial_target = float(trade.get("partial_target", trade.get("entry", 0.0) * (1 + max(0.01, PARTIAL_TP_PCT))) or 0.0)
        partial_taken = bool(trade.get("partial_taken", False))
        partial_close_fraction = float(trade.get("partial_close_fraction", PARTIAL_CLOSE_FRACTION) or PARTIAL_CLOSE_FRACTION)
        trailing_giveback_pct = float(trade.get("trailing_giveback_pct", TRAILING_STOP_GIVEBACK_PCT) or TRAILING_STOP_GIVEBACK_PCT)
        held_minutes = max(1, int((datetime.now(central) - trade.get("opened_at", datetime.now(central))).total_seconds() // 60))

        # 1) Primary exit: underlying thesis invalidation (not option PnL).
        thesis_state = _underlying_thesis_state(trade)
        if thesis_state.get("ready"):
            trade["thesis_data_fail_count"] = 0
            if thesis_state.get("invalid"):
                close_trade(trade, current_price, f"THESIS INVALIDATION: {thesis_state.get('reason', 'STRUCTURE FAILED')}", pnl_pct)
                continue
        else:
            miss_count = int(trade.get("thesis_data_fail_count", 0) or 0) + 1
            trade["thesis_data_fail_count"] = miss_count
            if EMERGENCY_DATA_FAIL_EXIT_ENABLED and miss_count >= max(1, EMERGENCY_DATA_FAIL_CYCLES):
                close_trade(trade, current_price, "EMERGENCY EXIT: UNDERLYING DATA MONITORING FAILED", pnl_pct)
                continue

        # 2) Profit management: take partial profits first, then trail runner.
        if (not partial_taken) and current_price >= partial_target and int(trade.get("qty", 0) or 0) > 1:
            partial_qty = max(1, int(round(int(trade.get("qty", 0)) * max(0.1, min(0.9, partial_close_fraction)))))
            close_trade(trade, current_price, "PARTIAL TAKE PROFIT", pnl_pct, close_qty=partial_qty, final_close=False)
            # After partial TP, protect remaining risk by moving stop near break-even.
            trade["stop"] = max(float(trade.get("stop", 0.0) or 0.0), float(trade.get("entry", 0.0) or 0.0) * 0.99)
            continue

        # Single-lot trades cannot scale out; allow full target exit.
        if (not partial_taken) and int(trade.get("qty", 0) or 0) <= 1 and pnl_pct >= target_pct:
            close_trade(trade, current_price, "TARGET HIT", pnl_pct)
            continue

        # 3) Runner trailing after partial take (underlying-based, not option-premium-based).
        if partial_taken and thesis_state.get("ready"):
            side = str(trade.get("side", "") or "").upper()
            underlying_now = _safe_float_num(thesis_state.get("price"), 0.0)
            trail_pct = max(0.0005, abs(RUNNER_UNDERLYING_TRAIL_PCT))
            if side == "CALL" and underlying_now > 0:
                peak = max(_safe_float_num(trade.get("runner_peak_underlying"), underlying_now), underlying_now)
                trade["runner_peak_underlying"] = peak
                if underlying_now <= peak * (1.0 - trail_pct):
                    close_trade(trade, current_price, "RUNNER TRAIL HIT (UNDERLYING)", pnl_pct)
                    continue
            if side == "PUT" and underlying_now > 0:
                trough = min(_safe_float_num(trade.get("runner_trough_underlying"), underlying_now) or underlying_now, underlying_now)
                trade["runner_trough_underlying"] = trough
                if underlying_now >= trough * (1.0 + trail_pct):
                    close_trade(trade, current_price, "RUNNER TRAIL HIT (UNDERLYING)", pnl_pct)
                    continue

        # 3b) Independent option-P&L peak-giveback guard for the runner. The underlying
        # can stay technically intact while delta/gamma/IV decay erodes most of the
        # option's own peak gain — this protects that peak without replacing the
        # underlying-based trail above.
        if partial_taken:
            runner_peak_pnl = float(trade.get("max_pnl_pct", pnl_pct) or pnl_pct)
            if runner_peak_pnl >= RUNNER_PNL_TRAIL_ACTIVATE and pnl_pct <= (runner_peak_pnl - RUNNER_PNL_MAX_GIVEBACK):
                close_trade(trade, current_price, "RUNNER PNL GIVEBACK STOP", pnl_pct)
                continue

        # 4) Emergency option-premium stop as risk backstop only.
        if pnl_pct <= -stop_pct:
            close_trade(trade, current_price, "EMERGENCY STOP LOSS", pnl_pct)
            continue

        # 5) Time stop: if the trade has sat too long, exit to avoid prolonged theta decay.
        if MAX_TRADE_HOLD_MINUTES > 0 and held_minutes >= MAX_TRADE_HOLD_MINUTES:
            close_trade(trade, current_price, f"TIME EXIT ({held_minutes}m)", pnl_pct)
            continue


def _capture_exit_market_context(trade):
    """Return the underlying structure at exit so alerts can explain the trade outcome."""
    symbol = str(trade.get("underlying", "") or "").upper()
    if not symbol:
        return {}
    try:
        client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
        bars = fetch_bars(client, symbol)
        if bars is None or bars.empty or len(bars) < 6:
            return {}
        df = calculate_indicators(bars)
        latest = df.iloc[-1]
        price = _safe_float_num(latest.get("close"), 0.0)
        vwap = _safe_float_num(latest.get("VWAP"), price)
        ema20 = _safe_float_num(latest.get("EMA20"), price)
        ema50 = _safe_float_num(latest.get("EMA50"), price)
        close_2 = _safe_float_num(df["close"].iloc[-3], price)
        close_5 = _safe_float_num(df["close"].iloc[-6], price)
        return {
            "price": price,
            "vwap": vwap,
            "ema20": ema20,
            "ema50": ema50,
            "move_2bar_pct": ((price - close_2) / close_2 * 100.0) if close_2 else 0.0,
            "move_5bar_pct": ((price - close_5) / close_5 * 100.0) if close_5 else 0.0,
        }
    except Exception as e:
        log(f"[{symbol}] Exit market-context capture failed: {e}")
        return {}


def _render_trade_candlestick_chart(trade, exit_price, closed_at):
    """Render a compact 5-minute underlying chart with entry and exit markers."""
    symbol = str(trade.get("underlying", "") or "").upper()
    opened_at = trade.get("opened_at")
    if not symbol or not isinstance(opened_at, datetime) or not isinstance(closed_at, datetime):
        return None
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.dates as mdates
        import matplotlib.pyplot as plt
        from matplotlib.patches import Rectangle

        client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
        request = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame(BAR_MINUTES, TimeFrameUnit.Minute),
            start=opened_at.astimezone(timezone.utc) - timedelta(minutes=45),
            end=closed_at.astimezone(timezone.utc) + timedelta(minutes=5),
            feed=DataFeed(FEED),
        )
        bars = client.get_stock_bars(request).df
        if bars is None or bars.empty:
            return None
        if isinstance(bars.index, pd.MultiIndex):
            bars = bars.xs(symbol, level=0)
        bars = calculate_indicators(bars[["open", "high", "low", "close", "volume"]])

        def _containing_bar(event_time):
            """Return the 5-minute bar containing an event timestamp."""
            event_timestamp = pd.Timestamp(event_time)
            bar_timezone = bars.index.tz
            if event_timestamp.tzinfo is None:
                event_timestamp = event_timestamp.tz_localize(central)
            if bar_timezone is not None:
                event_timestamp = event_timestamp.tz_convert(bar_timezone)
            else:
                event_timestamp = event_timestamp.tz_localize(None)
            position = bars.index.searchsorted(event_timestamp, side="right") - 1
            position = max(0, min(int(position), len(bars) - 1))
            return bars.index[position], bars.iloc[position]

        entry_bar_time, entry_bar = _containing_bar(opened_at)
        exit_bar_time, exit_bar = _containing_bar(closed_at)
        fig, axis = plt.subplots(figsize=(9, 5), dpi=150)
        fig.patch.set_facecolor("#111827")
        axis.set_facecolor("#111827")
        dates = mdates.date2num(bars.index.to_pydatetime())
        width = (BAR_MINUTES / (24 * 60)) * 0.72
        for date_num, (_, candle) in zip(dates, bars.iterrows()):
            color = "#22c55e" if candle["close"] >= candle["open"] else "#ef4444"
            axis.vlines(date_num, candle["low"], candle["high"], color=color, linewidth=1.1)
            body_low = min(candle["open"], candle["close"])
            body_height = max(abs(candle["close"] - candle["open"]), 0.001)
            axis.add_patch(Rectangle((date_num - width / 2, body_low), width, body_height, color=color, alpha=0.9))
        axis.plot(bars.index, bars["VWAP"], color="#60a5fa", linewidth=1.25, label="VWAP")
        axis.plot(bars.index, bars["EMA20"], color="#f59e0b", linewidth=1.25, label="EMA20")
        entry_underlying = _safe_float_num(trade.get("underlying_entry_price"), 0.0)
        entry_marker_price = entry_underlying if entry_underlying > 0 else float(entry_bar["close"])
        exit_marker_price = float(exit_bar["close"])
        opened_label = opened_at.astimezone(central).strftime("%H:%M CT")
        closed_label = closed_at.astimezone(central).strftime("%H:%M CT")
        entry_candle_label = entry_bar_time.tz_convert(central).strftime("%H:%M") if entry_bar_time.tzinfo else entry_bar_time.strftime("%H:%M")
        exit_candle_label = exit_bar_time.tz_convert(central).strftime("%H:%M") if exit_bar_time.tzinfo else exit_bar_time.strftime("%H:%M")
        axis.scatter([entry_bar_time], [entry_marker_price], color="#22c55e", s=55, zorder=5, label="Entry")
        axis.scatter([exit_bar_time], [exit_marker_price], color="#ef4444", s=55, zorder=5, label="Exit")
        axis.annotate(
            f"ENTRY {opened_label}\nbar {entry_candle_label} | ${entry_marker_price:.2f}",
            (entry_bar_time, entry_marker_price), xytext=(8, 12), textcoords="offset points",
            color="#bbf7d0", fontsize=7, weight="bold",
        )
        axis.annotate(
            f"EXIT {closed_label}\nbar {exit_candle_label} | ${exit_marker_price:.2f}",
            (exit_bar_time, exit_marker_price), xytext=(8, -22), textcoords="offset points",
            color="#fecaca", fontsize=7, weight="bold",
        )
        axis.axvline(entry_bar_time, color="#22c55e", linestyle="--", linewidth=0.8, alpha=0.7)
        axis.axvline(exit_bar_time, color="#ef4444", linestyle="--", linewidth=0.8, alpha=0.7)
        axis.set_title(
            f"{symbol} 5-Minute Candles | {trade.get('contract', '')}\n"
            f"{trade.get('side', '')} option ${trade.get('entry', 0):.2f} -> ${exit_price:.2f} | "
            f"{opened_label} -> {closed_label}",
            color="#f9fafb", pad=12, fontsize=10,
        )
        axis.grid(color="#374151", alpha=0.45, linewidth=0.6)
        axis.tick_params(colors="#d1d5db")
        for spine in axis.spines.values():
            spine.set_color("#4b5563")
        axis.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=central))
        axis.set_ylabel("Underlying price", color="#d1d5db")
        legend = axis.legend(loc="upper left", frameon=False, ncol=4, fontsize=8)
        for text in legend.get_texts():
            text.set_color("#e5e7eb")
        fig.tight_layout()
        buffer = BytesIO()
        fig.savefig(buffer, format="png", facecolor=fig.get_facecolor(), bbox_inches="tight")
        plt.close(fig)
        return buffer.getvalue()
    except Exception as e:
        log(f"[{symbol}] Candlestick chart render failed: {e}")
        return None


def _trade_exit_explanation(trade, reason, entry_price, exit_price, pnl_pct, market_context=None):
    """Explain the observed market reason, option move, and exit rule."""
    reason_text = str(reason or "").upper()
    side = str(trade.get("side", trade.get("signal", "")) or "").upper()
    move = "rose" if pnl_pct > 0 else "fell" if pnl_pct < 0 else "finished flat"
    price_move = f"option premium {move} from ${entry_price:.2f} to ${exit_price:.2f} ({pnl_pct * 100:+.2f}%)"
    context = market_context or {}
    underlying_price = _safe_float_num(context.get("price"), 0.0)
    vwap = _safe_float_num(context.get("vwap"), 0.0)
    ema20 = _safe_float_num(context.get("ema20"), 0.0)
    move_2bar = _safe_float_num(context.get("move_2bar_pct"), 0.0)
    move_5bar = _safe_float_num(context.get("move_5bar_pct"), 0.0)
    call_structure_failed = underlying_price > 0 and underlying_price < min(vwap, ema20)
    put_structure_failed = underlying_price > 0 and underlying_price > max(vwap, ema20)
    structure_failed = call_structure_failed if "CALL" in side else put_structure_failed
    if underlying_price > 0:
        if "CALL" in side:
            structure_state = "buyers lost short-term control" if structure_failed else "buyers still held short-term support"
        else:
            structure_state = "sellers lost short-term control" if structure_failed else "sellers still held short-term pressure"
        structure_text = (
            f"{structure_state}: ${underlying_price:.2f} vs VWAP ${vwap:.2f}/EMA20 ${ema20:.2f}; "
            f"last 2 bars {move_2bar:+.2f}%, last 5 bars {move_5bar:+.2f}%"
        )
    else:
        structure_text = "underlying exit structure was unavailable"

    if reason_text == "TARGET HIT":
        target_pct = float(trade.get("target_pct", PROFIT_TARGET_PCT) or PROFIT_TARGET_PCT) * 100
        return f"The {side} idea followed through. {structure_text}. {price_move}; the +{target_pct:.0f}% target was reached."
    if reason_text.startswith("THESIS INVALIDATION"):
        return f"The {side} idea was exited because the underlying thesis broke. {structure_text}. {price_move}."
    if reason_text == "STOP LOSS":
        stop_pct = float(trade.get("stop_pct", STOP_LOSS_PCT) or STOP_LOSS_PCT) * 100
        if structure_failed:
            return f"The {side} idea lost support. {structure_text}. {price_move}, so the -{stop_pct:.0f}% risk stop closed it."
        return f"The option weakened before the underlying trend clearly broke. {structure_text}. {price_move}; the -{stop_pct:.0f}% risk stop closed it."
    if reason_text == "EMERGENCY STOP LOSS":
        stop_pct = float(trade.get("stop_pct", STOP_LOSS_PCT) or STOP_LOSS_PCT) * 100
        return f"Emergency risk backstop triggered at -{stop_pct:.0f}% option PnL. {structure_text}. {price_move}."
    if reason_text.startswith("EMERGENCY EXIT"):
        return f"Emergency risk protocol exited the trade because monitoring reliability degraded. {structure_text}. {price_move}."
    if reason_text == "MOMENTUM FAILURE":
        structure = "below VWAP and EMA20" if "CALL" in side else "above VWAP and EMA20"
        return f"The {side} idea lost momentum: price moved {structure}. {structure_text}. {price_move}."
    if reason_text == "PARTIAL TAKE PROFIT":
        return f"The {side} idea followed through. {structure_text}. {price_move}; the first profit objective was realized."
    if reason_text.startswith("RUNNER TRAIL HIT"):
        return f"The {side} runner gave back enough underlying progress to hit the trail. {structure_text}. {price_move}."
    if reason_text == "RUNNER PNL GIVEBACK STOP":
        peak_pct = float(trade.get("max_pnl_pct", pnl_pct) or pnl_pct) * 100
        return (
            f"The {side} runner's option profit peaked near +{peak_pct:.1f}% and gave back too much "
            f"before the underlying thesis broke. {structure_text}. {price_move}."
        )
    if reason_text == "TRAILING STOP":
        max_seen = float(trade.get("max_pnl_pct", pnl_pct) or pnl_pct) * 100
        return f"The initial {side} move worked, then gave back gains. {structure_text}. Profit retraced from +{max_seen:.2f}% and the trailing stop closed it."
    if reason_text.startswith("TIME EXIT"):
        return f"The {side} idea did not move soon enough. {structure_text}. {price_move}."
    return f"{price_move}; {structure_text}; exit triggered by {reason}."


def _sheets_exit_reason(reason, pnl_pct, pnl_dollar, market_context):
    """Keep the concrete exit trigger and observed underlying structure in Sheets."""
    context = market_context or {}
    price = _safe_float_num(context.get("price"), 0.0)
    vwap = _safe_float_num(context.get("vwap"), 0.0)
    ema20 = _safe_float_num(context.get("ema20"), 0.0)
    move_2bar = _safe_float_num(context.get("move_2bar_pct"), 0.0)
    move_5bar = _safe_float_num(context.get("move_5bar_pct"), 0.0)
    outcome = "PROFIT" if pnl_pct > 0 else "LOSS"
    summary = f"{reason} | {outcome} ({pnl_pct * 100:+.2f}% / ${pnl_dollar:+.2f})"
    if price > 0:
        summary += (
            f" | exit underlying ${price:.2f} vs VWAP ${vwap:.2f}/EMA20 ${ema20:.2f}"
            f"; 2-bar {move_2bar:+.2f}%, 5-bar {move_5bar:+.2f}%"
        )
    return summary


def schedule_exit_review(trade, reason, entry_price, exit_price, pnl_pct, closed_at, market_context):
    """Create one durable, observation-only review due after a final exit."""
    if not EXIT_REVIEW_ENABLED:
        return
    if _gsheet is None and not ensure_google_sheets_ready():
        log(f"[{trade.get('underlying', 'UNKNOWN')}] Exit review scheduling skipped: Google Sheets unavailable.")
        return
    try:
        review_id = str(uuid.uuid4())[:8].upper()
        due_at = closed_at + timedelta(minutes=max(1, EXIT_REVIEW_DELAY_MINUTES))
        context = market_context or {}
        row = [
            review_id,
            "PENDING",
            due_at.strftime("%Y-%m-%d %H:%M:%S"),
            "",
            trade.get("trade_id", ""),
            trade.get("underlying", ""),
            trade.get("contract", ""),
            trade.get("side", trade.get("signal", "")),
            trade.get("opened_at").strftime("%Y-%m-%d %H:%M:%S") if isinstance(trade.get("opened_at"), datetime) else str(trade.get("opened_at", "")),
            closed_at.strftime("%Y-%m-%d %H:%M:%S"),
            reason,
            round(float(entry_price), 4),
            round(float(exit_price), 4),
            "",
            round(float(pnl_pct) * 100.0, 2),
            "",
            round(_safe_float_num(context.get("price"), 0.0), 4),
            "",
            "",
            "",
            "",
            "",
            "PENDING",
            "One 30-minute observation-only review; no automatic re-entry.",
            str(trade.get("entry_message_id", "") or ""),
        ]
        _gsheet.worksheet("Exit Reviews").append_row(row, value_input_option="USER_ENTERED")
        log(f"[{trade.get('underlying', 'UNKNOWN')}] Exit review {review_id} scheduled for {due_at:%H:%M CT}.")
    except Exception as e:
        log(f"[{trade.get('underlying', 'UNKNOWN')}] Exit review scheduling failed: {e}")


def _exit_review_assessment(exit_trigger, exit_option, review_option):
    """Classify a review using only the post-exit option move."""
    if exit_option <= 0 or review_option <= 0:
        return "INCONCLUSIVE", "No reliable option quote was available for the review."
    post_exit_move_pct = (review_option - exit_option) / exit_option * 100.0
    target_exit = str(exit_trigger or "").upper() in {"TARGET HIT", "PARTIAL TAKE PROFIT"}
    if target_exit:
        if post_exit_move_pct >= 5.0:
            return "LEFT MORE UPSIDE", "The option continued higher after the planned profit exit."
        if post_exit_move_pct <= -3.0:
            return "EXIT VALIDATED", "The option gave back value after the planned profit exit."
    else:
        if post_exit_move_pct <= -3.0:
            return "EXIT VALIDATED", "The option continued lower after the exit."
        if post_exit_move_pct >= 5.0:
            return "EXIT EARLY", "The option recovered meaningfully after the exit."
    return "INCONCLUSIVE", "The option move after exit was not large enough to judge the exit."


def process_due_exit_reviews(now_ct=None):
    """Perform due post-exit reviews; this observer never opens or closes positions."""
    if not EXIT_REVIEW_ENABLED:
        return
    if _gsheet is None and not ensure_google_sheets_ready():
        return
    now_ct = now_ct or datetime.now(central)
    try:
        ws = _gsheet.worksheet("Exit Reviews")
        rows = ws.get_all_values()
        if len(rows) <= 1:
            return
        headers = rows[0]
        index = {name: idx for idx, name in enumerate(headers)}
        for row_number, raw_row in enumerate(rows[1:], start=2):
            row = list(raw_row) + [""] * max(0, len(headers) - len(raw_row))
            if str(row[index["Status"]]).upper() != "PENDING":
                continue
            try:
                due_at = central.localize(datetime.strptime(row[index["Review Due CT"]], "%Y-%m-%d %H:%M:%S"))
            except (TypeError, ValueError):
                row[index["Status"]] = "ERROR"
                row[index["Notes"]] = "Could not parse review due time."
                ws.update(range_name=f"A{row_number}", values=[row], value_input_option="USER_ENTERED")
                continue
            if due_at > now_ct:
                continue

            contract = str(row[index["Contract"]] or "")
            symbol = str(row[index["Symbol"]] or "")
            side = str(row[index["Side"]] or "").upper()
            exit_option = _safe_float_num(row[index["Exit Option"]], 0.0)
            review_option = get_current_option_price({"contract": contract, "underlying": symbol})
            market_context = _capture_exit_market_context({"underlying": symbol})
            assessment, notes = _exit_review_assessment(
                row[index["Exit Trigger"]], exit_option, _safe_float_num(review_option, 0.0)
            )
            if review_option and exit_option > 0:
                post_exit_move_pct = (float(review_option) - exit_option) / exit_option * 100.0
            else:
                post_exit_move_pct = None

            row[index["Status"]] = "COMPLETED"
            row[index["Reviewed At CT"]] = now_ct.strftime("%Y-%m-%d %H:%M:%S")
            row[index["Option at Review"]] = round(float(review_option), 4) if review_option else ""
            row[index["Option Move After Exit %"]] = round(post_exit_move_pct, 2) if post_exit_move_pct is not None else ""
            row[index["Underlying at Review"]] = round(_safe_float_num(market_context.get("price"), 0.0), 4) or ""
            row[index["VWAP at Review"]] = round(_safe_float_num(market_context.get("vwap"), 0.0), 4) or ""
            row[index["EMA20 at Review"]] = round(_safe_float_num(market_context.get("ema20"), 0.0), 4) or ""
            row[index["2-Bar Move %"]] = round(_safe_float_num(market_context.get("move_2bar_pct"), 0.0), 2)
            row[index["5-Bar Move %"]] = round(_safe_float_num(market_context.get("move_5bar_pct"), 0.0), 2)
            row[index["Assessment"]] = assessment
            row[index["Notes"]] = notes
            ws.update(range_name=f"A{row_number}", values=[row], value_input_option="USER_ENTERED")

            review_option_text = f"${float(review_option):.2f}" if review_option else "unavailable"
            move_text = f"{post_exit_move_pct:+.2f}%" if post_exit_move_pct is not None else "unavailable"
            underlying_text = "underlying review data unavailable"
            if _safe_float_num(market_context.get("price"), 0.0) > 0:
                underlying_text = (
                    f"underlying ${market_context['price']:.2f} vs VWAP ${market_context['vwap']:.2f}/"
                    f"EMA20 ${market_context['ema20']:.2f}; last 2 bars {market_context['move_2bar_pct']:+.2f}%, "
                    f"last 5 bars {market_context['move_5bar_pct']:+.2f}%"
                )
            message = (
                f"\U0001f50e **POST-EXIT CHECK — {symbol} | {side} | +{max(1, EXIT_REVIEW_DELAY_MINUTES)}m**\n\n"
                f"**We exited:** `${exit_option:.2f}`\n"
                f"**Option now:** `{review_option_text}` (`{move_text}` after exit)\n"
                f"**Assessment:** `{assessment}`\n"
                f"**What happened:** {notes}\n"
                f"**Market now:** {underlying_text}"
            )
            message_id = str(row[index["Discord Message ID"]] or "")
            sent = send_discord_bot_reply(message_id, message, color=DISCORD_COLOR_WARN)
            if not sent:
                send_discord(message, color=DISCORD_COLOR_WARN, webhook_url=DISCORD_WEBHOOK_LIVE_TRADES_URL)
            log(f"[{symbol}] Exit review {row[index['Review ID']]} completed: {assessment}.")
    except Exception as e:
        log(f"Exit review processing failed: {e}")


def close_trade(trade, exit_price, reason, pnl_pct, close_qty=None, final_close=True):
    """Submit a paper exit (if enabled), Discord the result, and append to CSV.

    exit_price is the mid-price trigger used to detect target/stop.  After
    submitting the market SELL we replace it with the actual Alpaca fill price
    so Discord and the CSV reflect what the account really received (bid-side).
    """
    current_qty = int(trade.get("qty", max(BASE_POSITION_QTY, MIN_POSITION_QTY)) or 0)
    close_qty_int = int(max(1, min(current_qty, int(close_qty or current_qty))))

    exit_confirmed = not ENABLE_ALPACA_PAPER_TRADING
    if ENABLE_ALPACA_PAPER_TRADING and _trading_client is not None:
        try:
            _, fill_price = place_paper_exit(
                trade["contract"],
                close_qty_int,
            )
            if fill_price is not None:
                # Recalculate PnL using the real fill, not the mid-price estimate.
                actual_exit = fill_price
                actual_pnl  = (actual_exit - trade["entry"]) / trade["entry"]
                log(f"[{trade['underlying']}] Exit fill ${actual_exit:.2f} "
                    f"(mid was ${exit_price:.2f}, diff ${actual_exit - exit_price:+.2f})")
                exit_price = actual_exit
                pnl_pct    = actual_pnl
                exit_confirmed = True
            else:
                # Order submitted but fill unknown: verify whether broker position is gone.
                try:
                    _trading_client.get_open_position(trade["contract"])
                    log(
                        f"[{trade['underlying']}] Exit submit pending/unfilled for {trade['contract']} "
                        "— keeping trade OPEN."
                    )
                    return
                except Exception:
                    # Position no longer open at broker; treat as closed using trigger price.
                    log(f"[{trade['underlying']}] Broker position already closed for {trade['contract']}.")
                    exit_confirmed = True
        except Exception as e:
            log(f"[{trade['underlying']}] Paper exit submit failed: {e}")
            # If position still exists, do not mark as closed locally.
            try:
                _trading_client.get_open_position(trade["contract"])
                log(
                    f"[{trade['underlying']}] Exit not confirmed at broker for {trade['contract']} "
                    "— keeping trade OPEN."
                )
                return
            except Exception:
                # Position not found => likely closed externally; allow local close bookkeeping.
                log(f"[{trade['underlying']}] Broker position missing for {trade['contract']} after exit error; treating as closed.")
                exit_confirmed = True

    if not exit_confirmed:
        return

    exit_market_context = _capture_exit_market_context(trade)
    emoji = "\u2705" if pnl_pct > 0 else "\u274c"
    outcome_label = "PROFIT" if pnl_pct > 0 else "LOSS"
    closed_at = datetime.now(central)
    duration_min = max(1, int((closed_at - trade["opened_at"]).total_seconds() // 60))
    entry_px = float(trade["entry"])
    exit_px = float(exit_price)
    remaining_qty = max(0, int(current_qty - close_qty_int))
    exit_scope = "PARTIAL EXIT" if (not final_close and close_qty_int < current_qty) else "FINAL EXIT"
    strike_val = float(trade.get("strike", 0) or 0)
    if strike_val <= 0:
        parsed_strike = _strike_from_contract(trade.get("contract", ""))
        if parsed_strike is not None:
            strike_val = float(parsed_strike)
    strike_text = str(int(strike_val)) if abs(strike_val - int(strike_val)) < 1e-9 else f"{strike_val:.2f}"
    exit_header = f"{trade['underlying']} strike {strike_text}"

    # Compute blended (combined) P&L if this is a final close after a partial exit.
    _prev_partial_qty = int(trade.get("partial_close_qty", 0) or 0)
    _prev_partial_px = float(trade.get("partial_exit_px", 0.0) or 0.0)
    _prev_partial_dollar = float(trade.get("partial_realized_dollar", 0.0) or 0.0)
    _prev_partial_cost = float(trade.get("partial_realized_cost", 0.0) or 0.0)
    # Restart-safe fallback: if in-memory partial state is missing, reconstruct
    # cumulative partial totals from the existing Trades partial rows.
    if final_close and _prev_partial_qty <= 0 and abs(_prev_partial_dollar) < 1e-9:
        partial_rows, partial_dollar, partial_cost = _partial_totals_from_trades_sheet(trade)
        if partial_rows > 0:
            _prev_partial_dollar = float(partial_dollar)
            _prev_partial_cost = float(partial_cost)
            if entry_px > 0 and _prev_partial_cost > 0:
                _prev_partial_qty = int(round(_prev_partial_cost / (entry_px * 100.0)))
    _combined_dollar = 0.0
    _combined_cost = 0.0
    combined_pnl_pct = pnl_pct
    combined_note = ""
    if final_close and (_prev_partial_qty > 0 or abs(_prev_partial_dollar) > 1e-9) and entry_px > 0:
        # Backward compatibility for trades opened before cumulative partial
        # accounting fields were introduced.
        if (_prev_partial_cost <= 0 or _prev_partial_dollar == 0.0) and _prev_partial_px > 0:
            _prev_partial_dollar = (_prev_partial_px - entry_px) * _prev_partial_qty * 100.0
            _prev_partial_cost = entry_px * _prev_partial_qty * 100.0

        _total_orig_qty = close_qty_int + _prev_partial_qty
        _final_dollar = (exit_px - entry_px) * close_qty_int * 100.0
        _final_cost = entry_px * close_qty_int * 100.0
        _combined_dollar = _prev_partial_dollar + _final_dollar
        _combined_cost = _prev_partial_cost + _final_cost
        if _combined_cost > 0:
            combined_pnl_pct = _combined_dollar / _combined_cost
        _prev_partial_pct = (_prev_partial_dollar / _prev_partial_cost) if _prev_partial_cost > 0 else float(trade.get("partial_pnl_pct", 0.0) or 0.0)
        combined_note = (
            f"\n\U0001f500 **Partial leg:** `{_prev_partial_pct * 100:+.2f}%` on `{_prev_partial_qty}` contracts"
            f"\n\U0001f4ca **Net combined:** `${_combined_dollar:+.2f}` (`{combined_pnl_pct * 100:+.2f}%`) on `{_total_orig_qty}` contracts"
        )

    is_profit = combined_pnl_pct > 0
    if final_close and not is_profit:
        _record_post_loss_setup(
            trade.get("underlying"),
            trade.get("side", trade.get("signal", "").split()[-1] if trade.get("signal") else ""),
            exit_market_context,
            trade.get("entry_ignition_delta", 0),
        )
    outcome_label = "PROFIT" if is_profit else "LOSS"
    exit_type_label = "Profit" if is_profit else "Loss"
    exit_icon = "\U0001f7e2" if is_profit else "\U0001f534"
    grade = "A" if combined_pnl_pct >= 0.20 else "B" if combined_pnl_pct >= 0.10 else "C" if combined_pnl_pct >= -0.10 else "D"
    outcome_pnl_pct = combined_pnl_pct if final_close else pnl_pct
    exit_explanation = _trade_exit_explanation(
        trade, reason, entry_px, exit_px, outcome_pnl_pct, exit_market_context
    )
    outcome_reason_label = "Profit reason" if is_profit else "Loss reason"

    short_reason = str(reason or "EXIT").split("|", 1)[0].strip()
    if short_reason.startswith("THESIS INVALIDATION"):
        short_reason = "THESIS INVALIDATED"
    elif short_reason.startswith("EMERGENCY STOP LOSS"):
        short_reason = "EMERGENCY OPTION STOP"
    elif short_reason.startswith("RUNNER TRAIL HIT"):
        short_reason = "RUNNER TRAILING STOP"
    elif short_reason.startswith("RUNNER PNL GIVEBACK STOP"):
        short_reason = "RUNNER PROFIT GIVEBACK"
    elif short_reason.startswith("TIME EXIT"):
        short_reason = "TIME EXIT"
    is_partial = (not final_close and close_qty_int < current_qty)
    shown_dollar = _combined_dollar if (final_close and _combined_cost > 0) else ((exit_px - entry_px) * close_qty_int * 100.0)
    if abs(shown_dollar - int(round(shown_dollar))) < 0.005:
        net_str = f"${int(round(shown_dollar)):+d}"
    else:
        net_str = f"${shown_dollar:+.2f}"

    if is_partial:
        status_icon = "\U0001f4b0"
        net_icon = "\U0001f512" if shown_dollar >= 0 else "\U0001f534"
        line1 = f"{status_icon} **{trade['underlying']} {trade['side']}** · {outcome_pnl_pct * 100:+.2f}% · PARTIAL"
        line3 = f"{net_icon} {net_str}"
    elif outcome_pnl_pct >= 0:
        status_icon = "\u2705"
        line1 = f"{status_icon} **{trade['underlying']} {trade['side']}** · {outcome_pnl_pct * 100:+.2f}%"
        line3 = f"\U0001f4b0 {net_str}"
    else:
        status_icon = "\U0001f6d1"
        line1 = f"{status_icon} **{trade['underlying']} {trade['side']}** · {outcome_pnl_pct * 100:+.2f}%"
        line3 = f"\U0001f534 {net_str} · {short_reason}"

    exit_message = (
        f"{line1}\n"
        f"`${entry_px:.2f}` → `${exit_px:.2f}` · `{duration_min}m` · Qty `{close_qty_int}`\n"
        f"{line3}\n"
        f"Reason: {short_reason}"
    )
    exit_color = DISCORD_COLOR_CALL if is_profit else DISCORD_COLOR_PUT
    exit_sent = send_discord_bot_reply(trade.get("entry_message_id"), exit_message, color=exit_color)
    if not exit_sent:
        exit_sent = append_discord_entry_update(
            trade.get("entry_message_id"),
            exit_message,
            color=exit_color,
            webhook_url=DISCORD_WEBHOOK_LIVE_TRADES_URL,
        )
    if not exit_sent:
        log(f"[{trade['underlying']}] Exit Discord update skipped: entry message unavailable.")
    if final_close:
        chart_bytes = _render_trade_candlestick_chart(trade, exit_px, closed_at)
        if chart_bytes:
            chart_sent = send_discord_chart_reply(
                trade.get("entry_message_id"),
                chart_bytes,
                f"{trade['underlying']}_{closed_at:%Y%m%d_%H%M}_exit.png",
                f"\U0001f4c8 **{trade['contract']}** | 5-minute candles | "
                f"entry `{trade['opened_at']:%H:%M CT}` | exit `{closed_at:%H:%M CT}` | "
                f"VWAP (blue) | EMA20 (orange)",
            )
            if not chart_sent:
                log(f"[{trade['underlying']}] Exit candle chart was not sent to Discord.")

    if final_close and _prev_partial_qty > 0 and _combined_cost > 0:
        sheets_reason = _sheets_exit_reason(
            reason, combined_pnl_pct, _combined_dollar, exit_market_context
        )
    else:
        _leg_dollar = (exit_px - entry_px) * close_qty_int * 100.0
        sheets_reason = _sheets_exit_reason(
            reason, pnl_pct, _leg_dollar, exit_market_context
        )
    row = {
        "opened_at": trade["opened_at"],
        "closed_at": closed_at,
        "underlying": trade["underlying"],
        "contract":   trade["contract"],
        "signal":     trade["signal"],
        "qty":        close_qty_int,
        "entry":      trade["entry"],
        "exit":       exit_price,
        "pnl_pct":    pnl_pct * 100,
        "reason":     sheets_reason,
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

    if final_close:
        # Build an alerts/sheets row with combined P&L so Alerts + Trades sheets
        # reflect the net trade outcome, not just the final leg.
        combined_row = dict(row)
        if _prev_partial_qty > 0 and _combined_cost > 0:
            combined_row["pnl_pct"] = round(combined_pnl_pct * 100, 2)
            combined_row["_combined_pnl_dollar"] = round(_combined_dollar, 2)
        update_alert_close_to_sheets(combined_row, trade)
        log_trade_to_sheets(combined_row, trade, final_close=True)
        schedule_exit_review(
            trade, reason, entry_px, exit_px, combined_pnl_pct, closed_at, exit_market_context
        )
    else:
        log_trade_to_sheets(row, trade, final_close=False)
    perf_trade = dict(trade)
    perf_trade["qty"] = close_qty_int
    _record_trade_close_for_perf(perf_trade, exit_price, combined_pnl_pct, closed_at, final_close=final_close)

    if (not final_close) and close_qty_int < current_qty:
        partial_leg_dollar = (exit_px - entry_px) * close_qty_int * 100.0
        partial_leg_cost = entry_px * close_qty_int * 100.0
        trade["qty"] = max(0, current_qty - close_qty_int)
        trade["partial_taken"] = True
        # Store partial leg data so the final close can compute combined P&L.
        trade["partial_close_qty"] = int(trade.get("partial_close_qty", 0) or 0) + close_qty_int
        trade["partial_exit_px"] = float(exit_price)
        trade["partial_realized_dollar"] = float(trade.get("partial_realized_dollar", 0.0) or 0.0) + partial_leg_dollar
        trade["partial_realized_cost"] = float(trade.get("partial_realized_cost", 0.0) or 0.0) + partial_leg_cost
        total_partial_cost = float(trade.get("partial_realized_cost", 0.0) or 0.0)
        total_partial_dollar = float(trade.get("partial_realized_dollar", 0.0) or 0.0)
        trade["partial_pnl_pct"] = (total_partial_dollar / total_partial_cost) if total_partial_cost > 0 else pnl_pct
        log(f"[{trade['underlying']}] Partial close executed: {close_qty_int} closed, {trade['qty']} remaining.")
        return

    _open_trades.pop(trade["contract"], None)

    with _auto_retrain_lock:
        _auto_retrain_state["closed_count"] = int(_auto_retrain_state.get("closed_count", 0)) + 1

    # 30-minute cooldown before the same (symbol, side) can re-alert.
    # Prevents same-day whipsaw but allows re-entry after the lockout expires.
    alert_key = (trade["underlying"], trade.get("side", trade["signal"].split()[-1]))
    _alert_cooldowns[alert_key] = datetime.now(central) + timedelta(minutes=30)
    # Clear the alert lock so the symbol can re-alert after cooldown.
    # The ignition gate prevents immediate re-chasing — no need to block all day.
    _alerted_today["keys"].discard(alert_key)

    maybe_trigger_auto_retrain(trigger_reason=f"closed:{trade.get('underlying', 'UNKNOWN')}")

    log(f"[{trade['underlying']}] Closed {trade['contract']} ({reason}, {pnl_pct * 100:+.2f}%)")


def _refresh_option_quote_before_execution(symbol, option):
    """Re-pull a live snapshot for the selected contract right before submitting
    an order. The contract-search cache can be up to OPTION_SEARCH_CACHE_TTL_SECONDS
    old — this refresh prevents executing on a stale/since-widened quote.
    """
    if _option_client is None:
        return True, "option client unavailable, skipping refresh (fail-open)"
    contract_sym = str(option.get("contract", "") or "")
    if not contract_sym:
        return False, "no contract symbol to refresh"
    try:
        snap_req = OptionSnapshotRequest(symbol_or_symbols=contract_sym, feed=OPTIONS_FEED)
        snaps = _option_client.get_option_snapshot(snap_req)
        snap = snaps.get(contract_sym) if isinstance(snaps, dict) else snaps
        if not snap:
            return False, f"no live snapshot returned for {contract_sym}"
        q = getattr(snap, "latest_quote", None) or getattr(snap, "quote", None)
        if q is None:
            return False, f"no live quote returned for {contract_sym}"
        bid = _safe_float_num(getattr(q, "bid_price", 0.0), 0.0)
        ask = _safe_float_num(getattr(q, "ask_price", 0.0), 0.0)
        if bid <= 0.0 and ask <= 0.0:
            return False, "NO_QUOTE on execution refresh"
        mid = (bid + ask) / 2.0 if (bid + ask) > 0 else 0.01
        spread_pct = (ask - bid) / mid
        max_spread_pct = ETF_MAX_OPTION_SPREAD_PCT if symbol in ETF_SYMBOLS else STOCK_MAX_OPTION_SPREAD_PCT
        if spread_pct > max_spread_pct:
            return False, f"spread widened to {spread_pct*100:.1f}% > {max_spread_pct*100:.0f}% max on execution refresh"
        daily = getattr(snap, "daily_bar", None)
        if daily is not None:
            option["volume"] = _safe_int_num(getattr(daily, "volume", option.get("volume", 0)), option.get("volume", 0))
        option["bid"] = bid
        option["ask"] = ask
        option["spread_pct"] = spread_pct
        return True, f"refreshed: bid=${bid:.2f} ask=${ask:.2f} spread={spread_pct*100:.1f}%"
    except Exception as e:
        return False, f"quote refresh failed: {type(e).__name__}: {e}"


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
    direction_capacity_ok, direction_capacity_reason = same_direction_position_capacity_ok(symbol, side)
    if not direction_capacity_ok:
        log(f"[{symbol}] Correlated-entry guard: {direction_capacity_reason} — skipping.")
        return False
    if _trading_client is None:
        return False

    # The contract search result may be cached for up to OPTION_SEARCH_CACHE_TTL_SECONDS —
    # re-validate the live quote/spread right before committing capital.
    refresh_ok, refresh_reason = _refresh_option_quote_before_execution(symbol, option)
    if not refresh_ok:
        log(f"[{symbol}] Execution-time quote refresh failed for {option.get('contract', '')}: {refresh_reason} — skipping entry.")
        return False
    log(f"[{symbol}] Execution-time quote refresh: {refresh_reason}")

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

    if option.get("_liquidity_substitute_used"):
        substitute_mid = _safe_float_num(option.get("_liquidity_substitute_mid", 0.0), 0.0)
        slippage_vs_mid = (fill_price - substitute_mid) if substitute_mid > 0 else None
        slippage_pct = (slippage_vs_mid / substitute_mid * 100.0) if (slippage_vs_mid is not None and substitute_mid > 0) else None
        log(
            f"[{symbol}] LIQUIDITY SUBSTITUTE FILL: contract={option.get('contract', '')} "
            f"substitute_mid=${substitute_mid:.2f} entry_fill=${fill_price:.2f} "
            f"slippage_vs_mid={'unknown' if slippage_vs_mid is None else f'${slippage_vs_mid:+.2f} ({slippage_pct:+.1f}%)'}"
        )

    trade = open_trade_record(symbol, signal_label, option, score, fill_price, qty, data=data)
    _open_trades[trade["contract"]] = trade
    _record_trade_open_for_perf(datetime.now(central))

    # Persist both ALERTS and TRADES records as part of the same entry flow.
    alert_row = log_alert_to_sheets(symbol, data, option)
    if alert_row:
        trade["alerts_row"] = int(alert_row)
    log_trade_open_to_sheets(trade)

    setup = str(trade.get("setup_type", trade.get("entry_timing", "UNKNOWN")) or "UNKNOWN")
    grade = "A" if score >= 90 else "B" if score >= 80 else "C" if score >= 70 else "D"
    stop_pct = float(trade.get("stop_pct", STOP_LOSS_PCT)) * 100.0
    target_pct = float(trade.get("target_pct", PROFIT_TARGET_PCT)) * 100.0
    partial_tp_pct = float(trade.get("partial_tp_pct", PARTIAL_TP_PCT))
    target_1 = float(trade.get("partial_target", trade['entry'] * (1 + max(0.01, partial_tp_pct))))
    target_2 = float(trade.get("target", trade['entry'] * (1 + PROFIT_TARGET_PCT)))

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
    why_now_line = _why_now_line(data, trade["side"])
    entry_timing = str(trade.get("entry_timing", "UNKNOWN") or "UNKNOWN")
    entry_ignition_delta = int(trade.get("entry_ignition_delta", 0) or 0)
    entry_option_oi = int(trade.get("entry_option_oi", 0) or 0)
    underlying_entry_price = float(trade.get("underlying_entry_price", data.get("price", 0.0) or 0.0) or 0.0)

    side_suffix = "C" if trade['side'] == 'CALL' else "P"
    strike_contract = f"{strike_text}{side_suffix}"
    primary_score = int(data.get("bull_score", score) if trade['side'] == 'CALL' else data.get("bear_score", score))
    setup_compact = "PULLBACK" if "PULLBACK" in setup.upper() else ("BREAKOUT" if "BREAKOUT" in setup.upper() else setup.upper())
    trigger_raw = str((trade.get("one_minute_trigger") or data.get("one_minute_trigger") or "") or "").upper()
    if trigger_raw == "FIRST_PULLBACK":
        trigger_compact = "1M RECLAIM + VOL"
    elif trigger_raw == "BREAKOUT_RETEST":
        trigger_compact = "RETEST CONFIRMED"
    elif trigger_raw == "MOMENTUM_COMPRESSION":
        trigger_compact = "COMPRESSION BREAK"
    else:
        trigger_compact = "SNIPER CONFIRMED"

    entry_msg = send_discord(
        f"\U0001f3af **{trade['underlying']} {trade['side']}** · {expiry_mmdd} · {strike_contract}\n"
        f"\U0001f4b5 `${trade['entry']:.2f}` · Spot `${underlying_entry_price:.2f}` · Qty `{trade['qty']}`\n"
        f"\U0001f525 {'Bull' if trade['side'] == 'CALL' else 'Bear'} `{primary_score}` · {setup_compact} · {trigger_compact}\n"
        f"\U0001f3af `${target_1:.2f}` / `${target_2:.2f}` · \U0001f6e1\ufe0f `-{stop_pct:.0f}%` · `{trade['opened_at']:%H:%M CT}`",
        color=DISCORD_COLOR_CALL if trade['side'] == 'CALL' else DISCORD_COLOR_PUT,
        wait_for_response=True,
        webhook_url=DISCORD_WEBHOOK_LIVE_TRADES_URL,
    )
    try:
        msg_id = (entry_msg or {}).get("id") if isinstance(entry_msg, dict) else None
        if msg_id:
            trade["entry_message_id"] = str(msg_id)
    except Exception:
        pass

    log(f"[{symbol}] Paper trade opened: {trade['contract']} fill ${trade['entry']:.2f} "
        f"underlying ${underlying_entry_price:.2f} timing={entry_timing} "
        f"ignition={entry_ignition_delta:+d} OI={entry_option_oi:,} "
        f"target ${trade['target']:.2f} stop ${trade['stop']:.2f}")
    return True


# ---------------------------------------------------------------------------
# main loop
# ---------------------------------------------------------------------------
def run_cycle(client):
    if not market_open_now():
        log("Market closed — skipping.")
        return
    cycle_started_at = time.perf_counter()

    _reset_perf_stats_if_new_day(datetime.now(central))

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

    symbols_to_scan = _order_symbols_by_priority(symbols_to_scan)
    prefetched_bars = prefetch_bars_parallel(client, symbols_to_scan)
    symbol_eval_ms = []
    candidates = []

    for symbol in symbols_to_scan:
        symbol_started_at = time.perf_counter()
        try:
            candidate = run_symbol(client, symbol, prefetched_bars=prefetched_bars.get(symbol))
            if candidate:
                candidates.append(candidate)
        except Exception:
            log(f"[{symbol}] Cycle error:")
            traceback.print_exc()
            sys.stdout.flush()
        finally:
            elapsed_ms = (time.perf_counter() - symbol_started_at) * 1000.0
            symbol_eval_ms.append(elapsed_ms)
            if ENABLE_SCAN_TIMING_LOGS and elapsed_ms >= SCAN_SYMBOL_LOG_THRESHOLD_MS:
                log(f"[SCAN] {symbol} evaluation took {elapsed_ms:.1f}ms.")

    if TWO_PLAYBOOK_ENTRY_MODE:
        execute_ranked_candidates(candidates)

    try:
        track_open_trades()
    except Exception:
        log("track_open_trades error:")
        traceback.print_exc()
        sys.stdout.flush()

    try:
        process_due_exit_reviews()
    except Exception as e:
        log(f"Exit review check error: {e}")

    try:
        maybe_send_hourly_perf_report(datetime.now(central))
    except Exception as e:
        log(f"Hourly perf report error: {e}")

    try:
        maybe_send_morning_briefing(client, datetime.now(central))
    except Exception as e:
        log(f"Morning briefing error: {e}")

    try:
        maybe_send_midday_briefing(client, datetime.now(central))
    except Exception as e:
        log(f"Midday briefing error: {e}")

    if ENABLE_SCAN_TIMING_LOGS:
        total_ms = (time.perf_counter() - cycle_started_at) * 1000.0
        avg_ms = (sum(symbol_eval_ms) / len(symbol_eval_ms)) if symbol_eval_ms else 0.0
        log(
            f"[SCAN] Poll cycle complete: {len(symbols_to_scan)} symbol(s), "
            f"avg eval {avg_ms:.1f}ms, total {total_ms:.1f}ms."
        )


async def _on_stock_trade_tick(trade):
    """WebSocket callback: enqueue symbol for immediate evaluation."""
    symbol = str(getattr(trade, "symbol", "") or "").upper().strip()
    if not symbol:
        return
    with _ws_pending_lock:
        _ws_pending_symbols.add(symbol)


def _reset_daily_alert_state_if_needed(now_ct):
    """Mirror run_cycle day rollover behavior for websocket mode."""
    today = now_ct.date()
    if _alerted_today["date"] != today:
        _alerted_today["date"] = today
        _alerted_today["keys"] = set()


def run_websocket_cycle(client):
    """Event-driven loop: evaluate symbols on ticks instead of fixed polling cadence."""
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        raise Exception("Missing Alpaca API keys in .env")

    stream = StockDataStream(
        ALPACA_API_KEY,
        ALPACA_SECRET_KEY,
        feed=DataFeed(FEED),
        raw_data=False,
    )
    for sym in SYMBOLS:
        stream.subscribe_trades(_on_stock_trade_tick, sym)

    stream_thread = threading.Thread(target=stream.run, daemon=True, name="alpaca-stock-stream")
    stream_thread.start()
    log(f"WebSocket mode enabled. Subscribed to trade ticks for {len(SYMBOLS)} symbols (feed={FEED}).")

    next_exit_check_at = datetime.now(central)
    next_exit_review_check_at = datetime.now(central)
    next_full_scan_at = datetime.now(central)
    min_eval_gap = max(1, WS_SYMBOL_MIN_EVAL_SECONDS)
    exit_check_gap = max(1, WS_EXIT_CHECK_SECONDS)
    loop_sleep = max(0.1, WS_LOOP_SLEEP_SECONDS)
    full_scan_gap = max(10, WS_FULL_SCAN_INTERVAL_SECONDS)
    session_open_seen = market_open_now()
    log(
        "WebSocket cadence: "
        f"symbol_min_eval={min_eval_gap}s, "
        f"full_scan={full_scan_gap}s, "
        f"exit_check={exit_check_gap}s, "
        f"loop_sleep={loop_sleep}s"
    )

    while True:
        now_ct = datetime.now(central)

        is_open = market_open_now()
        if not is_open:
            if session_open_seen:
                log("Market session ended — stopping bot process (no auto-restart).")
                try:
                    stream.stop()
                except Exception:
                    pass
                return
            log("Market closed (pre-open/off-hours) — websocket loop idle.")
            time.sleep(max(5, POLL_SECONDS))
            continue
        session_open_seen = True

        _reset_daily_alert_state_if_needed(now_ct)
        _reset_perf_stats_if_new_day(now_ct)

        symbols_to_run = []
        with _ws_pending_lock:
            pending = list(_ws_pending_symbols)

        for symbol in pending:
            last_eval = _ws_last_eval_at.get(symbol)
            if last_eval is None or (now_ct - last_eval).total_seconds() >= min_eval_gap:
                symbols_to_run.append(symbol)

        # Safety net: periodic full scan catches missed stream events or reconnect gaps.
        # Include trending symbols here so websocket mode keeps parity with polling mode.
        if now_ct >= next_full_scan_at:
            full_scan_symbols = list(SYMBOLS)
            try:
                trending_symbols, _ = get_trending_symbols(client, SYMBOLS)
                for tsym in trending_symbols:
                    if tsym not in full_scan_symbols:
                        full_scan_symbols.append(tsym)
            except Exception as e:
                log(f"Trending refresh warning (websocket full scan): {e}")

            for sym in full_scan_symbols:
                if sym not in symbols_to_run:
                    symbols_to_run.append(sym)
            next_full_scan_at = now_ct + timedelta(seconds=full_scan_gap)

        symbols_to_run = _order_symbols_by_priority(symbols_to_run)
        prefetched_bars = prefetch_bars_parallel(client, symbols_to_run)
        batch_started_at = time.perf_counter()
        symbol_eval_ms = []
        candidates = []

        for symbol in symbols_to_run:
            symbol_started_at = time.perf_counter()
            try:
                candidate = run_symbol(client, symbol, prefetched_bars=prefetched_bars.get(symbol))
                if candidate:
                    candidates.append(candidate)
            except Exception:
                log(f"[{symbol}] WebSocket cycle error:")
                traceback.print_exc()
                sys.stdout.flush()
            finally:
                elapsed_ms = (time.perf_counter() - symbol_started_at) * 1000.0
                symbol_eval_ms.append(elapsed_ms)
                if ENABLE_SCAN_TIMING_LOGS and elapsed_ms >= SCAN_SYMBOL_LOG_THRESHOLD_MS:
                    log(f"[SCAN] {symbol} evaluation took {elapsed_ms:.1f}ms.")
                _ws_last_eval_at[symbol] = datetime.now(central)
                with _ws_pending_lock:
                    _ws_pending_symbols.discard(symbol)

        if TWO_PLAYBOOK_ENTRY_MODE:
            execute_ranked_candidates(candidates)

        if ENABLE_SCAN_TIMING_LOGS and symbols_to_run:
            total_ms = (time.perf_counter() - batch_started_at) * 1000.0
            avg_ms = (sum(symbol_eval_ms) / len(symbol_eval_ms)) if symbol_eval_ms else 0.0
            log(
                f"[SCAN] WebSocket batch complete: {len(symbols_to_run)} symbol(s), "
                f"avg eval {avg_ms:.1f}ms, total {total_ms:.1f}ms."
            )

        if now_ct >= next_exit_check_at:
            try:
                track_open_trades()
            except Exception:
                log("track_open_trades error:")
                traceback.print_exc()
                sys.stdout.flush()
            next_exit_check_at = datetime.now(central) + timedelta(seconds=exit_check_gap)

        if now_ct >= next_exit_review_check_at:
            try:
                process_due_exit_reviews(now_ct)
            except Exception as e:
                log(f"Exit review check error: {e}")
            next_exit_review_check_at = datetime.now(central) + timedelta(
                seconds=max(10, EXIT_REVIEW_CHECK_SECONDS)
            )

        try:
            maybe_send_hourly_perf_report(datetime.now(central))
        except Exception as e:
            log(f"Hourly perf report error: {e}")

        try:
            maybe_send_morning_briefing(client, datetime.now(central))
        except Exception as e:
            log(f"Morning briefing error: {e}")

        try:
            maybe_send_midday_briefing(client, datetime.now(central))
        except Exception as e:
            log(f"Midday briefing error: {e}")

        time.sleep(loop_sleep)


def run_symbol(client, symbol, prefetched_bars=None):
    bars = prefetched_bars if prefetched_bars is not None else fetch_bars(client, symbol)
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
        elif symbol == "QQQ":
            _qqq_vwap_cache["side"] = "bull" if data["price"] > data["vwap"] else "bear"
            _qqq_vwap_cache["updated_at"] = datetime.now(central)

        _update_symbol_opportunity_cache(symbol, data)
        _maybe_send_transition_alert(symbol, data)

    if side == "NO TRADE":
        return

    closing_block_minutes = closing_no_trade_minutes_remaining()
    if closing_block_minutes > 0:
        log(
            f"[{symbol}] Closing window filter: skipping {data['signal']} during final "
            f"{CLOSING_NO_TRADE_MINUTES}m before close ({closing_block_minutes}m remaining)."
        )
        return

    watchlist_promoted = False
    if data.get("tier") == "WATCH" and not TWO_PLAYBOOK_ENTRY_MODE:
        if EXECUTE_WATCHLIST_SIGNALS:
            watchlist_promoted = True
            log(f"[{symbol}] WATCHLIST execution enabled globally (EXECUTE_WATCHLIST_SIGNALS=1).")
        else:
            wl_ok, wl_reason = watchlist_execution_confirmed(symbol, side, data)
            if not wl_ok:
                log(
                    f"[{symbol}] Execution gate: WATCHLIST tier blocked "
                    f"(selective criteria not met: {wl_reason})."
                )
                return
            watchlist_promoted = True
            log(f"[{symbol}] WATCHLIST promoted for execution — {wl_reason}.")
    data["watchlist_promoted"] = watchlist_promoted

    if watchlist_promoted and RECALL_FIRST_MODE and not TWO_PLAYBOOK_ENTRY_MODE:
        promoted_ok, promoted_reason = promoted_entry_quality_ok(symbol, side, data)
        data["promoted_quality_ok"] = bool(promoted_ok)
        if not promoted_ok:
            log(f"[{symbol}] Promoted quality gate: blocked — {promoted_reason}.")
            return
        log(f"[{symbol}] Promoted quality gate: passed — {promoted_reason}.")
    else:
        data["promoted_quality_ok"] = False

    # Hard minimum score gate with dynamic threshold from regime + continuation quality.
    # V2: this score is evidence feeding the unified Trend score, not an automatic veto —
    # only enforced as a hard block in legacy (non-V2) mode.
    enforce_hard_gate = (
        HARD_SCORE_GATE_ENABLED
        and (not (V2_ENTRY_QUALITY_ENABLED and TWO_PLAYBOOK_ENTRY_MODE))
        and ((not NO_GATING_MODE) or HARD_SCORE_GATE_IN_NO_GATING_MODE)
    )
    if HARD_SCORE_GATE_ENABLED and V2_ENTRY_QUALITY_ENABLED and TWO_PLAYBOOK_ENTRY_MODE:
        side_score = data["bull_score"] if side == "CALL" else data["bear_score"]
        min_required_score = min(BREAKOUT_MIN_SCORE, PULLBACK_MIN_SCORE)
        if side_score < min_required_score:
            log(
                f"[{symbol}] Hard score gate (advisory under V2): {side} {side_score} < {min_required_score} "
                "— not blocking, letting unified entry quality decide."
            )
    if enforce_hard_gate:
        side_score = data["bull_score"] if side == "CALL" else data["bear_score"]
        min_required_score = (
            min(BREAKOUT_MIN_SCORE, PULLBACK_MIN_SCORE)
            if TWO_PLAYBOOK_ENTRY_MODE
            else dynamic_min_required_score(symbol, side, data)
        )
        if data.get("watchlist_promoted", False) and RECALL_FIRST_MODE:
            dominance = abs(int(data.get("bull_score", 0)) - int(data.get("bear_score", 0)))
            promoted_floor = max(50, SCORE_WATCH)
            promoted_dom_floor = max(20, SCORE_DOMINANCE)
            if side_score < promoted_floor or dominance < promoted_dom_floor:
                log(
                    f"[{symbol}] Hard score gate (promoted): {side} score/dom too weak "
                    f"({side_score}/{dominance}) < ({promoted_floor}/{promoted_dom_floor}) - skipping."
                )
                return
            log(
                f"[{symbol}] Hard score gate relaxed (watchlist promoted): {side} {side_score} "
                f"with dominance {dominance} (base requirement {min_required_score})."
            )
        elif side_score < min_required_score:
            log(
                f"[{symbol}] Hard score gate: {side} {side_score} < {min_required_score} "
                f"- skipping (requires >= {min_required_score})."
            )
            return

    # Only send Discord alerts for STRONG tier unless watchlist was selectively promoted.
    if (
        (not NO_GATING_MODE)
        and not TWO_PLAYBOOK_ENTRY_MODE
        and data["tier"] != "STRONG"
        and not data.get("watchlist_promoted", False)
    ):
        log(f"[{symbol}] {data['signal']} (BULL {data['bull_score']} / BEAR {data['bear_score']}) "
            f"\u2014 below STRONG threshold, no Discord alert.")
        return

    enforce_opening_window = (not NO_GATING_MODE) or ENFORCE_OPENING_WINDOW_IN_NO_GATING
    if enforce_opening_window:
        opening_block_minutes = opening_no_trade_minutes_remaining()
        if opening_block_minutes > 0:
            opening_ok, opening_reason = opening_window_exception_ok(symbol, side, data)
            if opening_ok:
                log(
                    f"[{symbol}] Opening volatility override: allowing {data['signal']} during first "
                    f"{OPENING_NO_TRADE_MINUTES}m after open — {opening_reason}."
                )
            else:
                log(
                    f"[{symbol}] Opening volatility filter: skipping {data['signal']} during first "
                    f"{OPENING_NO_TRADE_MINUTES}m after open ({opening_block_minutes}m remaining; {opening_reason})."
                )
                return

    # One STRONG CALL alert and one STRONG PUT alert max per (symbol, side) per day.
    # After a trade closes, a cooldown allows the same setup to re-trigger.
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
    if IGNITION_REQUIRED and not NO_GATING_MODE and not TWO_PLAYBOOK_ENTRY_MODE:
        entry_timing = _classify_entry_timing(data or {}, side)
        if side == "CALL":
            now_score = data["bull_score"]
            past_score = data["bull_5m"]
            side_dominance = data["bull_score"] - data["bear_score"]
        else:
            now_score = data["bear_score"]
            past_score = data["bear_5m"]
            side_dominance = data["bear_score"] - data["bull_score"]

        is_etf = symbol in ETF_SYMBOLS
        watchlist_promoted = bool((data or {}).get("watchlist_promoted", False))
        promoted_quality_ok = bool((data or {}).get("promoted_quality_ok", False))
        required_delta = ignition_delta_required(now_score, ignition_min_delta)
        if entry_timing in {"HIGHER_LOW_RECLAIM", "LOWER_HIGH_FAILURE"}:
            required_delta = max(3, required_delta - 6)
        elif watchlist_promoted and RECALL_FIRST_MODE and promoted_quality_ok:
            required_delta = max(4, required_delta - 8)
        elif is_etf:
            required_delta = max(8, required_delta - 5)
        elif RECALL_FIRST_MODE:
            required_delta = max(6, required_delta - 3)

        if past_score is None:
            history = score_history.get(symbol, deque())
            if len(history) >= 2:
                # Cold-start fallback: compare against the oldest sampled score since boot
                # so we can still capture fast early-session ignitions.
                _, oldest_bull, oldest_bear = history[0]
                past_score = oldest_bull if side == "CALL" else oldest_bear
                delta = now_score - past_score
                if now_score < 95 and delta < required_delta:
                    warmup_promoted_override = (
                        watchlist_promoted
                        and promoted_quality_ok
                        and RECALL_FIRST_MODE
                        and now_score >= max(SCORE_WATCH + 4, 58)
                        and side_dominance >= max(12, SCORE_DOMINANCE - 2)
                        and delta >= 0
                    )
                    if warmup_promoted_override:
                        log(
                            f"[{symbol}] Ignition warmup promoted override: {side} score {past_score} -> {now_score} "
                            f"(Δ +{delta}) accepted while lookback history is still building."
                        )
                    elif ((is_etf or RECALL_FIRST_MODE or entry_timing in {"HIGHER_LOW_RECLAIM", "LOWER_HIGH_FAILURE"}) and now_score >= 70 and delta >= max(2, required_delta - 4)):
                        log(
                            f"[{symbol}] Ignition warmup override: {side} score {past_score} \u2192 {now_score} "
                            f"(\u0394 +{delta}) accepted with relaxed delta {required_delta}."
                        )
                    else:
                        log(
                            f"[{symbol}] Ignition gate: warmup history only ({len(history)} samples), "
                            f"{side} delta +{delta} < +{required_delta} \u2014 waiting for clearer ignition."
                        )
                        return
                log(
                    f"[{symbol}] Ignition warmup: using partial history ({len(history)} samples) "
                    f"{past_score} \u2192 {now_score} (\u0394 +{delta})."
                )
            elif now_score >= 90 and not STRICT_IGNITION_NO_BYPASS:
                # Optional perfect-score cold start override when strict ignition is disabled.
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

        # ─── REMOVED: Anti-chase momentum reversal check (redundant with ignition delta gate) ───
        # Ignition gate already requires delta >= required_delta, preventing fresh moves.
        # Removing this eliminates false blocks on setups like 08:36 PUT today.

        continuation_override = False
        # Perfect-score override: score ≥ 90 fires regardless of prior level.
        if now_score >= 90 and not STRICT_IGNITION_NO_BYPASS:
            log(
                f"[{symbol}] \U0001f525 Perfect-score override: {side} score {now_score} \u2265 90 "
                "\u2014 ignition gate bypassed."
            )
        elif now_score >= 90 and STRICT_IGNITION_NO_BYPASS and delta < required_delta:
            log(
                f"[{symbol}] Ignition gate: perfect-score bypass disabled — {side} delta only +{delta} "
                f"(need +{required_delta})."
            )
            return
        elif past_score >= IGNITION_PRIOR_MAX:
            if (is_etf or RECALL_FIRST_MODE or entry_timing in {"HIGHER_LOW_RECLAIM", "LOWER_HIGH_FAILURE"}) and now_score >= 70 and delta >= max(2, required_delta - 4):
                log(
                    f"[{symbol}] Ignition continuation override: {side} remains strong "
                    f"({past_score} -> {now_score}, \u0394 {delta:+d}) with relaxed delta {required_delta}."
                )
                continuation_override = True
            elif (
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
        if (not continuation_override) and now_score < 95 and delta < required_delta:
            log(
                f"[{symbol}] Ignition gate: {side} delta only +{delta} (need +{required_delta}) "
                f"\u2014 trend not igniting, no alert."
            )
            return

        log(
            f"[{symbol}] \U0001f680 Ignition confirmed: {side} score {past_score} \u2192 {now_score} "
            f"(\u0394 +{delta}) over last {IGNITION_LOOKBACK_S}s."
        )

        # ─ Ignition confirmation buffer ─────────────────────────────────────
        # Smart buffer: trust strong ignitions (delta > threshold+3), require 
        # confirmation for marginal ones (delta just barely above threshold).
        # This catches early-stage panic moves while preventing single-spike entries.
        key = (symbol, side)
        pending = _ignition_pending.get(key)
        
        # If delta is VERY strong (threshold+3 buffer), trust immediately
        strong_ignition = delta >= (required_delta + 3)
        
        if pending is None:
            if strong_ignition:
                # Strong ignition: proceed without waiting for confirmation
                log(
                    f"[{symbol}] Ignition confirmed (strong delta): {side} score {past_score} \u2192 {now_score} "
                    f"(\u0394 +{delta} >= {required_delta + 3}) \u2014 strong move, trusting immediately."
                )
                _ignition_pending.pop(key, None)  # Clear any stale entry
            else:
                # Marginal ignition: park and wait for confirmation on next scan
                _ignition_pending[key] = {"score": now_score, "delta": int(delta), "at": datetime.now(central)}
                log(
                    f"[{symbol}] Ignition buffer: {side} score {now_score} (\u0394 +{delta}) parked — "
                    f"waiting for confirmation on next scan before entering."
                )
                return
        elif pending["score"] < now_score - 5:
            # Score improved since last scan: upgrade to strong ignition
            if strong_ignition:
                log(
                    f"[{symbol}] Ignition upgraded to strong: {side} score {pending['score']} \u2192 {now_score} "
                    f"(\u0394 +{delta}) \u2014 proceeding immediately."
                )
                _ignition_pending.pop(key, None)
            else:
                _ignition_pending[key] = {"score": now_score, "delta": int(delta), "at": datetime.now(central)}
                return
        else:
            # Confirmed: score still elevated on a second consecutive scan.
            age_s = (datetime.now(central) - pending["at"]).total_seconds()
            log(
                f"[{symbol}] Ignition buffer confirmed: {side} score still {now_score} after {age_s:.0f}s — proceeding to entry."
            )
            _ignition_pending.pop(key, None)

        # Tag the data dict so downstream filters know ignition was freshly confirmed.
        if isinstance(data, dict):
            data["ignition_confirmed"] = True
            data["ignition_delta"] = int(delta)

    # Continuation check: relaxed version (ignition delta already requires momentum confirmation)
    # Note: We're keeping this gate but making it advisory only during fresh ignitions
    if not NO_GATING_MODE and not TWO_PLAYBOOK_ENTRY_MODE and not data.get("ignition_confirmed", False):
        cont_ok, cont_reason = entry_momentum_continuation_ok(symbol, side, data)
        if not cont_ok:
            log(f"[{symbol}] Momentum continuation filter: {side} blocked — {cont_reason}.")
            return

    # ── RSI exhaustion filter ─────────────────────────────────────────────────
    # Don't enter CALLs when RSI is already overbought (move likely exhausted),
    # or PUTs when RSI is already oversold.
    if RSI_FILTER and not NO_GATING_MODE and not TWO_PLAYBOOK_ENTRY_MODE:
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

    # ── Bearish tape CALL penalty: if SPY AND QQQ are both bearish, CALLs need
    # higher conviction. No hard block — just raises the effective bar.
    if (
        BEARISH_TAPE_CALL_PENALTY_ENABLED
        and side == "CALL"
        and (not NO_GATING_MODE)
    ):
        spy_side = _spy_vwap_side()
        qqq_side = _qqq_vwap_cache.get("side")
        spy_bear_score  = int(_spy_vwap_cache.get("bear_score", 0) or 0) if hasattr(_spy_vwap_cache, "get") else 0
        qqq_bear_score  = int(_qqq_vwap_cache.get("bear_score", 0) or 0) if hasattr(_qqq_vwap_cache, "get") else 0
        both_bearish = (spy_side == "bear" and qqq_side == "bear")
        if both_bearish:
            tape_penalty = max(0, BEARISH_TAPE_CALL_SCORE_PENALTY)
            macro_penalty = max(macro_penalty, tape_penalty)
            log(
                f"[{symbol}] Bearish tape: SPY+QQQ both bearish — CALL gets "
                f"-{tape_penalty} score penalty (no veto)."
            )

    raw_score = data["bull_score"] if side == "CALL" else data["bear_score"]
    effective_score = max(0, raw_score - macro_penalty)
    data["raw_entry_score"] = raw_score
    data["macro_penalty"] = macro_penalty
    data["effective_score"] = effective_score

    # Real lower-timeframe timing: only after the 5m setup passes the hard score gate.
    if TWO_PLAYBOOK_ENTRY_MODE and not NO_GATING_MODE and ONE_MINUTE_ENTRY_ENABLED:
        try:
            bars_1m = fetch_1m_bars(client, symbol)
            trigger, trigger_reason = one_minute_entry_timing(symbol, side, bars_1m, data)
            data["one_minute_trigger"] = trigger or ""
            data["one_minute_entry_confirmed"] = bool(trigger)
            if trigger:
                log(f"[{symbol}] 1m SNIPER PASS: {trigger} — {trigger_reason}")
            else:
                log(f"[{symbol}] 1m SNIPER WAIT: {trigger_reason}")
        except Exception as e:
            data["one_minute_trigger"] = ""
            data["one_minute_entry_confirmed"] = False
            log(f"[{symbol}] 1m sniper evaluation failed: {type(e).__name__}: {e}")
    else:
        data["one_minute_trigger"] = "DISABLED" if not ONE_MINUTE_ENTRY_ENABLED else ""
        data["one_minute_entry_confirmed"] = not ONE_MINUTE_ENTRY_ENABLED

    if TWO_PLAYBOOK_ENTRY_MODE and not NO_GATING_MODE:
        playbook_ok, playbook, playbook_reason = playbook_entry_ok(side, data, symbol)
        if not playbook_ok:
            if (
                "1m sniper trigger not confirmed" in str(playbook_reason).lower()
                and SNIPER_WATCH_ALERT_COOLDOWN_MINUTES > 0
            ):
                watch_key = (symbol, side)
                watch_until = _sniper_watch_cooldowns.get(watch_key)
                watch_now = datetime.now(central)
                if watch_until is None or watch_now >= watch_until:
                    side_score = int(data.get("bull_score", 0) if side == "CALL" else data.get("bear_score", 0))
                    dominance = int((data.get("bull_score", 0) - data.get("bear_score", 0)) if side == "CALL" else (data.get("bear_score", 0) - data.get("bull_score", 0)))
                    setup_guess = classify_entry_playbook(side, data)
                    if setup_guess and side_score >= max(SCORE_SIGNAL, 60) and dominance >= max(10, SCORE_DOMINANCE - 2):
                        setup_label = "PULLBACK READY" if "PULLBACK" in setup_guess else "BREAKOUT READY"
                        wait_label = "1M RECLAIM + VOL" if "PULLBACK" in setup_guess else "RETEST CONFIRMED"
                        watch_msg = (
                            f"\U0001f440 **{symbol} {side}** · SNIPER WATCH\n"
                            f"\U0001f525 {'Bull' if side == 'CALL' else 'Bear'} `{side_score}` · {setup_label}\n"
                            f"\u23f3 Waiting: {wait_label}"
                        )
                        send_discord(
                            watch_msg,
                            color=DISCORD_COLOR_WARN,
                            webhook_url=DISCORD_WEBHOOK_LIVE_TRADES_URL,
                        )
                        _sniper_watch_cooldowns[watch_key] = watch_now + timedelta(minutes=SNIPER_WATCH_ALERT_COOLDOWN_MINUTES)
            reason_text = str(playbook_reason or "")
            tag = "WAIT" if reason_text.startswith("ARMED_BREAKOUT_") else "BLOCKED"
            log(f"[{symbol}] Playbook gate: {side} {tag} — {playbook_reason}.")
            return
        data["entry_playbook"] = playbook
        data["setup_type"] = playbook
        if playbook == "BREAKOUT" and side == "PUT" and not BREAKOUT_PUT_ENTRIES_ENABLED:
            log(f"[{symbol}] Playbook gate: BREAKOUT PUT blocked by BREAKOUT_PUT_ENTRIES_ENABLED=0.")
            return
        log(f"[{symbol}] Playbook gate: {playbook} {side} passed — {playbook_reason}.")

    if ML_GATE_ENABLED and (not NO_GATING_MODE) and not TWO_PLAYBOOK_ENTRY_MODE:
        ml_prob, ml_exp_ret, ml_source = _predict_ml_entry(symbol, side, data)
        data["ml_probability"] = ml_prob
        data["ml_expected_return"] = ml_exp_ret
        data["ml_source"] = ml_source

        if ml_prob is None:
            if ML_GATE_REQUIRE_MODEL:
                log(f"[{symbol}] ML gate: model unavailable at {ML_GATE_MODEL_PATH} — blocking entry (ML_GATE_REQUIRE_MODEL=1).")
                return
            log(f"[{symbol}] ML gate: model unavailable at {ML_GATE_MODEL_PATH} — pass-through (ML_GATE_REQUIRE_MODEL=0).")
        else:
            min_prob = max(0.0, min(1.0, ML_MIN_PROBABILITY))
            if ml_prob < min_prob:
                log(
                    f"[{symbol}] ML gate: {side} blocked — win probability {ml_prob:.1%} < {min_prob:.1%} "
                    f"(source={ml_source})."
                )
                return
            if (ml_exp_ret is not None) and (ml_exp_ret < ML_MIN_EXPECTED_RETURN):
                log(
                    f"[{symbol}] ML gate: {side} blocked — expected return {ml_exp_ret:+.3f} "
                    f"< {ML_MIN_EXPECTED_RETURN:+.3f} (source={ml_source})."
                )
                return

    # ── Anti-chase filters ───────────────────────────────────────────────────
    # Avoid buying when price is already too extended away from VWAP, and avoid
    # entering when the latest candle already flipped against our side.
    if ANTI_CHASE_FILTER and not NO_GATING_MODE and not TWO_PLAYBOOK_ENTRY_MODE:
        ext_pct = float(data.get("vwap_extension_pct", 0.0))
        if ext_pct > max_ext_from_vwap:
            log(
                f"[{symbol}] Anti-chase: {side} blocked — price is {ext_pct*100:.2f}% from VWAP "
                f"(max {max_ext_from_vwap*100:.2f}%)."
            )
            return

    if CANDLE_CONFIRMATION and not NO_GATING_MODE and not TWO_PLAYBOOK_ENTRY_MODE:
        if side == "CALL" and not data.get("bullish_candle", False):
            log(f"[{symbol}] Candle filter: CALL blocked — latest candle is not bullish.")
            return
        if side == "PUT" and not data.get("bearish_candle", False):
            log(f"[{symbol}] Candle filter: PUT blocked — latest candle is not bearish.")
            return

    # Legacy mode locks before contract lookup. Two-playbook mode locks only
    # when a shared-cycle candidate is selected for execution.
    if not NO_GATING_MODE and not TWO_PLAYBOOK_ENTRY_MODE:
        _alerted_today["keys"].add(alert_key)

    if not NO_GATING_MODE:
        fresh_ok, fresh_reason = fresh_setup_confirmed(symbol, side, data)
        if not fresh_ok:
            if ALERT_ONLY_COOLDOWN_MINUTES > 0:
                _alert_cooldowns[alert_key] = now_ct + timedelta(minutes=ALERT_ONLY_COOLDOWN_MINUTES)
            log(f"[{symbol}] Fresh-setup gate: {side} blocked — {fresh_reason}.")
            return

    log_v2_pre_contract_components(symbol, side, data)

    option = get_option_contract(
        symbol,
        side,
        data["price"],
        data=data,
        max_ext_from_vwap=max_ext_from_vwap,
    )
    if not option:
        if (not NO_GATING_MODE) and ALERT_ONLY_COOLDOWN_MINUTES > 0:
            _alert_cooldowns[alert_key] = now_ct + timedelta(minutes=ALERT_ONLY_COOLDOWN_MINUTES)
        log(f"[{symbol}] {data['signal']} setup detected, but no valid 1DTE+ option found — skipping (real trades only).")
        return

    if str((option or {}).get("_selection_mode", "")).upper() == "RELAXED_FALLBACK":
        fallback_reason = str((option or {}).get("_entry_precheck_reason", "entry precheck failed") or "entry precheck failed")
        if (not NO_GATING_MODE) and ALERT_ONLY_COOLDOWN_MINUTES > 0:
            _alert_cooldowns[alert_key] = now_ct + timedelta(minutes=ALERT_ONLY_COOLDOWN_MINUTES)
        _alerted_today["keys"].discard(alert_key)
        log(
            f"[{symbol}] Relaxed fallback selected {option['contract']}; watchlist-only Discord alert suppressed, "
            f"execution skipped ({fallback_reason})."
        )
        return

    entry_ok, entry_reason = entry_contract_quality_ok(symbol, side, data, option, max_ext_from_vwap)
    if not entry_ok:
        if (not NO_GATING_MODE) and ALERT_ONLY_COOLDOWN_MINUTES > 0:
            _alert_cooldowns[alert_key] = now_ct + timedelta(minutes=ALERT_ONLY_COOLDOWN_MINUTES)
        _alerted_today["keys"].discard(alert_key)
        log(f"[{symbol}] Entry quality checklist blocked {side} — {entry_reason}.")
        return

    if V2_ENTRY_QUALITY_ENABLED and not NO_GATING_MODE:
        quality_ok, quality_score, quality_detail = unified_entry_quality_v2(symbol, side, data, option)
        data["entry_quality_v2_score"] = quality_score
        legacy_ok, legacy_detail = _legacy_shadow_verdict(symbol, side, data, option)
        _log_entry_attribution(symbol, side, data, quality_ok, quality_score, legacy_ok, legacy_detail)
        if not quality_ok:
            if ALERT_ONLY_COOLDOWN_MINUTES > 0:
                _alert_cooldowns[alert_key] = now_ct + timedelta(minutes=ALERT_ONLY_COOLDOWN_MINUTES)
            _alerted_today["keys"].discard(alert_key)
            log(f"[{symbol}] Entry quality V2 gate: {side} blocked — {quality_detail}.")
            return
        log(f"[{symbol}] Entry quality V2: {side} CONFIRMED — {quality_score:.1f}/100 ({quality_detail}).")
    elif ENTRY_CONFLUENCE_ENABLED and not NO_GATING_MODE:
        confluence_ok, confluence_points, confluence_detail = classify_entry_confluence(symbol, side, data, option)
        data["entry_confluence_points"] = confluence_points
        if not confluence_ok:
            if ALERT_ONLY_COOLDOWN_MINUTES > 0:
                _alert_cooldowns[alert_key] = now_ct + timedelta(minutes=ALERT_ONLY_COOLDOWN_MINUTES)
            _alerted_today["keys"].discard(alert_key)
            log(f"[{symbol}] Entry confluence gate: {side} blocked — {confluence_detail}.")
            return
        log(f"[{symbol}] Entry confluence: {side} CONFIRMED — {confluence_points:.1f}/12 ({confluence_detail}).")

    if TWO_PLAYBOOK_ENTRY_MODE:
        return {
            "symbol": symbol,
            "side": side,
            "data": data,
            "option": option,
            "playbook": data.get("entry_playbook", "UNKNOWN"),
            "alert_key": alert_key,
            "one_minute_trigger": data.get("one_minute_trigger", ""),
        }

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
    ml_prob = data.get("ml_probability")
    ml_exp_ret = data.get("ml_expected_return")
    ml_source = str(data.get("ml_source", "disabled") or "disabled")
    if ml_prob is None:
        ml_block = "ML gate not active for this setup"
    else:
        prob_txt = f"{(float(ml_prob) * 100.0):.1f}%"
        exp_txt = "n/a" if ml_exp_ret is None else f"{float(ml_exp_ret):+.3f}"
        ml_block = f"Prob: `{prob_txt}` | Expected Return: `{exp_txt}` | Source: `{ml_source}`"

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

**ML Overlay**
{ml_block}

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

    if AUTO_RETRAIN_MODEL_ENABLED:
        log(
            "Auto-retrain enabled: "
            f"source={AUTO_RETRAIN_SOURCE}, "
            f"min_new_closed={max(1, AUTO_RETRAIN_MIN_NEW_CLOSED_TRADES)}, "
            f"cooldown={max(1, AUTO_RETRAIN_COOLDOWN_MINUTES)}m, "
            f"min_samples={max(5, AUTO_RETRAIN_MIN_SAMPLES)}, "
            f"output={AUTO_RETRAIN_OUTPUT_PATH}"
        )

    init_google_sheets()

    if RECOVER_OPEN_POSITIONS:
        sync_open_trades_from_alpaca()

    log(
        f"Options Alert Bot started. Symbols={','.join(SYMBOLS)} "
        f"Mode=websocket (tick-triggered). Feed={FEED}."
    )
    try:
        run_websocket_cycle(client)
    except Exception:
        log("Fatal WebSocket loop error — exiting (no auto-restart).")
        traceback.print_exc()
        sys.stdout.flush()
        raise


if __name__ == "__main__":
    main()
