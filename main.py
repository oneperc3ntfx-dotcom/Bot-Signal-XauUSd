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
BOT_TOKEN = "TOKEN_TELEGRAM_ANDA"
CHAT_ID = "-1002883903673"
AUTHORIZED_USER_ID = 1305881282

# Multiple API Keys TwelveData
API_KEYS_TWELVE = [
    "21a0860958e641cc934bec6277415088",
    "94a7d766d73f4db4a7ddf877473711c7",
    "af23649e02da42aab3e78cf343513325",
]
_current_key_index = 0

def get_active_api_key():
    global _current_key_index
    key = API_KEYS_TWELVE[_current_key_index]
    _current_key_index = (_current_key_index + 1) % len(API_KEYS_TWELVE)
    return key

# Metals-API
API_KEY_METALS = "2fzz3e9hw1rachdt6jwwo4furz1arvngsm879pg5bj9ucoe2xjjbv4l4gn72"

# Stiker panah
STICKER_BUY = "CAACAgUAAxkBAAEFQwNn1k0a1b-buy-arrow"
STICKER_SELL = "CAACAgUAAxkBAAEFQwRn1k0a1b-sell-arrow"

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
    if weekday == 4 and jam >= time(22, 0):  # Jumat malam
        return False
    if weekday in [5, 6]:  # Sabtu Minggu
        return False
    return True

# ================== DATA FETCHERS ==================
def fetch_twelvedata_series(symbol="XAU/USD", interval="5min", count=120):
    for _ in range(len(API_KEYS_TWELVE)):
        api_key = get_active_api_key()
        url = (
            f"https://api.twelvedata.com/time_series?symbol={symbol}&interval={interval}"
            f"&apikey={api_key}&outputsize={count}&format=JSON"
        )
        try:
            r = requests.get(url, timeout=10).json()
            if r.get("status") == "error":
                continue
            return r.get("values", [])[::-1]
        except Exception:
            continue
    return None

def fetch_realtime_price_metals_fast():
    try:
        url = f"https://metals-api.com/api/latest?access_key={API_KEY_METALS}&base=USD&symbols=XAU"
        r = requests.get(url, timeout=5).json()
        rate = r.get("rates", {}).get("XAU")
        if rate and rate > 0:
            return round(1.0 / float(rate), 2)
        return None
    except Exception:
        return None

# ================== HELPERS ==================
def prepare_df(data):
    try:
        df = pd.DataFrame(data)
        df["datetime"] = pd.to_datetime(df["datetime"])
        df.set_index("datetime", inplace=True)
        for col in ["open", "high", "low", "close"]:
            df[col] = df[col].astype(float)
        return df
    except Exception:
        return None

# ---- Support & Resistance ----
def calc_support_resistance(df, lookback=20):
    recent = df.tail(lookback)
    sup = recent["low"].min()
    res = recent["high"].max()
    return round(sup, 2), round(res, 2)

# ---- Fibonacci Retracement ----
def calc_fibonacci(df):
    swing_high = df["high"].max()
    swing_low = df["low"].min()
    diff = swing_high - swing_low
    levels = {
        "23.6%": round(swing_high - 0.236 * diff, 2),
        "38.2%": round(swing_high - 0.382 * diff, 2),
        "50.0%": round(swing_high - 0.5 * diff, 2),
        "61.8%": round(swing_high - 0.618 * diff, 2),
        "78.6%": round(swing_high - 0.786 * diff, 2),
    }
    return levels

# ---- Candlestick Pattern ----
def detect_candle_patterns(df):
    patterns = []
    if df is None or len(df) < 2:
        return patterns
    last, prev = df.iloc[-1], df.iloc[-2]
    def body(c): return abs(c["close"] - c["open"])
    def rng(c): return c["high"] - c["low"]
    def upper(c): return c["high"] - max(c["close"], c["open"])
    def lower(c): return min(c["close"], c["open"]) - c["low"]
    if rng(last) > 0 and body(last) <= 0.1 * rng(last):
        patterns.append("➕ Doji")
    if body(last) > 0 and lower(last) >= 2 * body(last):
        patterns.append("🔨 Hammer")
    if (
        last["close"] > last["open"]
        and prev["close"] < prev["open"]
        and last["close"] > prev["open"]
        and last["open"] < prev["close"]
    ):
        patterns.append("📈 Bullish Engulfing")
    if (
        last["close"] < last["open"]
        and prev["close"] > prev["open"]
        and last["close"] < prev["open"]
        and last["open"] > prev["close"]
    ):
        patterns.append("📉 Bearish Engulfing")
    return patterns

# ---- Chart Pattern ----
def detect_chart_pattern(df):
    closes = df["close"].tail(6).values
    if len(closes) < 6:
        return "-"
    if abs(closes[-1] - closes[-3]) < 0.5 and abs(closes[-2] - closes[-4]) < 0.5:
        return "⚖️ Double Top/Bottom"
    return "-"

# ---- Generate Signal ----
def generate_signal(df):
    if df is None or len(df) < 20:
        return None, None, None
    rsi = RSIIndicator(df["close"], window=14).rsi()
    ema20 = EMAIndicator(df["close"], window=20).ema_indicator()
    macd = MACD(close=df["close"], window_slow=26, window_fast=12, window_sign=9)
    df["rsi"], df["ema20"], df["macd"], df["macdsig"] = rsi, ema20, macd.macd(), macd.macd_signal()
    df.dropna(inplace=True)
    last, prev = df.iloc[-1], df.iloc[-2]

    score, notes = 0, []
    if last["rsi"] < 30 and last["close"] > last["ema20"]:
        score += 1; notes.append("RSI oversold + harga di atas EMA20")
    if last["close"] > prev["close"]:
        score += 1; notes.append("Candle naik dari sebelumnya")
    if last["macd"] > last["macdsig"]:
        score += 1; notes.append("MACD bullish")

    arah = "BUY" if last["close"] > prev["close"] else "SELL"
    return arah, score, notes

# ---- Format Signal ----
def format_signal(symbol, arah, price, tp, sl, score, df, patterns, sr, fib, chartpat):
    last = df.iloc[-1]
    rsi_val = last["rsi"]
    ema20_val = last["ema20"]
    atr = AverageTrueRange(df["high"], df["low"], df["close"], window=14).average_true_range().iloc[-1]
    konf = "🟢 KUAT" if score >= 2 else ("🟡 MODERAT" if score == 1 else "🔴 LEMAH")
    pat = ", ".join(patterns) if patterns else "-"
    fib_txt = " | ".join([f"{k}: {v}" for k, v in fib.items()])

    now_wib = datetime.now(pytz.timezone("Asia/Jakarta")).strftime("%Y-%m-%d %H:%M:%S")
    return (
        f"📊 **Trading Signal {symbol}** 📊\n"
        f"🕒 {now_wib} WIB\n\n"
        f"🎯 Arah : **{arah}**\n"
        f"💵 Entry : `{price}`\n"
        f"🎯 Target : `{tp}`\n"
        f"🛑 Stoploss : `{sl}`\n\n"
        f"📈 RSI : {rsi_val:.1f} | EMA20 : {ema20_val:.2f}\n"
        f"📉 ATR(14) : {atr:.2f}\n"
        f"🕯️ Candle Pattern : {pat}\n"
        f"📊 Chart Pattern : {chartpat}\n"
        f"📌 Support : {sr[0]} | Resistance : {sr[1]}\n"
        f"📐 Fibonacci : {fib_txt}\n"
        f"⚡ Confidence : {konf} ({score}/3)\n"
    )

# ================== TASK ==================
async def send_signal(context: ContextTypes.DEFAULT_TYPE):
    if not is_bot_working_now():
        return
    df = prepare_df(fetch_twelvedata_series(interval="5min"))
    arah, score, notes = generate_signal(df)
    if arah is None:
        return
    harga = fetch_realtime_price_metals_fast() or df["close"].iloc[-1]
    tp = round(harga + 2.0, 2) if arah == "BUY" else round(harga - 2.0, 2)
    sl = round(harga - 1.0, 2) if arah == "BUY" else round(harga + 1.0, 2)
    patterns = detect_candle_patterns(df.tail(3))
    sr = calc_support_resistance(df)
    fib = calc_fibonacci(df.tail(50))
    chartpat = detect_chart_pattern(df)

    msg = format_signal("XAU/USD", arah, harga, tp, sl, score, df, patterns, sr, fib, chartpat)
    if notes:
        msg += "\n📝 Catatan:\n- " + "\n- ".join(notes)

    await context.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")
    # kirim stiker panah
    if arah == "BUY":
        await context.bot.send_sticker(chat_id=CHAT_ID, sticker=STICKER_BUY)
    else:
        await context.bot.send_sticker(chat_id=CHAT_ID, sticker=STICKER_SELL)

# ================== HANDLERS ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != AUTHORIZED_USER_ID:
        await update.message.reply_text("❌ Anda tidak berhak pakai bot ini.")
        return
    await update.message.reply_text("✅ Bot aktif, sinyal akan dikirim setiap 30 menit.")

async def manual_signal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id == AUTHORIZED_USER_ID:
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
    bot_app.job_queue.run_repeating(send_signal, interval=1800, first=10)
    print("🤖 Bot berjalan...")
    bot_app.run_polling()

if __name__ == "__main__":
    main()
