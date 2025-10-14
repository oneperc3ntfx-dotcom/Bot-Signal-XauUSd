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
# Load environment variables
# ==========================
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
AUTHORIZED_USER_ID = int(os.getenv("AUTHORIZED_USER_ID"))

TD_API_KEYS = os.getenv("TD_API_KEYS").split(",")
FINNHUB_TOKEN = os.getenv("FINNHUB_TOKEN")

PAIR_SYMBOL = os.getenv("PAIR_SYMBOL", "XAU/USD")
CANDLE_INTERVAL_MIN = int(os.getenv("CANDLE_INTERVAL_MIN", 5))

bot = Bot(token=BOT_TOKEN)
last_signal_time = None  # untuk /status


# ==========================
# Fetch Realtime Price
# ==========================
def fetch_realtime_price():
    """Ambil harga realtime dominan dari Finnhub, fallback ke Twelve Data jika gagal"""
    try:
        # Format simbol untuk Finnhub
        symbol = f"OANDA:{PAIR_SYMBOL.replace('/', '_')}"
        url = f"https://finnhub.io/api/v1/quote?symbol={symbol}&token={FINNHUB_TOKEN}"
        res = requests.get(url, timeout=10).json()
        price = res.get("c", None)
        if price:
            return round(price, 2)
    except Exception as e:
        print("⚠️ Finnhub error:", e)

    # Fallback ke Twelve Data
    try:
        api_key = TD_API_KEYS[0]
        url = f"https://api.twelvedata.com/price?symbol={PAIR_SYMBOL}&apikey={api_key}"
        res = requests.get(url, timeout=10).json()
        if "price" in res:
            return round(float(res["price"]), 2)
    except Exception as e:
        print("⚠️ Twelve Data error:", e)

    return None


# ==========================
# Fetch Candle Data
# ==========================
def fetch_candle_data():
    api_key = TD_API_KEYS[0]
    url = f"https://api.twelvedata.com/time_series?symbol={PAIR_SYMBOL}&interval={CANDLE_INTERVAL_MIN}min&apikey={api_key}&outputsize=500"
    response = requests.get(url).json()
    if "values" in response:
        df = pd.DataFrame(response["values"])
        df = df.astype({"open": float, "high": float, "low": float, "close": float, "volume": float})
        df = df[::-1]  # earliest first
        return df
    return pd.DataFrame()


# ==========================
# Indicators
# ==========================
def compute_indicators(df):
    # SMA & EMA
    df['SMA20'] = df['close'].rolling(20).mean()
    df['EMA20'] = df['close'].ewm(span=20, adjust=False).mean()

    # RSI
    delta = df['close'].diff()
    gain = np.where(delta > 0, delta, 0)
    loss = np.where(delta < 0, -delta, 0)
    avg_gain = pd.Series(gain).rolling(14).mean()
    avg_loss = pd.Series(loss).rolling(14).mean()
    rs = avg_gain / avg_loss
    df['RSI'] = 100 - (100 / (1 + rs))

    # MACD
    ema12 = df['close'].ewm(span=12, adjust=False).mean()
    ema26 = df['close'].ewm(span=26, adjust=False).mean()
    df['MACD'] = ema12 - ema26
    df['MACD_signal'] = df['MACD'].ewm(span=9, adjust=False).mean()

    # Bollinger Bands
    df['BB_middle'] = df['SMA20']
    df['BB_upper'] = df['SMA20'] + 2 * df['close'].rolling(20).std()
    df['BB_lower'] = df['SMA20'] - 2 * df['close'].rolling(20).std()

    # Stochastic
    low14 = df['low'].rolling(14).min()
    high14 = df['high'].rolling(14).max()
    df['%K'] = (df['close'] - low14) / (high14 - low14) * 100
    df['%D'] = df['%K'].rolling(3).mean()

    # ADX
    df['TR'] = np.maximum(df['high'] - df['low'], np.maximum(abs(df['high'] - df['close'].shift()), abs(df['low'] - df['close'].shift())))
    df['+DM'] = np.where((df['high'] - df['high'].shift()) > (df['low'].shift() - df['low']), df['high'] - df['high'].shift(), 0)
    df['-DM'] = np.where((df['low'].shift() - df['low']) > (df['high'] - df['high'].shift()), df['low'].shift() - df['low'], 0)
    df['+DI'] = 100 * (df['+DM'].ewm(alpha=1/14).mean() / df['TR'].ewm(alpha=1/14).mean())
    df['-DI'] = 100 * (df['-DM'].ewm(alpha=1/14).mean() / df['TR'].ewm(alpha=1/14).mean())
    df['ADX'] = 100 * abs(df['+DI'] - df['-DI']) / (df['+DI'] + df['-DI'])

    return df


# ==========================
# Signal Analysis
# ==========================
def analyze_candles(df):
    df = compute_indicators(df)
    last = df.iloc[-1]
    prev = df.iloc[-2]

    score = 0
    reasons = []

    # Trend
    if last['close'] > last['SMA20'] and last['close'] > last['EMA20']:
        score += 2; reasons.append("Trend naik (SMA/EMA)")
    elif last['close'] < last['SMA20'] and last['close'] < last['EMA20']:
        score -= 2; reasons.append("Trend turun (SMA/EMA)")

    # RSI
    if last['RSI'] > 70:
        score -= 2; reasons.append("Overbought (RSI)")
    elif last['RSI'] < 30:
        score += 2; reasons.append("Oversold (RSI)")

    # MACD
    if last['MACD'] > last['MACD_signal'] and prev['MACD'] <= prev['MACD_signal']:
        score += 1; reasons.append("MACD bullish crossover")
    elif last['MACD'] < last['MACD_signal'] and prev['MACD'] >= prev['MACD_signal']:
        score -= 1; reasons.append("MACD bearish crossover")

    # Bollinger Bands
    if last['close'] > last['BB_upper']:
        score -= 1; reasons.append("Harga di atas BB atas (potensi retrace)")
    elif last['close'] < last['BB_lower']:
        score += 1; reasons.append("Harga di bawah BB bawah (potensi rebound)")

    # Stochastic
    if last['%K'] > 80 and last['%D'] > 80:
        score -= 1; reasons.append("Overbought (Stochastic)")
    elif last['%K'] < 20 and last['%D'] < 20:
        score += 1; reasons.append("Oversold (Stochastic)")

    # ADX
    if last['ADX'] > 25:
        reasons.append("ADX kuat (trend kuat)")

    # Decision
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
# Telegram Signal Sender
# ==========================
def send_signal():
    global last_signal_time
    df = fetch_candle_data()
    if df.empty:
        bot.send_message(chat_id=CHAT_ID, text="❌ Gagal fetch candle data.")
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
    bot.send_message(chat_id=CHAT_ID, text=message)
    last_signal_time = datetime.datetime.now()


# ==========================
# Command Handlers
# ==========================
def start(update: Update, context: CallbackContext):
    update.message.reply_text(
        "🤖 Halo! Bot signal trading aktif.\n\n"
        "Perintah yang tersedia:\n"
        "• /harga → Cek harga realtime\n"
        "• /signal → Ambil sinyal terbaru\n"
        "• /status → Lihat status bot\n"
    )


def harga(update: Update, context: CallbackContext):
    price = fetch_realtime_price()
    if price:
        update.message.reply_text(f"💰 Harga {PAIR_SYMBOL} saat ini: {price}")
    else:
        update.message.reply_text("⚠️ Gagal mengambil harga realtime.")


def status(update: Update, context: CallbackContext):
    if last_signal_time:
        msg = f"✅ Bot aktif.\n🕒 Sinyal terakhir: {last_signal_time.strftime('%Y-%m-%d %H:%M:%S')}"
    else:
        msg = "✅ Bot aktif dan berjalan normal.\n❌ Belum ada sinyal dikirim."
    update.message.reply_text(msg)


def manual_signal(update: Update, context: CallbackContext):
    send_signal()
    update.message.reply_text("✅ Sinyal terbaru dikirim ke channel.")


# ==========================
# Scheduler
# ==========================
def is_working_hours():
    now = datetime.datetime.now()
    if now.weekday() >= 5:  # Sabtu/Minggu off
        return False
    start = now.replace(hour=5, minute=0, second=0, microsecond=0)
    end = (start + datetime.timedelta(days=1)) - datetime.timedelta(hours=1)
    break_start = now.replace(hour=23, minute=0, second=0, microsecond=0)
    break_end = now.replace(hour=0, minute=0, second=0, microsecond=0) + datetime.timedelta(days=1)
    return start <= now <= end and not (break_start <= now <= break_end)


def job():
    if is_working_hours():
        send_signal()


# ==========================
# Main
# ==========================
updater = Updater(BOT_TOKEN, use_context=True)
dp = updater.dispatcher

dp.add_handler(CommandHandler("start", start))
dp.add_handler(CommandHandler("harga", harga))
dp.add_handler(CommandHandler("status", status))
dp.add_handler(CommandHandler("signal", manual_signal))

# Schedule daily job jam 00:00
schedule.every().day.at("00:00").do(job)

print("🤖 Bot signal trading aktif. Menunggu jadwal 00:00 ...")

updater.start_polling()

# Loop untuk schedule
while True:
    schedule.run_pending()
    time.sleep(1)
