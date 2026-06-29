"""
Run this once to verify Google Sheets connectivity and write a sample trade row.
Usage: python3 test_sheets.py
"""
import os
from datetime import datetime
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials as GCredentials

load_dotenv()

GOOGLE_SPREADSHEET_ID    = os.getenv("GOOGLE_SPREADSHEET_ID", "")
GOOGLE_SPREADSHEET_NAME  = os.getenv("GOOGLE_SPREADSHEET_NAME", "SPY Options Bot Log")
GOOGLE_SERVICE_ACCOUNT_EMAIL = os.getenv("GOOGLE_SERVICE_ACCOUNT_EMAIL", "")
GOOGLE_PRIVATE_KEY       = os.getenv("GOOGLE_PRIVATE_KEY", "").replace("\\n", "\n")
OWNER_EMAIL              = os.getenv("OWNER_EMAIL", "")

_TRADES_HEADERS = [
    "Opened At", "Closed At", "Duration (min)",
    "Symbol", "Contract", "Signal", "Strike", "Expiry", "Qty",
    "Entry ($)", "Target ($)", "Stop ($)",
    "Exit ($)", "PnL (%)", "PnL ($)", "Max PnL (%)", "Reason", "Score", "Status",
]

def main():
    if not GOOGLE_SERVICE_ACCOUNT_EMAIL or not GOOGLE_PRIVATE_KEY:
        print("ERROR: GOOGLE_SERVICE_ACCOUNT_EMAIL or GOOGLE_PRIVATE_KEY not set.")
        return

    print("Authenticating with Google...")
    creds = GCredentials.from_service_account_info(
        {
            "type": "service_account",
            "project_id": "linear-catalyst-468901-g0",
            "private_key": GOOGLE_PRIVATE_KEY,
            "client_email": GOOGLE_SERVICE_ACCOUNT_EMAIL,
            "token_uri": "https://oauth2.googleapis.com/token",
        },
        scopes=[
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
        ],
    )
    gc = gspread.authorize(creds)

    # Open sheet
    if GOOGLE_SPREADSHEET_ID:
        sh = gc.open_by_key(GOOGLE_SPREADSHEET_ID)
        print(f"Opened sheet by ID: '{sh.title}'")
    else:
        try:
            sh = gc.open(GOOGLE_SPREADSHEET_NAME)
            print(f"Opened sheet by name: '{sh.title}'")
        except gspread.exceptions.SpreadsheetNotFound:
            sh = gc.create(GOOGLE_SPREADSHEET_NAME)
            sh.share(None, perm_type="anyone", role="writer")
            print(f"Created new sheet: '{sh.title}'")

    print(f"Sheet URL: https://docs.google.com/spreadsheets/d/{sh.id}")

    # Share to owner if set
    if OWNER_EMAIL:
        try:
            sh.share(OWNER_EMAIL, perm_type="user", role="writer", notify=False)
            print(f"Shared to {OWNER_EMAIL}")
        except Exception as e:
            print(f"Share failed: {e}")

    # Ensure Trades tab exists
    try:
        ws = sh.worksheet("Trades")
        print("Found existing 'Trades' tab.")
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title="Trades", rows=2000, cols=len(_TRADES_HEADERS))
        ws.append_row(_TRADES_HEADERS, value_input_option="USER_ENTERED")
        print("Created 'Trades' tab with headers.")

    # Write a sample row
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sample_row = [
        now,                        # Opened At
        now,                        # Closed At
        "2.5",                      # Duration (min)
        "QQQ",                      # Symbol
        "QQQ260630C00721000",        # Contract
        "STRONG CALL",              # Signal
        "721.0",                    # Strike
        "2026-06-30",               # Expiry
        "5",                        # Qty
        "4.62",                     # Entry ($)
        "6.00",                     # Target ($)
        "4.07",                     # Stop ($)
        "5.98",                     # Exit ($)
        "29.44",                    # PnL (%)
        "68.00",                    # PnL ($)
        "31.00",                    # Max PnL (%)
        "TARGET HIT",               # Reason
        "85",                       # Score
        "TEST — DELETE ME",         # Status
    ]
    ws.append_row(sample_row, value_input_option="USER_ENTERED")
    print(f"\n✅ Sample row written to 'Trades' tab successfully!")
    print(f"Open your sheet and look for status 'TEST — DELETE ME' to confirm.")

if __name__ == "__main__":
    main()
