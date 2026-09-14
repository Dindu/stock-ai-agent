"""Run the TradingView webhook receiver and option bot together locally."""

import os
import signal
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "tradingview" / "state"


def _runtime_env():
    env = os.environ.copy()
    env.update(
        {
            "PINE_BB_LOCAL_MODE": "0",
            "ULTI_ENTRY_ENABLED": "0",
            "ULTI_EXIT_ENABLED": "0",
            "TRADINGVIEW_ENTRY_ENABLED": "1",
            "TRADINGVIEW_EXIT_ENABLED": "1",
            "TRADINGVIEW_EXACT_STRATEGY_MODE": "1",
            "LEGACY_INTRADAY_ENTRY_ENABLED": "0",
            "LEGACY_INTRADAY_EXIT_ENABLED": "0",
            "SWING_MODE": "0",
            "TRADINGVIEW_ALLOWED_STRATEGY_MODE": "INTRADAY",
            "TRADINGVIEW_STATE_DIR": str(STATE_DIR),
            "TRADINGVIEW_SIGNAL_STATE_FILE": str(STATE_DIR / "latest_signals.json"),
            "TRADINGVIEW_ACTIONABLE_QUEUE_FILE": str(STATE_DIR / "actionable_signals.jsonl"),
            "TRADINGVIEW_CONSUMED_STATE_FILE": str(STATE_DIR / "bot_consumed_signal_ids.json"),
            "TRADINGVIEW_POSITION_OWNERSHIP_FILE": str(STATE_DIR / "tv_position_ownership.json"),
            "ALPACA_FEED": "iex",
        }
    )
    return env


def main():
    env = _runtime_env()
    webhook = subprocess.Popen(
        [sys.executable, "-m", "tradingview.webhook_server"],
        cwd=ROOT,
        env=env,
    )
    bot = None
    try:
        bot = subprocess.Popen(
            [sys.executable, "spy_options_poll_bot.py"],
            cwd=ROOT,
            env=env,
        )
        print(
            "TradingView webhook: http://127.0.0.1:8787/webhook/tradingview\n"
            "Health check: http://127.0.0.1:8787/health\n"
            "Bot authority: TradingView ENTRY/EXIT webhooks only",
            flush=True,
        )
        return bot.wait()
    except KeyboardInterrupt:
        return 130
    finally:
        for process in (bot, webhook):
            if process is not None and process.poll() is None:
                process.send_signal(signal.SIGTERM)
        for process in (bot, webhook):
            if process is not None and process.poll() is None:
                process.wait(timeout=10)


if __name__ == "__main__":
    raise SystemExit(main())