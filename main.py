# -*- coding: utf-8 -*-
"""
Bot sinyal XAU/USD: S/R (Pivot) + RSI — selalu keluarkan BUY/SELL (tanpa WAIT)
Jadwal: tiap 30 menit, aktif Senin 07:00 WIB s/d Jumat 24:00 WIB
"""

# --- Kompatibilitas event loop untuk Windows / Python 3.12+ ---
import asyncio
try:
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
except Exception:
    asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())

# --- Imports ---
from flask import Flask
from threading import Thread
import requests
from datetime import datetime, time
import pytz
import pandas as pd
from ta.momentum import RSIIndicator
from bs4 import BeautifulSoup

from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes,
    MessageHandler, filters
)

# ============== KONFIGURASI BOT ==============
BOT_TOKEN = "8114552558:AAFpnQEYHYa8P43g5rjOwPs5TSbjtYh9zS4"
CHAT_ID = "-1002883903673"           # Grup tujuan
AUTHORIZED_USER_ID = 1305881282      # Hanya user ini boleh /start
API_KEY = "21a0860958e641cc934bec6277415088"  # TwelveData

SYMBOL = "XAU/USD"
INTRVAL = "5min"     # Timeframe untuk RSI intraday
RSI_WINDOW = 14

# Parameter strategi
NEAR_BAND_USD = 2.5
TP_USD = 3.0
SL_USD = 1.5

# ============== KEEP ALIVE (opsional server) ==============
app = Flask(__name__)
@app.route("/")
def home():
    return "Bot berjalan"

def keep_alive():
    Thread(target=lambda: app.run(host="0.0.0.0", port=8080)).start()

# ============== UTIL WAKTU & JADWAL ==============
JKT = pytz.timezone("Asia/Jakarta")

def is_bot_working_now(now: datetime | None = None) -> bool:
    """Cek apakah bot aktif: Senin 07:00 s/d Jumat 24:00 WIB"""
    if now is None:
        now = datetime.now(JKT)
    wd = now.weekday()
    t = now.time()
    if wd in (5, 6):  # Sabtu & Minggu off
        return False
    if wd == 0 and t < time(7, 0):  # Senin sebelum 07:00 WIB
        return False
    return True

# ============== AMBIL DATA TIME SERIES ==============
def fetch_time_series(symbol: str, interval: str, count: int = 200):
    url = (
        f"https://api.twelvedata.com/time_series"
        f"?symbol={symbol}&interval={interval}&apikey={API_KEY}&outputsize={count}&format=JSON"
    )
    try:
        r = requests.get(url, timeout=15)
        if r.status_code != 200:
            print(f"❌ HTTP {r.status_code} saat fetch {interval}")
            return None
        values = r.json().get("values", [])
        return values[::-1]  # urut dari lama ke baru
    except Exception as e:
        print(f"❌ Error fetch_time_series({interval}): {e}")
        return None

def to_df(values):
    try:
        df = pd.DataFrame(values)
        df["datetime"] = pd.to_datetime(df["datetime"])
        df.set_index("datetime", inplace=True)
        for col in ("open", "high", "low", "close"):
            df[col] = df[col].astype(float)
        return df
    except Exception as e:
        print(f"❌ Error to_df: {e}")
        return None

# ============== HARGA REALTIME ==============
def fetch_realtime_price(symbol: str) -> float | None:
    """Ambil harga terakhir dari TwelveData"""
    url = f"https://api.twelvedata.com/quote?symbol={symbol}&apikey={API_KEY}"
    try:
        r = requests.get(url, timeout=10)
        if r.status_code != 200:
            print(f"❌ HTTP {r.status_code} saat fetch quote")
            return None
        data = r.json()
        if "close" in data:
            return float(data["close"])
        return None
    except Exception as e:
        print(f"❌ Error fetch_realtime_price: {e}")
        return None

# ============== SUPPORT/RESISTANCE (PIVOT) ==============
def daily_pivot_levels() -> dict | None:
    """Hitung pivot, S1, R1 dari data kemarin"""
    daily_vals = fetch_time_series(SYMBOL, "1day", count=3)
    if not daily_vals:
        return None
    ddf = to_df(daily_vals)
    if ddf is None or len(ddf) < 2:
        return None

    yesterday = ddf.iloc[-2]
    H, L, C = yesterday["high"], yesterday["low"], yesterday["close"]
    pivot = (H + L + C) / 3.0
    s1 = 2 * pivot - H
    r1 = 2 * pivot - L
    return {"pivot": pivot, "s1": s1, "r1": r1, "y_high": H, "y_low": L, "y_close": C}

# ============== RSI & SINYAL ==============
def rsi_from_intraday() -> tuple[pd.DataFrame | None, float | None]:
    vals = fetch_time_series(SYMBOL, INTRVAL, count=200)
    if not vals:
        return None, None
    df = to_df(vals)
    if df is None or len(df) < RSI_WINDOW + 5:
        return None, None
    rsi = RSIIndicator(df["close"], window=RSI_WINDOW).rsi()
    df["rsi"] = rsi
    return df, float(rsi.iloc[-1])

def choose_direction(price: float, rsi_last: float, levels: dict) -> tuple[str, str, list[str]]:
    """Tentukan BUY/SELL dan catatan analisa"""
    notes = []
    dist_s = abs(price - levels["s1"])
    dist_r = abs(price - levels["r1"])
    near_s = dist_s <= NEAR_BAND_USD
    near_r = dist_r <= NEAR_BAND_USD

    buy_score = 0
    sell_score = 0

    # Bias RSI
    if rsi_last < 50: buy_score += 1
    else: sell_score += 1

    if rsi_last <= 35: buy_score += 1
    if rsi_last >= 65: sell_score += 1

    if near_s: buy_score += 1
    if near_r: sell_score += 1

    if price <= levels["pivot"]: buy_score += 1
    else: sell_score += 1

    half = NEAR_BAND_USD / 2
    if dist_s <= half: buy_score += 1
    if dist_r <= half: sell_score += 1

    # Tentukan arah
    if buy_score > sell_score:
        arah = "BUY"
    elif sell_score > buy_score:
        arah = "SELL"
    else:
        arah = "BUY" if dist_s <= dist_r else "SELL"

    # Catatan analisa
    if arah == "BUY":
        if near_s: notes.append(f"✅ Dekat SUPPORT S1 ≈ {levels['s1']:.2f} (jarak {dist_s:.2f})")
        if rsi_last < 50: notes.append(f"✅ RSI {rsi_last:.1f} < 50 (bias BUY)")
        if rsi_last <= 35: notes.append("✅ RSI ≤ 35 (oversold)")
        if price <= levels["pivot"]: notes.append(f"✅ Harga ≤ Pivot ({levels['pivot']:.2f})")
        if not notes: notes.append("ℹ️ Netral, pilih BUY (tie-break ke support)")
    else:
        if near_r: notes.append(f"✅ Dekat RESISTANCE R1 ≈ {levels['r1']:.2f} (jarak {dist_r:.2f})")
        if rsi_last >= 50: notes.append(f"✅ RSI {rsi_last:.1f} ≥ 50 (bias SELL)")
        if rsi_last >= 65: notes.append("✅ RSI ≥ 65 (overbought)")
        if price > levels["pivot"]: notes.append(f"✅ Harga > Pivot ({levels['pivot']:.2f})")
        if not notes: notes.append("ℹ️ Netral, pilih SELL (tie-break ke resistance)")

    diff = abs(buy_score - sell_score)
    strength = "🟢 KUAT" if diff >= 2 else "🟡 MODERAT"
    return arah, strength, notes

def generate_signal() -> tuple[str, str, float, float, float, str, dict] | None:
    levels = daily_pivot_levels()
    if not levels:
        print("❌ Gagal ambil level pivot harian.")
        return None

    df, rsi_last = rsi_from_intraday()
    if df is None or rsi_last is None:
        print("❌ Gagal hitung RSI intraday.")
        return None

    price = fetch_realtime_price(SYMBOL)
    if price is None:
        print("❌ Gagal ambil harga realtime.")
        return None

    arah, strength, notes = choose_direction(price, rsi_last, levels)
    # Hitung TP/SL
    tp = price + TP_USD if arah == "BUY" else price - TP_USD
    sl = price - SL_USD if arah == "BUY" else price + SL_USD
    return (
        SYMBOL, arah, price, tp, sl, strength, notes
    )

# ============== FILTER BERITA FOREX ==============
def check_high_impact_news() -> list[str]:
    """Cek berita berdampak tinggi ±30 menit di ForexFactory"""
    url = "https://www.forexfactory.com/calendar.php?week=this"
    try:
        r = requests.get(url, timeout=10)
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.content, "html.parser")
        news_items = []
        for row in soup.select(".calendar__row"):
            impact = row.select_one(".impact span")
            time_cell = row.select_one(".calendar__time")
            if not impact or not time_cell:
                continue
            if "High" in impact.text:
                news_items.append(row.get_text(strip=True))
        return news_items
    except Exception:
        return []

# ============== TELEGRAM BOT ==============
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != AUTHORIZED_USER_ID:
        await update.message.reply_text("❌ Anda tidak berhak menggunakan bot ini")
        return
    await update.message.reply_text(
        "🤖 Bot sinyal XAU/USD aktif.\n"
        "Sinyal dikirim otomatis tiap 30 menit jika market buka."
    )

async def send_signal(update: Update | None, context: ContextTypes.DEFAULT_TYPE):
    if not is_bot_working_now():
        return
    news = check_high_impact_news()
    if news:
        print("⚠️ Berita berdampak tinggi, skip sinyal")
        return

    sig = generate_signal()
    if sig is None:
        return

    sym, arah, price, tp, sl, strength, notes = sig
    msg = f"📡 *Sinyal {sym}*\n"
    msg += f"🕒 {datetime.now(JKT).strftime('%d-%m-%Y %H:%M:%S')}\n"
    msg += f"📈 Arah: {arah}\n"
    msg += f"💰 Entry: {price:.2f}\n"
    msg += f"🎯 TP: {tp:.2f}\n"
    msg += f"🛑 SL: {sl:.2f}\n"
    msg += f"📊 Status: {strength}\n"
    msg += "\n".join(notes)

    if update:
        await update.message.reply_text(msg)
    else:
        await context.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")

# ============== MAIN ==============
async def main():
    keep_alive()
    app_telegram = ApplicationBuilder().token(BOT_TOKEN).build()
    app_telegram.add_handler(CommandHandler("start", start))
    # Kirim sinyal manual via /signal
    app_telegram.add_handler(CommandHandler("signal", send_signal))

    # Kirim otomatis tiap 30 menit
    async def job():
        while True:
            await send_signal(None, app_telegram)
            await asyncio.sleep(1800)  # 30 menit

    asyncio.create_task(job())
    await app_telegram.run_polling()

# =================================================
if __name__ == "__main__":
    asyncio.run(main())
