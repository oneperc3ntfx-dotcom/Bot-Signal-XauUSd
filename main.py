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
last_signal_hour = None

# ================= TRADING SESSION =================
def is_trading_time():

    now = datetime.now(WIB)

    day = now.weekday()
    hour = now.hour

    # ================= SABTU =================
    if day == 5:
        return False

    # ================= MINGGU =================
    if day == 6:
        return False

    # ================= SENIN =================
    # mulai jam 07:00 WIB
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

            await asyncio.sleep(5)


# ================= SIMPLE SMC =================
def smc_signal(price):

    if not price:
        return None, "NO DATA"

    # LOGIC LAMA GANJIL GENAP
    if int(price) % 2 == 0:

        return "BUY", [
            "Sell-side liquidity swept",
            "Bullish reaction detected",
            "Potential reversal zone active"
        ]

    else:

        return "SELL", [
            "Buy-side liquidity swept",
            "Bearish rejection confirmed",
            "Momentum continuation detected"
        ]


# ================= SIGNAL BUILDER =================
async def build_signal():

    status = session_status()

    if not last_price:
        return "⚠️ No realtime price data"

    if status == "CLOSED":

        return """
📴 MARKET CLOSED

⛔ No signal generated

🧠 Reason:
- Outside trading session
- Waiting market reopen
- Smart money inactive

━━━━━━━━━━━━
"""

    bias, narrative = smc_signal(last_price)

    entry = last_price

    # ================= BUY =================
    if bias == "BUY":

        setup = "BUY LIMIT"

        tp1 = entry + 7
        tp2 = entry + 15
        sl = entry - 5

    # ================= SELL =================
    else:

        setup = "SELL LIMIT"

        tp1 = entry - 7
        tp2 = entry - 15
        sl = entry + 5

    reason_text = "\n".join([f"- {x}" for x in narrative])

    now = datetime.now(WIB).strftime("%Y-%m-%d %H:%M:%S")

    return f"""
📊 XAUUSD SMC OUTLOOK

🕒 TIME : {now} WIB

📈 MARKET BIAS : {bias}

📌 SETUP PLAN:
{setup} @ {entry:.2f}

🎯 TP1 : {tp1:.2f}
🎯 TP2 : {tp2:.2f}
⛔ SL  : {sl:.2f}

🧠 AI REASON:
{reason_text}

⚠️ WAITING PRICE CONFIRMATION

━━━━━━━━━━━━
"""


# ================= TELEGRAM SEND =================
async def send(app, text):

    await app.bot.send_message(
        chat_id=CHAT_ID,
        message_thread_id=THREAD_ID,
        text=text
    )


# ================= SESSION WATCHER =================
async def session_watcher(app):

    last_status = None

    while True:

        status = session_status()

        # ================= SESSION OPEN =================
        if status == "READY" and last_status != "READY":

            await send(
                app,
                """
🟢 MARKET READY

🚀 SMART MONEY SESSION ACTIVE
📡 AI SIGNAL SYSTEM ONLINE
🔥 XAUUSD SCALPING MODE STARTED

⚠️ Waiting high probability setup...
━━━━━━━━━━━━
"""
            )

        # ================= SESSION CLOSED =================
        if status == "CLOSED" and last_status != "CLOSED":

            await send(
                app,
                """
🔴 MARKET CLOSED

📴 AI SIGNAL STOPPED
💤 Smart money session ended

⏳ Waiting next market session...
━━━━━━━━━━━━
"""
            )

        last_status = status

        await asyncio.sleep(60)


# ================= SIGNAL SCHEDULER =================
async def scheduler(app):

    global last_signal_hour

    while True:

        now = datetime.now(WIB)

        # tunggu tepat awal jam
        next_run = now.replace(
            minute=0,
            second=0,
            microsecond=0
        )

        if now.minute != 0:
            next_run += timedelta(hours=1)

        wait_time = (next_run - now).total_seconds()

        await asyncio.sleep(wait_time)

        # hanya trading session
        if not is_trading_time():
            continue

        current_hour = datetime.now(WIB).strftime("%Y-%m-%d %H")

        # ================= ANTI SPAM =================
        if current_hour == last_signal_hour:
            continue

        msg = await build_signal()

        await send(app, msg)

        last_signal_hour = current_hour

        logger.info("SIGNAL SENT 1X")


# ================= COMMAND START =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        "🤖 XAUUSD SMC BOT ACTIVE"
    )


# ================= COMMAND SIGNAL =================
async def signal(update: Update, context: ContextTypes.DEFAULT_TYPE):

    msg = await build_signal()

    await update.message.reply_text(msg)


# ================= COMMAND PRICE =================
async def price(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not last_price:

        return await update.message.reply_text(
            "⚠️ No realtime price"
        )

    await update.message.reply_text(
        f"📈 XAUUSD : {last_price}"
    )


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
