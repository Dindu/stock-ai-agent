import json
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
    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "llama3-8b-8192",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
                "response_format": {"type": "json_object"}
            },
            timeout=30
        )
        data = r.json()
        if "choices" not in data:
            print(f"[AI] Groq ERROR for {symbol}: {data.get('error', data)}", flush=True)
            return None
        result = data["choices"][0]["message"]["content"]
        print(f"[AI] Got response for {symbol}", flush=True)
        return result
    except Exception as e:
        print(f"[AI] Groq ERROR for {symbol}: {e}", flush=True)
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