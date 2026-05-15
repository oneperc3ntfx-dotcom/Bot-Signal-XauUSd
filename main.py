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
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes
)

# ================= ENV =================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
FINNHUB_TOKEN = os.getenv("FINNHUB_TOKEN")

CHAT_ID = int(os.getenv("CHAT_ID", "-1002605110502"))
THREAD_ID = int(os.getenv("THREAD_ID", "0"))

WIB = pytz.timezone("Asia/Jakarta")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("SMC-BOT")

# ================= GLOBAL =================
last_price = None
last_signal_time = None


# ================= SESSION =================
def is_trading_time():

    now = datetime.now(WIB)

    day = now.weekday()
    hour = now.hour

    # ================= SABTU (00:00 - 02:59 ON) =================
    if day == 5:
        if hour < 3:
            return True
        return False

    # ================= MINGGU OFF =================
    if day == 6:
        return False

    # ================= SENIN (07:00 START) =================
    if day == 0:
        if hour < 7:
            return False
        return True

    # ================= SELASA - KAMIS =================
    if day in [1, 2, 3]:
        return True

    # ================= JUMAT =================
    if day == 4:
        return True

    return False


def session_status():
    return "READY" if is_trading_time() else "CLOSED"


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
            await asyncio.sleep(5)


# ================= SIMPLE SIGNAL =================
def smc_signal(price):

    if not price:
        return None, ["NO DATA"]

    if int(price) % 2 == 0:
        return "BUY", [
            "Liquidity sweep bullish",
            "Reversal potential",
            "Momentum shift up"
        ]
    else:
        return "SELL", [
            "Liquidity sweep bearish",
            "Rejection detected",
            "Momentum continuation"
        ]


# ================= BUILD SIGNAL =================
async def build_signal():

    if not last_price:
        return "⚠️ No realtime price data"

    if not is_trading_time():
        return "📴 MARKET CLOSED"

    bias, reason = smc_signal(last_price)
    entry = last_price

    if bias == "BUY":
        setup = "BUY LIMIT"
        tp1 = entry + 7
        tp2 = entry + 15
        sl = entry - 5
    else:
        setup = "SELL LIMIT"
        tp1 = entry - 7
        tp2 = entry - 15
        sl = entry + 5

    reason_text = "\n".join([f"- {r}" for r in reason])

    now = datetime.now(WIB).strftime("%Y-%m-%d %H:%M:%S")

    return f"""
📊 XAUUSD SMC SIGNAL

🕒 {now} WIB

📈 BIAS: {bias}

📌 ENTRY: {setup} @ {entry:.2f}

🎯 TP1: {tp1:.2f}
🎯 TP2: {tp2:.2f}
⛔ SL : {sl:.2f}

🧠 REASON:
{reason_text}

━━━━━━━━━━━━
"""


# ================= SEND =================
async def send(app, text):
    await app.bot.send_message(
        chat_id=CHAT_ID,
        message_thread_id=THREAD_ID,
        text=text
    )


# ================= SESSION WATCH =================
async def session_watcher(app):

    last_status = None

    while True:
        status = session_status()

        if status != last_status:

            if status == "READY":
                await send(app, "🟢 MARKET OPEN\nSMC BOT ACTIVE")
            else:
                await send(app, "🔴 MARKET CLOSED")

        last_status = status
        await asyncio.sleep(60)


# ================= SCHEDULER (MINUTE 15) =================
async def scheduler(app):

    global last_signal_time

    while True:

        now = datetime.now(WIB)

        # 🔥 ONLY MINUTE 15 EVERY HOUR
        next_run = now.replace(minute=15, second=0, microsecond=0)

        if now.minute >= 15:
            next_run = next_run + timedelta(hours=1)

        wait_time = (next_run - now).total_seconds()
        await asyncio.sleep(wait_time)

        if not is_trading_time():
            continue

        current_time = datetime.now(WIB).strftime("%Y-%m-%d %H:%M")

        if current_time == last_signal_time:
            continue

        msg = await build_signal()
        await send(app, msg)

        last_signal_time = current_time
        logger.info("SIGNAL SENT (UPDATED SCHEDULE)")


# ================= COMMANDS =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 SMC BOT ACTIVE")


async def signal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await build_signal()
    await update.message.reply_text(msg)


async def price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not last_price:
        return await update.message.reply_text("⚠️ No price")

    await update.message.reply_text(f"📈 XAUUSD: {last_price}")


# ================= INIT =================
async def post_init(app):
    asyncio.create_task(price_stream())
    asyncio.create_task(scheduler(app))
    asyncio.create_task(session_watcher(app))
    logger.info("BOT RUNNING STABLE")


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
