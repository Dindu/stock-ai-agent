"""Compare live TradingView V15 heartbeats with the Python shadow engine."""

from __future__ import annotations

import argparse
import csv
import hmac
import json
import math
import os
import queue
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pandas as pd

from engine.ulti7_strategy import PineV95EntryEngine, pine_exit_event


BAR_COLUMNS = ("open", "high", "low", "close", "volume")
SETUP_CODES = {"REV": 1, "STRUCTURE": 2, "CONT": 3, "CONSENSUS": 4, "CONTEXT": 5}
EXIT_CODES = {"EARLY_FAIL": 7, "TP1": 1, "TP2": 2, "TP3": 6}
TRADE_ALERTS = {"ENTRY", "TP1", "TP2", "TP2_RUNNER_START", "RUNNER_EXIT", "EARLY_FAIL_EXIT", "SL", "FINAL_TP"}


def read_v15p_bars(path, symbol, limit):
    messages = []
    with Path(path).open(newline="", encoding="utf-8-sig") as file:
        for row in csv.DictReader(file):
            messages.append(row["Message"])
    header_messages = [message for message in messages if message.startswith("V15P_HEADER|")]
    if not header_messages:
        raise ValueError("bootstrap CSV has no V15P_HEADER record")
    header = header_messages[-1].split("|")[1:]
    records = []
    for message in messages:
        if not message.startswith("V15P|"):
            continue
        values = message.split("|")[1:]
        if len(values) != len(header):
            continue
        row = dict(zip(header, values))
        if row.get("symbol", "").upper() != symbol.upper() or row.get("timeframe") != "5":
            continue
        timestamp = pd.to_datetime(int(row["time_ms"]), unit="ms", utc=True)
        records.append({column: float(row[column]) for column in BAR_COLUMNS} | {"timestamp": timestamp})
    if not records:
        raise ValueError(f"bootstrap CSV contains no 5-minute bars for {symbol}")
    frame = pd.DataFrame(records).drop_duplicates("timestamp", keep="last").set_index("timestamp").sort_index()
    return frame[list(BAR_COLUMNS)].tail(limit)


def _exit_code(event):
    kind = event["kind"]
    if kind == "STOP":
        return 5 if event["reason"] == "PINE RUNNER TRAIL" else 4
    return EXIT_CODES.get(kind, 0)


def _json_default(value):
    if hasattr(value, "isoformat"):
        return value.isoformat()
    raise TypeError(f"Cannot JSON encode {type(value).__name__}")


class ShadowRunner:
    def __init__(self, symbol, state_dir, bootstrap_path, history_limit):
        self.symbol = symbol.upper()
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.checkpoint_path = self.state_dir / "shadow_parity_checkpoint.json"
        self.engine = PineV95EntryEngine()
        self.active_trade = None
        self.bars = pd.DataFrame(columns=BAR_COLUMNS, dtype=float)
        self.last_bar = None
        if self.checkpoint_path.exists():
            self._restore()
        else:
            if not bootstrap_path:
                raise ValueError("--bootstrap-v15p is required when no checkpoint exists")
            source = read_v15p_bars(bootstrap_path, self.symbol, history_limit)
            self._replay_bootstrap(source)
            self._save_checkpoint()

    def _replay_bootstrap(self, source):
        self.bars = source.copy()
        for position in range(len(source)):
            current = source.iloc[: position + 1]
            if len(current) >= 30:
                self._advance(current, current.index[-1])
        self.last_bar = source.index[-1]

    def _restore(self):
        saved = json.loads(self.checkpoint_path.read_text(encoding="utf-8"))
        if saved.get("symbol") != self.symbol:
            raise ValueError("checkpoint symbol does not match --symbol")
        records = saved.get("bars", [])
        if records:
            frame = pd.DataFrame(records)
            frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
            self.bars = frame.set_index("timestamp")[list(BAR_COLUMNS)].sort_index()
        self.engine.restore_state(saved.get("engine", {}))
        self.active_trade = saved.get("active_trade")
        self.last_bar = pd.Timestamp(saved["last_bar"]) if saved.get("last_bar") else None

    def _save_checkpoint(self):
        records = self.bars.tail(600).rename_axis("timestamp").reset_index().to_dict("records")
        payload = {
            "version": 1,
            "symbol": self.symbol,
            "last_bar": self.last_bar,
            "bars": records,
            "engine": self.engine.export_state(),
            "active_trade": self.active_trade,
        }
        descriptor, temporary = tempfile.mkstemp(dir=self.state_dir, prefix="shadow-checkpoint-")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                json.dump(payload, file, default=_json_default)
            os.replace(temporary, self.checkpoint_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _append(self, name, record):
        with (self.state_dir / name).open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, default=_json_default, separators=(",", ":")) + "\n")

    def _advance(self, history, bar_time):
        now = bar_time + pd.Timedelta(minutes=5)
        decision, _ = self.engine.evaluate(self.symbol, history, now=now)
        entry_side = 0
        entry_setup = 0
        exit_code = 0
        if self.active_trade is None and decision:
            self.active_trade = {
                "side": decision["side"],
                "qty": 100,
                "pine_original_qty": 100,
                "pine_exit_plan": dict(decision["exit_plan"]),
            }
            entry_side = 1 if decision["side"] == "CALL" else -1
            entry_setup = SETUP_CODES[decision["setup"]]
        if self.active_trade is not None:
            event = pine_exit_event(self.active_trade, history, now=now)
            if event:
                exit_code = _exit_code(event)
                plan = self.active_trade["pine_exit_plan"]
                if event["kind"] == "TP1":
                    self.active_trade["qty"] -= int(event["close_qty"])
                    plan["tp1_taken"] = True
                    entry = float(plan["entry_price"])
                    atr = float(plan["atr"])
                    plan["stop"] = max(float(plan["stop"]), entry + 0.05 * atr) if self.active_trade["side"] == "CALL" else min(float(plan["stop"]), entry - 0.05 * atr)
                    same_bar_tp2 = event.get("same_bar_tp2")
                    if same_bar_tp2:
                        self.active_trade["qty"] -= int(same_bar_tp2["close_qty"])
                        plan["tp2_taken"] = True
                        plan["runner_active"] = True
                        exit_code = 3
                elif event["kind"] == "TP2":
                    self.active_trade["qty"] -= int(event["close_qty"])
                    plan["tp2_taken"] = True
                    plan["runner_active"] = True
                else:
                    side = self.active_trade["side"]
                    if event["reason"] == "PINE ATR/STRUCTURE STOP" and not plan.get("tp1_taken"):
                        self.engine.record_exit(self.symbol, side, is_loss=True, bar_time=bar_time)
                    elif event["kind"] == "EARLY_FAIL" and event["underlying_exit_price"] < plan["entry_price"]:
                        self.engine.record_exit(self.symbol, side, is_loss=True, bar_time=bar_time)
                    if event["reason"] == "PINE RUNNER TRAIL":
                        self.engine.record_exit(self.symbol, side, runner=True, bar_time=bar_time)
                    self.active_trade = None
        return {"entry_side": entry_side, "entry_setup": entry_setup, "exit_code": exit_code}

    def handle_bar(self, payload):
        bar_time = pd.to_datetime(int(payload["bar_time_ms"]), unit="ms", utc=True)
        if self.last_bar is not None and bar_time <= self.last_bar:
            return {"status": "duplicate_or_old", "bar_time": bar_time}
        bar = {column: float(payload[column]) for column in BAR_COLUMNS}
        new_row = pd.DataFrame([bar], index=pd.DatetimeIndex([bar_time], name="timestamp"))
        self.bars = pd.concat([self.bars, new_row]).loc[lambda frame: ~frame.index.duplicated(keep="last")].sort_index().tail(600)
        python = self._advance(self.bars, bar_time)
        tradingview = {key: int(payload[key]) for key in ("entry_side", "entry_setup", "exit_code")}
        matches = {key: python[key] == tradingview[key] for key in ("entry_side", "entry_setup", "exit_code")}
        result = {
            "symbol": self.symbol,
            "bar_time": bar_time,
            "python": python,
            "tradingview": tradingview,
            "matches": matches,
            "match": all(matches.values()),
        }
        self.last_bar = bar_time
        self._append("shadow_comparison.jsonl", result)
        self._save_checkpoint()
        return result


class ShadowHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class ShadowHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/health":
            self.send_error(404)
            return
        self._respond(200, {"ok": True, "service": "ulti7-shadow-parity", "symbol": self.server.symbol})

    def do_POST(self):
        expected_path = "/tv/" + self.server.token
        if not hmac.compare_digest(self.path, expected_path):
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 16384:
            self.send_error(413)
            return
        try:
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("JSON body must be an object")
            symbol = payload.get("symbol")
            if not isinstance(symbol, str) or symbol.upper() != self.server.symbol:
                raise ValueError("unexpected symbol")
            if payload.get("timeframe") != "5":
                raise ValueError("expected a 5-minute chart")
            event = payload.get("event")
            if event != "BAR" and event not in TRADE_ALERTS:
                raise ValueError("unknown event")
            if event == "BAR":
                required = (*BAR_COLUMNS, "bar_time_ms", "entry_side", "entry_setup", "exit_code")
                missing = [field for field in required if field not in payload]
                if missing:
                    raise ValueError(f"missing heartbeat fields: {missing}")
                numeric = [float(payload[field]) for field in BAR_COLUMNS]
                if not all(math.isfinite(value) for value in numeric):
                    raise ValueError("OHLCV fields must be finite numbers")
                int(payload["bar_time_ms"])
                for field in ("entry_side", "entry_setup", "exit_code"):
                    int(payload[field])
            self.server.events.put(payload)
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            self._respond(400, {"error": str(error)})
            return
        self._respond(202, {"accepted": True})

    def _respond(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bootstrap-v15p", type=Path, help="TradingView Pine Logs CSV used to warm the Python state")
    parser.add_argument("--symbol", default="SPY")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--state-dir", type=Path, default=Path("tradingview/state"))
    parser.add_argument("--history-bars", type=int, default=500)
    args = parser.parse_args()
    token = os.getenv("TV_SHADOW_TOKEN", "")
    if len(token) < 16:
        parser.error("set TV_SHADOW_TOKEN to a random value of at least 16 characters")
    if args.history_bars < 300:
        parser.error("--history-bars must be at least 300")

    runner = ShadowRunner(args.symbol, args.state_dir, args.bootstrap_v15p, args.history_bars)
    events = queue.Queue()
    server = ShadowHTTPServer((args.host, args.port), ShadowHandler)
    server.events = events
    server.token = token
    server.symbol = args.symbol.upper()
    Thread(target=server.serve_forever, daemon=True).start()
    print(f"Shadow parity listening on http://{args.host}:{args.port}/health for {args.symbol}; no orders enabled")
    try:
        while True:
            try:
                payload = events.get(timeout=1)
            except queue.Empty:
                continue
            if payload["event"] != "BAR":
                runner._append("shadow_tv_alerts.jsonl", payload)
                continue
            result = runner.handle_bar(payload)
            if result.get("status") == "duplicate_or_old":
                continue
            print(f"{result['bar_time'].isoformat()} match={result['match']} python={result['python']} tv={result['tradingview']}", flush=True)
    except KeyboardInterrupt:
        print("Stopping shadow parity receiver", flush=True)
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()