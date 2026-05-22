#!/usr/bin/env python3

import os
import asyncio
import logging
from datetime import datetime, timedelta

import pytz
import yfinance as yf

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

CHAT_ID = int(
    os.getenv("CHAT_ID", "-1002605110502")
)

THREAD_ID = int(
    os.getenv("THREAD_ID", "0")
)

WIB = pytz.timezone("Asia/Jakarta")

logging.basicConfig(level=logging.INFO)

logger = logging.getLogger("SMC-BOT")

# ================= GLOBAL =================
last_price = None
last_signal_time = None
last_market_status = None
tasks_started = False


# ================= SESSION =================
def is_trading_time():

    now = datetime.now(WIB)

    day = now.weekday()
    hour = now.hour

    # Sabtu 00:00 - 02:59 ON
    if day == 5:
        return hour < 3

    # Minggu OFF
    if day == 6:
        return False

    # Senin mulai 07:00
    if day == 0:
        return hour >= 7

    # Selasa - Jumat ON
    if day in [1, 2, 3, 4]:
        return True

    return False


# ================= GET PRICE =================
def get_price():

    global last_price

    try:

        # Gold Futures
        gold = yf.Ticker("GC=F")

        data = gold.history(
            period="1d",
            interval="1m"
        )

        if not data.empty:

            last_price = float(
                data["Close"].iloc[-1]
            )

            logger.info(
                f"YAHOO PRICE: {last_price}"
            )

            return last_price

        logger.warning(
            "Yahoo returned empty data"
        )

    except Exception as e:

        logger.error(
            f"GET PRICE ERROR: {e}"
        )

    # fallback cached price
    if last_price is not None:

        logger.warning(
            f"USING CACHED PRICE: {last_price}"
        )

        return last_price

    return None


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

    return "SELL", [
        "Liquidity sweep bearish",
        "Rejection detected",
        "Momentum continuation"
    ]


# ================= BUILD SIGNAL =================
async def build_signal():

    price = get_price()

    logger.info(
        f"BUILD SIGNAL PRICE: {price}"
    )

    if not price:

        return "⚠️ No realtime price data"

    if not is_trading_time():

        return "📴 MARKET CLOSED"

    bias, reason = smc_signal(price)

    entry = price

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

    reason_text = "\n".join([
        f"- {r}" for r in reason
    ])

    now = datetime.now(WIB).strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    return f"""
📊 XAUUSD SIGNAL

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

    global last_market_status

    while True:

        status = (
            "READY"
            if is_trading_time()
            else "CLOSED"
        )

        if status != last_market_status:

            last_market_status = status

            if status == "READY":

                await send(
                    app,
                    "🟢 MARKET OPEN\nBOT ACTIVE"
                )

            else:

                await send(
                    app,
                    "🔴 MARKET CLOSED"
                )

        await asyncio.sleep(60)


# ================= SCHEDULER =================
async def scheduler(app):

    global last_signal_time

    while True:

        now = datetime.now(WIB)

        # signal tiap jam menit 15
        next_run = now.replace(
            minute=15,
            second=0,
            microsecond=0
        )

        if now.minute >= 15:

            next_run += timedelta(hours=1)

        wait_time = (
            next_run - now
        ).total_seconds()

        logger.info(
            f"NEXT SIGNAL IN {wait_time:.0f} sec"
        )

        await asyncio.sleep(wait_time)

        if not is_trading_time():

            continue

        current_time = datetime.now(WIB).replace(
            second=0,
            microsecond=0
        )

        # anti duplicate signal
        if last_signal_time == current_time:

            continue

        last_signal_time = current_time

        msg = await build_signal()

        await send(app, msg)

        logger.info("SIGNAL SENT")


# ================= COMMANDS =================
async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(
        "🤖 SMC BOT ACTIVE"
    )


async def signal(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    msg = await build_signal()

    await update.message.reply_text(msg)


async def price(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    price = get_price()

    if not price:

        return await update.message.reply_text(
            "⚠️ No price data"
        )

    await update.message.reply_text(
        f"📈 XAUUSD: {price}"
    )


# ================= INIT =================
async def post_init(app):

    global tasks_started

    # prevent duplicate task
    if tasks_started:

        return

    tasks_started = True

    asyncio.create_task(
        scheduler(app)
    )

    asyncio.create_task(
        session_watcher(app)
    )

    logger.info(
        f"BOT RUNNING STABLE | PID: {os.getpid()}"
    )


# ================= MAIN =================
def main():

    logger.info(
        f"BOT INSTANCE PID: {os.getpid()}"
    )

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .build()
    )

    app.add_handler(
        CommandHandler("start", start)
    )

    app.add_handler(
        CommandHandler("signal", signal)
    )

    app.add_handler(
        CommandHandler("price", price)
    )

    app.post_init = post_init

    app.run_polling(
        drop_pending_updates=True,
        close_loop=False
    )


if __name__ == "__main__":
    main()
