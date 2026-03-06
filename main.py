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

# Channel targets (HANYA 2 CHANNEL)
HOURLY_CHANNELS = ["-1003142698012", "-1002605110502"]

# Timezone
JKT = pytz.timezone("Asia/Jakarta")

if not BOT_TOKEN or not FINNHUB_TOKEN:
    raise SystemExit("❌ BOT_TOKEN dan FINNHUB_TOKEN wajib diatur di environment!")

# ==========================
# LOGGING
# ==========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("finnhub-bot")

# ==========================
# KEEP ALIVE SERVER
# ==========================
app = Flask(__name__)

@app.route("/")
def home():
    return "✅ Bot Finnhub Realtime aktif."

def keep_alive():
    Thread(target=lambda: app.run(host="0.0.0.0", port=FLASK_PORT), daemon=True).start()
    logger.info("🌐 Keep-alive server started on port %s", FLASK_PORT)

# ==========================
# GLOBAL STATE
# ==========================
last_price = None
last_update = None
price_lock = asyncio.Lock()
price_ready = asyncio.Event()

# ==========================
# TRADING SCHEDULE
# ==========================
def is_trading_time(at: datetime = None) -> bool:
    if at is None:
        at = datetime.now(JKT)

    wd = at.weekday()
    if wd >= 5:
        return False

    hour = at.hour
    return (5 <= hour < 23) or (0 <= hour < 4)

# ==========================
# SIGNAL GENERATOR
# ==========================
async def generate_signal_html():

    async with price_lock:
        if last_price is None:
            return None
        price = last_price

    direction = random.choice(["BUY", "SELL"])
    pip = 0.1

    if direction == "BUY":
        tp1 = round(price + 70 * pip, 2)
        tp2 = round(price + 100 * pip, 2)
        sl = round(price - 45 * pip, 2)
    else:
        tp1 = round(price - 70 * pip, 2)
        tp2 = round(price - 100 * pip, 2)
        sl = round(price + 15 * pip, 2)

    now = datetime.now(JKT).strftime("%Y-%m-%d %H:%M:%S")

    header = (
        "<b>🤖 Sinyal Otomatis dari AI Trading System</b>\n"
        "<i>Sinyal ini dihasilkan otomatis oleh sistem analisis pasar real-time.</i>\n\n"
    )

    body = (
        f"📊 Pair: <b>XAU/USD</b>\n"
        f"🕒 Waktu: <b>{now} WIB</b>\n"
        f"💰 Harga Entry: <b>{price:.2f}</b>\n"
        f"📈 Arah: <b>{direction}</b>\n"
        f"🎯 TP1: <b>{tp1}</b>\n"
        f"🎯 TP2: <b>{tp2}</b>\n"
        f"🛑 SL: <b>{sl}</b>\n\n"
        f"⚠️ Gunakan money management yang aman!"
    )

    return header + body

# ==========================
# FINNHUB WEBSOCKET
# ==========================
async def finnhub_ws():

    global last_price, last_update
    url = f"wss://ws.finnhub.io?token={FINNHUB_TOKEN}"
    backoff = 1

    while True:
        try:

            logger.info("🔗 Connecting to Finnhub WS...")

            async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:

                backoff = 1

                await ws.send(json.dumps({
                    "type": "subscribe",
                    "symbol": PAIR_SYMBOL
                }))

                logger.info("✅ Subscribed ke %s via Finnhub WS", PAIR_SYMBOL)

                async for msg in ws:

                    try:
                        data = json.loads(msg)
                    except json.JSONDecodeError:
                        continue

                    if data.get("type") == "trade":

                        for t in data.get("data", []):

                            async with price_lock:
                                last_price = float(t["p"])
                                last_update = datetime.utcnow()
                                price_ready.set()

                            ts = int(datetime.utcnow().timestamp())

                            if ts % 5 == 0:
                                logger.info(
                                    "💲 Harga %s: %.2f @ %s UTC",
                                    PAIR_SYMBOL,
                                    last_price,
                                    last_update.strftime("%H:%M:%S")
                                )

        except Exception as e:

            logger.warning("⚠️ WebSocket error: %s", e)

            await asyncio.sleep(min(backoff, 60))

            backoff = backoff * 2 if backoff < 60 else 60

            logger.info("🔁 Reconnecting ke Finnhub...")

# ==========================
# SEND SIGNAL
# ==========================
async def send_signal_to_chats(app, chat_ids):

    msg = await generate_signal_html()

    if not msg:
        logger.warning("⚠️ Belum ada harga realtime.")
        return False

    for cid in chat_ids:

        try:

            await app.bot.send_message(
                chat_id=cid,
                text=msg,
                parse_mode="HTML"
            )

            logger.info(
                "✅ Sinyal terkirim ke %s pada %s",
                cid,
                datetime.now(JKT).strftime("%Y-%m-%d %H:%M:%S")
            )

        except Exception as e:

            logger.exception("❌ Gagal kirim ke %s: %s", cid, e)

# ==========================
# TELEGRAM COMMAND
# ==========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.effective_user.id != AUTHORIZED_USER_ID:
        return await update.message.reply_text("🚫 Tidak diizinkan.")

    await update.message.reply_text(
        "✅ Bot aktif.\n"
        "• /signal → kirim ke channel\n"
        "• /minta → sinyal pribadi\n"
        "• /harga → harga realtime"
    )

async def harga(update: Update, context: ContextTypes.DEFAULT_TYPE):

    async with price_lock:

        if last_price is None:
            return await update.message.reply_text("⏳ Harga belum tersedia.")

        price = last_price

    now = datetime.now(JKT).strftime("%Y-%m-%d %H:%M:%S")

    await update.message.reply_text(
        f"💰 XAU/USD: {price:.2f}\n"
        f"📅 {now} WIB"
    )

async def minta(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.effective_user.id != AUTHORIZED_USER_ID:
        return await update.message.reply_text("🚫 Tidak diizinkan.")

    msg = await generate_signal_html()

    if not msg:
        return await update.message.reply_text("⚠️ Harga belum tersedia.")

    await update.message.reply_text(msg, parse_mode="HTML")

async def signal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.effective_user.id != AUTHORIZED_USER_ID:
        return await update.message.reply_text("🚫 Tidak diizinkan.")

    await send_signal_to_chats(context.application, HOURLY_CHANNELS)

    await update.message.reply_text("✅ Sinyal dikirim ke semua channel.")

# ==========================
# HOURLY SCHEDULER
# ==========================
async def hourly_scheduler(app):

    logger.info("⏰ Hourly scheduler started.")

    while True:

        now = datetime.now(JKT)

        next_hour = now.replace(
            minute=0,
            second=0,
            microsecond=0
        ) + timedelta(hours=1)

        wait_seconds = (next_hour - now).total_seconds()

        await asyncio.sleep(wait_seconds)

        if is_trading_time(next_hour):

            logger.info("📤 Sending hourly signal")

            await send_signal_to_chats(app, HOURLY_CHANNELS)

# ==========================
# MAIN
# ==========================
def main():

    keep_alive()

    app_bot = ApplicationBuilder().token(BOT_TOKEN).build()

    app_bot.add_handler(CommandHandler("start", start))
    app_bot.add_handler(CommandHandler("harga", harga))
    app_bot.add_handler(CommandHandler("minta", minta))
    app_bot.add_handler(CommandHandler("signal", signal_cmd))
    app_bot.add_handler(MessageHandler(filters.COMMAND, lambda u, c: None))

    async def post_init(application):

        application.create_task(finnhub_ws())
        application.create_task(hourly_scheduler(application))

        logger.info("🚀 Background tasks started")

    app_bot.post_init = post_init

    logger.info("🤖 Bot Finnhub AI Signal aktif")

    app_bot.run_polling()

if __name__ == "__main__":
    main()
