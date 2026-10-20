"""Execution-only freshness checks for completed-bar strategy signals."""

from datetime import datetime, timezone

import pandas as pd


def signal_execution_freshness(
    decision,
    current_price,
    quote_time,
    now=None,
    max_age_seconds=60,
    max_move_atr=0.25,
    max_quote_age_seconds=30,
):
    """Return an execution skip reason and measurements without altering a signal."""
    now_stamp = pd.Timestamp(now or datetime.now(timezone.utc))
    now_stamp = now_stamp.tz_localize("UTC") if now_stamp.tzinfo is None else now_stamp.tz_convert("UTC")
    candle_open = pd.Timestamp(decision["entry_bar"])
    candle_open = candle_open.tz_localize("UTC") if candle_open.tzinfo is None else candle_open.tz_convert("UTC")
    candle_close = candle_open + pd.Timedelta(minutes=5)
    age_seconds = (now_stamp - candle_close).total_seconds()
    metrics = {"candle_close_time": candle_close.isoformat(), "signal_age_seconds": age_seconds, "move_atr": None}

    if age_seconds < 0:
        return "SIGNAL_CANDLE_NOT_CLOSED", metrics
    if age_seconds > float(max_age_seconds):
        return "EXECUTION_SKIPPED_STALE", metrics
    if current_price is None or float(current_price) <= 0:
        return "CURRENT_PRICE_UNAVAILABLE", metrics
    if quote_time is None:
        return "CURRENT_PRICE_TIMESTAMP_UNAVAILABLE", metrics

    quote_stamp = pd.Timestamp(quote_time)
    quote_stamp = quote_stamp.tz_localize("UTC") if quote_stamp.tzinfo is None else quote_stamp.tz_convert("UTC")
    quote_age_seconds = (now_stamp - quote_stamp).total_seconds()
    metrics["quote_age_seconds"] = quote_age_seconds
    if quote_age_seconds < -2 or quote_age_seconds > float(max_quote_age_seconds):
        return "EXECUTION_SKIPPED_STALE_QUOTE", metrics

    atr = float(decision.get("atr", 0.0) or 0.0)
    signal_price = float(decision.get("entry_underlying", 0.0) or 0.0)
    if atr <= 0 or signal_price <= 0:
        return "SIGNAL_ATR_UNAVAILABLE", metrics
    move_atr = abs(float(current_price) - signal_price) / atr
    metrics["move_atr"] = move_atr
    if move_atr > float(max_move_atr):
        return "EXECUTION_SKIPPED_PRICE_MOVE", metrics
    return "", metrics