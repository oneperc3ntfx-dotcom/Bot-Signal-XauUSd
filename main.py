import os
import time
import requests
import schedule
import datetime
import pandas as pd
import numpy as np
from telegram import Bot, Update
from telegram.ext import Updater, CommandHandler, CallbackContext

# ==========================
# Load Environment Variables
# ==========================
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
TD_API_KEYS = os.getenv("TD_API_KEYS", "").split(",")
FINNHUB_TOKEN = os.getenv("FINNHUB_TOKEN")
PAIR_SYMBOL = os.getenv("PAIR_SYMBOL", "XAU/USD")
CANDLE_INTERVAL_MIN = int(os.getenv("CANDLE_INTERVAL_MIN", 5))

if not BOT_TOKEN or not CHAT_ID:
    print("❌ ERROR: BOT_TOKEN atau CHAT_ID tidak ditemukan di environment.")
    exit(1)

bot = Bot(token=BOT_TOKEN)
updater = Updater(BOT_TOKEN, use_context=True)
dispatcher = updater.dispatcher

last_signal_time = None

# ==========================
# Fetch Candle Data
# ==========================
def fetch_candle_data():
    if not TD_API_KEYS or TD_API_KEYS[0] == "":
        print("❌ API Key Twelve Data tidak ditemukan.")
        return pd.DataFrame()

    api_key = TD_API_KEYS[0]
    url = f"https://api.twelvedata.com/time_series?symbol={PAIR_SYMBOL}&interval={CANDLE_INTERVAL_MIN}min&apikey={api_key}&outputsize=100"
    
    try:
        response = requests.get(url, timeout=10)
        data = response.json()
        if "values" in data:
            df = pd.DataFrame(data["values"])
            df = df.astype({"open": float, "high": float, "low": float, "close": float, "volume": float})
            df = df[::-1]
            return df
        else:
            print("⚠️ Tidak ada data candle dari Twelve Data:", data)
            return pd.DataFrame()
    except Exception as e:
        print("❌ Error fetch candle:", e)
        return pd.DataFrame()

# ==========================
# Fetch Realtime Price
# ==========================
def fetch_realtime_price():
    try:
        symbol = PAIR_SYMBOL.replace("/", "")
        url = f"https://finnhub.io/api/v1/quote?symbol={symbol}&token={FINNHUB_TOKEN}"
        res = requests.get(url, timeout=10).json()
        return res.get("c", None)  # Current price
    except Exception as e:
        print("❌ Error fetch realtime price:", e)
        return None

# ==========================
# Compute Indicators
# ==========================
def compute_indicators(df):
    df['SMA20'] = df['close'].rolling(20).mean()
    df['EMA20'] = df['close'].ewm(span=20, adjust=False).mean()

    delta = df['close'].diff()
    gain = np.where(delta > 0, delta, 0)
    loss = np.where(delta < 0, -delta, 0)
    avg_gain = pd.Series(gain).rolling(14).mean()
    avg_loss = pd.Series(loss).rolling(14).mean()
    rs = avg_gain / avg_loss
    df['RSI'] = 100 - (100 / (1 + rs))

    ema12 = df['close'].ewm(span=12, adjust=False).mean()
    ema26 = df['close'].ewm(span=26, adjust=False).mean()
    df['MACD'] = ema12 - ema26
    df['MACD_signal'] = df['MACD'].ewm(span=9, adjust=False).mean()

    df['BB_middle'] = df['SMA20']
    df['BB_upper'] = df['SMA20'] + 2*df['close'].rolling(20).std()
    df['BB_lower'] = df['SMA20'] - 2*df['close'].rolling(20).std()

    low14 = df['low'].rolling(14).min()
    high14 = df['high'].rolling(14).max()
    df['%K'] = (df['close'] - low14) / (high14 - low14) * 100
    df['%D'] = df['%K'].rolling(3).mean()

    return df

# ==========================
# Weighted Signal Analysis
# ==========================
def analyze_candles(df):
    df = compute_indicators(df)
    last = df.iloc[-1]
    prev = df.iloc[-2]

    score = 0
    reasons = []

    if last['close'] > last['SMA20'] and last['close'] > last['EMA20']:
        score += 2
        reasons.append("Trend naik (SMA/EMA)")
    elif last['close'] < last['SMA20'] and last['close'] < last['EMA20']:
        score -= 2
        reasons.append("Trend turun (SMA/EMA)")

    if last['RSI'] > 70:
        score -= 2
        reasons.append("Overbought (RSI)")
    elif last['RSI'] < 30:
        score += 2
        reasons.append("Oversold (RSI)")

    if last['MACD'] > last['MACD_signal'] and prev['MACD'] <= prev['MACD_signal']:
        score += 1
        reasons.append("MACD bullish crossover")
    elif last['MACD'] < last['MACD_signal'] and prev['MACD'] >= prev['MACD_signal']:
        score -= 1
        reasons.append("MACD bearish crossover")

    if last['close'] > last['BB_upper']:
        score -= 1
        reasons.append("Harga di atas BB atas (kemungkinan retrace)")
    elif last['close'] < last['BB_lower']:
        score += 1
        reasons.append("Harga di bawah BB bawah (kemungkinan rebound)")

    if last['%K'] > 80 and last['%D'] > 80:
        score -= 1
        reasons.append("Overbought (Stochastic)")
    elif last['%K'] < 20 and last['%D'] < 20:
        score += 1
        reasons.append("Oversold (Stochastic)")

    if score > 1:
        signal = "BUY"
        tp = last['close'] * 1.01
        sl = last['close'] * 0.995
    elif score < -1:
        signal = "SELL"
        tp = last['close'] * 0.99
        sl = last['close'] * 1.005
    else:
        signal = "HOLD"
        tp = sl = None

    return {
        "signal": signal,
        "reason": "; ".join(reasons) if reasons else "No clear signal",
        "tp": round(tp, 2) if tp else None,
        "sl": round(sl, 2) if sl else None
    }

# ==========================
# Send Signal to Telegram
# ==========================
def send_signal():
    global last_signal_time
    df = fetch_candle_data()
    if df.empty or len(df) < 30:
        print("⚠️ Data candle kosong atau kurang untuk analisa.")
        return

    analysis = analyze_candles(df)
    message = (
        f"📊 Pair: {PAIR_SYMBOL}\n"
        f"🕒 Time: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"🔔 Signal: {analysis['signal']}\n"
        f"💡 Reason: {analysis['reason']}\n"
        f"🎯 TP: {analysis['tp']}\n"
        f"🛑 SL: {analysis['sl']}\n\n"
        "⚠️ HARAP GUNAKAN MONEY MANAGEMENT SESUAI EQUITAS. JANGAN FULL MARGIN !!"
    )

    try:
        bot.send_message(chat_id=CHAT_ID, text=message)
        last_signal_time = datetime.datetime.now()
        print("✅ Signal terkirim ke Telegram.")
    except Exception as e:
        print("❌ Gagal kirim ke Telegram:", e)

# ==========================
# Command Handlers
# ==========================
def start(update: Update, context: CallbackContext):
    update.message.reply_text(
        "🤖 Halo! Bot signal trading aktif.\n\n"
        "Perintah yang tersedia:\n"
        "/harga - Cek harga realtime\n"
        "/signal - Kirim sinyal analisa terbaru\n"
        "/status - Lihat status bot"
    )

def harga(update: Update, context: CallbackContext):
    price = fetch_realtime_price()
    if price:
        update.message.reply_text(f"💰 Harga {PAIR_SYMBOL} saat ini: {price}")
    else:
        update.message.reply_text("⚠️ Gagal mengambil harga realtime.")

def signal_now(update: Update, context: CallbackContext):
    send_signal()
    update.message.reply_text("✅ Sinyal terbaru dikirim ke channel.")

def status(update: Update, context: CallbackContext):
    global last_signal_time
    status_msg = "✅ Bot aktif dan berjalan normal.\n"
    if last_signal_time:
        status_msg += f"📅 Sinyal terakhir dikirim: {last_signal_time.strftime('%Y-%m-%d %H:%M:%S')}"
    else:
        status_msg += "❌ Belum ada sinyal dikirim."
    update.message.reply_text(status_msg)

dispatcher.add_handler(CommandHandler("start", start))
dispatcher.add_handler(CommandHandler("harga", harga))
dispatcher.add_handler(CommandHandler("signal", signal_now))
dispatcher.add_handler(CommandHandler("status", status))

# ==========================
# Scheduler
# ==========================
def job():
    now = datetime.datetime.now()
    if now.weekday() < 5:  # Senin - Jumat
        send_signal()
    else:
        print("⏸️ Akhir pekan, bot tidak aktif.")

# 00:00 WIB = 17:00 UTC
schedule.every().day.at("17:00").do(job)

# ==========================
# Run bot
# ==========================
print("🤖 Bot signal trading aktif. Menunggu jadwal 00:00 WIB dan siap menerima chat...")

updater.start_polling()  # biar bisa respon chat

while True:
    try:
        schedule.run_pending()
        time.sleep(1)
    except Exception as e:
        print("❌ Error utama:", e)
        time.sleep(5)
