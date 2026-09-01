import unittest

import spy_options_poll_bot as bot


class EntryTimingV1Tests(unittest.TestCase):
    def setUp(self):
        bot._entry_timing_v1_state.clear()
        self.original_enabled = bot.ENTRY_TIMING_V1_ENABLED
        bot.ENTRY_TIMING_V1_ENABLED = True

    def tearDown(self):
        bot.ENTRY_TIMING_V1_ENABLED = self.original_enabled
        bot._entry_timing_v1_state.clear()

    def _data(self, side, **updates):
        if side == "CALL":
            data = {
                "price": 100.10,
                "vwap": 100.00,
                "ema9": 100.05,
                "ema20": 99.90,
                "atr14": 1.00,
                "vwap_extension_pct": 0.001,
                "vol_ratio": 1.20,
                "bullish_candle": True,
            }
        else:
            data = {
                "price": 99.90,
                "vwap": 100.00,
                "ema9": 99.95,
                "ema20": 100.10,
                "atr14": 1.00,
                "vwap_extension_pct": 0.001,
                "vol_ratio": 1.20,
                "bearish_candle": True,
            }
        data.update(updates)
        return data

    def _evaluate(self, side, **updates):
        return bot._entry_timing_v1_evaluate("SPY", side, self._data(side, **updates))

    def test_call_and_put_require_two_distinct_completed_reclaim_candles(self):
        for side in ("CALL", "PUT"):
            with self.subTest(side=side):
                allowed, state, _, _ = self._evaluate(side, one_minute_entry_confirmed=False, one_minute_bar_time="")
                self.assertEqual((state, allowed), ("SETUP_FORMING", False))

                allowed, state, _, _ = self._evaluate(side, one_minute_entry_confirmed=True, one_minute_bar_time="2026-09-01 09:35:00-05:00")
                self.assertEqual((state, allowed), ("RECLAIM", False))

                allowed, state, _, _ = self._evaluate(side, one_minute_entry_confirmed=True, one_minute_bar_time="2026-09-01 09:35:00-05:00")
                self.assertEqual((state, allowed), ("RECLAIM", False))

                allowed, state, _, _ = self._evaluate(side, one_minute_entry_confirmed=True, one_minute_bar_time="2026-09-01 09:36:00-05:00")
                self.assertEqual((state, allowed), ("CONFIRMED", False))

                allowed, state, _, _ = self._evaluate(side, one_minute_entry_confirmed=True, one_minute_bar_time="2026-09-01 09:36:00-05:00")
                self.assertEqual((state, allowed), ("ENTRY_WINDOW", True))

    def test_expired_window_is_missed_until_a_fresh_reset(self):
        key = ("SPY", "CALL")
        bot._entry_timing_v1_state[key] = {
            "state": "ENTRY_WINDOW",
            "entry_window_started": bot.datetime.now(bot.central) - bot.timedelta(minutes=bot.ENTRY_TIMING_V1_ENTRY_WINDOW_MINUTES + 1),
        }
        allowed, state, _, _ = self._evaluate("CALL", one_minute_entry_confirmed=True, one_minute_bar_time="2026-09-01 09:40:00-05:00")
        self.assertEqual((state, allowed), ("MISSED", False))

        allowed, state, _, _ = self._evaluate("CALL", one_minute_entry_confirmed=False, one_minute_bar_time="")
        self.assertEqual((state, allowed), ("SETUP_FORMING", False))

    def test_setup_is_invalidated_when_directional_structure_breaks(self):
        self._evaluate("CALL", one_minute_entry_confirmed=False, one_minute_bar_time="")
        allowed, state, _, _ = self._evaluate(
            "CALL",
            price=99.80,
            ema9=99.90,
            ema20=100.00,
            bullish_candle=False,
            one_minute_entry_confirmed=False,
            one_minute_bar_time="",
        )
        self.assertEqual((state, allowed), ("INVALIDATED", False))


if __name__ == "__main__":
    unittest.main()