"""
7-Indicator Confluence — Python port of the ULTI-7 Pine merge (scripts/ULTI6_all_in_one_faithful.pine).

Ports the entry-relevant vote from each of the 7 source modules onto 5-minute bars:
  1. EMA9        — close vs EMA(9)
  2. VWAP        — close vs session VWAP
  3. Volume      — bull/bear volume spike (ratio vs 20-bar SMA)
  4. Poki        — WMA-diff oscillator regime vs linreg (bullishRule/bearishRule)
  5. Smart Money — EMA20/VWAP20 trend alignment (gainzalgo trend5M)
  6. UAlgo       — market structure state (BoS/CHoCH via zigzag pivot break)
  7. VIDYA       — Variable Index Dynamic Average trend (crossover of source vs ATR bands)

Each module casts a vote of +1 (bullish), -1 (bearish), or 0 (neutral). The net score
(-7..+7) and aligned-vote count drive the "technicals" score bucket and the entry gate.
Cache: per scan cycle (cleared alongside the accumulation cache).
"""

import math

import numpy as np
import pandas as pd
import yfinance as yf

_cache = {}  # {symbol: dict result} — cleared each scan cycle


def get_confluence(symbol):
    """Return the 7-indicator confluence result for `symbol`. Cached per scan cycle."""
    if symbol in _cache:
        return _cache[symbol]
    result = _fetch(symbol)
    _cache[symbol] = result
    return result


def clear_cache():
    """Call at the start of each scan cycle to ensure fresh data."""
    _cache.clear()


def _empty_result():
    return {
        "score": 0, "bull_votes": 0, "bear_votes": 0, "votes": {}, "description": "",
        "recovery": _empty_recovery(),
    }


def _fetch(symbol):
    try:
        df = yf.download(symbol, period="5d", interval="5m", progress=False, auto_adjust=False)
        if df is None or len(df) < 30:
            return _empty_result()

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
        if len(df) < 30:
            return _empty_result()
        return _calculate_confluence(df)
    except Exception:
        return _empty_result()


def get_confluence_from_bars(bars):
    """Calculate confluence from the active bot's existing 5-minute bars."""
    try:
        df = bars.rename(
            columns={
                "open": "Open", "high": "High", "low": "Low",
                "close": "Close", "volume": "Volume",
            }
        )
        df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
        if len(df) < 30:
            return _empty_result()
        return _calculate_confluence(df)
    except Exception:
        return _empty_result()


def _calculate_confluence(df):
        votes = {
            "ema":    _vote_ema(df),
            "vwap":   _vote_vwap(df),
            "volume": _vote_volume(df),
            "poki":   _vote_poki(df),
            "smc":    _vote_smc(df),
            "ualgo":  _vote_ualgo(df),
            "vidya":  _vote_vidya(df),
        }

        bull_votes = sum(1 for v in votes.values() if v > 0)
        bear_votes = sum(1 for v in votes.values() if v < 0)
        score = sum(votes.values())

        aligned = max(bull_votes, bear_votes)
        direction = "bullish" if bull_votes > bear_votes else "bearish" if bear_votes > bull_votes else "mixed"
        description = f"{aligned}/7 indicators {direction} (EMA/VWAP/Vol/Poki/SMC/UAlgo/VIDYA)"

        recovery = _recovery_entry_signal(df)

        return {
            "score": score, "bull_votes": bull_votes, "bear_votes": bear_votes,
            "votes": votes, "description": description, "recovery": recovery,
        }


def _empty_recovery():
    return {
        "stack": "neutral", "fresh": False, "buy_sell_ratio": 1.0, "reason": "insufficient data",
    }


def _recovery_entry_signal(df, ema_len=20, lookback=10, fresh_bars=6):
    """EMA20 vs VWAP structural stack + buy/sell volume pressure.

    A 'fresh recovery' is a stack that flipped bullish/bearish within the last
    `fresh_bars` bars (price just reclaimed structure); an older stack is
    'extended' and should require stronger confluence before chasing it.
    """
    ema20 = df["Close"].ewm(span=ema_len, adjust=False).mean()
    typical = (df["High"] + df["Low"] + df["Close"]) / 3.0
    session = df.index.date
    pv = (typical * df["Volume"]).groupby(session).cumsum()
    vv = df["Volume"].groupby(session).cumsum()
    vwap = pv / vv.replace(0, np.nan)

    stack_bull = ((ema20 > vwap) & (df["Close"] > ema20) & (df["Close"] > vwap)).to_numpy()
    stack_bear = ((ema20 < vwap) & (df["Close"] < ema20) & (df["Close"] < vwap)).to_numpy()

    if pd.isna(vwap.iloc[-1]):
        return _empty_recovery()

    def _consecutive_true_from_end(mask):
        count = 0
        for value in mask[::-1]:
            if not value:
                break
            count += 1
        return count

    if stack_bull[-1]:
        stack = "bullish"
        bars_since_flip = _consecutive_true_from_end(stack_bull)
    elif stack_bear[-1]:
        stack = "bearish"
        bars_since_flip = _consecutive_true_from_end(stack_bear)
    else:
        stack = "neutral"
        bars_since_flip = 0

    fresh = 0 < bars_since_flip <= fresh_bars

    window = df.iloc[-lookback:]
    rng = (window["High"] - window["Low"]).replace(0, np.nan)
    buy_vol = (window["Volume"] * (window["Close"] - window["Low"]) / rng).fillna(0).sum()
    sell_vol = (window["Volume"] * (window["High"] - window["Close"]) / rng).fillna(0).sum()
    buy_sell_ratio = buy_vol / sell_vol if sell_vol > 0 else float("inf") if buy_vol > 0 else 1.0

    reason = (
        f"EMA20/VWAP stack={stack}"
        + (f" (fresh, {int(bars_since_flip)} bars)" if fresh else f" (extended, {int(bars_since_flip)} bars)" if stack != "neutral" else "")
        + f", buy/sell vol={buy_sell_ratio:.2f}"
    )

    return {
        "stack": stack, "fresh": fresh, "buy_sell_ratio": round(min(buy_sell_ratio, 99.0), 2), "reason": reason,
    }


def _vote_ema(df, length=9):
    ema = df["Close"].ewm(span=length, adjust=False).mean()
    close = df["Close"].iloc[-1]
    return 1 if close > ema.iloc[-1] else -1 if close < ema.iloc[-1] else 0


def _vote_vwap(df):
    typical = (df["High"] + df["Low"] + df["Close"]) / 3.0
    session = df.index.date
    pv = (typical * df["Volume"]).groupby(session).cumsum()
    vv = df["Volume"].groupby(session).cumsum()
    vwap = pv / vv.replace(0, np.nan)
    close = df["Close"].iloc[-1]
    vw = vwap.iloc[-1]
    if pd.isna(vw):
        return 0
    return 1 if close > vw else -1 if close < vw else 0


def _vote_volume(df, ma_len=20, spike_mult=1.5):
    vol_ma = df["Volume"].rolling(ma_len).mean()
    if pd.isna(vol_ma.iloc[-1]) or vol_ma.iloc[-1] <= 0:
        return 0
    ratio = df["Volume"].iloc[-1] / vol_ma.iloc[-1]
    if ratio < spike_mult:
        return 0
    return 1 if df["Close"].iloc[-1] > df["Open"].iloc[-1] else -1 if df["Close"].iloc[-1] < df["Open"].iloc[-1] else 0


def _wma(series, length):
    weights = np.arange(1, length + 1)
    return series.rolling(length).apply(lambda x: np.dot(x, weights) / weights.sum(), raw=True)


def _linreg(series, length):
    def _slope_val(x):
        idx = np.arange(len(x))
        slope, intercept = np.polyfit(idx, x, 1)
        return slope * (len(x) - 1) + intercept
    return series.rolling(length).apply(_slope_val, raw=True)


def _vote_poki(df, length=32, linreg_len=50):
    close = df["Close"]
    n2ma = 2 * _wma(close, max(length // 2, 1))
    nma = _wma(close, length)
    diff = n2ma - nma
    sqn = max(int(round(math.sqrt(length))), 1)
    n1 = _wma(diff, sqn)
    lr = _linreg(close, linreg_len)

    n1_last, lr_last = n1.iloc[-1], lr.iloc[-1]
    if pd.isna(n1_last) or pd.isna(lr_last):
        return 0
    return 1 if n1_last > lr_last else -1


def _vote_smc(df, ema_len=20):
    ema = df["Close"].ewm(span=ema_len, adjust=False).mean()
    typical = (df["High"] + df["Low"] + df["Close"]) / 3.0
    session = df.index.date
    pv = (typical * df["Volume"]).groupby(session).cumsum()
    vv = df["Volume"].groupby(session).cumsum()
    vwap = pv / vv.replace(0, np.nan)

    close = df["Close"].iloc[-1]
    e, vw = ema.iloc[-1], vwap.iloc[-1]
    if pd.isna(vw):
        return 0
    if close > e and close > vw:
        return 1
    if close < e and close < vw:
        return -1
    return 0


def _vote_ualgo(df, zigzag_len=9):
    highs, lows, closes = df["High"].values, df["Low"].values, df["Close"].values
    n = len(closes)
    if n <= zigzag_len:
        return 0

    trend = 1
    last_state = None
    last_high_val, last_low_val = None, None

    for i in range(zigzag_len, n):
        to_up = highs[i - zigzag_len] >= np.max(highs[max(0, i - zigzag_len):i + 1])
        to_down = lows[i - zigzag_len] <= np.min(lows[max(0, i - zigzag_len):i + 1])
        prev_trend = trend
        if trend == 1 and to_down:
            trend = -1
        elif trend == -1 and to_up:
            trend = 1

        if trend != prev_trend and trend == 1:
            last_high_val = highs[i - zigzag_len]
        elif trend != prev_trend and trend == -1:
            last_low_val = lows[i - zigzag_len]

        if last_low_val is not None and closes[i] < last_low_val:
            last_state = "down"
        if last_high_val is not None and closes[i] > last_high_val:
            last_state = "up"

    return 1 if last_state == "up" else -1 if last_state == "down" else 0


def _vote_vidya(df, length=10, momentum=20, band_distance=2.0, atr_len=14):
    close = df["Close"].values
    high, low = df["High"].values, df["Low"].values
    n = len(close)
    if n <= max(momentum, atr_len) + 1:
        return 0

    change = np.diff(close, prepend=close[0])
    sum_pos = pd.Series(np.where(change >= 0, change, 0.0)).rolling(momentum).sum().values
    sum_neg = pd.Series(np.where(change >= 0, 0.0, -change)).rolling(momentum).sum().values
    denom = sum_pos + sum_neg
    with np.errstate(divide="ignore", invalid="ignore"):
        abs_cmo = np.abs(100 * (sum_pos - sum_neg) / denom)
    abs_cmo = np.nan_to_num(abs_cmo, nan=0.0)
    alpha = 2.0 / (length + 1)

    vidya = np.zeros(n)
    for i in range(1, n):
        a = alpha * abs_cmo[i] / 100.0
        vidya[i] = a * close[i] + (1 - a) * vidya[i - 1]
    vidya_smoothed = pd.Series(vidya).rolling(15).mean().values

    tr = np.maximum(high - low, np.maximum(np.abs(high - np.roll(close, 1)), np.abs(low - np.roll(close, 1))))
    tr[0] = high[0] - low[0]
    atr = pd.Series(tr).ewm(alpha=1 / atr_len, adjust=False).mean().values

    is_trend_up = False
    for i in range(atr_len + momentum, n):
        v = vidya_smoothed[i]
        if np.isnan(v):
            continue
        upper = v + atr[i] * band_distance
        lower = v - atr[i] * band_distance
        if close[i] > upper:
            is_trend_up = True
        elif close[i] < lower:
            is_trend_up = False

    return 1 if is_trend_up else -1
