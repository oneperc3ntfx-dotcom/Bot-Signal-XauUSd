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
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator, MACD
from ta.volatility import AverageTrueRange

# ================== CONFIG ==================
BOT_TOKEN = "8114552558:AAFpnQEYHYa8P43g5rjOwPs5TSbjtYh9zS4"
CHAT_ID = "-1002883903673"
AUTHORIZED_USER_ID = 1305881282

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
    weekday, jam = now.weekday(), now.time()
    if weekday == 4 and jam >= time(22, 0): return False
    if weekday in [5, 6]: return False
    return True

# ================== DATA FETCHERS ==================
def fetch_twelvedata_series(symbol="XAU/USD", interval="5min", count=120):
    for _ in range(len(API_KEYS_TWELVE)):
        api_key = get_active_api_key()
        url = f"https://api.twelvedata.com/time_series?symbol={symbol}&interval={interval}&apikey={api_key}&outputsize={count}&format=JSON"
        try:
            r = requests.get(url, timeout=10)
            data = r.json()
            if data.get("status") == "error": continue
            return data.get("values", [])[::-1]
        except: continue
    return None

def fetch_realtime_price_metals_fast():
    try:
        url = f"https://metals-api.com/api/latest?access_key={API_KEY_METALS}&base=USD&symbols=XAU"
        r = requests.get(url, timeout=5).json()
        rate = r.get("rates", {}).get("XAU")
        if rate and rate > 0:
            return round(1.0 / float(rate), 2)
        return None
    except: return None

def fetch_realtime_price_twelve():
    for _ in range(len(API_KEYS_TWELVE)):
        api_key = get_active_api_key()
        url = f"https://api.twelvedata.com/time_series?symbol=XAU/USD&interval=1min&apikey={api_key}&outputsize=1&format=JSON"
        try:
            r = requests.get(url, timeout=10).json()
            if r.get("status") == "error": continue
            last = r.get("values", [])[0] if r.get("values") else None
            return float(last["close"]) if last else None
        except: continue
    return None

# ================== HELPERS ==================
def prepare_df(data):
    if not data: return None
    df = pd.DataFrame(data)
    df["datetime"] = pd.to_datetime(df["datetime"])
    df.set_index("datetime", inplace=True)
    for col in ["open","high","low","close"]:
        df[col] = df[col].astype(float)
    return df

# ---- Analisa Teknis Lengkap ----
def analyze(df, price):
    if df is None or len(df) < 20: return [], None, None, None, None, None, None

    rsi = RSIIndicator(df["close"], window=14).rsi().iloc[-1]
    ema20 = EMAIndicator(df["close"], window=20).ema_indicator().iloc[-1]
    macd_val = MACD(df["close"]).macd().iloc[-1]
    macd_sig = MACD(df["close"]).macd_signal().iloc[-1]
    atr = AverageTrueRange(df["high"], df["low"], df["close"]).average_true_range().iloc[-1]

    # Support & Resistance (20 candle terakhir)
    recent = df.tail(20)
    support = recent["low"].min()
    resistance = recent["high"].max()

    # Fibonacci Retracement (high-low terakhir 50 candle)
    fib_range = df.tail(50)
    high, low = fib_range["high"].max(), fib_range["low"].min()
    fib_618 = high - (high - low) * 0.618
    fib_500 = high - (high - low) * 0.5
    fib_382 = high - (high - low) * 0.382

    analysis = []
    analysis.append(f"RSI: {rsi:.2f} ({'Oversold' if rsi<30 else 'Overbought' if rsi>70 else 'Normal'})")
    analysis.append(f"EMA20 trend: {'Bullish' if df['close'].iloc[-1]>ema20 else 'Bearish'}")
    analysis.append(f"MACD: {'Bullish' if macd_val>macd_sig else 'Bearish'}")
    analysis.append(f"ATR(14): {atr:.2f}")
    analysis.append(f"Support: {support:.2f} | Resistance: {resistance:.2f}")
    analysis.append(f"Fibo 61.8%: {fib_618:.2f} | 50%: {fib_500:.2f} | 38.2%: {fib_382:.2f}")

    # Tentukan arah signal
    direction = "BUY" if df["close"].iloc[-1] > ema20 and macd_val > macd_sig else "SELL"

    # TP/SL dari SNR
    if direction == "BUY":
        tp_snr = resistance
        sl_snr = max(support, price - atr)
    else:
        tp_snr = support
        sl_snr = min(resistance, price + atr)

    # TP/SL dari Risk Reward (1:2) pakai ATR
    if direction == "BUY":
        sl_rr = price - atr
        tp_rr = price + (atr * 2)
    else:
        sl_rr = price + atr
        tp_rr = price - (atr * 2)

    return analysis, tp_snr, sl_snr, tp_rr, sl_rr, direction, atr

# ================== MESSAGE ==================
def format_signal(price, analysis, tp_snr, sl_snr, tp_rr, sl_rr, direction):
    analysis_text = "\n- ".join(analysis)
    arrow = "⬆️" if direction == "BUY" else "⬇️"

    now_wib = datetime.now(pytz.timezone("Asia/Jakarta")).strftime("%Y-%m-%d %H:%M:%S")
    return (
        f"📊 **Gold Trading Signal** 📊\n\n"
        f"Signal: {arrow} {direction}\n"
        f"Price: `{price}`\n\n"
        f"Analysis:\n- {analysis_text}\n\n"
        f"🎯 TP (SNR): {tp_snr:.2f}\n"
        f"🛑 SL (SNR): {sl_snr:.2f}\n\n"
        f"🎯 TP (RR 1:2): {tp_rr:.2f}\n"
        f"🛑 SL (RR 1:2): {sl_rr:.2f}\n\n"
        f"🕒 {now_wib} WIB"
    )

# ================== TASK ==================
async def send_signal(context: ContextTypes.DEFAULT_TYPE):
    if not is_bot_working_now(): return

    candles = fetch_twelvedata_series(interval="5min")
    df = prepare_df(candles)
    if df is None: return

    harga_live = fetch_realtime_price_metals_fast() or fetch_realtime_price_twelve() or df["close"].iloc[-1]
    analysis, tp_snr, sl_snr, tp_rr, sl_rr, direction, _ = analyze(df, harga_live)
    if not analysis: return

    msg = format_signal(harga_live, analysis, tp_snr, sl_snr, tp_rr, sl_rr, direction)
    await context.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")

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

    bot_app.job_queue.run_repeating(send_signal, interval=1800, first=10)  # setiap 30 menit

    print("🤖 Bot berjalan...")
    bot_app.run_polling()

if __name__ == "__main__":
    main()
