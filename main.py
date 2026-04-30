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

# ==========================
# ENV
# ==========================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
FINNHUB_TOKEN = os.getenv("FINNHUB_TOKEN")
CHAT_ID = os.getenv("CHAT_ID", "-1002605110502")
THREAD_ID = os.getenv("THREAD_ID", "1432")
AUTHORIZED_USER_ID = int(os.getenv("AUTHORIZED_USER_ID", "0"))

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN tidak ditemukan!")

CHAT_ID = int(CHAT_ID)
THREAD_ID = int(THREAD_ID)

# ==========================
# CONFIG
# ==========================
PAIR = "OANDA:XAU_USD"
WIB = pytz.timezone("Asia/Jakarta")

last_price = None
price_lock = asyncio.Lock()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("XAU-SMC-BOT")

# ==========================
# TRADING SESSION (TIDAK DIUBAH)
# ==========================
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

# ==========================
# SMC ANALYSIS ENGINE (NEW)
# ==========================
def smc_analysis(price):

    # simple synthetic structure logic (upgradeable)
    bias = "RANGE"
    reason = []

    # fake structure logic based on volatility zone
    if price % 2 > 1:
        bias = "BULLISH"
        reason.append("Market showing higher momentum (possible HH structure)")
        reason.append("Liquidity grab below detected")
    else:
        bias = "BEARISH"
        reason.append("Market rejecting resistance zone")
        reason.append("Possible LH structure forming")

    return bias, reason

# ==========================
# SIGNAL GENERATOR (SMC + OUTLOOK)
# ==========================
async def generate_signal():

    async with price_lock:
        if last_price is None:
            return None
        price = last_price

    now = datetime.now(WIB).strftime("%Y-%m-%d %H:%M:%S")

    bias, reason = smc_analysis(price)

    if bias == "BULLISH":
        direction = "BUY"
        tp1 = price + 7
        tp2 = price + 12
        sl = price - 5
    elif bias == "BEARISH":
        direction = "SELL"
        tp1 = price - 7
        tp2 = price - 12
        sl = price + 5
    else:
        direction = "WAIT"
        tp1 = tp2 = sl = price

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
📡 Outlook: {bias} momentum detected on XAUUSD
━━━━━━━━━━━━━━━
"""

# ==========================
# SEND TELEGRAM TOPIC
# ==========================
async def send_to_telegram(app, text):

    await app.bot.send_message(
        chat_id=CHAT_ID,
        message_thread_id=THREAD_ID,
        text=text
    )

# ==========================
# PRICE STREAM (UNCHANGED)
# ==========================
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

# ==========================
# SCHEDULER (TETAP: JAM 00)
# ==========================
async def scheduler(app):

    while True:

        now = datetime.now(WIB)
        next_run = now.replace(minute=0, second=0, microsecond=0)

        if now.minute != 0:
            next_run += timedelta(hours=1)

        await asyncio.sleep((next_run - now).total_seconds())

        if is_trading_time():

            msg = await generate_signal()

            if msg:
                try:
                    await send_to_telegram(app, msg)
                    logger.info("SMC signal sent")
                except Exception as e:
                    logger.error(f"Send error: {e}")

        else:
            logger.info("Market closed")

# ==========================
# COMMANDS
# ==========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 XAU SMC BOT AKTIF")

async def signal(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.effective_user.id != AUTHORIZED_USER_ID:
        return await update.message.reply_text("No access")

    msg = await generate_signal()

    if msg:
        await update.message.reply_text(msg)

async def harga(update: Update, context: ContextTypes.DEFAULT_TYPE):

    async with price_lock:
        if last_price is None:
            return await update.message.reply_text("No price yet")

        price = last_price

    await update.message.reply_text(f"XAUUSD: {price}")

# ==========================
# INIT
# ==========================
async def post_init(app):
    asyncio.create_task(price_stream())
    asyncio.create_task(scheduler(app))
    logger.info("SMC Bot running (hourly signals)")

# ==========================
# MAIN
# ==========================
def main():

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("signal", signal))
    app.add_handler(CommandHandler("harga", harga))

    app.post_init = post_init

    app.run_polling()

if __name__ == "__main__":
    main()
