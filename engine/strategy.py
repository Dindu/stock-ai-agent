import json
import re

from engine.insiders import get_insider_signal
from engine.accumulation import check_accumulation


# ─── Scenario Detection ────────────────────────────────────────────────────────

def detect_scenario(stock):
    """
    Identify which market scenario this stock fits.
    Returns (scenario_name, description).

    Scenarios (in priority order):
      gap_up       — Overnight gap >+2% (catalyst after hours / pre-market)
      gap_down     — Overnight gap <-2% on volume (panic sell → bounce candidate)
      volume_surge — Relative volume 3x+ (institutional activity)
      momentum     — Strong intraday +3%+ with above-average volume
      oversold     — Down 3%+ with high volume (capitulation / reversal)
      breakout     — Up 1.5-3% with rising volume (early move)
      none         — Nothing notable
    """
    change     = stock.get("change", 0)
    volume     = stock.get("volume", 0)
    gap_pct    = stock.get("gap_pct", 0)
    rel_volume = stock.get("rel_volume", 1.0)

    if gap_pct >= 2.0 and volume >= 200000:
        return "gap_up", f"Gapped up {gap_pct:+.1f}% overnight — likely news catalyst after hours"

    if gap_pct <= -2.0 and volume >= 500000:
        return "gap_down", f"Gapped down {gap_pct:+.1f}% overnight on {volume:,} vol — panic sell, watch for bounce"

    if rel_volume >= 3.0 and volume >= 500000:
        return "volume_surge", f"Volume {rel_volume:.1f}x normal ({volume:,}) — institutional activity, something brewing"

    if change >= 3.0 and rel_volume >= 1.5 and volume >= 200000:
        return "momentum", f"Strong move {change:+.1f}% with {rel_volume:.1f}x volume — catalyst-driven momentum"

    if change <= -3.0 and volume >= 500000:
        return "oversold", f"Down {change:.1f}% on {volume:,} vol — potential capitulation and reversal setup"

    if 1.5 <= change < 3.0 and rel_volume >= 1.2 and volume >= 150000:
        return "breakout", f"Early breakout {change:+.1f}% with rising volume — watch for continuation"

    return "none", ""


# ─── Pre-Score ─────────────────────────────────────────────────────────────────

def pre_score(stock):
    """
    Scenario-aware gate before calling Groq.
    Returns 0 for stocks with no identifiable setup — saves Groq quota.
    """
    scenario, _ = detect_scenario(stock)
    if scenario == "none":
        return 0

    score      = 0
    volume     = stock.get("volume", 0)
    rel_volume = stock.get("rel_volume", 1.0)
    change     = stock.get("change", 0)

    # Volume base (activity = opportunity)
    if volume >= 5000000:   score += 30
    elif volume >= 2000000: score += 22
    elif volume >= 1000000: score += 15
    elif volume >= 500000:  score += 10
    elif volume >= 200000:  score += 5

    # Relative volume bonus
    if rel_volume >= 5.0:   score += 15
    elif rel_volume >= 3.0: score += 10
    elif rel_volume >= 1.5: score += 5

    # Scenario-specific bonuses
    bonuses = {
        "gap_up": 15, "gap_down": 12, "volume_surge": 12,
        "momentum": 10 + min(int(change * 2), 10),
        "oversold": 10, "breakout": 8,
    }
    score += bonuses.get(scenario, 0)

    return score


# ─── 6-Factor Opportunity Score ────────────────────────────────────────────────

def score_stock(stock, ai_raw):
    """
    6-factor scoring — AI handles text analysis (55 pts), rules handle data patterns (45 pts).

    AI scores  (from Groq):   Catalyst(30) + Fundamentals(15) + Market(10) = 55
    Rule scores (from data):  Insider(20) + Accumulation(15) + Technicals(10) = 45
    Total: 100

    Returns: (total_score, reasons, breakdown, catalyst_summary, hold_period, trade_type, catalyst_type, flags)
    """
    symbol = stock.get("symbol", "?")

    breakdown = {
        "catalyst":     0,   # AI: news/event driving a move (0-30)
        "fundamentals": 0,   # AI: revenue growth, analyst targets, upside (0-15)
        "market":       0,   # AI: macro tailwinds for this specific sector (0-10)
        "insider":      0,   # Rules: SEC EDGAR Form 4 filings (0-20)
        "accumulation": 0,   # Rules: Alpaca bars — tight price + rising volume (0-15)
        "technicals":    0,   # Rules: scenario quality as entry setup (0-10)
    }
    flags            = []
    reasons          = []
    catalyst_summary = ""
    hold_period      = "1-2 weeks"
    trade_type       = "avoid"
    catalyst_type    = "none"
    risk_level       = "medium"

    # ── AI factors ──────────────────────────────────────────────────────────────
    try:
        if not ai_raw:
            raise ValueError("No AI response")

        match = re.search(r'\{.*\}', ai_raw, re.DOTALL)
        ai = json.loads(match.group() if match else ai_raw)

        breakdown["catalyst"]     = min(max(int(ai.get("catalyst", 0)), 0), 30)
        breakdown["fundamentals"] = min(max(int(ai.get("fundamentals", 0)), 0), 15)
        breakdown["market"]       = min(max(int(ai.get("market", 0)), 0), 10)

        trade_type       = ai.get("trade_type", "avoid")
        risk_level       = ai.get("risk_level", "medium")
        catalyst_summary = ai.get("catalyst_summary", "")
        hold_period      = ai.get("hold_period", "1-2 weeks")
        catalyst_type    = ai.get("catalyst_type", "none")
        reasons          = ai.get("reasons", [])

    except Exception:
        reasons = ["AI parse error"]

    # ── Rule-based factors ───────────────────────────────────────────────────────

    # Insider buying (SEC EDGAR)
    insider_score, insider_desc = get_insider_signal(symbol)
    breakdown["insider"] = insider_score
    if insider_desc:
        flags.append(f"👔 {insider_desc}")

    # Accumulation pattern (Alpaca bars)
    acc_score, acc_desc = check_accumulation(symbol)
    breakdown["accumulation"] = acc_score
    if acc_desc:
        flags.append(f"📊 {acc_desc}")

    # Technical setup quality based on scenario
    scenario, _ = detect_scenario(stock)
    tech_scores = {
        "breakout":     10,
        "gap_up":       8,
        "momentum":     7,
        "volume_surge": 6,
        "oversold":     5,
        "gap_down":     4,
    }
    breakdown["technicals"] = tech_scores.get(scenario, 2)

    # ── Final score ──────────────────────────────────────────────────────────────
    score = sum(breakdown.values())

    # Penalties
    if trade_type == "avoid":
        score = max(score - 15, 0)
    if risk_level == "high":
        score = max(score - 10, 0)

    return min(score, 100), reasons, breakdown, catalyst_summary, hold_period, trade_type, catalyst_type, flags

