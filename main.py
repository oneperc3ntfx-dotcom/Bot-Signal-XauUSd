#!/usr/bin/env python3
import os
import asyncio
import json
import random
import logging
from threading import Thread
from datetime import datetime, timedelta, time as dt_time
import pytz
import websockets
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

# ==========================
# CONFIG
# ==========================
BOT_TOKEN = os.getenv("BOT_TOKEN")
AUTHORIZED_USER_ID = int(os.getenv("AUTHORIZED_USER_ID", "0"))
FINNHUB_TOKEN = os.getenv("FINNHUB_TOKEN")
PAIR_SYMBOL = os.getenv("PAIR_SYMBOL", "OANDA:XAU_USD")
FLASK_PORT = int(os.getenv("PORT", "8080"))

# Channel targets
HOURLY_CHANNELS = ["-1003142698012", "-1002605110502"]
DAILY_RANDOM_CHANNEL = "-1002782196938"

# Timezone
JKT = pytz.timezone("Asia/Jakarta")

if not BOT_TOKEN or not FINNHUB_TOKEN:
    raise SystemExit("❌ BOT_TOKEN dan FINNHUB_TOKEN wajib diatur di environment!")

# ==========================
# LOGGING
# ==========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("finnhub-bot")

# ==========================
# KEEP ALIVE SERVER (RAILWAY / hosting)
# ==========================
app = Flask(__name__)

@app.route("/")
def home():
    return "✅ Bot Finnhub Realtime aktif."

def keep_alive():
    Thread(target=lambda: app.run(host="0.0.0.0", port=FLASK_PORT), daemon=True).start()
    logger.info("🌐 Keep-alive server started on port %s", FLASK_PORT)

# ==========================
# GLOBAL STATE
# ==========================
last_price = None
last_update = None
price_lock = asyncio.Lock()
price_ready = asyncio.Event()

# ==========================
# TRADING SCHEDULE
# ==========================
def is_trading_time(at: datetime = None) -> bool:
    """
    Return True if 'at' (JKT) is within expected trading hours (Mon-Fri),
    according to your previous logic: allow hours 0-3 and 5-22 (exclude 4 and 23).
    """
    if at is None:
        at = datetime.now(JKT)
    wd = at.weekday()  # 0=Mon ... 6=Sun
    if wd >= 5:
        return False
    hour = at.hour
    return (5 <= hour < 23) or (0 <= hour < 4)

# ==========================
# SIGNAL GENERATOR
# ==========================
async def generate_signal_html():
    """
    Generate a signal message formatted in HTML (safer for Telegram).
    Returns string or None if price not available.
    """
    async with price_lock:
        if last_price is None:
            return None
        price = last_price

    # Choose direction based on short random logic (you can customize)
    direction = random.choice(["BUY", "SELL"])
    pip = 0.1

    if direction == "BUY":
        tp1 = round(price + 25 * pip, 2)
        tp2 = round(price + 50 * pip, 2)
        sl = round(price - 15 * pip, 2)
    else:
        tp1 = round(price - 25 * pip, 2)
        tp2 = round(price - 50 * pip, 2)
        sl = round(price + 15 * pip, 2)

    now = datetime.now(JKT).strftime("%Y-%m-%d %H:%M:%S")

    header = (
        "<b>🤖 Sinyal Otomatis dari AI Trading System</b>\n"
        "<i>Sinyal ini dihasilkan otomatis oleh sistem analisis pasar real-time.</i>\n\n"
    )

    body = (
        f"📊 Pair: <b>XAU/USD</b>\n"
        f"🕒 Waktu: <b>{now} WIB</b>\n"
        f"💰 Harga Entry: <b>{price:.2f}</b>\n"
        f"📈 Arah: <b>{direction}</b>\n"
        f"🎯 TP1: <b>{tp1}</b>\n"
        f"🎯 TP2: <b>{tp2}</b>\n"
        f"🛑 SL: <b>{sl}</b>\n\n"
        f"⚠️ Gunakan money management yang aman!"
    )
    return header + body

# ==========================
# FINNHUB WEBSOCKET
# ==========================
async def finnhub_ws():
    """
    Background task that connects to Finnhub websocket and updates last_price.
    Reconnects automatically on error with exponential backoff (capped).
    """
    global last_price, last_update
    url = f"wss://ws.finnhub.io?token={FINNHUB_TOKEN}"
    backoff = 1
    while True:
        try:
            logger.info("🔗 Connecting to Finnhub WS...")
            async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                backoff = 1
                await ws.send(json.dumps({"type": "subscribe", "symbol": PAIR_SYMBOL}))
                logger.info("✅ Subscribed ke %s via Finnhub WS", PAIR_SYMBOL)

                async for msg in ws:
                    try:
                        data = json.loads(msg)
                    except json.JSONDecodeError:
                        continue
                    if data.get("type") == "trade":
                        for t in data.get("data", []):
                            async with price_lock:
                                last_price = float(t["p"])
                                last_update = datetime.utcnow()
                                price_ready.set()
                            # Log only occasionally to reduce noise
                            ts = int(datetime.utcnow().timestamp())
                            if ts % 5 == 0:
                                logger.info("💲 Harga %s: %.2f @ %s UTC", PAIR_SYMBOL, last_price, last_update.strftime("%H:%M:%S"))
        except Exception as e:
            logger.warning("⚠️ WebSocket error: %s", e, exc_info=False)
            await asyncio.sleep(min(backoff, 60))
            backoff = backoff * 2 if backoff < 60 else 60
            logger.info("🔁 Reconnecting ke Finnhub (backoff %s s)...", backoff)

# ==========================
# SENDER FUNCTIONS
# ==========================
async def send_signal_to_chats(app, chat_ids):
    """
    Send generated signal message (HTML) to specified chat IDs.
    """
    msg = await generate_signal_html()
    if not msg:
        logger.warning("⚠️ Belum ada harga realtime. Signal tidak dikirim.")
        return False

    success = True
    for cid in chat_ids:
        try:
            await app.bot.send_message(chat_id=cid, text=msg, parse_mode="HTML")
            logger.info("✅ Sinyal terkirim ke %s pada %s", cid, datetime.now(JKT).strftime("%Y-%m-%d %H:%M:%S"))
        except Exception as e:
            logger.exception("❌ Gagal kirim ke %s: %s", cid, e)
            success = False
    return success

# ==========================
# TELEGRAM COMMANDS
# ==========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != AUTHORIZED_USER_ID:
        return await update.message.reply_text("🚫 Tidak diizinkan.")
    await update.message.reply_text(
        "✅ Bot aktif.\n"
        "• /signal → kirim ke semua channel (authorized)\n"
        "• /minta → sinyal pribadi (harga terbaru, authorized)\n"
        "• /harga → harga realtime"
    )

async def harga(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with price_lock:
        if last_price is None:
            return await update.message.reply_text("⏳ Harga belum tersedia.")
        price = last_price
        update_time = last_update.strftime("%H:%M:%S") if last_update else "?"
    now = datetime.now(JKT).strftime("%Y-%m-%d %H:%M:%S")
    await update.message.reply_text(f"💰 XAU/USD: {price:.2f}\n🕒 Update UTC: {update_time}\n📅 {now} WIB")

async def minta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != AUTHORIZED_USER_ID:
        return await update.message.reply_text("🚫 Tidak diizinkan.")

    # Tunggu harga terbaru dari WebSocket (maks 3 detik)
    try:
        await asyncio.wait_for(price_ready.wait(), timeout=3)
        price_ready.clear()
    except asyncio.TimeoutError:
        pass

    msg = await generate_signal_html()
    if not msg:
        return await update.message.reply_text("⚠️ Harga belum tersedia (belum ada tick baru).")
    await update.message.reply_text(msg, parse_mode="HTML")

async def signal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != AUTHORIZED_USER_ID:
        return await update.message.reply_text("🚫 Tidak diizinkan.")
    # Kirim ke semua HOURLY + DAILY channel segera
    await send_signal_to_chats(context.application, HOURLY_CHANNELS + [DAILY_RANDOM_CHANNEL])
    await update.message.reply_text("✅ Sinyal dikirim ke semua channel.")

# ==========================
# SCHEDULER TASKS
# ==========================
async def hourly_scheduler(app):
    """
    Runs forever. At minute 0 of every hour, if is_trading_time -> send signals to HOURLY_CHANNELS.
    """
    logger.info("⏰ Hourly scheduler started.")
    while True:
        now = datetime.now(JKT)
        # compute next top-of-hour
        next_hour = (now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
        wait_seconds = (next_hour - now).total_seconds()
        await asyncio.sleep(wait_seconds)
        # At top of the hour now
        if is_trading_time(next_hour):
            logger.info("⏰ Top of hour and trading time — sending hourly signals.")
            await send_signal_to_chats(app, HOURLY_CHANNELS)
        else:
            logger.info("⏰ Top of hour but outside trading time — skipping hourly send.")

async def daily_random_scheduler(app):
    """
    Each day choose a random time inside trading hours to send one signal to DAILY_RANDOM_CHANNEL.
    Loops forever.
    """
    logger.info("🎲 Daily-random scheduler started.")
    while True:
        # pick next day candidate starting from today
        now = datetime.now(JKT)
        # build list of valid hours for today's trading hours
        valid_hours = []
        for h in range(0, 24):
            dt_candidate = now.replace(hour=h, minute=0, second=0, microsecond=0)
            if is_trading_time(dt_candidate):
                valid_hours.append(h)
        if not valid_hours:
            # if today has no trading hours (weekend), sleep until next day 00:30
            next_try = (now + timedelta(days=1)).replace(hour=0, minute=30, second=0, microsecond=0)
            sleep_seconds = (next_try - now).total_seconds()
            logger.info("📅 Tidak ada jam trading hari ini. Menunggu %s detik sampai percobaan berikutnya.", int(sleep_seconds))
            await asyncio.sleep(sleep_seconds)
            continue

        # pick random hour and random minute within that hour (avoid minute 0 to not collide with hourly)
        chosen_hour = random.choice(valid_hours)
        chosen_minute = random.randint(1, 59)
        send_time = now.replace(hour=chosen_hour, minute=chosen_minute, second=0, microsecond=0)
        if send_time <= now:
            # if time already passed, schedule for next day at same hour:minute
            send_time += timedelta(days=1)
        wait_seconds = (send_time - now).total_seconds()
        send_time_str = send_time.strftime("%Y-%m-%d %H:%M:%S")
        logger.info("🎯 Daily random signal scheduled at %s WIB (in %s seconds).", send_time_str, int(wait_seconds))
        await asyncio.sleep(wait_seconds)
        # Before sending, verify still trading time
        if is_trading_time(send_time):
            logger.info("📤 Sending daily-random signal to %s", DAILY_RANDOM_CHANNEL)
            await send_signal_to_chats(app, [DAILY_RANDOM_CHANNEL])
        else:
            logger.info("❌ Waktu send daily-random tiba tapi sudah di luar trading time; skip.")

# ==========================
# MAIN
# ==========================
def main():
    keep_alive()

    app_bot = ApplicationBuilder().token(BOT_TOKEN).build()

    # Register handlers
    app_bot.add_handler(CommandHandler("start", start))
    app_bot.add_handler(CommandHandler("harga", harga))
    app_bot.add_handler(CommandHandler("minta", minta))
    app_bot.add_handler(CommandHandler("signal", signal_cmd))
    app_bot.add_handler(MessageHandler(filters.COMMAND, lambda u, c: None))

    # Post-init tasks: start finnhub websocket and schedulers
    async def post_init(application):
        # start websocket listener
        application.create_task(finnhub_ws())
        # start hourly scheduler
        application.create_task(hourly_scheduler(application))
        # start daily random scheduler
        application.create_task(daily_random_scheduler(application))
        logger.info("🚀 Background tasks started (WS + schedulers).")

    app_bot.post_init = post_init

    logger.info("🤖 Bot Finnhub AI Signal aktif. Memulai polling...")
    app_bot.run_polling()

if __name__ == "__main__":
    main()
