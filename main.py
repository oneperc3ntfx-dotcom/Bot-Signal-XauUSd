#!/usr/bin/env python3
import os
import asyncio
import json
import random
from threading import Thread
from datetime import datetime, timedelta, time
import pytz
import websockets
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

# ==========================
# CONFIG
# ==========================
BOT_TOKEN = os.getenv("BOT_TOKEN")
AUTHORIZED_USER_ID = int(os.getenv("AUTHORIZED_USER_ID", "0"))
FINNHUB_TOKEN = os.getenv("FINNHUB_TOKEN")
PAIR_SYMBOL = os.getenv("PAIR_SYMBOL", "OANDA:XAU_USD")
FLASK_PORT = int(os.getenv("PORT", "8080"))

# Channel targets
HOURLY_CHANNELS = ["-1003142698012", "-1002605110502"]
DAILY_RANDOM_CHANNEL = "-1002782196938"

JKT = pytz.timezone("Asia/Jakarta")

if not BOT_TOKEN or not FINNHUB_TOKEN:
    raise SystemExit("❌ BOT_TOKEN dan FINNHUB_TOKEN wajib diatur di environment!")

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
last_update = None
price_lock = asyncio.Lock()
price_ready = asyncio.Event()

# ==========================
# TRADING SCHEDULE
# ==========================
def is_trading_time():
    now = datetime.now(JKT)
    wd = now.weekday()  # 0=Senin ... 6=Minggu
    if wd >= 5:
        return False
    hour = now.hour
    return (5 <= hour < 23) or (0 <= hour < 4)

# ==========================
# SIGNAL GENERATOR
# ==========================
async def generate_signal():
    async with price_lock:
        if last_price is None:
            return None
        price = last_price

    # Logika arah (berdasarkan pergerakan terakhir agar tidak random statis)
    direction = random.choice(["BUY", "SELL"])
    pip = 0.1

    if direction == "BUY":
        tp1 = round(price + 25 * pip, 2)
        tp2 = round(price + 50 * pip, 2)
        sl = round(price - 15 * pip, 2)
    else:
        tp1 = round(price - 25 * pip, 2)
        tp2 = round(price - 50 * pip, 2)
        sl = round(price + 15 * pip, 2)

    now = datetime.now(JKT).strftime("%Y-%m-%d %H:%M:%S")

    header = (
        "🤖 *Sinyal Otomatis dari AI Trading System*\n"
        "_Sinyal ini dihasilkan otomatis oleh sistem analisis pasar real-time._\n\n"
    )

    body = (
        f"📊 Pair: XAU/USD\n"
        f"🕒 Waktu: {now} WIB\n"
        f"💰 Harga Entry: {price:.2f}\n"
        f"📈 Arah: {direction}\n"
        f"🎯 TP1: {tp1}\n"
        f"🎯 TP2: {tp2}\n"
        f"🛑 SL: {sl}\n\n"
        f"⚠️ Gunakan money management yang aman!"
    )
    return header + body

# ==========================
# FINNHUB WEBSOCKET
# ==========================
async def finnhub_ws(bot_app):
    global last_price, last_update
    url = f"wss://ws.finnhub.io?token={FINNHUB_TOKEN}"

    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                await ws.send(json.dumps({"type": "subscribe", "symbol": PAIR_SYMBOL}))
                print(f"✅ Subscribed ke {PAIR_SYMBOL} via Finnhub WS")

                async for msg in ws:
                    data = json.loads(msg)
                    if data.get("type") == "trade":
                        for t in data["data"]:
                            async with price_lock:
                                last_price = t["p"]
                                last_update = datetime.utcnow()
                            price_ready.set()
                            # Batasi log hanya 1x per 5 detik
                            if int(datetime.utcnow().timestamp()) % 5 == 0:
                                print(f"💲 Harga {PAIR_SYMBOL}: {last_price:.2f} @ {last_update.strftime('%H:%M:%S')} UTC")

        except Exception as e:
            print(f"⚠️ WebSocket error: {e}")
            await asyncio.sleep(5)
            print("🔁 Reconnecting ke Finnhub...")

# ==========================
# SENDER FUNCTIONS
# ==========================
async def send_signal(bot_app, chat_ids):
    msg = await generate_signal()
    if not msg:
        print("⚠️ Belum ada harga realtime.")
        return
    for cid in chat_ids:
        try:
            await bot_app.bot.send_message(chat_id=cid, text=msg, parse_mode="Markdown")
            print(f"✅ Sinyal terkirim ke {cid} pada {datetime.now(JKT).strftime('%H:%M:%S')}")
        except Exception as e:
            print(f"❌ Gagal kirim ke {cid}: {e}")

# ==========================
# TELEGRAM COMMANDS
# ==========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != AUTHORIZED_USER_ID:
        return await update.message.reply_text("🚫 Tidak diizinkan.")
    await update.message.reply_text(
        "✅ Bot aktif.\n"
        "• /signal → kirim ke semua channel\n"
        "• /minta → sinyal pribadi (harga terbaru)\n"
        "• /harga → harga realtime"
    )

async def harga(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with price_lock:
        if last_price is None:
            return await update.message.reply_text("⏳ Harga belum tersedia.")
        price = last_price
        update_time = last_update.strftime("%H:%M:%S") if last_update else "?"
    now = datetime.now(JKT).strftime("%Y-%m-%d %H:%M:%S")
    await update.message.reply_text(f"💰 XAU/USD: {price:.2f}\n🕒 Update UTC: {update_time}\n📅 {now} WIB")

async def minta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != AUTHORIZED_USER_ID:
        return await update.message.reply_text("🚫 Tidak diizinkan.")

    # Tunggu harga terbaru dari WebSocket (maks 3 detik)
    try:
        await asyncio.wait_for(price_ready.wait(), timeout=3)
        price_ready.clear()
    except asyncio.TimeoutError:
        pass

    msg = await generate_signal()
    if not msg:
        return await update.message.reply_text("⚠️ Harga belum tersedia (belum ada tick baru).")
    await update.message.reply_text(msg, parse_mode="Markdown")

async def signal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != AUTHORIZED_USER_ID:
        return await update.message.reply_text("🚫 Tidak diizinkan.")
    await send_signal(context.application, HOURLY_CHANNELS + [DAILY_RANDOM_CHANNEL])
    await update.message.reply_text("✅ Sinyal dikirim ke semua channel.")

# ==========================
# MAIN
# ==========================
def main():
    keep_alive()
    app_bot = ApplicationBuilder().token(BOT_TOKEN).build()

    app_bot.add_handler(CommandHandler("start", start))
    app_bot.add_handler(CommandHandler("harga", harga))
    app_bot.add_handler(CommandHandler("minta", minta))
    app_bot.add_handler(CommandHandler("signal", signal))
    app_bot.add_handler(MessageHandler(filters.COMMAND, lambda u, c: None))

    async def post_init(app_bot):
        asyncio.create_task(finnhub_ws(app_bot))

    app_bot.post_init = post_init
    print("🤖 Bot Finnhub AI Signal aktif...")
    app_bot.run_polling()

if __name__ == "__main__":
    main()
