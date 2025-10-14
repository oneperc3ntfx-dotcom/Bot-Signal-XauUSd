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
from ta.momentum import RSIIndicator, StochasticOscillator
from ta.trend import EMAIndicator, MACD
from ta.volatility import AverageTrueRange

# ====================
# CONFIG
# ====================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
AUTHORIZED_USER_ID = int(os.environ.get("AUTHORIZED_USER_ID", "0"))
FINNHUB_TOKEN = os.environ.get("FINNHUB_TOKEN")
TD_API_KEYS = os.environ.get("TD_API_KEYS", "").split(",")
PAIR_SYMBOL = os.environ.get("PAIR_SYMBOL", "XAU/USD")
CANDLE_INTERVAL_MIN = int(os.environ.get("CANDLE_INTERVAL_MIN", "5"))
JKT = pytz.timezone("Asia/Jakarta")
DATA_DIR = os.environ.get("DATA_DIR", "data")
CANDLES_CSV = os.path.join(DATA_DIR, "candles.csv")

if not BOT_TOKEN or not CHAT_ID or not FINNHUB_TOKEN or not TD_API_KEYS:
    raise SystemExit("ERROR: BOT_TOKEN, CHAT_ID, FINNHUB_TOKEN, TD_API_KEYS harus di-set")

os.makedirs(DATA_DIR, exist_ok=True)

# ====================
# Candle storage
# ====================
def save_candles_df(df: pd.DataFrame):
    df.sort_index().to_csv(CANDLES_CSV, float_format="%.6f")

def load_candles_df():
    if not os.path.exists(CANDLES_CSV):
        return None
    df = pd.read_csv(CANDLES_CSV, parse_dates=["datetime"]).set_index("datetime")
    for c in ["open","high","low","close"]:
        df[c] = df[c].astype(float)
    return df

# ====================
# Fetch candles from Twelve Data
# ====================
def fetch_candles_td():
    key = TD_API_KEYS[0]
    start = datetime.now(JKT).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(minutes=55)
    url = f"https://api.twelvedata.com/time_series?symbol={PAIR_SYMBOL}&interval={CANDLE_INTERVAL_MIN}min&apikey={key}&start_date={start.strftime('%Y-%m-%d %H:%M:%S')}&end_date={end.strftime('%Y-%m-%d %H:%M:%S')}&format=JSON"
    try:
        r = requests.get(url, timeout=10).json()
    except Exception as e:
        print("⚠️ Error fetch candles:", e)
        return None
    if r.get("status") != "ok":
        print("⚠️ Failed fetch candles:", r)
        return None
    data = r.get("values", [])
    if not data:
        return None
    df = pd.DataFrame(data)
    df["datetime"] = pd.to_datetime(df["datetime"])
    for c in ["open","high","low","close"]:
        df[c] = df[c].astype(float)
    df = df.set_index("datetime").sort_index()
    save_candles_df(df)
    return df

# ====================
# Indicators & Signal
# ====================
def prepare_df(df):
    if df is None:
        return None
    return df.tail(12)

def detect_patterns(df):
    pats = []
    if df is None or len(df)<2:
        return pats
    last, prev = df.iloc[-1], df.iloc[-2]
    body = abs(last["close"]-last["open"])
    rng = last["high"]-last["low"]
    if rng>0 and body<=0.1*rng:
        pats.append("Doji")
    if body>0 and last["low"]<prev["low"] and last["close"]>last["open"]:
        pats.append("Bullish candle")
    if body>0 and last["high"]>prev["high"] and last["close"]<last["open"]:
        pats.append("Bearish candle")
    return pats

def generate_signal(df):
    if df is None or len(df)<2:
        return None, None, None, None
    n = len(df)
    ema9 = EMAIndicator(df["close"], min(9, max(2,n))).ema_indicator()
    ema20 = EMAIndicator(df["close"], min(20, max(2,n))).ema_indicator()
    macd_calc = MACD(df["close"], 12, 26, 9)
    macd = macd_calc.macd()
    macd_sig = macd_calc.macd_signal()
    if n>=14:
        rsi = RSIIndicator(df["close"],14).rsi()
        stoch = StochasticOscillator(df["high"], df["low"], df["close"],14,3)
        k_val = float(stoch.stoch().iloc[-1])
        d_val = float(stoch.stoch_signal().iloc[-1])
        atr = float(AverageTrueRange(df["high"], df["low"], df["close"],14).average_true_range().iloc[-1])
    else:
        rsi = pd.Series([50.0]*n, index=df.index)
        k_val,d_val,atr = 50.0,50.0,0.0

    dfw = df.copy()
    dfw["rsi"], dfw["ema9"], dfw["ema20"], dfw["macd"], dfw["macdsig"] = rsi, ema9, ema20, macd, macd_sig
    dfw = dfw.dropna()
    if len(dfw)<2:
        return None,None,None,None
    last, prev = dfw.iloc[-1], dfw.iloc[-2]
    arah = "BUY" if last["close"]>prev["close"] else "SELL"
    score=0
    notes=[]
    if last["rsi"]<30 and last["close"]>last["ema9"]:
        score+=1; notes.append("RSI oversold + close > EMA9")
    if last["close"]>prev["close"]:
        score+=1; notes.append("Harga naik vs candle sebelumnya")
    if last["close"]>last["ema20"]:
        score+=1; notes.append("Close > EMA20 (trend naik)")
    if last["macd"]>last["macdsig"]:
        score+=1; notes.append("MACD bullish crossover")
    indicators={
        "rsi":float(last["rsi"]),
        "ema9":float(last["ema9"]),
        "ema20":float(last["ema20"]),
        "macd":float(last["macd"]),
        "macdsig":float(last["macdsig"]),
        "stoch_k":k_val,
        "stoch_d":d_val,
        "atr":atr,
        "last_close":float(last["close"])
    }
    return arah, score, "\n".join(notes), indicators

def build_message(arah,price,tp1,tp2,sl,status,ind,pat,score,notes,fake=False):
    now = datetime.now(JKT).strftime("%Y-%m-%d %H:%M:%S")
    ptxt = ", ".join(pat) if pat else "-"
    if fake:
        return f"📡 Sinyal XAU/USD (FAKE)\n🕒 {now} WIB\n🔎 Bot berhasil dijalankan — ini FAKE signal untuk konfirmasi deploy.\nHARAP GUNAKAN MONEY MANAGEMENT."
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

# ====================
# Working time check
# ====================
def is_working_time(now_jkt):
    wd=now_jkt.weekday()
    return wd<5

# ====================
# Send signal
# ====================
last_signal_date=None
last_price=None

def fetch_price_finnhub():
    global last_price
    try:
        url=f"https://finnhub.io/api/v1/quote?symbol=OANDA:XAU_USD&token={FINNHUB_TOKEN}"
        r=requests.get(url,timeout=5).json()
        last_price=r.get("c",0.0)
        last_price=float(last_price)
    except:
        last_price=0.0
    return last_price

async def send_signal(app_bot,fake=False):
    global last_signal_date
    df=load_candles_df()
    price=fetch_price_finnhub() if not fake else 0.0
    if df is None or len(df)<2:
        df=pd.DataFrame({"open":[price,price],"high":[price,price],"low":[price,price],"close":[price,price]},
                        index=[datetime.utcnow(),datetime.utcnow()])
    df_for_signal=prepare_df(df)
    if fake:
        msg=build_message(None,price,None,None,None,None,None,None,0,None,fake=True)
    else:
        arah,score,notes,ind=generate_signal(df_for_signal)
        if arah is None:
            print("⚠️ generate_signal tidak menghasilkan sinyal, kirim FAKE signal")
            msg=build_message(None,price,None,None,None,None,None,None,0,None,fake=True)
        else:
            pat=detect_patterns(df_for_signal)
            pip=0.01
            if arah=="BUY":
                tp1=round(price+25*pip,3)
                tp2=round(price+50*pip,3)
                sl=round(price-15*pip,3)
            else:
                tp1=round(price-25*pip,3)
                tp2=round(price-50*pip,3)
                sl=round(price+15*pip,3)
            status="🟢 KUAT" if score>=3 else ("🟡 SEDANG" if score==2 else "🔴 LEMAH")
            msg=build_message(arah,price,tp1,tp2,sl,status,ind,pat,score,notes,fake=False)
    try:
        await app_bot.bot.send_message(chat_id=CHAT_ID,text=msg)
    except Exception as e:
        print("⚠️ Gagal mengirim sinyal:", e)
    print(f"✅ Signal sent at {datetime.now(JKT)} (fake={fake})")
    now=datetime.now(JKT)
    if now.hour==1 and last_signal_date!=now.date():
        last_signal_date=now.date()

# ====================
# Scheduler
# ====================
async def schedule_task(app_bot):
    await send_signal(app_bot,fake=True)
    while True:
        now=datetime.now(JKT)
        next_run=now.replace(minute=0,second=0,microsecond=0)+timedelta(hours=1)
        wait=(next_run-now).total_seconds()
        await asyncio.sleep(wait)
        now_jkt=datetime.now(JKT)
        if is_working_time(now_jkt):
            await send_signal(app_bot)

# ====================
# Telegram Handlers
# ====================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u=update.effective_user
    if u and u.id==AUTHORIZED_USER_ID:
        await update.message.reply_text("✅ Bot aktif. Gunakan /signal untuk sinyal manual.")
    else:
        await update.message.reply_text("👋 Halo.")

async def signal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u=update.effective_user
    if not u or u.id!=AUTHORIZED_USER_ID:
        await update.message.reply_text("🚫 Tidak diizinkan.")
        return
    await update.message.reply_text("📡 Mengirim sinyal manual...")
    await send_signal(context.application)
    await update.message.reply_text("✅ Sinyal manual telah dikirim.")

# ====================
# Main
# ====================
def main():
    app_bot=ApplicationBuilder().token(BOT_TOKEN).build()
    app_bot.add_handler(CommandHandler("start",start_cmd))
    app_bot.add_handler(CommandHandler("signal",signal_cmd))

    async def start_tasks(app_bot):
        fetch_candles_td()
        asyncio.create_task(schedule_task(app_bot))

    app_bot.post_init=lambda app: asyncio.create_task(start_tasks(app))
    print("🤖 Telegram bot starting...")
    app_bot.run_polling()

if __name__=="__main__":
    main()
