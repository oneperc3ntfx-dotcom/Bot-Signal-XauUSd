#!/usr/bin/env python3
import os
import asyncio
import json
from threading import Thread
from datetime import datetime, timedelta
import pytz
import websockets
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters
import random

# ==========================
# CONFIG
# ==========================
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
AUTHORIZED_USER_ID = int(os.getenv("AUTHORIZED_USER_ID", "0"))
FINNHUB_TOKEN = os.getenv("FINNHUB_TOKEN")
PAIR_SYMBOL = os.getenv("PAIR_SYMBOL", "OANDA:XAU_USD")
FLASK_PORT = int(os.getenv("PORT", "8080"))
JKT = pytz.timezone("Asia/Jakarta")

if not BOT_TOKEN or not CHAT_ID or not FINNHUB_TOKEN:
    raise SystemExit("❌ BOT_TOKEN, CHAT_ID, dan FINNHUB_TOKEN wajib diatur di environment!")

# ==========================
# KEEP ALIVE SERVER (RAILWAY)
# ==========================
app = Flask(__name__)

@app.route("/")
def home():
    return "✅ Bot Finnhub Realtime aktif."

def keep_alive():
    Thread(target=lambda: app.run(host="0.0.0.0", port=FLASK_PORT), daemon=True).start()

# ==========================
# GLOBAL STATE
# ==========================
last_price = None
initial_signal_sent = False

# ==========================
# TRADING SCHEDULE
# ==========================
def is_trading_time():
    now = datetime.now(JKT)
    wd = now.weekday()  # 0=Senin ... 6=Minggu
    if wd >= 5:
        return False  # Sabtu Minggu libur
    hour = now.hour
    return (5 <= hour < 23) or (0 <= hour < 4)  # 05:00 - 04:00 WIB

# ==========================
# RANDOM SIGNAL GENERATOR
# ==========================
async def generate_signal():
    global last_price
    if last_price is None:
        return None

    direction = random.choice(["BUY", "SELL"])
    pip = 0.1  # untuk XAU/USD, 1 pip = 0.1

    if direction == "BUY":
        tp1 = round(last_price + 25 * pip, 2)
        tp2 = round(last_price + 50 * pip, 2)
        sl = round(last_price - 15 * pip, 2)
    else:
        tp1 = round(last_price - 25 * pip, 2)
        tp2 = round(last_price - 50 * pip, 2)
        sl = round(last_price + 15 * pip, 2)

    now = datetime.now(JKT).strftime("%Y-%m-%d %H:%M:%S")
    return (
        f"📊 Pair: XAU/USD\n"
        f"🕒 Time: {now} WIB\n"
        f"💰 Harga Entry: {last_price:.2f}\n"
        f"📈 Arah: {direction}\n"
        f"🎯 TP1: {tp1}\n"
        f"🎯 TP2: {tp2}\n"
        f"🛑 SL: {sl}\n\n"
        f"⚠️ PAKAI MONEY MANAGEMENT SESUAI EQUITAS , JANGAN FULL MARGIN !!"
    )

async def send_random_signal(bot_app):
    msg = await generate_signal()
    if not msg:
        print("⚠️ Belum ada harga realtime.")
        return
    try:
        await bot_app.bot.send_message(chat_id=CHAT_ID, text=msg)
        now = datetime.now(JKT).strftime("%Y-%m-%d %H:%M:%S")
        print(f"✅ Sinyal dikirim ke channel pada {now}")
    except Exception as e:
        print("❌ Gagal kirim sinyal:", e)

# ==========================
# FINNHUB WEBSOCKET
# ==========================
async def finnhub_ws(bot_app):
    global last_price, initial_signal_sent
    url = f"wss://ws.finnhub.io?token={FINNHUB_TOKEN}"

    while True:
        try:
            async with websockets.connect(url, ping_interval=None) as ws:
                await ws.send(json.dumps({"type": "subscribe", "symbol": PAIR_SYMBOL}))
                print(f"✅ Subscribed ke {PAIR_SYMBOL} via Finnhub WS")

                async for msg in ws:
                    data = json.loads(msg)
                    if data.get("type") == "trade":
                        for t in data["data"]:
                            last_price = t["p"]
                            ts = datetime.utcfromtimestamp(t["t"] / 1000).strftime("%H:%M:%S")
                            print(f"💲 Tick {ts} UTC: {last_price}")

                            if not initial_signal_sent and is_trading_time():
                                initial_signal_sent = True
                                await send_random_signal(bot_app)
        except Exception as e:
            print("⚠️ WebSocket error:", e)
            await asyncio.sleep(5)

# ==========================
# SCHEDULER (PER JAM)
# ==========================
async def hourly_signal(bot_app):
    while True:
        now = datetime.now(JKT)
        next_hour = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        wait = (next_hour - now).total_seconds()
        print(f"⏱ Next signal at {next_hour.strftime('%Y-%m-%d %H:%M:%S')} WIB (in {int(wait)}s)")
        await asyncio.sleep(wait)

        if is_trading_time():
            await send_random_signal(bot_app)
        else:
            print("⏸ Di luar jam trading.")

# ==========================
# TELEGRAM COMMANDS
# ==========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != AUTHORIZED_USER_ID:
        return await update.message.reply_text("🚫 Tidak diizinkan.")
    await update.message.reply_text("✅ Bot aktif.\nGunakan /signal (ke channel)\nGunakan /minta (ke pribadi)")

async def signal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != AUTHORIZED_USER_ID:
        return await update.message.reply_text("🚫 Tidak diizinkan.")
    await send_random_signal(context.application)
    await update.message.reply_text("✅ Sinyal dikirim ke channel.")

async def harga(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if last_price is None:
        return await update.message.reply_text("⏳ Harga belum tersedia.")
    now = datetime.now(JKT).strftime("%Y-%m-%d %H:%M:%S")
    await update.message.reply_text(f"💰 Harga XAU/USD: {last_price:.2f}\n🕒 {now} WIB")

# 🔥 Command baru: kirim sinyal hanya ke user pribadi
async def minta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != AUTHORIZED_USER_ID:
        return await update.message.reply_text("🚫 Tidak diizinkan.")
    msg = await generate_signal()
    if not msg:
        return await update.message.reply_text("⚠️ Harga belum tersedia.")
    await update.message.reply_text(msg)

# ==========================
# MAIN
# ==========================
def main():
    keep_alive()
    app_bot = ApplicationBuilder().token(BOT_TOKEN).build()

    app_bot.add_handler(CommandHandler("start", start))
    app_bot.add_handler(CommandHandler("signal", signal))
    app_bot.add_handler(CommandHandler("harga", harga))
    app_bot.add_handler(CommandHandler("minta", minta))
    app_bot.add_handler(MessageHandler(filters.COMMAND, lambda u, c: None))

    async def post_init(app_bot):
        asyncio.create_task(finnhub_ws(app_bot))
        asyncio.create_task(hourly_signal(app_bot))

    app_bot.post_init = post_init
    print("🤖 Bot random signal aktif (Finnhub WebSocket)...")
    app_bot.run_polling()

if __name__ == "__main__":
    main()
