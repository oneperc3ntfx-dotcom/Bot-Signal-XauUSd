#!/usr/bin/env python3
"""
Main bot script:
- Uses Twelve Data for price & historical 5-min candles.
- Aggregates ticks into 5-min candles (also can load historical candles on startup).
- Sends signal immediately on deploy if within working hours (Mon-Fri 07:00-21:00 WIB),
  and every hour at minute :00 within working hours.
- Persists candles to data/candles.csv (simple CSV).
- No sqlite dependency.
"""
import os
import asyncio
import random
from threading import Thread
from datetime import datetime, timedelta
import pytz
import requests
import pandas as pd
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

# TA libs
from ta.momentum import RSIIndicator, StochasticOscillator
from ta.trend import EMAIndicator, MACD
from ta.volatility import AverageTrueRange

# --------------- CONFIG (from ENV) ---------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
AUTHORIZED_USER_ID = int(os.environ.get("AUTHORIZED_USER_ID", "0"))
TD_API_KEYS = [k.strip() for k in os.environ.get("TD_API_KEYS", "").split(",") if k.strip()]
PAIR_SYMBOL = os.environ.get("PAIR_SYMBOL", "XAU/USD")
FLASK_PORT = int(os.environ.get("PORT", "8080"))
DATA_DIR = os.environ.get("DATA_DIR", "data")
CANDLES_CSV = os.path.join(DATA_DIR, "candles.csv")

# runtime config
TICK_INTERVAL_SECONDS = int(os.environ.get("TICK_INTERVAL_SECONDS", "60"))
CANDLE_INTERVAL_MIN = int(os.environ.get("CANDLE_INTERVAL_MIN", "5"))
JKT = pytz.timezone("Asia/Jakarta")

if not BOT_TOKEN or not CHAT_ID or not TD_API_KEYS:
    raise SystemExit("ERROR: BOT_TOKEN, CHAT_ID and TD_API_KEYS must be set in environment")

# --------------- simple key rotation ---------------
_td_key_index = random.randrange(len(TD_API_KEYS)) if TD_API_KEYS else 0
def _next_td_key():
    global _td_key_index
    if not TD_API_KEYS:
        return None
    _td_key_index = (_td_key_index + 1) % len(TD_API_KEYS)
    return TD_API_KEYS[_td_key_index]

# --------------- Flask keep-alive ---------------
app = Flask(__name__)
@app.route("/")
def home():
    return "Bot is running."

def keep_alive():
    Thread(target=lambda: app.run(host="0.0.0.0", port=FLASK_PORT), daemon=True).start()

# --------------- candle storage (CSV) ---------------
os.makedirs(DATA_DIR, exist_ok=True)

def save_candles_df(df: pd.DataFrame):
    try:
        df_sorted = df.sort_index()
        df_sorted.to_csv(CANDLES_CSV, float_format="%.6f")
    except Exception as e:
        print("❌ save_candles_df error:", e)

def load_candles_df():
    try:
        if not os.path.exists(CANDLES_CSV):
            return None
        df = pd.read_csv(CANDLES_CSV, parse_dates=["datetime"])
        df = df.set_index("datetime").sort_index()
        for c in ["open", "high", "low", "close"]:
            df[c] = df[c].astype(float)
        return df
    except Exception as e:
        print("❌ load_candles_df error:", e)
        return None

# --------------- Twelve Data fetchers ---------------
def fetch_price_twelvedata():
    try:
        key = _next_td_key()
        if not key:
            return None
        url = "https://api.twelvedata.com/price"
        params = {"symbol": PAIR_SYMBOL, "apikey": key}
        r = requests.get(url, params=params, timeout=8)
        r.raise_for_status()
        j = r.json()
        if "price" in j:
            return float(j["price"])
        else:
            print("⚠️ TwelveData /price response:", j)
    except Exception as e:
        print("❌ fetch_price_twelvedata error:", e)
    return None

def fetch_historical_5m(outputsize=500):
    try:
        key = _next_td_key()
        if not key:
            return None
        url = "https://api.twelvedata.com/time_series"
        params = {
            "symbol": PAIR_SYMBOL,
            "interval": f"{CANDLE_INTERVAL_MIN}min",
            "outputsize": str(outputsize),
            "format": "JSON",
            "apikey": key
        }
        r = requests.get(url, params=params, timeout=12)
        r.raise_for_status()
        j = r.json()
        if "values" not in j:
            print("⚠️ TwelveData /time_series response:", j)
            return None
        values = j["values"]
        rows = []
        for v in reversed(values):
            rows.append({
                "datetime": pd.to_datetime(v["datetime"]),
                "open": float(v["open"]),
                "high": float(v["high"]),
                "low": float(v["low"]),
                "close": float(v["close"])
            })
        df = pd.DataFrame(rows).set_index("datetime").sort_index()
        return df
    except Exception as e:
        print("❌ fetch_historical_5m error:", e)
        return None

# --------------- tick aggregation (in-memory) ---------------
tick_buckets = {}
def floor_to_bucket(dt_utc):
    return dt_utc.replace(minute=(dt_utc.minute // CANDLE_INTERVAL_MIN) * CANDLE_INTERVAL_MIN, second=0, microsecond=0)

def add_tick(ts_utc, price):
    key = floor_to_bucket(ts_utc)
    tick_buckets.setdefault(key, []).append(price)
    close_old_buckets()

def close_old_buckets():
    now_utc = datetime.utcnow().replace(tzinfo=pytz.utc)
    keys = list(tick_buckets.keys())
    for k in keys:
        if now_utc >= (k + timedelta(minutes=CANDLE_INTERVAL_MIN)):
            prices = tick_buckets.pop(k, [])
            if prices:
                o, h, l, c = prices[0], max(prices), min(prices), prices[-1]
                df = load_candles_df()
                arr = {"datetime": [k], "open":[o], "high":[h], "low":[l], "close":[c]}
                new = pd.DataFrame(arr).set_index("datetime")
                if df is None:
                    df = new
                else:
                    df = pd.concat([df, new])
                    df = df[~df.index.duplicated(keep="last")]
                save_candles_df(df)
                print(f"🕯️ Closed bucket {k} O:{o} H:{h} L:{l} C:{c}")

# --------------- indicators & signal logic ---------------
def prepare_df(df):
    if df is None or len(df) < 30:
        return None
    return df

def detect_patterns(df):
    patterns = []
    if df is None or len(df) < 2:
        return patterns
    last = df.iloc[-1]
    prev = df.iloc[-2]
    body = abs(last["close"] - last["open"])
    rng = last["high"] - last["low"]
    if rng > 0 and body <= 0.1 * rng:
        patterns.append("Doji")
    if body > 0 and (last["low"] < prev["low"]) and (last["close"] > last["open"]):
        patterns.append("Bullish candle")
    if body > 0 and (last["high"] > prev["high"]) and (last["close"] < last["open"]):
        patterns.append("Bearish candle")
    return patterns

def generate_signal(df):
    try:
        if df is None or len(df) < 30:
            return None, None, None, None
        rsi = RSIIndicator(df["close"], 14).rsi()
        ema9 = EMAIndicator(df["close"], 9).ema_indicator()
        ema20 = EMAIndicator(df["close"], 20).ema_indicator()
        macd_calc = MACD(df["close"], 12, 26, 9)
        macd = macd_calc.macd()
        macd_sig = macd_calc.macd_signal()

        df_work = df.copy()
        df_work["rsi"] = rsi
        df_work["ema9"] = ema9
        df_work["ema20"] = ema20
        df_work["macd"] = macd
        df_work["macdsig"] = macd_sig
        df_work = df_work.dropna()
        if df_work.empty or len(df_work) < 2:
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

def build_message(arah, price, tp1, tp2, sl, status, indicators, patterns, score, notes):
    now = datetime.now(JKT).strftime("%Y-%m-%d %H:%M:%S")
    pat = ", ".join(patterns) if patterns else "-"
    macd_state = "bullish" if indicators["macd"] > indicators["macdsig"] else "bearish"
    trend_state = "up" if price > indicators["ema20"] else "down"
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
        f"- ATR: {indicators['atr']:.6f}\n"
        f"- Pattern: {pat}\n\n"
        f"HARAP GUNAKAN MONEY MANAGEMENT , JANGAN FULL MARGIN\n"
        f"(KETIKA MENGIKUTI SIGNAL HARAP SS DAN TINGGALKAN DI KOMENTAR)"
    )
    return msg

# --------------- working hours ---------------
def is_working_time(now_jkt: datetime):
    wd_ok = now_jkt.weekday() <= 4
    hr_ok = 7 <= now_jkt.hour <= 21
    return wd_ok and hr_ok

# --------------- send signal ---------------
last_signal_time = None

async def send_signal(app_bot, force=False):
    global last_signal_time
    df = load_candles_df()
    if df is None or len(df) < 60:
        fetched = fetch_historical_5m(outputsize=500)
        if fetched is not None:
            df = fetched
            save_candles_df(df)
            print("✅ Loaded historical candles from Twelve Data")
    df = prepare_df(df)
    now = datetime.now(JKT)
    if last_signal_time and (now - last_signal_time) < timedelta(minutes=50):
        print("⏸️ Signal recently sent; skipping.")
        return
    if not force and not is_working_time(now):
        print(f"⏱️ Outside working hours ({now.strftime('%Y-%m-%d %H:%M:%S')} WIB). Skipping.")
        return
    arah, score, notes, indicators = generate_signal(df)
    if arah is None:
        price = fetch_price_twelvedata() or float("nan")
        msg = (
            f"📡 Sinyal XAU/USD\n"
            f"🕒 {now.strftime('%Y-%m-%d %H:%M:%S')} WIB\n"
            f"⚠️ Tidak cukup data historis untuk analisis teknikal. Harga realtime: {price}\n\n"
            f"HARAP GUNAKAN MONEY MANAGEMENT , JANGAN FULL MARGIN\n"
            f"(KETIKA MENGIKUTI SIGNAL HARAP SS DAN TINGGALKAN DI KOMENTAR)"
        )
        try:
            await app_bot.bot.send_message(chat_id=CHAT_ID, text=msg)
            last_signal_time = datetime.now(JKT)
            print("✅ Fallback minimal signal sent.")
        except Exception as e:
            print("❌ send_signal fallback error:", e)
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

# --------------- background tasks ---------------
async def ticker_task():
    while True:
        price = fetch_price_twelvedata()
        if price:
            now_utc = datetime.utcnow().replace(tzinfo=pytz.utc)
            add_tick(now_utc, price)
            print(f"tick {now_utc.strftime('%Y-%m-%d %H:%M:%S')} price={price}")
        await asyncio.sleep(TICK_INTERVAL_SECONDS)

async def schedule_task(app_bot):
    now_jkt = datetime.now(JKT)
    if is_working_time(now_jkt):
        print("🚀 First-run during working hours: sending immediate signal.")
        await send_signal(app_bot, force=False)
    else:
        print("⏸ First-run not in working hours: immediate signal skipped.")
    while True:
        now = datetime.now(JKT)
        next_run = now.replace(minute=0, second=0, microsecond=0)
        if now >= next_run:
            next_run = next_run + timedelta(hours=1)
        wait = (next_run - now).total_seconds()
        print(f"⏱ Next scheduled signal at {next_run.strftime('%Y-%m-%d %H:%M:%S')} WIB (in {int(wait)}s)")
        await asyncio.sleep(wait)
        now_exec = datetime.now(JKT)
        if is_working_time(now_exec):
            await send_signal(app_bot)
        else:
            print(f"⏱️ {now_exec.strftime('%Y-%m-%d %H:%M:%S')} WIB outside working hours, not sending.")

# --------------- telegram handlers ---------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user and user.id == AUTHORIZED_USER_ID:
        await update.message.reply_text("✅ Bot aktif dan siap mengirim sinyal otomatis.")
    else:
        await update.message.reply_text("👋 Halo.")

async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return

# --------------- run bot ---------------
def main():
    keep_alive()
    app_bot = ApplicationBuilder().token(BOT_TOKEN).build()
    app_bot.add_handler(CommandHandler("start", start_cmd))
    app_bot.add_handler(MessageHandler(filters.COMMAND, unknown))

    async def start_background_tasks(app_bot):
        asyncio.create_task(ticker_task())
        asyncio.create_task(schedule_task(app_bot))

    app_bot.post_init = start_background_tasks

    print("🤖 Telegram bot starting (polling)...")
    app_bot.run_polling()

if __name__ == "__main__":
    main()
