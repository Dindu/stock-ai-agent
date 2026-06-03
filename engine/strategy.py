import json
import re

def pre_score(stock):
    """Quick rule-based gate before calling Groq. Returns 0 for clearly unqualified stocks."""
    change = stock.get("change", 0)
    volume = stock.get("volume", 0)

    # Long-only: never score negative/flat stocks
    if change <= 0:
        return 0

    score = 0

    # Price momentum
    if change >= 5:
        score += 30
    elif change >= 3:
        score += 20
    elif change >= 2:
        score += 12
    elif change >= 1.5:
        score += 6

    # Volume confirmation
    if volume >= 3000000:
        score += 25
    elif volume >= 1000000:
        score += 18
    elif volume >= 500000:
        score += 10
    elif volume >= 200000:
        score += 5

    return score


def score_stock(stock, ai_raw):
    """
    5-category scoring: Catalyst(30) + Market(20) + Fundamentals(20) + Technicals(20) + Sentiment(10) = 100
    Returns: (total_score, reasons, breakdown_dict, catalyst_summary)
    """
    breakdown = {"catalyst": 0, "market": 0, "fundamentals": 0, "technicals": 0, "sentiment": 0}
    reasons = []
    catalyst_summary = ""

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

        # Penalise avoid signals and high risk
        if ai.get("trade_type") == "avoid":
            score = max(score - 15, 0)
        if ai.get("risk_level") == "high":
            score = max(score - 10, 0)

        reasons          = ai.get("reasons", [])
        catalyst_summary = ai.get("catalyst_summary", "")

    except Exception:
        score = 0
        reasons = ["AI parse error"]

    return min(score, 100), reasons, breakdown, catalyst_summary


        reasons = ai.get("reasons", [])

    except:
        score -= 10
        reasons = ["AI error"]

    return min(max(score, 0), 100), reasons
