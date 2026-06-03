import json
import re

def pre_score(stock):
    """Rule-based score using only price/volume data. No AI needed."""
    score = 0

    change = stock.get("change", 0)
    volume = stock.get("volume", 0)

    # Long-only: never score negative stocks
    if change <= 0:
        return 0

    # Price momentum
    if change >= 5:
        score += 30
    elif change >= 3:
        score += 20
    elif change >= 2:
        score += 12
    elif change >= 1.5:
        score += 6

    # Volume strength
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
    """Full score: rule-based pre_score + AI confidence on top."""
    score = pre_score(stock)
    reasons = []

    try:
        if not ai_raw:
            raise ValueError("No AI response")
        match = re.search(r'\{.*\}', ai_raw, re.DOTALL)
        ai = json.loads(match.group() if match else ai_raw)

        confidence = ai.get("confidence", 0)
        risk = ai.get("risk_level", "")
        trade_type = ai.get("trade_type", "")

        score += confidence * 0.5

        if trade_type in ["momentum", "breakout"]:
            score += 15

        if risk == "high":
            score -= 15

        reasons = ai.get("reasons", [])

    except:
        score -= 10
        reasons = ["AI error"]

    return min(max(score, 0), 100), reasons
