"""Read-only market and trade observer.

This process may summarize bot logs and public RSS headlines, but it has no
broker client, order API, or trading-state write access by design.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import requests


LOG_PATH = Path(os.getenv("OBSERVER_LOG_PATH", "bot_output.log"))
OBSERVER_WEBHOOK = (
    os.getenv("OBSERVER_DISCORD_WEBHOOK", "").strip()
    or os.getenv("DISCORD_WEBHOOK", "").strip()
)
OBSERVER_ENABLED = os.getenv("OBSERVER_ENABLED", "0") == "1"
OLLAMA_URL = os.getenv("OBSERVER_OLLAMA_URL", "http://localhost:11434/api/generate")
OLLAMA_MODEL = os.getenv("OBSERVER_OLLAMA_MODEL", "qwen2.5:7b")
GROQ_API_KEY = (
    os.getenv("OBSERVER_GROQ_API_KEY", "").strip()
    or os.getenv("GROQ_API_KEY", "").strip()
)
_configured_observer_model = (
    os.getenv("OBSERVER_GROQ_MODEL", "").strip()
    or os.getenv("GROQ_MODEL", "").strip()
)
GROQ_MODEL = (
    "llama-3.3-70b-versatile"
    if _configured_observer_model in {"", "llama-3.1-8b-instant"}
    else _configured_observer_model
)
INTERVAL_SECONDS = max(60, int(os.getenv("OBSERVER_INTERVAL_SECONDS", "600")))
NEWS_INTERVAL_SECONDS = max(300, int(os.getenv("OBSERVER_NEWS_INTERVAL_SECONDS", "900")))
NEWS_FEEDS = [
    feed.strip()
    for feed in os.getenv(
        "OBSERVER_NEWS_FEEDS",
        "https://news.google.com/rss/search?q=stocks%20OR%20markets%20OR%20SPY%20OR%20QQQ%20OR%20NVDA%20OR%20AAPL&hl=en-US&gl=US&ceid=US%3Aen",
    ).split(",")
    if feed.strip()
]

TRADE_MARKERS = re.compile(
    r"Paper trade opened|Trade .* opened|Closed .*|Exit fill|Exit intent|"
    r"position sync|THESIS SCORE WARNING|GainzAlgo confirmed|No tradeable contract",
    re.IGNORECASE,
)

SYSTEM_PROMPT = """You are a read-only market and trade observer for a paper-trading bot.
Analyze the supplied facts like a careful human reviewer. Separate observed facts
from interpretation and mention timestamps. Discuss open trades, recent entries
and exits, strategy behavior, execution quality, market context, news, and patterns.
Never give executable trading instructions. Never recommend an entry, exit, order,
contract, size, threshold, configuration change, or override. Do not claim certainty.
Your output is an informational report only and cannot affect the trading bot."""


def _recent_log_lines(path: Path, limit: int = 1200) -> list[str]:
    if not path.exists():
        return [f"Log file not found: {path}"]
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        lines = list(deque(handle, maxlen=limit))
    return [line.rstrip() for line in lines if line.strip()]


def _interesting_lines(lines: Iterable[str], limit: int = 180) -> list[str]:
    selected = [line for line in lines if TRADE_MARKERS.search(line)]
    return selected[-limit:]


def _parse_news(xml_text: str, limit: int = 12) -> list[dict[str, str]]:
    root = ET.fromstring(xml_text)
    items = []
    for item in root.findall(".//item")[:limit]:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        published = (item.findtext("pubDate") or "").strip()
        if title:
            items.append({"title": title, "link": link, "published": published})
    return items


def fetch_news() -> list[dict[str, str]]:
    headlines: list[dict[str, str]] = []
    for feed in NEWS_FEEDS:
        try:
            response = requests.get(feed, timeout=10)
            response.raise_for_status()
            headlines.extend(_parse_news(response.text))
        except Exception as exc:
            headlines.append({"title": f"News feed unavailable: {feed} ({exc})", "link": "", "published": ""})
    return headlines[:24]


def _call_ollama(prompt: str) -> str:
    response = requests.post(
        OLLAMA_URL,
        json={"model": OLLAMA_MODEL, "prompt": prompt, "system": SYSTEM_PROMPT, "stream": False},
        timeout=90,
    )
    response.raise_for_status()
    return str(response.json().get("response", "")).strip()


def _call_groq(prompt: str) -> str:
    response = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
        json={
            "model": GROQ_MODEL,
            "temperature": 0.2,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}],
        },
        timeout=90,
    )
    response.raise_for_status()
    return str(response.json()["choices"][0]["message"]["content"]).strip()


def generate_report(log_lines: list[str], headlines: list[dict[str, str]]) -> str:
    facts = {
        "observed_at_utc": datetime.now(timezone.utc).isoformat(),
        "trade_and_bot_events": _interesting_lines(log_lines),
        "public_news_headlines": headlines,
    }
    prompt = (
        "Create a concise observer report with these headings: MARKET CONTEXT, "
        "TRADE MONITOR, BOT BEHAVIOR, NEWS, PATTERNS TO REVIEW. Keep it under 1,700 "
        "characters. State when data is missing. Treat news as headlines, not verified "
        "facts. Do not include buy/sell/hold or order recommendations.\n\n"
        + json.dumps(facts, ensure_ascii=True)
    )
    try:
        return _call_groq(prompt) if GROQ_API_KEY else _call_ollama(prompt)
    except Exception as exc:
        return f"Observer unavailable: {exc}\n\nLatest observed events:\n" + "\n".join(_interesting_lines(log_lines, 20))


def send_discord(report: str) -> bool:
    if not OBSERVER_WEBHOOK:
        return False
    response = requests.post(
        OBSERVER_WEBHOOK,
        json={"content": f"**READ-ONLY MARKET OBSERVER**\n{report[:1900]}"},
        timeout=15,
    )
    response.raise_for_status()
    return True


def run_once(include_news: bool = True) -> str:
    headlines = fetch_news() if include_news else []
    report = generate_report(_recent_log_lines(LOG_PATH), headlines)
    send_discord(report)
    return report


def _observer_loop(log_fn=None) -> None:
    last_news = 0.0
    while True:
        try:
            now = time.monotonic()
            include_news = now - last_news >= NEWS_INTERVAL_SECONDS
            report = run_once(include_news=include_news)
            if include_news:
                last_news = now
            if not OBSERVER_WEBHOOK and log_fn:
                log_fn("[OBSERVER] disabled output: set a separate OBSERVER_DISCORD_WEBHOOK")
            elif log_fn:
                log_fn("[OBSERVER] read-only report sent to Discord")
        except Exception as exc:
            if log_fn:
                log_fn(f"[OBSERVER] unavailable (trading unaffected): {exc}")
        time.sleep(INTERVAL_SECONDS)


def start_background_observer(log_fn=None):
    """Start the observer without blocking or sharing the trading control path."""
    if not OBSERVER_ENABLED:
        if log_fn:
            log_fn("[OBSERVER] disabled by OBSERVER_ENABLED=0")
        return None
    if not OBSERVER_WEBHOOK:
        if log_fn:
            log_fn("[OBSERVER] not started: OBSERVER_DISCORD_WEBHOOK is not configured")
        return None
    thread = threading.Thread(target=_observer_loop, args=(log_fn,), name="market-observer", daemon=True)
    thread.start()
    return thread


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the read-only market observer")
    parser.add_argument("--once", action="store_true", help="Generate one report and exit")
    args = parser.parse_args()
    if args.once:
        report = run_once()
        if not OBSERVER_WEBHOOK:
            print(report)
        return

    _observer_loop()


if __name__ == "__main__":
    main()
