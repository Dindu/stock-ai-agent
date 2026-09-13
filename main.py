"""Launch the Pine-aligned options bot.

The previous launcher started the unrelated AI/stock strategy.  Keep the
stable ``python main.py`` entrypoint, but make the Pine strategy the only
runtime authority.
"""

import os


# Use the local Python port of the Pine BB/ULTI strategy. TradingView webhook
# execution remains a separate integration for users who explicitly configure it.
os.environ["PINE_BB_LOCAL_MODE"] = "1"
os.environ["ULTI_ENTRY_ENABLED"] = "1"
os.environ["ULTI_EXIT_ENABLED"] = "1"
os.environ["SWING_MODE"] = "0"
os.environ["LEGACY_INTRADAY_ENTRY_ENABLED"] = "0"
os.environ["LEGACY_INTRADAY_EXIT_ENABLED"] = "0"
os.environ["TRADINGVIEW_ENTRY_ENABLED"] = "0"
os.environ["TRADINGVIEW_EXIT_ENABLED"] = "0"
os.environ["TRADINGVIEW_EXACT_STRATEGY_MODE"] = "0"
os.environ["ALPACA_FEED"] = "sip"

from spy_options_poll_bot import main as run_pine_bot


if __name__ == "__main__":
    run_pine_bot()