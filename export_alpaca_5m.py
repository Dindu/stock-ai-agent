"""Export read-only Alpaca 5-minute OHLCV bars for TradingView parity checks."""

from __future__ import annotations

import argparse
import os
from datetime import date, datetime, time, timezone
from pathlib import Path

import pandas as pd
from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from dotenv import load_dotenv


def _utc_midnight(value):
    return datetime.combine(value, time.min, tzinfo=timezone.utc)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="SPY")
    parser.add_argument("--start", type=date.fromisoformat, default=date(2026, 9, 15), help="UTC start date, inclusive")
    parser.add_argument("--end", type=date.fromisoformat, default=date(2026, 10, 6), help="UTC end date, exclusive")
    parser.add_argument("--feed", choices=("iex", "sip"), default=os.getenv("ALPACA_FEED", "iex").lower())
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    if args.start >= args.end:
        parser.error("--start must be earlier than --end")

    load_dotenv(dotenv_path=Path(".env"))
    api_key = os.getenv("ALPACA_API_KEY")
    secret_key = os.getenv("ALPACA_SECRET_KEY")
    if not api_key or not secret_key:
        raise SystemExit("ALPACA_API_KEY and ALPACA_SECRET_KEY must be set in .env or the environment.")

    try:
        feed = DataFeed(args.feed)
    except ValueError:
        raise SystemExit(f"Unsupported Alpaca data feed: {args.feed}") from None

    request = StockBarsRequest(
        symbol_or_symbols=args.symbol.upper(),
        timeframe=TimeFrame(5, TimeFrameUnit.Minute),
        start=_utc_midnight(args.start),
        end=_utc_midnight(args.end),
        feed=feed,
    )
    try:
        response = StockHistoricalDataClient(api_key, secret_key).get_stock_bars(request)
    except Exception as exc:
        raise SystemExit(f"Alpaca historical data request failed ({type(exc).__name__}); no orders were submitted.") from None
    if response.df is None or response.df.empty:
        raise SystemExit("Alpaca returned no bars for that symbol, date range, and feed.")

    bars = response.df
    if isinstance(bars.index, pd.MultiIndex):
        try:
            bars = bars.xs(args.symbol.upper(), level=0)
        except KeyError:
            raise SystemExit(f"Alpaca returned no bars for {args.symbol.upper()}.") from None
    bars.index = pd.to_datetime(bars.index, utc=True)
    bars.index.name = "timestamp"
    bars = bars.rename(columns={column: str(column).lower() for column in bars.columns})
    required = ["open", "high", "low", "close", "volume"]
    missing = [column for column in required if column not in bars.columns]
    if missing:
        raise SystemExit(f"Alpaca response is missing expected columns: {missing}")
    bars = bars[required].astype(float).sort_index()
    bars = bars.loc[~bars.index.duplicated(keep="last")]
    bars.insert(0, "symbol", args.symbol.upper())
    bars.insert(1, "feed", args.feed.upper())

    output = args.output or Path(
        f"alpaca_{args.symbol.lower()}_5m_{args.feed}_{args.start.isoformat()}_to_{args.end.isoformat()}.csv"
    )
    bars.to_csv(output, index_label="timestamp")
    print(f"Exported {len(bars)} {args.symbol.upper()} 5-minute {args.feed.upper()} bars to {output}")
    print(f"UTC range: {bars.index.min().isoformat()} through {bars.index.max().isoformat()}")
    print("This was a market-data-only request; no order APIs were called.")


if __name__ == "__main__":
    main()