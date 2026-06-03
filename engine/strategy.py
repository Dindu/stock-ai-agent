import json
import re

def score_stock(stock, ai_raw):

    score = 0
    reasons = []

    try:
        if not ai_raw:
            raise ValueError("No AI response")
        # Extract JSON block if model wrapped it in prose
        match = re.search(r'\{.*\}', ai_raw, re.DOTALL)
        ai = json.loads(match.group() if match else ai_raw)

        confidence = ai.get("confidence", 0)
        risk = ai.get("risk_level", "")
        trade_type = ai.get("trade_type", "")

        score += confidence * 0.5

        if trade_type in ["momentum", "breakout"]:
            score += 20

        if risk == "high":
            score -= 20

        reasons = ai.get("reasons", [])

    except:
        score -= 10
        reasons = ["AI error"]

    if stock["change"] > 2:
        score += 10

    if stock["volume"] > 3000000:
        score += 10

    return min(max(score, 0), 100), reasons