import requests
from config import OLLAMA_URL

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

    print(f"[AI] Sending prompt for {stock.get('symbol', '?')} to Ollama...", flush=True)
    try:
        r = requests.post(OLLAMA_URL, json={
            "model": "llama3.1",
            "prompt": prompt,
            "stream": False,
            "format": "json"
        }, timeout=60)
        result = r.json()["response"]
        print(f"[AI] Got response for {stock.get('symbol', '?')}", flush=True)
        return result
    except requests.exceptions.ConnectionError:
        print(f"[AI] ERROR: Ollama is not running at {OLLAMA_URL}. Start it with: ollama serve", flush=True)
        return None
    except Exception as e:
        print(f"[AI] ERROR for {stock.get('symbol', '?')}: {e}", flush=True)
        return None