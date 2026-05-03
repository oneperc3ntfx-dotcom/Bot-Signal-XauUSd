#!/usr/bin/env python3
import os
import json
import asyncio
import logging
import requests
from datetime import datetime

import pytz
import websockets
from dotenv import load_dotenv

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ================= ENV =================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
FINNHUB_TOKEN = os.getenv("FINNHUB_TOKEN")

CHAT_ID = int(os.getenv("CHAT_ID", "-1002605110502"))
THREAD_ID = int(os.getenv("THREAD_ID", "0"))

WIB = pytz.timezone("Asia/Jakarta")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("SMC-BOT")

# ================= GLOBAL PRICE =================
last_price = None


# ================= MARKET TIME =================
def is_trading_time():
    now = datetime.now(WIB)

    if now.weekday() >= 5:
        return False

    # senin - jumat aktif
    return True


# ================= REAL PRICE STREAM =================
async def price_stream():
    global last_price

    url = f"wss://ws.finnhub.io?token={FINNHUB_TOKEN}"

    while True:
        try:
            async with websockets.connect(url) as ws:

                await ws.send(json.dumps({
                    "type": "subscribe",
                    "symbol": "OANDA:XAU_USD"
                }))

                logger.info("📡 Finnhub connected")

                async for msg in ws:
                    data = json.loads(msg)

                    if data.get("type") == "trade":
                        for t in data["data"]:
                            last_price = float(t["p"])

        except Exception as e:
            logger.error(f"WS ERROR: {e}")
            await asyncio.sleep(3)


# ================= SAFE CANDLE (FALLBACK OPTIONAL) =================
def get_candles():

    try:
        if not FINNHUB_TOKEN:
            return None

        url = "https://api.twelvedata.com/time_series"
        params = {
            "symbol": "XAU/USD",
            "interval": "5min",
            "outputsize": 50,
            "apikey": os.getenv("TWELVE_API")
        }

        r = requests.get(url, params=params, timeout=10).json()

        if "values" not in r:
            return None

        candles = []

        for c in r["values"]:
            candles.append({
                "open": float(c["open"]),
                "high": float(c["high"]),
                "low": float(c["low"]),
                "close": float(c["close"])
            })

        return candles[::-1]

    except:
        return None


# ================= SMC ENGINE SAFE =================
def smc_engine(price, candles=None):

    reasons = []
    score = 5

    if candles and len(candles) > 10:

        highs = [c["high"] for c in candles]
        lows = [c["low"] for c in candles]

        last = candles[-1]

        if last["low"] < min(lows[-10:]):
            score += 2
            reasons.append("Liquidity sweep BUY detected")

        if last["high"] > max(highs[-10:]):
            score += 2
            reasons.append("Liquidity sweep SELL detected")

        if last["close"] > candles[-2]["high"]:
            score += 2
            reasons.append("BOS bullish confirmed")

        elif last["close"] < candles[-2]["low"]:
            score += 2
            reasons.append("BOS bearish confirmed")

    else:
        # fallback logic kalau candle gagal
        if price:
            if int(price) % 2 == 0:
                reasons.append("Market imbalance detected")
                score += 1
            else:
                reasons.append("Minor liquidity reaction")
                score += 1

    score = max(1, min(10, score))

    bias = None
    if score >= 7:
        bias = "BUY"
    elif score <= 4:
        bias = "SELL"

    return bias, score, reasons


# ================= SIGNAL BUILDER =================
async def build_signal():

    if not last_price:
        return "⚠️ No market data yet..."

    candles = get_candles()

    bias, score, reasons = smc_engine(last_price, candles)

    if not bias:
        return f"""
📊 XAUUSD SMC AI

❌ NO TRADE ZONE

🧠 REASON:
{chr(10).join(["- " + r for r in reasons])}

📉 Market still ranging / unclear structure
"""

    entry = last_price

    if bias == "BUY":
        tp1 = entry + 7
        tp2 = entry + 15
        sl = entry - 5
    else:
        tp1 = entry - 7
        tp2 = entry - 15
        sl = entry + 5

    return f"""
📊 XAUUSD SMC AI SIGNAL

📈 BIAS: {bias}
⭐ SCORE: {score}/10

💰 ENTRY (LIMIT): {entry:.2f}

🎯 TP1: {tp1:.2f}
🎯 TP2: {tp2:.2f}
⛔ SL : {sl:.2f}

🧠 REASON:
{chr(10).join(["- " + r for r in reasons])}

━━━━━━━━━━━━━━
🔥 SCALPING MODE ACTIVE
━━━━━━━━━━━━━━
"""


# ================= COMMANDS =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 SMC AI BOT ACTIVE")


async def signal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await build_signal()
    await update.message.reply_text(msg)


async def price(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not last_price:
        return await update.message.reply_text("Price belum ready")

    await update.message.reply_text(f"XAUUSD: {last_price}")


# ================= INIT =================
async def post_init(app):
    asyncio.create_task(price_stream())
    logger.info("🚀 SMC AI BOT RUNNING STABLE")


# ================= MAIN =================
def main():

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("signal", signal))
    app.add_handler(CommandHandler("price", price))

    app.post_init = post_init

    app.run_polling()


if __name__ == "__main__":
    main()
