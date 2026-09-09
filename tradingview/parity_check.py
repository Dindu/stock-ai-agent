"""Pine-vs-Python parity checker for ULTI-6 (see engine/ulti_python.py).

Fetches recent Alpaca bars at the base 5m timeframe PLUS the six higher/lower
timeframes SMC votes on (1m/15m/30m/1H/4H/D), runs the full Python ULTI engine
(simulate()) with real multi-timeframe trend voting, and compares its
ENTRY/EXIT events against the TradingView webhook's persisted state
(tradingview/state/actionable_signals.jsonl). Also prints a component-level
table (BB/SMC/Poki/PAT/score/veto per bar) so a mismatch can be attributed to
one specific section instead of the whole strategy.

NO EXECUTION HAPPENS HERE. This is a read-only comparison tool. Do not wire its
output into order placement until a full session of parity has been reviewed
against the real TradingView chart, including the component-level table and
the two documented calculation uncertainties (SMC MTF VWAP anchor, SAR seed) —
see engine/ulti_python.py's module docstring.

Usage:
    python3 -m tradingview.parity_check --symbols AMD,MU,IBM --minutes 240
    python3 -m tradingview.parity_check --symbols AMD --minutes 120 --components --component-rows 30
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import DataFeed

from engine.ulti_python import simulate, align_mtf_trends

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
FEED = os.getenv("ALPACA_FEED", "iex").lower()
ACTIONABLE_QUEUE_FILE = Path(__file__).parent / "state" / "actionable_signals.jsonl"
MATCH_TOLERANCE_SECONDS = 60

# Every timeframe SMC can vote on (smc_higher_tf/smc_lower_tf/smc_restrict_tf
# default to "5M" == the base chart timeframe; the others are fetched so a
# custom config can use them without further code changes).
_MTF_REQUESTS = {
    "1M": TimeFrame(1, TimeFrameUnit.Minute),
    "5M": TimeFrame(5, TimeFrameUnit.Minute),
    "15M": TimeFrame(15, TimeFrameUnit.Minute),
    "30M": TimeFrame(30, TimeFrameUnit.Minute),
    "1H": TimeFrame(1, TimeFrameUnit.Hour),
    "4H": TimeFrame(4, TimeFrameUnit.Hour),
    "D": TimeFrame(1, TimeFrameUnit.Day),
}


def fetch_bars(client, symbol, timeframe, minutes):
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=minutes * 3 + 60)  # buffer for off-hours/weekends
    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=timeframe,
        start=start,
        end=end,
        feed=DataFeed(FEED),
    )
    df = client.get_stock_bars(req).df
    if df is None or df.empty:
        return df
    if hasattr(df.index, "nlevels") and df.index.nlevels > 1:
        df = df.xs(symbol, level=0)
    return df.dropna()


def fetch_multi_timeframe_bars(client, symbol, minutes):
    """Fetch each SMC-relevant timeframe's own native bars from Alpaca (not
    resampled from 5m — Alpaca's own aggregation must match what Pine's
    request.security() would see)."""
    out = {}
    for label, tf in _MTF_REQUESTS.items():
        # Higher timeframes need proportionally more lookback to have enough
        # bars for their own EMA20/VWAP warm-up.
        multiplier = {"1M": 1, "5M": 1, "15M": 3, "30M": 6, "1H": 12, "4H": 48, "D": 300}[label]
        out[label] = fetch_bars(client, symbol, tf, minutes * multiplier)
    return out


def load_actionable_signals(symbol):
    if not ACTIONABLE_QUEUE_FILE.exists():
        return []
    out = []
    with ACTIONABLE_QUEUE_FILE.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                sig = json.loads(line)
            except Exception:
                continue
            if str(sig.get("symbol", "")).upper() == symbol.upper():
                out.append(sig)
    return out


def _find_match(py_time, py_side, tv_candidates):
    py_ts = py_time.to_pydatetime() if hasattr(py_time, "to_pydatetime") else py_time
    if py_ts.tzinfo is None:
        py_ts = py_ts.replace(tzinfo=timezone.utc)
    for tv in tv_candidates:
        if str(tv.get("side", "")).upper() != py_side:
            continue
        tv_raw = str(tv.get("bar_time", "")).replace("Z", "+00:00")
        try:
            tv_ts = datetime.fromisoformat(tv_raw)
        except Exception:
            continue
        if abs((tv_ts - py_ts).total_seconds()) <= MATCH_TOLERANCE_SECONDS:
            return tv
    return None


def print_component_table(diag, rows):
    """Component-level diagnostic table — attributes a mismatch to one section."""
    cols = ["time", "close", "bb_long_signal", "bb_short_signal", "smc_bull", "smc_bear",
            "poki_bull", "poki_bear", "pat_bull", "pat_bear", "vol_bull", "vol_bear",
            "bull_score", "bear_score", "veto_blocked_long", "veto_blocked_short", "lifecycle"]
    view = diag[cols].tail(rows).copy()
    for c in ("bb_long_signal", "bb_short_signal", "smc_bull", "smc_bear", "poki_bull",
              "poki_bear", "pat_bull", "pat_bear", "vol_bull", "vol_bear",
              "veto_blocked_long", "veto_blocked_short"):
        view[c] = view[c].map(lambda v: "Y" if v else ".")
    print(view.to_string(index=False))


def compare_symbol(client, symbol, minutes, show_components, component_rows):
    df = fetch_bars(client, symbol, TimeFrame(5, TimeFrameUnit.Minute), minutes)
    if df is None or len(df) < 55:
        print(f"[{symbol}] not enough 5m bars ({0 if df is None else len(df)}/55) — skipping.")
        return

    mtf_bars = fetch_multi_timeframe_bars(client, symbol, minutes)
    mtf_trends = align_mtf_trends(df, mtf_bars, base_tf_label="5M")
    fetched = {k: (0 if v is None else len(v)) for k, v in mtf_bars.items()}
    print(f"[{symbol}] MTF bars fetched: {fetched}")

    py_events, diag = simulate(df, mtf_trends=mtf_trends, diagnostics=True)
    py_entries = [e for e in py_events if e["event"] == "ENTRY"]
    py_exits = [e for e in py_events if e["event"] == "EXIT"]

    tv_signals = load_actionable_signals(symbol)
    tv_entries = [s for s in tv_signals if str(s.get("event", "")).upper() == "ENTRY"]
    tv_exits = [s for s in tv_signals if str(s.get("event", "")).upper() == "EXIT"]

    print(f"[{symbol}] {len(df)} bars -> Python: {len(py_entries)} ENTRY, {len(py_exits)} EXIT "
          f"| TradingView: {len(tv_entries)} ENTRY, {len(tv_exits)} EXIT recorded.")

    if show_components:
        print(f"[{symbol}] component-level table (last {component_rows} bars):")
        print_component_table(diag, component_rows)

    for label, py_list, tv_list in (("ENTRY", py_entries, tv_entries), ("EXIT", py_exits, tv_exits)):
        for ev in py_list:
            matched = _find_match(ev["time"], ev["side"], tv_list)
            if matched:
                tv_price = matched.get("underlying_price")
                drift = abs(float(tv_price) - ev["price"]) / ev["price"] if tv_price else None
                print(f"[{symbol}] {label} {ev['time']} {ev['side']} @ {ev['price']:.2f} "
                      f"({ev['reason_code']}) -> MATCH (TV @ {tv_price}, "
                      f"drift={'n/a' if drift is None else f'{drift*100:.2f}%'}, "
                      f"signal_id={matched.get('signal_id')})")
            else:
                print(f"[{symbol}] {label} {ev['time']} {ev['side']} @ {ev['price']:.2f} "
                      f"({ev['reason_code']}) -> NO TV SIGNAL FOUND "
                      f"(either TV alert didn't fire, webhook missed it, or a component still differs)")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default="AMD", help="Comma-separated symbol list")
    parser.add_argument("--minutes", type=int, default=240, help="Lookback window in minutes")
    parser.add_argument("--components", action="store_true", help="Print the per-bar component-level diagnostic table")
    parser.add_argument("--component-rows", type=int, default=20, help="How many recent bars to show in the component table")
    args = parser.parse_args()

    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        raise SystemExit("Missing ALPACA_API_KEY/ALPACA_SECRET_KEY in environment.")

    client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    for symbol in symbols:
        try:
            compare_symbol(client, symbol, args.minutes, args.components, args.component_rows)
        except Exception as e:
            print(f"[{symbol}] parity check failed: {e}")


if __name__ == "__main__":
    main()


