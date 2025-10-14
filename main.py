#!/usr/bin/env python3
import os
import asyncio
import json
import requests
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
POLYGON_KEY = os.environ.get("POLYGON_KEY")
PAIR_SYMBOL = "XAU/USD"  # untuk tampilan
POLYGON_TICKER = "C:XAUUSD"  # untuk Polygon WebSocket
FLASK_PORT = int(os.environ.get("PORT", "8080"))
JKT = pytz.timezone("Asia/Jakarta")

if not BOT_TOKEN or not CHAT_ID or not POLYGON_KEY:
    raise SystemExit("❌ ERROR: BOT_TOKEN, CHAT_ID, dan POLYGON_KEY harus di-set di environment")

# ====================
# Flask Keep Alive
# ====================
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is running OK ✅"

def keep_alive():
    Thread(target=lambda: app.run(host="0.0.0.0", port=FLASK_PORT), daemon=True).start()

# ====================
# GLOBAL VARIABEL
# ====================
last_price = None
initial_signal_sent = False

# ====================
# FUNGSI UTIL
# ====================
def is_working_time(now_jkt):
    """Jam kerja: Senin–Jumat, 06:00–04:00 WIB"""
    wd = now_jkt.weekday()
    if wd >= 5:  # Sabtu, Minggu
        return False
    hour = now_jkt.hour
    return (hour >= 6) or (hour < 4)

async def send_random_signal(app_bot):
    """Kirim sinyal random BUY/SELL dengan TP1, TP2, SL"""
    global last_price
    if last_price is None:
        print("⚠️ Belum ada harga realtime.")
        return

    arah = "BUY" if (os.urandom(1)[0] % 2 == 0) else "SELL"
    pip = 0.1  # 1 pip = 0.1 untuk XAU/USD

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
        "⚠️ GUNAKAN MONEY MANAGEMENT SESUAI EQUITAS, JANGAN FULL MARGIN!!"
    )

    try:
        await app_bot.bot.send_message(chat_id=CHAT_ID, text=msg)
        print(f"✅ Sinyal {arah} terkirim ({now})")
    except Exception as e:
        print("❌ Gagal kirim sinyal:", e)

# ====================
# POLYGON FALLBACK (REST)
# ====================
def fetch_polygon_price():
    """Ambil harga terakhir dari Polygon REST API"""
    try:
        url = f"https://api.polygon.io/v2/last/trade/{POLYGON_TICKER}?apiKey={POLYGON_KEY}"
        res = requests.get(url, timeout=10).json()
        price = res.get("results", {}).get("p")
        if price:
            return round(price, 2)
        else:
            print("⚠️ Polygon REST tidak memberikan harga:", res)
    except Exception as e:
        print("⚠️ Polygon REST error:", e)
    return None

# ====================
# POLYGON WEBSOCKET
# ====================
async def polygon_ws(app_bot):
    """Ambil tick realtime dari Polygon.io"""
    global last_price, initial_signal_sent
    url = "wss://socket.polygon.io/forex"
    headers = [("Authorization", f"Bearer {POLYGON_KEY}")]

    while True:
        try:
            async with websockets.connect(url, extra_headers=headers, ping_interval=20) as ws:
                await ws.send(json.dumps({"action": "auth", "params": POLYGON_KEY}))
                await ws.send(json.dumps({"action": "subscribe", "params": POLYGON_TICKER}))
                print(f"✅ Subscribed ke {POLYGON_TICKER} via Polygon WebSocket")

                async for msg in ws:
                    data = json.loads(msg)
                    if isinstance(data, list):
                        for d in data:
                            if d.get("p"):  # trade data
                                price = float(d["p"])
                                last_price = price
                                ts = datetime.utcfromtimestamp(d["t"] / 1000).replace(tzinfo=pytz.utc)
                                print(f"💲 Tick {ts.strftime('%Y-%m-%d %H:%M:%S')} UTC: {price}")

                                # kirim sinyal pertama
                                if not initial_signal_sent:
                                    initial_signal_sent = True
                                    await send_random_signal(app_bot)
        except Exception as e:
            print("⚠️ WebSocket error:", e)
            # fallback ke REST API
            rest_price = fetch_polygon_price()
            if rest_price:
                last_price = rest_price
                print(f"🔁 Fallback harga Polygon REST: {last_price}")
            await asyncio.sleep(5)

# ====================
# SCHEDULER (tiap jam)
# ====================
async def schedule_task(app_bot):
    """Jadwal sinyal otomatis tiap jam"""
    while True:
        now = datetime.now(JKT)
        next_run = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        wait = (next_run - now).total_seconds()
        print(f"⏱ Next signal at {next_run.strftime('%Y-%m-%d %H:%M:%S')} WIB (in {int(wait)}s)")
        await asyncio.sleep(wait)

        if not is_working_time(datetime.now(JKT)):
            print("⏸ Di luar jam trading.")
            continue

        await send_random_signal(app_bot)

# ====================
# TELEGRAM COMMANDS
# ====================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != AUTHORIZED_USER_ID:
        await update.message.reply_text("🚫 Tidak diizinkan.")
        return
    await update.message.reply_text("✅ Bot aktif.\nGunakan /signal atau /harga.")

async def signal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != AUTHORIZED_USER_ID:
        await update.message.reply_text("🚫 Tidak diizinkan.")
        return
    await send_random_signal(context.application)
    await update.message.reply_text("✅ Sinyal manual terkirim.")

async def harga_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global last_price
    if last_price is None:
        last_price = fetch_polygon_price()
    if last_price:
        now = datetime.now(JKT).strftime("%Y-%m-%d %H:%M:%S")
        await update.message.reply_text(f"💰 Harga realtime XAU/USD: {last_price:.2f}\n🕒 {now} WIB")
    else:
        await update.message.reply_text("⚠️ Tidak dapat mengambil harga realtime.")

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
