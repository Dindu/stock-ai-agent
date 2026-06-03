import json
import re
import time
import requests
from config import OLLAMA_URL, GROQ_API_KEY

def analyze(stock, news, macro):

    prompt = f"""
You are a professional swing trader.

Stock: {stock}
News: {news}
Macro: {macro}

Return JSON:
{{
 "confidence": 0-100,
 "bias": "bullish|bearish|neutral",
 "trade_type": "breakout|momentum|avoid",
 "risk_level": "low|medium|high",
 "reasons": ["r1","r2","r3"]
}}
"""

    symbol = stock.get('symbol', '?')

    if GROQ_API_KEY:
        return _analyze_groq(symbol, prompt)
    else:
        return _analyze_ollama(symbol, prompt)


def _analyze_groq(symbol, prompt):
    print(f"[AI] Sending prompt for {symbol} to Groq...", flush=True)
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