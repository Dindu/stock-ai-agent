import json
import re
import time
import requests
from config import OLLAMA_URL, GROQ_API_KEY

def analyze(stock, news, macro):

    symbol     = stock.get('symbol', '?')
    price      = stock.get('price', 0)
    change     = stock.get('change', 0)
    volume     = stock.get('volume', 0)
    gap_pct    = stock.get('gap_pct', 0)
    rel_volume = stock.get('rel_volume', 1.0)
    scenario   = stock.get('scenario', 'unknown')
    scene_desc = stock.get('scenario_desc', '')

    # Scenario-specific focus instructions for the AI
    scenario_guides = {
        "gap_up": (
            "SCENARIO: GAP UP — Stock opened significantly higher than yesterday's close.\n"
            "Focus on: What caused the gap? (earnings beat, analyst upgrade, contract win, M&A?)\n"
            "Key question: Is this a sustainable move or will it fade? Is there real fundamental support?\n"
            "High catalyst score if news is specific and material. Low if no identifiable reason."
        ),
        "gap_down": (
            "SCENARIO: GAP DOWN — Stock opened significantly lower, likely on fear or bad news.\n"
            "Focus on: Is the selloff an overreaction? Are fundamentals still intact?\n"
            "Key question: Is this a buying opportunity (panic sell, strong company) or a justified drop?\n"
            "High catalyst score if you identify a clear recovery reason. Penalise if fundamentals are broken."
        ),
        "volume_surge": (
            "SCENARIO: VOLUME SURGE — Unusually high volume vs yesterday. Something is happening.\n"
            "Focus on: Is this institutional accumulation (bullish) or distribution (bearish)?\n"
            "Key question: Does news + price action + volume tell a coherent bullish story?\n"
            "High catalyst score if smart money appears to be buying."
        ),
        "momentum": (
            "SCENARIO: MOMENTUM — Strong intraday move with above-average volume.\n"
            "Focus on: Is there a real catalyst behind the move, or just noise?\n"
            "Key question: Can this momentum continue for 1-2 weeks? Is there room to run?\n"
            "High technicals score if breaking out of a base. Lower if already extended."
        ),
        "oversold": (
            "SCENARIO: OVERSOLD — Stock is down significantly today on high volume.\n"
            "Focus on: Is this capitulation (panic selling near a bottom) or a justified decline?\n"
            "Key question: Are fundamentals still strong? Is the drop macro-driven, not company-specific?\n"
            "High scores only if the company is fundamentally sound and decline is external."
        ),
        "breakout": (
            "SCENARIO: BREAKOUT — Stock is making a measured move above recent resistance.\n"
            "Focus on: Is volume confirming the breakout? Any catalyst driving it?\n"
            "Key question: Is this the start of a new leg up, or a false breakout likely to fail?\n"
            "High technicals score if volume is expanding and price cleared a key level cleanly."
        ),
    }
    scenario_guide = scenario_guides.get(scenario, "Evaluate this stock for a swing trade opportunity.")

    prompt = f"""You are a professional swing trader hunting for GEM opportunities — stocks with 10%+ upside over 1-4 weeks.

{scenario_guide}

Symbol: {symbol}
Detected scenario: {scene_desc}
Price: ${price:.2f} | Change today: {change:+.2f}% | Gap vs yesterday: {gap_pct:+.2f}% | Volume: {volume:,} | Relative Volume: {rel_volume:.1f}x normal

Recent News:
{news or 'No recent news available'}

Market Context:
{macro}

Score strictly across 5 categories:
- catalyst (0-30): Specific identifiable reason for a move in the next 1-4 weeks
  (Earnings beat/FDA/contract = 25-30 | Analyst upgrade = 15-20 | Sector tailwind = 10-15 | No catalyst = 0-5)
- market (0-20): Is macro environment specifically supportive for this stock and sector?
  (Strong sector + rates favorable + SPY stable = 15-20 | Headwinds = 0-8)
- fundamentals (0-20): Revenue growth, profitability trend, price target vs current price
  (Strong growth + >15% analyst upside = 16-20 | Weak/declining = 0-8)
- technicals (0-20): Chart setup — clear entry, defined risk, volume confirmation
  (Breakout/support hold + volume = 16-20 | Extended/no setup = 0-8)
- sentiment (0-10): Analyst consensus, institutional interest, news tone, insider activity
  (Strong buy + positive news = 8-10 | Mixed = 4-6 | Negative = 0-3)

Return JSON only:
{{
  "catalyst": <int 0-30>,
  "market": <int 0-20>,
  "fundamentals": <int 0-20>,
  "technicals": <int 0-20>,
  "sentiment": <int 0-10>,
  "trade_type": "breakout|momentum|reversal|avoid",
  "risk_level": "low|medium|high",
  "catalyst_summary": "<one sentence: the specific gem opportunity or why there is none>",
  "hold_period": "<1-3 days|1-2 weeks|2-4 weeks>",
  "reasons": ["<specific reason 1>", "<specific reason 2>", "<specific reason 3>"]
}}"""

    if GROQ_API_KEY:
        return _analyze_groq(symbol, prompt)
    else:
        return _analyze_ollama(symbol, prompt)


def _analyze_groq(symbol, prompt):
    print(f"[AI] Sending prompt for {symbol} to Groq...", flush=True)
    time.sleep(4)  # proactive throttle: ~15 calls/min stays under 6000 TPM
    for attempt in range(5):
        try:
            r = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {GROQ_API_KEY}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": "llama-3.1-8b-instant",
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.2,
                    "response_format": {"type": "json_object"}
                },
                timeout=30
            )
            data = r.json()
            if "choices" in data:
                print(f"[AI] Got response for {symbol}", flush=True)
                return data["choices"][0]["message"]["content"]
            error = data.get("error", {})
            if isinstance(error, dict) and error.get("code") == "rate_limit_exceeded":
                msg = error.get("message", "")
                wait = re.search(r"try again in ([0-9.]+)s", msg)
                wait_secs = float(wait.group(1)) + 0.5 if wait else 5.0
                print(f"[AI] Rate limited for {symbol}, waiting {wait_secs:.1f}s...", flush=True)
                time.sleep(wait_secs)
                continue
            print(f"[AI] Groq ERROR for {symbol}: {error}", flush=True)
            return None
        except Exception as e:
            print(f"[AI] Groq ERROR for {symbol}: {e}", flush=True)
            return None
    print(f"[AI] Groq gave up after 5 retries for {symbol}", flush=True)
    return None


def _analyze_ollama(symbol, prompt):
    print(f"[AI] Sending prompt for {symbol} to Ollama...", flush=True)
    try:
        r = requests.post(OLLAMA_URL, json={
            "model": "llama3.1",
            "prompt": prompt,
            "stream": False,
            "format": "json"
        }, timeout=60)
        result = r.json()["response"]
        print(f"[AI] Got response for {symbol}", flush=True)
        return result
    except requests.exceptions.ConnectionError:
        print(f"[AI] ERROR: Ollama is not running at {OLLAMA_URL}. Start it with: ollama serve", flush=True)
        return None
    except Exception as e:
        print(f"[AI] ERROR for {symbol}: {e}", flush=True)
        return None