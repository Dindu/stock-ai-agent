import os
from dotenv import load_dotenv

load_dotenv()

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_BASE_URL = "https://paper-api.alpaca.markets"
ALPACA_DATA_URL = "https://data.alpaca.markets"

DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK")

GROQ_API_KEY = os.getenv("GROQ_API_KEY")  # Set this on Render for cloud LLM
OLLAMA_URL = "http://localhost:11434/api/generate"  # Used locally if GROQ_API_KEY not set

SCAN_INTERVAL = 300