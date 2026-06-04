import json
import re


# ─── Scenario Detection ────────────────────────────────────────────────────────

def detect_scenario(stock):
    """
    Identify which market scenario this stock fits.
    Returns (scenario_name, description) — used to gate pre-filter and focus the AI prompt.

    Scenarios (in priority order):
      gap_up         — Overnight gap up >2% (news catalyst after hours / pre-market)
      gap_down       — Overnight gap down >2% on big volume (panic sell → bounce candidate)
      volume_surge   — Relative volume 3x+ (institutional accumulation or distribution)
      momentum       — Strong intraday move +3%+ with above-average volume
      oversold       — Down 3%+ today with high volume (capitulation / reversal setup)
      breakout       — Up 1.5-3% pushing through resistance (early breakout)
      none           — Nothing notable
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
    Returns 0 for stocks with no identifiable setup.
    """
    scenario, _ = detect_scenario(stock)

    if scenario == "none":
        return 0

    score      = 0
    volume     = stock.get("volume", 0)
    rel_volume = stock.get("rel_volume", 1.0)
    change     = stock.get("change", 0)
    gap_pct    = stock.get("gap_pct", 0)

    # Volume base (activity = opportunity)
    if volume >= 5000000:
        score += 30
    elif volume >= 2000000:
        score += 22
    elif volume >= 1000000:
        score += 15
    elif volume >= 500000:
        score += 10
    elif volume >= 200000:
        score += 5

    # Relative volume bonus (unusual vs normal day)
    if rel_volume >= 5.0:
        score += 15
    elif rel_volume >= 3.0:
        score += 10
    elif rel_volume >= 1.5:
        score += 5

    # Scenario-specific bonuses
    if scenario == "gap_up":
        score += 15
    elif scenario == "gap_down":
        score += 12  # bounce candidate
    elif scenario == "volume_surge":
        score += 12
    elif scenario == "momentum":
        score += 10 + min(int(change * 2), 10)
    elif scenario == "oversold":
        score += 10
    elif scenario == "breakout":
        score += 8

    return score


# ─── Full AI Score ─────────────────────────────────────────────────────────────

def score_stock(stock, ai_raw):
    """
    5-category scoring: Catalyst(30) + Market(20) + Fundamentals(20) + Technicals(20) + Sentiment(10) = 100
    Returns: (total_score, reasons, breakdown_dict, catalyst_summary, hold_period, trade_type)
    """
    breakdown = {"catalyst": 0, "market": 0, "fundamentals": 0, "technicals": 0, "sentiment": 0}
    reasons = []
    catalyst_summary = ""
    hold_period = "1-2 weeks"
    trade_type = "avoid"

    try:
        if not ai_raw:
            raise ValueError("No AI response")

        match = re.search(r'\{.*\}', ai_raw, re.DOTALL)
        ai = json.loads(match.group() if match else ai_raw)

        breakdown["catalyst"]     = min(max(int(ai.get("catalyst", 0)), 0), 30)
        breakdown["market"]       = min(max(int(ai.get("market", 0)), 0), 20)
        breakdown["fundamentals"] = min(max(int(ai.get("fundamentals", 0)), 0), 20)
        breakdown["technicals"]   = min(max(int(ai.get("technicals", 0)), 0), 20)
        breakdown["sentiment"]    = min(max(int(ai.get("sentiment", 0)), 0), 10)

        score      = sum(breakdown.values())
        trade_type = ai.get("trade_type", "avoid")
        risk       = ai.get("risk_level", "medium")

        if trade_type == "avoid":
            score = max(score - 15, 0)
        if risk == "high":
            score = max(score - 10, 0)

        reasons          = ai.get("reasons", [])
        catalyst_summary = ai.get("catalyst_summary", "")
        hold_period      = ai.get("hold_period", "1-2 weeks")

    except Exception:
        score = 0
        reasons = ["AI parse error"]

    return min(score, 100), reasons, breakdown, catalyst_summary, hold_period, trade_type



def score_stock(stock, ai_raw):
    """
    5-category scoring: Catalyst(30) + Market(20) + Fundamentals(20) + Technicals(20) + Sentiment(10) = 100
    Returns: (total_score, reasons, breakdown_dict, catalyst_summary, hold_period, trade_type)
    """
    breakdown = {"catalyst": 0, "market": 0, "fundamentals": 0, "technicals": 0, "sentiment": 0}
    reasons = []
    catalyst_summary = ""
    hold_period = "1-2 weeks"
    trade_type = "avoid"

    try:
        if not ai_raw:
            raise ValueError("No AI response")

        match = re.search(r'\{.*\}', ai_raw, re.DOTALL)
        ai = json.loads(match.group() if match else ai_raw)

        breakdown["catalyst"]     = min(max(int(ai.get("catalyst", 0)), 0), 30)
        breakdown["market"]       = min(max(int(ai.get("market", 0)), 0), 20)
        breakdown["fundamentals"] = min(max(int(ai.get("fundamentals", 0)), 0), 20)
        breakdown["technicals"]   = min(max(int(ai.get("technicals", 0)), 0), 20)
        breakdown["sentiment"]    = min(max(int(ai.get("sentiment", 0)), 0), 10)

        score = sum(breakdown.values())

        trade_type = ai.get("trade_type", "avoid")
        risk       = ai.get("risk_level", "medium")

        # Penalise avoid signals and high risk
        if trade_type == "avoid":
            score = max(score - 15, 0)
        if risk == "high":
            score = max(score - 10, 0)

        reasons          = ai.get("reasons", [])
        catalyst_summary = ai.get("catalyst_summary", "")
        hold_period      = ai.get("hold_period", "1-2 weeks")

    except Exception:
        score = 0
        reasons = ["AI parse error"]

    return min(score, 100), reasons, breakdown, catalyst_summary, hold_period, trade_type
