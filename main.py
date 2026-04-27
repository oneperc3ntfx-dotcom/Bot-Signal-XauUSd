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
# LOAD ENV
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
logger = logging.getLogger("BOT")

# ==========================
# TRADING TIME FILTER
# ==========================
def is_trading_time():

    now = datetime.now(WIB)
    day = now.weekday()  # 0 = Senin

    if day >= 5:  # Sabtu Minggu
        return False

    if 8 <= now.hour < 21:
        return True

    return False

# ==========================
# SIGNAL GENERATOR
# ==========================
async def generate_signal():

    async with price_lock:
        if last_price is None:
            return None
        price = last_price

    direction = random.choice(["BUY", "SELL"])

    if direction == "BUY":
        tp = price + 10
        sl = price - 5
    else:
        tp = price - 10
        sl = price + 5

    now = datetime.now(WIB).strftime("%Y-%m-%d %H:%M:%S")

    return f"""
📊 XAUUSD SIGNAL

Time : {now} WIB
Price : {price}

Direction : {direction}

TP : {round(tp,2)}
SL : {round(sl,2)}

⚠️ Risk management wajib
"""

# ==========================
# REALTIME PRICE (FINNHUB)
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
# AUTO SIGNAL (SETIAP JAM 00)
# ==========================
async def scheduler(app):

    while True:

        now = datetime.now(WIB)

        next_run = now.replace(minute=0, second=0, microsecond=0)

        if now.minute != 0:
            next_run += timedelta(hours=1)

        wait = (next_run - now).total_seconds()

        await asyncio.sleep(wait)

        if is_trading_time():

            msg = await generate_signal()

            if msg:
                try:
                    await app.bot.send_message(
                        chat_id=CHAT_ID,
                        text=msg
                    )
                    logger.info("Signal sent to group")
                except Exception as e:
                    logger.error(f"Send error: {e}")

        else:
            logger.info("Market closed")

# ==========================
# COMMAND: START
# ==========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        "🤖 BOT AKTIF\n\n"
        "/signal - ambil signal\n"
        "/harga - lihat harga"
    )

# ==========================
# COMMAND: HARGA
# ==========================
async def harga(update: Update, context: ContextTypes.DEFAULT_TYPE):

    async with price_lock:
        if last_price is None:
            return await update.message.reply_text("Harga belum tersedia")
        price = last_price

    await update.message.reply_text(f"XAUUSD: {price}")

# ==========================
# COMMAND: SIGNAL (PRIVATE ONLY)
# ==========================
async def signal(update: Update, context: ContextTypes.DEFAULT_TYPE):

    # hanya private chat
    if update.effective_chat.type != "private":
        return

    # hanya user tertentu
    if update.effective_user.id != AUTHORIZED_USER_ID:
        return await update.message.reply_text("Tidak diizinkan")

    msg = await generate_signal()

    if not msg:
        return await update.message.reply_text("Harga belum tersedia")

    await update.message.reply_text(msg)

# ==========================
# POST INIT
# ==========================
async def post_init(app):
    app.create_task(price_stream())
    app.create_task(scheduler(app))
    logger.info("Background tasks running")

# ==========================
# MAIN
# ==========================
def main():

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("harga", harga))
    app.add_handler(CommandHandler("signal", signal))

    app.post_init = post_init

    logger.info("BOT STARTED")
    app.run_polling()

if __name__ == "__main__":
    main()
