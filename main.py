#!/usr/bin/env python3
import os
import asyncio
import json
import random
import logging
from threading import Thread
from datetime import datetime, timedelta
import pytz
import websockets
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ==========================
# CONFIG
# ==========================

BOT_TOKEN = os.getenv("7678173969:AAFsD26EC2p4vyeTjxgGVSH3kMi_obIJ3k0")
AUTHORIZED_USER_ID = int(os.getenv("1305881282", "0"))
FINNHUB_TOKEN = os.getenv("d3ndrd9r01qo7510lisgd3ndrd9r01qo7510lit0")

PAIR_SYMBOL = "OANDA:XAU_USD"
FLASK_PORT = int(os.getenv("PORT", "8080"))

# CHANNEL BARU
HOURLY_CHANNELS = ["-1002605110502"]

JKT = pytz.timezone("Asia/Jakarta")

# ==========================
# LOG
# ==========================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("AI-BOT")

# ==========================
# KEEP ALIVE
# ==========================

app = Flask(__name__)

@app.route("/")
def home():
    return "BOT RUNNING"

def keep_alive():
    Thread(target=lambda: app.run(host="0.0.0.0", port=FLASK_PORT)).start()
    logger.info("Keep Alive aktif")

# ==========================
# GLOBAL PRICE
# ==========================

last_price = None
price_lock = asyncio.Lock()

# ==========================
# TRADING TIME
# ==========================

def is_trading_time(now=None):

    if not now:
        now = datetime.now(JKT)

    weekday = now.weekday()

    if weekday >= 5:
        return False

    hour = now.hour

    if 8 <= hour <= 21:
        return True

    return False

# ==========================
# SIGNAL GENERATOR
# ==========================

async def generate_signal_html():

    async with price_lock:

        if last_price is None:
            return None

        price = last_price

    direction = random.choice(["BUY","SELL"])

    pip = 0.1

    if direction == "BUY":

        tp1 = round(price + 70*pip,2)
        tp2 = round(price + 100*pip,2)
        sl = round(price - 45*pip,2)

    else:

        tp1 = round(price - 70*pip,2)
        tp2 = round(price - 100*pip,2)
        sl = round(price + 45*pip,2)

    now = datetime.now(JKT).strftime("%Y-%m-%d %H:%M:%S")

    msg = f"""
🤖 <b>AI MARKET SIGNAL</b>

Instrument : <b>XAU/USD (GOLD)</b>
Time : {now} WIB

Direction : <b>{direction}</b>

Entry Price : <b>{price}</b>

Take Profit
TP1 : {tp1}
TP2 : {tp2}

Stop Loss
SL : {sl}

━━━━━━━━━━━━━━━

⚠️ Gunakan money management yang baik
Risk per trade maksimal 1-3% dari equity
"""

    return msg

# ==========================
# FINNHUB WS
# ==========================

async def finnhub_ws():

    global last_price

    url = f"wss://ws.finnhub.io?token={FINNHUB_TOKEN}"

    while True:

        try:

            logger.info("Connecting Finnhub WS")

            async with websockets.connect(url) as ws:

                await ws.send(json.dumps({
                    "type":"subscribe",
                    "symbol":PAIR_SYMBOL
                }))

                logger.info("Subscribed %s",PAIR_SYMBOL)

                async for msg in ws:

                    data = json.loads(msg)

                    if data.get("type")=="trade":

                        for trade in data["data"]:

                            async with price_lock:
                                last_price = float(trade["p"])

        except Exception as e:

            logger.warning("WS ERROR %s",e)

            await asyncio.sleep(5)

# ==========================
# SEND SIGNAL
# ==========================

async def send_signal(app):

    msg = await generate_signal_html()

    if not msg:
        return

    for ch in HOURLY_CHANNELS:

        try:

            await app.bot.send_message(
                chat_id=ch,
                text=msg,
                parse_mode="HTML"
            )

            logger.info("Signal sent %s",ch)

        except Exception as e:

            logger.error(e)

# ==========================
# COMMAND
# ==========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        "🤖 AI SIGNAL BOT AKTIF\n\n"
        "/harga\n"
        "/minta\n"
        "/signal"
    )

# ==========================

async def harga(update: Update, context: ContextTypes.DEFAULT_TYPE):

    async with price_lock:

        if last_price is None:
            return await update.message.reply_text("Harga belum tersedia")

        price = last_price

    await update.message.reply_text(f"XAUUSD : {price}")

# ==========================

async def minta(update: Update, context: ContextTypes.DEFAULT_TYPE):

    msg = await generate_signal_html()

    if not msg:
        return await update.message.reply_text("Harga belum tersedia")

    await update.message.reply_text(msg,parse_mode="HTML")

# ==========================

async def signal(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.effective_user.id != AUTHORIZED_USER_ID:
        return await update.message.reply_text("Tidak diizinkan")

    await send_signal(context.application)

    await update.message.reply_text("Signal dikirim")

# ==========================
# HOURLY SCHEDULER
# ==========================

async def hourly_scheduler(app):

    logger.info("Scheduler aktif")

    while True:

        now = datetime.now(JKT)

        next_hour = now.replace(minute=0,second=0,microsecond=0) + timedelta(hours=1)

        wait = (next_hour-now).total_seconds()

        await asyncio.sleep(wait)

        if is_trading_time():

            logger.info("Sending hourly signal")

            await send_signal(app)

# ==========================
# MAIN
# ==========================

def main():

    keep_alive()

    bot = ApplicationBuilder().token(BOT_TOKEN).build()

    bot.add_handler(CommandHandler("start",start))
    bot.add_handler(CommandHandler("harga",harga))
    bot.add_handler(CommandHandler("minta",minta))
    bot.add_handler(CommandHandler("signal",signal))

    async def post_init(app):

        app.create_task(finnhub_ws())
        app.create_task(hourly_scheduler(app))

        logger.info("Background task started")

    bot.post_init = post_init

    logger.info("AI GOLD SIGNAL BOT AKTIF")

    bot.run_polling()

if __name__ == "__main__":
    main()
