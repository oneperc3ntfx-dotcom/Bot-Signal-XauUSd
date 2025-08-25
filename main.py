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

# ================== CONFIG ==================
BOT_TOKEN = "8114552558:AAFpnQEYHYa8P43g5rjOwPs5TSbjtYh9zS4"
CHAT_ID = "-1002883903673"
AUTHORIZED_USER_ID = 1305881282
TWELVE_API_KEY = "21a0860958e641cc934bec6277415088"
METALS_API_KEY = "2fzz3e9hw1rachdt6jwwo4furz1arvngsm879pg5bj9ucoe2xjjbv4l4gn72"
# ============================================

app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running"

def keep_alive():
    Thread(target=lambda: app.run(host='0.0.0.0', port=8080)).start()

# ================== MARKET TIME ==================
def is_bot_working_now():
    now = datetime.now(pytz.timezone("Asia/Jakarta"))
    weekday = now.weekday()
    jam = now.time()
    
    if weekday == 4 and jam >= time(22, 0):  # Jumat setelah 22:00
        return False
    if weekday in [5, 6]:  # Sabtu dan Minggu
        return False
    return True
# =================================================

# ================== DATA FETCHER ==================
def fetch_data(symbol="XAU/USD", interval="5min", count=50):
    url = f"https://api.twelvedata.com/time_series?symbol={symbol}&interval={interval}&apikey={TWELVE_API_KEY}&outputsize={count}&format=JSON"
    try:
        response = requests.get(url, timeout=10)
        if response.status_code != 200:
            print(f"❌ Gagal ambil data: HTTP {response.status_code}")
            return None
        data = response.json().get("values", [])
        return data[::-1]  # ascending
    except Exception as e:
        print(f"❌ Error fetch_data: {e}")
        return None

def fetch_realtime_price_twelve(symbol="XAU/USD"):
    """Ambil harga realtime (1M terakhir) dari Twelve Data"""
    url = f"https://api.twelvedata.com/time_series?symbol={symbol}&interval=1min&apikey={TWELVE_API_KEY}&outputsize=1&format=JSON"
    try:
        response = requests.get(url, timeout=10).json()
        last_candle = response.get("values", [])[0]
        return float(last_candle["close"])
    except Exception as e:
        print(f"❌ Error fetch_realtime_price_twelve: {e}")
        return None

def fetch_realtime_price_metals():
    """Ambil harga realtime dari Metals-API"""
    url = f"https://metals-api.com/api/latest?access_key={METALS_API_KEY}&base=USD&symbols=XAU"
    try:
        response = requests.get(url, timeout=10).json()
        if "rates" in response and "XAU" in response["rates"]:
            return float(response["rates"]["XAU"])
        else:
            print("❌ Gagal ambil harga dari Metals-API:", response)
            return None
    except Exception as e:
        print(f"❌ Error fetch_realtime_price_metals: {e}")
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
# =================================================

# ================== STRATEGY ==================
def generate_signal(df):
    if df is None or len(df) < 20:
        print("❌ Data tidak cukup")
        return None, None, None

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

        return arah, score, note
    except Exception as e:
        print(f"❌ Error generate_signal: {e}")
        return None, None, None

def format_status(score):
    if score >= 3:
        return "🟢 KUAT"
    elif score == 2:
        return "🟡 MODERAT"
    return "🔴 LEMAH"
# =================================================

# ================== NEWS FILTER ==================
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
            if delta <= 1800:  # 30 menit
                return True
        return False
    except Exception as e:
        print(f"❌ Error cek news: {e}")
        return False
# =================================================

# ================== SIGNAL ENGINE ==================
async def send_signal(context: ContextTypes.DEFAULT_TYPE):
    if not is_bot_working_now():
        print("⏱️ Di luar jam kerja bot.")
        return

    if check_high_impact_news():
        await context.bot.send_message(chat_id=CHAT_ID, text="🚨 Ada berita berdampak tinggi. Sinyal diskip.")
        return

    # Ambil candle 5M untuk analisa
    candles = fetch_data(interval="5min")
    df = prepare_df(candles)
    arah, score, note = generate_signal(df)

    if arah is None:
        await context.bot.send_message(chat_id=CHAT_ID, text="❌ Gagal generate sinyal.")
        return

    # Ambil harga realtime
    harga_live = fetch_realtime_price_metals()
    if harga_live is None:
        harga_live = fetch_realtime_price_twelve()
    if harga_live is None:
        harga_live = df["close"].iloc[-1]

    # Hitung TP & SL
    if arah == "BUY":
        tp = round(harga_live + 2.0, 2)
        sl = round(harga_live - 1.0, 2)
    else:
        tp = round(harga_live - 2.0, 2)
        sl = round(harga_live + 1.0, 2)

    time_now = datetime.now(pytz.timezone("Asia/Jakarta")).strftime("%H:%M:%S")

    msg = f"""📡 *Sinyal XAU/USD*
🕒 {time_now} WIB
📈 Arah: *{arah}*
💰 Harga: `{harga_live}`
🎯 TP: `{tp}`
🛑 SL: `{sl}`
📊 Status: {format_status(score)}

🔍 Analisa:
{note}
"""

    await context.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")
# =================================================

# ================== COMMAND HANDLER ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != AUTHORIZED_USER_ID:
        await update.message.reply_text("🚫 Anda tidak diizinkan.")
        return
    await update.message.reply_text("✅ Bot aktif.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("/start\n/help\n/info")

async def info_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot sinyal XAU/USD\nSenin–Kamis 24 jam\nJumat hingga 22:00 WIB\nAnalisa setiap jam (TF M5, harga realtime Metals-API + Twelve Data)")

async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❓ Perintah tidak dikenali.")
# =================================================

# ================== MAIN ==================
def main():
    keep_alive()
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("info", info_command))
    application.add_handler(MessageHandler(filters.COMMAND, unknown_command))

    job_queue = application.job_queue

    # Sinyal tiap 1 jam
    job_queue.run_repeating(send_signal, interval=3600, first=0)

    # Sinyal pertama setelah startup
    async def startup(context: ContextTypes.DEFAULT_TYPE):
        await send_signal(context)

    job_queue.run_once(startup, when=0)

    print("🚀 Bot berjalan...")
    application.run_polling()

if __name__ == "__main__":
    main()
