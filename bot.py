import re
import asyncio
import logging
import os
import threading
import time
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from twilio.rest import Client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN  = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_FROM        = os.getenv("TWILIO_FROM_NUMBER")
NEXFIELD_NUMBER    = os.getenv("NEXFIELD_NUMBER")
CALL_DELAY         = int(os.getenv("CALL_DELAY", "120"))

twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

def extract_phone(text: str):
    match = re.search(r'(\+1[\s\-]?\d{10}|\b\d{10}\b|\+\d{11,12})', text)
    if not match:
        return None
    digits = re.sub(r'[^\d]', '', match.group())
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return None

def make_call(phone):
    logger.info(f"Жду {CALL_DELAY} сек перед звонком на {phone}")
    time.sleep(CALL_DELAY)
    try:
        call = twilio_client.calls.create(
            to=phone,
            from_=TWILIO_FROM,
            twiml=f"<Response><Say voice='alice'>Please hold while we connect you.</Say><Dial>{NEXFIELD_NUMBER}</Dial></Response>"
        )
        logger.info(f"Звонок на {phone} — SID: {call.sid}")
    except Exception as e:
        logger.error(f"Ошибка звонка на {phone}: {e}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    text = update.message.text
    logger.info(f"Получено сообщение: {text[:80]}")

    phone = extract_phone(text)
    if not phone:
        logger.info("Номер не найден")
        return

    logger.info(f"Найден номер: {phone} — ставлю в очередь")
    await update.message.reply_text(f"📞 Звоню на {phone} через {CALL_DELAY // 60} мин...")

    threading.Thread(target=make_call, args=(phone,), daemon=True).start()

if __name__ == "__main__":
    logger.info("Бот запущен")
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.ALL, handle_message))
    app.run_polling()
