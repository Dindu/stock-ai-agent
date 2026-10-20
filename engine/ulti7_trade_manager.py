"""Broker-independent ULTI-7 underlying trade-state simulation."""

from dataclasses import dataclass

import pandas as pd


@dataclass
class _Position:
    side: str
    entry_bar: pd.Timestamp
    entry_price: float
    atr: float
    stop: float
    initial_risk: float
    tp1: float
    tp2: float
    tp3: float
    remaining_fraction: float = 1.0
    realized_r: float = 0.0
    tp1_taken: bool = False
    tp2_taken: bool = False
    runner_active: bool = False
    last_bar: pd.Timestamp = None


class Ulti7UnderlyingTradeManager:
    """Simulate the Pine visual manager without placing or reconciling orders."""

    def __init__(self, runner_enabled=True, trail_lookback=3, trail_buffer_atr=0.10):
        self.runner_enabled = bool(runner_enabled)
        self.trail_lookback = int(trail_lookback)
        self.trail_buffer_atr = float(trail_buffer_atr)
        self.position = None

    def export_state(self):
        if self.position is None:
            return {"position": None}
        values = vars(self.position).copy()
        values["entry_bar"] = self.position.entry_bar.isoformat()
        values["last_bar"] = self.position.last_bar.isoformat() if self.position.last_bar is not None else None
        return {"position": values}

    def restore_state(self, snapshot):
        values = (snapshot or {}).get("position")
        if not isinstance(values, dict):
            self.position = None
            return
        restored = dict(values)
        restored["entry_bar"] = pd.Timestamp(restored["entry_bar"])
        if restored.get("last_bar"):
            restored["last_bar"] = pd.Timestamp(restored["last_bar"])
        self.position = _Position(**restored)

    @staticmethod
    def _timestamp(value, index):
        stamp = pd.Timestamp(value)
        if index.tz is None:
            return stamp.tz_localize(None) if stamp.tzinfo else stamp
        return stamp.tz_localize(index.tz) if stamp.tzinfo is None else stamp.tz_convert(index.tz)

    def open_from_signal(self, decision, bars, enabled=True):
        """Open a simulated position from an accepted signal, only while flat."""
        if not enabled or self.position is not None or not decision or bars is None:
            return None
        if not isinstance(bars.index, pd.DatetimeIndex) or len(bars) < 7:
            return None

        side = str(decision.get("side", "")).upper()
        if side not in ("CALL", "PUT"):
            return None
        entry_bar = self._timestamp(decision.get("entry_bar", bars.index[-1]), bars.index)
        entry_price = float(decision.get("entry_underlying", 0.0) or 0.0)
        atr = float(decision.get("atr", 0.0) or 0.0)
        structure = bars.loc[bars.index < entry_bar].tail(6)
        if entry_price <= 0 or atr <= 0 or len(structure) < 6:
            return None

        if side == "CALL":
            stop = min(entry_price - 0.50 * atr, float(structure["low"].min()) - 0.10 * atr)
            direction = 1.0
        else:
            stop = max(entry_price + 0.50 * atr, float(structure["high"].max()) + 0.10 * atr)
            direction = -1.0
        initial_risk = abs(entry_price - stop)
        if initial_risk <= 0:
            return None

        self.position = _Position(
            side=side,
            entry_bar=entry_bar,
            entry_price=entry_price,
            atr=atr,
            stop=stop,
            initial_risk=initial_risk,
            tp1=entry_price + direction * 0.75 * atr,
            tp2=entry_price + direction * 1.50 * atr,
            tp3=entry_price + direction * 2.25 * atr,
            last_bar=entry_bar,
        )
        return {
            "kind": "ENTRY",
            "side": side,
            "entry_price": entry_price,
            "stop": stop,
            "tp1": self.position.tp1,
            "tp2": self.position.tp2,
            "tp3": self.position.tp3,
            "scales": {"tp1": 0.50, "tp2": 0.15, "runner": 0.35},
        }

    def _record_fraction(self, position, fraction, price):
        direction = 1.0 if position.side == "CALL" else -1.0
        position.realized_r += fraction * direction * (price - position.entry_price) / position.initial_risk
        position.remaining_fraction = max(0.0, position.remaining_fraction - fraction)

    def _close_remaining(self, position, kind, price, reason):
        self._record_fraction(position, position.remaining_fraction, price)
        event = {
            "kind": kind,
            "reason": reason,
            "fill_price": float(price),
            "realized_r": position.realized_r,
            "is_loss": position.realized_r < 0,
            "runner_exit": position.runner_active,
            "pre_tp1": not position.tp1_taken,
            "side": position.side,
        }
        self.position = None
        return event

    def _late4(self, position, bars, after_entry):
        if position.tp1_taken or len(after_entry) < 4:
            return None
        from .ulti7_strategy import _pine_base_series

        features = _pine_base_series(bars)
        close = features["close"].iloc[-2:]
        prior_ema = features["ema20"].shift(1).iloc[-2:]
        prior_vwap = features["vwap"].shift(1).iloc[-2:]
        if position.side == "CALL":
            failed_twice = bool(((close < prior_ema) & (close < prior_vwap)).all())
        else:
            failed_twice = bool(((close > prior_ema) & (close > prior_vwap)).all())
        latest = bars.iloc[-1]
        touched_tp1 = float(latest["high"]) >= position.tp1 if position.side == "CALL" else float(latest["low"]) <= position.tp1
        if failed_twice and not touched_tp1:
            return self._close_remaining(position, "LATE4", float(latest["close"]), "PINE LATE4 EARLY FAIL")
        return None

    def advance(self, bars):
        """Process one new completed bar and return its state-transition events."""
        position = self.position
        if position is None or bars is None or not isinstance(bars.index, pd.DatetimeIndex) or bars.empty:
            return []
        current_bar = self._timestamp(bars.index[-1], bars.index)
        if current_bar <= position.entry_bar or current_bar == position.last_bar:
            return []
        position.last_bar = current_bar
        after_entry = bars.loc[bars.index > position.entry_bar]
        if after_entry.empty:
            return []

        latest = bars.iloc[-1]
        high, low, open_price = float(latest["high"]), float(latest["low"]), float(latest["open"])
        if position.tp2_taken and self.runner_enabled:
            previous = bars.iloc[-self.trail_lookback - 1:-1]
            if len(previous) == self.trail_lookback:
                if position.side == "CALL":
                    position.stop = max(position.stop, float(previous["low"].min()) - self.trail_buffer_atr * position.atr)
                else:
                    position.stop = min(position.stop, float(previous["high"].max()) + self.trail_buffer_atr * position.atr)

        stop_hit = low <= position.stop if position.side == "CALL" else high >= position.stop
        if stop_hit:
            gap_through = open_price <= position.stop if position.side == "CALL" else open_price >= position.stop
            fill = open_price if gap_through else position.stop
            reason = "PINE RUNNER TRAIL" if position.runner_active else "PINE ATR/STRUCTURE STOP"
            return [self._close_remaining(position, "STOP", fill, reason)]

        late4 = self._late4(position, bars, after_entry)
        if late4:
            return [late4]

        events = []
        hit_tp1 = high >= position.tp1 if position.side == "CALL" else low <= position.tp1
        hit_tp2 = high >= position.tp2 if position.side == "CALL" else low <= position.tp2
        hit_tp3 = high >= position.tp3 if position.side == "CALL" else low <= position.tp3
        if not position.tp1_taken and hit_tp1:
            self._record_fraction(position, 0.50, position.tp1)
            position.tp1_taken = True
            offset = 0.05 * position.atr
            position.stop = max(position.stop, position.entry_price + offset) if position.side == "CALL" else min(position.stop, position.entry_price - offset)
            events.append({"kind": "TP1", "fill_price": position.tp1, "realized_r": position.realized_r, "remaining_fraction": position.remaining_fraction})
        if position.tp1_taken and not position.tp2_taken and hit_tp2:
            self._record_fraction(position, 0.15, position.tp2)
            position.tp2_taken = True
            position.runner_active = self.runner_enabled
            events.append({"kind": "TP2", "fill_price": position.tp2, "realized_r": position.realized_r, "remaining_fraction": position.remaining_fraction, "runner_active": position.runner_active})
        if position.tp2_taken and not self.runner_enabled and hit_tp3:
            events.append(self._close_remaining(position, "TP3", position.tp3, "PINE FINAL TARGET"))
        return events


class Ulti7Strategy:
    """Coordinate accepted underlying signals with the simulated trade manager."""

    def __init__(self, entry_engine=None, trade_manager=None):
        if entry_engine is None:
            from .ulti7_strategy import PineV95EntryEngine

            entry_engine = PineV95EntryEngine()
        self.entry_engine = entry_engine
        self.trade_manager = trade_manager or Ulti7UnderlyingTradeManager()

    def export_state(self):
        entry_state = self.entry_engine.export_state() if hasattr(self.entry_engine, "export_state") else None
        return {"entry_engine": entry_state, "trade_manager": self.trade_manager.export_state()}

    def restore_state(self, snapshot):
        snapshot = snapshot or {}
        if snapshot.get("entry_engine") is not None and hasattr(self.entry_engine, "restore_state"):
            self.entry_engine.restore_state(snapshot["entry_engine"])
        self.trade_manager.restore_state(snapshot.get("trade_manager"))

    def on_completed_bars(self, symbol, bars, data=None, confluence=None, now=None, trades_enabled=True):
        """Evaluate signals first, then advance any managed trade for this bar."""
        decision, reason = self.entry_engine.evaluate(symbol, bars, data, confluence, now)
        entry_event = self.trade_manager.open_from_signal(decision, bars, enabled=trades_enabled) if decision else None
        trade_events = self.trade_manager.advance(bars)
        if hasattr(self.entry_engine, "record_exit"):
            for event in trade_events:
                loss_lock = (event["kind"] == "STOP" and event.get("pre_tp1")) or (event["kind"] == "LATE4" and event.get("is_loss"))
                runner_lock = event["kind"] == "STOP" and event.get("runner_exit")
                if loss_lock or runner_lock:
                    self.entry_engine.record_exit(
                        symbol,
                        event["side"],
                        is_loss=bool(loss_lock),
                        runner=bool(runner_lock),
                        bar_time=bars.index[-1],
                    )
        return {
            "signal": decision,
            "signal_reason": reason,
            "managed_entry": entry_event,
            "accepted_unmanaged": decision is not None and entry_event is None,
            "trade_events": trade_events,
        }