import json
import asyncio
import websockets
from config import FINNHUB_TOKEN, PAIR

last_price = None

async def stream_price(lock):
    global last_price

    url = f"wss://ws.finnhub.io?token={FINNHUB_TOKEN}"

    while True:
        try:
            async with websockets.connect(url) as ws:

                await ws.send(json.dumps({
                    "type": "subscribe",
                    "symbol": PAIR
                }))

                async for msg in ws:
                    data = json.loads(msg)

                    if data.get("type") == "trade":
                        for t in data["data"]:
                            async with lock:
                                last_price = float(t["p"])

        except:
            await asyncio.sleep(5)
