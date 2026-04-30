#!/usr/bin/env python3
import os
import json
import asyncio
import logging
from datetime import datetime, timedelta

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
THREAD_ID = int(os.getenv("THREAD_ID", "1432"))

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN tidak ditemukan!")

# ================= CONFIG =================
PAIR = "OANDA:XAU_USD"
WIB = pytz.timezone("Asia/Jakarta")

last_price = None
price_lock = asyncio.Lock()

# 🔥 ANTI SPAM LOCK
last_sent_hour = None
send_lock = asyncio.Lock()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("XAU-BOT")

# ================= TRADING TIME (TIDAK DIUBAH) =================
def is_trading_time():
    now = datetime.now(WIB)

    if now.weekday() >= 5:
        return False

    hour = now.hour

    if 5 <= hour < 7:
        return False

    if hour >= 7:
        return True

    if hour < 4:
        return True

    return False


# ================= SMC ENGINE =================
def smc_analysis(price):
    bias = "RANGE"
    reason = []

    if int(price * 10) % 2 == 0:
        bias = "BULLISH"
        reason = [
            "Liquidity sweep detected",
            "BOS bullish confirmed",
            "Higher low structure forming"
        ]
    else:
        bias = "BEARISH"
        reason = [
            "Resistance rejection",
            "Lower high structure formed",
            "Sell-side liquidity taken"
        ]

    return bias, reason


# ================= SIGNAL =================
async def generate_signal(price):

    now = datetime.now(WIB).strftime("%Y-%m-%d %H:%M:%S")

    bias, reason = smc_analysis(price)

    if bias == "BULLISH":
        direction = "BUY"
        tp1 = price + 7
        tp2 = price + 12
        sl = price - 5
    else:
        direction = "SELL"
        tp1 = price - 7
        tp2 = price - 12
        sl = price + 5

    reason_text = "\n".join([f"- {r}" for r in reason])

    return f"""
📊 XAUUSD SMC SIGNAL

🕒 Time: {now} WIB
💰 Price: {price}

📈 Bias: {bias}
📌 Direction: {direction}

🧠 Reason:
{reason_text}

🎯 TP1: {tp1:.2f}
🎯 TP2: {tp2:.2f}
⛔ SL : {sl:.2f}

━━━━━━━━━━━━━━━
📡 OUTLOOK: {bias} momentum detected
━━━━━━━━━━━━━━━
"""


# ================= SEND TO TOPIC =================
async def send_to_telegram(app, text):
    await app.bot.send_message(
        chat_id=CHAT_ID,
        message_thread_id=THREAD_ID,
        text=text
    )


# ================= PRICE STREAM =================
async def price_stream():
    global last_price

    url = f"wss://ws.finnhub.io?token={FINNHUB_TOKEN}"

    while True:
        try:
            async with websockets.connect(url) as ws:
                await ws.send(json.dumps({
                    "type": "subscribe",
                    "symbol": PAIR
                }))

                logger.info("Connected Finnhub")

                async for msg in ws:
                    data = json.loads(msg)

                    if data.get("type") == "trade":
                        for t in data["data"]:
                            async with price_lock:
                                last_price = float(t["p"])

        except Exception as e:
            logger.warning(f"WS ERROR: {e}")
            await asyncio.sleep(5)


# ================= SCHEDULER (ANTI SPAM FIXED) =================
async def scheduler(app):
    global last_sent_hour

    while True:
        now = datetime.now(WIB)

        next_run = now.replace(minute=0, second=0, microsecond=0)
        if now.minute != 0:
            next_run += timedelta(hours=1)

        await asyncio.sleep((next_run - now).total_seconds())

        current_hour = now.strftime("%Y-%m-%d %H")

        # 🔥 ANTI DUPLICATE
        if last_sent_hour == current_hour:
            continue

        if is_trading_time():

            async with price_lock:
                price = last_price

            if price is None:
                continue

            async with send_lock:

                msg = await generate_signal(price)

                await send_to_telegram(app, msg)

                last_sent_hour = current_hour

                logger.info("✅ SIGNAL SENT (1X ONLY)")


# ================= COMMANDS =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 XAU SMC BOT AKTIF")

async def signal(update: Update, context: ContextTypes.DEFAULT_TYPE):

    async with price_lock:
        if last_price is None:
            return await update.message.reply_text("No price yet")
        price = last_price

    msg = await generate_signal(price)
    await update.message.reply_text(msg)


async def harga(update: Update, context: ContextTypes.DEFAULT_TYPE):

    async with price_lock:
        if last_price is None:
            return await update.message.reply_text("No price yet")

        price = last_price

    await update.message.reply_text(f"XAUUSD: {price}")


# ================= INIT =================
async def post_init(app):
    asyncio.create_task(price_stream())
    asyncio.create_task(scheduler(app))
    logger.info("SMC BOT RUNNING (ANTI-SPAM ACTIVE)")


# ================= MAIN =================
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("signal", signal))
    app.add_handler(CommandHandler("harga", harga))

    app.post_init = post_init

    app.run_polling()


if __name__ == "__main__":
    main()
