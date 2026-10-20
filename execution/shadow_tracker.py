"""Google Sheets-backed lifecycle tracking for alert-only ULTI-7 signals."""

import hashlib
import json
import threading
from datetime import datetime

import pandas as pd

from engine.ulti7_strategy import pine_exit_event


HEADERS = [
    "Signal ID", "Status", "Symbol", "Side", "Setup", "Signal Candle Open CT",
    "Signal Candle Close CT", "Entry Spot", "ATR", "TP1 Spot", "TP2 Spot", "TP3 Spot",
    "Initial Stop", "Current Stop", "Stage", "TP1 Hit At CT", "TP1 Spot Fill",
    "TP2 Hit At CT", "TP2 Spot Fill", "Exit At CT", "Exit Spot Fill", "Exit Reason",
    "Realized Underlying R", "Last Processed Candle CT", "State JSON",
]

_TERMINAL = {"CLOSED", "UNMANAGED_POSITION_OPEN"}


def signal_id(symbol, decision):
    identity = "|".join((
        str(symbol).upper(),
        str(decision.get("entry_bar", "")),
        str(decision.get("setup", "")),
        str(decision.get("side", "")),
    ))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


class Ulti7ShadowTracker:
    def __init__(self, worksheet, logger=None):
        self.worksheet = worksheet
        self.logger = logger or (lambda message: None)
        self._lock = threading.RLock()
        self._rows = {}
        self._trades = {}
        self.restore()

    @staticmethod
    def signal_key(symbol, decision):
        return signal_id(symbol, decision)

    @staticmethod
    def _timestamp(value):
        stamp = pd.Timestamp(value)
        return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")

    @classmethod
    def _ct(cls, value):
        if not value:
            return ""
        return cls._timestamp(value).tz_convert("America/Chicago").isoformat()

    @staticmethod
    def _json_default(value):
        if hasattr(value, "isoformat"):
            return value.isoformat()
        if hasattr(value, "item"):
            return value.item()
        raise TypeError(f"Cannot serialize {type(value).__name__}")

    def restore(self):
        with self._lock:
            try:
                records = self.worksheet.get_all_records()
            except Exception as exc:
                self.logger(f"ULTI-7 shadow sheet restore failed: {exc}")
                return
            for row_number, record in enumerate(records, start=2):
                identity = str(record.get("Signal ID", "") or "")
                if not identity:
                    continue
                self._rows[identity] = row_number
                if str(record.get("Status", "")).upper() in _TERMINAL:
                    continue
                raw_state = record.get("State JSON", "")
                if not raw_state:
                    continue
                try:
                    self._trades[identity] = json.loads(raw_state)
                except (TypeError, ValueError):
                    self.logger(f"ULTI-7 shadow row {row_number} has invalid State JSON.")

    def _values(self, trade):
        plan = trade.get("pine_exit_plan") or {}
        entry_bar = plan.get("entry_bar")
        close_bar = pd.Timestamp(entry_bar) + pd.Timedelta(minutes=5) if entry_bar else None
        return [
            trade.get("signal_id", ""), trade.get("status", ""), trade.get("underlying", ""),
            trade.get("side", ""), trade.get("setup_type", ""), self._ct(entry_bar), self._ct(close_bar),
            plan.get("entry_price", ""), plan.get("atr", ""), plan.get("tp1", ""), plan.get("tp2", ""),
            plan.get("tp3", ""), plan.get("initial_stop", plan.get("stop", "")), plan.get("stop", ""),
            trade.get("stage", "SIGNAL"), trade.get("tp1_hit_at", ""), trade.get("tp1_fill", ""),
            trade.get("tp2_hit_at", ""), trade.get("tp2_fill", ""), trade.get("exit_at", ""),
            trade.get("exit_fill", ""), trade.get("exit_reason", ""), trade.get("realized_r", 0.0),
            self._ct(trade.get("last_processed_bar")),
            json.dumps(trade, default=self._json_default, separators=(",", ":")),
        ]

    def _write(self, trade, new_row=False):
        values = self._values(trade)
        identity = trade["signal_id"]
        if new_row:
            response = self.worksheet.append_row(values, value_input_option="USER_ENTERED")
            updated_range = ((response or {}).get("updates") or {}).get("updatedRange", "")
            try:
                row_number = int(updated_range.rsplit("!", 1)[-1].split(":", 1)[0][1:])
            except (ValueError, IndexError):
                row_number = len(self.worksheet.col_values(1))
            self._rows[identity] = row_number
        else:
            row_number = self._rows[identity]
            last_column = chr(ord("A") + len(HEADERS) - 1)
            self.worksheet.update(
                range_name=f"A{row_number}:{last_column}{row_number}",
                values=[values],
                value_input_option="USER_ENTERED",
            )

    def add_signal(self, symbol, decision):
        with self._lock:
            identity = signal_id(symbol, decision)
            if identity in self._rows:
                return "DUPLICATE"
            active = next((
                trade for trade in self._trades.values()
                if trade.get("underlying") == str(symbol).upper() and trade.get("status") == "TRACKING"
            ), None)
            plan = dict(decision.get("exit_plan") or {})
            managed = active is None and bool(plan)
            trade = {
                "signal_id": identity,
                "underlying": str(symbol).upper(),
                "side": str(decision.get("side", "")).upper(),
                "setup_type": str(decision.get("setup", "")),
                "status": "TRACKING" if managed else "UNMANAGED_POSITION_OPEN",
                "stage": "ENTRY" if managed else "UNMANAGED",
                "realized_r": 0.0,
                "created_at": datetime.now().astimezone().isoformat(),
            }
            if managed:
                plan.update({"tp2_close_all": True, "tp1_taken": False, "tp2_taken": False, "runner_active": False, "last_bar": ""})
                trade.update({
                    "qty": 100,
                    "pine_original_qty": 100,
                    "pine_exit_plan": plan,
                    "last_processed_bar": plan.get("entry_bar", ""),
                })
            else:
                trade["pine_exit_plan"] = plan
                trade["exit_reason"] = "A shadow position is already tracking for this symbol" if active else "Missing signal exit plan"
            self._trades[identity] = trade
            self._write(trade, new_row=True)
            return trade["status"]

    @staticmethod
    def _fill_price(event, trade, latest):
        plan = trade["pine_exit_plan"]
        kind = event.get("kind")
        if kind == "EARLY_FAIL":
            return float(event.get("underlying_exit_price", latest["close"]))
        if kind == "TP1":
            return float(plan["tp1"])
        if kind in ("TP2", "TP3"):
            return float(plan["tp2"] if kind == "TP2" else plan["tp3"])
        if kind == "STOP":
            stop = float(plan["stop"])
            if trade["side"] == "CALL":
                return float(latest["open"]) if float(latest["open"]) <= stop else stop
            return float(latest["open"]) if float(latest["open"]) >= stop else stop
        return float(latest["close"])

    @staticmethod
    def _realize_r(trade, quantity, fill_price):
        plan = trade["pine_exit_plan"]
        risk = float(plan.get("initial_risk", 0.0) or 0.0)
        if risk <= 0:
            return
        direction = 1.0 if trade["side"] == "CALL" else -1.0
        fraction = float(quantity) / float(trade.get("pine_original_qty", 100) or 100)
        trade["realized_r"] = float(trade.get("realized_r", 0.0)) + fraction * direction * (float(fill_price) - float(plan["entry_price"])) / risk
        trade["qty"] = max(0, int(trade.get("qty", 0)) - int(quantity))

    def _apply_event(self, trade, event, latest, bar_time):
        plan = trade["pine_exit_plan"]
        kind = event.get("kind")
        fill_price = self._fill_price(event, trade, latest)
        quantity = max(0, min(int(trade.get("qty", 0)), int(event.get("close_qty", 0) or trade.get("qty", 0))))
        self._realize_r(trade, quantity, fill_price)
        trade["last_processed_bar"] = str(bar_time)
        if kind == "TP1":
            plan["tp1_taken"] = True
            offset = 0.05 * float(plan["atr"])
            entry = float(plan["entry_price"])
            plan["stop"] = max(float(plan["stop"]), entry + offset) if trade["side"] == "CALL" else min(float(plan["stop"]), entry - offset)
            trade["stage"] = "TP1"
            trade["tp1_hit_at"] = self._ct(bar_time)
            trade["tp1_fill"] = fill_price
        elif kind == "TP2":
            plan["tp2_taken"] = True
            trade["stage"] = "TP2"
            trade["tp2_hit_at"] = self._ct(bar_time)
            trade["tp2_fill"] = fill_price
            trade["status"] = "CLOSED" if event.get("final_close", True) else "TRACKING"
        else:
            trade["status"] = "CLOSED"
            trade["stage"] = kind
            trade["exit_at"] = self._ct(bar_time)
            trade["exit_fill"] = fill_price
            trade["exit_reason"] = event.get("reason", kind)

    def advance(self, symbol, completed_bars, now=None):
        if completed_bars is None or completed_bars.empty:
            return []
        events = []
        with self._lock:
            active = [
                trade for trade in self._trades.values()
                if trade.get("underlying") == str(symbol).upper() and trade.get("status") == "TRACKING"
            ]
            for trade in active:
                latest = completed_bars.iloc[-1]
                bar_time = completed_bars.index[-1]
                before = str(trade.get("last_processed_bar", ""))
                event = pine_exit_event(trade, completed_bars, now=now)
                plan = trade["pine_exit_plan"]
                if event:
                    if event.get("kind") == "TP1" and event.get("same_bar_tp2"):
                        tp2 = event["same_bar_tp2"]
                        self._apply_event(trade, event, latest, bar_time)
                        self._apply_event(trade, tp2, latest, bar_time)
                        events.extend([event, tp2])
                    else:
                        self._apply_event(trade, event, latest, bar_time)
                        events.append(event)
                    self._write(trade)
                elif before != str(bar_time):
                    trade["last_processed_bar"] = str(bar_time)
                    self._write(trade)
        return events

    def active_trades(self, symbol=None):
        with self._lock:
            return [
                trade for trade in self._trades.values()
                if trade.get("status") == "TRACKING"
                and (symbol is None or trade.get("underlying") == str(symbol).upper())
            ]