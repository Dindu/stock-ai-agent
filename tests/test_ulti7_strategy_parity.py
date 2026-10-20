import ast
import unittest
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytz

from engine.ulti7_strategy import PineV95EntryEngine, _pine_base_series
from engine.ulti7_trade_manager import Ulti7Strategy, Ulti7UnderlyingTradeManager


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

    def test_smc_volume_change_uses_previous_bar_where_lazy_call_ran(self):
        volume = [1000.0] * 60 + [3000.0, 0.0, 0.0, 1100.0]
        bars = self._bars(volume)
        result = PineV95EntryEngine._smc_events(bars, _pine_base_series(bars))
        average = bars["volume"].rolling(50).mean()
        short_average = bars["volume"].rolling(5).mean()
        expected = []
        previous_evaluated = float("nan")
        for position in range(len(bars)):
            qualifies = bars["volume"].iloc[position] > average.iloc[position]
            change = short_average.iloc[position] - previous_evaluated if qualifies else float("nan")
            expected.append(bool(qualifies and pd.notna(change) and change > 0))
            if qualifies:
                previous_evaluated = short_average.iloc[position]
        expected = pd.Series(expected, index=bars.index)
        self.assertEqual(bool(result["volume_condition"].iloc[-1]), bool(expected.iloc[-1]))
        immediate_previous_change = short_average.diff().iloc[-1]
        self.assertGreater(immediate_previous_change, 0)
        self.assertFalse(bool(expected.iloc[-1]))

    def test_smc_buy_requires_pine_default_filters(self):
        volume = [1000.0 + position * 10.0 for position in range(70)]
        bars = self._bars(volume)
        result = PineV95EntryEngine._smc_events(bars, _pine_base_series(bars))
        self.assertTrue(bool(result["buy"].iloc[-1]))
        self.assertFalse(bool(result["sell"].iloc[-1]))


class UnderlyingTradeManagerTests(unittest.TestCase):
    def setUp(self):
        index = pd.date_range("2026-10-08 13:00", periods=8, freq="5min", tz="UTC")
        close = pd.Series([100.0] * len(index), index=index)
        self.bars = pd.DataFrame(
            {
                "open": close,
                "high": close + 0.2,
                "low": close - 0.2,
                "close": close,
                "volume": 1000.0,
            },
            index=index,
        )
        self.decision = {
            "side": "CALL",
            "entry_underlying": 100.0,
            "entry_bar": str(index[-1]),
            "atr": 1.0,
        }

    def test_open_uses_spec_targets_and_stays_simulated(self):
        manager = Ulti7UnderlyingTradeManager()
        event = manager.open_from_signal(self.decision, self.bars)
        self.assertEqual(event["kind"], "ENTRY")
        self.assertEqual((event["tp1"], event["tp2"], event["tp3"]), (100.75, 101.5, 102.25))
        self.assertEqual(event["scales"], {"tp1": 0.5, "tp2": 0.15, "runner": 0.35})
        self.assertIsNotNone(manager.position)
        self.assertIsNone(manager.open_from_signal(self.decision, self.bars))

    def test_same_bar_stop_and_target_collision_stops_first_and_models_gap(self):
        manager = Ulti7UnderlyingTradeManager()
        manager.open_from_signal(self.decision, self.bars)
        bar_time = self.bars.index[-1] + pd.Timedelta(minutes=5)
        collision = pd.DataFrame(
            [{"open": 98.0, "high": 102.0, "low": 97.0, "close": 100.0, "volume": 1000.0}],
            index=[bar_time],
        )
        result = manager.advance(pd.concat([self.bars, collision]))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["kind"], "STOP")
        self.assertEqual(result[0]["fill_price"], 98.0)
        self.assertIsNone(manager.position)

    def test_tp1_then_tp2_preserves_runner_fraction(self):
        manager = Ulti7UnderlyingTradeManager()
        manager.open_from_signal(self.decision, self.bars)
        bar_time = self.bars.index[-1] + pd.Timedelta(minutes=5)
        targets = pd.DataFrame(
            [{"open": 100.0, "high": 102.0, "low": 99.9, "close": 101.8, "volume": 1000.0}],
            index=[bar_time],
        )
        events = manager.advance(pd.concat([self.bars, targets]))
        self.assertEqual([event["kind"] for event in events], ["TP1", "TP2"])
        self.assertTrue(manager.position.runner_active)
        self.assertAlmostEqual(manager.position.remaining_fraction, 0.35)

    def test_late4_exits_after_four_bars_before_tp1(self):
        manager = Ulti7UnderlyingTradeManager()
        history = self.bars.copy()
        history["low"] = 95.0
        manager.open_from_signal(self.decision, history)
        events = []
        for offset in range(1, 5):
            bar_time = history.index[-1] + pd.Timedelta(minutes=5)
            failure = pd.DataFrame(
                [{"open": 98.5, "high": 99.0, "low": 97.0, "close": 98.0, "volume": 1000.0}],
                index=[bar_time],
            )
            history = pd.concat([history, failure])
            events = manager.advance(history)
        self.assertEqual([event["kind"] for event in events], ["LATE4"])
        self.assertIsNone(manager.position)

    def test_runner_trail_uses_prior_bars_and_closes_remaining_fraction(self):
        manager = Ulti7UnderlyingTradeManager()
        manager.open_from_signal(self.decision, self.bars)
        history = self.bars
        bar_time = history.index[-1] + pd.Timedelta(minutes=5)
        target_bar = pd.DataFrame(
            [{"open": 100.0, "high": 101.6, "low": 100.6, "close": 101.0, "volume": 1000.0}],
            index=[bar_time],
        )
        history = pd.concat([history, target_bar])
        self.assertEqual([event["kind"] for event in manager.advance(history)], ["TP1", "TP2"])
        for _ in range(3):
            bar_time = history.index[-1] + pd.Timedelta(minutes=5)
            history = pd.concat([history, pd.DataFrame(
                [{"open": 101.0, "high": 101.2, "low": 100.5, "close": 101.0, "volume": 1000.0}],
                index=[bar_time],
            )])
            manager.advance(history)
        bar_time = history.index[-1] + pd.Timedelta(minutes=5)
        trail_bar = pd.DataFrame(
            [{"open": 101.0, "high": 101.1, "low": 100.3, "close": 100.7, "volume": 1000.0}],
            index=[bar_time],
        )
        result = manager.advance(pd.concat([history, trail_bar]))
        self.assertEqual(result[0]["kind"], "STOP")
        self.assertEqual(result[0]["reason"], "PINE RUNNER TRAIL")
        self.assertAlmostEqual(result[0]["fill_price"], 100.4)
        self.assertAlmostEqual(result[0]["realized_r"], 1.48)
        self.assertIsNone(manager.position)

    def test_manager_state_round_trips(self):
        manager = Ulti7UnderlyingTradeManager()
        manager.open_from_signal(self.decision, self.bars)
        snapshot = manager.export_state()
        restored = Ulti7UnderlyingTradeManager()
        restored.restore_state(snapshot)
        self.assertEqual(restored.position.side, "CALL")
        self.assertEqual(restored.position.entry_bar, manager.position.entry_bar)
        self.assertEqual(restored.position.stop, manager.position.stop)

    def test_strategy_snapshot_includes_open_position(self):
        strategy = Ulti7Strategy()
        strategy.trade_manager.open_from_signal(self.decision, self.bars)
        snapshot = strategy.export_state()
        restored = Ulti7Strategy()
        restored.restore_state(snapshot)
        self.assertEqual(restored.trade_manager.position.side, "CALL")
        self.assertEqual(restored.trade_manager.position.entry_bar, strategy.trade_manager.position.entry_bar)

    def test_accepted_signal_while_open_is_unmanaged_but_trade_advances(self):
        class FixedSignalEngine:
            def evaluate(self, symbol, bars, data, confluence, now):
                decision = dict(self.decision)
                decision["entry_bar"] = str(bars.index[-1])
                return decision, "accepted"

            decision = dict(self.decision)

        signal_engine = FixedSignalEngine()
        strategy = Ulti7Strategy(entry_engine=signal_engine)
        first = strategy.on_completed_bars("SPY", self.bars)
        self.assertIsNotNone(first["managed_entry"])
        next_bar = pd.DataFrame(
            [{"open": 100.0, "high": 100.2, "low": 99.8, "close": 100.0, "volume": 1000.0}],
            index=[self.bars.index[-1] + pd.Timedelta(minutes=5)],
        )
        second = strategy.on_completed_bars("SPY", pd.concat([self.bars, next_bar]))
        self.assertTrue(second["accepted_unmanaged"])
        self.assertIsNone(second["managed_entry"])
        self.assertIsNotNone(strategy.trade_manager.position)

    def test_pre_tp1_stop_activates_engine_loss_lock(self):
        class RecordingSignalEngine:
            decision = dict(self.decision)

            def __init__(self):
                self.exits = []

            def evaluate(self, symbol, bars, data, confluence, now):
                if len(bars) == 8:
                    decision = dict(self.decision)
                    decision["entry_bar"] = str(bars.index[-1])
                    return decision, "accepted"
                return None, "no candidate"

            def record_exit(self, symbol, side, is_loss=False, runner=False, bar_time=None):
                self.exits.append((symbol, side, is_loss, runner, bar_time))

        signal_engine = RecordingSignalEngine()
        strategy = Ulti7Strategy(entry_engine=signal_engine)
        strategy.on_completed_bars("SPY", self.bars)
        stop_bar = pd.DataFrame(
            [{"open": 98.0, "high": 99.0, "low": 97.0, "close": 98.0, "volume": 1000.0}],
            index=[self.bars.index[-1] + pd.Timedelta(minutes=5)],
        )
        result = strategy.on_completed_bars("SPY", pd.concat([self.bars, stop_bar]))
        self.assertEqual(result["trade_events"][0]["kind"], "STOP")
        self.assertEqual(signal_engine.exits[0][1:4], ("CALL", True, False))


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
