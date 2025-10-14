import os
import time
import requests
import random
import schedule
import datetime
from telegram import Bot, Update
from telegram.ext import Updater, CommandHandler, CallbackContext

# ==========================
# Load Environment Variables
# ==========================
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
AUTHORIZED_USER_ID = int(os.getenv("AUTHORIZED_USER_ID", "0"))
PAIR_SYMBOL = os.getenv("PAIR_SYMBOL", "XAU/USD")

bot = Bot(token=BOT_TOKEN)
last_signal_time = None


# ==========================
# Fetch Realtime Price (Yahoo Finance)
# ==========================
def fetch_realtime_price():
    """
    Ambil harga realtime dari Yahoo Finance.
    Contoh simbol:
    - XAU/USD → XAUUSD=X
    - EUR/USD → EURUSD=X
    - GBP/USD → GBPUSD=X
    """
    try:
        yahoo_symbol = f"{PAIR_SYMBOL.replace('/', '')}=X"
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_symbol}?interval=1m"
        res = requests.get(url, timeout=10).json()

        price = res["chart"]["result"][0]["meta"]["regularMarketPrice"]
        if price:
            return round(price, 2)
    except Exception as e:
        print("⚠️ Yahoo Finance error:", e)
    return None


# ==========================
# Generate Random Signal
# ==========================
def generate_signal():
    """Random BUY/SELL dengan harga realtime dan TP/SL tetap"""
    global last_signal_time
    price = fetch_realtime_price()

    if not price:
        bot.send_message(chat_id=CHAT_ID, text="⚠️ Gagal mengambil harga realtime dari Yahoo Finance.")
        return

    direction = random.choice(["BUY", "SELL"])
    pip_value = 0.1  # 1 pip = 0.1 untuk XAU/USD (bisa disesuaikan)

    if direction == "BUY":
        tp1 = round(price + (25 * pip_value), 2)
        tp2 = round(price + (50 * pip_value), 2)
        sl = round(price - (15 * pip_value), 2)
    else:
        tp1 = round(price - (25 * pip_value), 2)
        tp2 = round(price - (50 * pip_value), 2)
        sl = round(price + (15 * pip_value), 2)

    message = (
        f"📊 Pair: {PAIR_SYMBOL}\n"
        f"🕒 Time: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"💰 Harga Entry: {price}\n"
        f"📈 Arah: {direction}\n"
        f"🎯 TP1: {tp1}\n"
        f"🎯 TP2: {tp2}\n"
        f"🛑 SL: {sl}\n\n"
        "⚠️ GUNAKAN MONEY MANAGEMENT SESUAI EQUITAS , JANGAN FULL MARGIN !!"
    )

    bot.send_message(chat_id=CHAT_ID, text=message)
    last_signal_time = datetime.datetime.now()


# ==========================
# Telegram Commands
# ==========================
def start(update: Update, context: CallbackContext):
    update.message.reply_text(
        "🤖 Halo! Bot signal random aktif.\n\n"
        "Perintah yang tersedia:\n"
        "• /harga → Cek harga realtime\n"
        "• /signal → Kirim sinyal BUY/SELL acak\n"
        "• /status → Lihat status bot\n"
    )


def harga(update: Update, context: CallbackContext):
    price = fetch_realtime_price()
    if price:
        update.message.reply_text(f"💰 Harga {PAIR_SYMBOL} saat ini: {price}")
    else:
        update.message.reply_text("⚠️ Gagal mengambil harga realtime dari Yahoo Finance.")


def status(update: Update, context: CallbackContext):
    if last_signal_time:
        msg = f"✅ Bot aktif.\n🕒 Sinyal terakhir: {last_signal_time.strftime('%Y-%m-%d %H:%M:%S')}"
    else:
        msg = "✅ Bot aktif dan berjalan normal.\n❌ Belum ada sinyal dikirim."
    update.message.reply_text(msg)


def manual_signal(update: Update, context: CallbackContext):
    generate_signal()
    update.message.reply_text("✅ Sinyal acak dikirim ke channel.")


# ==========================
# Scheduler (Setiap Jam 00:00 WIB)
# ==========================
def job():
    generate_signal()


# ==========================
# Main
# ==========================
updater = Updater(BOT_TOKEN, use_context=True)
dp = updater.dispatcher

dp.add_handler(CommandHandler("start", start))
dp.add_handler(CommandHandler("harga", harga))
dp.add_handler(CommandHandler("status", status))
dp.add_handler(CommandHandler("signal", manual_signal))

# Jadwal kirim sinyal otomatis jam 00:00 WIB (17:00 UTC di Railway)
schedule.every().day.at("17:00").do(job)

print("🤖 Bot signal random aktif (Yahoo Finance). Menunggu jadwal 00:00 WIB ...")

# Kirim sinyal awal saat bot pertama kali aktif
try:
    print("🚀 Mengirim sinyal awal setelah deploy...")
    generate_signal()
except Exception as e:
    print("❌ Gagal mengirim sinyal awal:", e)

updater.start_polling()

while True:
    schedule.run_pending()
    time.sleep(1)
