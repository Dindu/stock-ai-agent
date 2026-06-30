import sys
import requests

sys.path.insert(0, '.')
from config import DISCORD_WEBHOOK


def send(message: str, label: str) -> None:
    r = requests.post(DISCORD_WEBHOOK, json={"content": message}, timeout=15)
    print(f"{label}: status={r.status_code}")
    if r.status_code >= 300:
        print(r.text)
        raise SystemExit(1)


def main() -> None:
    if not DISCORD_WEBHOOK:
        raise SystemExit("DISCORD_WEBHOOK is not configured.")

    entry_msg = (
        "🚀 **ENTRY ALERT**\n\n"
        "🚀 **QQQ260630C00722000 | $5.25 | 2026-06-30**\n"
        "------------------------------\n"
        "📊 **Setup:** `INDEX SLOW DRIFT` | Score: `81/100` (A)\n"
        "🎯 **Plan:** Entry `$5.25 - $5.29` | Target `+25% / +50%` | Stop `-25%`\n"
        "🛑 **Invalidation:** VWAP loss\n"
        "🤖 **AI:** 🟡 **MEDIUM** — balanced setup with no major disqualifier\n\n"
        "📌 **Action:** ENTERED (`1` contract)"
    )

    exit_msg = (
        "🔴 **EXIT ALERT**\n\n"
        "❌ **LOSS** — QQQ260630P00707000 | `-26.10%`\n"
        "------------------------------\n"
        "📊 **Trade:** `$7.39 -> $5.46` | `5m`\n"
        "📉 **Result:** `STOP LOSS`\n"
        "🧠 **Summary:** `Breakout failed`\n"
        "🏆 **Grade:** `C`\n\n"
        "📌 **Outcome:** `VALID LOSS (RULES FOLLOWED)`"
    )

    send(entry_msg, "ENTRY")
    send(exit_msg, "EXIT")
    print("Done: sample entry/exit alerts sent.")


if __name__ == "__main__":
    main()
