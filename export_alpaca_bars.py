"""Export read-only Alpaca SPY 5-minute bars for V15 parity checks."""

import argparse
import os
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit


WORKSPACE = Path(__file__).resolve().parent


def _utc_timestamp(value):
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        return timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="SPY")
    parser.add_argument("--feed", choices=("iex", "sip"), default="iex")
    parser.add_argument("--start", default="2026-09-15T00:00:00Z", help="UTC start, inclusive")
    parser.add_argument("--end", default="2026-10-06T00:00:00Z", help="UTC end, exclusive")
    parser.add_argument(
        "--output",
        type=Path,
        help="Output CSV path (defaults to a feed-specific file in the workspace)",
    )
    args = parser.parse_args()

    load_dotenv(dotenv_path=WORKSPACE / ".env")
    key = os.getenv("ALPACA_API_KEY")
    secret = os.getenv("ALPACA_SECRET_KEY")
    if not key or not secret:
        parser.error("ALPACA_API_KEY and ALPACA_SECRET_KEY must be set in the workspace .env")

    start = _utc_timestamp(args.start)
    end = _utc_timestamp(args.end)
    if start >= end:
        parser.error("--start must be earlier than --end")

    output = args.output or WORKSPACE / f"alpaca_{args.symbol.lower()}_5m_{args.feed}_2026-09-15_to_2026-10-06.csv"

    request = StockBarsRequest(
        symbol_or_symbols=args.symbol.upper(),
        timeframe=TimeFrame(5, TimeFrameUnit.Minute),
        start=start.to_pydatetime(),
        end=end.to_pydatetime(),
        feed=DataFeed.SIP if args.feed == "sip" else DataFeed.IEX,
    )
    try:
        result = StockHistoricalDataClient(key, secret).get_stock_bars(request).df
    except Exception as error:
        raise SystemExit(f"Alpaca historical data request failed: {type(error).__name__}: {error}") from None
    if result is None or result.empty:
        raise SystemExit("Alpaca returned no bars for that symbol and date range.")

    if isinstance(result.index, pd.MultiIndex):
        result = result.xs(args.symbol.upper(), level=0)
    result.index = pd.to_datetime(result.index, utc=True)
    result = result.rename_axis("timestamp").reset_index()
    result.columns = [str(column).lower() for column in result.columns]
    result = result[["timestamp", "open", "high", "low", "close", "volume"]]
    output.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(output, index=False, date_format="%Y-%m-%dT%H:%M:%S%z")

    session = result[
        (result["timestamp"].dt.tz_convert("America/Chicago").dt.date == pd.Timestamp("2026-10-05").date())
    ]
    print(f"Exported {len(result)} {args.symbol.upper()} {args.feed.upper()} 5-minute bars to {output}")
    print(f"Oct 5 bars: {len(session)}")
    print("Columns: timestamp (UTC), open, high, low, close, volume")


if __name__ == "__main__":
    main()