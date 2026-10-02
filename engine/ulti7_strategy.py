"""Python execution-layer port of the ULTI-7 V9.5 entry state machine.

This module returns an underlying-market entry decision only. The caller keeps
ownership of option selection, broker orders, fills, and trade accounting.
"""

from dataclasses import dataclass, field
from datetime import datetime
from threading import RLock

import pandas as pd


def _wilder_rma(values, length):
    values = pd.Series(values, dtype=float)
    result = pd.Series(float("nan"), index=values.index, dtype=float)
    valid = values.dropna()
    if len(valid) < length:
        return result
    first = valid.index[length - 1]
    start = values.index.get_loc(first)
    result.iloc[start] = float(values.iloc[start - length + 1:start + 1].mean())
    alpha = 1.0 / length
    for i in range(start + 1, len(values)):
        if pd.notna(values.iloc[i]):
            result.iloc[i] = alpha * values.iloc[i] + (1.0 - alpha) * result.iloc[i - 1]
    return result


def _session_vwap(frame):
    typical = (frame["high"] + frame["low"] + frame["close"]) / 3.0
    volume = frame["volume"].astype(float)
    if isinstance(frame.index, pd.DatetimeIndex):
        index = frame.index
        if index.tz is not None:
            session = index.tz_convert("America/New_York").date
        else:
            session = index.date
    else:
        session = [0] * len(frame)
    pv = (typical * volume).groupby(session).cumsum()
    vv = volume.groupby(session).cumsum().replace(0, float("nan"))
    return pv / vv


def _pine_base_series(frame):
    close = frame["close"].astype(float)
    previous_close = close.shift(1)
    true_range = pd.concat(
        [frame["high"] - frame["low"], (frame["high"] - previous_close).abs(), (frame["low"] - previous_close).abs()],
        axis=1,
    ).max(axis=1)
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = _wilder_rma(gain, 14)
    avg_loss = _wilder_rma(loss, 14)
    rs = avg_gain / avg_loss.replace(0.0, float("nan"))
    rsi = 100.0 - (100.0 / (1.0 + rs))
    rsi = rsi.where(avg_loss != 0.0, 100.0).where(avg_gain != 0.0, 0.0)
    return {
        "close": close,
        "ema9": close.ewm(span=9, adjust=False).mean(),
        "ema20": close.ewm(span=20, adjust=False).mean(),
        "ema21": close.ewm(span=21, adjust=False).mean(),
        "vwap": _session_vwap(frame),
        "atr14": _wilder_rma(true_range, 14),
        "atr200": _wilder_rma(true_range, 200),
        "rsi": rsi,
        "relvol": frame["volume"].astype(float) / frame["volume"].rolling(20).mean(),
        "true_range": true_range,
    }


def _poki_events(frame, atr14):
    close = frame["close"].astype(float).to_numpy()
    high = frame["high"].astype(float).to_numpy()
    low = frame["low"].astype(float).to_numpy()
    atr = atr14.to_numpy()
    direction = 0
    renko_close = 0.0
    long_event = [False] * len(frame)
    short_event = [False] * len(frame)
    previous_direction = 0
    for i in range(len(frame)):
        prior_renko = renko_close
        threshold = atr[i]
        if pd.notna(threshold) and close[i] > prior_renko + threshold:
            renko_close = close[i]
        elif pd.notna(threshold) and close[i] < prior_renko - threshold:
            renko_close = close[i]
        if renko_close > prior_renko:
            direction = 1
        elif renko_close < prior_renko:
            direction = -1
        if direction != previous_direction:
            long_event[i] = direction == 1
            short_event[i] = direction == -1
        previous_direction = direction
    return pd.Series(long_event, index=frame.index), pd.Series(short_event, index=frame.index)


def _vidya_events(frame, atr200):
    close = frame["close"].astype(float)
    momentum = close.diff()
    positive = momentum.where(momentum >= 0.0, 0.0).rolling(20).sum()
    negative = (-momentum).where(momentum < 0.0, 0.0).rolling(20).sum()
    denominator = positive + negative
    abs_cmo = (100.0 * (positive - negative) / denominator).abs()
    abs_cmo = abs_cmo.replace([float("inf"), -float("inf")], float("nan"))
    alpha = (2.0 / 11.0) * abs_cmo / 100.0
    vidya = pd.Series(0.0, index=frame.index)
    for i in range(1, len(frame)):
        current_alpha = alpha.iloc[i]
        if pd.isna(current_alpha):
            vidya.iloc[i] = 0.0
        else:
            vidya.iloc[i] = current_alpha * close.iloc[i] + (1.0 - current_alpha) * vidya.iloc[i - 1]
    smoothed = vidya.rolling(15).mean()
    upper = smoothed + atr200 * 2.0
    lower = smoothed - atr200 * 2.0
    trend_up = pd.Series(False, index=frame.index)
    for i in range(1, len(frame)):
        previous_up = bool(trend_up.iloc[i - 1])
        cross_up = close.iloc[i] > upper.iloc[i] and close.iloc[i - 1] <= upper.iloc[i - 1]
        cross_down = close.iloc[i] < lower.iloc[i] and close.iloc[i - 1] >= lower.iloc[i - 1]
        trend_up.iloc[i] = True if cross_up else False if cross_down else previous_up
    crossed_up = trend_up & ~trend_up.shift(1, fill_value=False)
    crossed_down = ~trend_up & trend_up.shift(1, fill_value=False)
    return crossed_up.shift(1, fill_value=False), crossed_down.shift(1, fill_value=False), trend_up


def create_pine_exit_plan(side, entry_price, atr, bars, entry_bar=None):
    """Build fixed Pine-style ATR targets and a six-bar structural stop."""
    frame = bars
    entry_price = float(entry_price or 0.0)
    atr = float(atr or 0.0)
    if frame is None or len(frame) < 7 or entry_price <= 0 or atr <= 0:
        return {}
    structure = frame.iloc[-7:-1]
    if side == "CALL":
        stop = min(entry_price - 0.50 * atr, float(structure["low"].min()) - 0.10 * atr)
        direction = 1.0
    elif side == "PUT":
        stop = max(entry_price + 0.50 * atr, float(structure["high"].max()) + 0.10 * atr)
        direction = -1.0
    else:
        return {}
    return {
        "entry_bar": str(entry_bar if entry_bar is not None else frame.index[-1]),
        "entry_price": entry_price,
        "atr": atr,
        "stop": stop,
        "initial_stop": stop,
        "initial_risk": abs(entry_price - stop),
        "tp1": entry_price + direction * 0.75 * atr,
        "tp2": entry_price + direction * 1.50 * atr,
        "tp3": entry_price + direction * 2.25 * atr,
        "tp1_taken": False,
        "tp2_taken": False,
        "runner_active": False,
        "last_bar": "",
    }


def pine_exit_event(trade, bars):
    """Advance an open Pine-managed trade and return its next exit action."""
    plan = trade.get("pine_exit_plan") or {}
    frame = bars
    if not plan or frame is None or len(frame) < 4:
        return None
    bar_time = str(frame.index[-1])
    if bar_time <= str(plan.get("entry_bar", "")) or bar_time == plan.get("last_bar"):
        return None
    plan["last_bar"] = bar_time
    latest = frame.iloc[-1]
    high = float(latest["high"])
    low = float(latest["low"])
    side = str(trade.get("side", "")).upper()
    true_range = pd.concat(
        [frame["high"] - frame["low"], (frame["high"] - frame["close"].shift()).abs(), (frame["low"] - frame["close"].shift()).abs()],
        axis=1,
    ).max(axis=1)
    atr_series = _wilder_rma(true_range, 14)
    trail_atr = float(atr_series.iloc[-2]) if len(atr_series) >= 2 and pd.notna(atr_series.iloc[-2]) else float(plan["atr"])
    if plan.get("tp2_taken"):
        completed = frame.iloc[-4:-1]
        if side == "CALL":
            plan["stop"] = max(float(plan["stop"]), float(completed["low"].min()) - 0.10 * trail_atr)
        else:
            plan["stop"] = min(float(plan["stop"]), float(completed["high"].max()) + 0.10 * trail_atr)
    elif plan.get("tp1_taken"):
        entry = float(plan["entry_price"])
        plan["stop"] = max(float(plan["stop"]), entry + 0.05 * plan["atr"]) if side == "CALL" else min(float(plan["stop"]), entry - 0.05 * plan["atr"])
    stop = float(plan["stop"])

    # A stop wins if a single bar spans both the stop and a profit target.
    if (side == "CALL" and low <= stop) or (side == "PUT" and high >= stop):
        reason = "PINE RUNNER TRAIL" if plan.get("runner_active") else "PINE ATR/STRUCTURE STOP"
        return {"kind": "STOP", "reason": reason, "close_qty": int(trade.get("qty", 0) or 0)}

    hit_tp1 = (side == "CALL" and high >= plan["tp1"]) or (side == "PUT" and low <= plan["tp1"])
    hit_tp2 = (side == "CALL" and high >= plan["tp2"]) or (side == "PUT" and low <= plan["tp2"])
    if not plan.get("tp1_taken") and hit_tp1:
        original_qty = int(trade.get("pine_original_qty", trade.get("qty", 0)) or 0)
        current_qty = int(trade.get("qty", 0) or 0)
        close_qty = min(max(0, current_qty - 1), int(original_qty * 0.50 + 0.5))
        event = {"kind": "TP1", "reason": "PINE TP1 0.75 ATR", "close_qty": close_qty}
        if hit_tp2:
            remaining_qty = max(0, current_qty - close_qty)
            tp2_qty = min(max(0, remaining_qty - 1), int(original_qty * 0.15 + 0.5))
            event["same_bar_tp2"] = {
                "kind": "TP2",
                "reason": "PINE TP2 1.50 ATR",
                "close_qty": tp2_qty,
            }
        return event

    if plan.get("tp1_taken") and not plan.get("tp2_taken") and hit_tp2:
        original_qty = int(trade.get("pine_original_qty", trade.get("qty", 0)) or 0)
        current_qty = int(trade.get("qty", 0) or 0)
        close_qty = min(max(0, current_qty - 1), int(original_qty * 0.15 + 0.5))
        return {"kind": "TP2", "reason": "PINE TP2 1.50 ATR", "close_qty": close_qty}

    return None


@dataclass
class _SymbolState:
    initialized: bool = False
    last_bar: object = None
    ready_side: str = ""
    ready_age: int = 0
    last_entry_age: int = -100000
    age: int = 0
    last_reversal_side: str = ""
    loss_block_side: str = ""
    loss_block_bar: object = None
    runner_block_side: str = ""
    runner_block_bar: object = None
    continuation_side: str = ""
    continuation_age: int = 0
    prior_source_votes: dict = field(default_factory=dict)
    prior_price: float = 0.0
    prior_ema9: float = 0.0
    prior_ema21: float = 0.0
    prior_vidya_up: bool = False
    prior_regime: str = ""


class Ulti7EntryEngine:
    """V2 context gates plus V3-V6 priority entries and V9 CONT quality."""

    def __init__(self):
        self._states = {}
        self._state_lock = RLock()

    def record_exit(self, symbol, side, is_loss=False, runner=False):
        with self._state_lock:
            state = self._states.setdefault(str(symbol).upper(), _SymbolState())
            if is_loss:
                state.loss_block_side = side
                state.loss_block_bar = state.last_bar
            if runner:
                state.runner_block_side = side
                state.runner_block_bar = state.last_bar

    def export_state(self):
        with self._state_lock:
            exported = {}
            for symbol, state in self._states.items():
                values = vars(state).copy()
                for key in ("last_bar", "loss_block_bar", "runner_block_bar"):
                    value = values.get(key)
                    if value is not None and hasattr(value, "isoformat"):
                        values[key] = value.isoformat()
                exported[symbol] = values
            return exported

    def restore_state(self, exported):
        timestamp_fields = {"last_bar", "loss_block_bar", "runner_block_bar"}
        known_fields = set(_SymbolState.__dataclass_fields__)
        with self._state_lock:
            for symbol, saved in (exported or {}).items():
                if not isinstance(saved, dict):
                    continue
                values = {key: value for key, value in saved.items() if key in known_fields}
                for key in timestamp_fields:
                    if values.get(key):
                        values[key] = pd.Timestamp(values[key])
                state = _SymbolState()
                for key, value in values.items():
                    setattr(state, key, value)
                self._states[str(symbol).upper()] = state

    @staticmethod
    def _num(value, default=0.0):
        try:
            result = float(value)
            return result if pd.notna(result) else default
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _source_votes(confluence, side):
        votes = (confluence or {}).get("votes", {})
        sign = 1 if side == "CALL" else -1
        return sum(1 for name in ("poki", "smc", "vidya") if int(votes.get(name, 0) or 0) == sign)

    @staticmethod
    def _mtf(bars):
        frame = bars.copy()
        if frame.empty or len(frame) < 5:
            return {}, {}
        if not isinstance(frame.index, pd.DatetimeIndex):
            return {}, {}
        if frame.index.tz is None:
            frame.index = frame.index.tz_localize("UTC")
        chart_index = frame.index.tz_convert("America/New_York")
        frame.index = chart_index
        base = _pine_base_series(frame)
        i = len(frame) - 2
        close = base["close"]
        ema = base["ema20"]
        vwap = base["vwap"]
        atr = base["atr14"]
        five = {
            "close": float(close.iloc[i]),
            "ema": float(ema.iloc[i]),
            "vwap": float(vwap.iloc[i]) if pd.notna(vwap.iloc[i]) else float(close.iloc[i]),
            "atr": float(atr.iloc[i]) if pd.notna(atr.iloc[i]) else 0.0,
            "swing_high": float(frame["high"].iloc[max(0, i - 11):i + 1].max()),
            "swing_low": float(frame["low"].iloc[max(0, i - 11):i + 1].min()),
            "close_3": float(close.iloc[max(0, i - 3)]),
        }
        five["bull"] = five["close"] > five["ema"] and five["close"] > five["vwap"]
        five["bear"] = five["close"] < five["ema"] and five["close"] < five["vwap"]
        sampled = frame.resample("15min", label="right", closed="left").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
        ).dropna()
        completed_15m = sampled.loc[sampled.index <= chart_index[-1]]
        fifteen = {}
        if len(completed_15m):
            c15 = completed_15m["close"].astype(float)
            e15 = c15.ewm(span=20, adjust=False).mean()
            v15 = _session_vwap(completed_15m)
            fifteen = {
                "close": float(c15.iloc[-1]),
                "ema": float(e15.iloc[-1]),
                "vwap": float(v15.iloc[-1]) if pd.notna(v15.iloc[-1]) else float(c15.iloc[-1]),
            }
            fifteen["bull"] = fifteen["close"] > fifteen["ema"] and fifteen["close"] > fifteen["vwap"]
            fifteen["bear"] = fifteen["close"] < fifteen["ema"] and fifteen["close"] < fifteen["vwap"]
        five["call_score"] = (2 if five["bull"] else 0) + (1 if fifteen.get("bull", False) else 0) + (1 if not five["bear"] and not fifteen.get("bear", False) else 0)
        five["put_score"] = (2 if five["bear"] else 0) + (1 if fifteen.get("bear", False) else 0) + (1 if not five["bull"] and not fifteen.get("bull", False) else 0)
        return five, fifteen

    @staticmethod
    def _smc_events(frame, base):
        length = 5
        high = frame["high"].astype(float).to_numpy()
        low = frame["low"].astype(float).to_numpy()
        close = frame["close"].astype(float).to_numpy()
        open_price = frame["open"].astype(float).to_numpy()
        volume = frame["volume"].astype(float)
        atr = base["atr14"].to_numpy()
        ema20 = base["ema20"].to_numpy()
        vwap = base["vwap"].to_numpy()
        count = len(frame)
        last_high = [float("nan")] * count
        last_low = [float("nan")] * count
        current_high = float("nan")
        current_low = float("nan")
        for i in range(count):
            center = i - length
            if center >= length and i >= center + length:
                high_window = high[center - length:center + length + 1]
                low_window = low[center - length:center + length + 1]
                if high[center] == max(high_window):
                    current_high = high[center]
                if low[center] == min(low_window):
                    current_low = low[center]
            last_high[i] = current_high
            last_low[i] = current_low

        smc_buy = [False] * count
        smc_sell = [False] * count
        choch_buy = [False] * count
        choch_sell = [False] * count
        bos_buy = [False] * count
        bos_sell = [False] * count
        long_volume_avg = volume.rolling(50).mean().to_numpy()
        short_volume_avg = volume.rolling(5).mean().to_numpy()
        short_volume_change = pd.Series(short_volume_avg).diff().to_numpy()
        highest_previous = frame["high"].rolling(5).max().shift(1).to_numpy()
        lowest_previous = frame["low"].rolling(5).min().shift(1).to_numpy()
        last_signal = "Neutral"
        last_signal_bar = -6
        last_signal_trend = 0
        for i in range(1, count):
            trend = 1 if close[i] > ema20[i] and close[i] > vwap[i] else -1 if close[i] < ema20[i] and close[i] < vwap[i] else 0
            previous_high = last_high[i - 1]
            previous_low = last_low[i - 1]
            previous_previous_high = last_high[i - 2] if i >= 2 else float("nan")
            previous_previous_low = last_low[i - 2] if i >= 2 else float("nan")
            choch_sell_now = close[i] < open_price[i] and pd.notna(last_high[i]) and low[i] < last_high[i] and pd.notna(previous_high) and low[i - 1] >= previous_high
            choch_buy_now = close[i] > open_price[i] and pd.notna(last_low[i]) and high[i] > last_low[i] and pd.notna(previous_low) and high[i - 1] <= previous_low
            bos_sell_now = close[i] < open_price[i] and pd.notna(previous_low) and low[i] < previous_low and pd.notna(previous_previous_low) and low[i - 1] >= previous_previous_low
            bos_buy_now = close[i] > open_price[i] and pd.notna(previous_high) and high[i] > previous_high and pd.notna(previous_previous_high) and high[i - 1] <= previous_previous_high
            choch_sell[i] = choch_sell_now
            choch_buy[i] = choch_buy_now
            bos_sell[i] = bos_sell_now
            bos_buy[i] = bos_buy_now

            if close[i - 1] <= 0 or not pd.notna(atr[i]):
                continue
            price_change = (close[i] - close[i - 1]) / close[i - 1] * 100.0
            momentum_threshold = 0.01 * (1.0 + (atr[i] / close[i]) * 2.0)
            volatility_ok = volume.iloc[i] > long_volume_avg[i] and short_volume_change[i] > 0
            breakout_buy_ok = close[i] > highest_previous[i] if pd.notna(highest_previous[i]) else False
            breakout_sell_ok = close[i] < lowest_previous[i] if pd.notna(lowest_previous[i]) else False
            distance_ok = i - last_signal_bar >= 5
            buy_allowed = last_signal != "Buy" or (last_signal == "Buy" and trend != last_signal_trend and trend != 1)
            sell_allowed = last_signal != "Sell" or (last_signal == "Sell" and trend != last_signal_trend and trend != -1)
            buy = price_change > momentum_threshold and distance_ok and trend == 1 and trend != 0 and volatility_ok and breakout_buy_ok and buy_allowed
            sell = price_change < -momentum_threshold and distance_ok and trend == -1 and trend != 0 and volatility_ok and breakout_sell_ok and sell_allowed
            if buy:
                smc_buy[i] = True
                last_signal, last_signal_bar, last_signal_trend = "Buy", i, trend
            elif sell:
                smc_sell[i] = True
                last_signal, last_signal_bar, last_signal_trend = "Sell", i, trend
        return {
            "buy": pd.Series(smc_buy, index=frame.index),
            "sell": pd.Series(smc_sell, index=frame.index),
            "choch_buy": pd.Series(choch_buy, index=frame.index),
            "choch_sell": pd.Series(choch_sell, index=frame.index),
            "bos_buy": pd.Series(bos_buy, index=frame.index),
            "bos_sell": pd.Series(bos_sell, index=frame.index),
        }

    def evaluate(self, symbol, bars, data, confluence, now=None):
        data = data or {}
        frame = bars.copy()
        if frame is None or len(frame) < 25:
            return None, "insufficient bars"
        state = self._states.setdefault(str(symbol).upper(), _SymbolState())
        has_prior_bar = state.last_bar is not None
        bar_time = frame.index[-1] if isinstance(frame.index, pd.DatetimeIndex) else len(frame)
        if state.last_bar != bar_time:
            state.age += 1
            state.last_bar = bar_time
        price = self._num(data.get("price", frame["close"].iloc[-1]))
        open_price = self._num(data.get("open", frame["open"].iloc[-1]), price)
        high = self._num(data.get("high", frame["high"].iloc[-1]), price)
        low = self._num(data.get("low", frame["low"].iloc[-1]), price)
        ema9 = self._num(data.get("ema9", frame["close"].ewm(span=9, adjust=False).mean().iloc[-1]), price)
        ema21 = self._num(data.get("ema20", frame["close"].ewm(span=21, adjust=False).mean().iloc[-1]), price)
        vwap = self._num(data.get("vwap", price), price)
        atr = max(self._num(data.get("atr14", 0.0)), 0.0)
        if atr <= 0:
            return None, "ATR unavailable"
        relvol = self._num(data.get("vol_ratio", 0.0))
        body_atr = abs(price - open_price) / atr if atr > 0 else 0.0
        candle_bull = price > open_price
        candle_bear = price < open_price
        close_loc = (price - low) / max(high - low, 1e-9)
        prior_bar = frame.iloc[-2]
        fresh_up = price > float(prior_bar["high"])
        fresh_down = price < float(prior_bar["low"])
        source_votes = (confluence or {}).get("votes", {})
        source_events = {
            source: int(source_votes.get(source, 0) or 0)
            if has_prior_bar and int(source_votes.get(source, 0) or 0) != int(state.prior_source_votes.get(source, 0) or 0)
            else 0
            for source in ("poki", "smc", "vidya")
        }
        long_votes = sum(1 for vote in source_events.values() if vote > 0)
        short_votes = sum(1 for vote in source_events.values() if vote < 0)
        five, fifteen = self._mtf(frame)
        five_bull, five_bear = five.get("bull", False), five.get("bear", False)
        fifteen_bull, fifteen_bear = fifteen.get("bull", False), fifteen.get("bear", False)
        call_mtf = (2 if five_bull else 0) + (1 if fifteen_bull else 0) + (1 if not five_bear and not fifteen_bear else 0)
        put_mtf = (2 if five_bear else 0) + (1 if fifteen_bear else 0) + (1 if not five_bull and not fifteen_bull else 0)
        recent = frame.iloc[-13:-1]
        swing_high = float(recent["high"].max()) if len(recent) else high
        swing_low = float(recent["low"].min()) if len(recent) else low
        room_call = (swing_high - price) / atr if atr > 0 else 999.0
        room_put = (price - swing_low) / atr if atr > 0 else 999.0
        extension_atr = abs(price - vwap) / atr if atr > 0 else 0.0
        rsi = self._num(data.get("rsi", 50.0), 50.0)
        prior_close = self._num(frame["close"].iloc[-2], price)
        prior_ema9 = self._num(frame["close"].ewm(span=9, adjust=False).mean().iloc[-2], ema9)
        prior_ema21 = self._num(frame["close"].ewm(span=21, adjust=False).mean().iloc[-2], ema21)
        chop = False
        if atr > 0 and len(frame) >= 5:
            ema_vwap_width = abs(five.get("ema", ema21) - five.get("vwap", vwap)) / atr
            churn_return = abs(five.get("close", price) - self._num(frame["close"].iloc[-5], price)) / atr
            chop = ema_vwap_width <= 0.18 and churn_return <= 0.35
        expansion_bull = candle_bull and body_atr >= 0.85 and relvol >= 1.50 and fresh_up
        expansion_bear = candle_bear and body_atr >= 0.85 and relvol >= 1.50 and fresh_down
        room_call_ok = room_call >= 0.75 or price > swing_high
        room_put_ok = room_put >= 0.75 or price < swing_low
        call_exhausted = extension_atr >= 1.80 and rsi >= 72
        put_exhausted = extension_atr >= 1.80 and rsi <= 28
        entry_quality_long = price > vwap and price > ema9 and candle_bull and rsi >= 50 and extension_atr / max(price / 100.0, 1e-9) <= 1.2
        entry_quality_short = price < vwap and price < ema9 and candle_bear and rsi <= 50 and extension_atr / max(price / 100.0, 1e-9) <= 1.2
        call_mtf_ok = call_mtf >= 1 and not (five_bear and fifteen_bear)
        put_mtf_ok = put_mtf >= 1 and not (five_bull and fifteen_bull)
        expansion_call_override = expansion_bull and five_bull and not (five_bear and fifteen_bear)
        expansion_put_override = expansion_bear and five_bear and not (five_bull and fifteen_bull)
        call_ok = entry_quality_long and call_mtf_ok and room_call_ok and (not call_exhausted or expansion_call_override) and (not chop or expansion_call_override)
        put_ok = entry_quality_short and put_mtf_ok and room_put_ok and (not put_exhausted or expansion_put_override) and (not chop or expansion_put_override)

        if state.ready_age > 8:
            state.ready_side, state.ready_age = "", 0
        arm_call = call_ok and long_votes > 0
        arm_put = put_ok and short_votes > 0
        if arm_call and not arm_put and state.ready_side != "CALL":
            state.ready_side, state.ready_age = "CALL", 0
        elif arm_put and not arm_call and state.ready_side != "PUT":
            state.ready_side, state.ready_age = "PUT", 0
        elif state.ready_side:
            state.ready_age += 1

        bull_displacement = candle_bull and body_atr >= 0.35 and relvol >= 0.90
        bear_displacement = candle_bear and body_atr >= 0.35 and relvol >= 0.90
        structure_call = state.ready_side == "CALL" and call_ok and fresh_up and bull_displacement
        structure_put = state.ready_side == "PUT" and put_ok and fresh_down and bear_displacement

        prior_bearish = prior_close < prior_ema9 or prior_close < prior_ema21 or five_bear
        prior_bullish = prior_close > prior_ema9 or prior_close > prior_ema21 or five_bull
        cross_up_ema9 = prior_close <= prior_ema9 and price > ema9
        cross_down_ema9 = prior_close >= prior_ema9 and price < ema9
        reversal_call_trigger = fresh_up or cross_up_ema9 or (price > float(prior_bar["high"]) and prior_close <= prior_ema9)
        reversal_put_trigger = fresh_down or cross_down_ema9 or (price < float(prior_bar["low"]) and prior_close >= prior_ema9)
        reversal_call = prior_bearish and reversal_call_trigger and candle_bull and body_atr >= 0.55 and relvol >= 0.85 and price > ema9 and price > vwap and entry_quality_long and room_call_ok and not call_exhausted
        reversal_put = prior_bullish and reversal_put_trigger and candle_bear and body_atr >= 0.55 and relvol >= 0.85 and price < ema9 and price < vwap and entry_quality_short and room_put_ok and not put_exhausted
        previous_bear_regime = five_bear or prior_close < prior_ema21 or not state.prior_vidya_up
        previous_bull_regime = five_bull or prior_close > prior_ema21 or state.prior_vidya_up
        fast_break_high = price > float(frame["high"].iloc[-4:-1].max())
        fast_break_low = price < float(frame["low"].iloc[-4:-1].min())
        fast_rev_call = previous_bear_regime and candle_bull and body_atr >= 0.45 and relvol >= 0.70 and price > ema9 and (fresh_up or fast_break_high or cross_up_ema9) and room_call_ok and not call_exhausted
        fast_rev_put = previous_bull_regime and candle_bear and body_atr >= 0.45 and relvol >= 0.70 and price < ema9 and (fresh_down or fast_break_low or cross_down_ema9) and room_put_ok and not put_exhausted
        reversal_call = (reversal_call or fast_rev_call) and (long_votes > 0 or (body_atr >= 0.75 and relvol >= 1.0 and fresh_up))
        reversal_put = (reversal_put or fast_rev_put) and (short_votes > 0 or (body_atr >= 0.75 and relvol >= 1.0 and fresh_down))
        if state.last_reversal_side == "PUT" and five_bull and price > ema21 and int(source_votes.get("vidya", 0) or 0) > 0:
            state.last_reversal_side = ""
        if state.last_reversal_side == "CALL" and five_bear and price < ema21 and int(source_votes.get("vidya", 0) or 0) < 0:
            state.last_reversal_side = ""
        reversal_call = reversal_call and state.last_reversal_side != "CALL"
        reversal_put = reversal_put and state.last_reversal_side != "PUT"

        cont_call_pullback = five_bull and price > ema21 and price > vwap and low <= ema9 + 0.35 * atr and low >= vwap - 0.35 * atr
        cont_put_pullback = five_bear and price < ema21 and price < vwap and high >= ema9 - 0.35 * atr and high <= vwap + 0.35 * atr
        if state.loss_block_side == "CALL" and state.loss_block_bar is not None and bar_time > state.loss_block_bar and (fast_break_high or cont_call_pullback or source_events.get("smc", 0) > 0):
            state.loss_block_side = ""
            state.loss_block_bar = None
        if state.loss_block_side == "PUT" and state.loss_block_bar is not None and bar_time > state.loss_block_bar and (fast_break_low or cont_put_pullback or source_events.get("smc", 0) < 0):
            state.loss_block_side = ""
            state.loss_block_bar = None
        if state.runner_block_side == "CALL" and state.runner_block_bar is not None and bar_time > state.runner_block_bar and (cont_call_pullback or source_events.get("smc", 0) > 0):
            state.runner_block_side = ""
            state.runner_block_bar = None
        if state.runner_block_side == "PUT" and state.runner_block_bar is not None and bar_time > state.runner_block_bar and (cont_put_pullback or source_events.get("smc", 0) < 0):
            state.runner_block_side = ""
            state.runner_block_bar = None
        if cont_call_pullback:
            state.continuation_side, state.continuation_age = "CALL", 0
        elif cont_put_pullback:
            state.continuation_side, state.continuation_age = "PUT", 0
        elif state.continuation_side:
            state.continuation_age += 1
        if state.continuation_age > 8:
            state.continuation_side = ""
        cont_call = state.continuation_side == "CALL" and five_bull and fresh_up and candle_bull and body_atr >= 0.30 and relvol >= 0.75 and call_ok
        cont_put = state.continuation_side == "PUT" and five_bear and fresh_down and candle_bear and body_atr >= 0.30 and relvol >= 0.75 and put_ok
        midday = bool(now and 11 <= now.hour < 13)
        def cont_pass(is_call):
            mtf = call_mtf if is_call else put_mtf
            votes = long_votes if is_call else short_votes
            counter = five_bear if is_call else five_bull
            quality = mtf + min(votes, 2) + (1 if relvol >= 1.0 else 0) + (1 if not chop else 0)
            required = 4 + (1 if midday else 0) + (1 if counter else 0)
            return quality >= required and relvol >= 0.85
        cont_call = cont_call and cont_pass(True)
        cont_put = cont_put and cont_pass(False)
        consensus_call, consensus_put = long_votes >= 2, short_votes >= 2
        context_call = long_votes >= 1 and five_bull and not chop
        context_put = short_votes >= 1 and five_bear and not chop
        candidates = {
            "CALL": [state.loss_block_side != "CALL" and state.runner_block_side != "CALL" and reversal_call, state.loss_block_side != "CALL" and state.runner_block_side != "CALL" and call_ok and structure_call, state.loss_block_side != "CALL" and state.runner_block_side != "CALL" and call_ok and cont_call, state.loss_block_side != "CALL" and state.runner_block_side != "CALL" and call_ok and consensus_call, state.loss_block_side != "CALL" and state.runner_block_side != "CALL" and call_ok and context_call],
            "PUT": [state.loss_block_side != "PUT" and state.runner_block_side != "PUT" and reversal_put, state.loss_block_side != "PUT" and state.runner_block_side != "PUT" and put_ok and structure_put, state.loss_block_side != "PUT" and state.runner_block_side != "PUT" and put_ok and cont_put, state.loss_block_side != "PUT" and state.runner_block_side != "PUT" and put_ok and consensus_put, state.loss_block_side != "PUT" and state.runner_block_side != "PUT" and put_ok and context_put],
        }
        priorities = ["REV", "STRUCTURE", "CONT", "CONSENSUS", "CONTEXT"]
        valid = {side: next((priorities[i] for i, ok in enumerate(flags) if ok), "") for side, flags in candidates.items()}
        selected_side = ""
        selected_setup = ""
        if valid["CALL"] and not valid["PUT"]: selected_side, selected_setup = "CALL", valid["CALL"]
        elif valid["PUT"] and not valid["CALL"]: selected_side, selected_setup = "PUT", valid["PUT"]
        elif valid["CALL"] and valid["PUT"]:
            if priorities.index(valid["CALL"]) < priorities.index(valid["PUT"]): selected_side, selected_setup = "CALL", valid["CALL"]
            elif priorities.index(valid["PUT"]) < priorities.index(valid["CALL"]): selected_side, selected_setup = "PUT", valid["PUT"]
            elif long_votes != short_votes: selected_side, selected_setup = ("CALL", valid["CALL"]) if long_votes > short_votes else ("PUT", valid["PUT"])
            elif five_bull != five_bear: selected_side, selected_setup = ("CALL", valid["CALL"]) if five_bull else ("PUT", valid["PUT"])
        if state.age - state.last_entry_age < 5: selected_side = ""
        if selected_setup == "REV" and state.last_reversal_side == selected_side: selected_side = ""
        result = None
        if selected_side:
            exit_plan = create_pine_exit_plan(selected_side, price, atr, frame, bar_time)
            exit_plan["setup_type"] = selected_setup
            result = {"side": selected_side, "setup": selected_setup, "long_votes": long_votes, "short_votes": short_votes, "call_mtf": call_mtf, "put_mtf": put_mtf, "atr": atr, "tp1_atr": 0.75, "tp2_atr": 1.50, "tp3_atr": 2.25, "entry_underlying": price, "entry_bar": str(bar_time), "exit_plan": exit_plan}
            state.last_entry_age = state.age
            state.ready_side = ""
            if selected_setup == "REV": state.last_reversal_side = selected_side
            if selected_setup == "CONT": state.continuation_side = ""
        state.prior_source_votes = {name: int(source_votes.get(name, 0) or 0) for name in ("poki", "smc", "vidya")}
        state.prior_price = price
        state.prior_ema9 = ema9
        state.prior_ema21 = ema21
        state.prior_vidya_up = bool(source_votes.get("vidya", 0) > 0)
        state.prior_regime = "BULL" if five_bull else "BEAR" if five_bear else "NEUTRAL"
        return result, f"setup={selected_setup or 'NONE'}, sources={long_votes}/{short_votes}, MTF={call_mtf}/{put_mtf}, room={room_call:.2f}/{room_put:.2f} ATR"


class PineV95EntryEngine(Ulti7EntryEngine):
    """Deterministic 5-minute implementation of the saved ULTI-7 V9.5 defaults."""

    def record_exit(self, symbol, side, is_loss=False, runner=False, bar_time=None):
        with self._state_lock:
            state = self._states.setdefault(str(symbol).upper(), _SymbolState())
            exit_bar = pd.Timestamp(bar_time) if bar_time else state.last_bar
            if is_loss:
                state.loss_block_side = side
                state.loss_block_bar = exit_bar
            if runner:
                state.runner_block_side = side
                state.runner_block_bar = exit_bar

    def evaluate(self, symbol, bars, data=None, confluence=None, now=None):
        with self._state_lock:
            return self._evaluate_unlocked(symbol, bars, data, confluence, now)

    def _evaluate_unlocked(self, symbol, bars, data=None, confluence=None, now=None):
        frame = bars.copy()
        if frame is None or frame.empty:
            return None, "no bars"
        if isinstance(frame.index, pd.DatetimeIndex):
            now_ts = pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz="UTC")
            if now_ts.tzinfo is None:
                now_ts = now_ts.tz_localize("UTC")
            else:
                now_ts = now_ts.tz_convert("UTC")
            last_bar = pd.Timestamp(frame.index[-1])
            if last_bar.tzinfo is None:
                last_bar = last_bar.tz_localize("UTC")
            else:
                last_bar = last_bar.tz_convert("UTC")
            if last_bar + pd.Timedelta(minutes=5) > now_ts:
                frame = frame.iloc[:-1]
        if len(frame) < 30:
            return None, "insufficient completed 5-minute bars"

        state = self._states.setdefault(str(symbol).upper(), _SymbolState())
        if not state.initialized:
            state.initialized = True
            # READY, continuation, and entry cooldown expire within 8/8/5 bars.
            for replay_end in range(max(29, len(frame) - 12), len(frame) - 1):
                replay_frame = frame.iloc[:replay_end + 1]
                replay_now = pd.Timestamp(replay_frame.index[-1]) + pd.Timedelta(minutes=5)
                self.evaluate(symbol, replay_frame, now=replay_now)
        bar_time = frame.index[-1]
        new_bar = state.last_bar != bar_time
        if new_bar:
            state.age += 1
            if state.ready_side:
                state.ready_age += 1
            if state.continuation_side:
                state.continuation_age += 1
            state.last_bar = bar_time

        features = _pine_base_series(frame)
        five, fifteen = self._mtf(frame)
        if not five:
            return None, "insufficient MTF context"
        close = features["close"]
        ema9 = features["ema9"]
        ema20 = features["ema20"]
        ema21 = features["ema21"]
        vwap = features["vwap"]
        atr = features["atr14"]
        rsi = features["rsi"]
        relvol = features["relvol"]
        current_close = float(close.iloc[-1])
        current_open = float(frame["open"].iloc[-1])
        current_high = float(frame["high"].iloc[-1])
        current_low = float(frame["low"].iloc[-1])
        current_atr = float(atr.iloc[-1]) if pd.notna(atr.iloc[-1]) else 0.0
        if current_atr <= 0:
            return None, "ATR unavailable"
        current_relvol = float(relvol.iloc[-1]) if pd.notna(relvol.iloc[-1]) else 0.0
        current_rsi = float(rsi.iloc[-1]) if pd.notna(rsi.iloc[-1]) else 50.0
        current_ema9 = float(ema9.iloc[-1])
        current_ema21 = float(ema21.iloc[-1])
        current_vwap = float(vwap.iloc[-1]) if pd.notna(vwap.iloc[-1]) else current_close
        prior_close = float(close.iloc[-2])
        prior_ema9 = float(ema9.iloc[-2])
        prior_ema21 = float(ema21.iloc[-2])
        prior_vwap = float(vwap.iloc[-2]) if pd.notna(vwap.iloc[-2]) else prior_close
        candle_range = max(current_high - current_low, 1e-9)
        body_atr = abs(current_close - current_open) / current_atr
        bull_candle = current_close > current_open
        bear_candle = current_close < current_open
        fresh_high = current_close > float(frame["high"].iloc[-2])
        fresh_low = current_close < float(frame["low"].iloc[-2])

        # Pine's shared entry-quality score (defaults enabled).
        return_5 = close / close.shift(5) - 1.0
        moving_away_bull = (close > vwap) & ((close - vwap) > (close.shift(1) - vwap.shift(1)))
        moving_away_bear = (close < vwap) & ((close - vwap) < (close.shift(1) - vwap.shift(1)))
        clamp = lambda values: values.clip(lower=0.0, upper=1.0)
        momentum_bull = 100.0 * (
            clamp((rsi - 50.0) / 20.0) * 0.45
            + clamp(return_5 / 0.01) * 0.35
            + moving_away_bull.astype(float) * 0.20
        )
        momentum_bear = 100.0 * (
            clamp((50.0 - rsi) / 20.0) * 0.45
            + clamp(-return_5 / 0.01) * 0.35
            + moving_away_bear.astype(float) * 0.20
        )
        recent_high = frame["high"].rolling(20).max().shift(1)
        recent_low = frame["low"].rolling(20).min().shift(1)
        pattern_bull = 100.0 * (
            (close > frame["open"]).astype(float) * 0.35
            + (close > recent_high).astype(float) * 0.35
            + moving_away_bull.astype(float) * 0.30
        )
        pattern_bear = 100.0 * (
            (close < frame["open"]).astype(float) * 0.35
            + (close < recent_low).astype(float) * 0.35
            + moving_away_bear.astype(float) * 0.30
        )
        extension_pct = (close - vwap).abs() / vwap.replace(0.0, float("nan")) * 100.0
        strong_bull_context = (close > vwap) & (close > ema9) & (ema9 > ema9.shift(1))
        strong_bear_context = (close < vwap) & (close < ema9) & (ema9 < ema9.shift(1))
        momentum_bull_rising = momentum_bull >= momentum_bull.shift(1).fillna(momentum_bull)
        momentum_bear_rising = momentum_bear >= momentum_bear.shift(1).fillna(momentum_bear)
        long_quality = bool(
            extension_pct.iloc[-1] <= 1.2
            and momentum_bull.iloc[-1] >= 20.0
            and pattern_bull.iloc[-1] >= 25.0
            and momentum_bull_rising.iloc[-1]
            and (not strong_bull_context.iloc[-1] or (momentum_bull.iloc[-1] >= 45.0 and pattern_bull.iloc[-1] >= 45.0))
        )
        short_quality = bool(
            extension_pct.iloc[-1] <= 1.2
            and momentum_bear.iloc[-1] >= 20.0
            and pattern_bear.iloc[-1] >= 25.0
            and momentum_bear_rising.iloc[-1]
            and (not strong_bear_context.iloc[-1] or (momentum_bear.iloc[-1] >= 45.0 and pattern_bear.iloc[-1] >= 45.0))
        )

        five_bull, five_bear = bool(five["bull"]), bool(five["bear"])
        fifteen_bull, fifteen_bear = bool(fifteen.get("bull", False)), bool(fifteen.get("bear", False))
        call_mtf, put_mtf = int(five["call_score"]), int(five["put_score"])
        five_atr = float(five["atr"])
        call_room = (float(five["swing_high"]) - current_close) / five_atr if five_atr > 0 else 999.0
        put_room = (current_close - float(five["swing_low"])) / five_atr if five_atr > 0 else 999.0
        room_call_ok = call_room >= 0.75 or current_close > float(five["swing_high"])
        room_put_ok = put_room >= 0.75 or current_close < float(five["swing_low"])
        five_extension = abs(current_close - float(five["vwap"])) / five_atr if five_atr > 0 else 0.0
        call_exhausted = five_extension >= 1.80 and current_rsi >= 72.0
        put_exhausted = five_extension >= 1.80 and current_rsi <= 28.0
        ema_vwap_width = abs(float(five["ema"]) - float(five["vwap"])) / five_atr if five_atr > 0 else 999.0
        churn_return = abs(float(five["close"]) - float(five["close_3"])) / five_atr if five_atr > 0 else 999.0
        chop = ema_vwap_width <= 0.18 and churn_return <= 0.35
        expansion_bull = bull_candle and body_atr >= 0.85 and current_relvol >= 1.50 and fresh_high
        expansion_bear = bear_candle and body_atr >= 0.85 and current_relvol >= 1.50 and fresh_low
        expansion_call_override = expansion_bull and five_bull and not (five_bear and fifteen_bear)
        expansion_put_override = expansion_bear and five_bear and not (five_bull and fifteen_bull)
        call_ok = long_quality and call_mtf >= 1 and not (five_bear and fifteen_bear) and room_call_ok and (not call_exhausted or expansion_call_override) and (not chop or expansion_call_override)
        put_ok = short_quality and put_mtf >= 1 and not (five_bull and fifteen_bull) and room_put_ok and (not put_exhausted or expansion_put_override) and (not chop or expansion_put_override)

        poki_call_series, poki_put_series = _poki_events(frame, atr)
        vidya_call_series, vidya_put_series, vidya_trend = _vidya_events(frame, features["atr200"])
        smc = self._smc_events(frame, features)
        long_votes = int(bool(poki_call_series.iloc[-1] and call_ok)) + int(bool(smc["buy"].iloc[-1] and call_ok)) + int(bool(vidya_call_series.iloc[-1] and call_ok))
        short_votes = int(bool(poki_put_series.iloc[-1] and put_ok)) + int(bool(smc["sell"].iloc[-1] and put_ok)) + int(bool(vidya_put_series.iloc[-1] and put_ok))
        smc_choch_buy = bool(smc["choch_buy"].iloc[-1])
        smc_choch_sell = bool(smc["choch_sell"].iloc[-1])
        smc_bos_buy = bool(smc["bos_buy"].iloc[-1])
        smc_bos_sell = bool(smc["bos_sell"].iloc[-1])
        displacement_bull = bull_candle and body_atr >= 0.35 and current_relvol >= 0.90
        displacement_bear = bear_candle and body_atr >= 0.35 and current_relvol >= 0.90

        if state.ready_age > 8:
            state.ready_side, state.ready_age = "", 0
        arm_call = call_ok and (smc_choch_buy or long_votes > 0)
        arm_put = put_ok and (smc_choch_sell or short_votes > 0)
        if arm_call and not arm_put and state.ready_side != "CALL":
            state.ready_side, state.ready_age = "CALL", 0
        elif arm_put and not arm_call and state.ready_side != "PUT":
            state.ready_side, state.ready_age = "PUT", 0
        structure_call = state.ready_side == "CALL" and call_ok and (smc_bos_buy or fresh_high and displacement_bull) and displacement_bull
        structure_put = state.ready_side == "PUT" and put_ok and (smc_bos_sell or fresh_low and displacement_bear) and displacement_bear

        prior_bearish = prior_close < prior_ema9 or prior_close < prior_ema21 or five_bear
        prior_bullish = prior_close > prior_ema9 or prior_close > prior_ema21 or five_bull
        cross_up_ema9 = prior_close <= prior_ema9 and current_close > current_ema9
        cross_down_ema9 = prior_close >= prior_ema9 and current_close < current_ema9
        reversal_call_trigger = smc_choch_buy or cross_up_ema9 or (fresh_high and prior_close <= prior_ema9)
        reversal_put_trigger = smc_choch_sell or cross_down_ema9 or (fresh_low and prior_close >= prior_ema9)
        reversal_call = prior_bearish and reversal_call_trigger and bull_candle and body_atr >= 0.55 and current_relvol >= 0.85 and current_close > current_ema9 and current_close > current_vwap and long_quality and room_call_ok and not call_exhausted
        reversal_put = prior_bullish and reversal_put_trigger and bear_candle and body_atr >= 0.55 and current_relvol >= 0.85 and current_close < current_ema9 and current_close < current_vwap and short_quality and room_put_ok and not put_exhausted
        previous_bear_regime = five_bear or prior_close < prior_ema21 or not bool(vidya_trend.iloc[-2])
        previous_bull_regime = five_bull or prior_close > prior_ema21 or bool(vidya_trend.iloc[-2])
        fast_break_high = current_close > float(frame["high"].iloc[-4:-1].max())
        fast_break_low = current_close < float(frame["low"].iloc[-4:-1].min())
        fast_rev_call = previous_bear_regime and bull_candle and body_atr >= 0.45 and current_relvol >= 0.70 and current_close > current_ema9 and (smc_choch_buy or fast_break_high or current_close > current_ema21 and prior_close <= prior_ema21) and room_call_ok and not call_exhausted
        fast_rev_put = previous_bull_regime and bear_candle and body_atr >= 0.45 and current_relvol >= 0.70 and current_close < current_ema9 and (smc_choch_sell or fast_break_low or current_close < current_ema21 and prior_close >= prior_ema21) and room_put_ok and not put_exhausted
        v0_call_strong = body_atr >= 0.75 and current_relvol >= 1.0 and (fast_break_high or smc_choch_buy)
        v0_put_strong = body_atr >= 0.75 and current_relvol >= 1.0 and (fast_break_low or smc_choch_sell)
        reversal_call = (reversal_call or fast_rev_call) and (long_votes > 0 or v0_call_strong)
        reversal_put = (reversal_put or fast_rev_put) and (short_votes > 0 or v0_put_strong)

        vidya_up = bool(vidya_trend.iloc[-1])
        if state.last_reversal_side == "PUT" and five_bull and current_close > current_ema21 and vidya_up:
            state.last_reversal_side = ""
        if state.last_reversal_side == "CALL" and five_bear and current_close < current_ema21 and not vidya_up:
            state.last_reversal_side = ""
        reversal_call = reversal_call and state.last_reversal_side != "CALL"
        reversal_put = reversal_put and state.last_reversal_side != "PUT"

        continuation_call_pullback = five_bull and current_close > current_ema21 and current_close > current_vwap and current_low <= current_ema9 + 0.35 * current_atr and current_low >= current_vwap - 0.35 * current_atr
        continuation_put_pullback = five_bear and current_close < current_ema21 and current_close < current_vwap and current_high >= current_ema9 - 0.35 * current_atr and current_high <= current_vwap + 0.35 * current_atr
        if state.loss_block_bar is not None and bar_time > state.loss_block_bar:
            if state.loss_block_side == "CALL" and (smc_choch_buy or continuation_call_pullback or fast_break_high):
                state.loss_block_side, state.loss_block_bar = "", None
            elif state.loss_block_side == "PUT" and (smc_choch_sell or continuation_put_pullback or fast_break_low):
                state.loss_block_side, state.loss_block_bar = "", None
        if state.runner_block_bar is not None and bar_time > state.runner_block_bar:
            if state.runner_block_side == "CALL" and (continuation_call_pullback or smc_choch_buy):
                state.runner_block_side, state.runner_block_bar = "", None
            elif state.runner_block_side == "PUT" and (continuation_put_pullback or smc_choch_sell):
                state.runner_block_side, state.runner_block_bar = "", None
        if continuation_call_pullback:
            state.continuation_side, state.continuation_age = "CALL", 0
        elif continuation_put_pullback:
            state.continuation_side, state.continuation_age = "PUT", 0
        if state.continuation_age > 8:
            state.continuation_side = ""
        continuation_call = state.continuation_side == "CALL" and five_bull and current_close > float(frame["high"].iloc[-2]) and current_close > current_ema9 and bull_candle and body_atr >= 0.30 and current_relvol >= 0.75 and call_ok
        continuation_put = state.continuation_side == "PUT" and five_bear and current_close < float(frame["low"].iloc[-2]) and current_close < current_ema9 and bear_candle and body_atr >= 0.30 and current_relvol >= 0.75 and put_ok

        bar_local = pd.Timestamp(bar_time)
        if bar_local.tzinfo is not None:
            bar_local = bar_local.tz_convert("America/Chicago")
        midday = 11 <= bar_local.hour < 13
        call_quality = call_mtf + min(long_votes, 2) + int(current_relvol >= 1.0) + int(not chop)
        put_quality = put_mtf + min(short_votes, 2) + int(current_relvol >= 1.0) + int(not chop)
        call_required = 4 + int(midday) + int(five_bear)
        put_required = 4 + int(midday) + int(five_bull)
        continuation_call = continuation_call and call_quality >= call_required and current_relvol >= 0.85
        continuation_put = continuation_put and put_quality >= put_required and current_relvol >= 0.85
        consensus_call, consensus_put = long_votes >= 2, short_votes >= 2
        context_call = long_votes >= 1 and five_bull and not chop
        context_put = short_votes >= 1 and five_bear and not chop

        call_allowed = state.loss_block_side != "CALL" and state.runner_block_side != "CALL"
        put_allowed = state.loss_block_side != "PUT" and state.runner_block_side != "PUT"
        call_candidate = call_allowed and (reversal_call or call_ok and (structure_call or continuation_call or consensus_call or context_call))
        put_candidate = put_allowed and (reversal_put or put_ok and (structure_put or continuation_put or consensus_put or context_put))
        selected_side = ""
        if call_candidate and not put_candidate:
            selected_side = "CALL"
        elif put_candidate and not call_candidate:
            selected_side = "PUT"
        elif call_candidate and put_candidate:
            if reversal_call != reversal_put:
                selected_side = "CALL" if reversal_call else "PUT"
            elif structure_call != structure_put:
                selected_side = "CALL" if structure_call else "PUT"
            elif continuation_call != continuation_put:
                selected_side = "CALL" if continuation_call else "PUT"
            elif consensus_call != consensus_put:
                selected_side = "CALL" if consensus_call else "PUT"
            elif long_votes != short_votes:
                selected_side = "CALL" if long_votes > short_votes else "PUT"
            else:
                selected_side = "CALL" if five_bull and not five_bear else "PUT"

        setup_flags = {
            "CALL": [reversal_call, structure_call, continuation_call, consensus_call, context_call],
            "PUT": [reversal_put, structure_put, continuation_put, consensus_put, context_put],
        }
        priorities = ["REV", "STRUCTURE", "CONT", "CONSENSUS", "CONTEXT"]
        selected_setup = ""
        if selected_side:
            selected_setup = next((priorities[index] for index, enabled in enumerate(setup_flags[selected_side]) if enabled), "")
        if state.age - state.last_entry_age < 5:
            selected_side, selected_setup = "", ""
        if selected_setup == "REV" and state.last_reversal_side == selected_side:
            selected_side, selected_setup = "", ""

        result = None
        if selected_side:
            risk_atr = float(five["atr"])
            exit_plan = create_pine_exit_plan(selected_side, current_close, risk_atr, frame, bar_time)
            if not exit_plan:
                return None, "unable to construct ATR/structure risk plan"
            exit_plan["setup_type"] = selected_setup
            result = {
                "side": selected_side,
                "setup": selected_setup,
                "long_votes": long_votes,
                "short_votes": short_votes,
                "call_mtf": call_mtf,
                "put_mtf": put_mtf,
                "atr": risk_atr,
                "entry_underlying": current_close,
                "entry_bar": str(bar_time),
                "exit_plan": exit_plan,
            }
            state.last_entry_age = state.age
            state.ready_side, state.ready_age = "", 0
            if selected_setup == "REV":
                state.last_reversal_side = selected_side
            if selected_setup == "CONT":
                state.continuation_side, state.continuation_age = "", 0

        return result, (
            f"setup={selected_setup or 'NONE'}, source_events={long_votes}/{short_votes}, "
            f"MTF={call_mtf}/{put_mtf}, room={call_room:.2f}/{put_room:.2f} ATR, "
            f"V2={'pass' if call_ok or put_ok else 'blocked'}"
        )


ulti7_entry_engine = PineV95EntryEngine()
