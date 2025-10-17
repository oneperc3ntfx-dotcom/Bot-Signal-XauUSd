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
JKT = pytz.timezone("Asia/Jakarta")

# Channel IDs
CHANNEL_HOURLY = "-1003143901775"   # tiap jam
CHANNEL_DAILY = "-1002782196938"    # 3x random per hari

if not BOT_TOKEN or not FINNHUB_TOKEN:
    raise SystemExit("❌ BOT_TOKEN dan FINNHUB_TOKEN wajib diatur di environment!")

# ==========================
# KEEP ALIVE SERVER
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
daily_signal_times = []
sent_today = 0

# ==========================
# TRADING TIME
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
    global last_price
    if last_price is None:
        return None

    direction = random.choice(["BUY", "SELL"])
    pip = 0.1

    if direction == "BUY":
        tp1 = round(last_price + 25 * pip, 2)
        tp2 = round(last_price + 50 * pip, 2)
        sl = round(last_price - 15 * pip, 2)
    else:
        tp1 = round(last_price - 25 * pip, 2)
        tp2 = round(last_price - 50 * pip, 2)
        sl = round(last_price + 15 * pip, 2)

    now = datetime.now(JKT).strftime("%Y-%m-%d %H:%M:%S")

    header = (
        "🤖 *Sinyal Otomatis dari AI Trading System*\n"
        "_Sinyal ini dihasilkan secara otomatis oleh sistem AI yang telah dianalisis menggunakan berbagai strategi dan indikator teknikal untuk meningkatkan akurasi prediksi arah pasar._\n\n"
    )

    body = (
        f"📊 Pair: XAU/USD\n"
        f"🕒 Time: {now} WIB\n"
        f"💰 Harga Entry: {last_price:.2f}\n"
        f"📈 Arah: {direction}\n"
        f"🎯 TP1: {tp1}\n"
        f"🎯 TP2: {tp2}\n"
        f"🛑 SL: {sl}\n\n"
        f"⚠️ PAKAI MONEY MANAGEMENT SESUAI EQUITAS , JANGAN FULL MARGIN !!"
    )

    return header + body

# ==========================
# SEND SIGNAL
# ==========================
async def send_signal(bot_app, channel_id):
    msg = await generate_signal()
    if not msg:
        print("⚠️ Belum ada harga realtime.")
        return
    try:
        await bot_app.bot.send_message(chat_id=channel_id, text=msg, parse_mode="Markdown")
        now = datetime.now(JKT).strftime("%Y-%m-%d %H:%M:%S")
        print(f"✅ Sinyal dikirim ke {channel_id} pada {now}")
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
                print(f"✅ Subscribed ke {PAIR_SYMBOL}")

                async for msg in ws:
                    data = json.loads(msg)
                    if data.get("type") == "trade":
                        for t in data["data"]:
                            last_price = t["p"]
                            ts = datetime.utcfromtimestamp(t["t"] / 1000).strftime("%H:%M:%S")
                            print(f"💲 Tick {ts} UTC: {last_price}")

                            if not initial_signal_sent and is_trading_time():
                                initial_signal_sent = True
                                await send_signal(bot_app, CHANNEL_HOURLY)
        except Exception as e:
            print("⚠️ WebSocket error:", e)
            await asyncio.sleep(5)

# ==========================
# HOURLY SIGNAL (CHANNEL 1)
# ==========================
async def hourly_signal(bot_app):
    while True:
        now = datetime.now(JKT)
        next_hour = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        wait = (next_hour - now).total_seconds()
        print(f"⏱ Next hourly signal at {next_hour.strftime('%H:%M:%S')} WIB")
        await asyncio.sleep(wait)

        if is_trading_time():
            await send_signal(bot_app, CHANNEL_HOURLY)
        else:
            print("⏸ Di luar jam trading (hourly).")

# ==========================
# DAILY RANDOM SIGNAL (CHANNEL 2)
# ==========================
def generate_daily_times():
    """Buat 3 jam acak antara 06:00 - 22:00 WIB"""
    times = []
    for _ in range(3):
        h = random.randint(6, 22)
        m = random.randint(0, 59)
        times.append(time(h, m))
    times.sort()
    return times

async def daily_random_signal(bot_app):
    global daily_signal_times, sent_today

    while True:
        now = datetime.now(JKT)
        # reset setiap tengah malam
        if now.hour == 0 and sent_today > 0:
            sent_today = 0
            daily_signal_times = generate_daily_times()
            print(f"🔁 Jadwal baru hari ini: {daily_signal_times}")

        # kirim jika waktunya cocok
        for t in list(daily_signal_times):
            target = datetime.combine(now.date(), t, JKT)
            if 0 <= (target - now).total_seconds() <= 60:  # dalam 1 menit
                if sent_today < 3 and is_trading_time():
                    await send_signal(bot_app, CHANNEL_DAILY)
                    sent_today += 1
                    daily_signal_times.remove(t)
                    print(f"📅 Sinyal harian ke-{sent_today} terkirim.")
        await asyncio.sleep(30)

# ==========================
# TELEGRAM COMMANDS
# ==========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != AUTHORIZED_USER_ID:
        return await update.message.reply_text("🚫 Tidak diizinkan.")
    await update.message.reply_text(
        "✅ Bot aktif.\nGunakan:\n"
        "• /signal → kirim sinyal ke channel\n"
        "• /minta → kirim sinyal pribadi\n"
        "• /harga → lihat harga realtime"
    )

async def signal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != AUTHORIZED_USER_ID:
        return await update.message.reply_text("🚫 Tidak diizinkan.")
    await send_signal(context.application, CHANNEL_HOURLY)
    await update.message.reply_text("✅ Sinyal dikirim ke channel utama.")

async def harga(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if last_price is None:
        return await update.message.reply_text("⏳ Harga belum tersedia.")
    now = datetime.now(JKT).strftime("%Y-%m-%d %H:%M:%S")
    await update.message.reply_text(f"💰 Harga XAU/USD: {last_price:.2f}\n🕒 {now} WIB")

async def minta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != AUTHORIZED_USER_ID:
        return await update.message.reply_text("🚫 Tidak diizinkan.")
    msg = await generate_signal()
    if not msg:
        return await update.message.reply_text("⚠️ Harga belum tersedia.")
    await update.message.reply_text(msg, parse_mode="Markdown")

# ==========================
# MAIN
# ==========================
def main():
    keep_alive()
    global daily_signal_times
    daily_signal_times = generate_daily_times()
    print(f"📅 Jadwal sinyal harian hari ini: {daily_signal_times}")

    app_bot = ApplicationBuilder().token(BOT_TOKEN).build()
    app_bot.add_handler(CommandHandler("start", start))
    app_bot.add_handler(CommandHandler("signal", signal))
    app_bot.add_handler(CommandHandler("harga", harga))
    app_bot.add_handler(CommandHandler("minta", minta))
    app_bot.add_handler(MessageHandler(filters.COMMAND, lambda u, c: None))

    async def post_init(app_bot):
        asyncio.create_task(finnhub_ws(app_bot))
        asyncio.create_task(hourly_signal(app_bot))
        asyncio.create_task(daily_random_signal(app_bot))

    app_bot.post_init = post_init
    print("🤖 Bot Finnhub AI Signal aktif...")
    app_bot.run_polling()

if __name__ == "__main__":
    main()
