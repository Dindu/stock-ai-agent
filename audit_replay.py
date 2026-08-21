"""Gate-by-gate historical replay: reconstructs what the bot's DIRECTION,
CONTEXT/CONVICTION, STRUCTURE (playbook) and TIMING (1m sniper) gates would
have produced at each 5-minute bar during today's session, using the exact
same functions the live bot calls (analyze, classify_entry_playbook,
one_minute_entry_timing, playbook_entry_ok).

NOTE / LIMITATIONS:
- SPY/QQQ macro-tape penalty and live option-chain delta (CONTRACT gate) are
  NOT replayed here: macro cache is populated by the live polling loop and
  historical options snapshots at a past instant aren't available via the API.
  Everything up through STRUCTURE + TIMING is a faithful replay of the real code.
- The bot's ignition/5m-delta history is wall-clock based (datetime.now(central)),
  so this script monkeypatches spy_options_poll_bot.datetime to a controllable
  clock and steps it forward one bar at a time to reproduce it correctly.

Usage: python3 audit_replay.py SYMBOL [YYYY-MM-DD]
"""
import sys
from datetime import datetime as real_datetime, timedelta, date as real_date
import pytz
import pandas as pd

import spy_options_poll_bot as bot

CENTRAL = bot.central
EASTERN = pytz.timezone("America/New_York")


class FakeDateTime(real_datetime):
    _now = None

    @classmethod
    def now(cls, tz=None):
        if cls._now is None:
            return real_datetime.now(tz)
        return cls._now.astimezone(tz) if tz else cls._now


bot.datetime = FakeDateTime


def set_clock(ts_central):
    FakeDateTime._now = ts_central


def diagnose_one_minute_call(bars_1m, five_min_data):
    """Mirror the CALL branch of one_minute_entry_timing(), but return every
    sub-condition individually so we can see exactly which one is failing."""
    if bars_1m is None or len(bars_1m) < 25:
        return {"insufficient_history": True}

    df = bars_1m.copy()
    df["EMA9"] = df["close"].ewm(span=9, adjust=False).mean()
    df["EMA20"] = df["close"].ewm(span=20, adjust=False).mean()
    df["VOL_AVG20"] = df["volume"].rolling(20).mean()
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    idx = df.index
    if getattr(idx, "tz", None) is not None:
        idx_et = idx.tz_convert(EASTERN)
    else:
        idx_et = idx.tz_localize("UTC").tz_convert(EASTERN)
    today_et = bot.datetime.now(EASTERN).date()
    session_mask = pd.Series([ts.date() == today_et for ts in idx_et], index=df.index, dtype=bool)
    vwap = pd.Series(float("nan"), index=df.index, dtype=float)
    if session_mask.any():
        tv = typical[session_mask] * df.loc[session_mask, "volume"]
        vwap[session_mask] = tv.cumsum() / df.loc[session_mask, "volume"].cumsum()
    df["VWAP"] = vwap

    latest = df.iloc[-1]
    prev = df.iloc[-2]
    recent = df.iloc[-(bot.ONE_MINUTE_MAX_TRIGGER_AGE_BARS + 1):]
    avg_vol = float(latest["VOL_AVG20"]) if pd.notna(latest["VOL_AVG20"]) else 0.0
    vol_ratio = float(latest["volume"] / avg_vol) if avg_vol > 0 else 1.0

    price = float(latest["close"])
    ema9 = float(latest["EMA9"])
    one_vwap = float(latest["VWAP"]) if pd.notna(latest["VWAP"]) else price
    ema9_slope = float(latest["EMA9"] - df["EMA9"].iloc[-4]) if len(df) >= 4 else 0.0
    bullish = price > float(latest["open"])

    five_price = float(five_min_data.get("price", price))
    five_vwap = float(five_min_data.get("vwap", price))
    five_ema20 = float(five_min_data.get("ema20", price))
    five_ema50 = float(five_min_data.get("ema50", price))

    five_aligned = five_price > five_vwap and five_price > five_ema20 and five_price > five_ema50
    micro_high = float(recent["high"].iloc[:-1].max()) if len(recent) > 1 else float(prev["high"])
    pullback_touch = float(recent["low"].min()) <= ema9 * (1.0 + bot.ONE_MINUTE_EMA9_TOUCH_TOLERANCE)
    reclaim_window = df.iloc[-bot.ONE_MINUTE_RECLAIM_WINDOW_BARS:]
    reclaim = bool(
        ((reclaim_window["close"] > reclaim_window["open"]) & (reclaim_window["close"] > micro_high)).any()
    ) and price > ema9
    near_vwap = abs(price - one_vwap) / max(price, 0.01) <= bot.ONE_MINUTE_LEVEL_RETEST_TOLERANCE
    level_retest = float(latest["low"]) <= micro_high * (1.0 + bot.ONE_MINUTE_LEVEL_RETEST_TOLERANCE) and price > micro_high and bullish
    compression_window = df.iloc[-4:-1]
    compression = False
    if len(compression_window) == 3:
        rng = (float(compression_window["high"].max()) - float(compression_window["low"].min())) / max(float(latest["close"]), 0.01)
        compression = rng <= bot.ONE_MINUTE_COMPRESSION_PCT
    trigger_move_pct = abs(price - float(prev["close"])) / max(abs(float(prev["close"])), 0.01) if float(prev["close"]) else 0.0

    return {
        "five_aligned": five_aligned,
        "ema9_slope_pos": ema9_slope > 0,
        "ema9_slope": ema9_slope,
        "pullback_touch": pullback_touch,
        "reclaim": reclaim,
        "near_vwap_or_above_5vwap": near_vwap or price > five_vwap,
        "vol_ratio_ok": vol_ratio >= bot.ONE_MINUTE_MIN_VOLUME_RATIO,
        "vol_ratio": vol_ratio,
        "trigger_move_ok": trigger_move_pct >= bot.ONE_MINUTE_MIN_TRIGGER_MOVE_PCT,
        "trigger_move_pct": trigger_move_pct,
        "level_retest": level_retest,
        "compression": compression,
    }


def main():
    symbol = sys.argv[1].upper() if len(sys.argv) > 1 else "PLTR"
    target_date = sys.argv[2] if len(sys.argv) > 2 else real_datetime.now(CENTRAL).strftime("%Y-%m-%d")
    target_date = real_datetime.strptime(target_date, "%Y-%m-%d").date()

    client = bot.StockHistoricalDataClient(bot.ALPACA_API_KEY, bot.ALPACA_SECRET_KEY)

    end = real_datetime.now(pytz.UTC)
    start = end - timedelta(days=6)
    req5 = bot.StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=bot.TimeFrame(5, bot.TimeFrameUnit.Minute),
        start=start, end=end, feed=bot.DataFeed(bot.FEED),
    )
    bars5 = client.get_stock_bars(req5).df
    if isinstance(bars5.index, pd.MultiIndex):
        bars5 = bars5.xs(symbol, level=0)
    bars5 = bars5[["open", "high", "low", "close", "volume"]]

    req1 = bot.StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=bot.TimeFrame(1, bot.TimeFrameUnit.Minute),
        start=start, end=end, feed=bot.DataFeed(bot.FEED),
    )
    bars1 = client.get_stock_bars(req1).df
    if isinstance(bars1.index, pd.MultiIndex):
        bars1 = bars1.xs(symbol, level=0)
    bars1 = bars1[["open", "high", "low", "close", "volume"]]

    idx5_central = bars5.index.tz_convert(CENTRAL)
    today_positions = [i for i, ts in enumerate(idx5_central) if ts.date() == target_date]
    if not today_positions:
        print(f"No {symbol} bars found for {target_date}.")
        return

    print(f"=== {symbol} gate-by-gate replay for {target_date} ===\n")
    print(f"{'Time CT':8} {'Bull':>4} {'Bear':>4} {'Tier':8} {'Signal':22} {'Playbook':22} {'1m Trigger':16} Verdict")
    print("-" * 120)

    for i in today_positions:
        if i < 55:
            continue
        slice5 = bars5.iloc[: i + 1].copy()
        bar_time_central = idx5_central[i]
        set_clock(bar_time_central)

        try:
            side, data = bot.analyze(slice5, client, symbol)
        except Exception as e:
            print(f"{bar_time_central:%H:%M}  analyze() error: {e}")
            continue
        if data is None:
            continue

        bull = data.get("bull_score", 0)
        bear = data.get("bear_score", 0)
        tier = "-"
        # side/tier/signal aren't returned directly; recover tier label heuristically.
        signal_label = side

        playbook_str = "-"
        trigger_str = "-"
        verdict = "no side / below floor"

        if side in ("CALL", "PUT"):
            playbook = bot.classify_entry_playbook(side, data)
            data["entry_playbook"] = playbook
            playbook_str = playbook or "NONE"

            mask1 = idx5_central.searchsorted  # unused placeholder
            bars1_upto = bars1[bars1.index <= bars5.index[i]].tail(bot.ONE_MINUTE_LOOKBACK_BARS + 5)
            trigger, trigger_reason = bot.one_minute_entry_timing(symbol, side, bars1_upto, data)
            data["one_minute_trigger"] = trigger or ""
            data["one_minute_entry_confirmed"] = bool(trigger)
            trigger_str = trigger or "WAIT"

            playbook_ok, playbook2, playbook_reason = bot.playbook_entry_ok(side, data, symbol)
            if playbook_ok:
                verdict = f"PASSED STRUCTURE+TIMING ({playbook2})"
            else:
                verdict = f"BLOCKED: {playbook_reason}"

            if side == "CALL" and not trigger:
                diag = diagnose_one_minute_call(bars1_upto, data)
                if diag.get("insufficient_history"):
                    verdict += " | 1m diag: insufficient 1m history"
                else:
                    failing = [k for k in (
                        "five_aligned", "ema9_slope_pos", "pullback_touch", "reclaim",
                        "near_vwap_or_above_5vwap", "vol_ratio_ok", "trigger_move_ok",
                    ) if not diag.get(k)]
                    verdict += (
                        f" | 1m diag: failing={failing or 'none(compression/retest path)'} "
                        f"vol_ratio={diag['vol_ratio']:.2f} ema9_slope={diag['ema9_slope']:+.4f} "
                        f"trigger_move={diag['trigger_move_pct']*100:.3f}%"
                    )
        else:
            verdict = "NO TRADE (score/dominance floor not met)"

        print(
            f"{bar_time_central:%H:%M}  {bull:4d} {bear:4d} {tier:8} {signal_label:22} "
            f"{playbook_str:22} {trigger_str:16} {verdict}"
        )


if __name__ == "__main__":
    main()
