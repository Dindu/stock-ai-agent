import requests
from datetime import datetime
from config import DISCORD_WEBHOOK

def send(stock):
    symbol  = stock['symbol']
    price   = stock['price']
    qty     = stock.get('qty', 10)
    score   = stock['score']
    change  = stock['change']
    stop    = stock.get('stop', price * 0.97)
    target  = stock.get('target', price * 1.08)
    cost    = price * qty
    reasons = "\n".join(f"• {r}" for r in stock.get('reasons', []))
    now     = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    msg = (
        f"🟢 **BUY SIGNAL — {symbol}**\n"
        f"🕐 {now}\n\n"
        f"**Entry:** ${price:.2f} × {qty} shares = ${cost:,.2f}\n"
        f"**AI Score:** {score:.0f}/100  |  Change today: {change:+.2f}%\n\n"
        f"📈 **Take Profit:** ${target:.2f}  (+8%)\n"
        f"🛑 **Stop Loss:**   ${stop:.2f}  (-3%)\n\n"
        f"**Reasons:**\n{reasons}"
    )

    requests.post(DISCORD_WEBHOOK, json={"content": msg})

def send_exit(symbol, reason, entry, exit_price, qty, pnl):
    now     = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    pnl_amt = (exit_price - entry) * qty
    emoji   = "🟢" if pnl >= 0 else "🔴"

    msg = (
        f"{emoji} **EXIT — {symbol}**\n"
        f"🕐 {now}\n\n"
        f"**Reason:** {reason}\n"
        f"**Entry:** ${entry:.2f}  →  **Exit:** ${exit_price:.2f}\n"
        f"**Qty:** {qty} shares\n"
        f"**P&L:** {pnl:+.2f}%  (${pnl_amt:+.2f})"
    )

    requests.post(DISCORD_WEBHOOK, json={"content": msg})