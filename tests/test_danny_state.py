from datetime import datetime, timedelta, timezone

from engine import danny_state


def snapshot(**overrides):
    data = {
        "price": 100.0,
        "atr14": 1.0,
        "vwap": 99.0,
        "ema20": 99.0,
        "recent_low": 99.0,
        "recent_high": 101.0,
        "support_level": {"level": 99.0},
        "resistance_level": {"level": 101.0},
        "bull_score": 70,
        "bear_score": 20,
        "gainz_buy_breakout": False,
        "gainz_sell_breakdown": False,
        "gainz_buy_momentum_ok": True,
        "gainz_sell_momentum_ok": False,
        "gainz_volume_ratio": 1.0,
    }
    data.update(overrides)
    return data


def test_call_progresses_across_bars_to_ready():
    danny_state.reset("TEST")
    start = datetime(2026, 9, 7, 14, 0, tzinfo=timezone.utc)

    watching = danny_state.update("TEST", snapshot(price=99.4), start)
    assert watching["call_stage"] == danny_state.WATCHING

    holding = danny_state.update("TEST", snapshot(price=99.2), start + timedelta(minutes=1))
    assert holding["call_stage"] == danny_state.HOLDING

    confirming = danny_state.update(
        "TEST",
        snapshot(price=100.0, gainz_buy_breakout=True),
        start + timedelta(minutes=2),
    )
    assert confirming["call_stage"] == danny_state.CONFIRMING

    ready = danny_state.update(
        "TEST",
        snapshot(price=100.0, gainz_buy_breakout=True, gainz_volume_ratio=1.2),
        start + timedelta(minutes=3),
    )
    assert ready["call_stage"] == danny_state.READY
    assert ready["ready_side"] == "CALL"


def test_put_uses_resistance_side():
    danny_state.reset("TEST_PUT")
    start = datetime(2026, 9, 7, 14, 0, tzinfo=timezone.utc)

    state = danny_state.update(
        "TEST_PUT",
        snapshot(
            price=100.6,
            vwap=101.0,
            ema20=101.0,
            bull_score=20,
            bear_score=70,
            gainz_buy_momentum_ok=False,
            gainz_sell_momentum_ok=True,
        ),
        start,
    )
    assert state["put_stage"] == danny_state.WATCHING
    assert state["put_level"] == 101.0
