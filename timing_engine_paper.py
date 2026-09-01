#!/usr/bin/env python3
"""Paper-only timing engine for live validation.

This is intentionally isolated from production trading logic.
It logs a parallel Timing-V1 decision alongside the current bot's decision
without sending real orders or modifying the live strategy.

Architecture:
    Direction Engine -> bullish/bearish
    Timing Engine -> NO_SETUP / SETUP_FORMING / RESET / RECLAIM / CONFIRMED /
        ENTRY_WINDOW / CHASED / INVALID
    Paper execution + telemetry

This file is meant to be safe to run in a monitoring window while the bot
continues operating normally.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import pytz

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import spy_options_poll_bot as bot

CENTRAL = pytz.timezone("America/New_York")


@dataclass
class TimingDecision:
    symbol: str
    side: str
    state: str
    would_enter: bool
    entry_price: Optional[float]
    vwap: Optional[float]
    ema9: Optional[float]
    ema20: Optional[float]
    atr14: Optional[float]
    pullback_depth: Optional[float]
    trigger_volume_ratio: Optional[float]
    bars_since_reclaim: Optional[int]
    first_touch_outcome: Optional[str]
    first_touch_exit_price: Optional[float]
    first_touch_net_return: Optional[float]
    reason: str
    current_bot_would_enter: bool
    current_bot_reason: str
    ts: datetime

    def as_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "state": self.state,
            "would_enter": self.would_enter,
            "entry_price": self.entry_price,
            "vwap": self.vwap,
            "ema9": self.ema9,
            "ema20": self.ema20,
            "atr14": self.atr14,
            "pullback_depth": self.pullback_depth,
            "trigger_volume_ratio": self.trigger_volume_ratio,
            "bars_since_reclaim": self.bars_since_reclaim,
            "first_touch_outcome": self.first_touch_outcome,
            "first_touch_exit_price": self.first_touch_exit_price,
            "first_touch_net_return": self.first_touch_net_return,
            "reason": self.reason,
            "current_bot_would_enter": self.current_bot_would_enter,
            "current_bot_reason": self.current_bot_reason,
            "ts": self.ts.isoformat(),
        }


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _calc_vwap(session_df: pd.DataFrame) -> pd.Series:
    df = session_df.copy()
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    vwap = pd.Series(float("nan"), index=df.index, dtype=float)
    session_mask = pd.Series(False, index=df.index)
    if isinstance(df.index, pd.DatetimeIndex):
        session_dates = df.index.tz_convert(CENTRAL).date if hasattr(df.index, "tz") else df.index.date
        # Build per-day session mask by date so VWAP resets each session.
        session_dates = pd.Series(df.index, index=df.index)
        if hasattr(df.index, "tz"):
            session_dates = pd.to_datetime(session_dates).dt.tz_convert(CENTRAL).dt.date
        else:
            session_dates = pd.to_datetime(session_dates).dt.date
        for day in session_dates.unique():
            idx = df.index[session_dates == day]
            session_mask.loc[idx] = True
    if session_mask.any():
        tv = typical[session_mask] * df.loc[session_mask, "volume"]
        vwap[session_mask] = tv.cumsum() / df.loc[session_mask, "volume"].cumsum()
    return vwap


def _calc_indicators_1m(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy().sort_index()
    out["typical_price"] = (out["high"] + out["low"] + out["close"]) / 3.0
    out["VWAP"] = _calc_vwap(out)
    out["EMA9"] = out["close"].ewm(span=9, adjust=False).mean()
    out["EMA20"] = out["close"].ewm(span=20, adjust=False).mean()
    out["ATR14"] = (
        out["high"].combine(out["close"].shift(1), max).combine(out["low"].combine(out["close"].shift(1), min), lambda a, b: max(a, b))
    )
    # Explicit ATR fallback using the standard high-low-close delta computation for
    # a minute-level research-only timing implementation.
    tr = pd.concat([
        (out["high"] - out["low"]).abs(),
        (out["high"] - out["close"].shift(1)).abs(),
        (out["low"] - out["close"].shift(1)).abs(),
    ], axis=1).max(axis=1)
    out["ATR14"] = tr.ewm(span=14, adjust=False).mean()
    out["VOL_AVG20"] = out["volume"].rolling(20, min_periods=1).mean()
    out["VOL_RATIO"] = out["volume"] / out["VOL_AVG20"].replace(0, pd.NA)
    out["VOL_RATIO"] = out["VOL_RATIO"].fillna(1.0)
    return out


def _direction_engine(symbol: str, df_5m: pd.DataFrame) -> Tuple[str, Optional[Dict[str, Any]]]:
    if df_5m is None or len(df_5m) < 55:
        return "NO_SETUP", None
    try:
        side, data = bot.analyze(df_5m, None, symbol)
    except Exception:
        return "NO_SETUP", None
    if side not in {"CALL", "PUT"}:
        return "NO_SETUP", None
    return side, data


def _classify_phase(side: str, last_1m: pd.Series, recent_1m: pd.DataFrame) -> Tuple[str, Dict[str, Any]]:
    price = _safe_float(last_1m["close"]) if "close" in last_1m else 0.0
    vwap = _safe_float(last_1m["VWAP"]) if "VWAP" in last_1m else price
    ema9 = _safe_float(last_1m["EMA9"]) if "EMA9" in last_1m else price
    ema20 = _safe_float(last_1m["EMA20"]) if "EMA20" in last_1m else price
    atr14 = _safe_float(last_1m["ATR14"]) if "ATR14" in last_1m else 0.0
    vol_ratio = _safe_float(last_1m["VOL_RATIO"]) if "VOL_RATIO" in last_1m else 1.0
    pullback_depth = abs(price - vwap) / vwap if vwap else 0.0
    ext_from_vwap = abs(price - vwap) / max(vwap, 1.0)
    prev_close = _safe_float(recent_1m["close"].iloc[-2]) if len(recent_1m) >= 2 else price
    prev_low = _safe_float(recent_1m["low"].iloc[-2]) if len(recent_1m) >= 2 else price
    prev_high = _safe_float(recent_1m["high"].iloc[-2]) if len(recent_1m) >= 2 else price
    max_allowed_ext = max(0.012, (atr14 / max(price, 1.0)) * 2.5)

    if side == "CALL":
        bullish_context = price > vwap and price > ema20 and price > ema9
        reclaim = (price > vwap and price > ema20 and price > ema9 and prev_close <= max(vwap, ema20))
        reset = (price > vwap and price > ema20 and abs(price - vwap) <= max(0.004, atr14 / max(price, 1.0) * 3.0))
        if not bullish_context and price <= vwap:
            return "NO_SETUP", {"pullback_depth": pullback_depth, "vol_ratio": vol_ratio, "ext_from_vwap": ext_from_vwap, "reclaim": reclaim, "reset": reset}
        if ext_from_vwap > max_allowed_ext:
            return "CHASED", {"pullback_depth": pullback_depth, "vol_ratio": vol_ratio, "ext_from_vwap": ext_from_vwap, "reclaim": reclaim, "reset": reset}
        if reclaim and vol_ratio > 1.0 and price > ema9:
            return "RECLAIM", {"pullback_depth": pullback_depth, "vol_ratio": vol_ratio, "ext_from_vwap": ext_from_vwap, "reclaim": reclaim, "reset": reset}
        if reset and vol_ratio >= 1.0:
            return "RESET", {"pullback_depth": pullback_depth, "vol_ratio": vol_ratio, "ext_from_vwap": ext_from_vwap, "reclaim": reclaim, "reset": reset}
        if bullish_context and pullback_depth > 0.0:
            return "SETUP_FORMING", {"pullback_depth": pullback_depth, "vol_ratio": vol_ratio, "ext_from_vwap": ext_from_vwap, "reclaim": reclaim, "reset": reset}
        if price > vwap and price > ema20 and price > ema9:
            return "CONFIRMED", {"pullback_depth": pullback_depth, "vol_ratio": vol_ratio, "ext_from_vwap": ext_from_vwap, "reclaim": reclaim, "reset": reset}
        return "NO_SETUP", {"pullback_depth": pullback_depth, "vol_ratio": vol_ratio, "ext_from_vwap": ext_from_vwap, "reclaim": reclaim, "reset": reset}

    if side == "PUT":
        bearish_context = price < vwap and price < ema20 and price < ema9
        reclaim = (price < vwap and price < ema20 and price < ema9 and prev_close >= min(vwap, ema20))
        reset = (price < vwap and price < ema20 and abs(price - vwap) <= max(0.004, atr14 / max(price, 1.0) * 3.0))
        if not bearish_context and price >= vwap:
            return "NO_SETUP", {"pullback_depth": pullback_depth, "vol_ratio": vol_ratio, "ext_from_vwap": ext_from_vwap, "reclaim": reclaim, "reset": reset}
        if ext_from_vwap > max_allowed_ext:
            return "CHASED", {"pullback_depth": pullback_depth, "vol_ratio": vol_ratio, "ext_from_vwap": ext_from_vwap, "reclaim": reclaim, "reset": reset}
        if reclaim and vol_ratio > 1.0 and price < ema9:
            return "RECLAIM", {"pullback_depth": pullback_depth, "vol_ratio": vol_ratio, "ext_from_vwap": ext_from_vwap, "reclaim": reclaim, "reset": reset}
        if reset and vol_ratio >= 1.0:
            return "RESET", {"pullback_depth": pullback_depth, "vol_ratio": vol_ratio, "ext_from_vwap": ext_from_vwap, "reclaim": reclaim, "reset": reset}
        if bearish_context and pullback_depth > 0.0:
            return "SETUP_FORMING", {"pullback_depth": pullback_depth, "vol_ratio": vol_ratio, "ext_from_vwap": ext_from_vwap, "reclaim": reclaim, "reset": reset}
        if price < vwap and price < ema20 and price < ema9:
            return "CONFIRMED", {"pullback_depth": pullback_depth, "vol_ratio": vol_ratio, "ext_from_vwap": ext_from_vwap, "reclaim": reclaim, "reset": reset}
        return "NO_SETUP", {"pullback_depth": pullback_depth, "vol_ratio": vol_ratio, "ext_from_vwap": ext_from_vwap, "reclaim": reclaim, "reset": reset}

    return "INVALID", {"pullback_depth": pullback_depth, "vol_ratio": vol_ratio, "ext_from_vwap": ext_from_vwap, "reclaim": False, "reset": False}


def _current_bot_decision(symbol: str, side: str, data: Optional[Dict[str, Any]]) -> Tuple[bool, str]:
    if not data:
        return False, "no data"
    try:
        ok, reason = bot.playbook_entry_ok(side, data)
        if ok:
            return True, reason
    except Exception:
        pass
    try:
        ok, reason = bot.one_minute_entry_timing(symbol, side, pd.DataFrame(), data)
        if ok:
            return True, reason
    except Exception:
        pass
    return False, "directional but failed gating"


def _first_touch_result(entry_price: float, side: str, window: pd.Series, target_pct: float, stop_pct: float) -> Dict[str, Any]:
    entry = float(entry_price)
    if side == "CALL":
        target = entry * (1.0 + target_pct)
        stop = entry * (1.0 - stop_pct)
        target_hit_idx = None
        stop_hit_idx = None
        for idx, price in enumerate(window):
            if target_hit_idx is None and float(price) >= target:
                target_hit_idx = idx
            if stop_hit_idx is None and float(price) <= stop:
                stop_hit_idx = idx
            if target_hit_idx is not None and stop_hit_idx is not None:
                break
        if target_hit_idx is not None and (stop_hit_idx is None or target_hit_idx < stop_hit_idx):
            return {"outcome": "TARGET", "exit_price": target, "hit_target": True, "hit_stop": False, "net_return": target_pct}
        if stop_hit_idx is not None and (target_hit_idx is None or stop_hit_idx < target_hit_idx):
            return {"outcome": "STOP", "exit_price": stop, "hit_target": False, "hit_stop": True, "net_return": -stop_pct}
        end = float(window.iloc[-1])
        return {"outcome": "HORIZON", "exit_price": end, "hit_target": False, "hit_stop": False, "net_return": (end - entry) / entry}

    target = entry * (1.0 - target_pct)
    stop = entry * (1.0 + stop_pct)
    target_hit_idx = None
    stop_hit_idx = None
    for idx, price in enumerate(window):
        if target_hit_idx is None and float(price) <= target:
            target_hit_idx = idx
        if stop_hit_idx is None and float(price) >= stop:
            stop_hit_idx = idx
        if target_hit_idx is not None and stop_hit_idx is not None:
            break
    if target_hit_idx is not None and (stop_hit_idx is None or target_hit_idx < stop_hit_idx):
        return {"outcome": "TARGET", "exit_price": target, "hit_target": True, "hit_stop": False, "net_return": target_pct}
    if stop_hit_idx is not None and (target_hit_idx is None or stop_hit_idx < target_hit_idx):
        return {"outcome": "STOP", "exit_price": stop, "hit_target": False, "hit_stop": True, "net_return": -stop_pct}
    end = float(window.iloc[-1])
    return {"outcome": "HORIZON", "exit_price": end, "hit_target": False, "hit_stop": False, "net_return": (entry - end) / entry}


def _fetch_live_bars(symbol: str, minutes: int = 120) -> pd.DataFrame:
    load_dotenv = __import__("dotenv").load_dotenv
    load_dotenv()
    key = os.getenv("ALPACA_API_KEY")
    secret = os.getenv("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise RuntimeError("ALPACA_API_KEY and ALPACA_SECRET_KEY are required for live paper validation.")
    client = bot.StockHistoricalDataClient(key, secret)
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=minutes)
    request = bot.StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=bot.TimeFrame(1, bot.TimeFrameUnit.Minute),
        start=start,
        end=end,
        feed=bot.DataFeed(os.getenv("ALPACA_FEED", "iex").lower()),
    )
    df = client.get_stock_bars(request).df
    if df is None or df.empty:
        raise RuntimeError(f"No live 1m bars available for {symbol}.")
    if isinstance(df.index, pd.MultiIndex):
        df = df.xs(symbol, level=0)
    return df.sort_index()


def _paper_poll(symbol: str, minutes_back: int = 180) -> TimingDecision:
    bars_1m = _fetch_live_bars(symbol, minutes_back)
    if bars_1m is None or bars_1m.empty:
        raise RuntimeError(f"No 1m bars found for {symbol}.")
    bars_1m = _calc_indicators_1m(bars_1m)
    last = bars_1m.iloc[-1]
    price = _safe_float(last["close"])
    side, data = _direction_engine(symbol, bars_1m.resample("5min").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna())
    if side not in {"CALL", "PUT"}:
        return TimingDecision(
            symbol=symbol,
            side="NO_SETUP",
            state="NO_SETUP",
            would_enter=False,
            entry_price=None,
            vwap=None,
            ema9=None,
            ema20=None,
            atr14=None,
            pullback_depth=None,
            trigger_volume_ratio=None,
            bars_since_reclaim=None,
            first_touch_outcome=None,
            first_touch_exit_price=None,
            first_touch_net_return=None,
            reason="direction engine: no side",
            current_bot_would_enter=False,
            current_bot_reason="no direction",
            ts=datetime.now(CENTRAL),
        )

    phase, features = _classify_phase(side, last, bars_1m.iloc[-20:])
    current_bot_would_enter, current_bot_reason = _current_bot_decision(symbol, side, data)
    vwap = _safe_float(last["VWAP"]) if "VWAP" in last else price
    ema9 = _safe_float(last["EMA9"]) if "EMA9" in last else price
    ema20 = _safe_float(last["EMA20"]) if "EMA20" in last else price
    atr14 = _safe_float(last["ATR14"]) if "ATR14" in last else 0.0
    volume_ratio = _safe_float(last["VOL_RATIO"]) if "VOL_RATIO" in last else 1.0
    pullback_depth = _safe_float(features.get("pullback_depth", 0.0))
    trigger_volume_ratio = _safe_float(volume_ratio)
    bars_since_reclaim = 0
    allow_entry = phase in {"RECLAIM", "CONFIRMED"} and phase != "CHASED"
    state = "ENTRY_WINDOW" if allow_entry else phase
    entry_price = price if allow_entry else None
    first_touch_result = None
    if entry_price is not None:
        target_pct = 0.02
        stop_pct = 0.01
        window = bars_1m.iloc[-90:]["close"] if len(bars_1m) >= 90 else bars_1m["close"]
        first_touch_result = _first_touch_result(entry_price, side, window, target_pct, stop_pct)
    reason = f"{state}: {json.dumps(features, sort_keys=True)}"
    return TimingDecision(
        symbol=symbol,
        side=side,
        state=state,
        would_enter=allow_entry,
        entry_price=entry_price,
        vwap=vwap,
        ema9=ema9,
        ema20=ema20,
        atr14=atr14,
        pullback_depth=pullback_depth,
        trigger_volume_ratio=trigger_volume_ratio,
        bars_since_reclaim=bars_since_reclaim,
        first_touch_outcome=None if first_touch_result is None else first_touch_result.get("outcome"),
        first_touch_exit_price=None if first_touch_result is None else first_touch_result.get("exit_price"),
        first_touch_net_return=None if first_touch_result is None else first_touch_result.get("net_return"),
        reason=reason,
        current_bot_would_enter=current_bot_would_enter,
        current_bot_reason=current_bot_reason,
        ts=datetime.now(CENTRAL),
    )


def main():
    parser = argparse.ArgumentParser(description="Paper-only Timing Engine V1")
    parser.add_argument("--symbol", default="QQQ")
    parser.add_argument("--minutes", type=int, default=180)
    parser.add_argument("--json", action="store_true", help="Print JSON telemetry instead of a compact log.")
    parser.add_argument("--telemetry-file", default="output/timing_engine_paper.jsonl", help="Append JSONL telemetry lines for live-paper comparisons.")
    args = parser.parse_args()

    try:
        decision = _paper_poll(args.symbol.upper(), args.minutes)
    except Exception as exc:
        print(f"TIMING_PAPER_ERROR: {exc}")
        return 1

    payload = decision.as_dict()
    payload["current_bot"] = {
        "would_enter": payload["current_bot_would_enter"],
        "reason": payload["current_bot_reason"],
    }
    payload["timing_v1"] = {
        "would_enter": payload["would_enter"],
        "state": payload["state"],
        "entry_price": payload["entry_price"],
        "reason": payload["reason"],
    }

    os.makedirs(os.path.dirname(args.telemetry_file) or ".", exist_ok=True)
    with open(args.telemetry_file, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"[{payload['ts']}] {payload['symbol']} {payload['side']} | state={payload['state']} | current_bot={payload['current_bot_would_enter']} ({payload['current_bot_reason']}) | timing_v1={payload['would_enter']} | entry={payload['entry_price']} | vwap={payload['vwap']} | ema9={payload['ema9']} | ema20={payload['ema20']} | atr14={payload['atr14']} | pullback_depth={payload['pullback_depth']} | vol_ratio={payload['trigger_volume_ratio']} | first_touch={payload['first_touch_outcome']}@{payload['first_touch_exit_price']} | reason={payload['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
