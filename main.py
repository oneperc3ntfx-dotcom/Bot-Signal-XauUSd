import asyncio
asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())  # Untuk Python 3.12

from flask import Flask
from threading import Thread
import requests
from datetime import datetime, time
import pytz
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes,
    MessageHandler, filters
)
import pandas as pd
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator
from bs4 import BeautifulSoup

# Konfigurasi
BOT_TOKEN = "8114552558:AAFpnQEYHYa8P43g5rjOwPs5TSbjtYh9zS4"
CHAT_ID = "-1002883903673"
AUTHORIZED_USER_ID = 1305881282
API_KEY = "21a0860958e641cc934bec6277415088"

app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running"

def keep_alive():
    Thread(target=lambda: app.run(host='0.0.0.0', port=8080)).start()

def is_bot_working_now():
    now = datetime.now(pytz.timezone("Asia/Jakarta"))
    weekday = now.weekday()  # Senin=0, Minggu=6
    jam = now.time()
    
    if weekday >= 5:  # Sabtu & Minggu
        return False
    if jam < time(7, 0) or jam >= time(24, 0):  # Sebelum jam 7 atau setelah 24:00
        return False
    return True

def fetch_data(symbol="XAU/USD", interval="5min", count=50):
    url = f"https://api.twelvedata.com/time_series?symbol={symbol}&interval={interval}&apikey={API_KEY}&outputsize={count}&format=JSON"
    try:
        response = requests.get(url, timeout=10)
        if response.status_code != 200:
            print(f"❌ Gagal ambil data: HTTP {response.status_code}")
            return None
        data = response.json().get("values", [])
        return data[::-1]
    except Exception as e:
        print(f"❌ Error fetch_data: {e}")
        return None

def prepare_df(data):
    try:
        df = pd.DataFrame(data)
        df["datetime"] = pd.to_datetime(df["datetime"])
        df.set_index("datetime", inplace=True)
        df = df.astype(float)
        return df
    except Exception as e:
        print(f"❌ Error prepare_df: {e}")
        return None

def generate_signal(df):
    if df is None or len(df) < 20:
        print("❌ Data tidak cukup")
        return None, None, None, None, None

    try:
        rsi = RSIIndicator(df["close"], window=14).rsi()
        ema = EMAIndicator(df["close"], window=9).ema_indicator()

        df["rsi"] = rsi
        df["ema"] = ema
        df.dropna(inplace=True)

        last = df.iloc[-1]
        prev = df.iloc[-2]

        note = ""
        score = 0

        if last["rsi"] < 30 and last["close"] > last["ema"]:
            score += 1
            note += "✅ RSI oversold + harga di atas EMA\n"
        if last["close"] > prev["close"]:
            score += 1
            note += "✅ Harga naik dari candle sebelumnya\n"
        if last["close"] > last["ema"]:
            score += 1
            note += "✅ Harga di atas EMA\n"

        arah = "BUY" if last["close"] > prev["close"] else "SELL"

        harga = last["close"]
        tp = round(harga + 2.0, 2) if arah == "BUY" else round(harga - 2.0, 2)
        sl = round(harga - 1.0, 2) if arah == "BUY" else round(harga + 1.0, 2)

        return arah, score, note, tp, sl
    except Exception as e:
        print(f"❌ Error generate_signal: {e}")
        return None, None, None, None, None

def format_status(score):
    if score >= 3:
        return "🟢 KUAT"
    elif score == 2:
        return "🟡 MODERAT"
    return "🔴 LEMAH"

async def send_signal(context: ContextTypes.DEFAULT_TYPE):
    if not is_bot_working_now():
        print("⏱️ Di luar jam kerja bot.")
        return

    candles = fetch_data(interval="5min")
    df = prepare_df(candles)
    arah, score, note, tp, sl = generate_signal(df)

    if arah is None:
        await context.bot.send_message(chat_id=CHAT_ID, text="❌ Gagal generate sinyal.")
        return

    harga = df["close"].iloc[-1]
    time_now = datetime.now(pytz.timezone("Asia/Jakarta")).strftime("%H:%M:%S")

    msg = f"""📡 *Sinyal XAU/USD*
🕒 {time_now} WIB
📈 Arah: *{arah}*
💰 Harga: `{harga}`
🎯 TP: `{tp}`
🛑 SL: `{sl}`
📊 Status: {format_status(score)}

🔍 Analisa:
{note}
"""
    await context.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")

# Commands
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != AUTHORIZED_USER_ID:
        await update.message.reply_text("🚫 Anda tidak diizinkan.")
        return
    await update.message.reply_text("✅ Bot aktif.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("/start\n/help\n/info")

async def info_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot sinyal XAU/USD\nSenin–Jumat 07:00–24:00 WIB\nAnalisa setiap 5 menit (TF M5)\nKirim sinyal setiap 15 menit")

async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❓ Perintah tidak dikenali.")

def main():
    keep_alive()
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("info", info_command))
    application.add_handler(MessageHandler(filters.COMMAND, unknown_command))

    job_queue = application.job_queue

    # Analisa setiap 5 menit (fetch & prepare data)
    async def analyze_task(context: ContextTypes.DEFAULT_TYPE):
        if is_bot_working_now():
            candles = fetch_data(interval="5min")
            df = prepare_df(candles)
            context.chat_data["latest_df"] = df

    job_queue.run_repeating(analyze_task, interval=300, first=0)  # 5 menit

    # Kirim sinyal setiap 15 menit
    async def signal_task(context: ContextTypes.DEFAULT_TYPE):
        if is_bot_working_now() and "latest_df" in context.chat_data:
            df = context.chat_data["latest_df"]
            arah, score, note, tp, sl = generate_signal(df)
            if arah is None:
                return
            harga = df["close"].iloc[-1]
            time_now = datetime.now(pytz.timezone("Asia/Jakarta")).strftime("%H:%M:%S")
            msg = f"""📡 *Sinyal XAU/USD*
🕒 {time_now} WIB
📈 Arah: *{arah}*
💰 Harga: `{harga}`
🎯 TP: `{tp}`
🛑 SL: `{sl}`
📊 Status: {format_status(score)}

🔍 Analisa:
{note}
"""
            await context.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")

    job_queue.run_repeating(signal_task, interval=900, first=0)  # 15 menit

    print("🚀 Bot berjalan...")
    application.run_polling()

if __name__ == "__main__":
    main()
