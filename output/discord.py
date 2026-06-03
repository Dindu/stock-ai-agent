import requests
from datetime import datetime
from config import DISCORD_WEBHOOK

def send(stock):
    symbol   = stock['symbol']
    price    = stock['price']
    qty      = stock.get('qty', 10)
    score    = stock['score']
    change   = stock['change']
    stop     = stock.get('stop', price * 0.97)
    target   = stock.get('target', price * 1.08)
    rr       = stock.get('rr', 0)
    cost     = price * qty
    catalyst = stock.get('catalyst_summary', '')
    reasons  = "\n".join(f"• {r}" for r in stock.get('reasons', []))
    bd       = stock.get('breakdown', {})
    now      = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    score_bar = (
        f"`Catalyst     {bd.get('catalyst', 0):>2}/30`\n"
        f"`Market       {bd.get('market', 0):>2}/20`\n"
        f"`Fundamentals {bd.get('fundamentals', 0):>2}/20`\n"
        f"`Technicals   {bd.get('technicals', 0):>2}/20`\n"
        f"`Sentiment    {bd.get('sentiment', 0):>2}/10`\n"
        f"`─────────────────`\n"
        f"`TOTAL        {score:>2}/100`"
    )

    msg = (
        f"🟢 **BUY SIGNAL — {symbol}**\n"
        f"🕐 {now}\n\n"
        f"**Catalyst:** {catalyst}\n\n"
        f"**Score Breakdown:**\n{score_bar}\n\n"
        f"**Entry:** ${price:.2f} × {qty} shares = ${cost:,.2f}  |  Change: {change:+.2f}%\n"
        f"📈 **Target:** ${target:.2f}  |  🛑 **Stop:** ${stop:.2f}  |  ⚖️ **R:R {rr:.1f}:1**\n\n"
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