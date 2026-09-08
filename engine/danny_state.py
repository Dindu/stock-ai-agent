"""Danny-style sequential setup lifecycle built on existing bot evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import RLock
from typing import Any


WATCHING = "WATCHING"
HOLDING = "HOLDING"
CONFIRMING = "CONFIRMING"
READY = "READY"
IDLE = "IDLE"


@dataclass
class SetupState:
    stage: str = IDLE
    playbook: str = "REVERSAL"
    level: float | None = None
    started_at: datetime | None = None
    volume_confirmed: bool = False
    ready_at: datetime | None = None


_STATES: dict[tuple[str, str], SetupState] = {}
_LOCK = RLock()


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _level(value: Any) -> float | None:
    if isinstance(value, dict):
        value = value.get("level")
    value = _number(value, 0.0)
    return value if value > 0 else None


def _support_level(data: dict[str, Any]) -> float | None:
    support = _level(data.get("support_level"))
    price = _number(data.get("price"))
    if support is not None and support <= price:
        return support
    for candidate in (data.get("vwap"), data.get("ema20"), data.get("recent_low")):
        candidate = _level(candidate)
        if candidate is not None and candidate <= price:
            return candidate
    return None


def _resistance_level(data: dict[str, Any]) -> float | None:
    resistance = _level(data.get("resistance_level"))
    price = _number(data.get("price"))
    if resistance is not None and resistance >= price:
        return resistance
    for candidate in (data.get("vwap"), data.get("ema20"), data.get("recent_high")):
        candidate = _level(candidate)
        if candidate is not None and candidate >= price:
            return candidate
    return None


def _reset(state: SetupState) -> None:
    state.stage = IDLE
    state.playbook = "REVERSAL"
    state.level = None
    state.started_at = None
    state.volume_confirmed = False
    state.ready_at = None


def _transition(state: SetupState, stage: str, now: datetime) -> None:
    state.stage = stage
    if stage == WATCHING:
        state.started_at = now
        state.ready_at = None
        state.volume_confirmed = False
    elif stage == READY:
        state.ready_at = now


def update(symbol: str, data: dict[str, Any], now: datetime | None = None, *,
           watch_distance_atr: float = 1.25, invalidation_atr: float = 0.35,
           max_age_minutes: int = 30, ready_window_minutes: int = 8,
           min_score: float = 55.0, min_dominance: float = 8.0,
           min_structure: float = 8.0, min_momentum: float = 5.0,
           min_volume_ratio: float = 1.05) -> dict[str, Any]:
    """Advance both CALL and PUT setup states from one closed-bar snapshot.

    The function is deliberately evidence-based and side-effect limited: it never
    places orders. It returns the current state plus a READY decision for callers.
    """
    symbol = str(symbol or "").upper()
    data = data or {}
    now = now or datetime.now().astimezone()
    price = _number(data.get("price"))
    atr = max(_number(data.get("atr14")), 0.0)
    if not symbol or price <= 0 or atr <= 0:
        return {"call_stage": IDLE, "put_stage": IDLE, "ready_side": None, "reason": "missing price/ATR"}

    with _LOCK:
        results: dict[str, Any] = {}
        for side, level, score_key, opposite_key, structure_key, momentum_key, volume_key in (
            ("CALL", _support_level(data), "bull_score", "bear_score", "gainz_buy_breakout", "gainz_buy_momentum_ok", "gainz_volume_ratio"),
            ("PUT", _resistance_level(data), "bear_score", "bull_score", "gainz_sell_breakdown", "gainz_sell_momentum_ok", "gainz_volume_ratio"),
        ):
            state = _STATES.setdefault((symbol, side), SetupState())
            score = _number(data.get(score_key))
            dominance = score - _number(data.get(opposite_key))
            near = level is not None and abs(price - level) <= atr * watch_distance_atr
            invalidated = state.level is not None and (
                price < state.level - atr * invalidation_atr if side == "CALL"
                else price > state.level + atr * invalidation_atr
            )
            age_expired = state.started_at is not None and now - state.started_at > timedelta(minutes=max_age_minutes)
            ready_expired = state.ready_at is not None and now - state.ready_at > timedelta(minutes=ready_window_minutes)
            authorized = score >= min_score and dominance >= min_dominance
            aligned = (
                price >= _number(data.get("ema20")) and price >= _number(data.get("vwap"))
                if side == "CALL" else
                price <= _number(data.get("ema20")) and price <= _number(data.get("vwap"))
            )
            hold = state.level is not None and (
                price >= state.level if side == "CALL" else price <= state.level
            )
            structure = bool(data.get(structure_key)) or bool(data.get("gainz_breakout" if side == "CALL" else "gainz_breakdown")) or bool(data.get("fresh_breakout" if side == "CALL" else "fresh_breakdown"))
            momentum = bool(data.get(momentum_key)) or (
                _number(data.get("gainz_price_change_pct")) > 0 if side == "CALL"
                else _number(data.get("gainz_price_change_pct")) < 0
            )
            volume = _number(data.get(volume_key), _number(data.get("vol_ratio"), 0.0)) >= min_volume_ratio
            trend_strength = _number(
                data.get("market_regime_score_bull" if side == "CALL" else "market_regime_score_bear"),
                abs(_number(data.get("gainz_trend_strength"))),
            )
            trend_established = aligned and trend_strength >= 16.0
            continuation_level = level
            breakout = bool(data.get("fresh_breakout" if side == "CALL" else "fresh_breakdown"))
            if not breakout:
                breakout = bool(data.get(structure_key)) or bool(
                    data.get("gainz_breakout" if side == "CALL" else "gainz_breakdown")
                )
            location_ok = not bool(data.get("bull_extended" if side == "CALL" else "bear_extended", False))

            if invalidated or age_expired or (state.stage == READY and ready_expired):
                _reset(state)

            if state.stage == IDLE and authorized and trend_established and continuation_level is not None and location_ok and near:
                state.level = continuation_level
                state.playbook = "TREND_PULLBACK"
                _transition(state, WATCHING, now)
            elif state.stage == IDLE and authorized and breakout and location_ok:
                state.level = level if level is not None else price
                state.playbook = "BREAKOUT_RETEST"
                _transition(state, WATCHING, now)
            elif state.stage == IDLE and near and authorized:
                state.level = level
                state.playbook = "REVERSAL"
                _transition(state, WATCHING, now)
            elif state.stage == WATCHING and hold:
                _transition(state, HOLDING, now)
            elif state.stage == HOLDING and (structure or (aligned and momentum)):
                _transition(state, CONFIRMING, now)
            elif state.stage == CONFIRMING:
                state.volume_confirmed = state.volume_confirmed or volume
                if state.volume_confirmed and authorized and (aligned or structure) and momentum:
                    _transition(state, READY, now)

            results[f"{side.lower()}_stage"] = state.stage
            results[f"{side.lower()}_playbook"] = state.playbook
            results[f"{side.lower()}_level"] = state.level
            results[f"{side.lower()}_volume_confirmed"] = state.volume_confirmed

        ready_sides = [side for side in ("CALL", "PUT") if results.get(f"{side.lower()}_stage") == READY]
        if len(ready_sides) == 1:
            results["ready_side"] = ready_sides[0]
            results["reason"] = f"{ready_sides[0]} sequential setup READY"
        else:
            results["ready_side"] = None
            results["reason"] = "no unique READY setup" if not ready_sides else "conflicting READY setups"
        return results


def reset(symbol: str | None = None) -> None:
    """Reset lifecycle state, useful for tests or a new trading session."""
    with _LOCK:
        if symbol is None:
            _STATES.clear()
            return
        symbol = str(symbol).upper()
        for key in [key for key in _STATES if key[0] == symbol]:
            _STATES.pop(key, None)
