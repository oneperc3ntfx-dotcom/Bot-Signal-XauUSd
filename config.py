import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
FINNHUB_TOKEN = os.getenv("FINNHUB_TOKEN")
TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY")

CHAT_ID = int(os.getenv("CHAT_ID"))
THREAD_ID = int(os.getenv("THREAD_ID"))

AUTHORIZED_USER_ID = int(os.getenv("AUTHORIZED_USER_ID", "0"))

PAIR = "OANDA:XAU_USD"
