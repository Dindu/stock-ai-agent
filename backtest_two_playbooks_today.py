#!/usr/bin/env python3
"""Replay today's two-playbook signals using completed Alpaca 5-minute bars.

This is an underlying-equivalent backtest, not an option-price backtest. The
project does not persist historical option contracts, quote spreads, fills, or
implied volatility, so it cannot honestly reconstruct realized option P&L.
"""

import argparse
import os
from datetime import datetime, time, timedelta
from pathlib import Path

import pandas as pd
import pytz
from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from dotenv import load_dotenv

import spy_options_poll_bot as bot


CENTRAL = pytz.timezone("America/Chicago")
EASTERN = pytz.timezone("America/New_York")
INITIAL_NOTIONAL = 10_000.0
OPENING_BLOCK_MINUTES = 15
CLOSING_BLOCK_MINUTES = 30
MAX_HOLD_MINUTES = 90
COOLDOWN_MINUTES = 30
TARGET_PCT = 0.20
STOP_PCT = 0.10
REQUIRE_SPY_ALIGNMENT_FOR_CONTINUATIONS = True
BLOCK_SPY_CHOP_CONTINUATIONS = True
SPY_CHOP_MAX_VWAP_DISTANCE_PCT = 0.0015
SPY_CHOP_MAX_EMA20_SLOPE_PCT = 0.00015


def _feed():
    return DataFeed(os.getenv("ALPACA_FEED", "iex").lower())


def fetch_bars(client, symbols, end_date, calendar_days, cache_path=None):
    cache_candidates = []
    if cache_path:
        cache_candidates = sorted(
            cache_path.parent.glob(f"backtest_bars_{_feed().value}_{end_date}_*d.pkl"),
            key=lambda path: int(path.stem.rsplit("_", 1)[-1][:-1]),
        )
    reusable_cache = next(
        (
            path
            for path in cache_candidates
            if int(path.stem.rsplit("_", 1)[-1][:-1]) >= calendar_days
        ),
        None,
    )
    if reusable_cache:
        frame = pd.read_pickle(reusable_cache)
    else:
        start = EASTERN.localize(datetime.combine(end_date - timedelta(days=calendar_days), time(9, 30))).astimezone(pytz.UTC)
        end = EASTERN.localize(datetime.combine(end_date, time(16, 0))).astimezone(pytz.UTC)
        request = StockBarsRequest(
            symbol_or_symbols=symbols,
            timeframe=TimeFrame(5, TimeFrameUnit.Minute),
            start=start,
            end=end,
            feed=_feed(),
        )
        frame = client.get_stock_bars(request).df
        if cache_path and frame is not None and not frame.empty:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            frame.to_pickle(cache_path)
    if frame is None or frame.empty:
        return {}
    result = {}
    for symbol in symbols:
        try:
            item = frame.xs(symbol, level=0).sort_index()
        except (KeyError, TypeError):
            continue
        if len(item) >= 55:
            result[symbol] = item
    return result


def calculate_backtest_indicators(frame):
    """Calculate indicators with VWAP reset for the replay bar's own session."""
    indicators = frame.copy()
    indicators["EMA20"] = indicators["close"].ewm(span=20, adjust=False).mean()
    indicators["EMA50"] = indicators["close"].ewm(span=50, adjust=False).mean()
    indicators["VOL_AVG"] = indicators["volume"].rolling(20).mean()
    indicators["RSI14"] = bot.calculate_rsi(indicators["close"], period=14)
    index_et = indicators.index.tz_convert(EASTERN)
    session_dates = pd.Series(index_et.date, index=indicators.index)
    typical = (indicators["high"] + indicators["low"] + indicators["close"]) / 3.0
    cumulative_value = (typical * indicators["volume"]).groupby(session_dates).cumsum()
    cumulative_volume = indicators["volume"].groupby(session_dates).cumsum()
    indicators["VWAP"] = cumulative_value / cumulative_volume.replace(0, float("nan"))
    return indicators


def technical_data(frame, indicators=None):
    indicators = calculate_backtest_indicators(frame) if indicators is None else indicators
    current = indicators.iloc[-1]
    prior = indicators.iloc[-2]
    price = float(current["close"])
    vwap = float(current["VWAP"])
    ema20 = float(current["EMA20"])
    ema50 = float(current["EMA50"])
    recent = indicators.iloc[-(bot.RECENT_HIGH_LOOKBACK + 1):-1]
    recent_high = float(recent["high"].max())
    recent_low = float(recent["low"].min())
    micro = indicators.iloc[-4:-1]
    micro_high = float(micro["high"].max())
    micro_low = float(micro["low"].min())
    bullish = price > float(current["open"])
    bearish = price < float(current["open"])
    rising = ema20 > float(indicators["EMA20"].iloc[-5])
    falling = ema20 < float(indicators["EMA20"].iloc[-5])
    vol_avg = float(current["VOL_AVG"])
    strong_volume = float(current["volume"]) > vol_avg * bot.VOLUME_MULTIPLIER
    moving_up = (price - vwap) > (float(prior["close"]) - float(prior["VWAP"]))
    moving_down = (price - vwap) < (float(prior["close"]) - float(prior["VWAP"]))

    bull = (
        (20 if price > vwap else 0)
        + (15 if price > ema20 else 0)
        + (15 if price > ema50 else 0)
        + (10 if rising else 0)
        + (15 if strong_volume and bullish else 0)
        + (15 if price > recent_high else 0)
        + (10 if moving_up else 0)
    )
    bear = (
        (20 if price < vwap else 0)
        + (15 if price < ema20 else 0)
        + (15 if price < ema50 else 0)
        + (10 if falling else 0)
        + (15 if strong_volume and bearish else 0)
        + (15 if price < recent_low else 0)
        + (10 if moving_down else 0)
    )
    return {
        "price": price,
        "vwap": vwap,
        "ema20": ema20,
        "ema50": ema50,
        "bull_score": bull,
        "bear_score": bear,
        "bullish_candle": bullish,
        "bearish_candle": bearish,
        "fresh_breakout": float(prior["close"]) <= recent_high and price > recent_high,
        "fresh_breakdown": float(prior["close"]) >= recent_low and price < recent_low,
        "micro_high": micro_high,
        "micro_low": micro_low,
        "prior_bar_high": float(prior["high"]),
        "prior_bar_low": float(prior["low"]),
        "vwap_extension_pct": abs((price - vwap) / vwap) if vwap else 0.0,
    }


def signal_candidate(symbol, frame, indicators, bar_index, history):
    slice_frame = frame.iloc[: bar_index + 1]
    data = technical_data(slice_frame, indicators.iloc[: bar_index + 1])
    data["bull_5m"] = history["bull_score"] if history else None
    data["bear_5m"] = history["bear_score"] if history else None
    diff = data["bull_score"] - data["bear_score"]
    side = "CALL" if diff >= bot.PLAYBOOK_MIN_DOMINANCE else "PUT" if -diff >= bot.PLAYBOOK_MIN_DOMINANCE else None
    if not side:
        return None, data
    passed, playbook, _ = bot.playbook_entry_ok(side, data)
    if not passed:
        return None, data
    return {"symbol": symbol, "side": side, "data": data, "playbook": playbook}, data


def market_alignment_ok(candidate, spy_frame, spy_indicators, timestamp):
    """Block continuation entries in an SPY-aligned but intraday-choppy tape."""
    if candidate["playbook"] != "PULLBACK_CONTINUATION":
        return True
    if candidate["symbol"] == "SPY":
        return True
    if spy_frame is None:
        return False
    index = spy_frame.index.get_indexer([timestamp], method="pad")[0]
    if index < 55:
        return False
    spy_slice = spy_frame.iloc[: index + 1]
    spy_indicator_slice = spy_indicators.iloc[: index + 1]
    spy_data = technical_data(spy_slice, spy_indicator_slice)
    vwap_distance = abs((spy_data["price"] - spy_data["vwap"]) / spy_data["vwap"]) if spy_data["vwap"] else 0.0
    ema20_now = float(spy_indicator_slice["EMA20"].iloc[-1])
    ema20_prior = float(spy_indicator_slice["EMA20"].iloc[-4])
    ema20_slope = abs((ema20_now - ema20_prior) / ema20_prior) if ema20_prior else 0.0
    if (
        BLOCK_SPY_CHOP_CONTINUATIONS
        and vwap_distance <= SPY_CHOP_MAX_VWAP_DISTANCE_PCT
        and ema20_slope <= SPY_CHOP_MAX_EMA20_SLOPE_PCT
    ):
        return False
    if not REQUIRE_SPY_ALIGNMENT_FOR_CONTINUATIONS:
        return True
    if candidate["side"] == "CALL":
        return spy_data["price"] > spy_data["vwap"] and spy_data["price"] > spy_data["ema20"]
    return spy_data["price"] < spy_data["vwap"] and spy_data["price"] < spy_data["ema20"]


def close_trade(trade, exit_price, timestamp, reason):
    direction = 1.0 if trade["side"] == "CALL" else -1.0
    pnl_pct = direction * ((exit_price / trade["entry"]) - 1.0)
    return {
        **trade,
        "exit": exit_price,
        "exited_at": timestamp,
        "reason": reason,
        "pnl_pct": pnl_pct,
        "pnl_dollars": INITIAL_NOTIONAL * pnl_pct,
    }


def run_symbol_backtest(
    symbol,
    frame,
    indicators,
    session_date,
    spy_frame=None,
    spy_indicators=None,
    block_breakout_puts=False,
    target_pct=TARGET_PCT,
    stop_pct=STOP_PCT,
    max_hold_minutes=MAX_HOLD_MINUTES,
):
    open_trade = None
    closed = []
    history = None
    cooldown_until = None
    session_rows = [
        row_index
        for row_index, timestamp in enumerate(frame.index)
        if timestamp.tz_convert(EASTERN).date() == session_date
    ]
    if not session_rows:
        return closed, None
    last_session_index = session_rows[-1]

    for bar_index in range(55, last_session_index):
        timestamp = frame.index[bar_index]
        timestamp_et = timestamp.tz_convert(EASTERN)
        if timestamp_et.date() != session_date:
            continue
        minutes = timestamp_et.hour * 60 + timestamp_et.minute
        market_open = 9 * 60 + 30
        market_close = 16 * 60

        if open_trade:
            bar = frame.iloc[bar_index]
            price = float(bar["close"])
            direction = 1.0 if open_trade["side"] == "CALL" else -1.0
            held = (timestamp - open_trade["entered_at"]).total_seconds() / 60.0
            entry = open_trade["entry"]
            target_price = entry * (1.0 + (direction * target_pct))
            stop_price = entry * (1.0 - (direction * stop_pct))
            target_hit = float(bar["high"]) >= target_price if direction > 0 else float(bar["low"]) <= target_price
            stop_hit = float(bar["low"]) <= stop_price if direction > 0 else float(bar["high"]) >= stop_price
            # Five-minute OHLC bars cannot order a target and stop within one bar.
            # Count that ambiguity as a stop to avoid overstating this proxy.
            if stop_hit:
                closed.append(close_trade(open_trade, stop_price, timestamp, "UNDERLYING_STOP"))
                open_trade = None
                cooldown_until = timestamp + timedelta(minutes=COOLDOWN_MINUTES)
            elif target_hit:
                closed.append(close_trade(open_trade, target_price, timestamp, "UNDERLYING_TARGET"))
                open_trade = None
                cooldown_until = timestamp + timedelta(minutes=COOLDOWN_MINUTES)
            elif held >= max_hold_minutes:
                closed.append(close_trade(open_trade, price, timestamp, "TIME_STOP"))
                open_trade = None
                cooldown_until = timestamp + timedelta(minutes=COOLDOWN_MINUTES)

        candidate, history = signal_candidate(symbol, frame, indicators, bar_index, history)
        can_enter = cooldown_until is None or timestamp >= cooldown_until
        if (
            open_trade is None
            and can_enter
            and market_open + OPENING_BLOCK_MINUTES <= minutes < market_close - CLOSING_BLOCK_MINUTES
        ):
            if (
                candidate
                and not (block_breakout_puts and candidate["playbook"] == "BREAKOUT" and candidate["side"] == "PUT")
                and market_alignment_ok(candidate, spy_frame, spy_indicators, timestamp)
            ):
                entry_bar = frame.iloc[bar_index + 1]
                open_trade = {
                    **candidate,
                    "entry": float(entry_bar["open"]),
                    "entered_at": frame.index[bar_index + 1],
                }

    if open_trade:
        open_trade = {
            **open_trade,
            "mark": float(frame.iloc[last_session_index]["close"]),
            "marked_at": frame.index[last_session_index],
        }
    return closed, open_trade


def run_backtest(symbol_frames, session_date, block_breakout_puts=False, target_pct=TARGET_PCT, stop_pct=STOP_PCT, max_hold_minutes=MAX_HOLD_MINUTES):
    trades = []
    open_positions = []
    spy_frame = symbol_frames.get("SPY")
    indicators_by_symbol = {
        symbol: calculate_backtest_indicators(frame)
        for symbol, frame in symbol_frames.items()
    }
    spy_indicators = indicators_by_symbol.get("SPY")
    for symbol, frame in symbol_frames.items():
        closed, open_trade = run_symbol_backtest(
            symbol,
            frame,
            indicators_by_symbol[symbol],
            session_date,
            spy_frame,
            spy_indicators,
            block_breakout_puts,
            target_pct,
            stop_pct,
            max_hold_minutes,
        )
        trades.extend(closed)
        if open_trade:
            open_positions.append(open_trade)
    return sorted(trades, key=lambda trade: trade["entered_at"]), sorted(open_positions, key=lambda trade: trade["entered_at"])


def available_sessions(symbol_frames, end_date, count):
    sessions = set()
    for frame in symbol_frames.values():
        sessions.update(ts.tz_convert(EASTERN).date() for ts in frame.index)
    eligible = sorted(day for day in sessions if day <= end_date)
    return eligible[-count:]


def print_session_results(session_date, trades, open_positions, detail):
    wins = [trade for trade in trades if trade["pnl_dollars"] > 0]
    losses = [trade for trade in trades if trade["pnl_dollars"] < 0]
    realized_total = sum(trade["pnl_dollars"] for trade in trades)
    unrealized_total = sum(
        INITIAL_NOTIONAL
        * (1.0 if trade["side"] == "CALL" else -1.0)
        * ((trade["mark"] / trade["entry"]) - 1.0)
        for trade in open_positions
    )
    print(
        f"{session_date} | closed={len(trades)} wins={len(wins)} losses={len(losses)} "
        f"win_rate={(len(wins) / len(trades) * 100) if trades else 0:.1f}% "
        f"realized=${realized_total:+,.2f} open={len(open_positions)} "
        f"mtm=${unrealized_total:+,.2f} combined=${realized_total + unrealized_total:+,.2f}"
    )
    if detail:
        for trade in trades:
            entered = trade["entered_at"].tz_convert(EASTERN).strftime("%H:%M")
            exited = trade["exited_at"].tz_convert(EASTERN).strftime("%H:%M")
            print(
                f"  {trade['symbol']} {trade['side']} {trade['playbook']} {entered}->{exited} "
                f"{trade['reason']} {trade['pnl_pct'] * 100:+.2f}% ${trade['pnl_dollars']:+,.2f}"
            )
    return wins, losses, realized_total, unrealized_total


def print_trade_attribution(trades):
    """Summarize closed outcomes by the entry dimensions used to form the signal."""
    buckets = {
        "playbook": {},
        "playbook_side": {},
        "side": {},
        "symbol": {},
        "entry_hour": {},
        "exit_reason": {},
        "playbook_exit_reason": {},
    }
    for trade in trades:
        entry_hour = trade["entered_at"].tz_convert(EASTERN).strftime("%H:00")
        values = {
            "playbook": trade["playbook"],
            "playbook_side": f"{trade['playbook']} {trade['side']}",
            "side": trade["side"],
            "symbol": trade["symbol"],
            "entry_hour": entry_hour,
            "exit_reason": trade["reason"],
            "playbook_exit_reason": f"{trade['playbook']} {trade['reason']}",
        }
        for dimension, label in values.items():
            record = buckets[dimension].setdefault(label, {"trades": 0, "wins": 0, "pnl": 0.0})
            record["trades"] += 1
            record["wins"] += int(trade["pnl_dollars"] > 0)
            record["pnl"] += trade["pnl_dollars"]

    print("LOSS ATTRIBUTION")
    for dimension, groups in buckets.items():
        print(f"  {dimension.upper()}")
        ordered = sorted(groups.items(), key=lambda item: item[1]["pnl"])
        for label, record in ordered:
            trades_count = record["trades"]
            win_rate = (record["wins"] / trades_count * 100.0) if trades_count else 0.0
            print(
                f"    {label}: trades={trades_count} win_rate={win_rate:.1f}% "
                f"pnl=${record['pnl']:+,.2f} avg=${record['pnl'] / trades_count:+,.2f}"
            )


def main():
    parser = argparse.ArgumentParser(description="Replay the two-playbook strategy on Alpaca 5-minute bars.")
    parser.add_argument("--days", type=int, default=1, help="Number of most recent market sessions to replay.")
    parser.add_argument("--detail", action="store_true", help="Print every closed trade.")
    parser.add_argument("--attribution", action="store_true", help="Print outcome breakdowns by playbook, side, symbol, and entry hour.")
    parser.add_argument("--no-cache", action="store_true", help="Download bars instead of reusing a matching local replay cache.")
    parser.add_argument("--block-breakout-puts", action="store_true", help="Backtest-only: exclude the negative breakout PUT setup.")
    parser.add_argument("--underlying-target-pct", type=float, default=TARGET_PCT, help="Backtest-only underlying target percentage.")
    parser.add_argument("--underlying-stop-pct", type=float, default=STOP_PCT, help="Backtest-only underlying stop percentage.")
    parser.add_argument("--max-hold-minutes", type=int, default=MAX_HOLD_MINUTES, help="Backtest-only maximum holding time.")
    args = parser.parse_args()
    load_dotenv()
    key = os.getenv("ALPACA_API_KEY")
    secret = os.getenv("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise RuntimeError("ALPACA_API_KEY and ALPACA_SECRET_KEY are required.")

    session_date = datetime.now(CENTRAL).date()
    symbols = list(dict.fromkeys(bot.SYMBOLS))
    client = StockHistoricalDataClient(key, secret)
    days = max(1, args.days)
    if args.underlying_target_pct <= 0 or args.underlying_stop_pct <= 0 or args.max_hold_minutes <= 0:
        raise ValueError("Underlying target, stop, and maximum hold must be positive.")
    calendar_days = max(10, (days * 3) + 10)
    cache_path = None if args.no_cache else Path("output") / f"backtest_bars_{_feed().value}_{session_date}_{calendar_days}d.pkl"
    frames = fetch_bars(client, symbols, session_date, calendar_days, cache_path)
    if not frames:
        raise RuntimeError(f"No usable 5-minute Alpaca bars returned through {session_date}.")

    sessions = available_sessions(frames, session_date, days)
    if len(sessions) < days:
        raise RuntimeError(f"Only {len(sessions)} market session(s) are available; requested {days}.")
    print(f"SESSIONS: {sessions[0]} through {sessions[-1]} ({len(sessions)} sessions)")
    print(
        "METHOD: underlying-equivalent replay; $10,000 notional per trade; next-bar-open entries; "
        f"target={args.underlying_target_pct:.2%}; stop={args.underlying_stop_pct:.2%}; "
        f"max_hold={args.max_hold_minutes}m; independent per symbol"
    )
    print(f"SYMBOLS WITH DATA: {','.join(sorted(frames))}")
    total_wins = total_losses = total_closed = total_open = 0
    total_realized = total_unrealized = 0.0
    all_closed_trades = []
    for day in sessions:
        trades, open_positions = run_backtest(
            frames,
            day,
            args.block_breakout_puts,
            args.underlying_target_pct,
            args.underlying_stop_pct,
            args.max_hold_minutes,
        )
        wins, losses, realized, unrealized = print_session_results(day, trades, open_positions, args.detail)
        total_wins += len(wins)
        total_losses += len(losses)
        total_closed += len(trades)
        total_open += len(open_positions)
        total_realized += realized
        total_unrealized += unrealized
        all_closed_trades.extend(trades)
    print(
        f"TOTAL | closed={total_closed} wins={total_wins} losses={total_losses} "
        f"win_rate={(total_wins / total_closed * 100) if total_closed else 0:.1f}% "
        f"realized=${total_realized:+,.2f} open={total_open} mtm=${total_unrealized:+,.2f} "
        f"combined=${total_realized + total_unrealized:+,.2f}"
    )
    if args.attribution:
        print_trade_attribution(all_closed_trades)


if __name__ == "__main__":
    main()