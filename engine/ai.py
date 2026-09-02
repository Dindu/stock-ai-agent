import json
import re
import time
import requests
from config import OLLAMA_URL, GROQ_API_KEY

def analyze(stock, news, macro):
    """
    AI focuses only on text/news analysis — what it's actually good at.
    Returns catalyst score (0-30), fundamentals (0-15), market (0-10),
    plus catalyst_type, trade_type, risk_level, and reasons.

    Rules-based code handles: insider (0-20), accumulation (0-15), technicals (0-10).
    Total possible from AI: 55 points. From rules: 45 points. Combined: 100.
    """
    symbol     = stock.get('symbol', '?')
    price      = stock.get('price', 0)
    change     = stock.get('change', 0)
    volume     = stock.get('volume', 0)
    gap_pct    = stock.get('gap_pct', 0)
    rel_volume = stock.get('rel_volume', 1.0)
    scenario   = stock.get('scenario', 'unknown')
    scene_desc = stock.get('scenario_desc', '')

    scenario_guides = {
        "gap_up": (
            "SCENARIO: GAP UP — Stock opened significantly higher overnight.\n"
            "Your job: Identify the specific catalyst (earnings beat, contract, upgrade, M&A).\n"
            "High catalyst score ONLY if news is specific and material. 0-5 if no clear reason."
        ),
        "gap_down": (
            "SCENARIO: GAP DOWN — Stock dropped overnight on fear or bad news.\n"
            "Your job: Is this an overreaction bounce candidate, or is the company genuinely broken?\n"
            "High scores ONLY if fundamentals are intact and the drop is external/sector-driven."
        ),
        "volume_surge": (
            "SCENARIO: VOLUME SURGE — Unusually high volume. Something is happening.\n"
            "Your job: Does the news explain this volume? Is it institutional accumulation?\n"
            "High catalyst score if news + volume tell a coherent bullish story."
        ),
        "momentum": (
            "SCENARIO: MOMENTUM — Strong intraday move with above-average volume.\n"
            "Your job: Is there a real catalyst, or just noise? Can momentum continue 1-2 weeks?\n"
            "High scores only if there's a specific news driver with room to run."
        ),
        "oversold": (
            "SCENARIO: OVERSOLD — Stock is significantly down today.\n"
            "Your job: Is this panic selling of a fundamentally strong company?\n"
            "High scores ONLY if the drop is macro/sector driven, not company-specific bad news."
        ),
        "breakout": (
            "SCENARIO: BREAKOUT — Stock making a move above recent resistance.\n"
            "Your job: Is there a catalyst confirming the breakout, or is it random?\n"
            "High scores if volume confirms and there's a specific reason for the move."
        ),
    }
    scenario_guide = scenario_guides.get(scenario, "Identify whether this stock has a genuine opportunity.")

    prompt = f"""You are a professional stock analyst identifying high-conviction swing trade opportunities.

{scenario_guide}

Symbol: {symbol}
Price: ${price:.2f} | Change: {change:+.2f}% | Gap vs yesterday: {gap_pct:+.2f}% | Volume: {volume:,} | Relative Volume: {rel_volume:.1f}x

Recent News:
{news or 'No recent news available'}

Market Context:
{macro}

Score ONLY the factors you can assess from news and context. Be strict.

SCORING:
- catalyst (0-30): Specific identifiable catalyst driving potential upside in 1-4 weeks
  30=earnings beat/FDA approval/major contract | 20=analyst upgrade | 12=sector tailwind | 0-5=no catalyst
- fundamentals (0-15): Revenue growth trend, analyst price target vs current price, profit trajectory
  15=strong growth + >20% target upside | 8=moderate | 0-4=declining/no upside
- market (0-10): Is macro specifically supportive for THIS stock's sector right now?
  10=strong tailwind (rate cuts for fintechs, defense in tension, etc.) | 5=neutral | 0-2=headwind

- catalyst_type: classify the primary driver as one of:
  earnings_beat | analyst_upgrade | contract_win | fda_approval | partnership |
  sector_rotation | rate_catalyst | short_squeeze | general_momentum | none

Return JSON only:
{{
  "catalyst": <int 0-30>,
  "fundamentals": <int 0-15>,
  "market": <int 0-10>,
  "catalyst_type": "<one of the types above>",
  "trade_type": "breakout|momentum|reversal|avoid",
  "risk_level": "low|medium|high",
  "catalyst_summary": "<one specific sentence: the gem opportunity or why there is none>",
  "hold_period": "<1-3 days|1-2 weeks|2-4 weeks>",
  "reasons": ["<specific reason 1>", "<specific reason 2>", "<specific reason 3>"]
}}"""

    if GROQ_API_KEY:
        return _analyze_groq(symbol, prompt)
    else:
        return _analyze_ollama(symbol, prompt)


def analyze_briefing(prompt):
    """Generate one model-backed market briefing from an aggregate prompt."""
    if GROQ_API_KEY:
        return _analyze_groq_briefing(prompt)
    return _analyze_ollama_briefing(prompt)




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


def _analyze_groq_briefing(prompt):
    print("[AI] Sending aggregate briefing to Groq...", flush=True)
    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "llama-3.1-8b-instant",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
            },
            timeout=45,
        )
        data = r.json()
        if "choices" in data:
            print("[AI] Got aggregate briefing response", flush=True)
            return data["choices"][0]["message"].get("content")
        print(f"[AI] Groq briefing ERROR: {data.get('error', data)}", flush=True)
    except Exception as e:
        print(f"[AI] Groq briefing ERROR: {e}", flush=True)
    return None


def _analyze_ollama_briefing(prompt):
    print("[AI] Sending aggregate briefing to Ollama...", flush=True)
    try:
        r = requests.post(
            OLLAMA_URL,
            json={"model": "llama3.1", "prompt": prompt, "stream": False},
            timeout=90,
        )
        result = r.json().get("response")
        if result:
            print("[AI] Got aggregate briefing response", flush=True)
        return result
    except Exception as e:
        print(f"[AI] Ollama briefing ERROR: {e}", flush=True)
        return None