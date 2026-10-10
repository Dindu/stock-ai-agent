import ast
import unittest
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytz

from engine.ulti7_strategy import PineV95EntryEngine, _pine_base_series


class SmcPineParityTests(unittest.TestCase):
    def _bars(self, volume):
        index = pd.date_range("2026-10-08 13:00", periods=len(volume), freq="5min", tz="UTC")
        close = pd.Series([100.0 + position * 0.03 for position in range(len(volume))], index=index)
        volume = pd.Series(volume, index=index, dtype=float)
        return pd.DataFrame(
            {
                "open": close - 0.005,
                "high": close + 0.01,
                "low": close - 0.01,
                "close": close,
                "volume": volume,
            },
            index=index,
        )

    def test_smc_volume_change_uses_immediately_previous_bar(self):
        volume = [1000.0] * 60 + [0.0, 1100.0]
        bars = self._bars(volume)
        result = PineV95EntryEngine._smc_events(bars, _pine_base_series(bars))
        expected = (bars["volume"] > bars["volume"].rolling(50).mean()) & (bars["volume"].rolling(5).mean().diff() > 0)
        self.assertEqual(bool(result["volume_condition"].iloc[-1]), bool(expected.iloc[-1]))
        self.assertTrue(bool(expected.iloc[-1]))

    def test_smc_buy_requires_pine_default_filters(self):
        volume = [1000.0] * 69 + [1500.0]
        bars = self._bars(volume)
        result = PineV95EntryEngine._smc_events(bars, _pine_base_series(bars))
        self.assertTrue(bool(result["buy"].iloc[-1]))
        self.assertFalse(bool(result["sell"].iloc[-1]))


class ScanAlertFormatTests(unittest.TestCase):
    def setUp(self):
        source = ast.parse(Path("spy_options_poll_bot.py").read_text())
        functions = [
            node for node in source.body
            if isinstance(node, ast.FunctionDef) and node.name in {"format_v15_scan_alert", "_trim_text"}
        ]
        scope = {
            "pd": pd, "central": pytz.timezone("America/Chicago"),
            "ETF_SYMBOLS": {"SPY", "QQQ", "IWM"}, "FEED": "iex",
        }
        exec(compile(ast.Module(body=functions, type_ignores=[]), "alert-format", "exec"), scope)
        self.format_alert = scope["format_v15_scan_alert"]
        self.decision = {
            "side": "CALL", "setup": "STRUCTURE", "entry_underlying": 100.0,
            "entry_bar": "2026-10-08T15:10:00Z", "long_votes": 2, "short_votes": 1,
            "call_mtf": 3, "put_mtf": 1,
            "exit_plan": {"tp1": 101.0, "tp2": 101.5, "stop": 99.0},
        }
        self.detected = datetime(2026, 10, 8, 15, 15, 6, tzinfo=timezone.utc)

    def test_call_alert_shows_signal_levels_and_not_a_fill(self):
        message = self.format_alert("SPY", self.decision, {}, self.detected)
        for text in (
            "SPY CALL | SPOT ENTRY $100.00", "`STRUCTURE`",
            "TP1 `$101.00` | TP2 `$101.50` | SL `$99.00`",
            "Bar `10:10 CT`",
        ):
            self.assertIn(text, message)
        self.assertEqual(len(message.splitlines()), 2)

    def test_put_alert_remains_two_lines_with_news(self):
        self.decision.update(side="PUT", setup="REV", entry_bar="2026-10-08 15:10:00")
        self.decision["exit_plan"] = {"tp1": 99.0, "tp2": 98.5, "stop": 101.0}
        message = self.format_alert("MU", self.decision, {"latest_news": "Headline " * 1000}, self.detected)
        self.assertIn("MU PUT | SPOT ENTRY $100.00", message)
        self.assertIn("`REV`", message)
        self.assertIn("TP1 `$99.00` | TP2 `$98.50` | SL `$101.00`", message)
        self.assertNotIn("Headline", message)
        self.assertEqual(len(message.splitlines()), 2)
        self.assertLess(len(message), 250)

    def test_missing_plan_and_news_do_not_invent_levels(self):
        self.decision.pop("exit_plan")
        message = self.format_alert("SPY", self.decision, {"latest_news": "No recent Alpaca news"}, self.detected)
        self.assertNotIn("TP1", message)
        self.assertNotIn("Latest News", message)
        self.assertIn("Levels unavailable", message)
        self.assertNotIn("Alert only, no order", message)
        self.assertEqual(len(message.splitlines()), 2)


if __name__ == "__main__":
    unittest.main()
