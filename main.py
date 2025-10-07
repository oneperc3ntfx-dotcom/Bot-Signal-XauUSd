import asyncio
import threading
from flask import Flask
import requests
from datetime import datetime, timedelta
import pytz
import sqlite3
import os
import pandas as pd

from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters
)
from ta.momentum import RSIIndicator, StochasticOscillator
from ta.trend import EMAIndicator, MACD
from ta.volatility import AverageTrueRange

# ================== CONFIG ==================
BOT_TOKEN = "8114552558:AAFpnQEYHYa8P43g5rjOwPs5TSbjtYh9zS4"
CHAT_ID = "-1002903040446"  # ID channel
AUTHORIZED_USER_ID = 1305881282  # hanya kamu

# Finnhub API key (sesuai yang kamu berikan)
FINNHUB_API_KEY = "d3ih5cpr01qmn7fk333gd3ih5cpr01qmn7fk3340"

DATA_DIR = "data"
DB_PATH = os.path.join(DATA_DIR, "xauusd_m5.db")
PAIR_SYMBOL = "XAUUSD"
TICK_INTERVAL_SECONDS = 60
CANDLE_INTERVAL_MIN = 5
JKT = pytz.timezone("Asia/Jakarta")

tick_buckets = {}
last_signal_time = None  # untuk mencegah duplikat

# ================== FLASK KEEP-ALIVE ==================
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running."

def keep_alive():
    threading.Thread(target=lambda: app.run(host='0.0.0.0', port=8080), daemon=True).start()

# ================== DB SETUP ==================
def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS candles (
            dt TEXT PRIMARY KEY,
            open REAL, high REAL, low REAL, close REAL
        )
    """)
    conn.commit()
    return conn

db_conn = init_db()

def insert_candle(dt, o, h, l, c):
    try:
        cur = db_conn.cursor()
        cur.execute(
            "INSERT OR REPLACE INTO candles (dt, open, high, low, close) VALUES (?, ?, ?, ?, ?)",
            (dt.strftime("%Y-%m-%d %H:%M:%S"), o, h, l, c)
        )
        db_conn.commit()
        print(f"✅ Inserted {dt} O:{o} H:{h} L:{l} C:{c}")
    except Exception as e:
        print("❌ insert_candle error:", e)

def get_last_n_candles(n=200):
    cur = db_conn.cursor()
    cur.execute("SELECT dt, open, high, low, close FROM candles ORDER BY dt DESC LIMIT ?", (n,))
    rows = cur.fetchall()
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["datetime", "open", "high", "low", "close"])
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.set_index("datetime").sort_index()
    for c in ["open", "high", "low", "close"]:
        df[c] = df[c].astype(float)
    return df

# ================== PRICE SOURCES ==================
def fetch_price_finnhub():
    try:
        # Finnhub symbol for gold spot (OANDA feed)
        symbol = "OANDA:XAU_USD"
        url = f"https://finnhub.io/api/v1/quote?symbol={symbol}&token={FINNHUB_API_KEY}"
        r = requests.get(url, timeout=8)
        r.raise_for_status()
        j = r.json()
        # 'c' is current price
        if "c" in j and j["c"] and float(j["c"]) > 0:
            return float(j["c"])
        else:
            print("⚠️ Finnhub returned no price or zero:", j)
    except Exception as e:
        print("❌ fetch_price_finnhub error:", e)
    return None

def fetch_price_freeforex():
    # fallback simple source (keamanan jika Finnhub error)
    try:
        url = f"https://www.freeforexapi.com/api/live?pairs={PAIR_SYMBOL}"
        r = requests.get(url, timeout=8)
        r.raise_for_status()
        j = r.json()
        if "rates" in j and PAIR_SYMBOL in j["rates"]:
            return float(j["rates"][PAIR_SYMBOL]["rate"])
    except Exception as e:
        print("❌ fetch_price_freeforex error:", e)
    return None

def fetch_price():
    price = fetch_price_finnhub()
    if price is None:
        price = fetch_price_freeforex()
    return price

# ================== CANDLE AGGREGATION ==================
def floor_to_5min(dt):
    return dt.replace(minute=(dt.minute // CANDLE_INTERVAL_MIN) * CANDLE_INTERVAL_MIN, second=0, microsecond=0)

def add_tick(ts, price):
    # ts expected timezone-aware (UTC)
    ts_utc = ts.astimezone(pytz.utc)
    bucket = floor_to_5min(ts_utc)
    key = bucket.strftime("%Y-%m-%d %H:%M:%S")
    tick_buckets.setdefault(key, []).append(price)
    close_old_buckets()

def close_old_buckets():
    now_utc = datetime.utcnow().replace(tzinfo=pytz.utc)
    for key in list(tick_buckets.keys()):
        bucket_dt = datetime.strptime(key, "%Y-%m-%d %H:%M:%S").replace(tzinfo=pytz.utc)
        # jika bucket sudah lewat 5 menit (sudah selesai), simpan ke DB
        if now_utc >= bucket_dt + timedelta(minutes=CANDLE_INTERVAL_MIN):
            prices = tick_buckets.pop(key, [])
            if prices:
                try:
                    insert_candle(bucket_dt, prices[0], max(prices), min(prices), prices[-1])
                except Exception as e:
                    print("❌ Error inserting aggregated candle:", e)

# ================== INDICATOR ANALYSIS ==================
def prepare_df(df):
    if df is None or len(df) < 30:
        return None
    return df

def detect_patterns(df):
    patterns = []
    if df is None or len(df) < 2:
        return patterns
    last, prev = df.iloc[-1], df.iloc[-2]
    body = abs(last["close"] - last["open"])
    rng = last["high"] - last["low"]
    if rng > 0 and body <= 0.1 * rng:
        patterns.append("➕ Doji")
    if body > 0 and (last["low"] < prev["low"]) and (last["close"] > last["open"]):
        patterns.append("📈 Bullish candle")
    if body > 0 and (last["high"] > prev["high"]) and (last["close"] < last["open"]):
        patterns.append("📉 Bearish candle")
    return patterns

def generate_signal(df):
    try:
        if df is None or len(df) < 30:
            return None, None, None, None

        # hitung indikator pada keseluruhan df agar indikator punya history
        rsi = RSIIndicator(df["close"], 14).rsi()
        ema9 = EMAIndicator(df["close"], 9).ema_indicator()
        ema20 = EMAIndicator(df["close"], 20).ema_indicator()
        macd_calc = MACD(df["close"], 12, 26, 9)
        macd = macd_calc.macd()
        macd_sig = macd_calc.macd_signal()

        # buat salinan kecil (tidak mengubah df asli)
        df_work = df.copy()
        df_work.loc[:, "rsi"] = rsi
        df_work.loc[:, "ema9"] = ema9
        df_work.loc[:, "ema20"] = ema20
        df_work.loc[:, "macd"] = macd
        df_work.loc[:, "macdsig"] = macd_sig
        df_work = df_work.dropna()
        if df_work.empty or len(df_work) < 2:
            print("⚠️ generate_signal: not enough valid rows after indicators")
            return None, None, None, None

        last = df_work.iloc[-1]
        prev = df_work.iloc[-2]

        arah = "BUY" if last["close"] > prev["close"] else "SELL"
        score = 0
        notes = []
        if last["rsi"] < 30 and last["close"] > last["ema9"]:
            score += 1; notes.append("RSI oversold + >EMA9")
        if last["close"] > prev["close"]:
            score += 1; notes.append("Harga naik vs candle sebelumnya")
        if last["close"] > last["ema20"]:
            score += 1; notes.append(">EMA20 (trend naik)")
        if last["macd"] > last["macdsig"]:
            score += 1; notes.append("MACD bullish")

        # tambahan indikator ATR & Stoch (dihitung pada seluruh series lalu ambil value terakhir)
        try:
            stoch = StochasticOscillator(df["high"], df["low"], df["close"], window=14, smooth_window=3)
            k_val = float(stoch.stoch().iloc[-1])
            d_val = float(stoch.stoch_signal().iloc[-1])
        except Exception:
            k_val = d_val = float("nan")
        try:
            atr = float(AverageTrueRange(df["high"], df["low"], df["close"], 14).average_true_range().iloc[-1])
        except Exception:
            atr = float("nan")

        indicators = {
            "rsi": float(last["rsi"]),
            "ema9": float(last["ema9"]),
            "ema20": float(last["ema20"]),
            "macd": float(last["macd"]),
            "macdsig": float(last["macdsig"]),
            "stoch_k": k_val,
            "stoch_d": d_val,
            "atr": atr,
            "last_close": float(last["close"]),
        }
        return arah, score, "\n".join(notes), indicators
    except Exception as e:
        print("❌ generate_signal error:", e)
        return None, None, None, None

# ================== MESSAGE BUILDER ==================
def build_message(arah, price, tp1, tp2, sl, status, indicators, patterns):
    now = datetime.now(JKT).strftime("%H:%M:%S")
    pat = ", ".join(patterns) if patterns else "-"
    msg = (
        f"📡 Sinyal XAU/USD\n"
        f"🕒 {now} WIB\n"
        f"📈 Arah: {arah}\n"
        f"💰 Harga: {price}\n"
        f"🎯 TP1: {tp1} | TP2: {tp2}\n"
        f"🛑 SL: {sl}\n"
        f"📊 Status: {status}\n\n"
        f"📊 RSI: {indicators['rsi']:.2f}\n"
        f"📈 MACD: {'bullish' if indicators['macd']>indicators['macdsig'] else 'bearish'}\n"
        f"📉 Trend: {'up' if price>indicators['ema20'] else 'down'}\n"
        f"📊 ATR: {indicators['atr']:.2f}\n"
        f"🕯️ Pattern: {pat}"
    )
    return msg

# ================== SIGNAL TASK ==================
async def send_signal(app_bot):
    global last_signal_time
    df = get_last_n_candles(200)
    df = prepare_df(df)
    if df is None:
        print("⚠️ Not enough candle data for analysis.")
        return

    # hindari duplikat: cek last_signal_time hours
    now = datetime.now(JKT)
    if last_signal_time and (now - last_signal_time) < timedelta(minutes=50):
        # safety check: jika sudah mengirim dalam 50 menit terakhir skip
        print("⏸️ Signal recently sent, skipping.")
        return

    arah, score, notes, indicators = generate_signal(df)
    if arah is None:
        print("⚠️ generate_signal returned None.")
        return

    patterns = detect_patterns(df)
    price = indicators["last_close"]

    if arah == "BUY":
        tp1, tp2, sl = round(price + 2.0, 2), round(price + 4.0, 2), round(price - 1.2, 2)
    else:
        tp1, tp2, sl = round(price - 2.0, 2), round(price - 4.0, 2), round(price + 1.2, 2)

    status = "🟢 KUAT" if score >= 3 else ("🟡 SEDANG" if score == 2 else "🔴 LEMAH")
    msg = build_message(arah, price, tp1, tp2, sl, status, indicators, patterns)
    if notes:
        msg += f"\n\n📝 Note:\n{notes}"

    try:
        await app_bot.bot.send_message(chat_id=CHAT_ID, text=msg)
        last_signal_time = datetime.now(JKT)
        print(f"✅ Signal sent at {last_signal_time}")
    except Exception as e:
        print("❌ send_signal error:", e)

# ================== LOOP TASKS ==================
async def ticker_task():
    while True:
        price = fetch_price()
        if price:
            now = datetime.utcnow().replace(tzinfo=pytz.utc)
            add_tick(now, price)
            print(f"tick {now.strftime('%H:%M:%S')} price={price}")
        await asyncio.sleep(TICK_INTERVAL_SECONDS)

async def schedule_task(app_bot):
    # kirim setiap jam di menit 00 (00:00, 01:00, 02:00, ...)
    while True:
        now = datetime.now(JKT)
        next_run = now.replace(minute=0, second=0, microsecond=0)
        if now >= next_run:
            next_run = next_run + timedelta(hours=1)
        wait = (next_run - now).total_seconds()
        print(f"⏱️ Next scheduled signal at {next_run.strftime('%Y-%m-%d %H:%M:%S')} WIB (in {int(wait)}s)")
        await asyncio.sleep(wait)
        await send_signal(app_bot)

# ================== BOT HANDLERS ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id == AUTHORIZED_USER_ID:
        await update.message.reply_text("✅ Bot aktif dan siap mengirim sinyal otomatis.")
    else:
        await update.message.reply_text("👋 Halo.")

async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return  # ignore unknown commands

# ================== RUN BOT (thread-safe) ==================
def run_bot():
    async def main():
        app_bot = ApplicationBuilder().token(BOT_TOKEN).build()
        app_bot.add_handler(CommandHandler("start", start))
        app_bot.add_handler(MessageHandler(filters.COMMAND, unknown))

        # start background tasks inside the bot loop
        asyncio.create_task(ticker_task())
        asyncio.create_task(schedule_task(app_bot))

        print("🤖 Telegram bot starting (polling)...")
        await app_bot.run_polling()

    # run the bot loop inside this thread
    asyncio.run(main())

if __name__ == "__main__":
    keep_alive()               # Flask keep-alive in background thread
    threading.Thread(target=run_bot, daemon=True).start()  # Bot runs in separate thread
    print("🤖 Bot & keep-alive started. Main thread idle.")
    # keep main thread alive (so container doesn't exit)
    try:
        while True:
            # just sleep main thread
            threading.Event().wait(3600)
    except KeyboardInterrupt:
        print("Shutdown requested, exiting...")

