"""Lightweight honesty overlay for trading decisions.

This is intentionally small and conservative. It is designed to be a risk and
validation layer, not a hard gate. The goal is to keep the current bot's live
behavior stable while adding the same principles used in more disciplined ML
trading stacks: measure confidence honestly, size down under bad conditions, and
log risk multipliers without overfitting.
"""

from __future__ import annotations

from math import prod
from typing import Iterable, Mapping, Any


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def probability_from_score(score: float) -> float:
    """Map a 0-100 score to a defensible up-probability estimate."""
    score_value = _clamp(float(score), 0.0, 100.0)
    # A neutral score is still only roughly 50/50; the slope is intentionally
    # conservative so the overlay does not overstate edge.
    return 0.50 + ((score_value - 50.0) / 100.0) * 0.35


def kelly_fraction(prob_up: float) -> float:
    """A conservative Kelly-style fraction. Zero at a coin flip."""
    p = _clamp(prob_up)
    return max(0.0, min(0.25, 2.0 * p - 1.0))


def omega_multiplier(circuit_breakers: Iterable[float]) -> float:
    """Floor the product of risk multipliers so bad conditions cannot be ignored."""
    values = [max(0.25, float(v)) for v in circuit_breakers]
    if not values:
        return 1.0
    return max(0.25, prod(values))


def position_size(prob_up: float, circuit_breakers: Iterable[float] = (1.0, 1.0, 1.0), max_units: int = 5) -> int:
    """Return a conservative sizing cap in option contracts.

    This mirrors the honest-agent pattern: edge is only meaningful if the market
    is not under stress and the confidence is real. The side effect is a soft cap
    that reduces sizing during bad regimes rather than blocking the trade outright.
    """
    p = _clamp(prob_up)
    if p <= 0.50:
        return 0
    edge = kelly_fraction(p)
    omega = omega_multiplier(circuit_breakers)
    raw_units = p * edge * omega * 10.0
    size = int(round(raw_units))
    return max(0, min(int(max_units), size))


def build_honesty_overlay(
    *,
    symbol: str,
    side: str,
    score: float,
    data: Mapping[str, Any] | None = None,
    recent_drawdown_pct: float = 0.0,
    consecutive_losses: int = 0,
    volatility_ratio: float = 1.0,
) -> dict:
    """Construct a soft risk overlay for a trade candidate.

    It deliberately never vetoes a trade on its own. It only yields a reduced
    size cap and a clear explanation, so the live strategy remains stable while we
    gather evidence on whether these conditions are materially useful.
    """
    data = data or {}
    p = probability_from_score(score)
    circuit = []

    if volatility_ratio > 1.35:
        circuit.append(0.5)
    else:
        circuit.append(1.0)

    if recent_drawdown_pct > 0.08:
        circuit.append(0.5)
    else:
        circuit.append(1.0)

    if consecutive_losses >= 2:
        circuit.append(0.5)
    else:
        circuit.append(1.0)

    risk_multiplier = omega_multiplier(circuit)
    cap = position_size(p, circuit_breakers=circuit, max_units=4)
    reason_bits = []
    if volatility_ratio > 1.35:
        reason_bits.append("high_vol")
    if recent_drawdown_pct > 0.08:
        reason_bits.append("drawdown")
    if consecutive_losses >= 2:
        reason_bits.append("loss_streak")
    if not reason_bits:
        reason_bits.append("baseline")

    return {
        "symbol": str(symbol or "").upper(),
        "side": str(side or "").upper(),
        "probability": round(p, 3),
        "score": float(score),
        "risk_multiplier": round(risk_multiplier, 3),
        "qty_cap": int(cap),
        "volatility_ratio": float(volatility_ratio),
        "recent_drawdown_pct": float(recent_drawdown_pct),
        "consecutive_losses": int(consecutive_losses),
        "reasons": reason_bits,
        "summary": (
            f"prob={p:.2f} risk_mult={risk_multiplier:.2f} qty_cap={cap} "
            f"reasons={','.join(reason_bits)}"
        ),
    }
