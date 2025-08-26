import asyncio
asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())  # Untuk Python 3.12+

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
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator, MACD
from ta.volatility import AverageTrueRange

# ================== CONFIG ==================
BOT_TOKEN = "8114552558:AAFpnQEYHYa8P43g5rjOwPs5TSbjtYh9zS4"  # Token bot kamu
CHAT_ID = "-1002883903673"  # Grup/Channel ID kamu
AUTHORIZED_USER_ID = 1305881282  # Hanya kamu yang bisa akses

# Multiple API Keys TwelveData (untuk analisa)
API_KEYS_TWELVE = [
    "94a7d766d73f4db4a7ddf877473711c7",
    "af23649e02da42aab3e78cf343513325",
    "af23649e02da42aab3e78cf343513325",
]
_current_key_index = 0

def get_active_api_key():
    global _current_key_index
    key = API_KEYS_TWELVE[_current_key_index]
    _current_key_index = (_current_key_index + 1) % len(API_KEYS_TWELVE)
    return key

# Metals API (untuk harga realtime)
API_KEY_METALS = "2fzz3e9hw1rachdt6jwwo4furz1arvngsm879pg5bj9ucoe2xjjbv4l4gn72"

# ================== FLASK KEEP-ALIVE ==================
flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "Bot is running"

def keep_alive():
    Thread(target=lambda: flask_app.run(host='0.0.0.0', port=8080)).start()

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

# ================== FETCH DATA ==================
def fetch_candles(symbol="XAU/USD", interval="5min", outputsize=100):
    key = get_active_api_key()
    url = f"https://api.twelvedata.com/time_series?symbol={symbol}&interval={interval}&apikey={key}&outputsize={outputsize}"
    try:
        r = requests.get(url, timeout=10)
        data = r.json()
        if "values" not in data:
            return None
        df = pd.DataFrame(data["values"])
        df["datetime"] = pd.to_datetime(df["datetime"])
        df = df.sort_values("datetime")
        df[["open", "high", "low", "close"]] = df[["open", "high", "low", "close"]].astype(float)
        return df
    except:
        return None

def fetch_price_metals(symbol="XAUUSD"):
    url = f"https://metals-api.com/api/latest?access_key={API_KEY_METALS}&base=USD&symbols={symbol}"
    try:
        r = requests.get(url, timeout=10)
        data = r.json()
        return data.get("rates", {}).get(symbol)
    except:
        return None

# ================== ANALYSIS ==================
def analyze(df):
    result = []
    close = df["close"]

    # RSI
    rsi = RSIIndicator(close=close, window=14).rsi().iloc[-1]
    if rsi < 30:
        result.append("RSI oversold")
    elif rsi > 70:
        result.append("RSI overbought")

    # EMA
    ema20 = EMAIndicator(close=close, window=20).ema_indicator().iloc[-1]
    ema50 = EMAIndicator(close=close, window=50).ema_indicator().iloc[-1]
    if ema20 > ema50:
        result.append("Trend bullish (EMA20>EMA50)")
    else:
        result.append("Trend bearish (EMA20<EMA50)")

    # MACD
    macd = MACD(close=close)
    macd_val = macd.macd().iloc[-1]
    macd_signal = macd.macd_signal().iloc[-1]
    if macd_val > macd_signal:
        result.append("MACD bullish crossover")
    elif macd_val < macd_signal:
        result.append("MACD bearish crossover")

    # ATR
    atr = AverageTrueRange(
        high=df["high"], low=df["low"], close=close, window=14
    ).average_true_range().iloc[-1]
    result.append(f"ATR: {atr:.2f}")

    # Support & Resistance (high/low terakhir)
    support = df["low"].min()
    resistance = df["high"].max()
    result.append(f"Support: {support:.2f}, Resistance: {resistance:.2f}")

    # Fibo (swing high-low)
    high = df["high"].max()
    low = df["low"].min()
    diff = high - low
    fibo_38 = high - 0.382 * diff
    fibo_61 = high - 0.618 * diff
    result.append(f"Fibo 38.2%: {fibo_38:.2f}, 61.8%: {fibo_61:.2f}")

    return result

# ================== SIGNAL ==================
async def send_signal(context: ContextTypes.DEFAULT_TYPE):
    if not is_bot_working_now():
        return

    df = fetch_candles()
    if df is None:
        return

    analysis = analyze(df)
    price = fetch_price_metals("XAUUSD")

    direction = "⬆️ BUY" if "bullish" in " ".join(analysis).lower() else "⬇️ SELL"
    signal_msg = f"""
📊 Gold Trading Signal

Signal: {direction}
Price: {price}

Analysis:
- {"\n- ".join(analysis)}

Time: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    """

    await context.bot.send_message(chat_id=CHAT_ID, text=signal_msg)

# ================== HANDLERS ==================
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

# ================== MAIN ==================
def main():
    keep_alive()
    bot_app = ApplicationBuilder().token(BOT_TOKEN).build()
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CommandHandler("signal", manual_signal))
    bot_app.add_handler(MessageHandler(filters.COMMAND, unknown))

    # Kirim sinyal tiap 30 menit
    bot_app.job_queue.run_repeating(send_signal, interval=1800, first=10)

    print("🤖 Bot berjalan...")
    bot_app.run_polling()

if __name__ == "__main__":
    main()
