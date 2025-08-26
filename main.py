# -*- coding: utf-8 -*-
import asyncio
asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())  # Untuk Python 3.12+

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
import numpy as np
from ta.momentum import RSIIndicator, StochasticOscillator
from ta.trend import EMAIndicator, MACD
from ta.volatility import BollingerBands, AverageTrueRange

# ================== CONFIG ==================
# Token & ID dari Anda
BOT_TOKEN = "8114552558:AAFpnQEYHYa8P43g5rjOwPs5TSbjtYh9zS4"
CHAT_ID = "-1002883903673"
AUTHORIZED_USER_ID = 1305881282

# TwelveData API Keys (untuk analisa M5)
API_KEYS_TWELVE = [
    "94a7d766d73f4db4a7ddf877473711c7",
    "af23649e02da42aab3e78cf343513325",
    "21a0860958e641cc934bec6277415088",
]
_current_key_index = 0

def get_active_api_key():
    global _current_key_index
    key = API_KEYS_TWELVE[_current_key_index]
    _current_key_index = (_current_key_index + 1) % len(API_KEYS_TWELVE)
    return key

# Metals-API (harga eksekusi realtime, hemat)
API_KEY_METALS = "2fzz3e9hw1rachdt6jwwo4furz1arvngsm879pg5bj9ucoe2xjjbv4l4gn72"

# Optional: sticker panah (akan fallback ke emoji bila gagal)
STICKER_BUY = "CAACAgUAAxkBAAEGqO1ndz3kY8J9B1cF6N4mQpdxo1iXGQACtQEAAtm1cVYpQeQvO3Lx6zQE"   # contoh
STICKER_SELL = "CAACAgUAAxkBAAEGqO5ndz4UswW5Yg1m4u4H8wBqZ_8YjAACtQIAAtm1cVZbO7QmH9g0FzQE" # contoh

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
    # Jumat setelah 22:00 WIB tutup, Sabtu & Minggu libur
    if weekday == 4 and jam >= time(22, 0):
        return False
    if weekday in [5, 6]:
        return False
    return True

# ================== DATA FETCHERS ==================
def fetch_twelvedata_series(symbol="XAU/USD", interval="5min", count=180):
    """Ambil candle untuk analisa dari TwelveData (dengan multi API key & failover)."""
    for _ in range(len(API_KEYS_TWELVE)):
        api_key = get_active_api_key()
        url = (
            f"https://api.twelvedata.com/time_series?symbol={symbol}&interval={interval}"
            f"&apikey={api_key}&outputsize={count}&format=JSON&dp=6&order=ASC"
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
            values = data.get("values", [])
            return values  # sudah ASC karena order=ASC
        except Exception as e:
            print(f"❌ Error fetch_twelvedata_series: {e}")
            continue
    return None

def fetch_realtime_price_metals_fast():
    """Ambil harga XAU/USD realtime dari Metals-API (/latest) -> hemat call."""
    try:
        url = f"https://metals-api.com/api/latest?access_key={API_KEY_METALS}&base=USD&symbols=XAU"
        r = requests.get(url, timeout=5).json()
        rate = r.get("rates", {}).get("XAU")
        if rate and rate > 0:
            # rate = XAU per USD -> kita butuh USD per XAU => 1 / rate
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
            f"&apikey={api_key}&outputsize=1&format=JSON&dp=6&order=DESC"
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
        for col in ["open", "high", "low", "close"]:
            df[col] = df[col].astype(float)
        df = df.set_index("datetime").sort_index()
        return df
    except Exception as e:
        print(f"❌ Error prepare_df: {e}")
        return None

def rolling_peaks(df: pd.DataFrame, window=5):
    """Deteksi swing high/low sederhana: bandingkan high/low dengan tetangga dalam window."""
    highs = df["high"]
    lows = df["low"]
    swing_high = (highs == highs.rolling(window, center=True).max())
    swing_low = (lows == lows.rolling(window, center=True).min())
    sh = df[highs == highs.rolling(window, center=True).max()]
    sl = df[lows == lows.rolling(window, center=True).min()]
    return sh.index.tolist(), sl.index.tolist()

def nearest_sr_levels(df: pd.DataFrame, lookback=120, n_levels=3):
    """Cari level support/resistance signifikan dari swing highs/lows terakhir."""
    sub = df.tail(lookback)
    highs = sub["high"]
    lows = sub["low"]
    rng = 5
    sh_mask = highs == highs.rolling(rng, center=True).max()
    sl_mask = lows == lows.rolling(rng, center=True).min()
    sh = sub[sh_mask]
    sl = sub[sl_mask]

    levels = []
    levels += list(sh["high"].round(2).values)
    levels += list(sl["low"].round(2).values)

    # Cluster level yang berdekatan (merge)
    levels = sorted(levels)
    merged = []
    tol = 0.3  # dolar
    for lv in levels:
        if not merged:
            merged.append([lv, 1])
        else:
            if abs(lv - merged[-1][0]) <= tol:
                merged[-1][0] = (merged[-1][0]*merged[-1][1] + lv)/(merged[-1][1]+1)
                merged[-1][1] += 1
            else:
                merged.append([lv, 1])
    merged.sort(key=lambda x: x[1], reverse=True)
    final = [round(x[0], 2) for x in merged[:n_levels*2]]  # ambil beberapa
    final = sorted(list(set(final)))
    return final

def nearest_levels_for_price(levels, price, up=True):
    if up:
        up_levels = [lv for lv in levels if lv >= price]
        return sorted(up_levels)[:2]
    else:
        dn_levels = [lv for lv in levels if lv <= price]
        dn_levels = sorted(dn_levels, reverse=True)[:2]
        return sorted(dn_levels)

def detect_candle_patterns(df: pd.DataFrame):
    """Deteksi beberapa pola candlestick populer (last 3 candle)."""
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

    # Doji
    if range_(last) > 0 and body(last) <= 0.1 * range_(last):
        patterns.append("➕ Doji")

    # Hammer / Shooting Star
    if body(last) > 0 and lower_wick(last) >= 2 * body(last) and upper_wick(last) <= body(last):
        patterns.append("🔨 Hammer")
    if body(last) > 0 and upper_wick(last) >= 2 * body(last) and lower_wick(last) <= body(last):
        if last["close"] < last["open"]:
            patterns.append("🌠 Shooting Star")
        else:
            patterns.append("🪓 Inverted Hammer")

    # Engulfing
    if (last["close"] > last["open"] and prev["close"] < prev["open"] and
        last["close"] > prev["open"] and last["open"] < prev["close"]):
        patterns.append("📈 Bullish Engulfing")
    if (last["close"] < last["open"] and prev["close"] > prev["open"] and
        last["close"] < prev["open"] and last["open"] > prev["close"]):
        patterns.append("📉 Bearish Engulfing")

    # Morning/Evening Star
    if len(d) >= 3:
        c1, c2, c3 = d.iloc[-3], d.iloc[-2], d.iloc[-1]
        if (c1["close"] < c1["open"] and abs(c2["close"]-c2["open"]) <= 0.5*abs(c1["close"]-c1["open"])
            and c3["close"] > c3["open"] and c3["close"] >= (c1["open"] + c1["close"]) / 2):
            patterns.append("🌅 Morning Star")
        if (c1["close"] > c1["open"] and abs(c2["close"]-c2["open"]) <= 0.5*abs(c1["close"]-c1["open"])
            and c3["close"] < c3["open"] and c3["close"] <= (c1["open"] + c1["close"]) / 2):
            patterns.append("🌆 Evening Star")
    return patterns

def detect_chart_pattern_simple(df: pd.DataFrame):
    """Chart pattern sederhana: Higher High/Higher Low (uptrend) atau Lower High/Lower Low (downtrend)."""
    sub = df.tail(20)
    highs = sub["high"].values
    lows = sub["low"].values
    # cek tren sederhana
    hh = np.all(np.diff(highs[-5:]) >= -1e-9)  # tak turun signifikan
    hl = np.all(np.diff(lows[-5:]) >= -1e-9)
    ll = np.all(np.diff(lows[-5:]) <= 1e-9)
    lh = np.all(np.diff(highs[-5:]) <= 1e-9)
    if hh and hl:
        return "📈 Higher High & Higher Low (Uptrend)"
    if ll and lh:
        return "📉 Lower High & Lower Low (Downtrend)"
    return "-"

def fibo_last_swing(df: pd.DataFrame):
    """Ambil swing high/low dari 60 candle terakhir untuk level fibo."""
    sub = df.tail(60)
    sh = sub["high"].idxmax()
    sl = sub["low"].idxmin()
    high = sub.loc[sh, "high"]
    low = sub.loc[sl, "low"]
    # pastikan arah: high di atas low
    if high < low:
        high, low = low, high
    diff = high - low
    levels = {
        "0.0%": round(high, 2),
        "23.6%": round(high - 0.236*diff, 2),
        "38.2%": round(high - 0.382*diff, 2),
        "50.0%": round(high - 0.5*diff, 2),
        "61.8%": round(high - 0.618*diff, 2),
        "78.6%": round(high - 0.786*diff, 2),
        "100%": round(low, 2),
    }
    return levels, high, low

def choose_sl_tp(arah, price, df: pd.DataFrame, levels, atr_val, rr=2.0):
    """Pilih SL di balik S/R terdekat + buffer ATR; TP mengacu S/R di arah target dengan RR>=1:2."""
    buffer = max(atr_val, 0.5)  # buffer minimal
    up_levels = [lv for lv in levels if lv > price]
    dn_levels = [lv for lv in levels if lv < price]

    if arah == "BUY":
        # SL di bawah support terdekat
        sl_level = max(dn_levels) if dn_levels else price - buffer
        sl = round(sl_level - 0.2, 2)  # sedikit di bawah level
        risk = price - sl
        target1 = price + risk  # RR 1:1
        target2 = price + rr * risk  # RR 1:2
        # Sesuaikan ke S/R atas terdekat namun >= target
        sorted_up = sorted(up_levels)
        tp1 = next((lv for lv in sorted_up if lv >= target1), target1)
        tp2 = next((lv for lv in sorted_up if lv >= target2), target2)
    else:
        # SELL
        sl_level = min(up_levels) if up_levels else price + buffer
        sl = round(sl_level + 0.2, 2)  # sedikit di atas level
        risk = sl - price
        target1 = price - risk  # RR 1:1
        target2 = price - rr * risk  # RR 1:2
        sorted_dn = sorted(dn_levels, reverse=True)
        tp1 = next((lv for lv in sorted_dn if lv <= target1), target1)
        tp2 = next((lv for lv in sorted_dn if lv <= target2), target2)

    # rounding
    return round(sl, 2), round(tp1, 2), round(tp2, 2)

# ---- Analisa Inti ----
def generate_signal(df: pd.DataFrame):
    if df is None or len(df) < 40:
        return None, None, None, None

    # indikator
    rsi = RSIIndicator(df["close"], window=14).rsi()
    ema20 = EMAIndicator(df["close"], window=20).ema_indicator()
    ema50 = EMAIndicator(df["close"], window=50).ema_indicator()
    macd_calc = MACD(close=df["close"], window_slow=26, window_fast=12, window_sign=9)
    macd_val = macd_calc.macd()
    macd_sig = macd_calc.macd_signal()
    stoch = StochasticOscillator(df["high"], df["low"], df["close"], window=14, smooth_window=3)
    k_val = stoch.stoch()
    d_val = stoch.stoch_signal()
    atr = AverageTrueRange(df["high"], df["low"], df["close"], window=14).average_true_range()

    df = df.copy()
    df["rsi"], df["ema20"], df["ema50"], df["macd"], df["macdsig"], df["k"], df["d"], df["atr"] = (
        rsi, ema20, ema50, macd_val, macd_sig, k_val, d_val, atr
    )
    df.dropna(inplace=True)
    last = df.iloc[-1]
    prev = df.iloc[-2]

    # Skoring sederhana (0-6)
    score = 0
    notes = []

    # Trend filter EMA20 vs EMA50
    if last["ema20"] > last["ema50"]:
        score += 1; notes.append("Trend: EMA20 > EMA50 (bullish)")
    else:
        notes.append("Trend: EMA20 < EMA50 (bearish)")

    # MACD
    if last["macd"] > last["macdsig"]:
        score += 1; notes.append("MACD: bullish")
    else:
        notes.append("MACD: bearish")

    # RSI zone
    if last["rsi"] < 30:
        score += 1; notes.append("RSI: oversold")
    elif last["rsi"] > 70:
        score += 1; notes.append("RSI: overbought")
    else:
        notes.append("RSI: normal")

    # Stochastic crossover
    if last["k"] > last["d"]:
        score += 1; notes.append("Stoch: bullish crossover")
    else:
        notes.append("Stoch: bearish crossover")

    # Price vs EMA20
    if last["close"] > last["ema20"]:
        score += 1; notes.append("Price di atas EMA20")
    else:
        notes.append("Price di bawah EMA20")

    # Momentum candle (close vs prev close)
    if last["close"] > prev["close"]:
        score += 1; notes.append("Momentum: naik vs candle sebelumnya")
    else:
        notes.append("Momentum: turun vs candle sebelumnya")

    # Arah sinyal
    arah = "BUY" if last["close"] > prev["close"] else "SELL"
    return arah, int(score), notes, df

# ================== MESSAGE ==================
def format_signal_message(symbol: str, timeframe: str, arah: str, price: float,
                          sl: float, tp1: float, tp2: float,
                          score: int, df: pd.DataFrame,
                          sr_levels: list, fibo_levels: dict,
                          candle_patterns: list, chart_pattern: str):
    last = df.iloc[-1]
    rsi_val = float(last["rsi"])
    ema20_val = float(last["ema20"])
    ema50_val = float(last["ema50"])
    k_val = float(last["k"])
    d_val = float(last["d"])
    atr_val = float(last["atr"])

    konf_stars = "⭐" * min(max(score, 1), 6)
    konf_txt = f"{konf_stars} ({score}/6)"
    pat_txt = ", ".join(candle_patterns) if candle_patterns else "-"

    # S/R & Fibo text
    sr_txt = ", ".join([str(x) for x in sr_levels[:6]]) if sr_levels else "-"
    fib_list = [f"{k} {v}" for k, v in fibo_levels.items()]
    fib_txt = ", ".join(fib_list)

    now_wib = datetime.now(pytz.timezone("Asia/Jakarta")).strftime("%Y-%m-%d %H:%M:%S")
    trend_txt = "Uptrend" if ema20_val > ema50_val else "Downtrend"
    momentum_txt = "Bullish" if last["macd"] > last["macdsig"] else "Bearish"

    lines = []
    lines.append(f"📊 **{symbol} (Gold) — {timeframe}**")
    lines.append(f"🕒 {now_wib} WIB")
    lines.append("")
    if arah == "BUY":
        lines.append("🟢 **Sinyal**: BUY")
    else:
        lines.append("🔴 **Sinyal**: SELL")
    lines.append(f"🎯 **Entry**: `{price}`")
    lines.append(f"⛔ **Stop Loss**: `{sl}`")
    lines.append(f"🎯 **Take Profit 1**: `{tp1}`")
    lines.append(f"🎯 **Take Profit 2**: `{tp2}`")
    # RR
    if arah == "BUY":
        risk = price - sl
        rr1 = (tp1 - price) / risk if risk > 0 else 0.0
        rr2 = (tp2 - price) / risk if risk > 0 else 0.0
    else:
        risk = sl - price
        rr1 = (price - tp1) / risk if risk > 0 else 0.0
        rr2 = (price - tp2) / risk if risk > 0 else 0.0
    lines.append(f"⚖️ **Risk/Reward**: 1:{rr2:.2f} (TP2), 1:{rr1:.2f} (TP1)")
    lines.append("")
    lines.append(f"📉 **Trend**: {trend_txt} (EMA20 {ema20_val:.2f} vs EMA50 {ema50_val:.2f})")
    lines.append(f"📈 **Momentum**: {momentum_txt} | MACD {'>' if last['macd']>last['macdsig'] else '<'} Signal")
    lines.append(f"💪 **Kekuatan Sinyal**: {konf_txt}")
    lines.append("")
    lines.append(f"🕯️ **Candle Pattern**: {pat_txt}")
    lines.append(f"📐 **Chart Pattern**: {chart_pattern}")
    lines.append(f"📊 **RSI(14)**: {rsi_val:.1f} | **Stoch %K/%D**: {k_val:.1f}/{d_val:.1f} | **ATR(14)**: {atr_val:.2f}")
    lines.append("")
    lines.append(f"🧱 **Support/Resistance**: {sr_txt}")
    lines.append(f"📏 **Fibonacci (last swing)**: {fib_txt}")

    return "\n".join(lines)

# ================== TASK (SEND SIGNAL) ==================
async def send_signal(context: ContextTypes.DEFAULT_TYPE):
    if not is_bot_working_now():
        return

    # Ambil data analisa
    candles = fetch_twelvedata_series(interval="5min", count=200)
    df_raw = prepare_df(candles)
    if df_raw is None or len(df_raw) < 60:
        return

    arah, score, notes, df_ind = generate_signal(df_raw)
    if arah is None:
        return

    # Candle & Chart pattern
    patterns = detect_candle_patterns(df_ind.tail(3))
    chart_pat = detect_chart_pattern_simple(df_ind)

    # Level S/R & Fibo
    sr_levels = nearest_sr_levels(df_ind, lookback=140, n_levels=3)
    fibo_levels, fib_high, fib_low = fibo_last_swing(df_ind)

    # Harga eksekusi realtime (hemat)
    harga_live = fetch_realtime_price_metals_fast() or fetch_realtime_price_twelve() or float(df_ind["close"].iloc[-1])

    # SL/TP (RR 1:2, menempel S/R)
    atr_val = float(df_ind["atr"].iloc[-1])
    sl, tp1, tp2 = choose_sl_tp(arah, harga_live, df_ind, sr_levels, atr_val, rr=2.0)

    # Compose message
    msg = format_signal_message(
        symbol="XAUUSD",
        timeframe="M5",
        arah=arah,
        price=harga_live,
        sl=sl, tp1=tp1, tp2=tp2,
        score=score,
        df=df_ind,
        sr_levels=sr_levels,
        fibo_levels=fibo_levels,
        candle_patterns=patterns,
        chart_pattern=chart_pat
    )

    # Kirim sticker panah (fallback ke emoji agar tidak error)
    try:
        if arah == "BUY":
            await context.bot.send_sticker(chat_id=CHAT_ID, sticker=STICKER_BUY)
        else:
            await context.bot.send_sticker(chat_id=CHAT_ID, sticker=STICKER_SELL)
    except Exception as e:
        print("Sticker gagal, fallback emoji:", e)
        arrow = "🟢⬆️" if arah == "BUY" else "🔴⬇️"
        await context.bot.send_message(chat_id=CHAT_ID, text=arrow)

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

    # Kirim signal setiap 30 menit (jadwal kerja tetap)
    bot_app.job_queue.run_repeating(send_signal, interval=1800, first=10)

    print("🤖 Bot berjalan...")
    bot_app.run_polling()

if __name__ == "__main__":
    main()
