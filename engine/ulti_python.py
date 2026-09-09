"""Full Python port of the ULTI-6 Pine strategy (pine/ULTI-6_BB_AB_Test_Strategy_v6_2_Live_Status.pine).

PARITY STATUS:
    All six confluence sections (EMA, VWAP, SMC/Gainz, Poki, Volume, PAT) plus
    the BB entry engine and the veto/management state machine are ported below,
    including real multi-timeframe (1m/5m/15m/30m/1H/4H/D) SMC trend voting via
    `align_mtf_trends()` — the neutral-stub MTF has been removed.

    This is a FORMULA-LEVEL port, verified by unit tests on synthetic data. It
    has NOT yet been validated bar-for-bar against a live TradingView chart.
    Two specific areas carry documented assumptions that only a live parity
    run (tradingview/parity_check.py, component-level table) can confirm or
    refute — this module cannot verify them itself, since doing so requires
    reading real values off a TradingView chart:

    1. SMC multi-timeframe VWAP anchor: Pine's `ta.vwap(hlc3)` inside each
       `request.security(..., "1"/"5".../"D")` call uses that timeframe's own
       session anchor. We anchor every timeframe's VWAP to the daily ET session
       (same rule as the 5m chart), which is the standard interpretation but is
       not independently verifiable without a live chart comparison.
    2. Parabolic SAR seed: the exact internal seed Pine's `ta.sar` uses for the
       first 1-2 bars of a series is not documented; we seed via the common
       "first bar close vs prior" convention. This transient decays within a
       handful of bars and should not affect any signal in the live/current
       session as long as enough lookback history is fetched.

    Do not wire this into order placement until a real session's parity log
    (READY/ENTRY/EXIT timestamps + prices, AND the component-level table) has
    been reviewed against the TradingView chart, per the user's explicit
    "verify parity first" plan.

Everything with Pine `var` (persistent across bars) state is computed in ONE
sequential per-bar pass (see `simulate()`), matching Pine's bar-by-bar
execution model exactly instead of approximating it with vectorized ops.

MTF completed-bar semantics: Pine's `request.security(..., lookahead=barmerge.
lookahead_off)` returns the PRIOR completed higher-timeframe bar's value while
the current higher-timeframe bar is still forming (this is what prevents
look-ahead bias). `align_mtf_trends()` reproduces this by indexing each higher
timeframe's trend series by its bar CLOSE time (start + timeframe duration)
and merge_asof'ing backward onto the 5m bar's own close time — a still-forming
higher-timeframe bar's close time is always in the future relative to "now",
so backward-asof naturally excludes it and lands on the last one that Pine
would actually have known about. The one exception is when a "higher"
timeframe equals the base chart timeframe (5M) — Pine's security() call
self-references the current bar directly with no additional lag in that case.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytz

_EASTERN = pytz.timezone("America/New_York")

CONFLUENCE_COMPONENTS_IMPLEMENTED = {"ema", "vwap", "smc", "poki", "volume", "pat"}
CONFLUENCE_COMPONENTS_PENDING = set()  # kept for backward compatibility with parity_check.py

TF_DELTAS = {
    "1M": pd.Timedelta(minutes=1),
    "5M": pd.Timedelta(minutes=5),
    "15M": pd.Timedelta(minutes=15),
    "30M": pd.Timedelta(minutes=30),
    "1H": pd.Timedelta(hours=1),
    "4H": pd.Timedelta(hours=4),
    "D": pd.Timedelta(days=1),
}

DEFAULT_CONFIG = {
    # Section 3 SMC
    "smc_pivot_len": 5,
    "smc_momentum_base": 0.01,
    "smc_min_signal_distance": 5,
    "smc_higher_tf": "5M",
    "smc_lower_tf": "5M",
    "smc_restrict_tf": "5M",
    "smc_volume_long": 50,
    "smc_volume_short": 5,
    # Section 4 Poki
    "poki_sar_start": 0.03,
    "poki_sar_step": 0.03,
    "poki_sar_max": 0.30,
    "poki_reg_len": 50,
    "poki_renko_method": "ATR",
    "poki_renko_value": 14.0,
    "poki_price_source": "Close",
    "poki_oscillating": True,
    "poki_normalize": False,
    "poki_hull_len": 32,
    # Section 5 Volume
    "vol_ma_len": 20,
    "vol_spike_mult": 1.5,
    # Section 6 PAT
    "pat_fvg_min_pct": 0.10,
    "pat_structure_len": 5,
    "pat_vi_threshold": 0.25,
    # Section 8B BB / ULTI
    "bb_length": 20,
    "bb_mult": 2.0,
    "veto_opp_score": 60,
    "veto_opp_lead": 25,
    "veto_extension_atr": 2.0,
    "mgmt_initial_stop_atr": 1.0,
    "mgmt_be_trigger_atr": 1.0,
    "mgmt_runner_trigger_atr": 1.5,
    "mgmt_trail_atr": 1.25,
    "mgmt_opp_score": 60,
    "mgmt_opp_bars": 2,
    "forming_atr": 0.30,
    "use_veto": True,
    "use_management": True,
}


# ---------------------------------------------------------------------------
# Section 1/2/5: EMA, session VWAP, volume spike (exact, mechanical ports)
# ---------------------------------------------------------------------------
def ema(series, length=9):
    """Pine ta.ema(source, length) == pandas ewm(span=length, adjust=False)."""
    return series.ewm(span=length, adjust=False).mean()


def session_vwap(df):
    """Pine ta.vwap(hlc3, timeframe.change('D'), 1) — resets at each new ET session."""
    typical = (df["high"] + df["low"] + df["close"]) / 3
    idx = df.index
    idx_et = idx.tz_convert(_EASTERN) if getattr(idx, "tz", None) is not None else idx.tz_localize("UTC").tz_convert(_EASTERN)
    session_date = pd.Series([ts.date() for ts in idx_et], index=df.index)
    vwap = pd.Series(float("nan"), index=df.index, dtype=float)
    for _, group_idx in session_date.groupby(session_date).groups.items():
        tv = typical.loc[group_idx] * df.loc[group_idx, "volume"]
        vwap.loc[group_idx] = tv.cumsum() / df.loc[group_idx, "volume"].cumsum()
    return vwap


def rsi(series, period=14):
    """Wilder RSI — same formula the bot already uses in calculate_rsi()."""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rsi_val = 100 - (100 / (1 + avg_gain / avg_loss.replace(0, float("nan"))))
    return rsi_val


def wilder_atr(df, period=14):
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def volume_spike(df, length=20, mult=1.5):
    vol_ma = df["volume"].rolling(length).mean()
    ratio = df["volume"] / vol_ma.replace(0, float("nan"))
    spike = ratio >= mult
    bull = (df["close"] > df["open"]) & spike
    bear = (df["close"] < df["open"]) & spike
    return bull.fillna(False), bear.fillna(False), spike.fillna(False)


# ---------------------------------------------------------------------------
# Section 8B core: Bollinger Bands (exact ta.stdev is POPULATION stdev, ddof=0)
# ---------------------------------------------------------------------------
def bollinger_bands(df, length=20, mult=2.0):
    basis = df["close"].rolling(length).mean()
    dev = mult * df["close"].rolling(length).std(ddof=0)
    return basis, basis + dev, basis - dev


def _crossover(a, b):
    a_prev, b_prev = a.shift(1), b.shift(1)
    return (a_prev <= b_prev) & (a > b)


def _crossunder(a, b):
    a_prev, b_prev = a.shift(1), b.shift(1)
    return (a_prev >= b_prev) & (a < b)


def bb_signals(df, length=20, mult=2.0):
    basis, upper, lower = bollinger_bands(df, length, mult)
    long_signal = _crossover(df["close"], lower).fillna(False)
    short_signal = _crossunder(df["close"], upper).fillna(False)
    return long_signal, short_signal, basis, upper, lower


# ---------------------------------------------------------------------------
# Pivot detection (ta.pivothigh/pivotlow): confirmed `right` bars AFTER the
# actual pivot bar. Vectorizable since it has no persistent state itself —
# the persistent "last confirmed pivot" carry-forward happens in simulate().
# ---------------------------------------------------------------------------
def pivot_high(series, left, right):
    vals = series.values
    n = len(vals)
    out = np.full(n, np.nan)
    for j in range(left, n - right):
        lwin = vals[j - left:j]
        rwin = vals[j + 1:j + right + 1]
        c = vals[j]
        if c >= lwin.max() and c >= rwin.max():
            out[j + right] = c
    return pd.Series(out, index=series.index)


def pivot_low(series, left, right):
    vals = series.values
    n = len(vals)
    out = np.full(n, np.nan)
    for j in range(left, n - right):
        lwin = vals[j - left:j]
        rwin = vals[j + 1:j + right + 1]
        c = vals[j]
        if c <= lwin.min() and c <= rwin.min():
            out[j + right] = c
    return pd.Series(out, index=series.index)


# ---------------------------------------------------------------------------
# Section 4 Poki: SAR, linear regression, WMA/Hull (SAR is inherently
# stateful; linreg/WMA/Hull are vectorizable via rolling windows).
# ---------------------------------------------------------------------------
def parabolic_sar(df, start=0.03, step=0.03, maximum=0.30):
    high, low, close = df["high"].values, df["low"].values, df["close"].values
    n = len(df)
    sar = np.full(n, np.nan)
    if n == 0:
        return pd.Series(sar, index=df.index)
    uptrend = close[1] >= close[0] if n > 1 else True
    ep = high[0] if uptrend else low[0]
    af = start
    sar[0] = low[0] if uptrend else high[0]
    for i in range(1, n):
        prev_sar = sar[i - 1]
        if uptrend:
            new_sar = prev_sar + af * (ep - prev_sar)
            floor_val = min(low[i - 1], low[i - 2]) if i >= 2 else low[i - 1]
            new_sar = min(new_sar, floor_val)
            if low[i] < new_sar:
                uptrend = False
                new_sar = ep
                ep = low[i]
                af = start
            elif high[i] > ep:
                ep = high[i]
                af = min(af + step, maximum)
        else:
            new_sar = prev_sar + af * (ep - prev_sar)
            cap_val = max(high[i - 1], high[i - 2]) if i >= 2 else high[i - 1]
            new_sar = max(new_sar, cap_val)
            if high[i] > new_sar:
                uptrend = True
                new_sar = ep
                ep = high[i]
                af = start
            elif low[i] < ep:
                ep = low[i]
                af = min(af + step, maximum)
        sar[i] = new_sar
    return pd.Series(sar, index=df.index)


def linreg(series, length):
    """Pine ta.linreg(source, length, 0): fitted regression value at the current bar."""
    def _fit(window):
        if np.any(np.isnan(window)):
            return np.nan
        x = np.arange(len(window))
        slope, intercept = np.polyfit(x, window, 1)
        return slope * (len(window) - 1) + intercept
    return series.rolling(length).apply(_fit, raw=True)


def wma(series, length):
    weights = np.arange(1, length + 1, dtype=float)
    denom = weights.sum()
    return series.rolling(length).apply(lambda w: np.dot(w, weights) / denom, raw=True)


def hull_ma(series, length):
    half = max(1, round(length / 2))
    sqrt_len = max(1, round(np.sqrt(length)))
    diff = 2.0 * wma(series, half) - wma(series, length)
    return wma(diff, sqrt_len)


def renko_simulate(df, method="ATR", value=14.0, price_source="Close", oscillating=True, normalize=False):
    """Poki's stateful Renko-direction + aggregated-volume simulation."""
    n = len(df)
    direction = np.zeros(n, dtype=int)
    res_vol = np.zeros(n)
    atr_for_renko = wilder_atr(df, max(1, round(value))) if method == "ATR" else None
    curr_close = np.nan
    bar_count = 1
    agg_vol = 0.0
    close_v, open_v, high_v, low_v, vol_v = (
        df["close"].values, df["open"].values, df["high"].values, df["low"].values, df["volume"].values,
    )
    for i in range(n):
        if method == "ATR":
            renko_size = atr_for_renko.iat[i]
        elif method == "Part of Price":
            renko_size = close_v[i] / max(value, 1)
        else:
            renko_size = value
        op_ = close_v[i]  # pokiOp == close for both "Close" and "Open / Close" price sources
        hi_ = high_v[i] if price_source == "High / Low" else max(close_v[i], op_)
        lo_ = low_v[i] if price_source == "High / Low" else min(close_v[i], op_)
        prev_close = curr_close if not np.isnan(curr_close) else close_v[i]
        if not np.isnan(renko_size) and hi_ > prev_close + renko_size:
            curr_close = hi_
        elif not np.isnan(renko_size) and lo_ < prev_close - renko_size:
            curr_close = lo_
        else:
            curr_close = prev_close
        prior_dir = direction[i - 1] if i > 0 else 0
        if curr_close > prev_close:
            direction[i] = 1
        elif curr_close < prev_close:
            direction[i] = -1
        else:
            direction[i] = prior_dir
        dir_change = direction[i] != prior_dir
        bar_count = (bar_count + 1) if (not dir_change and normalize) else 1
        base_vol = -vol_v[i] if (oscillating and direction[i] < 0) else vol_v[i]
        agg_vol = (agg_vol + base_vol) if not dir_change else base_vol
        res_vol[i] = agg_vol / bar_count if bar_count > 1 else agg_vol
    return pd.Series(direction, index=df.index), pd.Series(res_vol, index=df.index)


def poki_signals(df, config=None):
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    sar = parabolic_sar(df, cfg["poki_sar_start"], cfg["poki_sar_step"], cfg["poki_sar_max"])
    reg = linreg(df["close"], cfg["poki_reg_len"])
    reg_prev = linreg(df["close"].shift(1), cfg["poki_reg_len"])
    slope = reg - reg_prev
    hull = hull_ma(df["close"], cfg["poki_hull_len"])
    hull_bull = hull > hull.shift(1)
    direction, res_vol = renko_simulate(
        df, cfg["poki_renko_method"], cfg["poki_renko_value"], cfg["poki_price_source"],
        cfg["poki_oscillating"], cfg["poki_normalize"],
    )
    long_cond = _crossover(res_vol, pd.Series(0.0, index=df.index))
    short_cond = _crossunder(res_vol, pd.Series(0.0, index=df.index))
    poki_bull = hull_bull.fillna(False) & (slope > 0).fillna(False) & (df["close"] > sar) & (direction > 0)
    poki_bear = (~hull_bull.fillna(False)) & (slope < 0).fillna(False) & (df["close"] < sar) & (direction < 0)
    bull_score = (poki_bull | long_cond.fillna(False))
    bear_score = (poki_bear | short_cond.fillna(False))
    return {"bull": bull_score, "bear": bear_score, "sar": sar, "slope": slope, "direction": direction}


# ---------------------------------------------------------------------------
# Section 3 SMC and Section 6 PAT both carry persistent `var` state
# (last confirmed pivot, trend flip memory) — computed together with the BB
# management state machine in simulate() below, one bar at a time.
# ---------------------------------------------------------------------------
def _tf_trend_raw(df, ema_len=20):
    """EMA20+session-VWAP trend vote (-1/0/1) computed natively on a timeframe's own bars."""
    e = ema(df["close"], ema_len)
    v = session_vwap(df)
    bull = (df["close"] > e) & (df["close"] > v)
    bear = (df["close"] < e) & (df["close"] < v)
    return pd.Series(
        np.where(bull.fillna(False), 1, np.where(bear.fillna(False), -1, 0)),
        index=df.index,
    )


def align_mtf_trends(df_5m, mtf_bars, base_tf_label="5M", ema_len=20):
    """Real multi-timeframe SMC trend voting with Pine's completed-bar semantics.

    mtf_bars: dict {"1M": df, "5M": df, "15M": df, "30M": df, "1H": df, "4H": df,
    "D": df} of each timeframe's own raw OHLCV (from Alpaca, native bars — NOT
    resampled from 5m, since Alpaca's own aggregation must match what
    request.security() would see).

    Returns dict {tf_label: Series} aligned to df_5m.index, where each higher
    timeframe's value at 5m bar i is the LAST COMPLETED higher-timeframe bar
    as of that 5m bar's close — never a still-forming higher-timeframe bar
    (see module docstring for why the close-time asof-backward merge achieves
    this automatically). The base timeframe (matches the chart itself, "5M" by
    default) is used directly with no additional lag, matching Pine's
    same-timeframe security() self-reference behavior.
    """
    base_close = (df_5m.index + TF_DELTAS[base_tf_label]).values
    out = {}
    for label, tf_df in (mtf_bars or {}).items():
        if tf_df is None or len(tf_df) == 0:
            out[label] = pd.Series(0, index=df_5m.index)
            continue
        if label == base_tf_label:
            out[label] = _tf_trend_raw(tf_df, ema_len).reindex(df_5m.index).fillna(0)
            continue
        trend = _tf_trend_raw(tf_df, ema_len)
        close_times = (tf_df.index + TF_DELTAS[label]).values
        src = pd.DataFrame({"close_time": close_times, "trend": trend.values}).sort_values("close_time")
        base = pd.DataFrame({"close_time": base_close})
        merged = pd.merge_asof(base, src, on="close_time", direction="backward")
        out[label] = pd.Series(merged["trend"].fillna(0).values, index=df_5m.index)
    return out


def simulate(df, mtf_trends=None, config=None, diagnostics=False):
    """Bar-by-bar replay producing the full event lifecycle: FORMING/READY/
    ENTRY/EXIT_WATCH/EXIT, exactly mirroring the Pine script's persistent
    `var` state instead of approximating it with vectorized operations.

    mtf_trends: dict {"1M":Series,...,"D":Series} of -1/0/1 votes aligned to
    df.index — build with `align_mtf_trends()` using real Alpaca multi-
    timeframe bars. If omitted, SMC trend/breakout/momentum conditions are
    treated as neutral (0), which will under-fire relative to the real Pine
    chart — only pass None deliberately (e.g. quick BB-only checks).

    diagnostics: if True, also returns a per-bar DataFrame with every
    component's boolean/score/veto state (see tradingview/parity_check.py's
    component-level table) so a mismatch can be attributed to a specific
    section instead of the whole strategy.

    Returns: events (list[dict]) if diagnostics=False, else (events, diag_df).
    """
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    n = len(df)
    events = []
    diag_rows = [] if diagnostics else None
    if n < max(cfg["bb_length"], cfg["poki_reg_len"], 55):
        return (events, pd.DataFrame(diag_rows)) if diagnostics else events

    close = df["close"].values
    open_ = df["open"].values
    high = df["high"].values
    low = df["low"].values
    volume = df["volume"].values

    ema_val = ema(df["close"]).values
    vwap_val = session_vwap(df).values
    atr14 = wilder_atr(df, 14).values
    rsi14 = rsi(df["close"], 14).values
    vol_bull, vol_bear, _ = volume_spike(df, cfg["vol_ma_len"], cfg["vol_spike_mult"])
    vol_bull, vol_bear = vol_bull.values, vol_bear.values
    vol_avg = df["volume"].rolling(cfg["smc_volume_long"]).mean().values
    vol_avg_short = df["volume"].rolling(cfg["smc_volume_short"]).mean().values
    vol_change_short = pd.Series(vol_avg_short).diff().values

    basis, bb_upper, bb_lower = bollinger_bands(df, cfg["bb_length"], cfg["bb_mult"])
    bb_long_signal, bb_short_signal = bb_signals(df, cfg["bb_length"], cfg["bb_mult"])[:2]
    bb_long_signal, bb_short_signal = bb_long_signal.values, bb_short_signal.values
    bb_upper_v, bb_lower_v = bb_upper.values, bb_lower.values

    poki = poki_signals(df, cfg)
    poki_bull, poki_bear = poki["bull"].values, poki["bear"].values
    sar_v = poki["sar"].values

    smc_pivot_high = pivot_high(df["high"], cfg["smc_pivot_len"], cfg["smc_pivot_len"]).values
    smc_pivot_low = pivot_low(df["low"], cfg["smc_pivot_len"], cfg["smc_pivot_len"]).values
    pat_pivot_high = pivot_high(df["high"], cfg["pat_structure_len"], cfg["pat_structure_len"]).values
    pat_pivot_low = pivot_low(df["low"], cfg["pat_structure_len"], cfg["pat_structure_len"]).values

    rsi5 = pd.Series(rsi14).shift(5).values
    rsi10 = pd.Series(rsi14).shift(10).values
    low5, low10 = pd.Series(low).shift(5).values, pd.Series(low).shift(10).values
    high5, high10 = pd.Series(high).shift(5).values, pd.Series(high).shift(10).values

    tf_map = mtf_trends or {}
    higher_tf = tf_map.get(cfg["smc_higher_tf"])
    lower_tf = tf_map.get(cfg["smc_lower_tf"])
    higher_tf_v = higher_tf.values if higher_tf is not None else np.zeros(n)
    lower_tf_v = lower_tf.values if lower_tf is not None else np.zeros(n)

    highest_breakout = df["high"].rolling(cfg["smc_min_signal_distance"]).max().shift(1).values
    lowest_breakout = df["low"].rolling(cfg["smc_min_signal_distance"]).min().shift(1).values


    # --- persistent state (Pine `var`) ---
    smc_last_high = smc_last_low = float("nan")
    smc_last_signal_bar = -100000
    smc_last_signal = "Neutral"
    pat_last_sh = pat_last_sl = pat_prev_sh = pat_prev_sl = float("nan")
    pat_trend = 0
    position_side = 0  # 0 flat, 1 long, -1 short
    entry_price = entry_atr = best_price = None
    be_armed = runner_armed = False
    bear_exit_count = bull_exit_count = 0
    call_forming_prev = put_forming_prev = False

    for i in range(n):
        ts = df.index[i]
        smc_atr = atr14[i]
        vol_factor = (smc_atr / close[i]) if (close[i] and not np.isnan(smc_atr)) else 0.0
        momentum_threshold = cfg["smc_momentum_base"] * (1 + vol_factor * 2)
        price_change = ((close[i] - close[i - 1]) / close[i - 1] * 100) if i > 0 and close[i - 1] else 0.0

        if not np.isnan(smc_pivot_high[i]):
            smc_last_high = smc_pivot_high[i]
        if not np.isnan(smc_pivot_low[i]):
            smc_last_low = smc_pivot_low[i]

        smc_vol_cond = (volume[i] > vol_avg[i]) if not np.isnan(vol_avg[i]) else False
        smc_vol_cond = bool(smc_vol_cond) and (vol_change_short[i] > 0 if not np.isnan(vol_change_short[i]) else False)
        smc_buy_allowed = (
            (price_change > momentum_threshold)
            and (higher_tf_v[i] == 1)
            and (lower_tf_v[i] != -1)
            and smc_vol_cond
            and (not np.isnan(highest_breakout[i]) and close[i] > highest_breakout[i])
        )
        smc_sell_allowed = (
            (price_change < -momentum_threshold)
            and (higher_tf_v[i] == -1)
            and (lower_tf_v[i] != 1)
            and smc_vol_cond
            and (not np.isnan(lowest_breakout[i]) and close[i] < lowest_breakout[i])
        )
        smc_buy = smc_buy_allowed and (i - smc_last_signal_bar >= cfg["smc_min_signal_distance"])
        smc_sell = smc_sell_allowed and (i - smc_last_signal_bar >= cfg["smc_min_signal_distance"])
        if smc_buy:
            smc_last_signal_bar, smc_last_signal = i, "Buy"
        if smc_sell:
            smc_last_signal_bar, smc_last_signal = i, "Sell"

        smc_choch_buy = (not np.isnan(smc_last_low)) and (i > 0) and (high[i - 1] <= smc_last_low < high[i]) and close[i] > open_[i]
        smc_choch_sell = (not np.isnan(smc_last_high)) and (i > 0) and (low[i - 1] >= smc_last_high > low[i]) and close[i] < open_[i]
        prev_pivot_high_1 = smc_pivot_high[i - 1] if i > 0 else np.nan
        prev_pivot_low_1 = smc_pivot_low[i - 1] if i > 0 else np.nan
        smc_bos_buy = (not np.isnan(prev_pivot_high_1)) and (i > 0) and (high[i - 1] <= prev_pivot_high_1 < high[i]) and high[i] > prev_pivot_high_1 and close[i] > open_[i]
        smc_bos_sell = (not np.isnan(prev_pivot_low_1)) and (i > 0) and (low[i - 1] >= prev_pivot_low_1 > low[i]) and low[i] < prev_pivot_low_1 and close[i] < open_[i]

        recent_buy_vol = vol_avg_short[i] if close[i] > open_[i] else 0.0
        recent_sell_vol = vol_avg_short[i] if close[i] < open_[i] else 0.0
        vol_ratio = (
            recent_buy_vol / (recent_buy_vol + recent_sell_vol)
            if (recent_buy_vol > 0 and recent_sell_vol > 0) else 0.5
        )
        smc_strong_buy_flow = vol_ratio > 0.65 and (not np.isnan(vol_avg[i])) and volume[i] > vol_avg[i] * 1.5
        smc_strong_sell_flow = vol_ratio < 0.35 and (not np.isnan(vol_avg[i])) and volume[i] > vol_avg[i] * 1.5
        smc_bull_div = (
            i >= 10 and low[i] < low5[i] < low10[i] and rsi14[i] > rsi5[i] > rsi10[i] and rsi14[i] < 40
        )
        smc_bear_div = (
            i >= 10 and high[i] > high5[i] > high10[i] and rsi14[i] < rsi5[i] < rsi10[i] and rsi14[i] > 60
        )
        smc_bull = bool(smc_buy or smc_choch_buy or smc_bos_buy or smc_strong_buy_flow or smc_bull_div)
        smc_bear = bool(smc_sell or smc_choch_sell or smc_bos_sell or smc_strong_sell_flow or smc_bear_div)

        # --- PAT (section 6) ---
        if not np.isnan(pat_pivot_high[i]):
            pat_prev_sh, pat_last_sh = pat_last_sh, pat_pivot_high[i]
        if not np.isnan(pat_pivot_low[i]):
            pat_prev_sl, pat_last_sl = pat_last_sl, pat_pivot_low[i]
        pat_bos_bull = (not np.isnan(pat_last_sh)) and (i > 0) and (close[i - 1] <= pat_last_sh < close[i])
        pat_bos_bear = (not np.isnan(pat_last_sl)) and (i > 0) and (close[i - 1] >= pat_last_sl > close[i])
        pat_ch_bull = pat_bos_bull and pat_trend == -1
        pat_ch_bear = pat_bos_bear and pat_trend == 1
        if pat_bos_bull:
            pat_trend = 1
        if pat_bos_bear:
            pat_trend = -1
        pat_fvg_bull = i >= 2 and low[i] > high[i - 2] and close[i] and ((low[i] - high[i - 2]) / close[i] * 100 >= cfg["pat_fvg_min_pct"])
        pat_fvg_bear = i >= 2 and high[i] < low[i - 2] and close[i] and ((low[i - 2] - high[i]) / close[i] * 100 >= cfg["pat_fvg_min_pct"])
        pat_vi_bull = i > 0 and open_[i] > close[i - 1] and close[i - 1] and ((open_[i] - close[i - 1]) / close[i - 1] * 100 >= cfg["pat_vi_threshold"])
        pat_vi_bear = i > 0 and open_[i] < close[i - 1] and close[i - 1] and ((close[i - 1] - open_[i]) / close[i - 1] * 100 >= cfg["pat_vi_threshold"])
        pat_new_bull_ob = pat_bos_bull and i > 0 and close[i - 1] < open_[i - 1]
        pat_new_bear_ob = pat_bos_bear and i > 0 and close[i - 1] > open_[i - 1]
        pat_liquidity_grab_bull = (not np.isnan(pat_last_sl)) and low[i] < pat_last_sl and close[i] > pat_last_sl
        pat_liquidity_grab_bear = (not np.isnan(pat_last_sh)) and high[i] > pat_last_sh and close[i] < pat_last_sh
        pat_bull = bool(pat_bos_bull or pat_ch_bull or pat_liquidity_grab_bull or pat_fvg_bull or pat_new_bull_ob or pat_vi_bull)
        pat_bear = bool(pat_bos_bear or pat_ch_bear or pat_liquidity_grab_bear or pat_fvg_bear or pat_new_bear_ob or pat_vi_bear)

        # --- Confluence score (section 8) ---
        ema_bull = close[i] > ema_val[i] and (i > 0 and ema_val[i] > ema_val[i - 1])
        ema_bear = close[i] < ema_val[i] and (i > 0 and ema_val[i] < ema_val[i - 1])
        vwap_bull = (not np.isnan(vwap_val[i])) and close[i] > vwap_val[i]
        vwap_bear = (not np.isnan(vwap_val[i])) and close[i] < vwap_val[i]
        bull_score = (
            (15 if ema_bull else 0) + (15 if vwap_bull else 0) + (20 if smc_bull else 0)
            + (15 if poki_bull[i] else 0) + (10 if vol_bull[i] else 0) + (25 if pat_bull else 0)
        )
        bear_score = (
            (15 if ema_bear else 0) + (15 if vwap_bear else 0) + (20 if smc_bear else 0)
            + (15 if poki_bear[i] else 0) + (10 if vol_bear[i] else 0) + (25 if pat_bear else 0)
        )
        bull_score, bear_score = min(bull_score, 100), min(bear_score, 100)

        # --- BB / ULTI engine (section 8B) ---
        atr_ok = (not np.isnan(atr14[i])) and atr14[i] > 0
        dist_ema = abs(close[i] - ema_val[i]) / atr14[i] if atr_ok else 0.0
        dist_vwap = (abs(close[i] - vwap_val[i]) / atr14[i]) if (atr_ok and not np.isnan(vwap_val[i])) else 0.0
        long_chase = close[i] > ema_val[i] and (np.isnan(vwap_val[i]) or close[i] > vwap_val[i]) and max(dist_ema, dist_vwap) > cfg["veto_extension_atr"]
        short_chase = close[i] < ema_val[i] and (np.isnan(vwap_val[i]) or close[i] < vwap_val[i]) and max(dist_ema, dist_vwap) > cfg["veto_extension_atr"]
        severe_bear_opp = bear_score >= cfg["veto_opp_score"] and (bear_score - bull_score) >= cfg["veto_opp_lead"]
        severe_bull_opp = bull_score >= cfg["veto_opp_score"] and (bull_score - bear_score) >= cfg["veto_opp_lead"]
        allow_long = (not cfg["use_veto"]) or (not severe_bear_opp and not long_chase)
        allow_short = (not cfg["use_veto"]) or (not severe_bull_opp and not short_chase)

        if diagnostics:
            diag_rows.append({
                "time": ts,
                "close": float(close[i]),
                "bb_long_signal": bool(bb_long_signal[i]), "bb_short_signal": bool(bb_short_signal[i]),
                "bb_basis": float(basis.iat[i]) if not np.isnan(basis.iat[i]) else None,
                "bb_upper": float(bb_upper_v[i]) if not np.isnan(bb_upper_v[i]) else None,
                "bb_lower": float(bb_lower_v[i]) if not np.isnan(bb_lower_v[i]) else None,
                "smc_bull": smc_bull, "smc_bear": smc_bear,
                "poki_bull": bool(poki_bull[i]), "poki_bear": bool(poki_bear[i]), "sar": float(sar_v[i]) if not np.isnan(sar_v[i]) else None,
                "pat_bull": pat_bull, "pat_bear": pat_bear,
                "vol_bull": bool(vol_bull[i]), "vol_bear": bool(vol_bear[i]),
                "ema_bull": bool(ema_bull), "ema_bear": bool(ema_bear),
                "vwap": float(vwap_val[i]) if not np.isnan(vwap_val[i]) else None,
                "vwap_bull": bool(vwap_bull), "vwap_bear": bool(vwap_bear),
                "bull_score": bull_score, "bear_score": bear_score,
                "veto_active": bool(cfg["use_veto"]),
                "veto_blocked_long": bool(cfg["use_veto"] and (severe_bear_opp or long_chase)),
                "veto_blocked_short": bool(cfg["use_veto"] and (severe_bull_opp or short_chase)),
                "position_side": position_side,
            })

        call_forming = position_side == 0 and atr_ok and close[i] <= bb_lower_v[i] + atr14[i] * cfg["forming_atr"] and close[i] < basis.iat[i] and not bb_long_signal[i]
        put_forming = position_side == 0 and atr_ok and close[i] >= bb_upper_v[i] - atr14[i] * cfg["forming_atr"] and close[i] > basis.iat[i] and not bb_short_signal[i]
        if call_forming and not call_forming_prev:
            events.append({"time": ts, "event": "FORMING", "side": "CALL", "price": float(close[i]), "reason_code": "BB_LOWER_APPROACH", "reason": "Price approaching lower Bollinger Band"})
        if put_forming and not put_forming_prev:
            events.append({"time": ts, "event": "FORMING", "side": "PUT", "price": float(close[i]), "reason_code": "BB_UPPER_APPROACH", "reason": "Price approaching upper Bollinger Band"})
        call_forming_prev, put_forming_prev = call_forming, put_forming

        call_ready = position_side <= 0 and bb_long_signal[i] and allow_long
        put_ready = position_side >= 0 and bb_short_signal[i] and allow_short
        if call_ready:
            events.append({"time": ts, "event": "READY", "side": "CALL", "price": float(close[i]), "reason_code": "BB_LOWER_CROSSOVER", "reason": "Price crossed above lower Bollinger Band" + (", ULTI veto clear" if cfg["use_veto"] else "")})
        if put_ready:
            events.append({"time": ts, "event": "READY", "side": "PUT", "price": float(close[i]), "reason_code": "BB_UPPER_CROSSUNDER", "reason": "Price crossed below upper Bollinger Band" + (", ULTI veto clear" if cfg["use_veto"] else "")})

        # --- Management: run BEFORE reversal/entry, mirroring Pine's execution order ---
        if cfg["use_management"] and position_side != 0 and entry_price is not None:
            if position_side == 1:
                best_price = max(best_price, high[i])
                bear_exit_count = bear_exit_count + 1 if (bear_score >= cfg["mgmt_opp_score"] and bear_score > bull_score) else 0
            else:
                best_price = min(best_price, low[i])
                bull_exit_count = bull_exit_count + 1 if (bull_score >= cfg["mgmt_opp_score"] and bull_score > bear_score) else 0

            if position_side == 1:
                if not be_armed and high[i] >= entry_price + entry_atr * cfg["mgmt_be_trigger_atr"]:
                    be_armed = True
                if not runner_armed and high[i] >= entry_price + entry_atr * cfg["mgmt_runner_trigger_atr"]:
                    runner_armed = True
                stop = entry_price if be_armed else entry_price - entry_atr * cfg["mgmt_initial_stop_atr"]
                if runner_armed:
                    stop = max(stop, best_price - entry_atr * cfg["mgmt_trail_atr"])
                if low[i] <= stop:
                    events.append({"time": ts, "event": "EXIT", "side": "CALL", "price": float(stop), "reason_code": "ULTI_PROTECTIVE_STOP", "reason": "ULTI PROTECTIVE STOP"})
                    position_side, entry_price, entry_atr, best_price = 0, None, None, None
                    be_armed = runner_armed = False
                    bear_exit_count = bull_exit_count = 0
                elif bear_exit_count >= cfg["mgmt_opp_bars"]:
                    events.append({"time": ts, "event": "EXIT", "side": "CALL", "price": float(close[i]), "reason_code": "ULTI_EXIT", "reason": "ULTI EXIT"})
                    position_side, entry_price, entry_atr, best_price = 0, None, None, None
                    be_armed = runner_armed = False
                    bear_exit_count = bull_exit_count = 0
                elif bear_exit_count == 1:
                    events.append({"time": ts, "event": "EXIT_WATCH", "side": "CALL", "price": float(close[i]), "reason_code": "OPPOSING_SCORE_PRESSURE", "reason": f"Opposing PUT score pressure {bear_exit_count}/{cfg['mgmt_opp_bars']} bars"})
            elif position_side == -1:
                if not be_armed and low[i] <= entry_price - entry_atr * cfg["mgmt_be_trigger_atr"]:
                    be_armed = True
                if not runner_armed and low[i] <= entry_price - entry_atr * cfg["mgmt_runner_trigger_atr"]:
                    runner_armed = True
                stop = entry_price if be_armed else entry_price + entry_atr * cfg["mgmt_initial_stop_atr"]
                if runner_armed:
                    stop = min(stop, best_price + entry_atr * cfg["mgmt_trail_atr"])
                if high[i] >= stop:
                    events.append({"time": ts, "event": "EXIT", "side": "PUT", "price": float(stop), "reason_code": "ULTI_PROTECTIVE_STOP", "reason": "ULTI PROTECTIVE STOP"})
                    position_side, entry_price, entry_atr, best_price = 0, None, None, None
                    be_armed = runner_armed = False
                    bear_exit_count = bull_exit_count = 0
                elif bull_exit_count >= cfg["mgmt_opp_bars"]:
                    events.append({"time": ts, "event": "EXIT", "side": "PUT", "price": float(close[i]), "reason_code": "ULTI_EXIT", "reason": "ULTI EXIT"})
                    position_side, entry_price, entry_atr, best_price = 0, None, None, None
                    be_armed = runner_armed = False
                    bear_exit_count = bull_exit_count = 0
                elif bull_exit_count == 1:
                    events.append({"time": ts, "event": "EXIT_WATCH", "side": "PUT", "price": float(close[i]), "reason_code": "OPPOSING_SCORE_PRESSURE", "reason": f"Opposing CALL score pressure {bull_exit_count}/{cfg['mgmt_opp_bars']} bars"})

        # --- Reversal / new entry (stop order filled same bar since crossover already breached the level) ---
        if bb_long_signal[i] and allow_long and position_side <= 0:
            if position_side == -1:
                events.append({"time": ts, "event": "EXIT", "side": "PUT", "price": float(close[i]), "reason_code": "REVERSAL", "reason": "Reversed into opposite BB signal"})
            position_side = 1
            entry_price = float(close[i])
            entry_atr = atr14[i] if atr_ok else 0.0001
            best_price = high[i]
            be_armed = runner_armed = False
            bear_exit_count = bull_exit_count = 0
            events.append({"time": ts, "event": "ENTRY", "side": "CALL", "price": entry_price, "reason_code": "BB_LOWER_CROSSOVER", "reason": "Bollinger lower-band crossover" + (", ULTI veto clear" if cfg["use_veto"] else "")})
        elif bb_short_signal[i] and allow_short and position_side >= 0:
            if position_side == 1:
                events.append({"time": ts, "event": "EXIT", "side": "CALL", "price": float(close[i]), "reason_code": "REVERSAL", "reason": "Reversed into opposite BB signal"})
            position_side = -1
            entry_price = float(close[i])
            entry_atr = atr14[i] if atr_ok else 0.0001
            best_price = low[i]
            be_armed = runner_armed = False
            bear_exit_count = bull_exit_count = 0
            events.append({"time": ts, "event": "ENTRY", "side": "PUT", "price": entry_price, "reason_code": "BB_UPPER_CROSSUNDER", "reason": "Bollinger upper-band crossunder" + (", ULTI veto clear" if cfg["use_veto"] else "")})

        if diagnostics:
            diag_rows[-1]["lifecycle"] = (
                "IN_POSITION" if position_side != 0 else "FLAT"
            )
            bar_events = [e["event"] for e in events if e["time"] == ts]
            if bar_events:
                diag_rows[-1]["lifecycle"] = "/".join(bar_events)

    if diagnostics:
        return events, pd.DataFrame(diag_rows)
    return events


def confluence_score(df, config=None):
    """Batch convenience wrapper (used by parity_check.py's summary line) —
    the authoritative per-bar score used for actual veto/management decisions
    is computed inside simulate(); this just re-derives it for display."""
    events = simulate(df, config=config)
    return {
        "confluence_complete": True,
        "implemented": sorted(CONFLUENCE_COMPONENTS_IMPLEMENTED),
        "pending": sorted(CONFLUENCE_COMPONENTS_PENDING),
        "event_count": len(events),
    }

