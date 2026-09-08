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
        "fresh_breakout": False,
        "fresh_breakdown": False,
        "momentum_pct": 0.5,
        "vol_ratio": 1.0,
        "market_regime_score_bull": 20.0,
        "market_regime_score_bear": 0.0,
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
        snapshot(price=100.0, fresh_breakout=True),
        start + timedelta(minutes=2),
    )
    assert confirming["call_stage"] == danny_state.CONFIRMING

    ready = danny_state.update(
        "TEST",
        snapshot(price=100.0, fresh_breakout=True, vol_ratio=1.2),
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
            momentum_pct=-0.5,
        ),
        start,
    )
    assert state["put_stage"] == danny_state.WATCHING
    assert state["put_level"] == 101.0


def test_call_breakout_retest_progresses_to_ready():
    danny_state.reset("TEST_CONTINUATION")
    start = datetime(2026, 9, 7, 14, 0, tzinfo=timezone.utc)

    watching = danny_state.update(
        "TEST_CONTINUATION",
        snapshot(
            price=102.0,
            vwap=100.0,
            ema20=100.5,
            recent_low=100.0,
            recent_high=101.0,
            support_level={"level": 100.0},
            fresh_breakout=True,
            vol_ratio=1.0,
        ),
        start,
    )
    assert watching["call_stage"] == danny_state.WATCHING
    assert watching["call_playbook"] == "BREAKOUT_RETEST"

    holding = danny_state.update(
        "TEST_CONTINUATION",
        snapshot(
            price=101.0,
            vwap=100.0,
            ema20=100.5,
            recent_low=100.0,
            recent_high=102.0,
            support_level={"level": 100.0},
            fresh_breakout=True,
            vol_ratio=1.0,
        ),
        start + timedelta(minutes=1),
    )
    assert holding["call_stage"] == danny_state.HOLDING

    confirming = danny_state.update(
        "TEST_CONTINUATION",
        snapshot(
            price=102.0,
            vwap=100.0,
            ema20=100.5,
            recent_low=100.0,
            recent_high=102.0,
            support_level={"level": 100.0},
            gainz_buy_breakout=True,
            gainz_volume_ratio=1.0,
        ),
        start + timedelta(minutes=2),
    )
    assert confirming["call_stage"] == danny_state.CONFIRMING

    ready = danny_state.update(
        "TEST_CONTINUATION",
        snapshot(
            price=102.2,
            vwap=100.0,
            ema20=100.5,
            recent_low=100.0,
            recent_high=102.0,
            support_level={"level": 100.0},
            fresh_breakout=True,
            vol_ratio=1.2,
        ),
        start + timedelta(minutes=3),
    )
    assert ready["call_stage"] == danny_state.READY
    assert ready["ready_side"] == "CALL"
