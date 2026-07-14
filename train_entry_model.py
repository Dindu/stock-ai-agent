#!/usr/bin/env python3
"""Train and export a lightweight entry model for ML gate integration.

This script intentionally uses only stdlib + pandas (already in requirements)
so it can run in the current project environment without extra dependencies.

Input expectations (trade_results.csv style):
- underlying (symbol)
- signal (e.g., STRONG CALL / STRONG PUT)
- score (entry score)
- pnl_pct (percent return, e.g. +12.5)

Output:
- models/entry_model.json compatible with spy_options_poll_bot.py ML gate.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
from typing import Dict, List, Tuple

import pandas as pd
from dotenv import load_dotenv

try:
    import gspread
except Exception:
    gspread = None

try:
    from google.oauth2.service_account import Credentials as GCredentials
except Exception:
    GCredentials = None


ETF_SYMBOLS = {"SPY", "QQQ", "IWM"}
TOP_STOCK_SYMBOLS = {
    "AAPL", "NVDA", "MSFT", "AMZN", "META",
    "TSLA", "AMD", "PLTR", "NFLX", "GOOGL",
    "AVGO", "MSTR", "INTC", "COIN", "SPCX",
    "ADBE", "HOOD", "ORCL", "SOFI", "WMT",
}
AGGRESSIVE_STOCK_SYMBOLS = {"TSLA", "AMD", "PLTR", "SMCI", "COIN", "SOFI", "GOOGL"}


FULL_FEATURE_ORDER = [
    "is_call",
    "is_put",
    "is_etf",
    "is_top_stock",
    "is_aggressive_stock",
    "effective_score",
    "raw_score",
    "macro_penalty",
    "side_score",
    "opposite_score",
    "dominance",
    "delta_5m",
    "delta_10m",
    "momentum",
    "volume",
    "regime",
    "pattern",
    "momentum_quality",
    "ema20_slope_pct",
    "rsi",
    "vol_ratio",
    "vwap_extension_pct",
    "watchlist_promoted",
]


def _sigmoid(x: float) -> float:
    z = max(-50.0, min(50.0, float(x)))
    return 1.0 / (1.0 + math.exp(-z))


def _detect_side(signal: str) -> str:
    s = str(signal or "").upper()
    if "PUT" in s:
        return "PUT"
    return "CALL"


def _normalize_training_input(df: pd.DataFrame) -> pd.DataFrame:
    """Map supported input schemas into canonical columns.

    Canonical columns:
    - underlying
    - signal
    - score
    - pnl_pct
    """
    cols = {str(c).strip().lower(): c for c in df.columns}

    # Native trade_results.csv schema.
    if all(k in cols for k in ("underlying", "signal", "score", "pnl_pct")):
        out = pd.DataFrame()
        out["underlying"] = df[cols["underlying"]]
        out["signal"] = df[cols["signal"]]
        out["score"] = df[cols["score"]]
        out["pnl_pct"] = df[cols["pnl_pct"]]
        return out

    # Alerts sheet schema.
    alerts_needed = ("symbol", "side", "bull score", "bear score", "p&l %")
    if all(k in cols for k in alerts_needed):
        out = pd.DataFrame()
        out["underlying"] = df[cols["symbol"]]
        side_series = df[cols["side"]].astype(str).str.upper().str.strip()
        bull = pd.to_numeric(df[cols["bull score"]], errors="coerce")
        bear = pd.to_numeric(df[cols["bear score"]], errors="coerce")
        out["signal"] = side_series.map(lambda s: "STRONG PUT" if s == "PUT" else "STRONG CALL")
        out["score"] = bull.where(side_series != "PUT", bear)
        out["pnl_pct"] = pd.to_numeric(df[cols["p&l %"]], errors="coerce")
        return out

    # Trades sheet schema fallback (no score field, use a neutral proxy).
    trades_needed = ("symbol", "direction", "p&l %")
    if all(k in cols for k in trades_needed):
        out = pd.DataFrame()
        out["underlying"] = df[cols["symbol"]]
        direction = df[cols["direction"]].astype(str).str.upper().str.strip()
        out["signal"] = direction.map(lambda s: "STRONG PUT" if s == "PUT" else "STRONG CALL")
        out["score"] = 75.0
        out["pnl_pct"] = pd.to_numeric(df[cols["p&l %"]], errors="coerce")
        return out

    raise ValueError(
        "Unsupported input schema. Expected one of: "
        "trade_results.csv columns, Alerts sheet columns, or Trades sheet columns."
    )


def _build_gspread_client() -> gspread.Client:
    if gspread is None or GCredentials is None:
        raise ValueError("Google Sheets dependencies not installed (gspread/google-auth).")

    load_dotenv()
    client_email = os.getenv("GOOGLE_SERVICE_ACCOUNT_EMAIL", "")
    private_key = os.getenv("GOOGLE_PRIVATE_KEY", "").replace("\\n", "\n")
    if not client_email or not private_key:
        raise ValueError("Missing GOOGLE_SERVICE_ACCOUNT_EMAIL or GOOGLE_PRIVATE_KEY in environment.")

    creds = GCredentials.from_service_account_info(
        {
            "type": "service_account",
            "project_id": "linear-catalyst-468901-g0",
            "private_key": private_key,
            "client_email": client_email,
            "token_uri": "https://oauth2.googleapis.com/token",
        },
        scopes=[
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
        ],
    )
    return gspread.authorize(creds)


def load_frame_from_sheets(sheet_tab: str = "Alerts") -> pd.DataFrame:
    load_dotenv()
    gc = _build_gspread_client()
    sheet_id = str(os.getenv("GOOGLE_SPREADSHEET_ID", "") or "").strip()
    sheet_name = str(os.getenv("GOOGLE_SPREADSHEET_NAME", "SPY Options Bot Log") or "").strip()

    if sheet_id:
        ss = gc.open_by_key(sheet_id)
    else:
        ss = gc.open(sheet_name)

    ws = ss.worksheet(sheet_tab)
    records = ws.get_all_records()
    if not records:
        return pd.DataFrame()
    return pd.DataFrame(records)


def build_training_frame(df: pd.DataFrame) -> Tuple[pd.DataFrame, pd.Series, pd.Series]:
    d = _normalize_training_input(df)
    d["underlying"] = d["underlying"].astype(str).str.upper().str.strip()
    d["side"] = d["signal"].map(_detect_side)
    d["score"] = pd.to_numeric(d["score"], errors="coerce")
    d["pnl_pct"] = pd.to_numeric(d["pnl_pct"], errors="coerce")
    d = d.dropna(subset=["score", "pnl_pct"]).copy()

    features = pd.DataFrame(index=d.index)
    features["is_call"] = (d["side"] == "CALL").astype(float)
    features["is_put"] = (d["side"] == "PUT").astype(float)
    features["is_etf"] = d["underlying"].isin(ETF_SYMBOLS).astype(float)
    features["is_top_stock"] = d["underlying"].isin(TOP_STOCK_SYMBOLS).astype(float)
    features["is_aggressive_stock"] = d["underlying"].isin(AGGRESSIVE_STOCK_SYMBOLS).astype(float)

    # Columns available from current trade log schema.
    features["effective_score"] = d["score"].astype(float)
    features["raw_score"] = d["score"].astype(float)
    features["side_score"] = d["score"].astype(float)

    # Unknown-at-log-time runtime features default to neutral/zero.
    defaults = {
        "macro_penalty": 0.0,
        "opposite_score": 0.0,
        "dominance": d["score"].astype(float),
        "delta_5m": 0.0,
        "delta_10m": 0.0,
        "momentum": 0.0,
        "volume": 0.0,
        "regime": 0.0,
        "pattern": 0.0,
        "momentum_quality": 0.0,
        "ema20_slope_pct": 0.0,
        "rsi": 50.0,
        "vol_ratio": 1.0,
        "vwap_extension_pct": 0.0,
        "watchlist_promoted": 0.0,
    }
    for k, v in defaults.items():
        features[k] = v

    # Ensure full expected order exists.
    for f in FULL_FEATURE_ORDER:
        if f not in features.columns:
            features[f] = 0.0

    y = (d["pnl_pct"] > 0.0).astype(float)
    y_return = (d["pnl_pct"] / 100.0).astype(float)
    return features[FULL_FEATURE_ORDER], y, y_return


def standardize_matrix(x: pd.DataFrame) -> Tuple[pd.DataFrame, Dict[str, Dict[str, float]]]:
    stats: Dict[str, Dict[str, float]] = {}
    xz = x.copy().astype(float)
    for col in x.columns:
        mean = float(x[col].mean())
        std = float(x[col].std(ddof=0))
        if std < 1e-9:
            std = 1.0
        xz[col] = (x[col] - mean) / std
        stats[col] = {"mean": mean, "std": std}
    return xz, stats


def train_logistic_gd(x: pd.DataFrame, y: pd.Series, epochs: int = 800, lr: float = 0.05, l2: float = 0.002) -> Tuple[List[float], float]:
    cols = list(x.columns)
    w = {c: 0.0 for c in cols}
    b = 0.0
    n = max(1, len(x))

    for _ in range(max(50, epochs)):
        grad_w = {c: 0.0 for c in cols}
        grad_b = 0.0

        for idx in x.index:
            z = b
            for c in cols:
                z += w[c] * float(x.at[idx, c])
            p = _sigmoid(z)
            err = p - float(y.at[idx])
            grad_b += err
            for c in cols:
                grad_w[c] += err * float(x.at[idx, c])

        grad_b /= n
        for c in cols:
            grad = (grad_w[c] / n) + (l2 * w[c])
            w[c] -= lr * grad
        b -= lr * grad_b

    weights = [float(w[c]) for c in cols]
    return weights, float(b)


def train_linear_return_head(x: pd.DataFrame, y_return: pd.Series, epochs: int = 800, lr: float = 0.03, l2: float = 0.001) -> Tuple[List[float], float]:
    cols = list(x.columns)
    w = {c: 0.0 for c in cols}
    b = 0.0
    n = max(1, len(x))

    for _ in range(max(50, epochs)):
        grad_w = {c: 0.0 for c in cols}
        grad_b = 0.0

        for idx in x.index:
            pred = b
            for c in cols:
                pred += w[c] * float(x.at[idx, c])
            err = pred - float(y_return.at[idx])
            grad_b += err
            for c in cols:
                grad_w[c] += err * float(x.at[idx, c])

        grad_b /= n
        for c in cols:
            grad = (grad_w[c] / n) + (l2 * w[c])
            w[c] -= lr * grad
        b -= lr * grad_b

    weights = [float(w[c]) for c in cols]
    return weights, float(b)


def evaluate_basic(xz: pd.DataFrame, y: pd.Series, weights: List[float], bias: float, threshold: float) -> Dict[str, float]:
    probs = []
    preds = []
    for idx in xz.index:
        z = float(bias)
        for i, c in enumerate(xz.columns):
            z += float(weights[i]) * float(xz.at[idx, c])
        p = _sigmoid(z)
        probs.append(p)
        preds.append(1.0 if p >= threshold else 0.0)

    y_vals = [float(v) for v in y.tolist()]
    total = max(1, len(y_vals))
    correct = sum(1 for i in range(total) if preds[i] == y_vals[i])
    wins = sum(1 for v in y_vals if v == 1.0)
    pred_wins = sum(1 for v in preds if v == 1.0)
    tp = sum(1 for i in range(total) if preds[i] == 1.0 and y_vals[i] == 1.0)

    precision = (tp / pred_wins) if pred_wins else 0.0
    recall = (tp / wins) if wins else 0.0
    accuracy = correct / total
    avg_prob = sum(probs) / total
    return {
        "samples": float(total),
        "accuracy": float(accuracy),
        "precision": float(precision),
        "recall": float(recall),
        "avg_probability": float(avg_prob),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Train/export ML gate model from trade_results.csv.")
    parser.add_argument("--input", default="trade_results.csv", help="Path to trade results CSV")
    parser.add_argument("--source", choices=["auto", "csv", "sheets"], default="auto", help="Training data source")
    parser.add_argument("--sheet-tab", default="Alerts", help="Worksheet tab name when source=sheets (Alerts recommended)")
    parser.add_argument("--output", default="models/entry_model.json", help="Output model JSON path")
    parser.add_argument("--min-samples", type=int, default=40, help="Minimum rows required for training")
    parser.add_argument("--threshold", type=float, default=0.62, help="Initial probability threshold")
    parser.add_argument("--epochs", type=int, default=1000, help="Gradient descent epochs")
    parser.add_argument("--lr", type=float, default=0.04, help="Learning rate")
    args = parser.parse_args()

    out_path = Path(args.output)
    source = str(args.source).lower()

    raw = pd.DataFrame()
    source_desc = ""
    if source in ("auto", "csv"):
        in_path = Path(args.input)
        if in_path.exists():
            raw = pd.read_csv(in_path)
            source_desc = str(in_path)
        elif source == "csv":
            raise SystemExit(f"Input CSV not found: {in_path}")

    if raw.empty and source in ("auto", "sheets"):
        try:
            raw = load_frame_from_sheets(sheet_tab=str(args.sheet_tab))
            source_desc = f"google_sheet:{args.sheet_tab}"
        except Exception as e:
            if source == "sheets":
                raise SystemExit(f"Failed to load sheet data: {e}")

    if raw.empty:
        raise SystemExit("No training data found. Provide CSV or ensure Google Sheet has rows.")
    x, y, y_ret = build_training_frame(raw)

    if len(x) < max(5, args.min_samples):
        raise SystemExit(
            f"Not enough samples for training: {len(x)} rows (need >= {args.min_samples})."
        )

    xz, stats = standardize_matrix(x)

    weights, bias = train_logistic_gd(
        xz,
        y,
        epochs=max(100, int(args.epochs)),
        lr=float(args.lr),
        l2=0.002,
    )
    er_weights, er_bias = train_linear_return_head(
        xz,
        y_ret,
        epochs=max(100, int(args.epochs)),
        lr=float(max(0.005, args.lr * 0.7)),
        l2=0.001,
    )

    threshold = max(0.0, min(1.0, float(args.threshold)))
    metrics = evaluate_basic(xz, y, weights, bias, threshold)

    model = {
        "feature_order": FULL_FEATURE_ORDER,
        "weights": [float(w) for w in weights],
        "bias": float(bias),
        "threshold": float(threshold),
        "feature_stats": stats,
        "expected_return": {
            "feature_order": FULL_FEATURE_ORDER,
            "weights": [float(w) for w in er_weights],
            "bias": float(er_bias),
        },
        "training": {
            "source": source_desc,
            "rows": int(len(x)),
            "metrics": metrics,
        },
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(model, indent=2), encoding="utf-8")

    print("Model exported:", out_path)
    print("Rows:", len(x))
    print("Metrics:", json.dumps(metrics, indent=2))


if __name__ == "__main__":
    main()
