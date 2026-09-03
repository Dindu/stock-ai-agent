# Read-Only Market Observer

`market_observer.py` produces informational market and trade reports. It is intentionally separate from the trading bot and does not import execution, strategy, contract-selection, or exit modules.

## Safety boundary

The observer can read:

- `bot_output.log` (or `OBSERVER_LOG_PATH`)
- public RSS headlines
- its own environment configuration

The observer cannot:

- place, cancel, modify, or size orders;
- select option contracts;
- change Gainz thresholds or configuration;
- trigger entries or exits;
- write trading state or broker records.

The observer reuses the existing `DISCORD_WEBHOOK` by default. Set `OBSERVER_DISCORD_WEBHOOK` only if you later want a separate channel.

## Local setup

Install [Ollama](https://ollama.com), then download a local model:

```text
ollama pull qwen2.5:7b
```

Run one report locally:

```text
OBSERVER_LOG_PATH=bot_output.log python3 market_observer.py --once
```

Without `OBSERVER_DISCORD_WEBHOOK`, the report is printed only. To send reports to a separate Discord channel:

```text
OBSERVER_DISCORD_WEBHOOK=<read-only-observer-webhook> python3 market_observer.py
```

## Configuration

- `OBSERVER_LOG_PATH`: bot log path, default `bot_output.log`
- `OBSERVER_DISCORD_WEBHOOK`: optional separate webhook; defaults to the existing `DISCORD_WEBHOOK`
- `OBSERVER_OLLAMA_URL`: default `http://localhost:11434/api/generate`
- `OBSERVER_OLLAMA_MODEL`: default `qwen2.5:7b`
- `OBSERVER_GROQ_API_KEY`: optional observer-specific cloud key; defaults to the existing `GROQ_API_KEY`
- `OBSERVER_GROQ_MODEL`: default `llama-3.1-8b-instant`
- `OBSERVER_INTERVAL_SECONDS`: report interval, default `600` (10 minutes)
- `OBSERVER_NEWS_INTERVAL_SECONDS`: RSS refresh interval, default `900`
- `OBSERVER_NEWS_FEEDS`: comma-separated public RSS URLs

The trading bot starts this observer automatically as a daemon worker when `DISCORD_WEBHOOK` or `OBSERVER_DISCORD_WEBHOOK` is configured. It remains a separate worker so an observer outage cannot affect trading. Set `OBSERVER_ENABLED=0` to disable automatic startup, or run `python3 market_observer.py --once` manually for a print-only report.
