#!/usr/bin/env python3

import os
import asyncio
import logging
import requests

from datetime import datetime, timedelta

import pytz
from dotenv import load_dotenv

from telegram import Update, BotCommand
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes
)

# ================= LOAD ENV =================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

TWELVE_TOKEN = os.getenv(
    "TWELVE_TOKEN",
    "af23649e02da42aab3e78cf343513325"
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

logger = logging.getLogger("XAU-BOT")

# ================= GLOBAL =================

cached_price = None
cached_price_time = None

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

    global cached_price
    global cached_price_time

    now = datetime.now(WIB)

    # gunakan cache jika belum 15 menit
    if (
        cached_price is not None
        and cached_price_time is not None
    ):

        diff = (
            now - cached_price_time
        ).total_seconds()

        # cache 15 menit
        if diff < 900:

            logger.info(
                f"USING CACHED PRICE: {cached_price}"
            )

            return cached_price

    try:

        url = (
            "https://api.twelvedata.com/price"
            "?symbol=XAU/USD"
            f"&apikey={TWELVE_TOKEN}"
        )

        response = requests.get(
            url,
            timeout=10
        )

        logger.info(
            f"TWELVEDATA STATUS: {response.status_code}"
        )

        data = response.json()

        logger.info(
            f"TWELVEDATA DATA: {data}"
        )

        # validasi
        if (
            response.status_code == 200
            and "price" in data
        ):

            price = float(data["price"])

            cached_price = price
            cached_price_time = now

            logger.info(
                f"LIVE PRICE: {price}"
            )

            return price

    except Exception as e:

        logger.error(
            f"PRICE ERROR: {e}"
        )

    return cached_price


# ================= SIGNAL ENGINE =================

def smc_signal(price):

    if price is None:

        return None, ["NO DATA"]

    # contoh logic sederhana
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


# ================= SCHEDULER =================

async def scheduler(app):

    global last_signal_time

    while True:

        now = datetime.now(WIB)

        # signal hanya setiap menit 15
        next_run = now.replace(
            minute=15,
            second=0,
            microsecond=0
        )

        # jika lewat menit 15
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
        "🤖 XAU BOT ACTIVE"
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


# ================= POST INIT =================

async def post_init(app):

    global tasks_started

    if tasks_started:

        return

    tasks_started = True

    # daftar command menu telegram
    await app.bot.set_my_commands([
        BotCommand("start", "Start bot"),
        BotCommand("price", "Check XAUUSD price"),
        BotCommand("signal", "Generate signal")
    ])

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
        CommandHandler("price", price)
    )

    app.add_handler(
        CommandHandler("signal", signal)
    )

    app.post_init = post_init

    app.run_polling(
        drop_pending_updates=True,
        close_loop=False
    )


if __name__ == "__main__":

    main()
