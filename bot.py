import re
import logging
import os
import threading
import time
import requests
import urllib.parse
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes
from twilio.rest import Client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# =========================
# НАСТРОЙКИ
# =========================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN  = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_FROM        = os.getenv("TWILIO_FROM_NUMBER")
NEXFIELD_NUMBER    = os.getenv("NEXFIELD_NUMBER")
UPSTASH_URL        = os.getenv("UPSTASH_URL")
UPSTASH_TOKEN      = os.getenv("UPSTASH_TOKEN")

CALL_DELAY = int(os.getenv("CALL_DELAY", "45"))

twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# =========================
# UPSTASH REDIS
# =========================
def redis_set(key, value, ex=86400):
    headers = {"Authorization": f"Bearer {UPSTASH_TOKEN}"}
    try:
        requests.post(
            f"{UPSTASH_URL}/set/{urllib.parse.quote(str(key))}/{urllib.parse.quote(str(value))}/ex/{ex}",
            headers=headers, timeout=5
        )
    except Exception as e:
        logger.error(f"Redis set error: {e}")

def redis_get(key):
    headers = {"Authorization": f"Bearer {UPSTASH_TOKEN}"}
    try:
        r = requests.get(
            f"{UPSTASH_URL}/get/{urllib.parse.quote(str(key))}",
            headers=headers, timeout=5
        )
        return r.json().get("result")
    except Exception as e:
        logger.error(f"Redis get error: {e}")
        return None

def redis_delete(key):
    headers = {"Authorization": f"Bearer {UPSTASH_TOKEN}"}
    try:
        requests.post(
            f"{UPSTASH_URL}/del/{urllib.parse.quote(str(key))}",
            headers=headers, timeout=5
        )
    except Exception as e:
        logger.error(f"Redis delete error: {e}")

# =========================
# ПРОВЕРКИ
# =========================
def is_blacklisted(phone):
    return redis_get(f"blacklist:{phone}") is not None

def is_duplicate(phone):
    return redis_get(f"called:{phone}") is not None

# =========================
# ОПРЕДЕЛЕНИЕ НОМЕРА
# =========================
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

# =========================
# ЗВОНОК
# =========================
def make_call(phone):
    logger.info(f"Жду {CALL_DELAY} сек перед звонком на {phone}")
    time.sleep(CALL_DELAY)
    try:
        call = twilio_client.calls.create(
            to=phone,
            from_=TWILIO_FROM,
            twiml=f"<Response><Say voice='alice'>Please hold while we connect you.</Say><Dial>{NEXFIELD_NUMBER}</Dial></Response>"
        )
        redis_set(f"latest_lead", phone, ex=86400)
        redis_set(f"called:{phone}", "1", ex=86400)
        logger.info(f"Звонок на {phone} — SID: {call.sid}")
    except Exception as e:
        logger.error(f"Ошибка звонка на {phone}: {e}")

# =========================
# КОМАНДЫ
# =========================
async def cmd_block(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /block +19161234567")
        return
    phone = extract_phone(context.args[0])
    if not phone:
        await update.message.reply_text("Неверный формат номера")
        return
    redis_set(f"blacklist:{phone}", "1", ex=365*24*3600)
    await update.message.reply_text(f"Номер {phone} добавлен в черный список")
    logger.info(f"Blacklist добавлен: {phone}")

async def cmd_unblock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /unblock +19161234567")
        return
    phone = extract_phone(context.args[0])
    if not phone:
        await update.message.reply_text("Неверный формат номера")
        return
    redis_delete(f"blacklist:{phone}")
    await update.message.reply_text(f"Номер {phone} убран из черного списка")

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    latest = redis_get("latest_lead")
    await update.message.reply_text(
        f"Система активна\n"
        f"Задержка: {CALL_DELAY} сек\n"
        f"Последний лид: {latest or 'нет'}"
    )

# =========================
# ОБРАБОТЧИК TELEGRAM
# =========================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    text = update.message.text
    logger.info(f"Получено: {text[:80]}")

    phone = extract_phone(text)
    if not phone:
        logger.info("Номер не найден")
        return

    if is_blacklisted(phone):
        logger.info(f"Номер {phone} в черном списке")
        await update.message.reply_text(f"Номер {phone} в черном списке — звонок отменён")
        return

    if is_duplicate(phone):
        logger.info(f"Дубль: {phone}")
        await update.message.reply_text(f"Дубль — уже звонили на {phone} сегодня")
        return

    logger.info(f"Найден номер: {phone}")
    await update.message.reply_text(f"Звоню на {phone} через {CALL_DELAY} сек...")
    threading.Thread(target=make_call, args=(phone,), daemon=True).start()

# =========================
# ЗАПУСК
# =========================
if __name__ == "__main__":
    logger.info("Бот запущен")
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("block", cmd_block))
    app.add_handler(CommandHandler("unblock", cmd_unblock))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(MessageHandler(filters.ALL, handle_message))
    app.run_polling(allowed_updates=Update.ALL_TYPES)
