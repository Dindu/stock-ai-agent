import json
import re
import time
import requests
from config import OLLAMA_URL, GROQ_API_KEY

def analyze(stock, news, macro):

    symbol = stock.get('symbol', '?')
    price  = stock.get('price', 0)
    change = stock.get('change', 0)
    volume = stock.get('volume', 0)

    prompt = f"""You are a professional swing trader hunting for GEM opportunities — stocks with 10%+ upside potential over 1-4 weeks.

You are NOT just looking for stocks already moving up. You are scouting for:
1. Stocks with a strong catalyst (earnings beat, analyst upgrade, new contract, product launch) that have upside
2. Oversold stocks that dropped on panic/sector rotation but have strong fundamentals — bounce candidates
3. Stocks with major volume spikes suggesting institutional accumulation before a big move
4. Early-stage breakouts before the crowd notices
5. Stocks where news impact hasn't fully priced in yet

Symbol: {symbol}
Price: ${price:.2f} | Change today: {change:+.2f}% | Volume: {volume:,}

Recent News:
{news or 'No recent news available'}

Market Context:
{macro}

Score this stock across 5 categories. Be strict and honest.
A stock DOWN today can still score high if there is a real recovery catalyst.
A stock UP today can score low if the move is random with no fundamental reason.

SCORING GUIDE:
- catalyst (0-30): Clear specific reason for a move in the next 1-4 weeks?
  (Earnings beat / major contract / FDA approval = 25-30, Analyst upgrade = 15-20, Sector tailwind = 10-15, No catalyst = 0-5)
- market (0-20): Is macro environment supportive for THIS stock?
  (Rate cuts favor fintechs, defense stocks strong in geopolitical tension, etc. Relevant tailwind = 15-20, Headwind = 0-8)
- fundamentals (0-20): Revenue growth, profitability trend, analyst price target vs current price?
  (Strong growth + target upside >15% = 16-20, Declining or no upside = 0-8)
- technicals (0-20): Chart setup — is there a clear entry with defined risk?
  (Near support after drop + volume = 16-20, Breakout from base = 14-18, Extended/no setup = 0-8)
- sentiment (0-10): Analyst consensus, institutional interest, news tone, insider activity?
  (Strong buy consensus + positive news = 8-10, Mixed = 4-6, Negative = 0-3)

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