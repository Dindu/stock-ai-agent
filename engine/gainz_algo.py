"""Closed-bar Python implementation of the GainzAlgo entry conditions."""

import pandas as pd


def _trend(frame):
    if frame is None or len(frame) < 20:
        return 0
    ema = frame["close"].ewm(span=20, adjust=False).mean().iloc[-1]
    typical = (frame["high"] + frame["low"] + frame["close"]) / 3.0
    vwap = (typical * frame["volume"]).cumsum().iloc[-1] / max(frame["volume"].cumsum().iloc[-1], 1.0)
    close = float(frame["close"].iloc[-1])
    return 1 if close > ema and close > vwap else -1 if close < ema and close < vwap else 0


def _resample(frame, rule):
    return frame.resample(rule, label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna()


def evaluate(bars_1m, bars_5m=None, *, pivot_length=5, momentum_threshold_base=0.01,
             volume_period=50, breakout_period=5, min_signal_distance=5,
             support_level=None, resistance_level=None):
    """Return (side, details) using confirmed 1m data; side is CALL, PUT, or None."""
    if bars_1m is None or len(bars_1m) < max(55, volume_period + 2):
        return None, {"reason": "insufficient 1m history"}
    frame = bars_1m[["open", "high", "low", "close", "volume"]].copy().sort_index()
    frame = frame.iloc[:-1] if len(frame) and pd.Timestamp.now(tz="UTC") <= pd.Timestamp(frame.index[-1]) else frame
    if len(frame) < max(55, volume_period + 2):
        return None, {"reason": "insufficient closed 1m history"}

    close = frame["close"]
    prev_close = close.shift(1)
    atr = close.diff().abs().rolling(14).mean().iloc[-1]
    atr = float(atr) if pd.notna(atr) and atr > 0 else float((frame["high"] - frame["low"]).iloc[-1])
    price = float(close.iloc[-1])
    volatility_factor = atr / max(price, 0.01)
    momentum_threshold = momentum_threshold_base * (1.0 + volatility_factor * 2.0)
    price_change = float(((price - prev_close.iloc[-1]) / prev_close.iloc[-1]) * 100.0) if prev_close.iloc[-1] else 0.0

    higher_frame = bars_5m[["open", "high", "low", "close", "volume"]].copy().sort_index() if bars_5m is not None and len(bars_5m) else frame
    trends = {
        "1m": _trend(frame),
        "5m": _trend(higher_frame),
        "15m": _trend(_resample(higher_frame, "15min")),
        "30m": _trend(_resample(higher_frame, "30min")),
        "1h": _trend(_resample(higher_frame, "1h")),
        "4h": _trend(_resample(higher_frame, "4h")),
        "1d": _trend(_resample(higher_frame, "1D")),
    }
    trend_strength_raw = sum(trends.values())
    trend_strength = trend_strength_raw / 7.0 * 100.0
    volume_average = frame["volume"].rolling(volume_period).mean().iloc[-1]
    short_volume = frame["volume"].rolling(5).mean()
    current_volume = float(frame["volume"].iloc[-1])
    volume_average = float(volume_average)
    short_volume_current = float(short_volume.iloc[-1])
    short_volume_previous = float(short_volume.iloc[-2])
    volume_above_average = current_volume > volume_average
    short_volume_rising = short_volume_current > short_volume_previous
    volume_ok = volume_above_average and short_volume_rising
    highest = frame["high"].rolling(breakout_period).max().shift(1).iloc[-1]
    lowest = frame["low"].rolling(breakout_period).min().shift(1).iloc[-1]
    support = float(support_level or frame["low"].rolling(20).min().iloc[-1])
    resistance = float(resistance_level or frame["high"].rolling(20).max().iloc[-1])
    support_distance = abs(price - support) / max(price, 0.01)
    resistance_distance = abs(resistance - price) / max(price, 0.01)
    room_to_resistance = (resistance - price) / max(price, 0.01)
    room_to_support = (price - support) / max(price, 0.01)
    bullish = price > float(frame["open"].iloc[-1])
    bearish = price < float(frame["open"].iloc[-1])
    buy_breakout = price > highest
    sell_breakdown = price < lowest
    buy_location_ok = support_distance <= 0.0035 or (buy_breakout and room_to_resistance >= 0.0020)
    sell_location_ok = resistance_distance <= 0.0035 or (sell_breakdown and room_to_support >= 0.0020)
    buy_momentum_ok = price_change > momentum_threshold
    sell_momentum_ok = price_change < -momentum_threshold
    buy_trend_ok = trends["5m"] == 1 and trends["1m"] != -1
    sell_trend_ok = trends["5m"] == -1 and trends["1m"] != 1
    buy = buy_momentum_ok and buy_trend_ok and volume_ok and buy_breakout and buy_location_ok
    sell = sell_momentum_ok and sell_trend_ok and volume_ok and sell_breakdown and sell_location_ok
    if not (bullish or bearish):
        buy = sell = False
    return ("CALL" if buy else "PUT" if sell else None), {
        "gainz_trends": trends,
        "gainz_trend_strength": trend_strength,
        "gainz_price_change_pct": price_change,
        "gainz_momentum_threshold_pct": momentum_threshold,
        "gainz_volume_ok": volume_ok,
        "gainz_current_volume": current_volume,
        "gainz_volume_period": volume_period,
        "gainz_volume_average": volume_average,
        "gainz_volume_ratio": current_volume / volume_average if volume_average > 0 else 0.0,
        "gainz_short_volume_average": short_volume_current,
        "gainz_previous_short_volume_average": short_volume_previous,
        "gainz_short_volume_ratio": short_volume_current / short_volume_previous if short_volume_previous > 0 else 0.0,
        "gainz_volume_above_average": volume_above_average,
        "gainz_short_volume_rising": short_volume_rising,
        "gainz_last_bar_timestamp": frame.index[-1].isoformat(),
        "gainz_buy_momentum_ok": buy_momentum_ok,
        "gainz_sell_momentum_ok": sell_momentum_ok,
        "gainz_buy_trend_ok": buy_trend_ok,
        "gainz_sell_trend_ok": sell_trend_ok,
        "gainz_buy_breakout": buy_breakout,
        "gainz_sell_breakdown": sell_breakdown,
        "gainz_buy_location_ok": buy_location_ok,
        "gainz_sell_location_ok": sell_location_ok,
        "gainz_bullish_candle": bullish,
        "gainz_bearish_candle": bearish,
        "gainz_support": support,
        "gainz_resistance": resistance,
        "gainz_support_distance_pct": support_distance * 100.0,
        "gainz_resistance_distance_pct": resistance_distance * 100.0,
        "gainz_room_to_resistance_pct": room_to_resistance * 100.0,
        "gainz_room_to_support_pct": room_to_support * 100.0,
        "gainz_location_ok": buy_location_ok if buy else sell_location_ok if sell else False,
        "gainz_breakout": "BUY" if buy else "SELL" if sell else "NONE",
        "reason": "confirmed GainzAlgo BUY" if buy else "confirmed GainzAlgo SELL" if sell else "no confirmed GainzAlgo signal",
    }