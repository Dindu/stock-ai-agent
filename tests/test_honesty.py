from engine.honesty import build_honesty_overlay, position_size, probability_from_score


def test_probability_map_is_conservative():
    assert probability_from_score(50.0) == 0.5
    assert probability_from_score(80.0) > 0.5
    assert probability_from_score(100.0) < 0.9


def test_position_size_zeroes_out_coin_flip_edge():
    assert position_size(0.50) == 0
    assert position_size(0.49) == 0


def test_honesty_overlay_soft_caps_bad_regimes():
    overlay = build_honesty_overlay(
        symbol="SPY",
        side="CALL",
        score=82.0,
        recent_drawdown_pct=0.12,
        consecutive_losses=3,
        volatility_ratio=1.6,
    )

    assert overlay["qty_cap"] <= 4
    assert overlay["risk_multiplier"] < 1.0
    assert "drawdown" in overlay["reasons"]
    assert "loss_streak" in overlay["reasons"]
