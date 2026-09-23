"""Compare a TradingView exported CSV against ULTI-6 Python outputs.

Expected CSV columns can include any of these Python output columns plus timestamp.
For a practical parity test, export at least:
  timestamp, bull_score, bear_score, final_buy, final_sell
and add component/debug columns when diagnosing a mismatch.
"""
from __future__ import annotations
import argparse
import numpy as np
import pandas as pd
from engine.ulti6_strategy import ULTI6Config, ULTI6Strategy


TV_TO_PYTHON = {
    "py_parity_ema": "ema",
    "py_parity_vwap": "vwap",
    "py_parity_smc_trend_strength": "smc_trend_strength",
    "py_parity_smc_confidence": "smc_confidence",
    "py_parity_smc_trend_1": "smc_trend_1",
    "py_parity_smc_trend_5": "smc_trend_5",
    "py_parity_smc_trend_15": "smc_trend_15",
    "py_parity_smc_trend_30": "smc_trend_30",
    "py_parity_smc_trend_1h": "smc_trend_1h",
    "py_parity_smc_trend_4h": "smc_trend_4h",
    "py_parity_smc_trend_d": "smc_trend_d",
    "py_parity_poki_sar": "poki_sar",
    "py_parity_poki_slope": "poki_slope",
    "py_parity_poki_direction": "poki_direction",
    "py_parity_volume_ma": "volume_ma",
    "py_parity_volume_ratio": "volume_ratio",
    "py_parity_ema_bull": "ema_bull",
    "py_parity_ema_bear": "ema_bear",
    "py_parity_vwap_bull": "vwap_bull",
    "py_parity_vwap_bear": "vwap_bear",
    "py_parity_smc_bull": "smc_bull_score",
    "py_parity_smc_bear": "smc_bear_score",
    "py_parity_poki_bull": "poki_bull",
    "py_parity_poki_bear": "poki_bear",
    "py_parity_volume_bull": "volume_bull",
    "py_parity_volume_bear": "volume_bear",
    "py_parity_pat_bull": "pat_bull_score",
    "py_parity_pat_bear": "pat_bear_score",
    "py_parity_bull_score": "bull_score",
    "py_parity_bear_score": "bear_score",
    "py_parity_final_buy": "final_buy",
    "py_parity_final_sell": "final_sell",
}


def load_frame(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    lower = {str(c).strip().lower(): c for c in df.columns}
    for name in ["timestamp", "time", "date"]:
        if name in lower:
            ts_col = lower[name]
            break
    else:
        raise ValueError("CSV needs a timestamp/time/date column")
    ts = pd.to_datetime(df[ts_col], utc=True)
    df = df.drop(columns=[ts_col]).copy()
    df.index = ts
    df.columns = [str(c).strip().lower() for c in df.columns]
    df = df.rename(columns={column: TV_TO_PYTHON[column] for column in df.columns if column in TV_TO_PYTHON})
    return df


def compare(tv: pd.DataFrame, py: pd.DataFrame, tolerance: float = 1e-10):
    cols = [c for c in tv.columns if c in py.columns and c != "timestamp"]
    if not cols:
        raise ValueError("No overlapping comparison columns found")
    common_index = tv.index.intersection(py.index)
    if common_index.empty:
        raise ValueError("No overlapping timestamps between TradingView and Python data")
    a = tv.loc[common_index, cols]
    py_common = py.loc[common_index, cols]
    rows = []
    for col in cols:
        x, y = a[col].to_numpy(), py_common[col].to_numpy()
        if col.endswith(("_bull", "_bear", "_buy", "_sell")):
            xf = pd.to_numeric(pd.Series(x), errors="coerce").fillna(0).astype(bool).to_numpy()
            yf = pd.to_numeric(pd.Series(y), errors="coerce").fillna(0).astype(bool).to_numpy()
            neq = xf != yf
            max_err = np.nan
        else:
            xf = pd.to_numeric(pd.Series(x), errors="coerce").to_numpy(dtype=float)
            yf = pd.to_numeric(pd.Series(y), errors="coerce").to_numpy(dtype=float)
            both = np.isfinite(xf) & np.isfinite(yf)
            neq = both & (np.abs(xf - yf) > tolerance)
            neq |= np.isfinite(xf) ^ np.isfinite(yf)
            max_err = float(np.nanmax(np.abs(xf - yf))) if np.any(np.isfinite(xf) & np.isfinite(yf)) else np.nan
        rows.append({"column": col, "mismatches": int(np.sum(neq)), "max_abs_error": max_err})
    return pd.DataFrame(rows).sort_values(["mismatches", "column"], ascending=[False, True])


def first_mismatch(tv: pd.DataFrame, py: pd.DataFrame, tolerance: float = 1e-10):
    common = [column for column in tv.columns if column in py.columns]
    aligned_tv, aligned_py = tv[common].align(py[common], join="inner", axis=0)
    for timestamp in aligned_py.index:
        for column in common:
            tv_value, py_value = aligned_tv.at[timestamp, column], aligned_py.at[timestamp, column]
            try:
                tv_num = float(tv_value)
                py_num = float(py_value)
                if np.isfinite(tv_num) and np.isfinite(py_num):
                    different = abs(tv_num - py_num) > tolerance
                else:
                    different = np.isfinite(tv_num) != np.isfinite(py_num)
            except (TypeError, ValueError):
                different = str(tv_value).strip().lower() != str(py_value).strip().lower()
            if different:
                return timestamp, column, tv_value, py_value
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--candles", required=True, help="Python input OHLCV CSV")
    ap.add_argument("--tv", required=True, help="TradingView exported debug CSV")
    ap.add_argument("--mtf-dir", required=True, help="Directory containing 1m.csv, 5m.csv, 15m.csv, 30m.csv, 1h.csv, 4h.csv, and D.csv")
    ap.add_argument("--tolerance", type=float, default=1e-10)
    ap.add_argument("--output", default="ulti6_parity_report.csv")
    args = ap.parse_args()
    candles = load_frame(args.candles)
    tv = load_frame(args.tv)
    mtf_names = {"1": "1m.csv", "15": "15m.csv", "30": "30m.csv", "60": "1h.csv", "240": "4h.csv", "D": "D.csv"}
    mtf = {"5": candles}
    mtf.update({key: load_frame(f"{args.mtf_dir}/{filename}") for key, filename in mtf_names.items()})
    strategy = ULTI6Strategy(ULTI6Config())
    strategy.validate_exact_inputs(candles, mtf=mtf, comparison_start=tv.index.min())
    py = strategy.calculate(candles, mtf=mtf)
    report = compare(tv, py, tolerance=args.tolerance)
    report.to_csv(args.output, index=False)
    matched_rows = len(tv.index.intersection(py.index))
    mismatched_rows = int((report["mismatches"] > 0).sum())
    print(f"Matched timestamps: {matched_rows}")
    print(f"Mismatched fields: {mismatched_rows}")
    print(report.to_string(index=False))
    mismatch = first_mismatch(tv, py, tolerance=args.tolerance)
    if mismatch:
        timestamp, column, tv_value, py_value = mismatch
        print(f"First mismatch: {timestamp} | {column} | TV={tv_value!r} | Python={py_value!r}")
    else:
        print("First mismatch: none")
    print(f"Saved: {args.output}")

if __name__ == "__main__":
    main()
