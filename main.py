import asyncio
asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())  # Untuk Python 3.12

from flask import Flask
from threading import Thread
import requests
from datetime import datetime, time
import pytz
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters
)
import pandas as pd
from ta.momentum import RSIIndicator, StochasticOscillator
from ta.trend import EMAIndicator, MACD
from ta.volatility import BollingerBands, AverageTrueRange
from bs4 import BeautifulSoup

# ================== CONFIG ==================
BOT_TOKEN = "8114552558:AAFpnQEYHYa8P43g5rjOwPs5TSbjtYh9zS4"
CHAT_ID = "-1002883903673"
AUTHORIZED_USER_ID = 1305881282

# Multiple API Keys TwelveData
API_KEYS_TWELVE = [
    "94a7d766d73f4db4a7ddf877473711c7",
    "af23649e02da42aab3e78cf343513325",
    "af23649e02da42aab3e78cf343513325"
]

_current_key_index = 0

def get_active_api_key():
    global _current_key_index
    key = API_KEYS_TWELVE[_current_key_index]
    _current_key_index = (_current_key_index + 1) % len(API_KEYS_TWELVE)  # round-robin
    return key

API_KEY_METALS = "2fzz3e9hw1rachdt6jwwo4furz1arvngsm879pg5bj9ucoe2xjjbv4l4gn72"

# ================== FLASK KEEP-ALIVE ==================
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
    if weekday == 4 and jam >= time(22, 0):  # Jumat setelah 22:00 WIB
        return False
    if weekday in [5, 6]:  # Sabtu & Minggu
        return False
    return True

# ================== DATA FETCHER ==================
def fetch_twelvedata_series(symbol="XAU/USD", interval="5min", count=120):
    for _ in range(len(API_KEYS_TWELVE)):
        api_key = get_active_api_key()
        url = f"https://api.twelvedata.com/time_series?symbol={symbol}&interval={interval}&apikey={api_key}&outputsize={count}&format=JSON"
        try:
            response = requests.get(url, timeout=10)
            if response.status_code != 200:
                print(f"❌ Gagal ambil data TwelveData: HTTP {response.status_code}")
                continue
            data = response.json()
            if "status" in data and data["status"] == "error":
                print(f"❌ Error TwelveData: {data.get('message')}")
                continue
            return data.get("values", [])[::-1]
        except Exception as e:
            print(f"❌ Error fetch_twelvedata_series: {e}")
            continue
    return None

def fetch_realtime_price_metals_fast():
    """Ambil harga XAU/USD realtime dari Metals API (premium)."""
    try:
        url = f"https://metals-api.com/api/latest?access_key={API_KEY_METALS}&base=USD&symbols=XAU"
        r = requests.get(url, timeout=5).json()
        rate = r.get("rates", {}).get("XAU")
        if rate and rate > 0:
            return round(1.0 / float(rate), 2)
        print("❌ Metals-API response:", r)
        return None
    except Exception as e:
        print(f"❌ Error fetch_realtime_price_metals_fast: {e}")
        return None

def prepare_df(data):
    try:
        df = pd.DataFrame(data)
        df["datetime"] = pd.to_datetime(df["datetime"])
        df.set_index("datetime", inplace=True)
        for col in ["open", "high", "low", "close"]:
            df[col] = df[col].astype(float)
        return df
    except Exception as e:
        print(f"❌ Error prepare_df: {e}")
        return None

# ================== STRATEGI UTAMA ==================
def generate_signal(df):
    if df is None or len(df) < 20:
        print("❌ Data tidak cukup")
        return None, None, None
    try:
        rsi = RSIIndicator(df["close"], window=14).rsi()
        ema9 = EMAIndicator(df["close"], window=9).ema_indicator()
        df["rsi"] = rsi
        df["ema"] = ema9
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

# ================== ANALISA TAMBAHAN ==================
# (extra_analysis, detect_candles sama persis seperti sebelumnya)

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
            # cek jarak berita <30 menit
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

# ================== SIGNAL ENGINE ==================
_last_strong_sent_at = None
_strong_cooldown_min = 10

async def send_signal(context: ContextTypes.DEFAULT_TYPE):
    if not is_bot_working_now():
        print("⏱️ Di luar jam kerja bot.")
        return
    if check_high_impact_news():
        await context.bot.send_message(chat_id=CHAT_ID, text="🚨 Ada berita berdampak tinggi. Sinyal diskip.")
        return
    candles = fetch_twelvedata_series(interval="5min")
    df = prepare_df(candles)
    arah, score, note = generate_signal(df)
    if arah is None:
        await context.bot.send_message(chat_id=CHAT_ID, text="❌ Gagal generate sinyal.")
        return
    harga_live = fetch_realtime_price_metals_fast() or df["close"].iloc[-1]
    tp = round(harga_live + 2.0, 2) if arah == "BUY" else round(harga_live - 2.0, 2)
    sl = round(harga_live - 1.0, 2) if arah == "BUY" else round(harga_live + 1.0, 2)
    time_now = datetime.now(pytz.timezone("Asia/Jakarta")).strftime("%H:%M:%S")
    tambahan = ""  # panggil extra_analysis(df) kalau mau tambahan
    msg = f"""📡 *Sinyal XAU/USD* 🕒 {time_now} WIB
📈 Arah: *{arah}*
💰 Harga: {harga_live}
🎯 TP: {tp} 🛑 SL: {sl}
📊 Status: {'🟢 KUAT' if score>=3 else ('🟡 MODERAT' if score==2 else '🔴 LEMAH')}
🔍 Analisa: {note}{tambahan}"""
    await context.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")

# ================== STRONG SETUP ==================
# (is_strong_setup & monitor_strong_signal → ganti juga ambil harga pakai fetch_realtime_price_metals_fast)

# ================== HANDLER & MAIN ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != AUTHORIZED_USER_ID:
        await update.message.reply_text("❌ Anda tidak berhak pakai bot ini.")
        return
    await update.message.reply_text("✅ Bot berjalan!")

async def manual_signal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != AUTHORIZED_USER_ID:
        await update.message.reply_text("❌ Anda tidak berhak pakai bot ini.")
        return
    await send_signal(context)

async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❓ Perintah tidak dikenal.")

def main():
    keep_alive()
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("signal", manual_signal))
    app.add_handler(MessageHandler(filters.COMMAND, unknown))
    app.job_queue.run_repeating(send_signal, interval=300, first=10)
    app.job_queue.run_repeating(monitor_strong_signal, interval=60, first=20)
    print("🤖 Bot berjalan...")
    app.run_polling()

if __name__ == "__main__":
    main()
