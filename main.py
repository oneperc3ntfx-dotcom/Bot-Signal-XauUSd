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
from ta.momentum import RSIIndicator, StochasticOscillator
from ta.trend import EMAIndicator, MACD
from ta.volatility import AverageTrueRange

# ================== CONFIG ==================
BOT_TOKEN = "8114552558:AAFpnQEYHYa8P43g5rjOwPs5TSbjtYh9zS4"
CHAT_ID = "-1002883903673"
AUTHORIZED_USER_ID = 1305881282

# Multiple API Keys TwelveData (analisa)
API_KEYS_TWELVE = [
    "94a7d766d73f4db4a7ddf877473711c7",
    "af23649e02da42aab3e78cf343513325",
    "21a0860958e641cc934bec6277415088",
]
_current_key_index = 0

def get_active_api_key():
    global _current_key_index
    key = API_KEYS_TWELVE[_current_key_index]
    _current_key_index = (_current_key_index + 1) % len(API_KEYS_TWELVE)  # round-robin
    return key

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
    # Jumat setelah 22:00 WIB, Sabtu, Minggu -> libur
    if weekday == 4 and jam >= time(22, 0):
        return False
    if weekday in [5, 6]:
        return False
    return True

# ================== DATA FETCHERS ==================
def fetch_twelvedata_series(symbol="XAU/USD", interval="5min", count=120):
    """Ambil candle untuk analisa dari TwelveData (dengan multi API key & failover)."""
    for _ in range(len(API_KEYS_TWELVE)):
        api_key = get_active_api_key()
        url = (
            f"https://api.twelvedata.com/time_series?symbol={symbol}&interval={interval}"
            f"&apikey={api_key}&outputsize={count}&format=JSON"
        )
        try:
            response = requests.get(url, timeout=10)
            if response.status_code != 200:
                print(f"❌ TwelveData HTTP {response.status_code}")
                continue
            data = response.json()
            if data.get("status") == "error":
                print(f"❌ TwelveData error: {data.get('message')}")
                continue
            return data.get("values", [])[::-1]  # ascending
        except Exception as e:
            print(f"❌ Error fetch_twelvedata_series: {e}")
            continue
    return None

def fetch_realtime_price_goldapi():
    """Ambil harga XAU/USD realtime dari Gold-API.com"""
    try:
        url = "https://api.gold-api.com/price/XAU"
        r = requests.get(url, timeout=5).json()
        price = r.get("price")
        if price and price > 0:
            return round(float(price), 2)
        print("❌ Gold-API response:", r)
        return None
    except Exception as e:
        print(f"❌ Error fetch_realtime_price_goldapi: {e}")
        return None

def fetch_realtime_price_twelve():
    """Fallback harga jika Gold-API gagal: ambil close 1m terakhir TwelveData."""
    for _ in range(len(API_KEYS_TWELVE)):
        api_key = get_active_api_key()
        url = (
            f"https://api.twelvedata.com/time_series?symbol=XAU/USD&interval=1min"
            f"&apikey={api_key}&outputsize=1&format=JSON"
        )
        try:
            r = requests.get(url, timeout=10).json()
            if r.get("status") == "error":
                print(f"❌ TwelveData price error: {r.get('message')}")
                continue
            last = r.get("values", [])[0] if r.get("values") else None
            return float(last["close"]) if last else None
        except Exception as e:
            print(f"❌ Error fetch_realtime_price_twelve: {e}")
            continue
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
    except Exception as e:
        print(f"❌ Error prepare_df: {e}")
        return None

# ---- Support & Resistance (swing) ----
def swing_levels(df: pd.DataFrame, lookback=30):
    """Cari S/R terdekat dari 30 candle terakhir."""
    d = df.tail(lookback)
    swing_high = d["high"].max()
    swing_low = d["low"].min()
    return swing_high, swing_low

# ---- Fibonacci dari swing terakhir ----
def fib_levels(high, low):
    diff = high - low
    return {
        "0.236": round(high - 0.236 * diff, 2),
        "0.382": round(high - 0.382 * diff, 2),
        "0.500": round(high - 0.5   * diff, 2),
        "0.618": round(high - 0.618 * diff, 2),
        "0.786": round(high - 0.786 * diff, 2),
    }

# ---- Candlestick Pattern Detection (manual) ----
def detect_candle_patterns(df: pd.DataFrame):
    patterns = []
    if df is None or len(df) < 2:
        return patterns

    d = df.copy()
    last = d.iloc[-1]
    prev = d.iloc[-2]

    def body(c):  return abs(c["close"] - c["open"])
    def range_(c): return c["high"] - c["low"]
    def upper_wick(c): return c["high"] - max(c["close"], c["open"])
    def lower_wick(c): return min(c["close"], c["open"]) - c["low"]

    if range_(last) > 0 and body(last) <= 0.1 * range_(last):
        patterns.append("➕ Doji")
    if body(last) > 0 and lower_wick(last) >= 2 * body(last) and upper_wick(last) <= body(last):
        patterns.append("🔨 Hammer")
    if body(last) > 0 and upper_wick(last) >= 2 * body(last) and lower_wick(last) <= body(last):
        patterns.append("🌠 Shooting Star" if last["close"] < last["open"] else "🪓 Inverted Hammer")
    if (last["close"] > last["open"] and prev["close"] < prev["open"]
        and last["close"] > prev["open"] and last["open"] < prev["close"]):
        patterns.append("📈 Bullish Engulfing")
    if (last["close"] < last["open"] and prev["close"] > prev["open"]
        and last["close"] < prev["open"] and last["open"] > prev["close"]):
        patterns.append("📉 Bearish Engulfing")

    if len(d) >= 3:
        c1, c2, c3 = d.iloc[-3], d.iloc[-2], d.iloc[-1]
        if (c1["close"] < c1["open"] and abs(c2["close"]-c2["open"]) <= 0.5 * abs(c1["close"]-c1["open"])
            and c3["close"] > c3["open"] and c3["close"] >= (c1["open"] + c1["close"]) / 2):
            patterns.append("🌅 Morning Star")
        if (c1["close"] > c1["open"] and abs(c2["close"]-c2["open"]) <= 0.5 * abs(c1["close"]-c1["open"])
            and c3["close"] < c3["open"] and c3["close"] <= (c1["open"] + c1["close"]) / 2):
            patterns.append("🌆 Evening Star")

    return patterns

# ---- Analisa Inti ----
def generate_signal(df):
    if df is None or len(df) < 30:
        return None, None, None, None

    try:
        rsi = RSIIndicator(df["close"], window=14).rsi()
        ema9 = EMAIndicator(df["close"], window=9).ema_indicator()
        ema20 = EMAIndicator(df["close"], window=20).ema_indicator()
        macd_calc = MACD(close=df["close"], window_slow=26, window_fast=12, window_sign=9)
        macd_val = macd_calc.macd()
        macd_sig = macd_calc.macd_signal()

        df["rsi"], df["ema9"], df["ema20"], df["macd"], df["macdsig"] = rsi, ema9, ema20, macd_val, macd_sig
        df.dropna(inplace=True)

        last = df.iloc[-1]
        prev = df.iloc[-2]

        # Skor sederhana
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

        # Arah: momentum candle terakhir
        arah = "BUY" if last["close"] > prev["close"] else "SELL"

        # Tambahan indikator
        try:
            stoch = StochasticOscillator(df["high"], df["low"], df["close"], window=14, smooth_window=3)
            k_val = float(stoch.stoch().iloc[-1])
            d_val = float(stoch.stoch_signal().iloc[-1])
        except Exception:
            k_val = d_val = float("nan")
        try:
            atr = float(AverageTrueRange(df["high"], df["low"], df["close"], window=14).average_true_range().iloc[-1])
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
        print(f"❌ Error generate_signal: {e}")
        return None, None, None, None

# ================== MESSAGE (SCALPING STYLE) ==================
def build_scalping_message(arah: str, price: float, tp1: float, tp2: float, sl: float,
                           status_text: str, indicators: dict, patterns: list,
                           sr_high: float, sr_low: float, fibo: dict):
    now_wib = datetime.now(pytz.timezone("Asia/Jakarta")).strftime("%H:%M:%S")
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
        f"\n"
        f"🔍 Analisa:\n"
        f"{'✅' if indicators['macd']>indicators['macdsig'] else '❌'} MACD {'bullish' if indicators['macd']>indicators['macdsig'] else 'bearish'}\n"
        f"{'⚠️' if indicators['rsi']>70 else ('⚠️' if indicators['rsi']<30 else 'ℹ️')} RSI {indicators['rsi']:.1f}\n"
        f"{'📈' if indicators['last_close']>indicators['ema20'] else '📉'} Tren {trend_txt} (Price vs EMA20)\n"
        f"📊 ATR(14): {indicators['atr']:.2f}\n"
        f"🕯️ Candle: {pat_txt}\n"
        f"🧭 S/R: R {sr_high} | S {sr_low}\n"
        f"🔢 Fibo(0.382/0.618): {fibo['0.382']} / {fibo['0.618']}\n"
    )
    return msg

# ================== TASK: KIRIM SINYAL 30 MENIT ==================
async def send_signal(context: ContextTypes.DEFAULT_TYPE):
    if not is_bot_working_now():
        return

    # Ambil data analisa (M5) dari TwelveData
    candles = fetch_twelvedata_series(interval="5min")
    df = prepare_df(candles)
    arah, score, notes, indicators = generate_signal(df)
    if arah is None:
        return

    # Candle pattern singkat
    patterns = detect_candle_patterns(df.tail(3))

    # S/R & Fibo
    sr_high, sr_low = swing_levels(df, lookback=30)
    fibo = fib_levels(sr_high, sr_low)

    # Harga eksekusi realtime (Gold-API → TwelveData → last_close)
    price_live = fetch_realtime_price_goldapi() or fetch_realtime_price_twelve() or indicators["last_close"]

    # ====== SCALPING TP/SL ======
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

    await context.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")

# ================== HANDLERS ==================
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
    bot_app = ApplicationBuilder().token(BOT_TOKEN).build()
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CommandHandler("signal", manual_signal))
    bot_app.add_handler(MessageHandler(filters.COMMAND, unknown))

    # Sinyal reguler: tiap 30 menit
    bot_app.job_queue.run_repeating(send_signal, interval=1800, first=10)

    print("🤖 Bot berjalan...")
    bot_app.run_polling()

if __name__ == "__main__":
    main()
