import asyncio
import logging
from datetime import datetime
import pytz

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

from config import *
from data_feed import stream_price, last_price
from smc_engine import detect_smc, build_signal

logging.basicConfig(level=logging.INFO)

WIB = pytz.timezone("Asia/Jakarta")

price_lock = asyncio.Lock()
last_candles_cache = None
last_signal_cache = None


# ================= MARKET DATA (FAKE CANDLE BUILDER SIMPLE) =================
def make_candles(price):
    return [
        {"high": price + 2, "low": price - 2, "close": price}
        for _ in range(10)
    ]


# ================= AI ENGINE LOOP =================
async def engine(app):

    global last_signal_cache, last_candles_cache

    while True:

        async with price_lock:
            price = last_price

        if not price:
            await asyncio.sleep(2)
            continue

        candles = make_candles(price)
        last_candles_cache = candles

        bias, reasons, score = detect_smc(candles)

        if score >= 7:

            signal = build_signal(price, bias, score)
            last_signal_cache = signal

            msg = f"""
🔥 SMC AI SIGNAL READY

📊 Bias: {bias}
💰 Entry: {signal['entry']:.2f}

🎯 TP1: {signal['tp1']:.2f}
🎯 TP2: {signal['tp2']:.2f}
⛔ SL : {signal['sl']:.2f}

🧠 Score: {score}/10

Reason:
- {"\n- ".join(reasons)}
"""

            await app.bot.send_message(CHAT_ID, msg, message_thread_id=THREAD_ID)

        await asyncio.sleep(60)  # 1 menit scan (scalping mode)


# ================= COMMANDS =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 SMC AI BOT ACTIVE")


async def price(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.effective_user.id != AUTHORIZED_USER_ID:
        return

    if not last_price:
        return await update.message.reply_text("No price")

    await update.message.reply_text(f"💰 XAUUSD: {last_price}")


async def signal(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.effective_user.id != AUTHORIZED_USER_ID:
        return

    if not last_signal_cache:
        return await update.message.reply_text("No signal yet")

    s = last_signal_cache

    await update.message.reply_text(f"""
🔥 LAST SIGNAL

Type: {s['direction']}
Entry: {s['entry']:.2f}
TP1: {s['tp1']:.2f}
TP2: {s['tp2']:.2f}
SL: {s['sl']:.2f}
Score: {s['score']}/10
""")


# ================= STARTUP =================
async def post_init(app):
    asyncio.create_task(stream_price(price_lock))
    asyncio.create_task(engine(app))
    print("BOT RUNNING...")


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("price", price))
    app.add_handler(CommandHandler("signal", signal))

    app.post_init = post_init

    app.run_polling()


if __name__ == "__main__":
    main()
