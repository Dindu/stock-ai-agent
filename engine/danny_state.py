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

REVERSAL = "REVERSAL"
TREND_PULLBACK = "TREND_PULLBACK"
BREAKOUT_RETEST = "BREAKOUT_RETEST"
LEVEL_BOUNCE = "LEVEL_BOUNCE"
EMA_VWAP_RECLAIM = "EMA_VWAP_RECLAIM"
ZONE_RETEST = "ZONE_RETEST"

LB_IDLE = "IDLE"
LB_ARMED = "ARMED"
LB_HELD = "HELD"
LB_TRIGGERED = "TRIGGERED"

PATH_READY = "READY"
PATH_LEVEL_BOUNCE_EARLY = "LEVEL_BOUNCE_EARLY"
PATH_EARLY_CONFIRMED = "EARLY_CONFIRMED"

# A level-bounce or reclaim setup is itself stronger evidence than a fresh
# structure break, so those playbooks clear at a lower supporting score.
_PLAYBOOK_MIN_SCORE = {
    LEVEL_BOUNCE: 50.0,
    EMA_VWAP_RECLAIM: 52.0,
    ZONE_RETEST: 55.0,
}


@dataclass
class SetupState:
    stage: str = IDLE
    playbook: str = REVERSAL
    level: float | None = None
    started_at: datetime | None = None
    volume_confirmed: bool = False
    ready_at: datetime | None = None
    ready_bar: Any = None
    ready_retest_seen: bool = False
    thesis_id: str = ""
    anchor_bar: Any = None
    anchor_price: float | None = None
    executed: bool = False
    candidate_count: int = 0
    bars_since_hold: int = 0


@dataclass
class LevelBounceState:
    """Independent of SetupState so a later reclassification cannot move the anchor."""

    stage: str = LB_IDLE
    anchor: float | None = None
    anchor_bar: Any = None
    bars_since_held: int = 0
    thesis_seq: int = 0
    traded_thesis_seq: int | None = None
    traded_anchor: float | None = None
    consumed: bool = False
    structural_rearm_required: bool = False
    candidate_count: int = 0
    trade_count: int = 0


_STATES: dict[tuple[str, str], SetupState] = {}
_LB_STATES: dict[tuple[str, str], LevelBounceState] = {}
_LAST_BAR_BY_SYMBOL: dict[str, Any] = {}
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


def _touches(value: Any) -> int:
    if isinstance(value, dict):
        try:
            return int(value.get("touches", 0) or 0)
        except (TypeError, ValueError):
            return 0
    return 0


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
    state.playbook = REVERSAL
    state.level = None
    state.started_at = None
    state.volume_confirmed = False
    state.ready_at = None
    state.ready_bar = None
    state.ready_retest_seen = False
    state.thesis_id = ""
    state.anchor_bar = None
    state.anchor_price = None
    state.executed = False
    state.candidate_count = 0
    state.bars_since_hold = 0


def _reset_lb(lb: LevelBounceState) -> None:
    lb.stage = LB_IDLE
    lb.anchor = None
    lb.anchor_bar = None
    lb.bars_since_held = 0
    lb.consumed = False


def _transition(state: SetupState, stage: str, now: datetime) -> None:
    state.stage = stage
    if stage == WATCHING:
        state.started_at = now
        state.ready_at = None
        state.volume_confirmed = False
    elif stage == HOLDING:
        state.bars_since_hold = 0
    elif stage == READY:
        state.ready_at = now


def _min_score_for(playbook: str, default_min: float) -> float:
    return _PLAYBOOK_MIN_SCORE.get(playbook, default_min)


def _side_result(symbol: str, side: str) -> dict[str, Any]:
    state = _STATES.get((symbol, side), SetupState())
    lb = _LB_STATES.get((symbol, side), LevelBounceState())
    key = side.lower()
    return {
        f"{key}_stage": state.stage,
        f"{key}_playbook": state.playbook,
        f"{key}_level": state.level,
        f"{key}_volume_confirmed": state.volume_confirmed,
        f"{key}_ready_retest_seen": state.ready_retest_seen,
        f"{key}_thesis_id": state.thesis_id,
        f"{key}_anchor_bar": state.anchor_bar,
        f"{key}_anchor_price": state.anchor_price,
        f"{key}_executed": state.executed,
        f"{key}_candidate_count": state.candidate_count,
        f"{key}_lb_stage": lb.stage,
        f"{key}_lb_anchor": lb.anchor,
        f"{key}_lb_thesis_seq": lb.thesis_seq,
        f"{key}_lb_candidate_count": lb.candidate_count,
        f"{key}_lb_trade_count": lb.trade_count,
        f"{key}_lb_rearm_locked": lb.structural_rearm_required,
    }


def _resolve_entry(results: dict[str, Any], entries: dict[str, tuple[str, str]]) -> None:
    """Pick a single executable side; conflicting sides stand down."""
    ready_sides = [
        side for side in ("CALL", "PUT")
        if results.get(f"{side.lower()}_stage") == READY
        and not results.get(f"{side.lower()}_executed", False)
        and (
            results.get(f"{side.lower()}_playbook") != REVERSAL
            or results.get(f"{side.lower()}_ready_retest_seen")
        )
    ]
    results["ready_side"] = ready_sides[0] if len(ready_sides) == 1 else None

    candidates = list(entries.keys())
    if len(candidates) == 1:
        side = candidates[0]
        path, playbook = entries[side]
        results["entry_side"] = side
        results["entry_path"] = path
        results["entry_playbook"] = playbook
        results["reason"] = f"{side} {playbook} executable via {path}"
    else:
        results["entry_side"] = None
        results["entry_path"] = ""
        results["entry_playbook"] = ""
        results["reason"] = "no executable setup" if not candidates else "conflicting setups"


def _state_result(symbol: str) -> dict[str, Any]:
    results: dict[str, Any] = {}
    entries: dict[str, tuple[str, str]] = {}
    for side in ("CALL", "PUT"):
        results.update(_side_result(symbol, side))
        state = _STATES.get((symbol, side), SetupState())
        if (
            state.stage == READY
            and not state.executed
            and (state.playbook != REVERSAL or state.ready_retest_seen)
        ):
            entries[side] = (PATH_READY, state.playbook)
    _resolve_entry(results, entries)
    return results


def update(symbol: str, data: dict[str, Any], now: datetime | None = None, *,
           watch_distance_atr: float = 1.25, invalidation_atr: float = 0.35,
           max_age_minutes: int = 30, ready_window_minutes: int = 8,
           min_score: float = 55.0, min_dominance: float = 8.0,
           min_structure: float = 8.0, min_momentum: float = 5.0,
           min_volume_ratio: float = 1.05, early_window_bars: int = 3,
           lb_early_min_quality: float = 40.0,
           lb_anchor_separation_atr: float = 0.75) -> dict[str, Any]:
    """Advance both CALL and PUT setup states from one closed-bar snapshot.

    The function is deliberately evidence-based and side-effect limited: it never
    places orders. It returns the current state plus an executable entry decision.
    """
    symbol = str(symbol or "").upper()
    data = data or {}
    now = now or datetime.now().astimezone()
    price = _number(data.get("price"))
    atr = max(_number(data.get("atr14")), 0.0)
    if not symbol or price <= 0 or atr <= 0:
        return {
            "call_stage": IDLE, "put_stage": IDLE, "ready_side": None,
            "entry_side": None, "entry_path": "", "entry_playbook": "",
            "reason": "missing price/ATR",
        }

    with _LOCK:
        bar_key = data.get("bar_time")
        if bar_key is not None and _LAST_BAR_BY_SYMBOL.get(symbol) == bar_key:
            return _state_result(symbol)
        if bar_key is not None:
            _LAST_BAR_BY_SYMBOL[symbol] = bar_key

        open_ = _number(data.get("open"), price)
        high = _number(data.get("high"), price)
        low = _number(data.get("low"), price)
        prev_high = _number(data.get("prior_bar_high"), high)
        prev_low = _number(data.get("prior_bar_low"), low)
        ema20 = _number(data.get("ema20"))
        vwap = _number(data.get("vwap"))
        vol_ratio = _number(data.get("vol_ratio"), 0.0)
        candle_range = max(high - low, 1e-9)
        body = abs(price - open_)
        body_pct = body / candle_range

        mtf_5m = _number(data.get("mtf_5m"), 0.0)
        mtf_15m = _number(data.get("mtf_15m"), 0.0)
        mtf_available = bool(data.get("mtf_available", False))

        results: dict[str, Any] = {}
        entries: dict[str, tuple[str, str]] = {}

        for side in ("CALL", "PUT"):
            is_call = side == "CALL"
            level = _support_level(data) if is_call else _resistance_level(data)
            level_data = data.get("support_level" if is_call else "resistance_level")
            score = _number(data.get("bull_score" if is_call else "bear_score"))
            opposite = _number(data.get("bear_score" if is_call else "bull_score"))
            dominance = score - opposite
            structure = bool(data.get("fresh_breakout" if is_call else "fresh_breakdown"))
            momentum_value = _number(data.get("momentum_pct"))
            momentum = momentum_value > 0 if is_call else momentum_value < 0
            volume_ok = vol_ratio >= min_volume_ratio
            location_ok = not bool(data.get("bull_extended" if is_call else "bear_extended", False))
            trend_strength = _number(
                data.get("market_regime_score_bull" if is_call else "market_regime_score_bear"), 0.0
            )
            directional_candle = price > open_ if is_call else price < open_

            state = _STATES.setdefault((symbol, side), SetupState())
            lb = _LB_STATES.setdefault((symbol, side), LevelBounceState())

            near = level is not None and abs(price - level) <= atr * watch_distance_atr
            aligned = (
                price >= ema20 and price >= vwap if is_call
                else price <= ema20 and price <= vwap
            )
            mtf_aligned = not mtf_available or (
                mtf_5m >= 0 and mtf_15m >= 0 and (mtf_5m > 0 or mtf_15m > 0) if is_call
                else mtf_5m <= 0 and mtf_15m <= 0 and (mtf_5m < 0 or mtf_15m < 0)
            )
            # Early paths only require that the opposite regime is not confirmed.
            mtf_not_opposed = not mtf_available or (
                mtf_5m >= 0 and mtf_15m >= 0 if is_call else mtf_5m <= 0 and mtf_15m <= 0
            )
            authorized = score >= min_score and dominance >= min_dominance
            trend_established = aligned and trend_strength >= 16.0

            reclaim_level = max(ema20, vwap) if is_call else min(ema20, vwap)
            reclaim_watch = reclaim_level > 0 and (
                price >= reclaim_level and low <= reclaim_level + atr * 0.75 if is_call
                else price <= reclaim_level and high >= reclaim_level - atr * 0.75
            )
            zone_watch = near and _touches(level_data) >= 2
            bounce_watch = near and level is not None and location_ok

            # ---------- main lifecycle ----------
            invalidated = state.level is not None and (
                price < state.level - atr * invalidation_atr if is_call
                else price > state.level + atr * invalidation_atr
            )
            age_expired = state.started_at is not None and now - state.started_at > timedelta(minutes=max_age_minutes)
            ready_expired = state.ready_at is not None and now - state.ready_at > timedelta(minutes=ready_window_minutes)
            if invalidated or age_expired or (state.stage == READY and ready_expired):
                _reset(state)

            if state.stage == READY and state.playbook == REVERSAL and state.ready_bar != bar_key:
                anchor = state.level if state.level is not None else price
                retest = (
                    low <= anchor + atr * 0.55 and price >= anchor - atr * 0.05 and price >= open_
                    if is_call else
                    high >= anchor - atr * 0.55 and price <= anchor + atr * 0.05 and price <= open_
                )
                state.ready_retest_seen = state.ready_retest_seen or retest

            # Captured before any transition so one bar advances at most one stage.
            prior_stage = state.stage

            watch_eligible = structure or bounce_watch or zone_watch or reclaim_watch or (
                trend_established and near
            )
            if prior_stage == IDLE and state.stage == IDLE and not state.executed and authorized and location_ok and watch_eligible:
                if structure:
                    state.playbook = BREAKOUT_RETEST
                elif bounce_watch:
                    state.playbook = LEVEL_BOUNCE
                elif zone_watch:
                    state.playbook = ZONE_RETEST
                elif reclaim_watch:
                    state.playbook = EMA_VWAP_RECLAIM
                elif trend_established and near:
                    state.playbook = TREND_PULLBACK
                else:
                    state.playbook = REVERSAL
                state.level = level if level is not None else price
                _transition(state, WATCHING, now)
                state.anchor_bar = bar_key
                state.anchor_price = state.level
                state.thesis_id = f"{symbol}:{side}:{state.playbook}:{state.level:.4f}"

            playbook_min_score = _min_score_for(state.playbook, min_score)
            playbook_authorized = score >= playbook_min_score and dominance >= min_dominance

            hold = state.level is not None and (
                price >= state.level if is_call else price <= state.level
            )
            if prior_stage == WATCHING and state.stage == WATCHING and hold:
                _transition(state, HOLDING, now)
            elif prior_stage == HOLDING and state.stage == HOLDING and (structure or (aligned and momentum)):
                _transition(state, CONFIRMING, now)
            elif prior_stage == CONFIRMING and state.stage == CONFIRMING:
                state.volume_confirmed = state.volume_confirmed or volume_ok
                if (
                    state.volume_confirmed and playbook_authorized and mtf_aligned
                    and (aligned or structure) and momentum
                ):
                    state.ready_bar = bar_key
                    _transition(state, READY, now)

            if state.stage in {WATCHING, HOLDING, CONFIRMING, READY}:
                state.candidate_count += 1
            if state.stage in {HOLDING, CONFIRMING}:
                state.bars_since_hold += 1

            # ---------- independent level-bounce thesis ----------
            if lb.structural_rearm_required and lb.traded_anchor is not None:
                anchor_invalidated = (
                    price < lb.traded_anchor - atr * invalidation_atr if is_call
                    else price > lb.traded_anchor + atr * invalidation_atr
                )
                if anchor_invalidated:
                    lb.structural_rearm_required = False

            lb_failed = lb.stage != LB_IDLE and lb.anchor is not None and (
                price < lb.anchor - atr * invalidation_atr if is_call
                else price > lb.anchor + atr * invalidation_atr
            )
            lb_expired = lb.stage == LB_HELD and lb.bars_since_held > early_window_bars
            if lb_failed or lb_expired:
                _reset_lb(lb)

            anchor_separated = lb.traded_anchor is None or (
                level is not None and abs(level - lb.traded_anchor) >= atr * lb_anchor_separation_atr
            )
            if (
                lb.stage == LB_IDLE and not lb.structural_rearm_required
                and bounce_watch and anchor_separated
            ):
                lb.thesis_seq += 1
                lb.stage = LB_ARMED
                lb.anchor = level
                lb.anchor_bar = bar_key
                lb.consumed = False

            if lb.stage == LB_ARMED and lb.anchor is not None:
                held = (
                    low <= lb.anchor + atr * watch_distance_atr and price >= lb.anchor - atr * invalidation_atr
                    if is_call else
                    high >= lb.anchor - atr * watch_distance_atr and price <= lb.anchor + atr * invalidation_atr
                )
                if held:
                    lb.stage = LB_HELD
                    lb.bars_since_held = 0
            elif lb.stage == LB_HELD:
                lb.bars_since_held += 1

            # ---------- executable entries ----------
            ready_entry = (
                state.stage == READY and not state.executed
                and (state.playbook != REVERSAL or state.ready_retest_seen)
            )

            lb_locked = lb.traded_thesis_seq is not None and lb.thesis_seq <= lb.traded_thesis_seq
            lb_early = False
            if (
                lb.stage == LB_HELD and lb.bars_since_held >= 1 and lb.anchor is not None
                and not lb.consumed and not lb_locked and mtf_not_opposed and location_ok
            ):
                lb_body = directional_candle and body_pct >= 0.35 and body >= atr * 0.20
                lb_reaction = lb_body and (
                    price > prev_high or price >= lb.anchor + atr * 0.15 if is_call
                    else price < prev_low or price <= lb.anchor - atr * 0.15
                )
                lb_hold_ok = (
                    price >= lb.anchor - atr * invalidation_atr if is_call
                    else price <= lb.anchor + atr * invalidation_atr
                )
                lb_distance_ok = (
                    price - lb.anchor <= atr * 2.0 if is_call else lb.anchor - price <= atr * 2.0
                )
                lb_reclaim = (
                    price > ema20 or price > vwap if is_call else price < ema20 or price < vwap
                )
                quality = (
                    (25.0 if lb_hold_ok else 0.0)
                    + (20.0 if lb_reaction else 0.0)
                    + (15.0 if vol_ratio >= 1.0 else 0.0)
                    + (15.0 if lb_reclaim else 0.0)
                    + (15.0 if structure else 0.0)
                    + (10.0 if mtf_aligned else 0.0)
                )
                if (
                    lb_reaction and lb_hold_ok and lb_distance_ok
                    and vol_ratio >= 1.0 and quality >= lb_early_min_quality
                ):
                    lb.candidate_count += 1
                    lb_early = True

            early_confirmed = False
            if (
                state.stage in {HOLDING, CONFIRMING} and not state.executed
                and state.playbook != BREAKOUT_RETEST
                and 1 <= state.bars_since_hold <= early_window_bars
                and location_ok and mtf_not_opposed and state.level is not None
            ):
                displacement = (
                    directional_candle and body_pct >= 0.45 and body >= atr * 0.25
                    and (price > prev_high if is_call else price < prev_low)
                )
                distance_ok = (
                    price - state.level <= atr * 2.5 if is_call
                    else state.level - price <= atr * 2.5
                )
                early_confirmed = (
                    displacement and distance_ok and (structure or aligned)
                    and vol_ratio >= min_volume_ratio and score >= 45.0
                )

            if ready_entry:
                entries[side] = (PATH_READY, state.playbook)
            elif lb_early:
                entries[side] = (PATH_LEVEL_BOUNCE_EARLY, LEVEL_BOUNCE)
            elif early_confirmed:
                entries[side] = (PATH_EARLY_CONFIRMED, state.playbook)

            results.update(_side_result(symbol, side))

        _resolve_entry(results, entries)
        return results


def mark_executed(symbol: str, side: str, thesis_id: str | None = None) -> bool:
    """Mark one Danny thesis as executed so repeated scans cannot re-enter it."""
    key = (str(symbol or "").upper(), str(side or "").upper())
    with _LOCK:
        state = _STATES.get(key)
        lb = _LB_STATES.get(key)
        if state is None:
            return False
        if thesis_id and state.thesis_id and state.thesis_id != thesis_id:
            return False
        state.executed = True
        if lb is not None and lb.stage in {LB_ARMED, LB_HELD}:
            lb.stage = LB_TRIGGERED
            lb.consumed = True
            lb.trade_count += 1
            lb.traded_thesis_seq = lb.thesis_seq
            lb.traded_anchor = lb.anchor
            lb.structural_rearm_required = True
        return True


def reset(symbol: str | None = None) -> None:
    """Reset lifecycle state, useful for tests or a new trading session."""
    with _LOCK:
        if symbol is None:
            _STATES.clear()
            _LB_STATES.clear()
            _LAST_BAR_BY_SYMBOL.clear()
            return
        symbol = str(symbol).upper()
        for store in (_STATES, _LB_STATES):
            for key in [key for key in store if key[0] == symbol]:
                store.pop(key, None)
        _LAST_BAR_BY_SYMBOL.pop(symbol, None)
