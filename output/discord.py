import requests
from datetime import datetime
from config import DISCORD_WEBHOOK

def _score_bar(bd, score):
    return (
        f"`Catalyst     {bd.get('catalyst', 0):>2}/30`\n"
        f"`Fundamentals {bd.get('fundamentals', 0):>2}/15`\n"
        f"`Market       {bd.get('market', 0):>2}/10`\n"
        f"`Insider      {bd.get('insider', 0):>2}/20`\n"
        f"`Accumulation {bd.get('accumulation', 0):>2}/15`\n"
        f"`Technicals   {bd.get('technicals', 0):>2}/10`\n"
        f"`──────────────────`\n"
        f"`TOTAL        {score:>2}/100`"
    )

def send(stock):
    symbol      = stock['symbol']
    price       = stock['price']
    qty         = stock.get('qty', 10)
    score       = stock['score']
    change      = stock['change']
    stop        = stock.get('stop', price * 0.97)
    target      = stock.get('target', price * 1.08)
    rr          = stock.get('rr', 0)
    cost        = price * qty
    catalyst    = stock.get('catalyst_summary', '')
    trade_type  = stock.get('trade_type', '').upper()
    hold_period = stock.get('hold_period', '1-2 weeks')
    reasons     = "\n".join(f"• {r}" for r in stock.get('reasons', []))
    flags       = "\n".join(stock.get('flags', []))
    bd          = stock.get('breakdown', {})
    now         = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    type_emoji = {"BREAKOUT": "🚀", "MOMENTUM": "📈", "REVERSAL": "🔄", "AVOID": "⛔"}.get(trade_type, "📊")
    score_bar  = _score_bar(bd, score)

    msg = (
        f"🟢 **GEM FOUND — {symbol}** {type_emoji} `{trade_type}`\n"
        f"🕐 {now}\n\n"
        f"**Opportunity:** {catalyst}\n\n"
        f"**Score Breakdown:**\n{score_bar}\n\n"
        f"**Entry:** ${price:.2f} × {qty} shares = ${cost:,.2f}  |  Change: {change:+.2f}%\n"
        f"📈 **Target:** ${target:.2f}  |  🛑 **Stop:** ${stop:.2f}  |  ⚖️ **R:R {rr:.1f}:1**\n"
        f"🗓️ **Hold:** {hold_period}\n"
    )
    if flags:
        msg += f"\n**Signals:**\n{flags}\n"
    if reasons:
        msg += f"\n**Reasons:**\n{reasons}"

    requests.post(DISCORD_WEBHOOK, json={"content": msg})


def send_watchlist(stock):
    symbol        = stock['symbol']
    price         = stock['price']
    score         = stock['score']
    change        = stock['change']
    trigger_price = stock.get('trigger_price', price * 1.01)
    stop          = stock.get('stop', price * 0.97)
    target        = stock.get('target', price * 1.08)
    catalyst      = stock.get('catalyst_summary', '')
    trade_type    = stock.get('trade_type', '').upper()
    hold_period   = stock.get('hold_period', '1-2 weeks')
    reasons       = "\n".join(f"• {r}" for r in stock.get('reasons', []))
    flags         = "\n".join(stock.get('flags', []))
    bd            = stock.get('breakdown', {})
    now           = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    type_emoji = {"BREAKOUT": "🚀", "MOMENTUM": "📈", "REVERSAL": "🔄"}.get(trade_type, "📊")
    score_bar  = _score_bar(bd, score)

    msg = (
        f"📋 **WATCHLIST — {symbol}** {type_emoji} `{trade_type}`\n"
        f"🕐 {now}\n\n"
        f"**Opportunity:** {catalyst}\n\n"
        f"**Score Breakdown:**\n{score_bar}\n\n"
        f"🎯 **Buy trigger:** ${trigger_price:.2f}  *(breakout confirmation)*\n"
        f"🛑 **Stop:** ${stop:.2f}  |  📈 **Target:** ${target:.2f}\n"
        f"🗓️ **Hold:** {hold_period}  |  Change today: {change:+.2f}%\n"
    )
    if flags:
        msg += f"\n**Signals:**\n{flags}\n"
    if reasons:
        msg += f"\n**Reasons:**\n{reasons}"

    requests.post(DISCORD_WEBHOOK, json={"content": msg})


def send_exit(symbol, reason, entry, exit_price, qty, pnl):
    now     = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    pnl_amt = (exit_price - entry) * qty

    if pnl >= 5:
        outcome = "🟢 **PROFIT**"
        bar = "█" * min(int(pnl), 20)
    elif pnl > 0:
        outcome = "🟡 **SMALL PROFIT**"
        bar = "▒" * min(int(pnl * 2), 20)
    elif pnl >= -3:
        outcome = "🔴 **SMALL LOSS**"
        bar = "░" * min(int(abs(pnl) * 2), 20)
    else:
        outcome = "🔴 **LOSS**"
        bar = "░" * min(int(abs(pnl)), 20)

    msg = (
        f"{'🟢' if pnl >= 0 else '🔴'} **EXITING {symbol}**\n"
        f"🕐 {now}\n\n"
        f"**Reason:** {reason}\n\n"
        f"**Entry:** ${entry:.2f}  →  **Exit:** ${exit_price:.2f}\n"
        f"**Shares:** {qty}  |  **Move:** {((exit_price - entry) / entry * 100):+.2f}%\n\n"
        f"{outcome}\n"
        f"`{bar}` {pnl:+.2f}%  (${pnl_amt:+.2f})"
    )

    requests.post(DISCORD_WEBHOOK, json={"content": msg})
