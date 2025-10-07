import asyncio
asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())  # Untuk Python 3.12+

from flask import Flask
from threading import Thread
import requests
from datetime import datetime, timedelta
import pytz
import sqlite3
import os
import time

from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters
)

import pandas as pd
from ta.momentum import RSIIndicator, StochasticOscillator
from ta.trend import EMAIndicator, MACD
from ta.volatility import AverageTrueRange

# ================== CONFIG ==================
BOT_TOKEN = "8114552558:AAFpnQEYHYa8P43g5rjOwPs5TSbjtYh9zS4"
CHAT_ID = "-1002883903673"
AUTHORIZED_USER_ID = 1305881282

DATA_DIR = "data"
DB_PATH = os.path.join(DATA_DIR, "xauusd_m5.db")
PAIR_SYMBOL = "XAUUSD"  # freeforex uses XAUUSD
TICK_INTERVAL_SECONDS = 60  # ambil harga tiap 60 detik
CANDLE_INTERVAL_MIN = 5  # M5

JKT = pytz.timezone("Asia/Jakarta")

# in-memory tick buckets: {bucket_time_iso: [prices]}
tick_buckets = {}

# ================== FLASK KEEP-ALIVE ==================
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running"

def keep_alive():
    Thread(target=lambda: app.run(host='0.0.0.0', port=8080)).start()

# ================== DB HELPERS ==================
def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS candles (
            dt TEXT PRIMARY KEY,  -- ISO timestamp aligned to candle start UTC
            open REAL,
            high REAL,
            low REAL,
            close REAL
        )
    """)
    conn.commit()
    return conn

db_conn = init_db()

def insert_candle(dt: datetime, o, h, l, c):
    iso = dt.strftime("%Y-%m-%d %H:%M:%S")
    cur = db_conn.cursor()
    try:
        cur.execute("INSERT OR REPLACE INTO candles (dt, open, high, low, close) VALUES (?, ?, ?, ?, ?)",
                    (iso, o, h, l, c))
        db_conn.commit()
        print(f"✅ Inserted candle {iso} O:{o} H:{h} L:{l} C:{c}")
        return True
    except Exception as e:
        print("❌ insert_candle error:", e)
        return False

def get_last_n_candles(n=100):
    cur = db_conn.cursor()
    cur.execute("SELECT dt, open, high, low, close FROM candles ORDER BY dt DESC LIMIT ?", (n,))
    rows = cur.fetchall()
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["datetime", "open", "high", "low", "close"])
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.set_index("datetime").sort_index()
    for col in ["open", "high", "low", "close"]:
        df[col] = df[col].astype(float)
    return df

def get_last_candle_time():
    cur = db_conn.cursor()
    cur.execute("SELECT dt FROM candles ORDER BY dt DESC LIMIT 1")
    row = cur.fetchone()
    if not row:
        return None
    return datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")

# ================== FREEFOREX API (tick fetch) ==================
def fetch_price_freeforex():
    try:
        url = f"https://www.freeforexapi.com/api/live?pairs={PAIR_SYMBOL}"
        r = requests.get(url, timeout=8)
        r.raise_for_status()
        j = r.json()
        if "rates" in j and PAIR_SYMBOL in j["rates"]:
            rate = float(j["rates"][PAIR_SYMBOL]["rate"])
            return rate
        print("❌ freeforex invalid response:", j)
    except Exception as e:
        print("❌ fetch_price_freeforex error:", e)
    return None

# ================== BUCKET / CANDLE LOGIC ==================
def floor_to_5min(dt: datetime):
    # floor to nearest multiple of 5 minutes (UTC)
    minute = (dt.minute // CANDLE_INTERVAL_MIN) * CANDLE_INTERVAL_MIN
    return dt.replace(minute=minute, second=0, microsecond=0)

def add_tick_to_bucket(ts: datetime, price: float):
    # use UTC bucket key
    ts_utc = ts.astimezone(pytz.utc)
    bucket = floor_to_5min(ts_utc)
    key = bucket.strftime("%Y-%m-%d %H:%M:%S")
    if key not in tick_buckets:
        tick_buckets[key] = []
    tick_buckets[key].append((ts_utc, price))
    # attempt to close buckets older than one full interval
    try_close_buckets()

def try_close_buckets():
    now_utc = datetime.utcnow().replace(tzinfo=pytz.utc)
    keys_to_close = []
    for key in list(tick_buckets.keys()):
        bucket_dt = datetime.strptime(key, "%Y-%m-%d %H:%M:%S").replace(tzinfo=pytz.utc)
        # close bucket if bucket end < now (i.e., bucket <= now - interval)
        if bucket_dt + timedelta(minutes=CANDLE_INTERVAL_MIN) <= now_utc:
            keys_to_close.append(key)
    for k in keys_to_close:
        ticks = tick_buckets.pop(k, [])
        if ticks:
            # aggregate
            prices = [p for (_t, p) in ticks]
            o = prices[0]
            c = prices[-1]
            h = max(prices)
            l = min(prices)
        else:
            # no ticks: fallback to last DB close
            last_df = get_last_n_candles(1)
            if last_df is None:
                continue
            last_close = float(last_df["close"].iloc[-1])
            o = h = l = c = last_close
        bucket_dt = datetime.strptime(k, "%Y-%m-%d %H:%M:%S").replace(tzinfo=pytz.utc)
        # store as UTC ISO; analysis uses this DB
        insert_candle(bucket_dt, o, h, l, c)
        # after insert, trigger analysis/send signal
        # we cannot await here (called from sync context), so schedule task in event loop
        try:
            loop = asyncio.get_event_loop()
            loop.create_task(on_new_candle(bucket_dt))
        except Exception as e:
            print("❌ schedule on_new_candle failed:", e)

# ================== ANALYSIS & SIGNAL ==================
def prepare_df_for_analysis(df: pd.DataFrame):
    # ensure enough rows and proper types
    if df is None or len(df) < 30:
        return None
    return df

def detect_candle_patterns_from_df(df: pd.DataFrame):
    # reuse simple patterns logic (last 3)
    patterns = []
    if df is None or len(df) < 2:
        return patterns
    d = df.copy()
    last = d.iloc[-1]
    prev = d.iloc[-2]
    def body(c): return abs(c["close"] - c["open"])
    def range_(c): return c["high"] - c["low"]
    def upper_wick(c): return c["high"] - max(c["close"], c["open"])
    def lower_wick(c): return min(c["close"], c["open"]) - c["low"]

    if range_(last) > 0 and body(last) <= 0.1 * range_(last):
        patterns.append("➕ Doji")
    if body(last) > 0 and lower_wick(last) >= 2 * body(last) and upper_wick(last) <= body(last):
        patterns.append("🔨 Hammer")
    if body(last) > 0 and upper_wick(last) >= 2 * body(last) and lower_wick(last) <= body(last):
        patterns.append("🌠 Shooting Star" if last["close"] < last["open"] else "🪓 Inverted Hammer")
    if (last["close"] > last["open"] and prev["close"] < prev["open"] and last["close"] > prev["open"] and last["open"] < prev["close"]):
        patterns.append("📈 Bullish Engulfing")
    if (last["close"] < last["open"] and prev["close"] > prev["open"] and last["close"] < prev["open"] and last["open"] > prev["close"]):
        patterns.append("📉 Bearish Engulfing")
    return patterns

def generate_signal_from_df(df: pd.DataFrame):
    # copy of your generate_signal but adapted to use stored df
    if df is None or len(df) < 30:
        print("⚠️ generate_signal: insufficient data length for indicators")
        return None, None, None, None
    try:
        df_full = df.copy()
        rsi_full = RSIIndicator(df_full["close"], window=14).rsi()
        ema9_full = EMAIndicator(df_full["close"], window=9).ema_indicator()
        ema20_full = EMAIndicator(df_full["close"], window=20).ema_indicator()
        macd_calc_full = MACD(close=df_full["close"], window_slow=26, window_fast=12, window_sign=9)
        macd_full = macd_calc_full.macd()
        macd_sig_full = macd_calc_full.macd_signal()

        df_analyze = df_full.tail(7).copy()
        df_analyze.loc[:, "rsi"] = rsi_full.reindex(df_analyze.index)
        df_analyze.loc[:, "ema9"] = ema9_full.reindex(df_analyze.index)
        df_analyze.loc[:, "ema20"] = ema20_full.reindex(df_analyze.index)
        df_analyze.loc[:, "macd"] = macd_full.reindex(df_analyze.index)
        df_analyze.loc[:, "macdsig"] = macd_sig_full.reindex(df_analyze.index)
        df_analyze = df_analyze.dropna()
        if df_analyze.empty or len(df_analyze) < 2:
            print("⚠️ generate_signal: not enough valid rows after indicator calculation")
            return None, None, None, None

        last = df_analyze.iloc[-1]
        prev = df_analyze.iloc[-2]
        score = 0
        notes = []
        try:
            if last["rsi"] < 30 and last["close"] > last["ema9"]:
                score += 1; notes.append("RSI oversold + >EMA9")
        except Exception:
            pass
        if last["close"] > prev["close"]:
            score += 1; notes.append("Harga naik vs candle sebelumnya")
        if last["close"] > last["ema20"]:
            score += 1; notes.append(">EMA20 (trend naik)")
        if last["macd"] > last["macdsig"]:
            score += 1; notes.append("MACD bullish")

        arah = "BUY" if last["close"] > prev["close"] else "SELL"

        # Stochastic & ATR
        try:
            stoch_full = StochasticOscillator(df_full["high"], df_full["low"], df_full["close"], window=14, smooth_window=3)
            k_full = stoch_full.stoch()
            d_full = stoch_full.stoch_signal()
            k_val = float(k_full.reindex(df_analyze.index).iloc[-1])
            d_val = float(d_full.reindex(df_analyze.index).iloc[-1])
        except Exception:
            k_val = d_val = float("nan")
        try:
            atr_full = AverageTrueRange(df_full["high"], df_full["low"], df_full["close"], window=14).average_true_range()
            atr = float(atr_full.reindex(df_analyze.index).iloc[-1])
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
        print("❌ Error generate_signal_from_df:", e)
        return None, None, None, None

def build_scalping_message(arah, price, tp1, tp2, sl, status_text, indicators, patterns, sr_high, sr_low, fibo):
    now_wib = datetime.now(JKT).strftime("%H:%M:%S")
    pat_txt = ", ".join(patterns) if patterns else "-"
    trend_txt = "bullish" if indicators["last_close"] > indicators["ema20"] else "bearish"
    msg = (
        f"📡 Sinyal XAU/USD\n"
        f"🕒 {now_wib} WIB\n"
        f"📈 Arah: {arah}\n"
        f"💰 Harga: {price}\n"
        f"🎯 TP1: {tp1} | TP2: {tp2}\n"
        f"🛑 SL: {sl}\n"
        f"📊 Status: {status_text}\n"
        f"\n🔍 Analisa:\n"
        f"{'✅' if indicators['macd']>indicators['macdsig'] else '❌'} MACD {'bullish' if indicators['macd']>indicators['macdsig'] else 'bearish'}\n"
        f"{'⚠️' if indicators['rsi']>70 else ('⚠️' if indicators['rsi']<30 else 'ℹ️')} RSI {indicators['rsi']:.1f}\n"
        f"{'📈' if indicators['last_close']>indicators['ema20'] else '📉'} Tren {trend_txt} (Price vs EMA20)\n"
        f"📊 ATR(14): {indicators['atr']:.2f}\n"
        f"🕯️ Candle: {pat_txt}\n"
        f"🧭 S/R: R {sr_high} | S {sr_low}\n"
        f"🔢 Fibo(0.382/0.618): {fibo['0.382']} / {fibo['0.618']}\n"
    )
    return msg

# ================== ON NEW CANDLE (async) ==================
async def on_new_candle(bucket_dt):
    # called whenever a new M5 candle is stored
    # run analysis using last N candles and send signal
    df = get_last_n_candles(200)
    df_analysis = prepare_df_for_analysis(df)
    if df_analysis is None:
        print("⏳ on_new_candle: not enough historical data yet.")
        return
    arah, score, notes, indicators = generate_signal_from_df(df_analysis)
    if arah is None:
        print("⚠️ on_new_candle: signal generation failed.")
        return
    patterns = detect_candle_patterns_from_df(df_analysis.tail(7))
    sr_high, sr_low = df_analysis["high"].tail(30).max(), df_analysis["low"].tail(30).min()
    fibo = {
        "0.382": round(sr_high - 0.382 * (sr_high - sr_low), 2),
        "0.618": round(sr_high - 0.618 * (sr_high - sr_low), 2),
    }
    price_live = indicators["last_close"]

    if arah == "BUY":
        tp1 = round(price_live + 2.0, 2)
        tp2 = round(price_live + 4.0, 2)
        sl  = round(price_live - 1.2, 2)
    else:
        tp1 = round(price_live - 2.0, 2)
        tp2 = round(price_live - 4.0, 2)
        sl  = round(price_live + 1.2, 2)

    status_text = "🟢 KUAT" if score >= 3 else ("🟡 SEDANG" if score == 2 else "🔴 LEMAH")

    msg = build_scalping_message(
        arah=arah,
        price=price_live,
        tp1=tp1, tp2=tp2, sl=sl,
        status_text=status_text,
        indicators=indicators,
        patterns=patterns,
        sr_high=round(sr_high, 2),
        sr_low=round(sr_low, 2),
        fibo=fibo
    )
    if notes:
        extra = "\n".join([f"• {line}" for line in notes.split("\n")][:2])
        msg += f"\n📝 Note:\n{extra}"

    # send to Telegram
    try:
        app_bot = ApplicationBuilder().token(BOT_TOKEN).build()
        async with app_bot:
            await app_bot.bot.send_message(chat_id=CHAT_ID, text=msg)
        print("📨 Signal sent.")
    except Exception as e:
        print("❌ send signal error:", e)

# ================== SCHEDULED TICKER (async) ==================
async def ticker_task():
    while True:
        try:
            price = fetch_price_freeforex()
            now = datetime.utcnow().replace(tzinfo=pytz.utc)
            if price is not None:
                add_tick_to_bucket(now, price)
                print(f"tick {now.strftime('%Y-%m-%d %H:%M:%S')} price={price}")
            else:
                print("⚠️ ticker: price fetch returned None")
        except Exception as e:
            print("❌ ticker_task loop error:", e)
        await asyncio.sleep(TICK_INTERVAL_SECONDS)

# ================== BOT HANDLERS ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != AUTHORIZED_USER_ID:
        await update.message.reply_text("❌ Anda tidak berhak pakai bot ini.")
        return
    await update.message.reply_text("✅ Bot berjalan!")

async def manual_signal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != AUTHORIZED_USER_ID:
        await update.message.reply_text("❌ Anda tidak berhak pakai bot ini.")
        return
    # run analysis immediately on demand
    df = get_last_n_candles(200)
    df_analysis = prepare_df_for_analysis(df)
    if df_analysis is None:
        await update.message.reply_text("⏳ Data historis belum cukup untuk analisa.")
        return
    arah, score, notes, indicators = generate_signal_from_df(df_analysis)
    if arah is None:
        await update.message.reply_text("❌ Gagal membuat sinyal sekarang.")
        return
    patterns = detect_candle_patterns_from_df(df_analysis.tail(7))
    sr_high, sr_low = df_analysis["high"].tail(30).max(), df_analysis["low"].tail(30).min()
    fibo = {"0.382": round(sr_high - 0.382 * (sr_high - sr_low), 2), "0.618": round(sr_high - 0.618 * (sr_high - sr_low), 2)}
    price_live = indicators["last_close"]
    if arah == "BUY":
        tp1 = round(price_live + 2.0, 2); tp2 = round(price_live + 4.0, 2); sl = round(price_live - 1.2, 2)
    else:
        tp1 = round(price_live - 2.0, 2); tp2 = round(price_live - 4.0, 2); sl = round(price_live + 1.2, 2)
    status_text = "🟢 KUAT" if score >= 3 else ("🟡 SEDANG" if score == 2 else "🔴 LEMAH")
    msg = build_scalping_message(arah, price_live, tp1, tp2, sl, status_text, indicators, patterns, round(sr_high,2), round(sr_low,2), fibo)
    if notes:
        extra = "\n".join([f"• {line}" for line in notes.split("\n")][:2])
        msg += f"\n📝 Note:\n{extra}"
    await update.message.reply_text(msg)

async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❓ Perintah tidak dikenal.")

# ================== MAIN ==================
def main():
    keep_alive()

    bot_app = ApplicationBuilder().token(BOT_TOKEN).build()
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CommandHandler("signal", manual_signal))
    bot_app.add_handler(MessageHandler(filters.COMMAND, unknown))

    # Start ticker_task in background when bot starts
    async def runner():
        # start ticker
        task = asyncio.create_task(ticker_task())
        await task

    print("🤖 Bot berjalan...")
    # run the bot and background tasks
    bot_app.run_polling(bootstrap_retries=-1, close_loop=False, run_async=True)
    # run our runner in event loop
    loop = asyncio.get_event_loop()
    loop.create_task(runner())
    loop.run_forever()

if __name__ == "__main__":
    main()
