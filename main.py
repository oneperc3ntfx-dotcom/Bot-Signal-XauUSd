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

# ================== CONFIG ==================
BOT_TOKEN = "8114552558:AAFpnQEYHYa8P43g5rjOwPs5TSbjtYh9zS4"
CHAT_ID = "-1002883903673"
AUTHORIZED_USER_ID = 1305881282

# Multiple API Keys TwelveData (analisa)
API_KEYS_TWELVE = [
    "94a7d766d73f4db4a7ddf877473711c7",
    "af23649e02da42aab3e78cf343513325",
    "af23649e02da42aab3e78cf343513325",
]
_current_key_index = 0

def get_active_api_key():
    global _current_key_index
    key = API_KEYS_TWELVE[_current_key_index]
    _current_key_index = (_current_key_index + 1) % len(API_KEYS_TWELVE)  # round-robin
    return key

# Metals-API (harga eksekusi realtime)
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

def fetch_realtime_price_metals_fast():
    """Ambil harga XAU/USD realtime dari Metals-API premium (pembaruan detik-an)."""
    try:
        url = f"https://metals-api.com/api/latest?access_key={API_KEY_METALS}&base=USD&symbols=XAU"
        r = requests.get(url, timeout=5).json()
        rate = r.get("rates", {}).get("XAU")
        if rate and rate > 0:
            # USD per 1 XAU => konversi ke XAU/USD? Metals-API mengembalikan XAU price dalam base=USD: XAU = ounce emas per USD
            # rate = XAU (ounce) per 1 USD, maka 1 / rate = USD per 1 XAU
            return round(1.0 / float(rate), 2)
        print("❌ Metals-API response:", r)
        return None
    except Exception as e:
        print(f"❌ Error fetch_realtime_price_metals_fast: {e}")
        return None

def fetch_realtime_price_twelve():
    """Fallback harga jika Metals-API gagal: ambil close 1m terakhir TwelveData."""
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

# ---- Candlestick Pattern Detection (manual) ----
def detect_candle_patterns(df: pd.DataFrame):
    """Deteksi beberapa pola candlestick populer pada 3 candle terakhir.
    Return: list[str]
    """
    patterns = []
    if df is None or len(df) < 2:
        return patterns

    d = df.copy()
    last = d.iloc[-1]
    prev = d.iloc[-2]

    def body(c):
        return abs(c["close"] - c["open"])

    def range_(c):
        return c["high"] - c["low"]

    def upper_wick(c):
        return c["high"] - max(c["close"], c["open"])

    def lower_wick(c):
        return min(c["close"], c["open"]) - c["low"]

    # Doji (body kecil <= 10% range)
    if range_(last) > 0 and body(last) <= 0.1 * range_(last):
        patterns.append("➕ Doji")

    # Hammer (bullish) & Inverted Hammer / Shooting Star (bearish)
    if body(last) > 0 and lower_wick(last) >= 2 * body(last) and upper_wick(last) <= body(last):
        patterns.append("🔨 Hammer")
    if body(last) > 0 and upper_wick(last) >= 2 * body(last) and lower_wick(last) <= body(last):
        # Shooting Star / Inverted Hammer
        if last["close"] < last["open"]:
            patterns.append("🌠 Shooting Star")
        else:
            patterns.append("🪓 Inverted Hammer")

    # Engulfing
    if (
        last["close"] > last["open"]
        and prev["close"] < prev["open"]
        and last["close"] > prev["open"]
        and last["open"] < prev["close"]
    ):
        patterns.append("📈 Bullish Engulfing")
    if (
        last["close"] < last["open"]
        and prev["close"] > prev["open"]
        and last["close"] < prev["open"]
        and last["open"] > prev["close"]
    ):
        patterns.append("📉 Bearish Engulfing")

    # Morning Star / Evening Star (butuh 3 candle)
    if len(d) >= 3:
        c1, c2, c3 = d.iloc[-3], d.iloc[-2], d.iloc[-1]
        # Morning Star: down, small body, up closing into c1 body
        if (
            c1["close"] < c1["open"]
            and body(c2) <= 0.5 * body(c1)
            and c3["close"] > c3["open"]
            and c3["close"] >= (c1["open"] + c1["close"]) / 2
        ):
            patterns.append("🌅 Morning Star")
        # Evening Star: up, small body, down closing into c1 body
        if (
            c1["close"] > c1["open"]
            and body(c2) <= 0.5 * body(c1)
            and c3["close"] < c3["open"]
            and c3["close"] <= (c1["open"] + c1["close"]) / 2
        ):
            patterns.append("🌆 Evening Star")

    return patterns

# ---- Analisa Inti ----
def generate_signal(df):
    if df is None or len(df) < 20:
        return None, None, None
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

        note = []
        score = 0
        if last["rsi"] < 30 and last["close"] > last["ema9"]:
            score += 1; note.append("RSI oversold + di atas EMA9")
        if last["close"] > prev["close"]:
            score += 1; note.append("Harga naik vs candle sebelumnya")
        if last["close"] > last["ema20"]:
            score += 1; note.append("Harga di atas EMA20 (trend bullish)")
        if last["macd"] > last["macdsig"]:
            score += 1; note.append("MACD bullish")

        arah = "BUY" if last["close"] > prev["close"] else "SELL"
        return arah, score, "\n- ".join(note)
    except Exception as e:
        print(f"❌ Error generate_signal: {e}")
        return None, None, None

# ---- Strong Setup (M1 + M5) ----
_last_strong_sent_at = None
_strong_cooldown_min = 10

def is_strong_setup(df_m5, df_m1):
    try:
        ema20_m5 = EMAIndicator(df_m5["close"], window=20).ema_indicator()
        rsi_m5 = RSIIndicator(df_m5["close"], window=14).rsi()
        macd_m5 = MACD(close=df_m5["close"], window_slow=26, window_fast=12, window_sign=9)
        macd_val_m5 = macd_m5.macd(); macd_sig_m5 = macd_m5.macd_signal()

        ema20_m1 = EMAIndicator(df_m1["close"], window=20).ema_indicator()
        rsi_m1 = RSIIndicator(df_m1["close"], window=14).rsi()
        stoch = StochasticOscillator(df_m1["high"], df_m1["low"], df_m1["close"], window=14, smooth_window=3)
        k1, d1 = stoch.stoch(), stoch.stoch_signal()

        temp5 = df_m5.copy(); temp1 = df_m1.copy()
        temp5["ema20"], temp5["rsi"], temp5["macd"], temp5["macdsig"] = ema20_m5, rsi_m5, macd_val_m5, macd_sig_m5
        temp1["ema20"], temp1["rsi"], temp1["k"], temp1["d"] = ema20_m1, rsi_m1, k1, d1
        temp5.dropna(inplace=True); temp1.dropna(inplace=True)
        l5, p5 = temp5.iloc[-1], temp5.iloc[-2]
        l1, p1 = temp1.iloc[-1], temp1.iloc[-2]

        reasons, buy_score, sell_score = [], 0, 0
        if l5["close"] > l5["ema20"]: buy_score += 1; reasons.append("BUY: M5 di atas EMA20")
        if l5["macd"] > l5["macdsig"]: buy_score += 1; reasons.append("BUY: MACD M5 bullish")
        if l1["close"] > l1["ema20"] and l1["rsi"] > p1["rsi"]: buy_score += 1; reasons.append("BUY: M1 searah & RSI naik")
        if l1["k"] > l1["d"] and l1["k"] < 30: buy_score += 1; reasons.append("BUY: Stoch M1 bullish dari area rendah")

        if l5["close"] < l5["ema20"]: sell_score += 1; reasons.append("SELL: M5 di bawah EMA20")
        if l5["macd"] < l5["macdsig"]: sell_score += 1; reasons.append("SELL: MACD M5 bearish")
        if l1["close"] < l1["ema20"] and l1["rsi"] < p1["rsi"]: sell_score += 1; reasons.append("SELL: M1 searah & RSI turun")
        if l1["k"] < l1["d"] and l1["k"] > 70: sell_score += 1; reasons.append("SELL: Stoch M1 bearish dari area tinggi")

        if buy_score >= 3 and buy_score > sell_score:
            return "BUY", reasons
        if sell_score >= 3 and sell_score > buy_score:
            return "SELL", reasons
        return None, reasons
    except Exception as e:
        print(f"❌ Error is_strong_setup: {e}")
        return None, []

# ================== MESSAGE FORMATTING ==================
def format_signal_message(symbol: str, timeframe: str, arah: str, price: float, tp: float, sl: float,
                          score: int, df: pd.DataFrame, patterns: list):
    last = df.iloc[-1]
    rsi_val = float(last.get("rsi", float("nan")))
    ema20_val = float(last.get("ema20", float("nan"))) if "ema20" in df.columns else float("nan")

    # Stochastic & ATR for extra info (optional, safe fail)
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

    konf_txt = "🟢 KUAT" if score >= 3 else ("🟡 MODERAT" if score == 2 else "🔴 LEMAH")
    pat_txt = ", ".join(patterns) if patterns else "-"

    now_wib = datetime.now(pytz.timezone("Asia/Jakarta")).strftime("%Y-%m-%d %H:%M:%S")

    msg = (
        f"**{symbol} SIGNAL — {timeframe}**\n"
        f"🕒 {now_wib} WIB\n"
        f"\n"
        f"**Arah**: `{arah}`\n"
        f"**Entry**: `{price}`\n"
        f"**TP / SL**: `{tp}` / `{sl}`\n"
        f"**Confidence**: {konf_txt} ({score}/4)\n"
        f"\n"
        f"**Candle Pattern**: {pat_txt}\n"
        f"**Trend**: {'Price>EMA20 (bullish)' if not pd.isna(ema20_val) and last['close']>ema20_val else 'Price<EMA20 (bearish)'}\n"
        f"**Momentum**: RSI {rsi_val:.1f} | Stoch %K {k_val:.1f} vs %D {d_val:.1f}\n"
        f"**Volatility**: ATR(14) {atr:.2f}\n"
    )
    return msg

# ================== TASKS ==================
async def send_signal(context: ContextTypes.DEFAULT_TYPE):
    if not is_bot_working_now():
        return
    candles = fetch_twelvedata_series(interval="5min")
    df = prepare_df(candles)
    arah, score, notes = generate_signal(df)
    if arah is None:
        return

    # Pola candle di 3 candle terakhir
    patterns = detect_candle_patterns(df.tail(3))

    # Harga realtime dari Metals-API (hemat: endpoint latest)
    harga_live = fetch_realtime_price_metals_fast() or fetch_realtime_price_twelve() or df["close"].iloc[-1]
    tp = round(harga_live + 2.0, 2) if arah == "BUY" else round(harga_live - 2.0, 2)
    sl = round(harga_live - 1.0, 2) if arah == "BUY" else round(harga_live + 1.0, 2)

    msg = format_signal_message(
        symbol="XAU/USD",
        timeframe="M5",
        arah=arah,
        price=harga_live,
        tp=tp,
        sl=sl,
        score=score,
        df=df,
        patterns=patterns,
    )

    # Tambahkan catatan analisa ringkas di bawah
    if notes:
        msg += "\n**Notes**:\n- " + notes

    await context.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")

async def monitor_strong_signal(context: ContextTypes.DEFAULT_TYPE):
    global _last_strong_sent_at
    if not is_bot_working_now():
        return

    df_m5 = prepare_df(fetch_twelvedata_series(interval="5min"))
    df_m1 = prepare_df(fetch_twelvedata_series(interval="1min", count=120))
    if df_m5 is None or df_m1 is None or len(df_m5) < 30 or len(df_m1) < 30:
        return

    arah, reasons = is_strong_setup(df_m5, df_m1)
    if not arah:
        return

    now = datetime.now(pytz.timezone("Asia/Jakarta"))
    if _last_strong_sent_at and (now - _last_strong_sent_at) < timedelta(minutes=_strong_cooldown_min):
        return
    _last_strong_sent_at = now

    harga_live = fetch_realtime_price_metals_fast() or df_m1["close"].iloc[-1]
    tp = round(harga_live + 3.0, 2) if arah == "BUY" else round(harga_live - 3.0, 2)
    sl = round(harga_live - 1.5, 2) if arah == "BUY" else round(harga_live + 1.5, 2)

    # Candle pattern konteks M1 (lebih relevan timing)
    pat = detect_candle_patterns(df_m1.tail(3))
    pat_txt = ", ".join(pat) if pat else "-"

    header = f"**XAU/USD — STRONG {arah}**\n"
    info = (
        f"Entry: `{harga_live}` | TP: `{tp}` | SL: `{sl}`\n"
        f"Candle Pattern (M1): {pat_txt}\n"
        f"Alasan:\n- " + "\n- ".join(reasons)
    )
    await context.bot.send_message(chat_id=CHAT_ID, text=header + info, parse_mode="Markdown")

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

    # Sinyal reguler: tiap 30 menit; Strong scan: tiap 60 detik
    bot_app.job_queue.run_repeating(send_signal, interval=1800, first=10)
    bot_app.job_queue.run_repeating(monitor_strong_signal, interval=60, first=20)

    print("🤖 Bot berjalan...")
    bot_app.run_polling()

if __name__ == "__main__":
    main()

