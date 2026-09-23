"""
ULTI-6 | Python strategy port of the supplied Pine Script v6

Purpose
-------
Standalone signal engine for integration into an existing trading bot.
The TradingView-only drawing/table/object code is intentionally omitted.
The trading decision path is preserved:

EMA + VWAP + SMC/Gainz + Poki + Volume + PAT
    -> bull/bear scores
    -> bull/bear ready
    -> final BUY / SELL transition signals

Input
-----
A pandas.DataFrame with at least:
    open, high, low, close, volume
DatetimeIndex is strongly recommended.

Multi-timeframe
---------------
For Pine request.security() equivalents, either:
1) pass a dict of pre-built timeframe DataFrames via evaluate(..., mtf={...}), or
2) let the strategy resample the base DataFrame when the index is a DatetimeIndex.

For closest TradingView matching, use the same vendor/session/timezone data that
TradingView uses and pass provider-native MTF frames with sufficient history.
For live parity of request.security() realtime behavior, set realtime_mtf=True.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Dict, Optional, Tuple, Any

import math
import numpy as np
import pandas as pd


# -----------------------------------------------------------------------------
# Pine-compatible numeric helpers
# -----------------------------------------------------------------------------

def nz(x: Any, replacement: float = 0.0):
    """Pine nz(): replace NaN/None with replacement."""
    if isinstance(x, pd.Series):
        return x.fillna(replacement)
    if x is None:
        return replacement
    try:
        if np.isnan(x):
            return replacement
    except TypeError:
        pass
    return x


def pine_sma(s: pd.Series, length: int) -> pd.Series:
    return s.rolling(length, min_periods=length).mean()


def pine_stdev(s: pd.Series, length: int) -> pd.Series:
    # TradingView ta.stdev is population-style over the window.
    return s.rolling(length, min_periods=length).std(ddof=0)


def pine_ema(s: pd.Series, length: int) -> pd.Series:
    """EMA with Pine-style alpha and recursive calculation."""
    out = pd.Series(np.nan, index=s.index, dtype=float)
    vals = s.to_numpy(dtype=float)
    alpha = 2.0 / (length + 1.0)
    prev = np.nan
    count = 0
    for i, x in enumerate(vals):
        if np.isnan(x):
            continue
        count += 1
        if count == 1:
            prev = x
        else:
            prev = alpha * x + (1.0 - alpha) * prev
        out.iloc[i] = prev
    return out


def pine_rma(s: pd.Series, length: int) -> pd.Series:
    """Wilder's RMA/SMMA."""
    out = pd.Series(np.nan, index=s.index, dtype=float)
    vals = s.to_numpy(dtype=float)
    prev = np.nan
    seed = []
    for i, x in enumerate(vals):
        if np.isnan(x):
            continue
        if np.isnan(prev):
            seed.append(x)
            if len(seed) < length:
                continue
            prev = float(np.mean(seed[-length:]))
        else:
            prev = ((length - 1.0) * prev + x) / length
        out.iloc[i] = prev
    return out


def pine_wma(s: pd.Series, length: int) -> pd.Series:
    weights = np.arange(1, length + 1, dtype=float)
    denom = weights.sum()
    return s.rolling(length, min_periods=length).apply(
        lambda x: float(np.dot(x, weights) / denom), raw=True
    )


def pine_vwma(src: pd.Series, volume: pd.Series, length: int) -> pd.Series:
    num = (src * volume).rolling(length, min_periods=length).sum()
    den = volume.rolling(length, min_periods=length).sum()
    return num / den.replace(0, np.nan)


def pine_atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return pine_rma(tr, length)


def pine_rsi(s: pd.Series, length: int = 14) -> pd.Series:
    change = s.diff()
    up = change.clip(lower=0)
    down = -change.clip(upper=0)
    avg_up = pine_rma(up, length)
    avg_down = pine_rma(down, length)
    rs = avg_up / avg_down.replace(0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    # Pine reaches 100 when average loss is zero and 0 when average gain is zero.
    rsi = rsi.where(~((avg_down == 0) & (avg_up > 0)), 100.0)
    rsi = rsi.where(~((avg_up == 0) & (avg_down > 0)), 0.0)
    return rsi


def pine_highest(s: pd.Series, length: int) -> pd.Series:
    return s.rolling(length, min_periods=1).max()


def pine_lowest(s: pd.Series, length: int) -> pd.Series:
    return s.rolling(length, min_periods=1).min()


def crossover(a: pd.Series, b: pd.Series) -> pd.Series:
    """Pine ta.crossover(a,b): current a>b and previous a<=b."""
    return (a > b) & (a.shift(1) <= b.shift(1))


def crossunder(a: pd.Series, b: pd.Series) -> pd.Series:
    """Pine ta.crossunder(a,b): current a<b and previous a>=b."""
    return (a < b) & (a.shift(1) >= b.shift(1))


def pivot_high(series: pd.Series, left: int, right: int) -> pd.Series:
    """Confirmed pivot highs; value is emitted on confirmation bar."""
    n = len(series)
    out = pd.Series(np.nan, index=series.index, dtype=float)
    vals = series.to_numpy(dtype=float)
    for i in range(left + right, n):
        pivot_i = i - right
        window = vals[pivot_i - left : pivot_i + right + 1]
        if np.isnan(window).any():
            continue
        center = vals[pivot_i]
        if center == np.max(window):
            # Pine pivots can be affected by ties; requiring center to be the
            # first maximum would differ. This keeps the common interpretation.
            out.iloc[i] = center
    return out


def pivot_low(series: pd.Series, left: int, right: int) -> pd.Series:
    n = len(series)
    out = pd.Series(np.nan, index=series.index, dtype=float)
    vals = series.to_numpy(dtype=float)
    for i in range(left + right, n):
        pivot_i = i - right
        window = vals[pivot_i - left : pivot_i + right + 1]
        if np.isnan(window).any():
            continue
        center = vals[pivot_i]
        if center == np.min(window):
            out.iloc[i] = center
    return out


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    return pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ], axis=1
    ).max(axis=1)


# -----------------------------------------------------------------------------
# Timeframe helpers / request.security approximation
# -----------------------------------------------------------------------------

_TF_RULES = {
    "1": "1min",
    "1M": "1min",       # Pine script uses 1M as one-minute option in SMC inputs.
    "5": "5min", "5M": "5min",
    "15": "15min", "15M": "15min",
    "30": "30min", "30M": "30min",
    "60": "60min", "1H": "60min",
    "240": "240min", "4H": "240min",
    "D": "1D",
    "W": "1W",
    "M": "1ME",
}


def _ensure_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    required = ["open", "high", "low", "close", "volume"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"Missing required columns: {missing}")
    out = df.copy()
    out.columns = [str(c).lower() for c in out.columns]
    if not isinstance(out.index, pd.DatetimeIndex):
        raise ValueError("DataFrame index must be a pandas DatetimeIndex for MTF logic.")
    out = out.sort_index()
    return out


def resample_ohlcv(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    rule = _TF_RULES.get(timeframe)
    if rule is None:
        raise ValueError(f"Unsupported timeframe: {timeframe}")
    agg = {
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }
    out = df.resample(rule, label="left", closed="left").agg(agg).dropna(subset=["open", "high", "low", "close"])
    return out


def _approx_seconds(tf: str) -> float:
    """Approximate timeframe duration in seconds for MTF direction decisions."""
    t = str(tf).upper()
    if t in {"1", "1M"}:
        return 60.0
    if t in {"5", "5M"}:
        return 5 * 60.0
    if t in {"15", "15M"}:
        return 15 * 60.0
    if t in {"30", "30M"}:
        return 30 * 60.0
    if t in {"60", "1H"}:
        return 60 * 60.0
    if t in {"240", "4H"}:
        return 240 * 60.0
    if t in {"D", "1D"}:
        return 86400.0
    if t == "W":
        return 7 * 86400.0
    if t == "M":
        return 30 * 86400.0
    raise ValueError(f"Unsupported timeframe: {tf}")


def _median_bar_seconds(index: pd.DatetimeIndex) -> Optional[float]:
    if len(index) < 2:
        return None
    values = index.view("int64") / 1e9
    diffs = np.diff(values)
    diffs = diffs[diffs > 0]
    return float(np.median(diffs)) if len(diffs) else None


def security_series(
    base_index: pd.DatetimeIndex,
    requested_df: pd.DataFrame,
    series: pd.Series,
    requested_tf: Optional[str] = None,
    realtime: bool = False,
) -> pd.Series:
    """Reproduce historical request.security(..., lookahead_off) mapping.

    Important Pine behavior:
      * Same timeframe: requested value maps to the current chart bar.
      * Higher timeframe: on historical chart bars, the current HTF value is
        visible only on the last chart bar inside that HTF bar; earlier chart
        bars continue to show the previous HTF value.
      * Lower timeframe: lookahead_off returns the last intrabar of the chart bar.
      * Realtime: the latest chart bar can see the currently developing HTF value.

    This function intentionally does not use the common but incorrect
    "shift HTF series and forward-fill" shortcut for historical HTF requests.
    """
    base_index = pd.DatetimeIndex(base_index)
    req = pd.Series(series.to_numpy(dtype=float), index=pd.DatetimeIndex(requested_df.index)).sort_index()
    req = req[~req.index.duplicated(keep="last")]
    if len(base_index) == 0 or len(req) == 0:
        return pd.Series(np.nan, index=base_index, dtype=float)

    base_sec = _median_bar_seconds(base_index)
    req_sec = _median_bar_seconds(req.index)
    if requested_tf is not None:
        req_sec = _approx_seconds(requested_tf)
    if base_sec is None:
        base_sec = req_sec
    if req_sec is None:
        req_sec = base_sec

    out = pd.Series(np.nan, index=base_index, dtype=float)

    # Same timeframe: direct timestamp alignment.
    if abs(req_sec - base_sec) <= max(1.0, base_sec * 0.01):
        out = req.reindex(base_index)
        if realtime:
            # Current chart bar may map to current same-TF value, which is already
            # what direct alignment does when the latest row is present.
            pass
        return out

    if req_sec > base_sec * 1.01:
        # HTF request with lookahead_off.
        req_starts = req.index
        # For each chart bar, locate the active HTF bar by its opening timestamp.
        group_pos = np.searchsorted(req_starts.asi8, base_index.asi8, side="right") - 1
        valid = group_pos >= 0
        group_pos = np.clip(group_pos, 0, len(req) - 1)

        # Index of the last chart bar belonging to each requested HTF bucket.
        last_base_by_group: Dict[int, int] = {}
        for i, gp in enumerate(group_pos):
            if valid[i]:
                last_base_by_group[int(gp)] = i

        req_vals = req.to_numpy(dtype=float)
        base_vals = np.full(len(base_index), np.nan, dtype=float)
        for i, gp in enumerate(group_pos):
            if not valid[i]:
                continue
            gp = int(gp)
            if realtime and i == len(base_index) - 1:
                # On realtime bars Pine can expose the developing HTF value.
                base_vals[i] = req_vals[gp]
            elif i == last_base_by_group.get(gp):
                # Historical lookahead_off: current HTF value becomes visible on
                # the last chart bar inside the HTF interval.
                base_vals[i] = req_vals[gp]
            else:
                prev_gp = gp - 1
                base_vals[i] = req_vals[prev_gp] if prev_gp >= 0 else np.nan
        return pd.Series(base_vals, index=base_index)

    # LTF request with lookahead_off: return the last lower-TF bar contained in
    # each chart bar. The requested series should normally already be calculated
    # in the lower-TF context by the caller.
    req_times = req.index.asi8
    base_times = base_index.asi8
    base_vals = np.full(len(base_index), np.nan, dtype=float)
    for i, t in enumerate(base_times):
        next_t = base_times[i + 1] if i + 1 < len(base_times) else t + int(base_sec * 1e9)
        lo = np.searchsorted(req_times, t, side="left")
        hi = np.searchsorted(req_times, next_t, side="left") - 1
        if hi >= lo and hi >= 0 and hi < len(req):
            base_vals[i] = req.iloc[hi]
    return pd.Series(base_vals, index=base_index)


def prepare_mtf(base: pd.DataFrame, tf: str) -> pd.DataFrame:
    """Build an MTF OHLCV frame when an explicit provider frame is unavailable.

    For intraday U.S. equity data, the 30-minute offset keeps common bars aligned
    with the 09:30 exchange open. For daily/weekly/monthly contexts, period-based
    aggregation is used. For highest parity, pass provider-native MTF data instead.
    """
    base = _ensure_ohlcv(base)
    t = str(tf).upper()
    intraday = {"1", "1M", "5", "5M", "15", "15M", "30", "30M", "60", "1H", "240", "4H"}
    if t in intraday:
        minutes = int(round(_approx_seconds(t) / 60.0))
        if minutes == 1:
            return base.copy()
        rule = f"{minutes}min"
        return base.resample(
            rule, origin="start_day", offset="30min", label="left", closed="left"
        ).agg({
            "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"
        }).dropna(subset=["open", "high", "low", "close"])
    if t == "D":
        # TradingView's exchange-day context is normally calendar-day grouped for
        # US equities when the input is regular-session OHLCV.
        return base.groupby(base.index.normalize()).agg({
            "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"
        })
    if t == "W":
        return base.resample("W-MON", label="left", closed="left").agg({
            "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"
        }).dropna(subset=["open", "high", "low", "close"])
    if t == "M":
        return base.resample("MS", label="left", closed="left").agg({
            "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"
        }).dropna(subset=["open", "high", "low", "close"])
    raise ValueError(f"Unsupported timeframe: {tf}")


# -----------------------------------------------------------------------------
# VWAP
# -----------------------------------------------------------------------------

def anchored_vwap(df: pd.DataFrame, src: pd.Series, anchor: str = "Session") -> Tuple[pd.Series, pd.Series]:
    """Anchored VWAP plus population-style weighted stdev.

    Returns (vwap, upper_1sigma). Event anchors are not inferable from ordinary
    OHLCV data, so Earnings/Dividends/Splits require external event timestamps.
    """
    idx = df.index
    if anchor == "Session":
        key = idx.normalize()
    elif anchor == "Week":
        key = idx.to_period("W").start_time
    elif anchor == "Month":
        key = idx.to_period("M").start_time
    elif anchor == "Quarter":
        key = pd.PeriodIndex(idx, freq="Q").start_time
    elif anchor == "Year":
        key = idx.to_period("Y").start_time
    elif anchor == "Decade":
        key = pd.to_datetime([f"{(x.year // 10) * 10}-01-01" for x in idx])
    elif anchor == "Century":
        key = pd.to_datetime([f"{(x.year // 100) * 100}-01-01" for x in idx])
    elif anchor in {"Earnings", "Dividends", "Splits"}:
        # Without event data, keep one continuous anchor rather than inventing events.
        key = pd.Series(pd.Timestamp("1900-01-01"), index=idx)
    else:
        key = idx.normalize()

    volume = nz(df["volume"], 0.0).astype(float)
    pv = src.astype(float) * volume
    group = pd.Series(key, index=idx)

    cum_pv = pv.groupby(group, sort=False).cumsum()
    cum_v = volume.groupby(group, sort=False).cumsum()
    vwap = cum_pv / cum_v.replace(0, np.nan)

    # Weighted variance around anchored VWAP.
    dev2 = ((src - vwap) ** 2) * volume
    cum_dev2 = dev2.groupby(group, sort=False).cumsum()
    variance = cum_dev2 / cum_v.replace(0, np.nan)
    stdev = np.sqrt(variance.clip(lower=0))
    upper = vwap + stdev
    return vwap, upper


# -----------------------------------------------------------------------------
# SAR
# -----------------------------------------------------------------------------

def pine_sar(high: pd.Series, low: pd.Series, close: pd.Series, start: float, step: float, maximum: float) -> pd.Series:
    """Reproduce the standard Pine parabolic SAR state machine closely."""
    h = high.to_numpy(dtype=float)
    l = low.to_numpy(dtype=float)
    c = close.to_numpy(dtype=float)
    out = np.full(len(h), np.nan, dtype=float)
    if len(h) < 2:
        return pd.Series(out, index=high.index)

    # Matches the standard Pine implementation pattern: initialize on bar 1
    # using the direction of close[1] vs close[0].
    is_below = bool(c[1] > c[0])
    max_min = h[1] if is_below else l[1]
    result = l[0] if is_below else h[0]
    acceleration = float(start)
    out[1] = result

    for i in range(2, len(h)):
        result = result + acceleration * (max_min - result)

        if is_below:
            result = min(result, l[i - 1])
            result = min(result, l[i - 2])
            if l[i] < result:
                is_below = False
                result = max_min
                max_min = l[i]
                acceleration = start
            elif h[i] > max_min:
                max_min = h[i]
                acceleration = min(acceleration + step, maximum)
        else:
            result = max(result, h[i - 1])
            result = max(result, h[i - 2])
            if h[i] > result:
                is_below = True
                result = max_min
                max_min = h[i]
                acceleration = start
            elif l[i] < max_min:
                max_min = l[i]
                acceleration = min(acceleration + step, maximum)
        out[i] = result
    return pd.Series(out, index=high.index)


# -----------------------------------------------------------------------------
# Input configuration
# -----------------------------------------------------------------------------

@dataclass
class ULTI6Config:
    # Master
    use_confluence: bool = False
    min_confluence: int = 70
    confirmed_only: bool = True
    signal_on_change_only: bool = True

    # EMA
    ema_len: int = 9
    ema_smooth_type: str = "None"
    ema_smooth_len: int = 14
    ema_bb_mult: float = 2.0

    # VWAP
    hide_vwap_dwm: bool = False
    vwap_anchor: str = "Session"
    vwap_calc_mode: str = "Standard Deviation"
    vwap_mult1: float = 1.0
    vwap_mult2: float = 2.0
    vwap_mult3: float = 3.0
    # Chart/runtime context
    realtime_mtf: bool = False
    chart_timezone: str = "America/New_York"
    chart_session_start: str = "09:30"
    chart_session_end: str = "16:00"

    # SMC
    smc_pivot_len: int = 5
    smc_momentum_base: float = 0.01
    smc_min_signal_distance: int = 5
    smc_short_period: int = 30
    smc_long_period: int = 100
    smc_use_momentum: bool = True
    smc_use_trend: bool = True
    smc_higher_tf: str = "5M"
    smc_use_lower: bool = True
    smc_lower_tf: str = "5M"
    smc_use_volume: bool = True
    smc_use_breakout: bool = True
    smc_breakout_period: int = 5
    smc_restrict_repeated: bool = True
    smc_restrict_tf: str = "5M"
    smc_enable_market_profile: bool = True
    smc_enable_divergence: bool = True
    smc_volume_long: int = 50
    smc_volume_short: int = 5

    # Poki
    poki_start: float = 0.03
    poki_step: float = 0.03
    poki_max: float = 0.30
    poki_reg_len: int = 50
    poki_bb_len: int = 20
    poki_bb_mult: float = 2.0
    poki_overbought: float = 1.0
    poki_oversold: float = 0.0
    poki_renko_method: str = "ATR"
    poki_renko_value: float = 14.0
    poki_price_source: str = "Close"
    poki_oscillating: bool = True
    poki_normalize: bool = False
    poki_hull_len: int = 32

    # Volume
    vol_ma_len: int = 20
    vol_spike_mult: float = 1.5

    # PAT
    pat_fvg_min_pct: float = 0.10
    pat_max_zones: int = 8
    pat_structure_len: int = 5
    pat_eq_tolerance: float = 0.15
    pat_pd_lookback: int = 50
    pat_vi_threshold: float = 0.25


@dataclass
class ULTI6Result:
    timestamp: Any
    signal: str
    bull_score: float
    bear_score: float
    ema_bull: bool
    ema_bear: bool
    vwap_bull: bool
    vwap_bear: bool
    smc_bull: bool
    smc_bear: bool
    poki_bull: bool
    poki_bear: bool
    volume_bull: bool
    volume_bear: bool
    pat_bull: bool
    pat_bear: bool
    smc_trend_strength: float
    smc_confidence: float


# -----------------------------------------------------------------------------
# Main strategy
# -----------------------------------------------------------------------------

class ULTI6Strategy:
    def __init__(self, config: Optional[ULTI6Config] = None):
        self.cfg = config or ULTI6Config()

    # -----------------------
    # MTF helpers
    # -----------------------
    def _mtf_frames(self, df: pd.DataFrame, mtf: Optional[Dict[str, pd.DataFrame]]) -> Dict[str, pd.DataFrame]:
        aliases = {"1M": "1", "5M": "5", "15M": "15", "30M": "30", "1H": "60", "4H": "240"}
        frames = {} if mtf is None else {
            aliases.get(str(k).upper(), str(k).upper()): _ensure_ohlcv(v)
            for k, v in mtf.items()
        }
        needed = {"1", "5", "15", "30", "60", "240", "D"}
        for tf in needed:
            if tf not in frames:
                alias = {value: key for key, value in aliases.items()}.get(tf)
                if alias and alias in frames:
                    frames[tf] = frames[alias]
                else:
                    frames[tf] = prepare_mtf(df, tf)
        return frames

    def _mtf_ema_vwap(self, base_index, frames):
        result = {}
        realtime = self.cfg.realtime_mtf
        tf_display = {"1": "1", "5": "5", "15": "15", "30": "30", "60": "60", "240": "240", "D": "D"}
        for tf, frame in frames.items():
            ema = pine_ema(frame["close"], 20)
            src = (frame["high"] + frame["low"] + frame["close"]) / 3.0
            vwap, _ = anchored_vwap(frame, src, "Session")
            result[tf] = (
                security_series(base_index, frame, ema, requested_tf=tf_display[tf], realtime=realtime),
                security_series(base_index, frame, vwap, requested_tf=tf_display[tf], realtime=realtime),
            )
        return result
    # -----------------------
    # Main calculation
    # -----------------------
    def calculate(self, data: pd.DataFrame, mtf: Optional[Dict[str, pd.DataFrame]] = None) -> pd.DataFrame:
        df = _ensure_ohlcv(data)
        c = self.cfg
        n = len(df)
        if n == 0:
            return pd.DataFrame(index=df.index)

        # EMA section ---------------------------------------------------------
        ema_src = df["close"]
        ema_value = pine_ema(ema_src, c.ema_len)
        if c.ema_smooth_type == "SMA" or c.ema_smooth_type == "SMA + Bollinger Bands":
            ema_smooth = pine_sma(ema_value, c.ema_smooth_len)
        elif c.ema_smooth_type == "EMA":
            ema_smooth = pine_ema(ema_value, c.ema_smooth_len)
        elif c.ema_smooth_type == "SMMA (RMA)":
            ema_smooth = pine_rma(ema_value, c.ema_smooth_len)
        elif c.ema_smooth_type == "WMA":
            ema_smooth = pine_wma(ema_value, c.ema_smooth_len)
        elif c.ema_smooth_type == "VWMA":
            ema_smooth = pine_vwma(ema_value, df["volume"], c.ema_smooth_len)
        else:
            ema_smooth = pd.Series(np.nan, index=df.index)
        ema_bb_dev = (
            pine_stdev(ema_value, c.ema_smooth_len) * c.ema_bb_mult
            if c.ema_smooth_type == "SMA + Bollinger Bands"
            else pd.Series(np.nan, index=df.index)
        )

        # VWAP ----------------------------------------------------------------
        vwap_src = (df["high"] + df["low"] + df["close"]) / 3.0
        vwap_value, vwap_stdev_upper = anchored_vwap(df, vwap_src, c.vwap_anchor)
        vwap_stdev_abs = vwap_stdev_upper - vwap_value
        if c.vwap_calc_mode == "Standard Deviation":
            vwap_basis = vwap_stdev_abs
        else:
            vwap_basis = vwap_value * 0.01
        vwap_upper1 = vwap_value + vwap_basis * c.vwap_mult1
        vwap_lower1 = vwap_value - vwap_basis * c.vwap_mult1
        vwap_upper2 = vwap_value + vwap_basis * c.vwap_mult2
        vwap_lower2 = vwap_value - vwap_basis * c.vwap_mult2
        vwap_upper3 = vwap_value + vwap_basis * c.vwap_mult3
        vwap_lower3 = vwap_value - vwap_basis * c.vwap_mult3

        # SMC -----------------------------------------------------------------
        atr14 = pine_atr(df, 14)
        vol_factor = atr14 / df["close"].replace(0, np.nan)
        vol_factor = vol_factor.fillna(0.0)
        momentum_threshold = c.smc_momentum_base * (1.0 + vol_factor * 2.0)
        pre_threshold = momentum_threshold * 0.5 * (1.0 - vol_factor * 0.5)
        prev_close = df["close"].shift(1)
        price_change = np.where(
            prev_close.ne(0), ((df["close"] - prev_close) / prev_close) * 100.0, 0.0
        )
        price_change = pd.Series(price_change, index=df.index)

        smc_ph = pivot_high(df["high"], c.smc_pivot_len, c.smc_pivot_len)
        smc_pl = pivot_low(df["low"], c.smc_pivot_len, c.smc_pivot_len)
        last_high = []
        last_low = []
        lh = np.nan
        ll = np.nan
        for ph, pl in zip(smc_ph, smc_pl):
            if not np.isnan(ph):
                lh = ph
            if not np.isnan(pl):
                ll = pl
            last_high.append(lh)
            last_low.append(ll)
        smc_last_high = pd.Series(last_high, index=df.index)
        smc_last_low = pd.Series(last_low, index=df.index)

        frames = self._mtf_frames(df, mtf)
        mtf_values = self._mtf_ema_vwap(df.index, frames)
        smc_e1, smc_v1 = mtf_values["1"]
        smc_e5, smc_v5 = mtf_values["5"]
        smc_e15, smc_v15 = mtf_values["15"]
        smc_e30, smc_v30 = mtf_values["30"]
        smc_e1h, smc_v1h = mtf_values["60"]
        smc_e4h, smc_v4h = mtf_values["240"]
        smc_ed, smc_vd = mtf_values["D"]

        smc_trend1 = np.where((df.close > smc_e1) & (df.close > smc_v1), 1, np.where((df.close < smc_e1) & (df.close < smc_v1), -1, 0))
        smc_trend5 = np.where((df.close > smc_e5) & (df.close > smc_v5), 1, np.where((df.close < smc_e5) & (df.close < smc_v5), -1, 0))
        smc_trend15 = np.where((df.close > smc_e15) & (df.close > smc_v15), 1, np.where((df.close < smc_e15) & (df.close < smc_v15), -1, 0))
        smc_trend30 = np.where((df.close > smc_e30) & (df.close > smc_v30), 1, np.where((df.close < smc_e30) & (df.close < smc_v30), -1, 0))
        smc_trend1h = np.where((df.close > smc_e1h) & (df.close > smc_v1h), 1, np.where((df.close < smc_e1h) & (df.close < smc_v1h), -1, 0))
        smc_trend4h = np.where((df.close > smc_e4h) & (df.close > smc_v4h), 1, np.where((df.close < smc_e4h) & (df.close < smc_v4h), -1, 0))
        smc_trendd = np.where((df.close > smc_ed) & (df.close > smc_vd), 1, np.where((df.close < smc_ed) & (df.close < smc_vd), -1, 0))

        trend_raw = smc_trend1 + smc_trend5 + smc_trend15 + smc_trend30 + smc_trend1h + smc_trend4h + smc_trendd
        trend_strength = trend_raw / 7.0 * 100.0
        confidence = np.where(np.abs(trend_raw) == 7, 90.0, np.where(np.abs(trend_raw) >= 4, 75.0, np.where(np.abs(trend_raw) >= 2, 60.0, 50.0)))

        def get_tf_trend(tf: str) -> np.ndarray:
            return {
                "1M": smc_trend1,
                "5M": smc_trend5,
                "15M": smc_trend15,
                "30M": smc_trend30,
                "1H": smc_trend1h,
                "4H": smc_trend4h,
                "D": smc_trendd,
            }.get(tf, smc_trendd)

        higher_trend = get_tf_trend(c.smc_higher_tf)
        lower_trend = get_tf_trend(c.smc_lower_tf)
        restrict_trend = get_tf_trend(c.smc_restrict_tf)

        vol_avg = pine_sma(df["volume"], c.smc_volume_long)
        vol_short = pine_sma(df["volume"], c.smc_volume_short)
        vol_condition = (df["volume"] > vol_avg) & (vol_short.diff() > 0)
        highest_breakout = pine_highest(df["high"], c.smc_breakout_period)
        lowest_breakout = pine_lowest(df["low"], c.smc_breakout_period)

        smc_buy_allowed = (
            (~pd.Series(c.smc_use_momentum, index=df.index) | (price_change > momentum_threshold))
            & (~pd.Series(c.smc_use_trend, index=df.index) | (higher_trend == 1))
            & (~pd.Series(c.smc_use_lower, index=df.index) | (lower_trend != -1))
            & (~pd.Series(c.smc_use_volume, index=df.index) | vol_condition)
            & (~pd.Series(c.smc_use_breakout, index=df.index) | (df["close"] > highest_breakout.shift(1)))
        )
        smc_sell_allowed = (
            (~pd.Series(c.smc_use_momentum, index=df.index) | (price_change < -momentum_threshold))
            & (~pd.Series(c.smc_use_trend, index=df.index) | (higher_trend == -1))
            & (~pd.Series(c.smc_use_lower, index=df.index) | (lower_trend != 1))
            & (~pd.Series(c.smc_use_volume, index=df.index) | vol_condition)
            & (~pd.Series(c.smc_use_breakout, index=df.index) | (df["close"] < lowest_breakout.shift(1)))
        )

        smc_buy = np.zeros(n, dtype=bool)
        smc_sell = np.zeros(n, dtype=bool)
        smc_last_bar = -100000
        smc_last_sig = "Neutral"
        for i in range(n):
            buy = bool(smc_buy_allowed.iloc[i]) and (i - smc_last_bar >= c.smc_min_signal_distance) and (not c.smc_restrict_repeated or smc_last_sig != "Buy" or restrict_trend[i] != 1)
            sell = bool(smc_sell_allowed.iloc[i]) and (i - smc_last_bar >= c.smc_min_signal_distance) and (not c.smc_restrict_repeated or smc_last_sig != "Sell" or restrict_trend[i] != -1)
            smc_buy[i] = buy
            smc_sell[i] = sell
            if buy:
                smc_last_bar = i
                smc_last_sig = "Buy"
            if sell:
                smc_last_bar = i
                smc_last_sig = "Sell"
        smc_buy = pd.Series(smc_buy, index=df.index)
        smc_sell = pd.Series(smc_sell, index=df.index)

        smc_choch_sell = (~smc_last_high.isna()) & crossunder(df["low"], smc_last_high) & (df["close"] < df["open"])
        smc_choch_buy = (~smc_last_low.isna()) & crossover(df["high"], smc_last_low) & (df["close"] > df["open"])
        smc_bos_sell = (~smc_last_low.shift(1).isna()) & crossunder(df["low"], smc_last_low.shift(1)) & (df["low"] < smc_last_low.shift(1)) & (df["close"] < df["open"])
        smc_bos_buy = (~smc_last_high.shift(1).isna()) & crossover(df["high"], smc_last_high.shift(1)) & (df["high"] > smc_last_high.shift(1)) & (df["close"] > df["open"])

        raw_cvd = (np.where(df["close"] > prev_close, nz(df["volume"], 0.0), np.where(df["close"] < prev_close, -nz(df["volume"], 0.0), 0.0))).cumsum()
        recent_buy_vol = np.where(df["close"] > df["open"], pine_sma(df["volume"], 20), 0.0)
        recent_sell_vol = np.where(df["close"] < df["open"], pine_sma(df["volume"], 20), 0.0)
        vol_ratio = np.full(n, 0.5, dtype=float)
        valid_ratio = (recent_buy_vol > 0) & (recent_sell_vol > 0)
        denom = recent_buy_vol + recent_sell_vol
        np.divide(recent_buy_vol, denom, out=vol_ratio, where=valid_ratio)
        strong_buy_flow = pd.Series(c.smc_enable_market_profile & (vol_ratio > 0.65) & (df["volume"] > vol_avg * 1.5), index=df.index)
        strong_sell_flow = pd.Series(c.smc_enable_market_profile & (vol_ratio < 0.35) & (df["volume"] > vol_avg * 1.5), index=df.index)

        smc_rsi = pine_rsi(df["close"], 14)
        bull_div = pd.Series(
            c.smc_enable_divergence
            & (df["low"] < df["low"].shift(5))
            & (df["low"].shift(5) < df["low"].shift(10))
            & (smc_rsi > smc_rsi.shift(5))
            & (smc_rsi.shift(5) > smc_rsi.shift(10))
            & (smc_rsi < 40),
            index=df.index,
        )
        bear_div = pd.Series(
            c.smc_enable_divergence
            & (df["high"] > df["high"].shift(5))
            & (df["high"].shift(5) > df["high"].shift(10))
            & (smc_rsi < smc_rsi.shift(5))
            & (smc_rsi.shift(5) < smc_rsi.shift(10))
            & (smc_rsi > 60),
            index=df.index,
        )
        ready_buy = pd.Series(c.smc_use_momentum, index=df.index) & (price_change > pre_threshold) & (price_change < momentum_threshold) & (higher_trend == 1)
        ready_sell = pd.Series(c.smc_use_momentum, index=df.index) & (price_change < -pre_threshold) & (price_change > -momentum_threshold) & (higher_trend == -1)

        smc_bull_score = smc_buy | smc_choch_buy | smc_bos_buy | strong_buy_flow | bull_div
        smc_bear_score = smc_sell | smc_choch_sell | smc_bos_sell | strong_sell_flow | bear_div

        # Poki ----------------------------------------------------------------
        poki_sar = pine_sar(df["high"], df["low"], df["close"], c.poki_start, c.poki_step, c.poki_max)
        # ta.linreg(source, length, 0) = intercept + slope * (length - 1),
        # with x = 0..length-1 in the current rolling window.
        def _pine_linreg(series: pd.Series, length: int) -> pd.Series:
            x = np.arange(length, dtype=float)
            sx = x.sum()
            sxx = np.dot(x, x)
            denom = length * sxx - sx * sx
            def _calc(y):
                sy = np.sum(y)
                sxy = np.dot(x, y)
                slope = (length * sxy - sx * sy) / denom
                intercept = (sy - slope * sx) / length
                return intercept + slope * (length - 1)
            return series.rolling(length, min_periods=length).apply(_calc, raw=True)

        poki_reg = _pine_linreg(df["close"], c.poki_reg_len)
        close_prev_src = df["close"].shift(1)
        poki_reg_prev = _pine_linreg(close_prev_src, c.poki_reg_len)
        poki_slope = poki_reg - poki_reg_prev
        poki_bb_basis = pine_sma(df["close"], c.poki_bb_len)
        poki_bb_dev = pine_stdev(df["close"], c.poki_bb_len)
        poki_upper = poki_bb_basis + c.poki_bb_mult * poki_bb_dev
        poki_lower = poki_bb_basis - c.poki_bb_mult * poki_bb_dev
        poki_bbr = (df["close"] - poki_lower) / np.maximum(poki_upper - poki_lower, np.finfo(float).eps)

        poki_renko_atr_len = max(1, int(round(c.poki_renko_value)))
        poki_renko_size = np.where(
            c.poki_renko_method == "ATR",
            pine_atr(df, poki_renko_atr_len),
            np.where(c.poki_renko_method == "Part of Price", df["close"] / max(c.poki_renko_value, 1), c.poki_renko_value),
        )
        poki_op = df["close"] if c.poki_price_source in {"Close", "Open / Close"} else df["open"]
        poki_hi = np.where(c.poki_price_source == "High / Low", df["high"], np.maximum(df["close"], poki_op))
        poki_lo = np.where(c.poki_price_source == "High / Low", df["low"], np.minimum(df["close"], poki_op))

        curr_close = np.full(n, np.nan)
        direction = np.zeros(n, dtype=int)
        bar_count = np.ones(n, dtype=int)
        agg_vol = np.zeros(n, dtype=float)
        for i in range(n):
            prev_curr = curr_close[i - 1] if i > 0 and not np.isnan(curr_close[i - 1]) else df["close"].iloc[i]
            rsize = poki_renko_size.iloc[i] if isinstance(poki_renko_size, pd.Series) else poki_renko_size[i]
            if np.isnan(rsize):
                rsize = 0.0
            if poki_hi[i] > prev_curr + rsize:
                curr_close[i] = poki_hi[i]
            elif poki_lo[i] < prev_curr - rsize:
                curr_close[i] = poki_lo[i]
            else:
                curr_close[i] = prev_curr
            prev_dir = direction[i - 1] if i > 0 else 0
            direction[i] = 1 if curr_close[i] > prev_curr else -1 if curr_close[i] < prev_curr else prev_dir
            dir_change = direction[i] != prev_dir if i > 0 else False
            bar_count[i] = (bar_count[i - 1] + 1) if (not dir_change and c.poki_normalize and i > 0) else 1
            base_vol = -float(nz(df["volume"].iloc[i], 0.0)) if (c.poki_oscillating and direction[i] < 0) else float(nz(df["volume"].iloc[i], 0.0))
            agg_vol[i] = (agg_vol[i - 1] + base_vol) if (not dir_change and i > 0) else base_vol
        poki_curr_close = pd.Series(curr_close, index=df.index)
        poki_direction = pd.Series(direction, index=df.index)
        poki_bar_count = pd.Series(bar_count, index=df.index)
        poki_agg_vol = pd.Series(agg_vol, index=df.index)
        poki_res_vol = np.where(poki_bar_count > 1, poki_agg_vol / poki_bar_count, poki_agg_vol)

        w1 = 2.0 * pine_wma(df["close"], max(1, int(round(c.poki_hull_len / 2.0))))
        w2 = pine_wma(df["close"], c.poki_hull_len)
        poki_diff = w1 - w2
        poki_hull = pine_wma(poki_diff, max(1, int(round(math.sqrt(c.poki_hull_len)))))
        poki_hull_bull = poki_hull > poki_hull.shift(1)
        poki_res_vol_s = pd.Series(poki_res_vol, index=df.index)
        zero = pd.Series(0.0, index=df.index)
        poki_long_cond = crossover(poki_res_vol_s, zero)
        poki_short_cond = crossunder(poki_res_vol_s, zero)
        poki_bull = poki_hull_bull & (poki_slope > 0) & (df["close"] > poki_sar) & (poki_direction > 0)
        poki_bear = (~poki_hull_bull) & (poki_slope < 0) & (df["close"] < poki_sar) & (poki_direction < 0)
        poki_bull_score = poki_bull | poki_long_cond
        poki_bear_score = poki_bear | poki_short_cond

        # Basic volume --------------------------------------------------------
        volume_ma = pine_sma(df["volume"], c.vol_ma_len)
        volume_ratio = np.where(volume_ma > 0, df["volume"] / volume_ma, 0.0)
        volume_spike = pd.Series(volume_ratio >= c.vol_spike_mult, index=df.index)
        volume_bull = (df["close"] > df["open"]) & volume_spike
        volume_bear = (df["close"] < df["open"]) & volume_spike

        # PAT -----------------------------------------------------------------
        pat_ph = pivot_high(df["high"], c.pat_structure_len, c.pat_structure_len)
        pat_pl = pivot_low(df["low"], c.pat_structure_len, c.pat_structure_len)
        pat_last_sh = []
        pat_last_sl = []
        pat_prev_sh = []
        pat_prev_sl = []
        sh = sl = psh = psl = np.nan
        for ph, pl in zip(pat_ph, pat_pl):
            if not np.isnan(ph):
                psh = sh
                sh = ph
            if not np.isnan(pl):
                psl = sl
                sl = pl
            pat_prev_sh.append(psh)
            pat_prev_sl.append(psl)
            pat_last_sh.append(sh)
            pat_last_sl.append(sl)
        pat_last_sh = pd.Series(pat_last_sh, index=df.index)
        pat_last_sl = pd.Series(pat_last_sl, index=df.index)
        pat_prev_sh = pd.Series(pat_prev_sh, index=df.index)
        pat_prev_sl = pd.Series(pat_prev_sl, index=df.index)

        pat_bos_bull = (~pat_last_sh.isna()) & crossover(df["close"], pat_last_sh)
        pat_bos_bear = (~pat_last_sl.isna()) & crossunder(df["close"], pat_last_sl)
        pat_trend = np.zeros(n, dtype=int)
        current_trend = 0
        for i in range(n):
            if bool(pat_bos_bull.iloc[i]):
                current_trend = 1
            if bool(pat_bos_bear.iloc[i]):
                current_trend = -1
            pat_trend[i] = current_trend
        pat_trend = pd.Series(pat_trend, index=df.index)
        pat_ch_bull = pat_bos_bull & (pat_trend.shift(1) == -1)
        pat_ch_bear = pat_bos_bear & (pat_trend.shift(1) == 1)
        pat_chplus_bull = pat_ch_bull & (df["close"] > df["open"]) & volume_spike
        pat_chplus_bear = pat_ch_bear & (df["close"] < df["open"]) & volume_spike

        pat_fvg_bull = (df["low"] > df["high"].shift(2)) & (((df["low"] - df["high"].shift(2)) / df["close"]) * 100.0 >= c.pat_fvg_min_pct)
        pat_fvg_bear = (df["high"] < df["low"].shift(2)) & (((df["low"].shift(2) - df["high"]) / df["close"]) * 100.0 >= c.pat_fvg_min_pct)
        pat_vi_bull = (df["open"] > prev_close) & prev_close.ne(0) & (((df["open"] - prev_close) / prev_close) * 100.0 >= c.pat_vi_threshold)
        pat_vi_bear = (df["open"] < prev_close) & prev_close.ne(0) & (((prev_close - df["open"]) / prev_close) * 100.0 >= c.pat_vi_threshold)
        pat_new_bull_ob = pat_bos_bull & (df["close"].shift(1) < df["open"].shift(1))
        pat_new_bear_ob = pat_bos_bear & (df["close"].shift(1) > df["open"].shift(1))
        pat_eq_high = (~pat_ph.isna()) & (~pat_prev_sh.isna()) & ((pat_ph - pat_prev_sh).abs() / df["close"] * 100.0 <= c.pat_eq_tolerance)
        pat_eq_low = (~pat_pl.isna()) & (~pat_prev_sl.isna()) & ((pat_pl - pat_prev_sl).abs() / df["close"] * 100.0 <= c.pat_eq_tolerance)
        pat_liq_grab_bull = (~pat_last_sl.isna()) & (df["low"] < pat_last_sl) & (df["close"] > pat_last_sl)
        pat_liq_grab_bear = (~pat_last_sh.isna()) & (df["high"] > pat_last_sh) & (df["close"] < pat_last_sh)

        pd_high = pine_highest(df["high"], c.pat_pd_lookback)
        pd_low = pine_lowest(df["low"], c.pat_pd_lookback)
        pd_mid = (pd_high + pd_low) / 2.0
        pd_premium = df["close"] > pd_mid
        pd_discount = df["close"] < pd_mid

        # Stateful nearest active demand/supply midpoint.
        trade_demand_mid = np.full(n, np.nan)
        trade_supply_mid = np.full(n, np.nan)
        trade_demand_active = np.zeros(n, dtype=bool)
        trade_supply_active = np.zeros(n, dtype=bool)
        dm = sm = np.nan
        da = sa = False
        for i in range(n):
            if bool(pat_fvg_bull.iloc[i]):
                dm = (df["low"].iloc[i] + df["high"].iloc[max(0, i - 2)]) / 2.0
                da = True
            if bool(pat_fvg_bear.iloc[i]):
                sm = (df["low"].iloc[max(0, i - 2)] + df["high"].iloc[i]) / 2.0
                sa = True
            if da and not np.isnan(dm) and df["close"].iloc[i] < dm:
                da = False
            if sa and not np.isnan(sm) and df["close"].iloc[i] > sm:
                sa = False
            trade_demand_mid[i], trade_supply_mid[i] = dm, sm
            trade_demand_active[i], trade_supply_active[i] = da, sa

        pat_bull_score = pat_bos_bull | pat_ch_bull | pat_liq_grab_bull | pat_fvg_bull | pat_new_bull_ob | pat_vi_bull
        pat_bear_score = pat_bos_bear | pat_ch_bear | pat_liq_grab_bear | pat_fvg_bear | pat_new_bear_ob | pat_vi_bear

        # Confluence ----------------------------------------------------------
        ema_bull = (df["close"] > ema_value) & (ema_value > ema_value.shift(1))
        ema_bear = (df["close"] < ema_value) & (ema_value < ema_value.shift(1))
        vwap_bull = vwap_value.notna() & (df["close"] > vwap_value)
        vwap_bear = vwap_value.notna() & (df["close"] < vwap_value)

        bull_score = (
            ema_bull.astype(int) * 15
            + vwap_bull.astype(int) * 15
            + smc_bull_score.astype(int) * 20
            + poki_bull_score.astype(int) * 15
            + volume_bull.astype(int) * 10
            + pat_bull_score.astype(int) * 25
        ).clip(upper=100).astype(float)
        bear_score = (
            ema_bear.astype(int) * 15
            + vwap_bear.astype(int) * 15
            + smc_bear_score.astype(int) * 20
            + poki_bear_score.astype(int) * 15
            + volume_bear.astype(int) * 10
            + pat_bear_score.astype(int) * 25
        ).clip(upper=100).astype(float)

        if c.use_confluence:
            bull_ready = (bull_score >= c.min_confluence) & (bull_score > bear_score)
            bear_ready = (bear_score >= c.min_confluence) & (bear_score > bull_score)
        else:
            bull_ready = ema_bull | vwap_bull | smc_bull_score | poki_bull_score | volume_bull | pat_bull_score
            bear_ready = ema_bear | vwap_bear | smc_bear_score | poki_bear_score | volume_bear | pat_bear_score

        # Pine historical bars are confirmed. For batch OHLCV data every completed
        # row is treated as confirmed.
        raw_final_buy = pd.Series(bull_ready, index=df.index)
        raw_final_sell = pd.Series(bear_ready, index=df.index)
        if c.confirmed_only:
            confirmed = pd.Series(True, index=df.index)
            raw_final_buy &= confirmed
            raw_final_sell &= confirmed

        if c.signal_on_change_only:
            final_buy = raw_final_buy & (~raw_final_buy.shift(1, fill_value=False))
            final_sell = raw_final_sell & (~raw_final_sell.shift(1, fill_value=False))
        else:
            final_buy = raw_final_buy.copy()
            final_sell = raw_final_sell.copy()

        signal = np.where(final_buy, "BUY", np.where(final_sell, "SELL", "NONE"))

        result = pd.DataFrame(index=df.index)
        result["signal"] = signal
        result["bull_score"] = bull_score
        result["bear_score"] = bear_score
        result["ema_bull"] = ema_bull
        result["ema_bear"] = ema_bear
        result["vwap_bull"] = vwap_bull
        result["vwap_bear"] = vwap_bear
        result["smc_buy"] = smc_buy
        result["smc_sell"] = smc_sell
        result["smc_choch_buy"] = smc_choch_buy
        result["smc_choch_sell"] = smc_choch_sell
        result["smc_bos_buy"] = smc_bos_buy
        result["smc_bos_sell"] = smc_bos_sell
        result["smc_strong_buy_flow"] = strong_buy_flow
        result["smc_strong_sell_flow"] = strong_sell_flow
        result["smc_bull_div"] = bull_div
        result["smc_bear_div"] = bear_div
        result["smc_bull_score"] = smc_bull_score
        result["smc_bear_score"] = smc_bear_score
        result["smc_buy"] = smc_buy
        result["smc_sell"] = smc_sell
        result["smc_choch_buy"] = smc_choch_buy
        result["smc_choch_sell"] = smc_choch_sell
        result["smc_bos_buy"] = smc_bos_buy
        result["smc_bos_sell"] = smc_bos_sell
        result["smc_strong_buy_flow"] = strong_buy_flow
        result["smc_strong_sell_flow"] = strong_sell_flow
        result["smc_bull_div"] = bull_div
        result["smc_bear_div"] = bear_div
        result["smc_ready_buy"] = ready_buy
        result["smc_ready_sell"] = ready_sell
        result["smc_trend_strength"] = trend_strength
        result["smc_confidence"] = confidence
        result["smc_trend_1"] = smc_trend1
        result["smc_trend_5"] = smc_trend5
        result["smc_trend_15"] = smc_trend15
        result["smc_trend_30"] = smc_trend30
        result["smc_trend_1h"] = smc_trend1h
        result["smc_trend_4h"] = smc_trend4h
        result["smc_trend_d"] = smc_trendd
        result["poki_bull"] = poki_bull
        result["poki_bear"] = poki_bear
        result["poki_sar"] = poki_sar
        result["poki_slope"] = poki_slope
        result["poki_direction"] = poki_direction
        result["poki_bull_score"] = poki_bull_score
        result["poki_bear_score"] = poki_bear_score
        result["poki_long_cond"] = poki_long_cond
        result["poki_short_cond"] = poki_short_cond
        result["volume_bull"] = volume_bull
        result["volume_bear"] = volume_bear
        result["volume_ma"] = volume_ma
        result["volume_ratio"] = volume_ratio
        result["volume_spike"] = volume_spike
        result["pat_bos_bull"] = pat_bos_bull
        result["pat_bos_bear"] = pat_bos_bear
        result["pat_choch_bull"] = pat_ch_bull
        result["pat_choch_bear"] = pat_ch_bear
        result["pat_fvg_bull"] = pat_fvg_bull
        result["pat_fvg_bear"] = pat_fvg_bear
        result["pat_new_bull_ob"] = pat_new_bull_ob
        result["pat_new_bear_ob"] = pat_new_bear_ob
        result["pat_eq_high"] = pat_eq_high
        result["pat_eq_low"] = pat_eq_low
        result["pat_liquidity_grab_bull"] = pat_liq_grab_bull
        result["pat_liquidity_grab_bear"] = pat_liq_grab_bear
        result["pat_vi_bull"] = pat_vi_bull
        result["pat_vi_bear"] = pat_vi_bear
        result["pat_bull_score"] = pat_bull_score
        result["pat_bear_score"] = pat_bear_score
        result["active_demand"] = trade_demand_active
        result["active_supply"] = trade_supply_active
        result["trade_demand_mid"] = trade_demand_mid
        result["trade_supply_mid"] = trade_supply_mid
        result["final_buy"] = final_buy
        result["final_sell"] = final_sell
        result["raw_final_buy"] = raw_final_buy
        result["raw_final_sell"] = raw_final_sell
        result["ema"] = ema_value
        result["vwap"] = vwap_value
        return result

    def validate_exact_inputs(
        self,
        data: pd.DataFrame,
        mtf: Optional[Dict[str, pd.DataFrame]] = None,
        comparison_start=None,
    ) -> None:
        """Fail fast when required native MTF frames lack practical coverage.

        TradingView's request.security() calculations run in their own requested
        contexts. The Pine calculation budget is not a universal requirement
        that every provider must return exactly 2,000 rows; each frame must have
        warmup before the comparison window and cover its start.
        """
        required = {"1", "5", "15", "30", "60", "240", "D"}
        if mtf is None:
            raise ValueError(
                "Exact-parity mode requires provider-native 1m/5m/15m/30m/1h/4h/D "
                "data via mtf={...}. Resampling a short chart dataframe is only an approximation."
            )
        keys = {str(k).upper() for k in mtf}
        aliases = {"1M": "1", "5M": "5", "15M": "15", "30M": "30", "1H": "60", "4H": "240"}
        normalized = {aliases.get(k, k) for k in keys}
        missing = sorted(required - normalized)
        if missing:
            raise ValueError(f"Exact-parity mode is missing MTF frames: {missing}")
        for k, frame in mtf.items():
            checked = _ensure_ohlcv(frame)
            if len(checked) < 50:
                raise ValueError(f"MTF frame {k} has only {len(checked)} bars; supply sufficient warmup history for parity.")
            coverage_start = pd.Timestamp(comparison_start) if comparison_start is not None else data.index[0]
            if checked.index.min() > coverage_start:
                raise ValueError(f"MTF frame {k} starts after the base data window.")

    def evaluate(self, data: pd.DataFrame, mtf: Optional[Dict[str, pd.DataFrame]] = None) -> ULTI6Result:
        out = self.calculate(data, mtf=mtf)
        row = out.iloc[-1]
        return ULTI6Result(
            timestamp=out.index[-1],
            signal=str(row["signal"]),
            bull_score=float(row["bull_score"]),
            bear_score=float(row["bear_score"]),
            ema_bull=bool(row["ema_bull"]),
            ema_bear=bool(row["ema_bear"]),
            vwap_bull=bool(row["vwap_bull"]),
            vwap_bear=bool(row["vwap_bear"]),
            smc_bull=bool(row["smc_buy"] or row["smc_choch_buy"] or row["smc_bos_buy"] or row["smc_strong_buy_flow"] or row["smc_bull_div"]),
            smc_bear=bool(row["smc_sell"] or row["smc_choch_sell"] or row["smc_bos_sell"] or row["smc_strong_sell_flow"] or row["smc_bear_div"]),
            poki_bull=bool(row["poki_bull"] or row["poki_long_cond"]),
            poki_bear=bool(row["poki_bear"] or row["poki_short_cond"]),
            volume_bull=bool(row["volume_bull"]),
            volume_bear=bool(row["volume_bear"]),
            pat_bull=bool(row["pat_bos_bull"] or row["pat_choch_bull"] or row["pat_liquidity_grab_bull"] or row["pat_fvg_bull"] or row["pat_new_bull_ob"] or row["pat_vi_bull"]),
            pat_bear=bool(row["pat_bos_bear"] or row["pat_choch_bear"] or row["pat_liquidity_grab_bear"] or row["pat_fvg_bear"] or row["pat_new_bear_ob"] or row["pat_vi_bear"]),
            smc_trend_strength=float(row["smc_trend_strength"]),
            smc_confidence=float(row["smc_confidence"]),
        )


# Backward-compatible aliases for the first draft of this module.
ULT16Config = ULTI6Config
ULT16Result = ULTI6Result
ULT16Strategy = ULTI6Strategy


# -----------------------------------------------------------------------------
# Simple example / convenience API
# -----------------------------------------------------------------------------

def compute_ulti6(
    candles: pd.DataFrame,
    config: Optional[ULTI6Config] = None,
    mtf: Optional[Dict[str, pd.DataFrame]] = None,
) -> pd.DataFrame:
    """One-shot batch calculation."""
    return ULTI6Strategy(config).calculate(candles, mtf=mtf)


if __name__ == "__main__":
    # Minimal smoke test only; replace with your real OHLCV feed.
    idx = pd.date_range("2026-01-01 09:30", periods=500, freq="5min")
    rng = np.random.default_rng(7)
    close = pd.Series(100 + rng.normal(0, 0.5, len(idx)).cumsum(), index=idx)
    demo = pd.DataFrame(
        {
            "open": close.shift(1).fillna(close.iloc[0]),
            "high": close + rng.uniform(0, 0.4, len(idx)),
            "low": close - rng.uniform(0, 0.4, len(idx)),
            "close": close,
            "volume": rng.integers(1000, 5000, len(idx)),
        },
        index=idx,
    )
    strategy = ULTI6Strategy()
    result = strategy.evaluate(demo)
    print(asdict(result))
