#!/usr/bin/env python3
import os
import asyncio
import logging
import requests
from datetime import datetime, timedelta

import pytz
from dotenv import load_dotenv

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ================= ENV =================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
TWELVE_API = os.getenv("TWELVE_API")  # <-- API kamu
CHAT_ID = int(os.getenv("CHAT_ID", "-1002605110502"))
THREAD_ID = int(os.getenv("THREAD_ID", "1432"))

WIB = pytz.timezone("Asia/Jakarta")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("SMC-AI")

# ================= MARKET DATA =================
def get_candles():
    url = f"https://api.twelvedata.com/time_series?symbol=XAU/USD&interval=5min&outputsize=80&apikey={TWELVE_API}"
    r = requests.get(url).json()

    if "values" not in r:
        return None

    candles = []

    for c in r["values"]:
        candles.append({
            "high": float(c["high"]),
            "low": float(c["low"]),
            "close": float(c["close"]),
            "open": float(c["open"])
        })

    return candles[::-1]  # oldest → newest


# ================= SMC ENGINE REAL =================
def smc_engine(candles):

    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    closes = [c["close"] for c in candles]

    last_close = closes[-1]

    swing_high = max(highs[-20:])
    swing_low = min(lows[-20:])

    score = 5
    bias = "RANGE"
    reason = []

    # ================= LIQUIDITY SWEEP =================
    if highs[-2] > swing_high and last_close < swing_high:
        bias = "BEARISH"
        score += 2
        reason.append("Liquidity sweep HIGH (rejection)")

    if lows[-2] < swing_low and last_close > swing_low:
        bias = "BULLISH"
        score += 2
        reason.append("Liquidity sweep LOW (rejection)")

    # ================= BOS =================
    if last_close > swing_high:
        bias = "BULLISH"
        score += 2
        reason.append("BOS bullish confirmed")

    if last_close < swing_low:
        bias = "BEARISH"
        score += 2
        reason.append("BOS bearish confirmed")

    # ================= ORDER BLOCK SIMPLE =================
    if closes[-3] < closes[-2] > closes[-1]:
        reason.append("Possible bullish order block")

    if closes[-3] > closes[-2] < closes[-1]:
        reason.append("Possible bearish order block")

    return bias, score, reason, last_close


# ================= SIGNAL BUILDER =================
def build_signal(candles):

    bias, score, reason, price = smc_engine(candles)

    if score < 7:
        return None  # filter fake signal

    if bias == "BULLISH":

        entry = price
        tp1 = entry + 8
        tp2 = entry + 18
        sl = entry - 6
        outlook = "BUY LIMIT"

    elif bias == "BEARISH":

        entry = price
        tp1 = entry - 8
        tp2 = entry - 18
        sl = entry + 6
        outlook = "SELL LIMIT"

    else:
        return None

    reason_text = "\n".join([f"- {r}" for r in reason])

    return f"""
📊 XAUUSD SMC AI SIGNAL (M5)

🕒 {datetime.now(WIB).strftime("%Y-%m-%d %H:%M:%S")}

📍 OUTLOOK: {outlook}

💰 Entry Zone: {entry:.2f}
🎯 TP1: {tp1:.2f} (8–10 pts)
🎯 TP2: {tp2:.2f} (15–20 pts)
⛔ SL: {sl:.2f}

📈 Bias: {bias}
⭐ Score: {score}/10

🧠 Reason:
{reason_text}

━━━━━━━━━━━━━━━
⚡ AI SMC FILTER ACTIVE
━━━━━━━━━━━━━━━
"""


# ================= LOOP =================
async def signal_loop(app):

    while True:

        candles = get_candles()

        if candles:

            signal = build_signal(candles)

            if signal:

                await app.bot.send_message(
                    chat_id=CHAT_ID,
                    message_thread_id=THREAD_ID,
                    text=signal
                )

                logger.info("SIGNAL SENT")

        await asyncio.sleep(300)  # 5 menit (M5 sync)


# ================= COMMAND =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("SMC AI BOT ACTIVE")

async def signal(update: Update, context: ContextTypes.DEFAULT_TYPE):

    candles = get_candles()

    signal = build_signal(candles)

    if not signal:
        return await update.message.reply_text("No valid signal (score too low)")

    await update.message.reply_text(signal)


# ================= INIT =================
async def post_init(app):
    asyncio.create_task(signal_loop(app))
    logger.info("SMC AI RUNNING")


# ================= MAIN =================
def main():

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("signal", signal))

    app.post_init = post_init

    app.run_polling()


if __name__ == "__main__":
    main()
