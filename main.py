#!/usr/bin/env python3
import os
import asyncio
import json
from datetime import datetime, timedelta
import pytz
import pandas as pd
import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ====================
# CONFIG
# ====================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
AUTHORIZED_USER_ID = int(os.environ.get("AUTHORIZED_USER_ID", "0"))
FINNHUB_TOKEN = os.environ.get("FINNHUB_TOKEN")
TWELVE_API = os.environ.get("TWELVE_API")
PAIR_SYMBOL = "XAU/USD"
JKT = pytz.timezone("Asia/Jakarta")
CANDLE_INTERVAL_MIN = 5

if not BOT_TOKEN or not CHAT_ID or not FINNHUB_TOKEN or not TWELVE_API:
    raise SystemExit("ERROR: BOT_TOKEN, CHAT_ID, FINNHUB_TOKEN, TWELVE_API harus di-set")

# ====================
# Candle Storage
# ====================
CANDLES_CSV = "candles.csv"

def save_candles(df):
    df.sort_index().to_csv(CANDLES_CSV, float_format="%.6f")

def load_candles():
    if os.path.exists(CANDLES_CSV):
        df = pd.read_csv(CANDLES_CSV, parse_dates=["datetime"])
        df.set_index("datetime", inplace=True)
        return df
    return None

# ====================
# Fetch candles from Twelve Data (batch 00:00–00:55)
# ====================
def fetch_candles_twelve():
    today = datetime.now(JKT).strftime("%Y-%m-%d")
    url = f"https://api.twelvedata.com/time_series?symbol={PAIR_SYMBOL}&interval=5min&apikey={TWELVE_API}&start_date={today}&end_date={today}&outputsize=12"
    try:
        r = requests.get(url, timeout=10).json()
        if "values" not in r:
            print("⚠️ Failed fetch candles:", r)
            return None
        candles = r["values"]
        times = [datetime.strptime(c["datetime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=JKT) for c in reversed(candles)]
        df = pd.DataFrame({
            "datetime": times,
            "open": [float(c["open"]) for c in reversed(candles)],
            "high": [float(c["high"]) for c in reversed(candles)],
            "low": [float(c["low"]) for c in reversed(candles)],
            "close": [float(c["close"]) for c in reversed(candles)]
        }).set_index("datetime")
        save_candles(df)
        return df
    except Exception as e:
        print("❌ fetch_candles_twelve error:", e)
        return None

# ====================
# Fetch realtime price from Finnhub
# ====================
def fetch_price_finnhub():
    url = f"https://finnhub.io/api/v1/quote?symbol=OANDA:XAU_USD&token={FINNHUB_TOKEN}"
    try:
        r = requests.get(url, timeout=5).json()
        return float(r.get("c", 0))
    except:
        return 0.0

# ====================
# Simple indicators + signal
# ====================
def generate_signal(df):
    if df is None or len(df) < 2:
        return {
            "arah": "BUY",
            "score": 0,
            "notes": ["Fake signal untuk testing"],
            "rsi": 0.0,
            "macd": 0.0,
            "macd_state": "bearish",
            "trend": "down",
            "atr": 0.0,
            "pattern": "-"
        }
    last, prev = df.iloc[-1], df.iloc[-2]
    body = abs(last["close"] - last["open"])
    rng = last["high"] - last["low"]
    pat = []
    if rng>0 and body <= 0.1*rng: pat.append("Doji")
    if body>0 and last["close"]>last["open"]: pat.append("Bullish candle")
    if body>0 and last["close"]<last["open"]: pat.append("Bearish candle")

    # Simple score
    score = 0
    notes = []
    if last["close"]>prev["close"]:
        score +=1
        notes.append("Harga naik vs candle sebelumnya")
    else:
        score +=0
        notes.append("Harga turun vs candle sebelumnya")
    rsi = 50.0
    macd = 0.0
    macd_state = "bullish" if macd>0 else "bearish"
    trend = "up" if last["close"]>prev["close"] else "down"
    atr = last["high"] - last["low"]
    return {
        "arah": "BUY" if last["close"]>prev["close"] else "SELL",
        "score": score,
        "notes": notes,
        "rsi": rsi,
        "macd": macd,
        "macd_state": macd_state,
        "trend": trend,
        "atr": atr,
        "pattern": ", ".join(pat) if pat else "-"
    }

# ====================
# Build Telegram message
# ====================
def build_message(sig, price, fake=False):
    now = datetime.now(JKT).strftime("%Y-%m-%d %H:%M:%S")
    pip = 0.01
    if fake or price==0:
        tp1=tp2=sl=0
        status = "🔵 FAKE"
    else:
        if sig["arah"]=="BUY":
            tp1 = round(price+25*pip, 2)
            tp2 = round(price+50*pip, 2)
            sl  = round(price-15*pip, 2)
        else:
            tp1 = round(price-25*pip, 2)
            tp2 = round(price-50*pip, 2)
            sl  = round(price+15*pip, 2)
        status = "🟢 KUAT" if sig["score"]>=1 else "🟡 SEDANG"
    msg = (
        f"📡 Sinyal XAU/USD\n"
        f"🕒 {now} WIB\n"
        f"📈 Arah: {sig['arah']}\n"
        f"💰 Harga (realtime): {price}\n"
        f"🎯 TP1: {tp1} | TP2: {tp2}\n"
        f"🛑 SL: {sl}\n"
        f"📊 Status: {status}\n\n"
        f"🔎 Reason (score {sig['score']}):\n"
        f"{'; '.join(sig['notes'])}\n\n"
        f"📊 Indikator:\n"
        f"- RSI: {sig['rsi']:.2f}\n"
        f"- MACD: {sig['macd_state']}\n"
        f"- Trend: {sig['trend']}\n"
        f"- ATR: {sig['atr']:.6f}\n"
        f"- Pattern: {sig['pattern']}\n\n"
        f"HARAP GUNAKAN MONEY MANAGEMENT, JANGAN FULL MARGIN."
    )
    return msg

# ====================
# Scheduler jam 01:00 WIB
# ====================
async def schedule(app_bot):
    await asyncio.sleep(5)
    # Fake signal saat deploy
    df = load_candles() or fetch_candles_twelve()
    price = fetch_price_finnhub()
    msg = build_message(generate_signal(df), price, fake=True)
    await app_bot.bot.send_message(chat_id=CHAT_ID, text=msg)

    while True:
        now = datetime.now(JKT)
        next_run = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        wait = (next_run - now).total_seconds()
        await asyncio.sleep(wait)
        # Hanya jam 01:00 WIB
        now = datetime.now(JKT)
        if now.hour == 1:
            df = load_candles() or fetch_candles_twelve()
            price = fetch_price_finnhub()
            msg = build_message(generate_signal(df), price)
            await app_bot.bot.send_message(chat_id=CHAT_ID, text=msg)

# ====================
# Telegram Bot
# ====================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if u and u.id == AUTHORIZED_USER_ID:
        await update.message.reply_text("✅ Bot aktif. Gunakan /signal untuk sinyal manual atau /harga untuk harga realtime.")
    else:
        await update.message.reply_text("👋 Halo.")

async def signal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not u or u.id != AUTHORIZED_USER_ID:
        await update.message.reply_text("🚫 Tidak diizinkan.")
        return
    df = load_candles() or fetch_candles_twelve()
    price = fetch_price_finnhub()
    msg = build_message(generate_signal(df), price)
    await update.message.reply_text(msg)

async def harga_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    price = fetch_price_finnhub()
    now = datetime.now(JKT).strftime("%Y-%m-%d %H:%M:%S")
    await update.message.reply_text(f"💰 Harga realtime XAU/USD: {price}\n🕒 {now} WIB")

# ====================
# Main
# ====================
def main():
    app_bot = ApplicationBuilder().token(BOT_TOKEN).build()
    app_bot.add_handler(CommandHandler("start", start_cmd))
    app_bot.add_handler(CommandHandler("signal", signal_cmd))
    app_bot.add_handler(CommandHandler("harga", harga_cmd))

    async def post_init(app_bot):
        asyncio.create_task(schedule(app_bot))

    app_bot.post_init = post_init
    print("🤖 Telegram bot running...")
    app_bot.run_polling()

if __name__ == "__main__":
    main()
