#!/usr/bin/env python3
"""Backfill Trades sheet CLOSED rows with combined P&L when partial rows exist.

This repairs historical rows written before combined-P&L close logic was fixed.

Usage:
  python3 backfill_trades_combined_pnl.py            # dry run
  python3 backfill_trades_combined_pnl.py --apply    # apply updates
"""

from __future__ import annotations

import argparse
import os
from datetime import datetime
from typing import Dict, List, Tuple

import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials as GCredentials


def _to_float(value, default=0.0):
    try:
        txt = str(value or "").strip().replace(",", "")
        if txt == "":
            return float(default)
        return float(txt)
    except Exception:
        return float(default)


def _open_trades_worksheet():
    load_dotenv()
    sheet_id = os.getenv("GOOGLE_SPREADSHEET_ID", "").strip()
    sa_email = os.getenv("GOOGLE_SERVICE_ACCOUNT_EMAIL", "").strip()
    private_key = os.getenv("GOOGLE_PRIVATE_KEY", "").replace("\\n", "\n")
    sheet_name = os.getenv("GOOGLE_SPREADSHEET_NAME", "SPY Options Bot Log").strip()

    if not sa_email or not private_key:
        raise RuntimeError("Missing GOOGLE_SERVICE_ACCOUNT_EMAIL or GOOGLE_PRIVATE_KEY in env.")

    creds = GCredentials.from_service_account_info(
        {
            "type": "service_account",
            "project_id": "linear-catalyst-468901-g0",
            "private_key": private_key,
            "client_email": sa_email,
            "token_uri": "https://oauth2.googleapis.com/token",
        },
        scopes=[
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
        ],
    )
    gc = gspread.authorize(creds)
    book = gc.open_by_key(sheet_id) if sheet_id else gc.open(sheet_name)
    return book.worksheet("Trades")


def main():
    parser = argparse.ArgumentParser(description="Backfill combined P&L for CLOSED Trades rows.")
    parser.add_argument("--apply", action="store_true", help="Apply updates to Google Sheets (default is dry-run).")
    parser.add_argument(
        "--pct-tolerance",
        type=float,
        default=0.05,
        help="Tolerance in percentage points to decide if CLOSED row still equals last-leg pct.",
    )
    parser.add_argument("--max-updates", type=int, default=0, help="Optional cap on number of rows to update (0 = no cap).")
    args = parser.parse_args()

    ws = _open_trades_worksheet()
    values = ws.get_all_values()
    if not values:
        print("No Trades data found.")
        return

    headers = values[0]
    idx = {name: i for i, name in enumerate(headers)}

    required = ["Trade ID", "Entry Price", "Exit Price", "Exit Reason", "Status", "P&L", "P&L %", "Updated At"]
    missing = [k for k in required if k not in idx]
    if missing:
        raise RuntimeError(f"Trades headers missing required columns: {missing}")

    by_id: Dict[str, List[Tuple[int, List[str]]]] = {}
    for row_num, row in enumerate(values[1:], start=2):
        tid = str(row[idx["Trade ID"]] if idx["Trade ID"] < len(row) else "").strip()
        if not tid:
            continue
        by_id.setdefault(tid, []).append((row_num, row))

    candidates = []
    for tid, items in by_id.items():
        partial_rows = []
        closed_rows = []
        for row_num, row in items:
            status = str(row[idx["Status"]] if idx["Status"] < len(row) else "").strip().upper()
            if status == "PARTIAL":
                partial_rows.append((row_num, row))
            elif status == "CLOSED":
                closed_rows.append((row_num, row))

        if not partial_rows or not closed_rows:
            continue

        # Most recent CLOSED row for this Trade ID.
        closed_row_num, closed_row = sorted(closed_rows, key=lambda x: x[0])[-1]
        entry = _to_float(closed_row[idx["Entry Price"]])
        exit_px = _to_float(closed_row[idx["Exit Price"]])
        closed_dollar = _to_float(closed_row[idx["P&L"]])
        closed_pct = _to_float(closed_row[idx["P&L %"]])
        if entry <= 0:
            continue

        leg_pct = ((exit_px - entry) / entry) * 100.0

        # Only backfill rows that still look like last-leg values.
        if abs(closed_pct - leg_pct) > args.pct_tolerance:
            continue

        partial_dollar = 0.0
        partial_cost = 0.0
        for _, prow in partial_rows:
            p_d = _to_float(prow[idx["P&L"]])
            p_pct = _to_float(prow[idx["P&L %"]])
            partial_dollar += p_d
            if abs(p_pct) > 1e-9:
                partial_cost += p_d / (p_pct / 100.0)

        if abs(closed_pct) <= 1e-9:
            # Cannot infer final-leg cost from a 0% line robustly.
            continue

        final_leg_cost = closed_dollar / (closed_pct / 100.0)
        total_dollar = partial_dollar + closed_dollar
        total_cost = partial_cost + final_leg_cost
        if abs(total_cost) <= 1e-9:
            continue

        total_pct = (total_dollar / total_cost) * 100.0
        label = "PROFIT" if total_dollar > 0 else "LOSS"
        reason = f"{label} ({total_pct:+.2f}% / ${total_dollar:+.2f})"

        candidates.append(
            {
                "trade_id": tid,
                "row_num": closed_row_num,
                "old_dollar": closed_dollar,
                "old_pct": closed_pct,
                "new_dollar": round(total_dollar, 2),
                "new_pct": round(total_pct, 2),
                "new_reason": reason,
            }
        )

    if args.max_updates > 0:
        candidates = candidates[: args.max_updates]

    print(f"Scanned trade IDs: {len(by_id)}")
    print(f"Rows eligible for backfill: {len(candidates)}")
    for item in candidates[:10]:
        print(
            f"- row {item['row_num']} {item['trade_id']}: "
            f"P&L {item['old_dollar']:+.2f}->{item['new_dollar']:+.2f}, "
            f"P&L% {item['old_pct']:+.2f}->{item['new_pct']:+.2f}"
        )

    if not args.apply:
        print("Dry-run only. Re-run with --apply to write updates.")
        return

    now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # Columns: I=Exit Reason, M=P&L, N=P&L %, Q=Updated At
    requests = []
    for item in candidates:
        r = int(item["row_num"])
        requests.append({"range": f"I{r}", "values": [[item["new_reason"]]]})
        requests.append({"range": f"M{r}", "values": [[item["new_dollar"]]]})
        requests.append({"range": f"N{r}", "values": [[item["new_pct"]]]})
        requests.append({"range": f"Q{r}", "values": [[now_text]]})

    if requests:
        ws.batch_update(requests, value_input_option="USER_ENTERED")

    print(f"Applied updates: {len(candidates)} CLOSED rows")


if __name__ == "__main__":
    main()
