import asyncio
asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())  # untuk Python 3.12+

from flask import Flask
from threading import Thread
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
BOT_TOKEN = "7678173969:AAEUvVsRqbsHV-oUeky54CVytf_9nU9Fi5c"
CHAT_ID = "-1002903040446"  # ID channel
AUTHORIZED_USER_ID = 1305881282  # hanya kamu

DATA_DIR = "data"
DB_PATH = os.path.join(DATA_DIR, "xauusd_m5.db")
PAIR_SYMBOL = "XAUUSD"
TICK_INTERVAL_SECONDS = 60
CANDLE_INTERVAL_MIN = 5
JKT = pytz.timezone("Asia/Jakarta")

tick_buckets = {}
last_signal_time = None  # anti duplikat signal

# ================== FLASK KEEP-ALIVE ==================
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running."

def keep_alive():
    Thread(target=lambda: app.run(host='0.0.0.0', port=8080)).start()

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

# ================== FETCH PRICE ==================
def fetch_price_freeforex():
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

# ================== CANDLE AGGREGATION ==================
def floor_to_5min(dt):
    return dt.replace(minute=(dt.minute // CANDLE_INTERVAL_MIN) * CANDLE_INTERVAL_MIN, second=0, microsecond=0)

def add_tick(ts, price):
    ts_utc = ts.astimezone(pytz.utc)
    bucket = floor_to_5min(ts_utc)
    key = bucket.strftime("%Y-%m-%d %H:%M:%S")
    tick_buckets.setdefault(key, []).append(price)
    close_old_buckets()

def close_old_buckets():
    now_utc = datetime.utcnow().replace(tzinfo=pytz.utc)
    for key in list(tick_buckets.keys()):
        bucket_dt = datetime.strptime(key, "%Y-%m-%d %H:%M:%S").replace(tzinfo=pytz.utc)
        if now_utc >= bucket_dt + timedelta(minutes=CANDLE_INTERVAL_MIN):
            prices = tick_buckets.pop(key, [])
            if prices:
                insert_candle(bucket_dt, prices[0], max(prices), min(prices), prices[-1])

# ================== INDICATOR ANALYSIS ==================
def prepare_df(df):
    if df is None or len(df) < 30:
        return None
    return df

def detect_patterns(df):
    patterns = []
    if len(df) < 2: return patterns
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
        if df is None or len(df) < 30: return None, None, None, None
        rsi = RSIIndicator(df["close"], 14).rsi()
        ema9 = EMAIndicator(df["close"], 9).ema_indicator()
        ema20 = EMAIndicator(df["close"], 20).ema_indicator()
        macd_calc = MACD(df["close"], 12, 26, 9)
        macd, macdsig = macd_calc.macd(), macd_calc.macd_signal()

        df["rsi"] = rsi
        df["ema9"] = ema9
        df["ema20"] = ema20
        df["macd"] = macd
        df["macdsig"] = macdsig

        last, prev = df.iloc[-1], df.iloc[-2]
        arah = "BUY" if last["close"] > prev["close"] else "SELL"
        score = 0
        notes = []
        if last["rsi"] < 30: score += 1; notes.append("RSI Oversold")
        if last["close"] > last["ema20"]: score += 1; notes.append("Trend Up")
        if last["macd"] > last["macdsig"]: score += 1; notes.append("MACD Bullish")

        atr = AverageTrueRange(df["high"], df["low"], df["close"], 14).average_true_range().iloc[-1]
        indicators = {
            "rsi": last["rsi"],
            "ema20": last["ema20"],
            "macd": last["macd"],
            "macdsig": last["macdsig"],
            "atr": atr,
            "last_close": last["close"]
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
async def send_signal(bot_app):
    global last_signal_time
    df = get_last_n_candles(200)
    df = prepare_df(df)
    if df is None:
        print("⚠️ Not enough candle data.")
        return

    # anti duplikat
    now_hour = datetime.now(JKT).strftime("%Y-%m-%d %H:00")
    if last_signal_time == now_hour:
        print("⏸️ Already sent signal for this hour.")
        return
    last_signal_time = now_hour

    arah, score, notes, indicators = generate_signal(df)
    if arah is None: return
    patterns = detect_patterns(df)
    price = indicators["last_close"]

    if arah == "BUY":
        tp1, tp2, sl = round(price+2,2), round(price+4,2), round(price-1.2,2)
    else:
        tp1, tp2, sl = round(price-2,2), round(price-4,2), round(price+1.2,2)

    status = "🟢 KUAT" if score>=3 else ("🟡 SEDANG" if score==2 else "🔴 LEMAH")
    msg = build_message(arah, price, tp1, tp2, sl, status, indicators, patterns)
    if notes: msg += f"\n\n📝 Note:\n{notes}"

    try:
        await bot_app.bot.send_message(chat_id=CHAT_ID, text=msg)
        print(f"📨 Signal sent at {datetime.now(JKT)}")
    except Exception as e:
        print("❌ send_signal error:", e)

async def scheduled_task(bot_app):
    while True:
        now = datetime.now(JKT)
        next_hour = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        wait = (next_hour - now).total_seconds()
        print(f"⏱️ Next signal scheduled at {next_hour.strftime('%Y-%m-%d %H:%M:%S')} WIB")
        await asyncio.sleep(wait)
        await send_signal(bot_app)

# ================== BOT HANDLERS ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id == AUTHORIZED_USER_ID:
        await update.message.reply_text("✅ Bot aktif dan siap mengirim sinyal otomatis.")
    else:
        return

async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return  # tidak balas apapun

# ================== MAIN ==================
async def ticker_task():
    while True:
        price = fetch_price_freeforex()
        if price:
            now = datetime.utcnow().replace(tzinfo=pytz.utc)
            add_tick(now, price)
            print(f"tick {now.strftime('%H:%M:%S')} price={price}")
        await asyncio.sleep(TICK_INTERVAL_SECONDS)

def main():
    keep_alive()
    async def start_all():
        bot_app = ApplicationBuilder().token(BOT_TOKEN).build()
        bot_app.add_handler(CommandHandler("start", start))
        bot_app.add_handler(MessageHandler(filters.COMMAND, unknown))

        asyncio.create_task(ticker_task())
        asyncio.create_task(scheduled_task(bot_app))

        print("🤖 Bot berjalan...")
        await bot_app.run_polling()

    asyncio.run(start_all())

if __name__ == "__main__":
    main()
