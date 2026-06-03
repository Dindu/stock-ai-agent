import json
import re

def pre_score(stock):
    """
    Volume-first gate: high volume means something is happening — institutional activity,
    news catalyst, or major sentiment shift. Let the AI decide if it's worth buying.
    Also catches oversold bounce candidates (big drop + high volume = capitulation).
    """
    change = stock.get("change", 0)
    volume = stock.get("volume", 0)
    score = 0

    # Volume is the primary signal — activity means opportunity
    if volume >= 5000000:
        score += 30
    elif volume >= 2000000:
        score += 22
    elif volume >= 1000000:
        score += 15
    elif volume >= 500000:
        score += 10

    # Upward momentum adds conviction
    if change >= 5:
        score += 20
    elif change >= 3:
        score += 12
    elif change >= 1.5:
        score += 6

    # Significant drop on high volume = potential capitulation / reversal setup
    if change <= -3 and volume >= 1000000:
        score += 12

    return score


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


        reasons = ai.get("reasons", [])

    except:
        score -= 10
        reasons = ["AI error"]

    return min(max(score, 0), 100), reasons
