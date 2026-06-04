from execution.alpaca import get_positions, sell
from output.discord import send_exit
from engine import learner


def check_exits():
    positions = get_positions()
    if not positions:
        print("[EXITS] No open positions on Alpaca", flush=True)
        return

    for pos in positions:
        sym = pos["symbol"]
        price = pos["price"]
        entry = pos["entry"]
        qty = pos["qty"]
        stop = pos["stop"]
        target = pos["target"]
        pnl = ((price - entry) / entry) * 100

        print(f"[EXITS] {sym} | Current: ${price:.2f} | Entry: ${entry:.2f} | Stop: ${stop:.2f} | Target: ${target:.2f} | PnL: {pnl:.2f}%", flush=True)

        if price <= stop:
            print(f"[EXITS] STOP HIT on {sym} — selling at ${price:.2f} (PnL: {pnl:.2f}%)", flush=True)
            sell(sym, qty)
            send_exit(sym, "🛑 Stop Loss Hit", entry, price, qty, pnl)
            learner.log_exit(sym, price, "stop_loss", pnl)

        elif price >= target:
            print(f"[EXITS] TARGET HIT on {sym} — selling at ${price:.2f} (PnL: {pnl:.2f}%)", flush=True)
            sell(sym, qty)
            send_exit(sym, "🎯 Take Profit Hit", entry, price, qty, pnl)
            learner.log_exit(sym, price, "take_profit", pnl)