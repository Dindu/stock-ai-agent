"""Launch the existing WebSocket options bot."""

import os


os.environ.setdefault("ALPACA_FEED", "iex")
os.environ.setdefault("MOMENTUM_BREAKOUT_BYPASS_ENABLED", "1")
os.environ["ONE_MINUTE_ENTRY_ENABLED"] = "0"
os.environ["ENTRY_TIMING_V1_ENABLED"] = "0"

from spy_options_poll_bot import main as run_options_bot


if __name__ == "__main__":
    run_options_bot()
