import asyncio
asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())  # untuk Python 3.12+

from flask import Flask
from threading import Thread
import requests
from datetime import datetime, timedelta, time as dtime
import pytz
import sqlite3
import os
import pandas as pd
import random

from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters
)

from ta.momentum import RSIIndicator, StochasticOscillator
from ta.trend import EMAIndicator, MACD
from ta.volatility import AverageTrueRange

# ================== CONFIG ==================
BOT_TOKEN = "8114552558:AAFpnQEYHYa8P43g5rjOwPs5TSbjtYh9zS4"
CHAT_ID = "-1003142698012"  # ID channel
AUTHORIZED_USER_ID = 1305881282  # hanya kamu

DATA_DIR = "data"
DB_PATH = os.path.join(DATA_DIR, "xauusd_m5.db")
PAIR_SYMBOL = "XAU/USD"  # Twelve Data expects "XAU/USD"
TICK_INTERVAL_SECONDS = 60
CANDLE_INTERVAL_MIN = 5
JKT = pytz.timezone("Asia/Jakarta")

# Twelve Data API keys (rotasi)
TD_API_KEYS = [
    "21a0860958e641cc934bec6277415088",
    "af23649e02da42aab3e78cf343513325",
    "94a7d766d73f4db4a7ddf877473711c7"
]
_td_key_index = 0

tick_buckets = {}
last_signal_time = None  # anti duplikat signal

# ================== FLASK KEEP-ALIVE ==================
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running."

def keep_alive():
    Thread(target=lambda: app.run(host='0.0.0.0', port=8080), daemon=True).start()

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

# ================== PRICE SOURCE (Twelve Data) ==================
def _next_td_key():
    global _td_key_index
    # simple round-robin or randomize to spread load
    _td_key_index = (_td_key_index + 1) % len(TD_API_KEYS)
    return TD_API_KEYS[_td_key_index]

def fetch_price_twelvedata():
    """
    Use Twelve Data /price endpoint to get latest price for XAU/USD (e.g. "XAU/USD").
    Endpoint: https://api.twelvedata.com/price?symbol=XAU/USD&apikey=...
    """
    try:
        key = _next_td_key()
        url = "https://api.twelvedata.com/price"
        params = {"symbol": PAIR_SYMBOL, "apikey": key}
        r = requests.get(url, params=params, timeout=8)
        r.raise_for_status()
        j = r.json()
        # expected: {"price":"1920.12"} or {"status":"error", "message":...}
        if "price" in j:
            return float(j["price"])
        else:
            print("⚠️ TwelveData response:", j)
    except Exception as e:
        print("❌ fetch_price_twelvedata error:", e)
    return None

def fetch_price():
    # primary: Twelve Data
    return fetch_price_twelvedata()

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

        # compute indicators on full series
        rsi = RSIIndicator(df["close"], 14).rsi()
        ema9 = EMAIndicator(df["close"], 9).ema_indicator()
        ema20 = EMAIndicator(df["close"], 20).ema_indicator()
        macd_calc = MACD(df["close"], 12, 26, 9)
        macd = macd_calc.macd()
        macd_sig = macd_calc.macd_signal()

        # copy and attach indicator values aligned to index
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
            score += 1; notes.append("RSI oversold + close > EMA9")
        if last["close"] > prev["close"]:
            score += 1; notes.append("Harga naik vs candle sebelumnya")
        if last["close"] > last["ema20"]:
            score += 1; notes.append("Close > EMA20 (trend naik)")
        if last["macd"] > last["macdsig"]:
            score += 1; notes.append("MACD bullish crossover")

        # stochastic & atr from full df
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
def build_message(arah, price, tp1, tp2, sl, status, indicators, patterns, score, notes):
    now = datetime.now(JKT).strftime("%Y-%m-%d %H:%M:%S")
    pat = ", ".join(patterns) if patterns else "-"
    macd_state = "bullish" if indicators['macd'] > indicators['macdsig'] else 'bearish'
    trend_state = 'up' if price > indicators['ema20'] else 'down'
    reason_text = notes if notes else "-"
    msg = (
        f"📡 Sinyal XAU/USD\n"
        f"🕒 {now} WIB\n"
        f"📈 Arah: {arah}\n"
        f"💰 Harga (realtime): {price}\n"
        f"🎯 TP1: {tp1} | TP2: {tp2}\n"
        f"🛑 SL: {sl}\n"
        f"📊 Status: {status}\n\n"
        f"🔎 Reason (score {score}):\n{reason_text}\n\n"
        f"📊 Indikator:\n"
        f"- RSI: {indicators['rsi']:.2f}\n"
        f"- MACD: {macd_state}\n"
        f"- Trend (vs EMA20): {trend_state}\n"
        f"- ATR: {indicators['atr']:.4f}\n"
        f"- Pattern: {pat}\n\n"
        f"HARAP GUNAKAN MONEY MANAGEMENT , JANGAN FULL MARGIN\n"
        f"(KETIKA MENGIKUTI SIGNAL HARAP SS DAN TINGGALKAN DI KOMENTAR)"
    )
    return msg

# ================== WORK-HOURS CHECK ==================
def is_working_time(now_jkt: datetime):
    # Mon-Fri (weekday 0..4), hours 07..21 inclusive
    weekday_ok = now_jkt.weekday() <= 4
    hour_ok = 7 <= now_jkt.hour <= 21
    return weekday_ok and hour_ok

# ================== SIGNAL TASK ==================
async def send_signal(app_bot, force=False):
    """
    force=True akan mengirim tanpa cek jam kerja (dipakai untuk immediate-on-deploy
    jika kamu ingin override). Secara default kita hanya kirim selama jam kerja.
    """
    global last_signal_time
    df = get_last_n_candles(200)
    df = prepare_df(df)
    if df is None:
        print("⚠️ Not enough candle data for analysis.")
        return

    now = datetime.now(JKT)
    # hindari duplikat dengan jangka waktu 50 menit
    if last_signal_time and (now - last_signal_time) < timedelta(minutes=50):
        print("⏸️ Signal recently sent, skipping.")
        return

    # respect working hours unless forced
    if not force and not is_working_time(now):
        print(f"⏱️ Now {now.strftime('%Y-%m-%d %H:%M:%S')} WIB outside working hours, skipping send.")
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
    msg = build_message(arah, price, tp1, tp2, sl, status, indicators, patterns, score, notes)
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
    """
    Mengirim setiap jam di menit 00 (00:00, 01:00, ...), namun hanya selama
    Senin-Jumat jam 07:00-21:00 WIB. Juga pastikan saat pertama kali deploy,
    jika saat deploy berada di jam kerja, bot mengirim signal segera.
    """
    # kirim segera sekali saat start jika saat itu jam kerja
    now_jkt = datetime.now(JKT)
    if is_working_time(now_jkt):
        print("🚀 First-run during working hours: sending immediate signal.")
        await send_signal(app_bot, force=False)
    else:
        print("⏸ First-run not in working hours: immediate signal skipped.")

    while True:
        now = datetime.now(JKT)
        # compute next top-of-hour at minute 00
        next_run = now.replace(minute=0, second=0, microsecond=0)
        if now >= next_run:
            next_run = next_run + timedelta(hours=1)
        wait = (next_run - now).total_seconds()
        print(f"⏱️ Next scheduled signal at {next_run.strftime('%Y-%m-%d %H:%M:%S')} WIB (in {int(wait)}s)")
        await asyncio.sleep(wait)
        # at top of hour now, check if within working hours and weekday
        now_exec = datetime.now(JKT)
        if is_working_time(now_exec):
            await send_signal(app_bot)
        else:
            print(f"⏱️ {now_exec.strftime('%Y-%m-%d %H:%M:%S')} WIB outside working hours, not sending.")

# ================== BOT HANDLERS ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id == AUTHORIZED_USER_ID:
        await update.message.reply_text("✅ Bot aktif dan siap mengirim sinyal otomatis.")
    else:
        await update.message.reply_text("👋 Halo.")

async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return  # ignore unknown commands

# ================== RUN BOT (main thread) ==================
async def main_bot():
    app_bot = ApplicationBuilder().token(BOT_TOKEN).build()
    app_bot.add_handler(CommandHandler("start", start))
    app_bot.add_handler(MessageHandler(filters.COMMAND, unknown))

    # start background tasks inside the bot loop
    asyncio.create_task(ticker_task())
    asyncio.create_task(schedule_task(app_bot))

    print("🤖 Telegram bot starting (polling)...")
    await app_bot.run_polling()

if __name__ == "__main__":
    # start flask keep-alive in background thread
    keep_alive()

    # run bot and asyncio tasks in main thread event loop
    try:
        asyncio.run(main_bot())
    except (KeyboardInterrupt, SystemExit):
        print("Shutdown requested, exiting...")
