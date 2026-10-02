"""Compare Python ULTI-7 events with V9.5 TradingView data-window exports."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pandas as pd

from engine.ulti7_strategy import PineV95EntryEngine, pine_exit_event


SETUP_CODES = {"REV": 1, "STRUCTURE": 2, "CONT": 3, "CONSENSUS": 4, "CONTEXT": 5}


def _normalize_columns(frame):
    frame = frame.copy()
    frame.columns = [re.sub(r"[^a-z0-9]+", "_", str(column).strip().lower()).strip("_") for column in frame.columns]
    aliases = {
        "time": "timestamp",
        "date": "timestamp",
        "v95_parity_entry_side": "entry_side",
        "v95_parity_entry_setup": "entry_setup",
        "v95_parity_exit_code": "exit_code",
    }
    return frame.rename(columns={key: value for key, value in aliases.items() if key in frame.columns})


def _read_bars(path):
    frame = _normalize_columns(pd.read_csv(path))
    required = {"timestamp", "open", "high", "low", "close", "volume"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"bar file is missing columns: {missing}")
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    frame = frame.set_index("timestamp").sort_index()
    return frame[["open", "high", "low", "close", "volume"]].astype(float)


def _read_tradingview(path):
    frame = _normalize_columns(pd.read_csv(path))
    required = {"timestamp", "entry_side", "entry_setup", "exit_code"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(
            f"TradingView CSV must include timestamp and V95 Parity Entry Side/Setup/Exit Code plots; missing: {missing}"
        )
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    frame = frame.set_index("timestamp").sort_index()
    for column in ("entry_side", "entry_setup", "exit_code"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce").fillna(0).astype(int)
    return frame[["entry_side", "entry_setup", "exit_code"]]


def replay_python(bars, symbol):
    engine = PineV95EntryEngine()
    active_trade = None
    rows = []
    for position in range(len(bars)):
        current = bars.iloc[:position + 1]
        bar_time = current.index[-1]
        decision, _ = engine.evaluate(symbol, current, now=bar_time + pd.Timedelta(minutes=5))
        entry_side = 0
        entry_setup = 0
        exit_code = 0

        if active_trade is None and decision:
            active_trade = {
                "side": decision["side"],
                "qty": 100,
                "pine_original_qty": 100,
                "pine_exit_plan": dict(decision["exit_plan"]),
            }
            entry_side = 1 if decision["side"] == "CALL" else -1
            entry_setup = SETUP_CODES[decision["setup"]]

        if active_trade is not None:
            event = pine_exit_event(active_trade, current)
            if event:
                kind = event["kind"]
                exit_code = 5 if event["reason"] == "PINE RUNNER TRAIL" else 4 if kind == "STOP" else 6 if kind == "TP3" else 1 if kind == "TP1" else 2
                plan = active_trade["pine_exit_plan"]
                if kind == "TP1":
                    active_trade["qty"] -= int(event["close_qty"])
                    plan["tp1_taken"] = True
                    entry = float(plan["entry_price"])
                    atr = float(plan["atr"])
                    plan["stop"] = max(float(plan["stop"]), entry + 0.05 * atr) if active_trade["side"] == "CALL" else min(float(plan["stop"]), entry - 0.05 * atr)
                    same_bar_tp2 = event.get("same_bar_tp2")
                    if same_bar_tp2:
                        active_trade["qty"] -= int(same_bar_tp2["close_qty"])
                        plan["tp2_taken"] = True
                        plan["runner_active"] = True
                        exit_code = 3
                elif kind == "TP2":
                    active_trade["qty"] -= int(event["close_qty"])
                    plan["tp2_taken"] = True
                    plan["runner_active"] = True
                else:
                    closed_side = active_trade["side"]
                    if event["reason"] == "PINE ATR/STRUCTURE STOP" and not plan.get("tp1_taken"):
                        engine.record_exit(symbol, closed_side, is_loss=True, bar_time=str(bar_time))
                    if event["reason"] == "PINE RUNNER TRAIL":
                        engine.record_exit(symbol, closed_side, runner=True, bar_time=str(bar_time))
                    active_trade = None

        rows.append({"timestamp": bar_time, "entry_side": entry_side, "entry_setup": entry_setup, "exit_code": exit_code})
    result = pd.DataFrame(rows).set_index("timestamp")
    return result


def compare_events(python_events, tradingview_events):
    joined = python_events.add_prefix("python_").join(tradingview_events.add_prefix("tv_"), how="outer").fillna(0)
    joined = joined.astype(int)
    joined["entry_side_match"] = joined["python_entry_side"] == joined["tv_entry_side"]
    joined["entry_setup_match"] = joined["python_entry_setup"] == joined["tv_entry_setup"]
    joined["exit_match"] = joined["python_exit_code"] == joined["tv_exit_code"]
    joined["match"] = joined[["entry_side_match", "entry_setup_match", "exit_match"]].all(axis=1)
    return joined


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bars", required=True, help="5-minute OHLCV CSV, including warmup history")
    parser.add_argument("--tradingview", required=True, help="TradingView CSV with the three V95 Parity plots")
    parser.add_argument("--symbol", default="SPY")
    parser.add_argument("--output", default="ulti7_v95_parity_comparison.csv")
    args = parser.parse_args()

    bars = _read_bars(Path(args.bars))
    tradingview = _read_tradingview(Path(args.tradingview))
    python_events = replay_python(bars, args.symbol.upper())
    comparison = compare_events(python_events, tradingview)
    comparison.to_csv(args.output, index_label="timestamp")

    compared = comparison.index.isin(python_events.index) & comparison.index.isin(tradingview.index)
    comparable_rows = comparison.loc[compared]
    mismatches = comparable_rows.loc[~comparable_rows["match"]]
    missing_rows = len(comparison) - int(compared.sum())
    print(f"Compared {len(comparable_rows)} shared timestamps; {len(mismatches)} event mismatches; {missing_rows} timestamps missing on one side.")
    print(f"Wrote per-bar comparison to {args.output}")
    if len(mismatches):
        print("First mismatches:")
        print(mismatches.head(10).to_string())
    if len(mismatches) or missing_rows:
        raise SystemExit(1)
    print("All entry, setup, and exit events match on the shared 5-minute bars.")


if __name__ == "__main__":
    main()