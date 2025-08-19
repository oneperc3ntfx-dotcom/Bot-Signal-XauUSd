# -*- coding: utf-8 -*-
"""
Bot sinyal XAU/USD: S/R (Pivot) + RSI — selalu keluarkan BUY/SELL (tanpa WAIT)
Jadwal: tiap 30 menit, aktif Senin 07:00 WIB s/d Jumat 24:00 (Sabtu 00:00) WIB
"""

# --- Compat: event loop untuk Windows/Python 3.12+ ---
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

# ============== KONFIGURASI ==============
# (Menggunakan yang Anda berikan. Disarankan revoke & ganti token setelah tes)
BOT_TOKEN = "8114552558:AAFpnQEYHYa8P43g5rjOwPs5TSbjtYh9zS4"
CHAT_ID = "-1002883903673"           # Grup tujuan
AUTHORIZED_USER_ID = 1305881282      # Hanya user ini boleh /start
API_KEY = "21a0860958e641cc934bec6277415088"  # TwelveData

SYMBOL = "XAU/USD"
INTRVAL = "5min"     # TF intraday untuk RSI & harga realtime
RSI_WINDOW = 14

# Parameter strategi (silakan sesuaikan)
NEAR_BAND_USD = 2.5  # dianggap "dekat" dengan S1/R1 jika jaraknya <= nilai ini
TP_USD = 3.0         # target profit default (USD)
SL_USD = 1.5         # stop loss default (USD)

# ============== KEEP ALIVE (opsional server) ==============
app = Flask(__name__)
@app.route("/")
def home():
    return "Bot is running"

def keep_alive():
    Thread(target=lambda: app.run(host="0.0.0.0", port=8080)).start()

# ============== UTIL WAKTU & JADWAL ==============
JKT = pytz.timezone("Asia/Jakarta")

def is_bot_working_now(now: datetime | None = None) -> bool:
    """Aktif Sen 07:00 WIB s/d Jumat 24:00 WIB (Sabtu & Minggu off)."""
    if now is None:
        now = datetime.now(JKT)
    wd = now.weekday()  # 0=Mon ... 6=Sun
    t = now.time()
    if wd in (5, 6):           # Sabtu, Minggu
        return False
    if wd == 0 and t < time(7, 0):  # Senin sebelum 07:00
        return False
    return True

# ============== DATA SOURCE ==============
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
        return values[::-1]  # jadikan ascending by time
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

# ============== SUPPORT/RESISTANCE (PIVOT) ==============
def daily_pivot_levels() -> dict | None:
    """Hitung Pivot, S1, R1 dari candle harian kemarin (bar terakhir yang lengkap)."""
    daily_vals = fetch_time_series(SYMBOL, "1day", count=3)
    if not daily_vals:
        return None
    ddf = to_df(daily_vals)
    if ddf is None or len(ddf) < 2:
        return None

    yesterday = ddf.iloc[-2]  # bar harian yang telah closed
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
    """
    Paksa pilih BUY/SELL (tanpa WAIT) berbasis skor gabungan:
    - Kedekatan ke S1/R1
    - Posisi relatif terhadap Pivot
    - RSI threshold (bias BUY < 50, bias SELL >= 50)
    - Ekstra poin bila sangat dekat (<= 1/2 band)
    """
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

    # Agresif RSI extremes
    if rsi_last <= 35: buy_score += 1
    if rsi_last >= 65: sell_score += 1

    # Kedekatan ke level
    if near_s: buy_score += 1
    if near_r: sell_score += 1

    # Posisi terhadap pivot
    if price <= levels["pivot"]: buy_score += 1
    else: sell_score += 1

    # Bonus bila sangat dekat
    half = NEAR_BAND_USD / 2
    if dist_s <= half: buy_score += 1
    if dist_r <= half: sell_score += 1

    if buy_score > sell_score:
        arah = "BUY"
    elif sell_score > buy_score:
        arah = "SELL"
    else:
        # Tie-breaker: pilih sisi yang lebih dekat
        arah = "BUY" if dist_s <= dist_r else "SELL"

    # Catatan analisa
    if arah == "BUY":
        if near_s: notes.append(f"✅ Dekat SUPPORT S1 ≈ {levels['s1']:.2f} (jarak {dist_s:.2f})")
        if rsi_last < 50: notes.append(f"✅ RSI {rsi_last:.1f} < 50 (bias BUY)")
        if rsi_last <= 35: notes.append("✅ RSI ≤ 35 (cukup oversold)")
        if price <= levels["pivot"]: notes.append(f"✅ Harga ≤ Pivot ({levels['pivot']:.2f})")
        if not notes: notes.append("ℹ️ Kondisi netral, memilih BUY (tie-break ke support)")
    else:
        if near_r: notes.append(f"✅ Dekat RESISTANCE R1 ≈ {levels['r1']:.2f} (jarak {dist_r:.2f})")
        if rsi_last >= 50: notes.append(f"✅ RSI {rsi_last:.1f} ≥ 50 (bias SELL)")
        if rsi_last >= 65: notes.append("✅ RSI ≥ 65 (cukup overbought)")
        if price > levels["pivot"]: notes.append(f"✅ Harga > Pivot ({levels['pivot']:.2f})")
        if not notes: notes.append("ℹ️ Kondisi netral, memilih SELL (tie-break ke resistance)")

    # Strength (indikatif)
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
        print("❌ Gagal hitung RSI dari intraday.")
        return None

    price = float(df["close"].iloc[-1])
    arah, strength, notes = choose_direction(price, rsi_last, levels)

    # TP/SL sederhana berbasis arah
    if arah == "BUY":
        tp = round(price + TP_USD, 2)
        sl = round(price - SL_USD, 2)
    else:
        tp = round(price - TP_USD, 2)
        sl = round(price + SL_USD, 2)

    note_text = "\n".join(notes)
    extra = {
        "pivot": levels["pivot"],
        "s1": levels["s1"],
        "r1": levels["r1"],
        "y_high": levels["y_high"],
        "y_low": levels["y_low"],
        "y_close": levels["y_close"],
        "rsi": rsi_last
    }
    return arah, strength, price, tp, sl, note_text, extra

# ============== NEWS FILTER (ForexFactory) ==============
def check_high_impact_news() -> bool:
    """True jika ada high-impact news ±30 menit dari sekarang (WIB)."""
    try:
        url = "https://www.forexfactory.com/calendar.php?week=this"
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(url, headers=headers, timeout=12)
        if resp.status_code != 200:
            return False

        soup = BeautifulSoup(resp.text, "html.parser")
        rows = soup.select("tr.calendar__row")

        now_jkt = datetime.now(JKT)

        for row in rows:
            imp = row.select_one("td.calendar__impact")
            tcell = row.select_one("td.calendar__time")
            if not imp or not tcell:
                continue

            if "high" not in (imp.get("title", "") + imp.get_text(" ")).lower():
                continue

            time_txt = tcell.get_text(strip=True)
            if not time_txt or time_txt.lower() in ("all day", "tentative"):
                continue

            try:
                hh, mm = time_txt.split(":")
                news_time_local = datetime.now(pytz.timezone("America/New_York")).replace(
                    hour=int(hh), minute=int(mm), second=0, microsecond=0
                )
                news_time_wib = news_time_local.astimezone(JKT)
            except Exception:
                continue

            if abs((news_time_wib - now_jkt).total_seconds()) <= 1800:
                return True

        return False
    except Exception as e:
        print(f"❌ Error cek news: {e}")
        return False

# ============== SENDER ==============
async def send_signal(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(JKT)
    if not is_bot_working_now(now):
        print(f"⏱️ Di luar jam kerja bot: {now.strftime('%a %H:%M:%S')} WIB")
        return

    if check_high_impact_news():
        await context.bot.send_message(chat_id=CHAT_ID, text="🚨 High impact news ±30 menit. Sinyal diskip.")
        return

    res = generate_signal()
    if not res:
        await context.bot.send_message(chat_id=CHAT_ID, text="❌ Gagal menghasilkan sinyal (data tidak cukup).")
        return

    arah, status, entry, tp, sl, note, extra = res
    tnow = now.strftime("%Y-%m-%d %H:%M:%S")

    msg = (
        f"📡 *Sinyal XAU/USD* (S/R + RSI)\n"
        f"🕒 {tnow} WIB\n"
        f"📈 Arah: *{arah}*\n"
        f"💰 Entry: `{entry:.2f}`\n"
        f"🎯 TP: `{tp:.2f}`\n"
        f"🛑 SL: `{sl:.2f}`\n"
        f"📊 Status: {status}\n"
        f"\n🔍 *Analisa*\n{note}"
        f"\n\n— Pivot: `{extra['pivot']:.2f}` | S1: `{extra['s1']:.2f}` | R1: `{extra['r1']:.2f}`"
        f"\n— H/L/C (kemarin): `{extra['y_high']:.2f}` / `{extra['y_low']:.2f}` / `{extra['y_close']:.2f}`"
        f"\n— RSI(14) 5m: `{extra['rsi']:.1f}`"
    )

    await context.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")

# ============== COMMANDS ==============
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != AUTHORIZED_USER_ID:
        await update.message.reply_text("🚫 Anda tidak diizinkan.")
        return
    await update.message.reply_text("✅ Bot aktif. Sinyal tiap 30 menit (selalu BUY/SELL).")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("/start — aktifkan bot\n/help — bantuan\n/info — info bot")

async def info_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Bot sinyal XAU/USD (S/R + RSI)\n"
        "• Interval kirim: 30 menit\n"
        "• Jam kerja: Senin 07:00 WIB s/d Jumat 24:00 WIB\n"
        "• Saat ada high impact news ±30 menit: sinyal diskip"
    )

async def unknown_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❓ Perintah tidak dikenali.")

# ============== MAIN APP ==============
def main():
    try:
        keep_alive()  # opsional; aman bila lokal juga
    except Exception:
        pass

    app_ = ApplicationBuilder().token(BOT_TOKEN).build()

    app_.add_handler(CommandHandler("start", start))
    app_.add_handler(CommandHandler("help", help_cmd))
    app_.add_handler(CommandHandler("info", info_cmd))
    app_.add_handler(MessageHandler(filters.COMMAND, unknown_cmd))

    jq = app_.job_queue
    # Kirim sinyal setiap 30 menit
    jq.run_repeating(send_signal, interval=1800, first=0)

    # Kirim 1x saat startup jika dalam jam kerja
    async def startup_once(context: ContextTypes.DEFAULT_TYPE):
        if is_bot_working_now():
            await send_signal(context)
    jq.run_once(startup_once, when=0)

    print("🚀 Bot berjalan...")
    app_.run_polling()

if __name__ == "__main__":
    main()
