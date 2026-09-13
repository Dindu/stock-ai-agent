"""Replay TradingView-exported OHLCV bars through the local Pine engine.

Usage:
    python -m tradingview.replay_csv --csv /path/to/NVDA_5m.csv

TradingView CSV exports normally contain: time, open, high, low, close, volume.
The time column may be Unix seconds/milliseconds or an ISO timestamp.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from engine.ulti_python import simulate


def _parse_time(values: pd.Series, timezone_name: str) -> pd.DatetimeIndex:
    numeric = pd.to_numeric(values, errors="coerce")
    if numeric.notna().all():
        unit = "ms" if numeric.abs().median() > 10**11 else "s"
        return pd.to_datetime(numeric, unit=unit, utc=True)

    parsed = pd.to_datetime(values, errors="raise")
    if parsed.dt.tz is None:
        parsed = parsed.dt.tz_localize(timezone_name)
    return parsed.dt.tz_convert("UTC")


def load_bars(path: Path, timezone_name: str) -> pd.DataFrame:
    frame = pd.read_csv(path)
    frame.columns = [str(column).strip().lower() for column in frame.columns]
    aliases = {"date": "time", "datetime": "time", "timestamp": "time"}
    frame = frame.rename(columns=aliases)
    required = {"time", "open", "high", "low", "close"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"CSV is missing required column(s): {', '.join(sorted(missing))}")
    if "volume" not in frame.columns:
        frame["volume"] = 0

    frame.index = _parse_time(frame["time"], timezone_name)
    bars = frame[["open", "high", "low", "close", "volume"]].copy()
    for column in bars.columns:
        bars[column] = pd.to_numeric(bars[column], errors="raise")
    bars = bars[~bars.index.duplicated(keep="last")].sort_index()
    return bars.dropna()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True, type=Path)
    parser.add_argument("--timezone", default="America/New_York", help="Timezone for CSV timestamps without an offset")
    parser.add_argument("--from-date", help="UTC-inclusive start, for example 2026-09-10")
    parser.add_argument("--to-date", help="UTC-exclusive end, for example 2026-09-12")
    parser.add_argument("--management", action="store_true", help="Enable Pine ULTI management exits")
    parser.add_argument("--veto", action="store_true", help="Enable Pine ULTI veto mode")
    args = parser.parse_args()

    bars = load_bars(args.csv, args.timezone)
    if args.from_date:
        bars = bars[bars.index >= pd.Timestamp(args.from_date, tz="UTC")]
    if args.to_date:
        bars = bars[bars.index < pd.Timestamp(args.to_date, tz="UTC")]
    if len(bars) < 55:
        raise SystemExit(f"Need at least 55 bars after filtering; found {len(bars)}")

    events = simulate(
        bars,
        config={"use_veto": args.veto, "use_management": args.management},
    )
    for event in events:
        display_time = pd.Timestamp(event["time"]).tz_convert("Etc/GMT+5")
        print(
            f"{display_time:%Y-%m-%d %H:%M} UTC-5 | "
            f"{event['event']:<10} | {event['side']:<4} | "
            f"${event['price']:.2f} | {event['reason_code']}"
        )
    print(f"Bars replayed: {len(bars)} | Entries: {sum(event['event'] == 'ENTRY' for event in events)}")


if __name__ == "__main__":
    main()