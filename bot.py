import re
import logging
import os
import threading
import time
import requests
import urllib.parse
import json
from datetime import datetime, timedelta
import pytz
from flask import Flask, request
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
ZAPIER_WEBHOOK_URL = os.getenv("ZAPIER_WEBHOOK_URL", "")
CHAT_ID            = os.getenv("CHAT_ID", "")
PUBLIC_URL         = os.getenv("PUBLIC_URL", "")
PORT               = int(os.getenv("PORT", "8080"))

CALL_DELAY     = int(os.getenv("CALL_DELAY", "45"))
MAX_ATTEMPTS   = 3
RETRY_HOURS    = 2
BUSINESS_START = 9
BUSINESS_END   = 20
TIMEZONE       = pytz.timezone("America/Los_Angeles")

twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
flask_app = Flask(__name__)


# =========================
# TELEGRAM SEND (синхронно через HTTP — для использования из Flask)
# =========================
def send_telegram(text, chat_id=None):
    target = chat_id or CHAT_ID
    if not target or not TELEGRAM_BOT_TOKEN:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": target, "text": text},
            timeout=5,
        )
    except Exception as e:
        logger.error(f"Telegram send error: {e}")

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

def get_attempts(phone):
    val = redis_get(f"attempts:{phone}")
    return int(val) if val else 0

def increment_attempts(phone):
    attempts = get_attempts(phone) + 1
    redis_set(f"attempts:{phone}", str(attempts), ex=86400)
    return attempts

def is_business_hours():
    now = datetime.now(TIMEZONE)
    return BUSINESS_START <= now.hour < BUSINESS_END

def next_business_time():
    """Возвращает datetime ближайшего рабочего времени (BUSINESS_START в Сакраменто)."""
    now = datetime.now(TIMEZONE)
    callback = now.replace(hour=BUSINESS_START, minute=0, second=0, microsecond=0)
    if now.hour >= BUSINESS_END:
        callback = callback + timedelta(days=1)
    elif now.hour >= BUSINESS_START:
        callback = now
    return callback

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
# SMS
# =========================
def send_sms(phone):
    try:
        twilio_client.messages.create(
            to=phone,
            from_=TWILIO_FROM,
            body="Hi! This is Lumix Laser Removal. We tried to reach you several times but couldn't connect. Please call us back at (916) 279-3113"
        )
        logger.info(f"SMS отправлено на {phone}")
        send_telegram(f"📱 SMS-фолбэк отправлен на {phone} (3 попытки исчерпаны)")
    except Exception as e:
        logger.error(f"Ошибка SMS на {phone}: {e}")

# =========================
# GOOGLE CALENDAR через Zapier
# =========================
def create_calendar_event(phone, hours_from_now=2, title="Follow-up call"):
    if not ZAPIER_WEBHOOK_URL:
        logger.warning("ZAPIER_WEBHOOK_URL не настроен")
        return
    try:
        event_time = datetime.now(TIMEZONE) + timedelta(hours=hours_from_now)
        if event_time.hour >= BUSINESS_END:
            event_time = (event_time + timedelta(days=1)).replace(
                hour=BUSINESS_START, minute=0, second=0, microsecond=0
            )
        elif event_time.hour < BUSINESS_START:
            event_time = event_time.replace(
                hour=BUSINESS_START, minute=0, second=0, microsecond=0
            )
        data = {
            "phone": phone,
            "title": title,
            "time": event_time.isoformat(),
            "description": phone
        }
        requests.post(ZAPIER_WEBHOOK_URL, json=data, timeout=5)
        logger.info(f"Событие создано для {phone} на {event_time}")
    except Exception as e:
        logger.error(f"Ошибка создания события: {e}")

# =========================
# ОБРАБОТКА НЕУДАЧНОГО ЗВОНКА
# =========================
def handle_failed_call(phone, attempts):
    logger.info(f"Неудачный звонок {phone}, попытка {attempts}/{MAX_ATTEMPTS}")
    if attempts >= MAX_ATTEMPTS:
        logger.info(f"Все попытки исчерпаны для {phone} — отправляю SMS")
        send_sms(phone)
    else:
        create_calendar_event(
            phone,
            hours_from_now=RETRY_HOURS,
            title=f"Retry call {phone} attempt {attempts + 1}"
        )

# =========================
# ЗВОНОК
# =========================
def make_call(phone, force=False, chat_id=None):
    if not force and not is_business_hours():
        logger.info(f"Нерабочее время — создаю задачу перезвонить на {phone}")
        create_calendar_event(phone, hours_from_now=1, title=f"Call {phone}")
        return

    if not force:
        logger.info(f"Жду {CALL_DELAY} сек перед звонком на {phone}")
        time.sleep(CALL_DELAY)

    try:
        call_params = {
            "to": phone,
            "from_": TWILIO_FROM,
            "twiml": (
                f"<Response>"
                f"<Play loop='0'>http://com.twilio.music.classical.s3.amazonaws.com/BusyStrings.mp3</Play>"
                f"<Dial callerId='{phone}' record='record-from-ringing-dual'>{NEXFIELD_NUMBER}</Dial>"
                f"</Response>"
            ),
        }
        if PUBLIC_URL:
            call_params["status_callback"] = f"{PUBLIC_URL.rstrip('/')}/twilio/status"
            call_params["status_callback_event"] = ["completed"]
            call_params["status_callback_method"] = "POST"

        call = twilio_client.calls.create(**call_params)

        redis_set("latest_lead", phone, ex=86400)
        redis_set(f"called:{phone}", "1", ex=86400)
        redis_set(f"call_to:{call.sid}", phone, ex=86400)
        if chat_id:
            redis_set(f"call_chat:{call.sid}", str(chat_id), ex=86400)

        attempts = increment_attempts(phone)
        logger.info(f"Звонок на {phone} — SID: {call.sid} — попытка {attempts}")
        send_telegram(f"📞 Набираю {phone} (попытка {attempts}/{MAX_ATTEMPTS})", chat_id=chat_id)

        time.sleep(60)
        call_status = twilio_client.calls(call.sid).fetch().status
        logger.info(f"Статус звонка {phone}: {call_status}")

        if call_status in ["busy", "no-answer", "failed"]:
            handle_failed_call(phone, attempts)

    except Exception as e:
        logger.error(f"Ошибка звонка на {phone}: {e}")
        handle_failed_call(phone, get_attempts(phone))


# =========================
# FLASK: TWILIO STATUS CALLBACK
# =========================
@flask_app.route("/twilio/status", methods=["POST"])
def twilio_status():
    data = request.form.to_dict()
    call_sid = data.get("CallSid", "")
    status = data.get("CallStatus", "")
    to_number = data.get("To", "") or redis_get(f"call_to:{call_sid}") or "?"
    duration = data.get("CallDuration", "0")
    chat_id = redis_get(f"call_chat:{call_sid}") or CHAT_ID

    msg = None
    if status == "completed":
        try:
            dur_int = int(duration)
            if dur_int < 10:
                msg = f"❓ {to_number} — звонок завершился ({dur_int} сек, скорее всего сбросили)"
            else:
                mins, secs = divmod(dur_int, 60)
                msg = f"🏁 {to_number} — звонок завершён, длительность {mins}:{secs:02d}"
        except Exception:
            msg = f"🏁 {to_number} — звонок завершён"
    elif status == "busy":
        msg = f"📵 {to_number} — занято"
    elif status == "no-answer":
        msg = f"🔕 {to_number} — не отвечает"
    elif status == "failed":
        msg = f"❌ {to_number} — ошибка вызова"
    elif status == "canceled":
        msg = f"🚫 {to_number} — звонок отменён"

    if msg:
        send_telegram(msg, chat_id=chat_id)
    return "", 200


@flask_app.route("/", methods=["GET"])
def health():
    return "Lumix bot is running", 200

# =========================
# КОМАНДЫ
# =========================
async def cmd_call(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /call +19161234567")
        return
    phone = extract_phone(context.args[0])
    if not phone:
        await update.message.reply_text("Неверный формат номера")
        return
    await update.message.reply_text(f"Принудительный звонок на {phone}...")
    chat_id = update.message.chat_id
    threading.Thread(target=make_call, args=(phone, True, chat_id), daemon=True).start()

async def cmd_block(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /block +19161234567")
        return
    phone = extract_phone(context.args[0])
    if not phone:
        await update.message.reply_text("Неверный формат номера")
        return
    redis_set(f"blacklist:{phone}", "1", ex=365*24*3600)
    await update.message.reply_text(f"Номер {phone} заблокирован")

async def cmd_unblock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /unblock +19161234567")
        return
    phone = extract_phone(context.args[0])
    if not phone:
        await update.message.reply_text("Неверный формат номера")
        return
    redis_delete(f"blacklist:{phone}")
    await update.message.reply_text(f"Номер {phone} разблокирован")

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    latest = redis_get("latest_lead")
    now = datetime.now(TIMEZONE)
    working = "Да" if is_business_hours() else "Нет"
    await update.message.reply_text(
        f"Система активна\n"
        f"Задержка: {CALL_DELAY} сек\n"
        f"Рабочее время: {working} ({BUSINESS_START}:00-{BUSINESS_END}:00 PT)\n"
        f"Макс. попыток: {MAX_ATTEMPTS}\n"
        f"Повтор через: {RETRY_HOURS} ч\n"
        f"Время сейчас: {now.strftime('%H:%M PT')}\n"
        f"Последний лид: {latest or 'нет'}\n"
        f"PUBLIC_URL: {'есть' if PUBLIC_URL else 'НЕ настроен'}\n"
        f"CHAT_ID: {'есть' if CHAT_ID else 'НЕ настроен'}"
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Команды:\n\n"
        "/call +19161234567 — принудительный звонок\n"
        "/block +19161234567 — заблокировать номер\n"
        "/unblock +19161234567 — разблокировать номер\n"
        "/status — статус системы\n"
        "/help — эта справка"
    )

# =========================
# ОБРАБОТЧИК TELEGRAM
# =========================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    text = update.message.text
    chat_id = update.message.chat_id
    logger.info(f"Получено: {text[:80]} | chat_id: {chat_id}")

    phone = extract_phone(text)
    if not phone:
        logger.info("Номер не найден")
        return

    # Защита от звонков на свои собственные номера (анти-цикл)
    if phone in [TWILIO_FROM, NEXFIELD_NUMBER]:
        logger.info(f"Игнорирую свой собственный номер: {phone}")
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
    if is_business_hours():
        await update.message.reply_text(f"Звоню на {phone} через {CALL_DELAY} сек...")
    else:
        callback_time = next_business_time()
        await update.message.reply_text(
            f"Нерабочее время — создаю задачу перезвонить на {phone} "
            f"в {callback_time.strftime('%H:%M %d.%m')}"
        )
    threading.Thread(target=make_call, args=(phone, False, chat_id), daemon=True).start()

# =========================
# FLASK В ОТДЕЛЬНОМ ПОТОКЕ
# =========================
def run_flask():
    flask_app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

# =========================
# ЗАПУСК
# =========================
if __name__ == "__main__":
    logger.info(f"Запуск Flask на порту {PORT} для Twilio callbacks...")
    threading.Thread(target=run_flask, daemon=True).start()

    logger.info("Бот запущен")
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("call", cmd_call))
    app.add_handler(CommandHandler("block", cmd_block))
    app.add_handler(CommandHandler("unblock", cmd_unblock))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(MessageHandler(filters.ALL, handle_message))
    app.run_polling(allowed_updates=Update.ALL_TYPES)
