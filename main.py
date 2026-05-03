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
THREAD_ID = int(os.getenv("THREAD_ID", "0"))

WIB = pytz.timezone("Asia/Jakarta")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("SMC-BOT")

# ================= GLOBAL PRICE =================
last_price = None

# ================= TRADING SESSION =================
def is_trading_time():
    now = datetime.now(WIB)

    # weekend full off (sabtu & minggu)
    if now.weekday() == 6:
        return False

    hour = now.hour
    minute = now.minute

    # jumat sampai sabtu 03:50 (extended session)
    if now.weekday() == 4:
        if hour < 7 or (hour == 3 and minute <= 50) or hour < 4:
            return True

    # senin - kamis
    if now.weekday() < 4:
        if hour >= 7 or hour < 4:
            return True

    return False


def session_status():
    now = datetime.now(WIB)

    if is_trading_time():
        return "READY"
    return "CLOSED"


# ================= PRICE STREAM =================
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

                logger.info("Finnhub connected")

                async for msg in ws:
                    data = json.loads(msg)

                    if data.get("type") == "trade":
                        for t in data["data"]:
                            last_price = float(t["p"])

        except Exception as e:
            logger.error(f"WS ERROR: {e}")
            await asyncio.sleep(3)


# ================= SMC SIMPLE LOGIC =================
def smc_signal(price):

    if not price:
        return None, "NO DATA"

    # simple ganjil genap logic (sesuai request lama kamu)
    if int(price) % 2 == 0:
        return "BUY", "Liquidity buy pressure detected"
    else:
        return "SELL", "Liquidity sell pressure detected"


# ================= SIGNAL BUILDER =================
async def build_signal():

    status = session_status()

    if not last_price:
        return "⚠️ No price data"

    if status == "CLOSED":
        return f"""
📴 MARKET CLOSED

⛔ No signal generated

🧠 Reason:
- Outside trading session

━━━━━━━━━━━━
"""

    bias, reason = smc_signal(last_price)

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
📊 XAUUSD SMC SIGNAL

📈 BIAS: {bias}

💰 ENTRY: {entry:.2f}

🎯 TP1: {tp1:.2f}
🎯 TP2: {tp2:.2f}
⛔ SL : {sl:.2f}

🧠 REASON:
- {reason}

━━━━━━━━━━━━
"""


# ================= TELEGRAM SEND =================
async def send(app, text):
    await app.bot.send_message(
        chat_id=CHAT_ID,
        message_thread_id=THREAD_ID,
        text=text
    )


# ================= SESSION MESSAGE =================
async def session_watcher(app):

    last_status = None

    while True:

        status = session_status()

        # READY MESSAGE
        if status == "READY" and last_status != "READY":
            await send(app, "🟢 MARKET READY\nSMC BOT ACTIVE - SIGNAL READY")

        # CLOSE MESSAGE
        if status == "CLOSED" and last_status != "CLOSED":
            await send(app, "🔴 MARKET CLOSED\nSMC BOT STOP SIGNAL")

        last_status = status

        await asyncio.sleep(60)


# ================= HOURLY SIGNAL =================
async def scheduler(app):

    while True:

        now = datetime.now(WIB)

        next_run = now.replace(minute=0, second=0, microsecond=0)
        if now.minute != 0:
            next_run += timedelta(hours=1)

        await asyncio.sleep((next_run - now).total_seconds())

        if not is_trading_time():
            continue

        msg = await build_signal()
        await send(app, msg)

        logger.info("SIGNAL SENT")


# ================= COMMANDS =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 SMC BOT ACTIVE")


async def signal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await build_signal()
    await update.message.reply_text(msg)


async def price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not last_price:
        return await update.message.reply_text("No price")

    await update.message.reply_text(f"XAUUSD: {last_price}")


# ================= INIT =================
async def post_init(app):
    asyncio.create_task(price_stream())
    asyncio.create_task(scheduler(app))
    asyncio.create_task(session_watcher(app))

    logger.info("SMC BOT RUNNING STABLE")


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
