#!/usr/bin/env python3
import os
import json
import asyncio
import random
import logging
import requests
from datetime import datetime, timedelta

import pytz
import websockets
from dotenv import load_dotenv

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# =========================
# LOAD ENV
# =========================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
FINNHUB_TOKEN = os.getenv("FINNHUB_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
AUTHORIZED_USER_ID = int(os.getenv("AUTHORIZED_USER_ID", "0"))

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN tidak ditemukan!")

# =========================
# CONFIG
# =========================
PAIR = "OANDA:XAU_USD"
WIB = pytz.timezone("Asia/Jakarta")

last_price = None
price_lock = asyncio.Lock()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("RANDOM-BOT")

# =========================
# DELETE WEBHOOK (ANTI CONFLICT)
# =========================
def clear_webhook():
    try:
        requests.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook?drop_pending_updates=true"
        )
        logger.info("Webhook cleared")
    except Exception as e:
        logger.error(f"Webhook error: {e}")

# =========================
# TRADING TIME
# =========================
def is_trading_time():
    now = datetime.now(WIB)
    if now.weekday() >= 5:
        return False
    return 8 <= now.hour < 21

# =========================
# SIGNAL ENGINE (RANDOM + STRUCTURED)
# =========================
async def generate_signal():

    async with price_lock:
        if not last_price:
            return None
        price = last_price

    direction = random.choice(["BUY", "SELL"])

    pip = 0.1

    if direction == "BUY":
        tp1 = price + (70 * pip)
        tp2 = price + (100 * pip)
        sl = price - (40 * pip)
        outlook = "Momentum bullish terdeteksi pada struktur intraday."
    else:
        tp1 = price - (70 * pip)
        tp2 = price - (100 * pip)
        sl = price + (40 * pip)
        outlook = "Tekanan bearish masih mendominasi struktur pasar."

    now = datetime.now(WIB).strftime("%Y-%m-%d %H:%M:%S")

    return f"""
📊 XAUUSD SIGNAL

🕒 Time : {now} WIB
💰 Price : {price}

📈 Direction : {direction}

🎯 TP1 : {round(tp1,2)} (70 pips)
🎯 TP2 : {round(tp2,2)} (100 pips)
⛔ SL  : {round(sl,2)} (40 pips)

📌 Outlook:
{outlook}

━━━━━━━━━━━━━━━
"""

# =========================
# REALTIME PRICE
# =========================
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

# =========================
# HOURLY SIGNAL (STRICT 00 MINUTE WIB)
# =========================
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
                    logger.error(e)
        else:
            logger.info("Market closed")

# =========================
# COMMANDS
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("BOT AKTIF")

async def harga(update: Update, context: ContextTypes.DEFAULT_TYPE):

    async with price_lock:
        if not last_price:
            return await update.message.reply_text("No price")
        price = last_price

    await update.message.reply_text(f"XAUUSD: {price}")

async def signal(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.effective_user.id != AUTHORIZED_USER_ID:
        return

    msg = await generate_signal()

    if msg:
        await update.message.reply_text(msg)

# =========================
# POST INIT SAFE START
# =========================
async def post_init(app):
    app.create_task(price_stream())
    app.create_task(scheduler(app))
    logger.info("Background tasks started")

# =========================
# MAIN
# =========================
def main():

    clear_webhook()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("harga", harga))
    app.add_handler(CommandHandler("signal", signal))

    app.post_init = post_init

    logger.info("BOT STARTED CLEAN MODE")

    app.run_polling()

if __name__ == "__main__":
    main()
