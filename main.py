# ================== IMPORTS & EVENT LOOP ==================
import asyncio
asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())  # Python 3.12

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
from ta.trend import EMAIndicator, MACD, ADXIndicator
from ta.volatility import BollingerBands, AverageTrueRange

# ================== CONFIG ==================
BOT_TOKEN = "8114552558:AAFpnQEYHYa8P43g5rjOwPs5TSbjtYh9zS4"
CHAT_ID = "-1002883903673"
AUTHORIZED_USER_ID = 1305881282

# TwelveData (analisa) — pakai 5m saja per siklus, H1 di-resample agar hemat kredit
API_KEYS_TWELVE = [
    "94a7d766d73f4db4a7ddf877473711c7",
    "af23649e02da42aab3e78cf343513325",
    "af23649e02da42aab3e78cf343513325",
]
_current_key_index = 0
def get_active_api_key():
    global _current_key_index
    key = API_KEYS_TWELVE[_current_key_index]
    _current_key_index = (_current_key_index + 1) % len(API_KEYS_TWELVE)
    return key

# Metals-API (harga eksekusi realtime, hemat: endpoint latest)
API_KEY_METALS = "2fzz3e9hw1rachdt6jwwo4furz1arvngsm879pg5bj9ucoe2xjjbv4l4gn72"

# (Opsional) Sticker file_id — jika tidak valid, akan otomatis fallback ke emoji.
STICKER_BUY = "CAACAgIAAxkBAAEFqWJm6F-buy_sticker_id_sample"   # ganti dengan file_id valid milik Anda
STICKER_SELL = "CAACAgIAAxkBAAEFqWNm6F-sell_sticker_id_sample"  # ganti dengan file_id valid milik Anda

# ================== FLASK KEEP-ALIVE ==================
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running"

def keep_alive():
    Thread(target=lambda: app.run(host='0.0.0.0', port=8080)).start()

# ================== JAM KERJA ==================
def is_bot_working_now():
    now = datetime.now(pytz.timezone("Asia/Jakarta"))
    weekday = now.weekday()
    jam = now.time()
    # Jumat setelah 22:00 WIB libur, Sabtu & Minggu libur
    if weekday == 4 and jam >= time(22, 0):
        return False
    if weekday in [5, 6]:
        return False
    return True

# ================== FETCHERS ==================
def fetch_twelvedata_series(symbol="XAU/USD", interval="5min", count=400):
    """Ambil candle 5m untuk analisa. Hemat: sekali panggilan per siklus."""
    for _ in range(len(API_KEYS_TWELVE)):
        api_key = get_active_api_key()
        url = (
            f"https://api.twelvedata.com/time_series?symbol={symbol}&interval={interval}"
            f"&apikey={api_key}&outputsize={count}&format=JSON"
        )
        try:
            resp = requests.get(url, timeout=10)
            if resp.status_code != 200:
                print(f"❌ TwelveData HTTP {resp.status_code}")
                continue
            data = resp.json()
            if data.get("status") == "error":
                print(f"❌ TwelveData error: {data.get('message')}")
                continue
            values = data.get("values", [])[::-1]  # ascending
            return values
        except Exception as e:
            print(f"❌ Error fetch_twelvedata_series: {e}")
            continue
    return None

def fetch_realtime_price_metals_fast():
    """Ambil harga XAU/USD realtime dari Metals-API (latest, sangat hemat)."""
    try:
        url = f"https://metals-api.com/api/latest?access_key={API_KEY_METALS}&base=USD&symbols=XAU"
        r = requests.get(url, timeout=5).json()
        rate = r.get("rates", {}).get("XAU")
        if rate and rate > 0:
            # rate = XAU per 1 USD → konversi ke USD per 1 XAU
            return round(1.0 / float(rate), 2)
        print("❌ Metals-API response:", r)
        return None
    except Exception as e:
        print(f"❌ Error fetch_realtime_price_metals_fast: {e}")
        return None

def fetch_realtime_price_twelve_fallback():
    """Fallback harga jika Metals-API gagal."""
    for _ in range(len(API_KEYS_TWELVE)):
        api_key = get_active_api_key()
        url = (
            f"https://api.twelvedata.com/time_series?symbol=XAU/USD&interval=1min"
            f"&apikey={api_key}&outputsize=1&format=JSON"
        )
        try:
            r = requests.get(url, timeout=8).json()
            if r.get("status") == "error":
                print(f"❌ TwelveData price error: {r.get('message')}")
                continue
            last = r.get("values", [])[0] if r.get("values") else None
            return float(last["close"]) if last else None
        except Exception as e:
            print(f"❌ Error fetch_realtime_price_twelve_fallback: {e}")
            continue
    return None

# ================== HELPERS DF ==================
def prepare_df(data):
    try:
        df = pd.DataFrame(data)
        df["datetime"] = pd.to_datetime(df["datetime"])
        df.set_index("datetime", inplace=True)
        for c in ["open", "high", "low", "close"]:
            df[c] = df[c].astype(float)
        df.sort_index(inplace=True)
        return df
    except Exception as e:
        print(f"❌ Error prepare_df: {e}")
        return None

def resample_to_h1(df_5m: pd.DataFrame):
    """Resample 5m → 1H untuk trend filter tanpa tambahan hit API."""
    try:
        df_h1 = pd.DataFrame()
        df_h1["open"] = df_5m["open"].resample("60T").first()
        df_h1["high"] = df_5m["high"].resample("60T").max()
        df_h1["low"]  = df_5m["low"].resample("60T").min()
        df_h1["close"]= df_5m["close"].resample("60T").last()
        df_h1.dropna(inplace=True)
        return df_h1
    except Exception as e:
        print(f"❌ Error resample_to_h1: {e}")
        return None

# ================== SNR & FIBO ==================
def find_snr_levels(df: pd.DataFrame, lb: int = 60, tol: float = 0.15):
    """Cari level Support/Resistance pakai pivot sederhana dari lookback terakhir.
       tol = toleransi (USD) untuk pengelompokan level yang berdekatan.
    """
    if df is None or len(df) < lb:
        return [], []
    d = df.tail(lb)
    highs = d["high"].values
    lows  = d["low"].values

    pivots_hi, pivots_lo = [], []

    for i in range(2, len(d) - 2):
        if highs[i] == max(highs[i-2:i+3]):
            pivots_hi.append(highs[i])
        if lows[i] == min(lows[i-2:i+3]):
            pivots_lo.append(lows[i])

    # Gabung level yang berdekatan (clustering 1D sederhana)
    def cluster_levels(levels):
        levels = sorted(levels)
        if not levels:
            return []
        clusters = [[levels[0]]]
        for lv in levels[1:]:
            if abs(lv - clusters[-1][-1]) <= tol:
                clusters[-1].append(lv)
            else:
                clusters.append([lv])
        return [sum(c)/len(c) for c in clusters]

    res_levels = cluster_levels(pivots_hi)
    sup_levels = cluster_levels(pivots_lo)
    return sup_levels, res_levels

def fibo_levels(df: pd.DataFrame, lb: int = 120):
    """Hitung fibo retracement dari swing terakhir (lb candle terakhir)."""
    if df is None or len(df) < lb:
        d = df
    else:
        d = df.tail(lb)
    swing_high = d["high"].max()
    swing_low  = d["low"].min()
    dif = swing_high - swing_low
    if dif <= 0:
        return {}
    return {
        "0.0%": swing_high,
        "23.6%": swing_high - 0.236 * dif,
        "38.2%": swing_high - 0.382 * dif,
        "50.0%": swing_high - 0.500 * dif,
        "61.8%": swing_high - 0.618 * dif,
        "78.6%": swing_high - 0.786 * dif,
        "100%": swing_low,
    }

def nearest_level(price: float, levels: list):
    if not levels:
        return None, None
    diffs = [(abs(price - lv), lv) for lv in levels]
    diffs.sort(key=lambda x: x[0])
    return diffs[0][1], diffs[0][0]

# ================== CANDLE & CHART PATTERNS ==================
def detect_candles(df: pd.DataFrame):
    """Deteksi beberapa pola candle dasar dari 3 candle terakhir."""
    patterns = []
    if df is None or len(df) < 2:
        return patterns

    d = df.copy()
    last = d.iloc[-1]
    prev = d.iloc[-2]

    def body(c): return abs(c["close"] - c["open"])
    def rng(c):  return c["high"] - c["low"]
    def up_w(c): return c["high"] - max(c["close"], c["open"])
    def lo_w(c): return min(c["close"], c["open"]) - c["low"]

    # Doji
    if rng(last) > 0 and body(last) <= 0.1 * rng(last):
        patterns.append("➕ Doji")
    # Hammer
    if body(last) > 0 and lo_w(last) >= 2 * body(last) and up_w(last) <= body(last):
        patterns.append("🔨 Hammer")
    # Shooting Star / Inverted Hammer
    if body(last) > 0 and up_w(last) >= 2 * body(last) and lo_w(last) <= body(last):
        if last["close"] < last["open"]:
            patterns.append("🌠 Shooting Star")
        else:
            patterns.append("🪓 Inverted Hammer")
    # Engulfing
    if (last["close"] > last["open"] and prev["close"] < prev["open"]
        and last["close"] > prev["open"] and last["open"] < prev["close"]):
        patterns.append("📈 Bullish Engulfing")
    if (last["close"] < last["open"] and prev["close"] > prev["open"]
        and last["close"] < prev["open"] and last["open"] > prev["close"]):
        patterns.append("📉 Bearish Engulfing")
    # Morning/Evening Star
    if len(d) >= 3:
        c1, c2, c3 = d.iloc[-3], d.iloc[-2], d.iloc[-1]
        if (c1["close"] < c1["open"] and abs(c2["close"]-c2["open"]) <= 0.5*abs(c1["close"]-c1["open"])
            and c3["close"] > c3["open"] and c3["close"] >= (c1["open"]+c1["close"])/2):
            patterns.append("🌅 Morning Star")
        if (c1["close"] > c1["open"] and abs(c2["close"]-c2["open"]) <= 0.5*abs(c1["close"]-c1["open"])
            and c3["close"] < c3["open"] and c3["close"] <= (c1["open"]+c1["close"])/2):
            patterns.append("🌆 Evening Star")
    return patterns

def detect_chart_patterns(df: pd.DataFrame):
    """Deteksi ringan: Double Top/Bottom dan Triangle (heuristik sederhana)."""
    notes = []
    if df is None or len(df) < 60:
        return notes
    d = df.tail(120)
    closes = d["close"].values

    # Double Top/Bottom: bandingkan dua puncak/lembah terakhir
    import numpy as np
    peaks_idx = (np.diff(np.sign(np.diff(closes))) < 0).nonzero()[0] + 1
    trough_idx = (np.diff(np.sign(np.diff(closes))) > 0).nonzero()[0] + 1
    if len(peaks_idx) >= 2:
        if abs(closes[peaks_idx[-1]] - closes[peaks_idx[-2]]) <= 1.0:  # ~USD 1 toleransi
            notes.append("⛰️ Double Top (potensi bearish)")
    if len(trough_idx) >= 2:
        if abs(closes[trough_idx[-1]] - closes[trough_idx[-2]]) <= 1.0:
            notes.append("🏔️ Double Bottom (potensi bullish)")

    # Triangle: high turun & low naik (compressing range)
    last30 = d.tail(30)
    if len(last30) >= 10:
        highs_down = last30["high"].rolling(5).max().dropna()
        lows_up   = last30["low"].rolling(5).min().dropna()
        if len(highs_down) > 3 and len(lows_up) > 3:
            if highs_down.iloc[-1] < highs_down.iloc[0] and lows_up.iloc[-1] > lows_up.iloc[0]:
                notes.append("🔺 Triangle (konsolidasi, waspada breakout)")
    return notes

# ================== NEWS FILTER RINGAN ==================
def is_high_impact_window():
    """Dummy filter: hindari sinyal sekitar pukul xx:25–xx:35 WIB (simulasi rilis data).
       Jika ingin real API kalender, tinggal ganti fungsi ini.
    """
    now = datetime.now(pytz.timezone("Asia/Jakarta"))
    if 25 <= now.minute <= 35:
        return True
    return False

# ================== ANALISA & VOTING ==================
def compute_indicators(df_5m: pd.DataFrame, df_h1: pd.DataFrame):
    """Hitung indikator utama untuk voting."""
    out = {}

    # 5m indicators
    rsi = RSIIndicator(df_5m["close"], window=14).rsi()
    ema20_5 = EMAIndicator(df_5m["close"], window=20).ema_indicator()
    ema50_5 = EMAIndicator(df_5m["close"], window=50).ema_indicator()
    macd = MACD(df_5m["close"], window_slow=26, window_fast=12, window_sign=9)
    macd_val = macd.macd(); macd_sig = macd.macd_signal()
    adx = ADXIndicator(df_5m["high"], df_5m["low"], df_5m["close"], window=14).adx()
    bb = BollingerBands(df_5m["close"], window=20, window_dev=2)
    bb_high = bb.bollinger_hband(); bb_low = bb.bollinger_lband()

    df_5m["rsi"] = rsi
    df_5m["ema20"] = ema20_5
    df_5m["ema50"] = ema50_5
    df_5m["macd"] = macd_val
    df_5m["macdsig"] = macd_sig
    df_5m["adx"] = adx
    df_5m["bb_high"] = bb_high
    df_5m["bb_low"] = bb_low
    df_5m.dropna(inplace=True)

    # H1 trend filter
    ema50_h1 = EMAIndicator(df_h1["close"], window=50).ema_indicator()
    df_h1["ema50"] = ema50_h1
    df_h1.dropna(inplace=True)

    out["df_5m"] = df_5m
    out["df_h1"] = df_h1
    return out

def vote_signal(packs: dict):
    """Voting antar komponen.
       Return: arah ("BUY"/"SELL"/None), score, detail_reason(list[str])
    """
    df5 = packs["df_5m"]
    dfh = packs["df_h1"]
    last5 = df5.iloc[-1]
    lastH = dfh.iloc[-1]

    reasons = []
    buy_votes = 0
    sell_votes = 0

    # 1) Trend filter (H1)
    if lastH["close"] > lastH["ema50"]:
        buy_votes += 1; reasons.append("Trend H1: Bullish (Close > EMA50)")
    else:
        sell_votes += 1; reasons.append("Trend H1: Bearish (Close < EMA50)")

    # 2) EMA alignment (5m)
    if last5["ema20"] > last5["ema50"]:
        buy_votes += 1; reasons.append("EMA 20>50 (5m) mendukung BUY")
    else:
        sell_votes += 1; reasons.append("EMA 20<50 (5m) mendukung SELL")

    # 3) MACD (5m)
    if last5["macd"] > last5["macdsig"]:
        buy_votes += 1; reasons.append("MACD bullish (5m)")
    else:
        sell_votes += 1; reasons.append("MACD bearish (5m)")

    # 4) ADX (trend strength)
    if last5["adx"] >= 20:
        reasons.append(f"Kekuatan trend (ADX 5m): {last5['adx']:.1f} (cukup)")
    else:
        reasons.append(f"Kekuatan trend (ADX 5m): {last5['adx']:.1f} (lemah)")

    # 5) RSI (signal timing)
    if last5["rsi"] < 35:
        buy_votes += 1; reasons.append(f"RSI 5m oversold ({last5['rsi']:.1f}) → BUY bounce")
    elif last5["rsi"] > 65:
        sell_votes += 1; reasons.append(f"RSI 5m overbought ({last5['rsi']:.1f}) → SELL pullback")

    # 6) Bollinger touch
    if last5["close"] <= last5["bb_low"]:
        buy_votes += 1; reasons.append("Menyentuh lower BB (5m) → reversion BUY")
    elif last5["close"] >= last5["bb_high"]:
        sell_votes += 1; reasons.append("Menyentuh upper BB (5m) → reversion SELL")

    # 7) Candle pattern
    candles = detect_candles(df5.tail(3))
    if any("Bullish" in p or "Morning" in p or "Hammer" in p for p in candles):
        buy_votes += 1; reasons.append("Candle pattern bullish")
    if any("Bearish" in p or "Evening" in p or "Shooting" in p for p in candles):
        sell_votes += 1; reasons.append("Candle pattern bearish")

    # 8) Chart pattern
    chartp = detect_chart_patterns(df5)
    if any("Bottom" in p or "Triangle" in p for p in chartp):
        buy_votes += 1; reasons.append("Chart pattern mendukung BUY")
    if any("Top" in p or "Triangle" in p for p in chartp):
        sell_votes += 1; reasons.append("Chart pattern mendukung SELL")

    # 9) SNR confluence (posisi harga relatif)
    sup_levels, res_levels = find_snr_levels(df5, lb=80, tol=0.3)
    nearest_sup, dist_sup = nearest_level(last5["close"], sup_levels)
    nearest_res, dist_res = nearest_level(last5["close"], res_levels)
    if nearest_sup and dist_sup is not None and dist_res is not None:
        if dist_sup < dist_res:
            buy_votes += 1; reasons.append("Dekat Support → BUY lebih aman")
        else:
            sell_votes += 1; reasons.append("Dekat Resistance → SELL lebih aman")

    # Keputusan
    if buy_votes >= 4 and buy_votes > sell_votes:
        arah = "BUY"
        score = buy_votes
    elif sell_votes >= 4 and sell_votes > buy_votes:
        arah = "SELL"
        score = sell_votes
    else:
        arah = None
        score = max(buy_votes, sell_votes)
    # Tambahkan catatan pola untuk laporan
    reasons.extend([f"Candle: {', '.join(candles) if candles else '-'}",
                    f"Chart: {', '.join(chartp) if chartp else '-'}"])
    return arah, score, reasons, sup_levels, res_levels

# ================== RISK, TP/SL & FORMAT ==================
def compute_rr_targets(price, arah, df5: pd.DataFrame, sup_levels, res_levels):
    """SL pakai ATR(14) 5m (1x), TP 1:2. Juga cari TP alternatif berdasarkan S/R terdekat."""
    atr = AverageTrueRange(df5["high"], df5["low"], df5["close"], window=14).average_true_range().iloc[-1]
    sl_dist = max(0.5, float(atr))  # minimal buffer
    if arah == "BUY":
        sl = round(price - sl_dist, 2)
        tp = round(price + 2 * sl_dist, 2)
        # TP SNR = resistance terdekat di atas harga
        res_above = [lv for lv in res_levels if lv > price]
        tp_snr = round(min(res_above), 2) if res_above else None
    else:
        sl = round(price + sl_dist, 2)
        tp = round(price - 2 * sl_dist, 2)
        sup_below = [lv for lv in sup_levels if lv < price]
        tp_snr = round(max(sup_below), 2) if sup_below else None
    return tp, sl, tp_snr

def format_message(symbol, timeframe, arah, price, tp, sl, tp_snr, score, reasons, df5: pd.DataFrame):
    last = df5.iloc[-1]
    adx = last.get("adx", float("nan"))
    rsi = last.get("rsi", float("nan"))
    ema20 = last.get("ema20", float("nan"))
    ema50 = last.get("ema50", float("nan"))

    now_wib = datetime.now(pytz.timezone("Asia/Jakarta")).strftime("%Y-%m-%d %H:%M:%S")
    conf_txt = "🟢 KUAT" if score >= 5 else ("🟡 MODERAT" if score == 4 else "🟠 RENDAH")

    header = f"**{symbol} — SIGNAL {timeframe}**\n🕒 {now_wib} WIB\n"
    direction_line = f"**Arah**: `{arah}`\n**Entry**: `{price}`\n**TP/SL (RR 1:2)**: `{tp}` / `{sl}`\n"
    if tp_snr:
        direction_line += f"**TP S/R**: `{tp_snr}` (alternatif)\n"

    tech = (
        f"**Confidence**: {conf_txt} (votes={score})\n"
        f"**Momentum**: RSI {rsi:.1f} | ADX {adx:.1f}\n"
        f"**Trend**: EMA20 {ema20:.2f} vs EMA50 {ema50:.2f}\n"
    )
    notes = "• " + "\n• ".join(reasons)

    msg = header + "\n" + direction_line + "\n" + tech + "\n**Alasan/Votes**:\n" + notes
    return msg

# ================== TASK: SEND SIGNAL ==================
async def send_signal(context: ContextTypes.DEFAULT_TYPE):
    try:
        if not is_bot_working_now():
            return
        if is_high_impact_window():
            # Hindari news window ringan
            await context.bot.send_message(chat_id=CHAT_ID, text="⏸️ Market news window — sinyal diskip sesi ini.")
            return

        # 1) Ambil candle 5m (sekali), siapkan H1 via resample
        candles = fetch_twelvedata_series(interval="5min", count=400)
        df5 = prepare_df(candles)
        if df5 is None or len(df5) < 60:
            return
        dfh = resample_to_h1(df5)
        if dfh is None or len(dfh) < 10:
            return

        # 2) Hitung indikator & Voting
        packs = compute_indicators(df5.copy(), dfh.copy())
        arah, score, reasons, sup_levels, res_levels = vote_signal(packs)
        if arah is None:
            # Tidak cukup konfirmasi — tidak kirim sinyal
            print("ℹ️ No consensus signal this cycle.")
            return

        # 3) Ambil harga eksekusi hemat
        price_live = fetch_realtime_price_metals_fast() or df5["close"].iloc[-1]

        # 4) Hitung TP/SL (RR 1:2) + TP SNR
        tp, sl, tp_snr = compute_rr_targets(price_live, arah, packs["df_5m"], sup_levels, res_levels)

        # 5) Kirim sticker + pesan
        try:
            if arah == "BUY":
                await context.bot.send_sticker(chat_id=CHAT_ID, sticker=STICKER_BUY)
            else:
                await context.bot.send_sticker(chat_id=CHAT_ID, sticker=STICKER_SELL)
        except Exception as e:
            # Fallback ke emoji jika sticker invalid
            arrow = "🟢⬆️" if arah == "BUY" else "🔴⬇️"
            await context.bot.send_message(chat_id=CHAT_ID, text=arrow)

        msg = format_message(
            symbol="XAU/USD",
            timeframe="M5",
            arah=arah,
            price=price_live,
            tp=tp,
            sl=sl,
            tp_snr=tp_snr,
            score=score,
            reasons=reasons,
            df5=packs["df_5m"],
        )
        await context.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")
    except Exception as e:
        print("❌ send_signal error:", e)

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

    # Kirim sinyal TIAP 30 MENIT (Sesuai permintaan), weekend libur via is_bot_working_now()
    bot_app.job_queue.run_repeating(send_signal, interval=1800, first=10)

    print("🤖 Bot berjalan...")
    bot_app.run_polling()

if __name__ == "__main__":
    main()
