import asyncio
asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())  # Untuk Python 3.12

from flask import Flask
from threading import Thread
import requests
from datetime import datetime, time, timedelta
import pytz
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters
)
import pandas as pd
from ta.momentum import RSIIndicator, StochasticOscillator
from ta.trend import EMAIndicator, MACD
from ta.volatility import BollingerBands, AverageTrueRange
from bs4 import BeautifulSoup

# ================== CONFIG ==================
BOT_TOKEN = "8114552558:AAFpnQEYHYa8P43g5rjOwPs5TSbjtYh9zS4"
CHAT_ID = "-1002883903673"
AUTHORIZED_USER_ID = 1305881282

# Multiple API Keys TwelveData
API_KEYS_TWELVE = [
    "94a7d766d73f4db4a7ddf877473711c7",
    "af23649e02da42aab3e78cf343513325",
    "af23649e02da42aab3e78cf343513325"
]

_current_key_index = 0

def get_active_api_key():
    global _current_key_index
    key = API_KEYS_TWELVE[_current_key_index]
    _current_key_index = (_current_key_index + 1) % len(API_KEYS_TWELVE)  # round-robin
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
    weekday = now.weekday()
    jam = now.time()
    if weekday == 4 and jam >= time(22, 0):  # Jumat setelah 22:00 WIB
        return False
    if weekday in [5, 6]:  # Sabtu & Minggu
        return False
    return True

# ================== DATA FETCHER ==================
def fetch_twelvedata_series(symbol="XAU/USD", interval="5min", count=120):
    for _ in range(len(API_KEYS_TWELVE)):
        api_key = get_active_api_key()
        url = f"https://api.twelvedata.com/time_series?symbol={symbol}&interval={interval}&apikey={api_key}&outputsize={count}&format=JSON"
        try:
            response = requests.get(url, timeout=10)
            if response.status_code != 200:
                print(f"❌ Gagal ambil data TwelveData: HTTP {response.status_code}")
                continue
            data = response.json()
            if "status" in data and data["status"] == "error":
                print(f"❌ Error TwelveData: {data.get('message')}")
                continue
            return data.get("values", [])[::-1]
        except Exception as e:
            print(f"❌ Error fetch_twelvedata_series: {e}")
            continue
    return None

def fetch_realtime_price_metals():
    try:
        url = f"https://metals-api.com/api/latest?access_key={API_KEY_METALS}&base=USD&symbols=XAU"
        r = requests.get(url, timeout=10).json()
        rate = r.get("rates", {}).get("XAU")
        if rate and rate > 0:
            return round(1.0 / float(rate), 2)
        print("❌ Metals-API response:", r)
        return None
    except Exception as e:
        print(f"❌ Error fetch_realtime_price_metals: {e}")
        return None

def fetch_realtime_price_twelve():
    for _ in range(len(API_KEYS_TWELVE)):
        api_key = get_active_api_key()
        url = f"https://api.twelvedata.com/time_series?symbol=XAU/USD&interval=1min&apikey={api_key}&outputsize=1&format=JSON"
        try:
            r = requests.get(url, timeout=10).json()
            if "status" in r and r["status"] == "error":
                print(f"❌ Error TwelveData: {r.get('message')}")
                continue
            last = r.get("values", [])[0]
            return float(last["close"]) if last else None
        except Exception as e:
            print(f"❌ Error fetch_realtime_price_twelve: {e}")
            continue
    return None

def prepare_df(data):
    try:
        df = pd.DataFrame(data)
        df["datetime"] = pd.to_datetime(df["datetime"])
        df.set_index("datetime", inplace=True)
        for col in ["open", "high", "low", "close"]:
            df[col] = df[col].astype(float)
        return df
    except Exception as e:
        print(f"❌ Error prepare_df: {e}")
        return None

# ================== STRATEGI UTAMA ==================
def generate_signal(df):
    if df is None or len(df) < 20:
        print("❌ Data tidak cukup")
        return None, None, None
    try:
        rsi = RSIIndicator(df["close"], window=14).rsi()
        ema9 = EMAIndicator(df["close"], window=9).ema_indicator()
        df["rsi"] = rsi
        df["ema"] = ema9
        df.dropna(inplace=True)
        last = df.iloc[-1]
        prev = df.iloc[-2]
        note = ""
        score = 0
        if last["rsi"] < 30 and last["close"] > last["ema"]:
            score += 1
            note += "✅ RSI oversold + harga di atas EMA\n"
        if last["close"] > prev["close"]:
            score += 1
            note += "✅ Harga naik dari candle sebelumnya\n"
        if last["close"] > last["ema"]:
            score += 1
            note += "✅ Harga di atas EMA\n"
        arah = "BUY" if last["close"] > prev["close"] else "SELL"
        return arah, score, note
    except Exception as e:
        print(f"❌ Error generate_signal: {e}")
        return None, None, None

# ================== ANALISA TAMBAHAN ==================
def detect_candles(prev, last):
    notes = []
    body = abs(last["close"] - last["open"])
    range_ = last["high"] - last["low"]
    upper_wick = last["high"] - max(last["close"], last["open"])
    lower_wick = min(last["close"], last["open"]) - last["low"]

    if range_ > 0 and body <= 0.1 * range_:
        notes.append("⚠️ Doji terdeteksi")
    if body > 0 and lower_wick >= 2 * body and upper_wick <= body:
        notes.append("🔨 Hammer (bullish potensi reversal)")
    if body > 0 and upper_wick >= 2 * body and lower_wick <= body:
        notes.append("🌠 Shooting Star / Inverted Hammer (bearish potensi reversal)")
    if last["close"] > last["open"] and prev["close"] < prev["open"] and last["close"] > prev["open"] and last["open"] < prev["close"]:
        notes.append("✅ Bullish Engulfing")
    if last["close"] < last["open"] and prev["close"] > prev["open"] and last["close"] < prev["open"] and last["open"] > prev["close"]:
        notes.append("❌ Bearish Engulfing")

    return notes

def extra_analysis(df):
    try:
        ema9 = EMAIndicator(df["close"], window=9).ema_indicator()
        ema20 = EMAIndicator(df["close"], window=20).ema_indicator()
        rsi = RSIIndicator(df["close"], window=14).rsi()
        macd_calc = MACD(close=df["close"], window_slow=26, window_fast=12, window_sign=9)
        macd_val = macd_calc.macd()
        macd_sig = macd_calc.macd_signal()
        stoch = StochasticOscillator(df["high"], df["low"], df["close"], window=14, smooth_window=3)
        k = stoch.stoch()
        d = stoch.stoch_signal()
        bb = BollingerBands(df["close"], window=20, window_dev=2)
        bb_high = bb.bollinger_hband()
        bb_low = bb.bollinger_lband()
        atr = AverageTrueRange(df["high"], df["low"], df["close"], window=14).average_true_range()

        temp = df.copy()
        temp["ema9"], temp["ema20"], temp["rsi"], temp["macd"], temp["macdsig"], temp["k"], temp["d"], temp["bb_high"], temp["bb_low"], temp["atr"] = (
            ema9, ema20, rsi, macd_val, macd_sig, k, d, bb_high, bb_low, atr
        )
        temp = temp.dropna()
        last, prev = temp.iloc[-1], temp.iloc[-2]

        analysis = []
        if last["close"] >= last["bb_high"]:
            analysis.append("⚠️ Sentuh upper BB (potensi jenuh beli)")
        elif last["close"] <= last["bb_low"]:
            analysis.append("⚠️ Sentuh lower BB (potensi jenuh jual)")
        analysis.append("📈 MACD bullish" if last["macd"] > last["macdsig"] else "📉 MACD bearish")
        if last["k"] > 80:
            analysis.append("⚠️ Stochastic overbought")
        elif last["k"] < 20:
            analysis.append("⚠️ Stochastic oversold")
        elif last["k"] > last["d"]:
            analysis.append("✅ Stochastic bullish crossover")
        else:
            analysis.append("❌ Stochastic bearish crossover")
        analysis.append(f"📊 ATR: {round(last['atr'], 2)}")
        analysis += detect_candles(prev, last)
        if last["ema9"] > last["ema20"]:
            analysis.append("🟢 Tren pendek bullish (EMA9>EMA20)")
        elif last["ema9"] < last["ema20"]:
            analysis.append("🔴 Tren pendek bearish (EMA9<EMA20)")

        return "\n".join(analysis)
    except Exception as e:
        print(f"❌ Error extra_analysis: {e}")
        return ""

# ================== NEWS FILTER ==================
def check_high_impact_news():
    try:
        url = "https://www.forexfactory.com/calendar.php?week=this"
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code != 200:
            return False
        soup = BeautifulSoup(response.text, "html.parser")
        rows = soup.select("tr.calendar__row")
        now = datetime.now(pytz.timezone("Asia/Jakarta"))
        for row in rows:
            impact = row.select_one("td.calendar__impact")
            time_td = row.select_one("td.calendar__time")
            if not impact or not time_td:
                continue
            if "high" not in impact.get("title", "").lower():
                continue
            time_str = time_td.get_text(strip=True)
            if not time_str or time_str.lower() in ["all day", "tentative"]:
                continue
            try:
                news_time = datetime.strptime(time_str, "%H:%M").time()
            except:
                continue
            ny_tz = pytz.timezone("America/New_York")
            jakarta_tz = pytz.timezone("Asia/Jakarta")
            today_ny = datetime.now(ny_tz).replace(hour=news_time.hour, minute=news_time.minute, second=0, microsecond=0)
            news_jakarta_time = today_ny.astimezone(jakarta_tz)
            delta = abs((news_jakarta_time - now).total_seconds())
            if delta <= 1800:
                return True
        return False
    except Exception as e:
        print(f"❌ Error cek news: {e}")
        return False

# ================== SIGNAL ENGINE ==================
_last_strong_sent_at = None
_strong_cooldown_min = 10

async def send_signal(context: ContextTypes.DEFAULT_TYPE):
    if not is_bot_working_now():
        print("⏱️ Di luar jam kerja bot.")
        return
    if check_high_impact_news():
        await context.bot.send_message(chat_id=CHAT_ID, text="🚨 Ada berita berdampak tinggi. Sinyal diskip.")
        return
    candles = fetch_twelvedata_series(interval="5min")
    df = prepare_df(candles)
    arah, score, note = generate_signal(df)
    if arah is None:
        await context.bot.send_message(chat_id=CHAT_ID, text="❌ Gagal generate sinyal.")
        return
    harga_live = fetch_realtime_price_metals() or fetch_realtime_price_twelve() or df["close"].iloc[-1]
    tp = round(harga_live + 2.0, 2) if arah == "BUY" else round(harga_live - 2.0, 2)
    sl = round(harga_live - 1.0, 2) if arah == "BUY" else round(harga_live + 1.0, 2)
    time_now = datetime.now(pytz.timezone("Asia/Jakarta")).strftime("%H:%M:%S")
    tambahan = extra_analysis(df)
    msg = f"""📡 *Sinyal XAU/USD* 🕒 {time_now} WIB
📈 Arah: *{arah}*
💰 Harga: {harga_live}
🎯 TP: {tp} 🛑 SL: {sl}
📊 Status: {'🟢 KUAT' if score>=3 else ('🟡 MODERAT' if score==2 else '🔴 LEMAH')}
🔍 Analisa: {note}{tambahan}"""
    await context.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")

# ================== STRONG SETUP ==================
def is_strong_setup(df_m5, df_m1):
    try:
        ema9_m5 = EMAIndicator(df_m5["close"], window=9).ema_indicator()
        ema20_m5 = EMAIndicator(df_m5["close"], window=20).ema_indicator()
        rsi_m5 = RSIIndicator(df_m5["close"], window=14).rsi()
        macd_m5 = MACD(close=df_m5["close"], window_slow=26, window_fast=12, window_sign=9)
        macd_val_m5 = macd_m5.macd()
        macd_sig_m5 = macd_m5.macd_signal()
        bb_m5 = BollingerBands(df_m5["close"], window=20, window_dev=2)
        bb_high_m5 = bb_m5.bollinger_hband()
        bb_low_m5 = bb_m5.bollinger_lband()
        temp5 = df_m5.copy()
        temp5["ema9"], temp5["ema20"], temp5["rsi"], temp5["macd"], temp5["macdsig"], temp5["bb_high"], temp5["bb_low"] = (
            ema9_m5, ema20_m5, rsi_m5, macd_val_m5, macd_sig_m5, bb_high_m5, bb_low_m5
        )
        temp5 = temp5.dropna()
        l5, p5 = temp5.iloc[-1], temp5.iloc[-2]

        ema9_m1 = EMAIndicator(df_m1["close"], window=9).ema_indicator()
        ema20_m1 = EMAIndicator(df_m1["close"], window=20).ema_indicator()
        rsi_m1 = RSIIndicator(df_m1["close"], window=14).rsi()
        stoch_m1 = StochasticOscillator(df_m1["high"], df_m1["low"], df_m1["close"], window=14, smooth_window=3)
        k1, d1 = stoch_m1.stoch(), stoch_m1.stoch_signal()
        temp1 = df_m1.copy()
        temp1["ema9"], temp1["ema20"], temp1["rsi"], temp1["k"], temp1["d"] = (ema9_m1, ema20_m1, rsi_m1, k1, d1)
        temp1 = temp1.dropna()
        l1, p1 = temp1.iloc[-1], temp1.iloc[-2]

        reasons, buy_score, sell_score = [], 0, 0
        if l5["ema9"] > l5["ema20"]: buy_score += 1; reasons.append("BUY: EMA9>EMA20 (M5)")
        if l5["macd"] > l5["macdsig"]: buy_score += 1; reasons.append("BUY: MACD bullish (M5)")
        if l5["close"] <= l5["bb_low"] or l5["rsi"] < 35: buy_score += 1; reasons.append("BUY: di bawah/low BB atau RSI<35 (M5)")
        if l1["ema9"] > l1["ema20"] and l1["rsi"] > p1["rsi"]: buy_score += 1; reasons.append("BUY: timing M1 searah & RSI naik")
        if l1["k"] > l1["d"] and l1["k"] < 30: buy_score += 1; reasons.append("BUY: Stoch bullish dari area rendah (M1)")
        if l5["ema9"] < l5["ema20"]: sell_score += 1; reasons.append("SELL: EMA9<EMA20 (M5)")
        if l5["macd"] < l5["macdsig"]: sell_score += 1; reasons.append("SELL: MACD bearish (M5)")
        if l5["close"] >= l5["bb_high"] or l5["rsi"] > 65: sell_score += 1; reasons.append("SELL: di atas/high BB atau RSI>65 (M5)")
        if l1["ema9"] < l1["ema20"] and l1["rsi"] < p1["rsi"]: sell_score += 1; reasons.append("SELL: timing M1 searah & RSI turun")
        if l1["k"] < l1["d"] and l1["k"] > 70: sell_score += 1; reasons.append("SELL: Stoch bearish dari area tinggi (M1)")

        if buy_score >= 4 and buy_score > sell_score: return "BUY", reasons
        if sell_score >= 4 and sell_score > buy_score: return "SELL", reasons
        return None, reasons
    except Exception as e:
        print(f"❌ Error is_strong_setup: {e}")
        return None, []

async def monitor_strong_signal(context: ContextTypes.DEFAULT_TYPE):
    global _last_strong_sent_at
    if not is_bot_working_now(): return
    if check_high_impact_news(): return
    df_m5 = prepare_df(fetch_twelvedata_series(interval="5min"))
    df_m1 = prepare_df(fetch_twelvedata_series(interval="1min", count=120))
    if df_m5 is None or df_m1 is None: return
    arah, reasons = is_strong_setup(df_m5, df_m1)
    if arah:
        now = datetime.now(pytz.timezone("Asia/Jakarta"))
        if _last_strong_sent_at and (now - _last_strong_sent_at).total_seconds() < _strong_cooldown_min * 60:
            return
        _last_strong_sent_at = now
        harga_live = fetch_realtime_price_metals() or fetch_realtime_price_twelve() or df_m5["close"].iloc[-1]
        tp = round(harga_live + 3.0, 2) if arah == "BUY" else round(harga_live - 3.0, 2)
        sl = round(harga_live - 1.5, 2) if arah == "BUY" else round(harga_live + 1.5, 2)
        msg = f"""🚨 *STRONG {arah} SETUP* 🚨
💰 Harga: {harga_live}
🎯 TP: {tp} 🛑 SL: {sl}
Alasan:\n- """ + "\n- ".join(reasons)
        await context.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")

# ================== HANDLER ==================
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
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("signal", manual_signal))
    app.add_handler(MessageHandler(filters.COMMAND, unknown))
    app.job_queue.run_repeating(send_signal, interval=300, first=10)
    app.job_queue.run_repeating(monitor_strong_signal, interval=60, first=20)
    print("🤖 Bot berjalan...")
    app.run_polling()

if __name__ == "__main__":
    main()
