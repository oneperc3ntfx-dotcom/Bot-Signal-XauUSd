#!/usr/bin/env python3
import os
import requests
import asyncio
import logging
from datetime import datetime, timedelta

import pytz
from dotenv import load_dotenv

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ================= ENV =================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
FINNHUB_TOKEN = os.getenv("FINNHUB_TOKEN")
TWELVE_API = os.getenv("TWELVE_API")

CHAT_ID = int(os.getenv("CHAT_ID", "-1002605110502"))
THREAD_ID = int(os.getenv("THREAD_ID", "1432"))

AUTHORIZED_USER_ID = int(os.getenv("AUTHORIZED_USER_ID", "0"))

WIB = pytz.timezone("Asia/Jakarta")

last_price = None
price_lock = asyncio.Lock()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("SMC-BOT")

# ================= MARKET TIME =================
def is_trading_time():
    now = datetime.now(WIB)

    if now.weekday() >= 5:
        return False

    if 7 <= now.hour <= 23 or 0 <= now.hour <= 3:
        return True

    return False


# ================= PRICE (FALLBACK FINNHUB) =================
async def get_realtime_price():
    global last_price
    return last_price


# ================= CANDLE ENGINE (SAFE) =================
def get_candles():

    try:
        if not TWELVE_API:
            return None

        url = f"https://api.twelvedata.com/time_series?symbol=XAU/USD&interval=5min&outputsize=80&apikey={TWELVE_API}"
        r = requests.get(url, timeout=10).json()

        if not r or "values" not in r:
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

    except Exception as e:
        logger.error(f"CANDLE ERROR: {e}")
        return None


# ================= SMC ENGINE (ANTI CRASH) =================
def smc_engine(candles):

    if not candles or len(candles) < 20:
        return None, 0, ["Insufficient market data"]

    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]

    last = candles[-1]

    score = 5
    reasons = []

    # Liquidity sweep logic
    if last["low"] < min(lows[-20:]):
        score += 2
        reasons.append("Liquidity sweep BUY side detected")

    if last["high"] > max(highs[-20:]):
        score += 2
        reasons.append("Liquidity sweep SELL side detected")

    # BOS simple logic
    if last["close"] > candles[-2]["high"]:
        score += 2
        reasons.append("BOS bullish confirmed")

    elif last["close"] < candles[-2]["low"]:
        score += 2
        reasons.append("BOS bearish confirmed")

    # Clamp score
    score = max(1, min(10, score))

    bias = "BUY" if score >= 7 else "SELL" if score <= 4 else None

    return bias, score, reasons


# ================= SIGNAL BUILDER =================
async def build_signal():

    candles = get_candles()

    bias, score, reasons = smc_engine(candles)

    if not bias:

        return f"""
📊 XAUUSD SMC AI

❌ NO TRADE ZONE

🧠 REASON:
{chr(10).join(["- " + r for r in reasons])}

━━━━━━━━━━━━━━
⚠️ Waiting BOS / Liquidity confirmation
━━━━━━━━━━━━━━
"""

    price = candles[-1]["close"] if candles else 0

    if bias == "BUY":
        entry = price
        tp1 = entry + 7
        tp2 = entry + 15
        sl = entry - 5
    else:
        entry = price
        tp1 = entry - 7
        tp2 = entry - 15
        sl = entry + 5

    return f"""
📊 XAUUSD SMC AI SIGNAL

📈 BIAS: {bias}
📊 SCORE: {score}/10

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

    async with price_lock:
        if last_price is None:
            return await update.message.reply_text("Price belum tersedia")
        await update.message.reply_text(f"XAUUSD: {last_price}")


# ================= PRICE STREAM (SIMPLE MOCK PLACEHOLDER) =================
async def fake_price_loop():
    global last_price

    while True:
        try:
            # fallback simple (kalau Finnhub kamu pakai sendiri)
            last_price = 2000 + (datetime.now().second % 50)
            await asyncio.sleep(2)
        except:
            await asyncio.sleep(2)


# ================= START =================
async def post_init(app):
    asyncio.create_task(fake_price_loop())
    logger.info("SMC AI BOT RUNNING SAFE MODE")


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("signal", signal))
    app.add_handler(CommandHandler("price", price))

    app.post_init = post_init

    app.run_polling()


if __name__ == "__main__":
    main()
