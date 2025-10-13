#!/usr/bin/env python3
import os
import asyncio
import json
from threading import Thread
from datetime import datetime, timedelta
import pytz
import pandas as pd
import websockets
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

# === CONFIG ===
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
AUTHORIZED_USER_ID = int(os.environ.get("AUTHORIZED_USER_ID", "0"))
FINNHUB_TOKEN = os.environ.get("FINNHUB_TOKEN", "d3ih5cpr01qmn7fk333gd3ih5cpr01qmn7fk3340")
PAIR_SYMBOL = "OANDA:XAU_USD"
FLASK_PORT = int(os.environ.get("PORT", "8080"))
DATA_DIR = os.environ.get("DATA_DIR", "data")
CANDLES_CSV = os.path.join(DATA_DIR, "candles.csv")

CANDLE_INTERVAL_MIN = 5
JKT = pytz.timezone("Asia/Jakarta")

if not BOT_TOKEN or not CHAT_ID:
    raise SystemExit("ERROR: BOT_TOKEN and CHAT_ID must be set in environment")

# === Keep alive (Flask) ===
app = Flask(__name__)
@app.route("/")
def home():
    return "Bot is running."

def keep_alive():
    Thread(target=lambda: app.run(host="0.0.0.0", port=FLASK_PORT), daemon=True).start()

# === Candle storage ===
os.makedirs(DATA_DIR, exist_ok=True)

def save_candles_df(df: pd.DataFrame):
    try:
        df.sort_index().to_csv(CANDLES_CSV, float_format="%.6f")
    except Exception as e:
        print("❌ save_candles_df error:", e)

def load_candles_df():
    try:
        if not os.path.exists(CANDLES_CSV):
            return None
        df = pd.read_csv(CANDLES_CSV, parse_dates=["datetime"])
        df = df.set_index("datetime").sort_index()
        for c in ["open","high","low","close"]:
            df[c] = df[c].astype(float)
        return df
    except Exception as e:
        print("❌ load_candles_df error:", e)
        return None

# === Tick aggregation ===
tick_buckets = {}
last_price = None   # ✅ untuk simpan harga realtime terakhir

def floor_to_bucket(dt_utc):
    return dt_utc.replace(minute=(dt_utc.minute // CANDLE_INTERVAL_MIN)*CANDLE_INTERVAL_MIN, second=0, microsecond=0)

def add_tick(ts_utc, price):
    global last_price
    last_price = price
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
                arr = {"datetime":[k], "open":[o], "high":[h], "low":[l], "close":[c]}
                new = pd.DataFrame(arr).set_index("datetime")
                if df is None: df = new
                else:
                    df = pd.concat([df, new])
                    df = df[~df.index.duplicated(keep="last")]
                save_candles_df(df)
                print(f"🕯️ Closed bucket {k} O:{o} H:{h} L:{l} C:{c}")

# === Indicator & Signal Logic ===
from ta.momentum import RSIIndicator, StochasticOscillator
from ta.trend import EMAIndicator, MACD
from ta.volatility import AverageTrueRange

def prepare_df(df):
    if df is None or len(df) < 30:
        return None
    return df

def detect_patterns(df):
    pats = []
    if len(df) < 2: return pats
    last, prev = df.iloc[-1], df.iloc[-2]
    body = abs(last["close"] - last["open"])
    rng = last["high"] - last["low"]
    if rng>0 and body <= 0.1*rng: pats.append("Doji")
    if body>0 and (last["low"]<prev["low"]) and (last["close"]>last["open"]): pats.append("Bullish candle")
    if body>0 and (last["high"]>prev["high"]) and (last["close"]<last["open"]): pats.append("Bearish candle")
    return pats

def generate_signal(df):
    try:
        if df is None or len(df)<30: return None,None,None,None
        rsi = RSIIndicator(df["close"],14).rsi()
        ema9 = EMAIndicator(df["close"],9).ema_indicator()
        ema20 = EMAIndicator(df["close"],20).ema_indicator()
        macd_calc = MACD(df["close"],12,26,9)
        macd = macd_calc.macd(); macd_sig = macd_calc.macd_signal()
        stoch = StochasticOscillator(df["high"],df["low"],df["close"],window=14,smooth_window=3)
        k_val, d_val = float(stoch.stoch().iloc[-1]), float(stoch.stoch_signal().iloc[-1])
        atr = float(AverageTrueRange(df["high"],df["low"],df["close"],14).average_true_range().iloc[-1])

        dfw = df.copy()
        dfw["rsi"]=rsi; dfw["ema9"]=ema9; dfw["ema20"]=ema20; dfw["macd"]=macd; dfw["macdsig"]=macd_sig
        dfw = dfw.dropna()
        if len(dfw)<2: return None,None,None,None
        last, prev = dfw.iloc[-1], dfw.iloc[-2]
        arah = "BUY" if last["close"]>prev["close"] else "SELL"
        score, notes = 0, []
        if last["rsi"]<30 and last["close"]>last["ema9"]:
            score+=1; notes.append("RSI oversold + close > EMA9")
        if last["close"]>prev["close"]:
            score+=1; notes.append("Harga naik vs candle sebelumnya")
        if last["close"]>last["ema20"]:
            score+=1; notes.append("Close > EMA20 (trend naik)")
        if last["macd"]>last["macdsig"]:
            score+=1; notes.append("MACD bullish crossover")
        indicators = {
            "rsi":float(last["rsi"]),
            "ema9":float(last["ema9"]),
            "ema20":float(last["ema20"]),
            "macd":float(last["macd"]),
            "macdsig":float(last["macdsig"]),
            "stoch_k":k_val,"stoch_d":d_val,
            "atr":atr,"last_close":float(last["close"])
        }
        return arah,score,"\n".join(notes),indicators
    except Exception as e:
        print("❌ generate_signal error:",e)
        return None,None,None,None

def build_message(arah,price,tp1,tp2,sl,status,ind,pat,score,notes):
    now = datetime.now(JKT).strftime("%Y-%m-%d %H:%M:%S")
    ptxt = ", ".join(pat) if pat else "-"
    macd_state = "bullish" if ind["macd"]>ind["macdsig"] else "bearish"
    trend_state = "up" if price>ind["ema20"] else "down"
    return (
        f"📡 Sinyal XAU/USD\n🕒 {now} WIB\n"
        f"📈 Arah: {arah}\n💰 Harga (realtime): {price}\n"
        f"🎯 TP1: {tp1} | TP2: {tp2}\n🛑 SL: {sl}\n📊 Status: {status}\n\n"
        f"🔎 Reason (score {score}):\n{notes or '-'}\n\n"
        f"📊 Indikator:\n- RSI: {ind['rsi']:.2f}\n- MACD: {macd_state}\n"
        f"- Trend: {trend_state}\n- ATR: {ind['atr']:.6f}\n- Pattern: {ptxt}\n\n"
        f"HARAP GUNAKAN MONEY MANAGEMENT, JANGAN FULL MARGIN."
    )

# === Working time (Mon–Fri, 06:00–04:00 next day) ===
def is_working_time(now_jkt):
    wd = now_jkt.weekday()
    if wd >= 5:  # Sabtu/Minggu libur
        return False
    hour = now_jkt.hour
    # 06:00 - 23:59, atau 00:00 - 03:59
    return (hour >= 6) or (hour < 4)

# === Send Signal ===
last_signal_time = None

async def send_signal(app_bot):
    global last_signal_time
    df = prepare_df(load_candles_df())
    if df is None or len(df)<30:
        print("⚠️ Not enough candle data.")
        return
    arah,score,notes,ind = generate_signal(df)
    if arah is None: return
    pat = detect_patterns(df)
    price = ind["last_close"]
    if arah=="BUY":
        tp1,tp2,sl = round(price+2.0,2), round(price+4.0,2), round(price-1.2,2)
    else:
        tp1,tp2,sl = round(price-2.0,2), round(price-4.0,2), round(price+1.2,2)
    status = "🟢 KUAT" if score>=3 else ("🟡 SEDANG" if score==2 else "🔴 LEMAH")
    msg = build_message(arah,price,tp1,tp2,sl,status,ind,pat,score,notes)
    try:
        await app_bot.bot.send_message(chat_id=CHAT_ID, text=msg)
        last_signal_time = datetime.now(JKT)
        print(f"✅ Signal sent at {last_signal_time}")
    except Exception as e:
        print("❌ send_signal error:", e)

# === Scheduler ===
async def schedule_task(app_bot):
    await asyncio.sleep(5)
    print("🚀 Sending initial startup signal...")
    await send_signal(app_bot)

    while True:
        now = datetime.now(JKT)
        next_run = now + timedelta(minutes=5 - now.minute % 5)
        wait = (next_run - now).total_seconds()
        print(f"⏱ Next tick aggregation at {next_run.strftime('%Y-%m-%d %H:%M:%S')} WIB (in {int(wait)}s)")
        await asyncio.sleep(wait)

        now_jkt = datetime.now(JKT)
        if not is_working_time(now_jkt):
            print(f"⏸ Outside working hours: {now_jkt.strftime('%H:%M:%S')} WIB")
            continue

        close_old_buckets()

        if now_jkt.minute == 0:
            print(f"🚀 It's {now_jkt.strftime('%H:%M')} WIB — sending signal.")
            await send_signal(app_bot)
        else:
            print("💾 Candle updated — waiting for next 00 minute.")

# === Finnhub WebSocket ===
async def finnhub_ws():
    url = f"wss://ws.finnhub.io?token={FINNHUB_TOKEN}"
    while True:
        try:
            async with websockets.connect(url, ping_interval=None) as ws:
                sub = json.dumps({"type":"subscribe","symbol":PAIR_SYMBOL})
                await ws.send(sub)
                print(f"✅ Subscribed to {PAIR_SYMBOL} via Finnhub")
                async for msg in ws:
                    data = json.loads(msg)
                    if data.get("type")=="trade":
                        for t in data["data"]:
                            price = t["p"]
                            ts = datetime.utcfromtimestamp(t["t"]/1000).replace(tzinfo=pytz.utc)
                            add_tick(ts, price)
                            print(f"tick {ts.strftime('%Y-%m-%d %H:%M:%S')} price={price}")
        except Exception as e:
            print("⚠️ WebSocket error:", e)
            await asyncio.sleep(5)

# === Telegram Handlers ===
async def start_cmd(update:Update,context:ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if u and u.id == AUTHORIZED_USER_ID:
        await update.message.reply_text("✅ Bot aktif. Gunakan /signal untuk kirim sinyal manual atau /harga untuk lihat harga realtime.")
    else:
        await update.message.reply_text("👋 Halo.")

async def signal_cmd(update:Update, context:ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not u or u.id != AUTHORIZED_USER_ID:
        await update.message.reply_text("🚫 Tidak diizinkan.")
        return
    await update.message.reply_text("📡 Mengirim sinyal manual...")
    await send_signal(context.application)
    await update.message.reply_text("✅ Sinyal manual telah dikirim (jika data cukup).")

async def harga_cmd(update:Update, context:ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not u or u.id != AUTHORIZED_USER_ID:
        await update.message.reply_text("🚫 Tidak diizinkan.")
        return
    if last_price is None:
        await update.message.reply_text("⏳ Harga realtime belum tersedia.")
    else:
        now = datetime.now(JKT).strftime("%Y-%m-%d %H:%M:%S")
        await update.message.reply_text(f"💰 Harga realtime XAU/USD: {last_price:.3f}\n🕒 {now} WIB")

# === Main ===
def main():
    keep_alive()
    app_bot = ApplicationBuilder().token(BOT_TOKEN).build()
    app_bot.add_handler(CommandHandler("start", start_cmd))
    app_bot.add_handler(CommandHandler("signal", signal_cmd))
    app_bot.add_handler(CommandHandler("harga", harga_cmd))
    app_bot.add_handler(MessageHandler(filters.COMMAND, lambda u,c:None))

    async def start_tasks(app_bot):
        asyncio.create_task(finnhub_ws())
        asyncio.create_task(schedule_task(app_bot))

    app_bot.post_init = start_tasks
    print("🤖 Telegram bot starting (polling)...")
    app_bot.run_polling()

if __name__=="__main__":
    main()
