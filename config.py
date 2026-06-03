import os
from dotenv import load_dotenv

load_dotenv()

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_BASE_URL = "https://paper-api.alpaca.markets"
ALPACA_DATA_URL = "https://data.alpaca.markets"

DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK")

OLLAMA_URL = "http://localhost:11434/api/generate"

SCAN_INTERVAL = 300