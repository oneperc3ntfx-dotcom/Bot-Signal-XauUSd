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

# =========================
# Konfigurasi
# =========================
BOT_TOKEN = "8114552558:AAFpnQEYHYa8P43g5rjOwPs5TSbjtYh9zS4"
CHAT_ID = "-1002883903673"
AUTHORIZED_USER_ID = 1305881282
API_KEY = "21a0860958e641cc934bec6277415088"

# OFFSET harga yang ditambahkan ke harga asli, TP, dan SL.
# Contoh kasus kamu: harga sinyal 3313 sementara harga asli 3320 -> perlu +7.0
OFFSET = 7.0

app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running"

def keep_alive():
    Thread(target=lambda: app.run(host='0.0.0.0', port=8080)).start()

def is_bot_working_now():
    now = datetime.now(pytz.timezone("Asia/Jakarta"))
    weekday = now.weekday()  # 0=Senin, 6=Minggu
    jam = now.time()
    # Sabtu & Minggu libur
    if weekday in [5, 6]:
        return False
    # Jam kerja: 07:00 - 23:59
    if jam < time(7, 0) or jam > time(23, 59, 59):
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
    # Pastikan selalu return 6 nilai agar tidak error saat unpack
    if df is None or len(df) < 20:
        print("❌ Data tidak cukup")
        return None, None, None, None, None, None

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

        return arah, score, note, tp, sl, harga
    except Exception as e:
        print(f"❌ Error generate_signal: {e}")
        return None, None, None, None, None, None

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
        response = requests.get(url, headers=hea
