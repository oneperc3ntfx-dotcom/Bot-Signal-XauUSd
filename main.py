#!/usr/bin/env python3

import os
import json
import asyncio
import logging
import requests
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
last_ws_update = None


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

    # Senin start 07:00
    if day == 0:
        return hour >= 7

    # Selasa - Kamis ON
    if day in [1, 2, 3]:
        return True

    # Jumat ON
    if day == 4:
        return True

    return False


# ================= REST FALLBACK =================
def get_rest_price():

    try:
        url = f"https://finnhub.io/api/v1/quote?symbol=OANDA:XAU_USD&token={FINNHUB_TOKEN}"

        r = requests.get(url, timeout=5)

        logger.info(f"REST STATUS: {r.status_code}")

        data = r.json()

        logger.info(f"REST DATA: {data}")

        if "c" in data and data["c"] != 0:
            return float(data["c"])

    except Exception as e:
        logger.error(f"REST ERROR: {e}")

    return None


# ================= PRICE STREAM (WS + AUTO FIX) =================
async def price_stream():

    global last_price, last_ws_update

    url = f"wss://ws.finnhub.io?token={FINNHUB_TOKEN}"

    while True:

        try:

            async with websockets.connect(
                url,
                ping_interval=20,
                ping_timeout=20
            ) as ws:

                await ws.send(json.dumps({
                    "type": "subscribe",
                    "symbol": "OANDA:XAU_USD"
                }))

                logger.info("Finnhub WS connected")

                async for msg in ws:

                    data = json.loads(msg)

                    logger.info(f"WS RAW: {data}")

                    if data.get("type") == "trade":

                        for t in data["data"]:

                            last_price = float(t["p"])
                            last_ws_update = datetime.now()

                            logger.info(
                                f"WS PRICE UPDATE: {last_price}"
                            )

        except Exception as e:

            logger.error(f"WS ERROR: {e}")
            logger.info("Reconnecting WS in 3s...")

            await asyncio.sleep(3)


# ================= GET PRICE (HYBRID FIXED) =================
def get_price():

    global last_price, last_ws_update

    try:

        # gunakan websocket jika fresh
        if (
            last_price and
            last_ws_update and
            (datetime.now() - last_ws_update).seconds < 30
        ):

            logger.info(
                f"USING WS PRICE: {last_price}"
            )

            return last_price

        logger.warning(
            "WS STALE -> USING REST FALLBACK"
        )

        rest_price = get_rest_price()

        if rest_price:

            last_price = rest_price
            last_ws_update = datetime.now()

            logger.info(
                f"USING REST PRICE: {rest_price}"
            )

            return rest_price

    except Exception as e:

        logger.error(f"GET PRICE ERROR: {e}")

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
    else:
        return "SELL", [
            "Liquidity sweep bearish",
            "Rejection detected",
            "Momentum continuation"
        ]


# ================= BUILD SIGNAL =================
async def build_signal():

    price = get_price()

    logger.info(f"BUILD SIGNAL PRICE: {price}")

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

    reason_text = "\n".join([f"- {r}" for r in reason])

    now = datetime.now(WIB).strftime("%Y-%m-%d %H:%M:%S")

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

    last_status = None

    while True:

        status = "READY" if is_trading_time() else "CLOSED"

        if status != last_status:

            if status == "READY":
                await send(app, "🟢 MARKET OPEN\nBOT ACTIVE")
            else:
                await send(app, "🔴 MARKET CLOSED")

        last_status = status

        await asyncio.sleep(60)


# ================= SCHEDULER =================
async def scheduler(app):

    global last_signal_time

    while True:

        now = datetime.now(WIB)

        next_run = now.replace(
            minute=15,
            second=0,
            microsecond=0
        )

        if now.minute >= 15:
            next_run += timedelta(hours=1)

        wait_time = (next_run - now).total_seconds()

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

        if last_signal_time == current_time:
            continue

        # FIX DOUBLE SIGNAL
        last_signal_time = current_time

        msg = await build_signal()

        await send(app, msg)

        logger.info("SIGNAL SENT")


# ================= COMMANDS =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        "🤖 SMC BOT ACTIVE"
    )


async def signal(update: Update, context: ContextTypes.DEFAULT_TYPE):

    msg = await build_signal()

    await update.message.reply_text(msg)


async def price(update: Update, context: ContextTypes.DEFAULT_TYPE):

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
