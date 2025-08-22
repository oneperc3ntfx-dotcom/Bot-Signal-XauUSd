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
    weekday = now.weekday()
    jam = now.time()
    
    if weekday == 4 and jam >= time(22, 0):  # Jumat setelah 22:00
        return False
    if weekday in [5, 6]:  # Sabtu dan Minggu
        return False
    return True  # Senin–Kamis dan Jumat sebelum 22:00

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

def check_high_impact_news():
    try:
        url = "https://www.forexfactory.com/calendar.php?week=this"
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code != 200:
            return False
        soup = BeautifulSoup(response.text, "html.parser")
        rows = soup.select("tr.calendar__row")

        now = datetime.now(pytz.timezone("Asia/Jakarta"))

        for row in rows:
            impact = row.select_one("td.calendar__impact")
            time_td = row.select_one("td.calendar__time")
            if not impact or not time_td:
                continue
            if "high" not in impact.get("title", "").lower():
                continue
            time_str = time_td.get_text(strip=True)
            if not time_str or time_str.lower() in ["all day", "tentative"]:
                continue
            try:
                news_time = datetime.strptime(time_str, "%H:%M").time()
            except:
                continue
            ny_tz = pytz.timezone("America/New_York")
            jakarta_tz = pytz.timezone("Asia/Jakarta")
            today_ny = datetime.now(ny_tz).replace(hour=news_time.hour, minute=news_time.minute, second=0, microsecond=0)
            news_jakarta_time = today_ny.astimezone(jakarta_tz)
            delta = abs((news_jakarta_time - now).total_seconds())
            if delta <= 1800:
                return True
        return False
    except Exception as e:
        print(f"❌ Error cek news: {e}")
        return False

async def send_signal(context: ContextTypes.DEFAULT_TYPE):
    if not is_bot_working_now():
        print("⏱️ Di luar jam kerja bot.")
        return

    if check_high_impact_news():
        await context.bot.send_message(chat_id=CHAT_ID, text="🚨 Ada berita berdampak tinggi. Sinyal diskip.")
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
    await update.message.reply_text("Bot sinyal XAU/USD\nSenin–Kamis 24 jam\nJumat hingga 22:00 WIB\nAnalisa setiap jam (TF M5)")

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

    # Kirim sinyal setiap 1 jam (3600 detik)
    job_queue.run_repeating(send_signal, interval=3600, first=0)

    # Kirim sinyal langsung setelah startup
    async def startup(context: ContextTypes.DEFAULT_TYPE):
        await send_signal(context)

    job_queue.run_once(startup, when=0)

    print("🚀 Bot berjalan...")
    application.run_polling()

if __name__ == "__main__":
    main()
