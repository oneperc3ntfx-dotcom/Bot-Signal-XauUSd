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

# ====================
# CONFIG
# ====================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
AUTHORIZED_USER_ID = int(os.environ.get("AUTHORIZED_USER_ID", "0"))
POLYGON_API_KEY = os.environ.get("POLYGON_API_KEY")
PAIR_SYMBOL = "C:XAUUSD"  # Pair untuk Polygon Forex format
FLASK_PORT = int(os.environ.get("PORT", "8080"))
JKT = pytz.timezone("Asia/Jakarta")

if not BOT_TOKEN or not CHAT_ID or not POLYGON_API_KEY:
    raise SystemExit("❌ ERROR: BOT_TOKEN, CHAT_ID, dan POLYGON_API_KEY harus diatur di environment")

# ====================
# KEEP ALIVE (Flask)
# ====================
app = Flask(__name__)

@app.route("/")
def home():
    return "✅ Bot is running (Polygon.io WebSocket)."

def keep_alive():
    Thread(target=lambda: app.run(host="0.0.0.0", port=FLASK_PORT), daemon=True).start()

# ====================
# GLOBALS
# ====================
last_price = None
initial_signal_sent = False

# ====================
# TIME FILTER
# ====================
def is_working_time(now_jkt):
    wd = now_jkt.weekday()
    if wd >= 5:  # Sabtu/Minggu off
        return False
    hour = now_jkt.hour
    return (hour >= 6 and hour < 4 + 24)  # aktif 06:00–04:00 WIB

# ====================
# SIGNAL GENERATOR
# ====================
async def send_random_signal(app_bot):
    global last_price
    if last_price is None:
        print("⚠️ Belum ada harga realtime untuk kirim sinyal.")
        return

    arah = "BUY" if (os.urandom(1)[0] % 2 == 0) else "SELL"
    pip = 0.1  # 1 pip = 0.1 untuk XAUUSD

    if arah == "BUY":
        tp1 = round(last_price + 25 * pip, 2)
        tp2 = round(last_price + 50 * pip, 2)
        sl = round(last_price - 15 * pip, 2)
    else:
        tp1 = round(last_price - 25 * pip, 2)
        tp2 = round(last_price - 50 * pip, 2)
        sl = round(last_price + 15 * pip, 2)

    now = datetime.now(JKT).strftime("%Y-%m-%d %H:%M:%S")
    msg = (
        f"📊 Pair: XAU/USD\n"
        f"🕒 Time: {now} WIB\n"
        f"💰 Harga Entry: {last_price:.2f}\n"
        f"📈 Arah: {arah}\n"
        f"🎯 TP1: {tp1}\n"
        f"🎯 TP2: {tp2}\n"
        f"🛑 SL: {sl}\n\n"
        f"⚠️ GUNAKAN MONEY MANAGEMENT SESUAI EQUITAS, JANGAN FULL MARGIN!!"
    )

    try:
        await app_bot.bot.send_message(chat_id=CHAT_ID, text=msg)
        print(f"✅ Sinyal {arah} dikirim pada {now}")
    except Exception as e:
        print("❌ Gagal kirim sinyal:", e)

# ====================
# POLYGON.IO WEBSOCKET
# ====================
async def polygon_ws(app_bot):
    global last_price, initial_signal_sent
    url = "wss://socket.polygon.io/forex"
    headers = {"User-Agent": "SignalBot/1.0"}
    auth_msg = json.dumps({"action": "auth", "params": POLYGON_API_KEY})
    sub_msg = json.dumps({"action": "subscribe", "params": PAIR_SYMBOL})

    while True:
        try:
            async with websockets.connect(url, extra_headers=headers, ping_interval=20, ping_timeout=10) as ws:
                await ws.send(auth_msg)
                await ws.send(sub_msg)
                print(f"✅ Terhubung ke Polygon.io WebSocket dan subscribe {PAIR_SYMBOL}")

                async for message in ws:
                    data = json.loads(message)
                    if isinstance(data, list):
                        for d in data:
                            if d.get("p"):  # price
                                last_price = float(d["p"])
                                ts = datetime.utcfromtimestamp(d["t"] / 1000).replace(tzinfo=pytz.utc)
                                print(f"💲 Tick {ts.strftime('%Y-%m-%d %H:%M:%S')} UTC: {last_price}")

                                if not initial_signal_sent:
                                    initial_signal_sent = True
                                    await send_random_signal(app_bot)
                    elif "status" in data:
                        print("ℹ️", data)
        except Exception as e:
            print("⚠️ WebSocket error:", e)
            await asyncio.sleep(5)

# ====================
# SCHEDULER (SETIAP JAM)
# ====================
async def schedule_task(app_bot):
    while True:
        now = datetime.now(JKT)
        next_run = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        wait = (next_run - now).total_seconds()
        print(f"⏱ Next scheduled signal at {next_run.strftime('%Y-%m-%d %H:%M:%S')} WIB (in {int(wait)}s)")
        await asyncio.sleep(wait)

        if not is_working_time(datetime.now(JKT)):
            print("⏸ Di luar jam kerja trading.")
            continue

        await send_random_signal(app_bot)

# ====================
# TELEGRAM COMMANDS
# ====================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if u and u.id == AUTHORIZED_USER_ID:
        await update.message.reply_text("✅ Bot aktif.\nGunakan /signal untuk kirim sinyal manual.")
    else:
        await update.message.reply_text("🚫 Tidak diizinkan.")

async def signal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not u or u.id != AUTHORIZED_USER_ID:
        await update.message.reply_text("🚫 Tidak diizinkan.")
        return
    await update.message.reply_text("📡 Mengirim sinyal manual...")
    await send_random_signal(context.application)
    await update.message.reply_text("✅ Sinyal manual terkirim.")

async def harga_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if last_price is None:
        await update.message.reply_text("⏳ Harga realtime belum tersedia.")
    else:
        now = datetime.now(JKT).strftime("%Y-%m-%d %H:%M:%S")
        await update.message.reply_text(f"💰 Harga realtime XAU/USD: {last_price:.2f}\n🕒 {now} WIB")

# ====================
# MAIN
# ====================
def main():
    keep_alive()
    app_bot = ApplicationBuilder().token(BOT_TOKEN).build()
    app_bot.add_handler(CommandHandler("start", start_cmd))
    app_bot.add_handler(CommandHandler("signal", signal_cmd))
    app_bot.add_handler(CommandHandler("harga", harga_cmd))
    app_bot.add_handler(MessageHandler(filters.COMMAND, lambda u, c: None))

    async def start_tasks(app_bot):
        asyncio.create_task(polygon_ws(app_bot))
        asyncio.create_task(schedule_task(app_bot))

    app_bot.post_init = start_tasks
    print("🤖 Bot random signal aktif (Polygon.io realtime)...")
    app_bot.run_polling()

if __name__ == "__main__":
    main()
