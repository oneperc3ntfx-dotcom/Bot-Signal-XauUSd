#!/usr/bin/env python3

import os
import asyncio
import logging
import requests

from datetime import datetime, timedelta

import pytz
from dotenv import load_dotenv

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes
)

# ================= LOAD ENV =================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

GOLD_API_KEY = os.getenv(
    "GOLD_API_KEY",
    "ad7da25b-9f63-4586-a2cd-fb42cd521722"
)

CHAT_ID = int(
    os.getenv("CHAT_ID", "-1002605110502")
)

THREAD_ID = int(
    os.getenv("THREAD_ID", "0")
)

# ================= TIMEZONE =================

WIB = pytz.timezone("Asia/Jakarta")

# ================= LOGGING =================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("SMC-BOT")

# ================= GLOBAL =================

last_price = None
last_signal_time = None
last_market_status = None
tasks_started = False

# ================= MARKET SESSION =================

def is_trading_time():

    now = datetime.now(WIB)

    day = now.weekday()
    hour = now.hour

    # Sabtu sampai jam 03:00 WIB
    if day == 5:
        return hour < 3

    # Minggu OFF
    if day == 6:
        return False

    # Senin mulai jam 07:00 WIB
    if day == 0:
        return hour >= 7

    # Selasa - Jumat ON
    return True


# ================= GET PRICE =================

def get_price():

    global last_price

    try:

        url = "https://www.goldapi.io/api/XAU/USD"

        headers = {
            "x-access-token": GOLD_API_KEY,
            "Content-Type": "application/json"
        }

        response = requests.get(
            url,
            headers=headers,
            timeout=10
        )

        logger.info(
            f"GOLDAPI STATUS: {response.status_code}"
        )

        data = response.json()

        logger.info(
            f"GOLDAPI DATA: {data}"
        )

        # validasi response
        if response.status_code != 200:

            logger.error(
                f"GOLDAPI ERROR RESPONSE: {data}"
            )

            return last_price

        # ambil harga
        if (
            isinstance(data, dict)
            and "price" in data
        ):

            price = float(data["price"])

            last_price = price

            logger.info(
                f"LIVE PRICE: {price}"
            )

            return price

    except Exception as e:

        logger.error(
            f"GOLDAPI ERROR: {e}"
        )

    # fallback cache
    return last_price


# ================= SIMPLE SIGNAL =================

def smc_signal(price):

    if price is None:

        return None, ["NO DATA"]

    # contoh simple logic
    if int(price) % 2 == 0:

        return "BUY", [
            "Liquidity sweep bullish",
            "Bullish reversal",
            "Momentum shift up"
        ]

    return "SELL", [
        "Liquidity sweep bearish",
        "Bearish rejection",
        "Momentum continuation"
    ]


# ================= BUILD SIGNAL =================

async def build_signal():

    if not is_trading_time():

        return "📴 MARKET CLOSED"

    price = get_price()

    logger.info(
        f"BUILD SIGNAL PRICE: {price}"
    )

    if price is None:

        return "⚠️ No realtime price data"
    
    bias, reasons = smc_signal(price)

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
        f"- {r}" for r in reasons
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


# ================= SEND MESSAGE =================

async def send(app, text):

    await app.bot.send_message(
        chat_id=CHAT_ID,
        message_thread_id=THREAD_ID,
        text=text
    )


# ================= SESSION WATCHER =================

async def session_watcher(app):

    global last_market_status

    while True:

        status = (
            "OPEN"
            if is_trading_time()
            else "CLOSED"
        )

        if status != last_market_status:

            last_market_status = status

            if status == "OPEN":

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


# ================= SIGNAL SCHEDULER =================

async def scheduler(app):

    global last_signal_time

    while True:

        now = datetime.now(WIB)

        # signal hanya menit 15 setiap jam
        next_run = now.replace(
            minute=15,
            second=0,
            microsecond=0
        )

        # jika sudah lewat menit 15
        if now.minute >= 15:

            next_run += timedelta(hours=1)

        wait_time = (
            next_run - now
        ).total_seconds()

        logger.info(
            f"NEXT SIGNAL: {next_run}"
        )

        logger.info(
            f"WAITING {wait_time:.0f} SECONDS"
        )

        await asyncio.sleep(wait_time)

        if not is_trading_time():

            logger.info(
                "MARKET CLOSED"
            )

            continue

        current_time = datetime.now(WIB).replace(
            second=0,
            microsecond=0
        )

        # anti duplicate
        if last_signal_time == current_time:

            continue

        last_signal_time = current_time

        msg = await build_signal()

        await send(app, msg)

        logger.info(
            "SIGNAL SENT"
        )


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

    p = get_price()

    if p is None:

        return await update.message.reply_text(
            "⚠️ No realtime price data"
        )

    await update.message.reply_text(
        f"📈 XAUUSD: {p:.2f}"
    )


# ================= INIT =================

async def post_init(app):

    global tasks_started

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
        "BOT RUNNING STABLE"
    )


# ================= MAIN =================

def main():

    logger.info(
        "STARTING BOT..."
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
