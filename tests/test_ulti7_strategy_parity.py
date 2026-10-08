import unittest

import pandas as pd

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


if __name__ == "__main__":
    unittest.main()
