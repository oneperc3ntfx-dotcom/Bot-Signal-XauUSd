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
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

# ==========================
# CONFIG
# ==========================
BOT_TOKEN = os.getenv("BOT_TOKEN")
AUTHORIZED_USER_ID = int(os.getenv("AUTHORIZED_USER_ID", "0"))
FINNHUB_TOKEN = os.getenv("FINNHUB_TOKEN")
PAIR_SYMBOL = os.getenv("PAIR_SYMBOL", "OANDA:XAU_USD")
FLASK_PORT = int(os.getenv("PORT", "8080"))

# CHANNEL TARGET
HOURLY_CHANNELS = ["-1003142698012", "-1002605110502"]

# TIMEZONE
JKT = pytz.timezone("Asia/Jakarta")

if not BOT_TOKEN or not FINNHUB_TOKEN:
    raise SystemExit("❌ BOT_TOKEN dan FINNHUB_TOKEN wajib diatur!")

# ==========================
# LOGGING
# ==========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger("AI-BOT")

# ==========================
# KEEP ALIVE SERVER
# ==========================
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot aktif"

def keep_alive():
    Thread(target=lambda: app.run(host="0.0.0.0", port=FLASK_PORT), daemon=True).start()
    logger.info("Keep Alive aktif")

# ==========================
# GLOBAL STATE
# ==========================
last_price = None
price_lock = asyncio.Lock()

# ==========================
# TRADING SCHEDULE
# SENIN 07:00 → SABTU 05:00
# ==========================
def is_trading_time(at=None):

    if at is None:
        at = datetime.now(JKT)

    wd = at.weekday()
    hour = at.hour

    # Senin - Kamis
    if wd in [0,1,2,3]:
        return (7 <= hour <= 23) or (0 <= hour <= 5)

    # Jumat
    if wd == 4:
        return (7 <= hour <= 23)

    # Sabtu
    if wd == 5:
        return (0 <= hour <= 5)

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
        tp1 = round(price + 7,2)
        tp2 = round(price + 10,2)
        sl = round(price - 4.5,2)
    else:
        tp1 = round(price - 7,2)
        tp2 = round(price - 10,2)
        sl = round(price + 4.5,2)

    now = datetime.now(JKT).strftime("%Y-%m-%d %H:%M")

    msg = f"""
<b>🤖 AI GOLD SIGNAL</b>

📊 Pair : <b>XAU/USD</b>
🕒 Time : <b>{now} WIB</b>

💰 Entry : <b>{price}</b>
📈 Signal : <b>{direction}</b>

🎯 TP1 : <b>{tp1}</b>
🎯 TP2 : <b>{tp2}</b>

🛑 SL : <b>{sl}</b>

⚠️ Gunakan Money Management
"""

    return msg

# ==========================
# FINNHUB WEBSOCKET
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

                    if data.get("type") == "trade":

                        for t in data["data"]:

                            async with price_lock:
                                last_price = float(t["p"])

        except Exception as e:

            logger.warning("WS ERROR %s",e)
            await asyncio.sleep(5)

# ==========================
# SEND SIGNAL
# ==========================
async def send_signal(app, chats):

    msg = await generate_signal_html()

    if not msg:
        logger.warning("Harga belum ada")
        return

    for chat in chats:

        try:

            await app.bot.send_message(
                chat_id=chat,
                text=msg,
                parse_mode="HTML"
            )

            logger.info("Signal terkirim ke %s",chat)

        except Exception as e:

            logger.error("Gagal kirim %s",e)

# ==========================
# TELEGRAM COMMAND
# ==========================
async def start(update:Update,context:ContextTypes.DEFAULT_TYPE):

    if update.effective_user.id != AUTHORIZED_USER_ID:
        return

    await update.message.reply_text(
        "Bot aktif\n\n"
        "/signal kirim signal\n"
        "/harga cek harga"
    )

async def harga(update:Update,context:ContextTypes.DEFAULT_TYPE):

    async with price_lock:

        if last_price is None:
            return await update.message.reply_text("Harga belum ada")

        price = last_price

    await update.message.reply_text(f"XAUUSD : {price}")

async def signal(update:Update,context:ContextTypes.DEFAULT_TYPE):

    if update.effective_user.id != AUTHORIZED_USER_ID:
        return

    await send_signal(context.application,HOURLY_CHANNELS)

    await update.message.reply_text("Signal terkirim")

# ==========================
# HOURLY SCHEDULER
# ==========================
async def hourly_scheduler(app):

    logger.info("Scheduler aktif")

    while True:

        now = datetime.now(JKT)

        next_hour = now.replace(
            minute=0,
            second=0,
            microsecond=0
        ) + timedelta(hours=1)

        wait = (next_hour - now).total_seconds()

        await asyncio.sleep(wait)

        if is_trading_time(next_hour):

            logger.info("Kirim signal hourly")

            await send_signal(app,HOURLY_CHANNELS)

# ==========================
# MAIN
# ==========================
def main():

    keep_alive()

    bot = ApplicationBuilder().token(BOT_TOKEN).build()

    bot.add_handler(CommandHandler("start",start))
    bot.add_handler(CommandHandler("harga",harga))
    bot.add_handler(CommandHandler("signal",signal))
    bot.add_handler(MessageHandler(filters.COMMAND,lambda u,c:None))

    async def post_init(app):

        app.create_task(finnhub_ws())
        app.create_task(hourly_scheduler(app))

        logger.info("Background task started")

    bot.post_init = post_init

    logger.info("BOT AI SIGNAL AKTIF")

    bot.run_polling()

if __name__ == "__main__":
    main()
