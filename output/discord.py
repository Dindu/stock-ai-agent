import requests
from config import DISCORD_WEBHOOK

def send(stock):

    msg = f"""
🚀 TRADE ALERT

{stock['symbol']}
Score: {stock['score']}

Price: {stock['price']}
Change: {stock['change']:.2f}%

Reasons:
""" + "\n".join(stock.get("reasons", []))

    requests.post(DISCORD_WEBHOOK, json={"content": msg})