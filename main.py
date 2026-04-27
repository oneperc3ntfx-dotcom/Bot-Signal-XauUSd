#!/usr/bin/env python3
import os
import json
import asyncio
import random
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
CHAT_ID = os.getenv("CHAT_ID")
AUTHORIZED_USER_ID = int(os.getenv("AUTHORIZED_USER_ID", "0"))

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN tidak ditemukan!")

# ==========================
# CONFIG
# ==========================
PAIR = "OANDA:XAU_USD"
WIB = pytz.timezone("Asia/Jakarta")

last_price = None
price_lock = asyncio.Lock()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("RANDOM-BOT")

# ==========================
# TRADING TIME
# ==========================
def is_trading_time():
    now = datetime.now(WIB)

    if now.weekday() >= 5:
        return False

    return 8 <= now.hour < 21

# ==========================
# SIGNAL GENERATOR
# ==========================
async def generate_signal():

    async with price_lock:
        if last_price is None:
            return None
        price = last_price

    now = datetime.now(WIB).strftime("%Y-%m-%d %H:%M:%S")

    direction = random.choice(["BUY", "SELL"])

    pip = 0.1

    if direction == "BUY":
        tp1 = price + 70 * pip
        tp2 = price + 100 * pip
        sl = price - 40 * pip
    else:
        tp1 = price - 70 * pip
        tp2 = price - 100 * pip
        sl = price + 40 * pip

    return f"""
📊 XAUUSD SIGNAL

🕒 Time : {now} WIB
💰 Price : {price}

📈 Direction : {direction}

🎯 TP1 : {round(tp1,2)}
🎯 TP2 : {round(tp2,2)}
⛔ SL  : {round(sl,2)}

━━━━━━━━━━━━━━━

⚠️ Note:
- Hindari entry saat harga tidak sesuai dengan pasar
- Hindari entry saat candle agresif
- Hindari saat news high impact

━━━━━━━━━━━━━━━
"""

# ==========================
# REALTIME PRICE
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
# SCHEDULER (SETIAP JAM 00 WIB)
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
                    await app.bot.send_message(chat_id=CHAT_ID, text=msg)
                    logger.info("Signal sent to group")
                except Exception as e:
                    logger.error(f"Send error: {e}")
        else:
            logger.info("Market closed")

# ==========================
# COMMANDS
# ==========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 RANDOM SIGNAL BOT AKTIF")

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
# POST INIT
# ==========================
async def post_init(app):
    asyncio.create_task(price_stream())
    asyncio.create_task(scheduler(app))
    logger.info("Bot running")

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
