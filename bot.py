import os
import re
import uuid
import asyncio
import logging
import tempfile
import time
import psycopg
from psycopg_pool import ConnectionPool
from datetime import datetime
from google import genai
from telegram import (Update, InlineKeyboardButton, InlineKeyboardMarkup,
                      ReplyKeyboardMarkup, KeyboardButton, BotCommand)
from telegram.ext import (Application, CommandHandler, MessageHandler, filters,
                          ContextTypes, CallbackQueryHandler)
from pyrogram import Client as PyroClient

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
# my.telegram.org dan olinadi (katta video yuklab olish uchun)
API_ID = int(os.getenv("API_ID", "0") or "0")
API_HASH = os.getenv("API_HASH", "")
# Maksimal qabul qilinadigan video hajmi (2GB). Kerak bo'lsa pasaytiring.
MAX_VIDEO_BYTES = 2 * 1024 * 1024 * 1024
# Bir vaqtda nechta video tahlil qilinishi mumkin (qolganlar navbatda kutadi).
# Pullik Gemini + kattaroq serverga o'tganda Railway'dan MAX_CONCURRENT ni oshiring.
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT", "3") or "3")
# Navbat mexanizmi: bir vaqtda faqat MAX_CONCURRENT ta tahlil ishlaydi
_video_semaphore = asyncio.Semaphore(MAX_CONCURRENT)
ADMIN_ID = 7589459697
CARD_NUMBER = "6262 7300 6521 3151"
CARD_NAME = "Boqijonov Nurislom"

# Paketlar: callback -> (tahlillar soni, narx so'm)
PACKAGES = {
    'pkg_1': (1, 3990),
    'pkg_5': (5, 16000),
    'pkg_10': (10, 26000),
}

client = genai.Client(api_key=GEMINI_API_KEY)

# Xavfsizlik sozlamalari: oddiy reklama/Instagram videolari xato "bloklanib"
# bo'sh javob qaytmasligi uchun. Agar SDK qabul qilmasa, e'tiborsiz qoldiriladi.
try:
    from google.genai import types as genai_types
    SAFETY_SETTINGS = [
        genai_types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="BLOCK_ONLY_HIGH"),
        genai_types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="BLOCK_ONLY_HIGH"),
        genai_types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="BLOCK_ONLY_HIGH"),
        genai_types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="BLOCK_ONLY_HIGH"),
    ]
except Exception:
    genai_types = None
    SAFETY_SETTINGS = None

# Pyrogram "yuklab oluvchi" — faqat katta videolarni yuklab olish uchun.
# API_ID/API_HASH kiritilmagan bo'lsa, pyro=None bo'ladi va bot avvalgidek
# (faqat 20MB gacha, Bot API orqali) ishlayveradi — ya'ni hech narsa buzilmaydi.
pyro = None
if API_ID and API_HASH and TELEGRAM_TOKEN:
    pyro = PyroClient(
        "instadoktor_downloader",
        api_id=API_ID,
        api_hash=API_HASH,
        bot_token=TELEGRAM_TOKEN,
        no_updates=True,   # PTB bilan to'qnashmasligi uchun update olmaydi
        in_memory=True,    # session faylsiz (Railway diski vaqtinchalik)
        workdir="/tmp",
    )

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "")
# Railway ba'zan postgres:// beradi; psycopg postgresql:// kutadi - to'g'rilaymiz.
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# Ulanishlar puli: ko'p odam bir vaqtda ishlasa ham tez va xavfsiz.
_pool = None
if DATABASE_URL:
    try:
        _pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=10,
                               kwargs={"autocommit": True}, open=True)
        logging.info("PostgreSQL pool tayyor - balans DOIMIY saqlanadi")
    except Exception as e:
        logging.error(f"PostgreSQL pool xato: {e}")
        _pool = None
else:
    logging.warning("DIQQAT: DATABASE_URL yo'q! Postgres ulanmagan.")


def _db_execute(query, params=(), fetch=None):
    """Bitta so'rovni xavfsiz bajaradi. fetch: None / 'one' / 'all'."""
    if _pool is None:
        return None
    try:
        with _pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query, params)
                if fetch == 'one':
                    return cur.fetchone()
                if fetch == 'all':
                    return cur.fetchall()
                return None
    except Exception as e:
        logger.error(f"DB xato: {e}")
        return None


def init_db():
    if _pool is None:
        logger.error("Postgres ulanmagan - init_db o'tkazib yuborildi")
        return
    try:
        with _pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY, username TEXT, first_name TEXT,
                    joined TEXT, balance INTEGER DEFAULT 0)""")
                cur.execute("""CREATE TABLE IF NOT EXISTS analyses (
                    id SERIAL PRIMARY KEY, user_id BIGINT, created TEXT)""")
                cur.execute("""CREATE TABLE IF NOT EXISTS payments (
                    id SERIAL PRIMARY KEY, user_id BIGINT,
                    package TEXT, amount INTEGER, status TEXT, created TEXT)""")
                # analyses jadvaliga yangi ustunlar (bor bo'lsa - tegmaydi)
                for col, typ in [("username", "TEXT"), ("kind", "TEXT"),
                                 ("file_id", "TEXT"), ("foiz", "INTEGER DEFAULT 0"),
                                 ("qisqa", "TEXT"), ("toliq", "TEXT")]:
                    cur.execute(f"ALTER TABLE analyses ADD COLUMN IF NOT EXISTS {col} {typ}")
        logger.info("PostgreSQL baza tayyor (jadvallar mavjud)")
    except Exception as e:
        logger.error(f"init_db xato: {e}")


def save_user(user_id, username, first_name):
    _db_execute(
        "INSERT INTO users (user_id, username, first_name, joined, balance) "
        "VALUES (%s, %s, %s, %s, 0) ON CONFLICT (user_id) DO NOTHING",
        (user_id, username or "", first_name or "", datetime.now().strftime("%Y-%m-%d %H:%M"))
    )


def get_balance(user_id):
    if user_id == ADMIN_ID:
        return 999999
    row = _db_execute("SELECT balance FROM users WHERE user_id = %s", (user_id,), fetch='one')
    return row[0] if row else 0


def add_balance(user_id, amount):
    # Foydalanuvchi bazada bo'lmasa ham ishlashi uchun avval qatorini yaratamiz
    _db_execute(
        "INSERT INTO users (user_id, username, first_name, joined, balance) "
        "VALUES (%s, '', '', %s, 0) ON CONFLICT (user_id) DO NOTHING",
        (user_id, datetime.now().strftime("%Y-%m-%d %H:%M"))
    )
    _db_execute("UPDATE users SET balance = balance + %s WHERE user_id = %s", (amount, user_id))


def use_balance(user_id):
    if user_id == ADMIN_ID:
        return
    _db_execute("UPDATE users SET balance = balance - 1 WHERE user_id = %s AND balance > 0", (user_id,))


def save_analysis(user_id, username="", kind="video", file_id=None, foiz=0, qisqa=None, toliq=None):
    """Tahlilni saqlaydi va yangi yozuv ID sini qaytaradi (To'liq tugmasi uchun)."""
    row = _db_execute(
        "INSERT INTO analyses (user_id, username, kind, file_id, foiz, qisqa, toliq, created) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (user_id, username or "", kind, file_id, foiz, qisqa, toliq,
         datetime.now().strftime("%Y-%m-%d %H:%M")),
        fetch='one'
    )
    return row[0] if row else None


def get_full_analysis(analysis_id):
    """To'liq tahlil matnini qaytaradi (tugma bosilganda)."""
    row = _db_execute("SELECT toliq FROM analyses WHERE id = %s", (analysis_id,), fetch='one')
    return row[0] if row and row[0] else None


def get_top_today(limit=10):
    """Bugungi eng yuqori rekka foizli videolar (admin uchun)."""
    today = datetime.now().strftime("%Y-%m-%d")
    rows = _db_execute(
        "SELECT id, user_id, username, foiz, file_id, created FROM analyses "
        "WHERE kind = 'video' AND file_id IS NOT NULL AND created LIKE %s "
        "ORDER BY foiz DESC, id DESC LIMIT %s",
        (today + "%", limit), fetch='all'
    )
    return rows or []


def create_payment(user_id, package, amount):
    row = _db_execute(
        "INSERT INTO payments (user_id, package, amount, status, created) "
        "VALUES (%s, %s, %s, 'pending', %s) RETURNING id",
        (user_id, package, amount, datetime.now().strftime("%Y-%m-%d %H:%M")),
        fetch='one'
    )
    return row[0] if row else None


def get_stats():
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        total_users = (_db_execute("SELECT COUNT(*) FROM users", fetch='one') or [0])[0]
        total_analyses = (_db_execute("SELECT COUNT(*) FROM analyses", fetch='one') or [0])[0]
        today_analyses = (_db_execute("SELECT COUNT(*) FROM analyses WHERE created LIKE %s",
                                      (today + "%",), fetch='one') or [0])[0]
        revenue = (_db_execute("SELECT COALESCE(SUM(amount),0) FROM payments WHERE status = 'approved'",
                               fetch='one') or [0])[0]
        return total_users, total_analyses, today_analyses, revenue
    except Exception as e:
        logger.error(f"get_stats xato: {e}")
        return 0, 0, 0, 0


PROMPT_UZ = """Sen tajribali, xolis Instagram kontent tahlilchisisan. Blogger videosini HALOL va OBJEKTIV bahola. Faqat HAQIQATNI ayt — yaxshi bo'lsa yaxshi, kuchsiz bo'lsa kuchsiz de.

Javobingni ANIQ shu formatda, shu teglar bilan ber (teglarni o'zgartirma, teglardan tashqarida hech narsa yozma):

[FOIZ]videoning rekkaga (rekomendatsiyaga) chiqish ehtimoli, faqat 0-100 oralig'idagi bitta son[/FOIZ]

[QISQA]
QISQA va o'qishga oson tahlil (jami 8-12 qator). Har bo'lim 1-2 qisqa qator, ko'p emoji bilan:
🎣 Hook — ⭐ _/10 — qisqa sabab
🎬 Vizual/Montaj — ⭐ _/10 — qisqa sabab
🗣️ Audio/Nutq — ⭐ _/10 — qisqa sabab
📝 Kontent — ⭐ _/10 — qisqa sabab
📈 Rekka chiqish ehtimoli: _% — qisqa sabab
Oxirida 1 qatorli umumiy xulosa.
[/QISQA]

[TOLIQ]
TO'LIQ, chuqur tahlil (har bo'lim bir necha jumla, ko'p emoji bilan):
🎣 HOOK (0-3 sekund) — e'tiborni tortadimi? ⭐ Ball: _/10 — batafsil izoh
🎬 VIZUAL VA MONTAJ — 🎥 yoritish, kamera, montaj. ⭐ Ball: _/10 — batafsil izoh
🗣️ AUDIO VA NUTQ — 🎙️ nima gapirildi, ovoz toni. ⭐ Ball: _/10 — batafsil izoh
📝 KONTENT VA QIYMAT — 💬 xabar, CTA. ⭐ Ball: _/10 — batafsil izoh
📊 REKKA CHIQISH EHTIMOLI — 🎯 _% va batafsil sabablar.
✅ KUCHLI TOMONLARI — faqat haqiqiy kuchli joylar.
❌ KAMCHILIKLAR — barcha jiddiy kamchiliklar, ochiq ayt.
💡 TAVSIYALAR — 🚀 5 ta aniq, amaliy qadam.
[/TOLIQ]

Baho videoning haqiqiy sifatiga MOS bo'lsin. Foiz REAL bo'lsin. Sen do'st emas, ekspertsan — halol baho bloggerni o'stiradi."""

PROMPT_RU = """Ты опытный, объективный аналитик Instagram-контента. Оцени видео блогера ЧЕСТНО. Говори только ПРАВДУ.

Ответ дай СТРОГО в этом формате с этими тегами (не меняй теги, вне тегов ничего не пиши):

[FOIZ]вероятность попадания видео в рекомендации, только одно число 0-100[/FOIZ]

[QISQA]
КОРОТКИЙ, лёгкий для чтения анализ (всего 8-12 строк). Каждый раздел 1-2 строки, с эмодзи:
🎣 Хук — ⭐ _/10 — кратко
🎬 Визуал/Монтаж — ⭐ _/10 — кратко
🗣️ Аудио/Речь — ⭐ _/10 — кратко
📝 Контент — ⭐ _/10 — кратко
📈 Вероятность в рекомендации: _% — кратко
В конце 1 строка общего вывода.
[/QISQA]

[TOLIQ]
ПОЛНЫЙ, глубокий анализ (каждый раздел в несколько предложений, с эмодзи):
🎣 ХУК (0-3 сек) — цепляет? ⭐ Балл: _/10 — подробно
🎬 ВИЗУАЛ И МОНТАЖ — 🎥 свет, камера, монтаж. ⭐ Балл: _/10 — подробно
🗣️ АУДИО И РЕЧЬ — 🎙️ что сказано, тон. ⭐ Балл: _/10 — подробно
📝 КОНТЕНТ И ЦЕННОСТЬ — 💬 посыл, призыв. ⭐ Балл: _/10 — подробно
📊 ВЕРОЯТНОСТЬ В РЕКОМЕНДАЦИИ — 🎯 _% и причины.
✅ СИЛЬНЫЕ СТОРОНЫ — реальные плюсы.
❌ НЕДОСТАТКИ — все серьёзные минусы.
💡 РЕКОМЕНДАЦИИ — 🚀 5 конкретных шагов.
[/TOLIQ]

Оценка должна соответствовать реальному качеству. Процент реальный. Ты не друг, ты эксперт."""


PROMPT_PROFILE_UZ = """Sen Instagram bo'yicha tajribali, xolis ekspertsan. Senga foydalanuvchining Instagram profili va/yoki statistikasi (Insights) skrinshot(lar)i berildi. Ularni diqqat bilan ko'rib chiqib, professional va halol tahlil ber:

📊 UMUMIY TAASSUROT — profil haqida qisqacha.
👤 BIO VA PROFIL — ism, bio, profil rasmi, havola: nimasi yaxshi, nimasi kam.
🎯 KONTENT — postlar mavzusi, sifati, izchilligi.
📈 ENGAGEMENT — layk, izoh, ko'rishlar bo'yicha baho (agar ko'rinsa).
✅ KUCHLI TOMONLAR — faqat real plyuslar.
❌ KAMCHILIKLAR — barcha jiddiy minuslar, ochiq va halol.
💡 TAVSIYALAR — 🚀 5 ta aniq, amaliy qadam.

Faqat skrinshotda haqiqatan ko'ringan narsaga asoslan, ko'rinmaganini o'ylab topma. Do'st emas, ekspertsan — halol baho blogerni o'stiradi."""


PROMPT_PROFILE_RU = """Ты опытный, объективный эксперт по Instagram. Тебе дали скриншот(ы) профиля и/или статистики (Insights) пользователя. Внимательно изучи их и дай профессиональный, честный анализ:

📊 ОБЩЕЕ ВПЕЧАТЛЕНИЕ — кратко о профиле.
👤 БИО И ПРОФИЛЬ — имя, био, аватар, ссылка: что хорошо, чего не хватает.
🎯 КОНТЕНТ — тема, качество, регулярность постов.
📈 ВОВЛЕЧЁННОСТЬ — оценка по лайкам, комментариям, просмотрам (если видно).
✅ СИЛЬНЫЕ СТОРОНЫ — только реальные плюсы.
❌ НЕДОСТАТКИ — все серьёзные минусы, честно.
💡 РЕКОМЕНДАЦИИ — 🚀 5 конкретных шагов.

Опирайся только на то, что реально видно на скриншоте, не выдумывай. Ты не друг, ты эксперт — честная оценка помогает расти."""

TEXTS = {
    'uz': {
        'welcome': (
            "🩺 INSTADOKTOR — Instagram tahlil boti\n\n"
            "Men videolaringizni halol va xolis tahlil qilaman — "
            "maqtov emas, haqiqat aytaman. 💯\n\n"
            "📤 Videongizni yuboring (2GB gacha):"
        ),
        'menu_video': "🎬 Video tahlil",
        'menu_balance': "💰 Balansim",
        'menu_lang': "🌐 Til",
        'menu_help': "ℹ️ Yordam",
        'menu_profile': "📊 Profil tahlili",
        'profile_instr': ("📊 PROFIL TAHLILI\n\n"
                          "Instagram profilingizni chuqur tahlil qilaman: kuchli va kuchsiz tomonlari, kontent va aniq tavsiyalar.\n\n"
                          "📸 SKRINSHOT YO'LI (eng tez va aniq):\n"
                          "1. Instagram'da profilingizni oching\n"
                          "2. Profil sahifangiz skrinshotini oling (bio, postlar ko'rinsin)\n"
                          "3. Bo'lsa, statistika (Insights) skrinshotlarini ham oling\n"
                          "4. Skrinshot(lar)ni shu yerga yuboring\n"
                          "5. Tugagach '✅ Tahlil qilish' tugmasini bosing\n\n"
                          "💡 Qancha ko'p skrinshot — shuncha aniq natija.\n\n"
                          "Boshlash uchun skrinshot yuboring 👇"),
        'profile_got': "✅ Rasm qabul qilindi ({n} ta).\n\nYana yuboring yoki tahlilni boshlang 👇",
        'profile_btn': "✅ Tahlil qilish",
        'profile_none': "❌ Avval profil skrinshotini yuboring.",
        'profile_analyzing': "🧠 Profil tahlil qilinmoqda... ⚡",
        'full_btn': "📖 To'liq tahlilni ko'rish",
        'full_gone': "❌ To'liq tahlil topilmadi (eski bo'lishi mumkin).",
        'profil_info': "📊 Profil tahlil tez orada ishga tushadi! 🔜",
        'help_text': ("ℹ️ INSTADOKTOR — Yordam\n\n"
                      "🎬 Video tahlil — videongizni yuboring, men uni to'liq tahlil qilaman: "
                      "hook, vizual, audio, montaj va rekka chiqish ehtimoli.\n\n"
                      "💰 Balansim — qancha tahlil qolganini ko'rish.\n\n"
                      "🌐 Til — tilni o'zgartirish.\n\n"
                      "📏 Video 2GB dan kichik bo'lsin."),
        'lang_changed': "✅ Til o'zgartirildi!",
        'received': "⏳ Video qabul qilindi! Tahlil boshlanmoqda... ⚡",
        'queued': "⏳ Hozir navbat bandroq, videongiz navbatga qo'yildi. Bir oz kuting... 🕐",
        'too_big': "❌ Video juda katta (2GB dan oshmasligi kerak). 📏\n\nIltimos, qisqaroq yuboring.",
        'wrong_format': "❌ Video formatini tanimadim. MP4 yoki MOV yuboring. 📹",
        'uploading': "📤 Video yuklanmoqda...",
        'analyzing': "🧠 AI tahlil qilinmoqda (vizual + audio)... ⚡",
        'ready': "✅ Tahlil tayyor!",
        'error': "😔 Kechirasiz, tahlil qilib bo'lmadi. Iltimos, videoni qayta yuboring. 🔄",
        'send_video': "🎬 Tahlil uchun videoni yuboring! 📤",
        'no_balance': ("💳 Sizda tahlil qolmagan.\n\nDavom etish uchun paket tanlang:"),
        'balance_info': "💰 Sizda {n} ta tahlil bor.",
        'choose_pkg': "💳 Paketni tanlang:",
        'pay_instr': ("💳 To'lov uchun:\n\n"
                      "💰 Summa: {amount:,} so'm\n"
                      "💳 Karta: {card}\n"
                      "👤 Ega: {name}\n\n"
                      "✅ To'lagach, CHEK SKRINSHOTINI shu yerga yuboring.\n"
                      "Admin tekshirib, tahlillarni hisobingizga qo'shadi. ⏳"),
        'receipt_sent': "✅ Chekingiz adminga yuborildi. Tez orada tasdiqlanadi! ⏳",
        'approved': "🎉 To'lovingiz tasdiqlandi! Hisobingizga {n} ta tahlil qo'shildi.\n💰 Jami: {total} ta",
        'pkg_1': "1 tahlil — 3,990 so'm",
        'pkg_5': "5 tahlil — 16,000 so'm",
        'pkg_10': "10 tahlil — 26,000 so'm",
    },
    'ru': {
        'welcome': (
            "🩺 INSTADOKTOR — бот анализа Instagram\n\n"
            "Я честно и объективно анализирую ваши видео — "
            "не хвалю, а говорю правду. 💯\n\n"
            "📤 Отправьте ваше видео (до 2ГБ):"
        ),
        'menu_video': "🎬 Анализ видео",
        'menu_balance': "💰 Мой баланс",
        'menu_lang': "🌐 Язык",
        'menu_help': "ℹ️ Помощь",
        'menu_profile': "📊 Анализ профиля",
        'profile_instr': ("📊 АНАЛИЗ ПРОФИЛЯ\n\n"
                          "Глубоко проанализирую ваш Instagram-профиль: сильные и слабые стороны, контент и конкретные рекомендации.\n\n"
                          "📸 ЧЕРЕЗ СКРИНШОТ (быстро и точно):\n"
                          "1. Откройте свой профиль в Instagram\n"
                          "2. Сделайте скриншот страницы профиля (видны био, посты)\n"
                          "3. Если есть — сделайте скриншоты статистики (Insights)\n"
                          "4. Отправьте скриншот(ы) сюда\n"
                          "5. После — нажмите '✅ Анализировать'\n\n"
                          "💡 Чем больше скриншотов — тем точнее результат.\n\n"
                          "Чтобы начать, отправьте скриншот 👇"),
        'profile_got': "✅ Изображение получено ({n} шт.).\n\nОтправьте ещё или начните анализ 👇",
        'profile_btn': "✅ Анализировать",
        'profile_none': "❌ Сначала отправьте скриншот профиля.",
        'profile_analyzing': "🧠 Анализирую профиль... ⚡",
        'full_btn': "📖 Посмотреть полный анализ",
        'full_gone': "❌ Полный анализ не найден (возможно, старый).",
        'profil_info': "📊 Анализ профиля скоро заработает! 🔜",
        'help_text': ("ℹ️ INSTADOKTOR — Помощь\n\n"
                      "🎬 Анализ видео — отправьте видео, я полностью его проанализирую.\n\n"
                      "💰 Мой баланс — сколько анализов осталось.\n\n"
                      "🌐 Язык — сменить язык.\n\n"
                      "📏 Видео должно быть меньше 2ГБ."),
        'lang_changed': "✅ Язык изменён!",
        'received': "⏳ Видео получено! Начинаю анализ... ⚡",
        'queued': "⏳ Сейчас очередь занята, ваше видео в очереди. Немного подождите... 🕐",
        'too_big': "❌ Видео слишком большое (не более 2ГБ). 📏\n\nПожалуйста, отправьте покороче.",
        'wrong_format': "❌ Не распознал формат. Отправьте MP4 или MOV. 📹",
        'uploading': "📤 Видео загружается...",
        'analyzing': "🧠 ИИ анализирует (визуал + аудио)... ⚡",
        'ready': "✅ Анализ готов!",
        'error': "😔 Извините, не удалось проанализировать. Отправьте видео ещё раз. 🔄",
        'send_video': "🎬 Отправьте видео для анализа! 📤",
        'no_balance': ("💳 У вас не осталось анализов.\n\nВыберите пакет для продолжения:"),
        'balance_info': "💰 У вас {n} анализов.",
        'choose_pkg': "💳 Выберите пакет:",
        'pay_instr': ("💳 Для оплаты:\n\n"
                      "💰 Сумма: {amount:,} сум\n"
                      "💳 Карта: {card}\n"
                      "👤 Владелец: {name}\n\n"
                      "✅ После оплаты отправьте СКРИНШОТ ЧЕКА сюда.\n"
                      "Админ проверит и добавит анализы на ваш счёт. ⏳"),
        'receipt_sent': "✅ Ваш чек отправлен админу. Скоро подтвердим! ⏳",
        'approved': "🎉 Оплата подтверждена! На счёт добавлено {n} анализов.\n💰 Всего: {total}",
        'pkg_1': "1 анализ — 3 990 сум",
        'pkg_5': "5 анализов — 16 000 сум",
        'pkg_10': "10 анализов — 26 000 сум",
    }
}


def get_lang(context):
    return context.user_data.get('lang', 'uz')


def t(context, key):
    return TEXTS[get_lang(context)][key]


def main_keyboard(context):
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(t(context, 'menu_video')), KeyboardButton(t(context, 'menu_profile'))],
            [KeyboardButton(t(context, 'menu_balance')), KeyboardButton(t(context, 'menu_lang'))],
            [KeyboardButton(t(context, 'menu_help'))],
        ],
        resize_keyboard=True
    )


def lang_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🇺🇿 O'zbekcha", callback_data='lang_uz')],
        [InlineKeyboardButton("🇷🇺 Русский", callback_data='lang_ru')],
    ])


def package_keyboard(context):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(t(context, 'pkg_1'), callback_data='pkg_1')],
        [InlineKeyboardButton(t(context, 'pkg_5'), callback_data='pkg_5')],
        [InlineKeyboardButton(t(context, 'pkg_10'), callback_data='pkg_10')],
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    save_user(user.id, user.username, user.first_name)
    await update.message.reply_text("🌐 Tilni tanlang / Выберите язык:", reply_markup=lang_keyboard())


async def show_menu(message, context):
    await message.reply_text(t(context, 'welcome'), reply_markup=main_keyboard(context))


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == 'lang_uz':
        context.user_data['lang'] = 'uz'
        await query.message.reply_text(t(context, 'lang_changed'), reply_markup=main_keyboard(context))
        await show_menu(query.message, context)
    elif data == 'lang_ru':
        context.user_data['lang'] = 'ru'
        await query.message.reply_text(t(context, 'lang_changed'), reply_markup=main_keyboard(context))
        await show_menu(query.message, context)
    elif data in PACKAGES:
        count, amount = PACKAGES[data]
        create_payment(query.from_user.id, data, amount)
        context.user_data['pending_pkg'] = data
        context.user_data['mode'] = None  # profil rejimini o'chiramiz (chek bilan aralashmasin)
        await query.message.reply_text(
            t(context, 'pay_instr').format(amount=amount, card=CARD_NUMBER, name=CARD_NAME)
        )
    elif data.startswith('full_'):
        try:
            aid = int(data.split('_', 1)[1])
        except Exception:
            return
        toliq = get_full_analysis(aid)
        if not toliq:
            await query.message.reply_text(t(context, 'full_gone'))
            return
        if len(toliq) <= 4000:
            await query.message.reply_text(toliq)
        else:
            for i in range(0, len(toliq), 4000):
                await query.message.reply_text(toliq[i:i+4000])
    elif data == 'profile_analyze':
        user_id = query.from_user.id
        imgs = context.user_data.get('profile_imgs', [])
        if not imgs:
            await query.message.reply_text(t(context, 'profile_none'))
            return
        if get_balance(user_id) <= 0:
            await query.message.reply_text(t(context, 'no_balance'), reply_markup=package_keyboard(context))
            return
        wait_msg = await query.message.reply_text(t(context, 'profile_analyzing'))
        tmp_paths = []
        try:
            async with _video_semaphore:
                for fid in imgs:
                    p = os.path.join("/tmp", f"{uuid.uuid4().hex}.jpg")
                    f = await context.bot.get_file(fid)
                    await f.download_to_drive(p)
                    tmp_paths.append(p)

                prompt = PROMPT_PROFILE_RU if get_lang(context) == 'ru' else PROMPT_PROFILE_UZ
                tahlil = await asyncio.to_thread(_gemini_process_images, tmp_paths, prompt)

                if not tahlil or not tahlil.strip():
                    raise Exception("Profil tahlili bo'sh keldi")

                await wait_msg.edit_text(t(context, 'ready'))
                use_balance(user_id)       # balans faqat muvaffaqiyatda yechiladi
                _uname = query.from_user.username or query.from_user.first_name or ""
                save_analysis(user_id, username=_uname, kind="profile", toliq=tahlil)
                context.user_data['mode'] = None
                context.user_data['profile_imgs'] = []

                if len(tahlil) <= 4000:
                    await query.message.reply_text(tahlil)
                else:
                    for i in range(0, len(tahlil), 4000):
                        await query.message.reply_text(tahlil[i:i+4000])
        except Exception as e:
            logger.error(f"Profil tahlil xato: {e}")
            await wait_msg.edit_text(t(context, 'error'))
        finally:
            for p in tmp_paths:
                try:
                    if os.path.exists(p):
                        os.unlink(p)
                except Exception:
                    pass
    elif data.startswith('approve_'):
        # Admin tasdiqlash: approve_{user_id}_{package}
        parts = data.split('_')
        target_user = int(parts[1])
        package = parts[2] + '_' + parts[3] if len(parts) > 3 else parts[2]
        count, amount = PACKAGES.get(package, (0, 0))
        add_balance(target_user, count)
        new_balance = get_balance(target_user)
        try:
            await context.bot.send_message(
                target_user,
                f"🎉 To'lovingiz tasdiqlandi! Hisobingizga {count} ta tahlil qo'shildi.\n💰 Jami: {new_balance} ta"
            )
        except Exception as e:
            logger.error(f"Foydalanuvchiga xabar yuborilmadi: {e}")
        await query.message.reply_text(f"✅ Tasdiqlandi! {target_user} ga {count} ta tahlil qo'shildi.")


def _upload_and_wait(tmp_path, max_retries=3, max_wait=300):
    """Videoni Gemini'ga yuklaydi va ACTIVE bo'lguncha kutadi (qayta urinish bilan)."""
    last_error = None
    for attempt in range(max_retries):
        try:
            uploaded = client.files.upload(file=tmp_path)
            waited = 0
            while uploaded.state.name == "PROCESSING" and waited < max_wait:
                time.sleep(3)
                waited += 3
                uploaded = client.files.get(name=uploaded.name)
            if uploaded.state.name == "FAILED":
                raise Exception("Gemini videoni qayta ishlay olmadi (FAILED)")
            if uploaded.state.name != "ACTIVE":
                raise Exception(f"Video tayyor bo'lmadi (holat: {uploaded.state.name})")
            return uploaded
        except Exception as e:
            last_error = e
            logger.warning(f"Upload urinish {attempt+1}/{max_retries}: {e}")
            time.sleep((attempt + 1) * 4)
    raise last_error


def _extract_text(response):
    """Gemini javobidan matnni xavfsiz ajratadi (bo'sh/bloklangan holatlarni hisobga olib)."""
    try:
        if getattr(response, "text", None):
            return response.text.strip()
    except Exception:
        pass
    try:
        parts_text = []
        for cand in (getattr(response, "candidates", None) or []):
            content = getattr(cand, "content", None)
            for p in (getattr(content, "parts", None) or []):
                if getattr(p, "text", None):
                    parts_text.append(p.text)
        return "".join(parts_text).strip()
    except Exception:
        return ""


def _generate(contents, max_retries=4):
    """Gemini'ga so'rov yuboradi (qayta urinish + bo'sh javobni ushlash + safety bilan)."""
    last_error = None
    for attempt in range(max_retries):
        try:
            kwargs = {"model": "gemini-2.5-flash", "contents": contents}
            if genai_types is not None and SAFETY_SETTINGS is not None:
                try:
                    kwargs["config"] = genai_types.GenerateContentConfig(safety_settings=SAFETY_SETTINGS)
                except Exception:
                    pass
            response = client.models.generate_content(**kwargs)
            text = _extract_text(response)
            if text:
                return text
            raise Exception("Bo'sh javob keldi")
        except Exception as e:
            last_error = e
            logger.warning(f"Generate urinish {attempt+1}/{max_retries}: {e}")
            time.sleep((attempt + 1) * 5)
    raise last_error


def _analyze(uploaded_file, prompt, max_retries=4):
    """Bitta video uchun tahlil."""
    return _generate([uploaded_file, prompt], max_retries=max_retries)


def _gemini_process_images(image_paths, prompt):
    """BLOKLAYDIGAN: bir nechta rasm (skrinshot)ni Gemini bilan tahlil qiladi.
    Faqat alohida thread'da chaqiriladi (asyncio.to_thread)."""
    uploaded = []
    try:
        for p in image_paths:
            uploaded.append(_upload_and_wait(p))
        return _generate(uploaded + [prompt])
    finally:
        for u in uploaded:
            try:
                client.files.delete(name=u.name)
            except Exception:
                pass


def _gemini_process(tmp_path, prompt):
    """BLOKLAYDIGAN to'liq Gemini ishi. Faqat alohida thread'da chaqiriladi
    (asyncio.to_thread), shunda bot muzlamaydi va Pyrogram uzilmaydi."""
    uploaded = None
    try:
        uploaded = _upload_and_wait(tmp_path)
        return _analyze(uploaded, prompt)
    finally:
        if uploaded is not None:
            try:
                client.files.delete(name=uploaded.name)
            except Exception:
                pass


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Profil rejimida — profil skrinshoti; aks holda — to'lov cheki."""
    # PROFIL REJIMI: rasm = profil skrinshoti
    if context.user_data.get('mode') == 'profile':
        imgs = context.user_data.setdefault('profile_imgs', [])
        imgs.append(update.message.photo[-1].file_id)
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(t(context, 'profile_btn'), callback_data='profile_analyze')
        ]])
        await update.message.reply_text(
            t(context, 'profile_got').format(n=len(imgs)), reply_markup=kb
        )
        return

    # AKS HOLDA: to'lov cheki (eski mantiq)
    pending = context.user_data.get('pending_pkg')
    if not pending:
        return  # Chek kutilmayotgan bo'lsa, e'tibor bermaymiz
    user = update.effective_user
    count, amount = PACKAGES.get(pending, (0, 0))
    # Chekni admin ga yuboramiz
    caption = (
        f"💳 YANGI TO'LOV CHEKI\n\n"
        f"👤 {user.first_name} (@{user.username or 'username yo`q'})\n"
        f"🆔 ID: {user.id}\n"
        f"📦 Paket: {count} ta tahlil\n"
        f"💰 Summa: {amount:,} so'm"
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Tasdiqlash", callback_data=f"approve_{user.id}_{pending}")
    ]])
    try:
        await context.bot.send_photo(
            ADMIN_ID,
            update.message.photo[-1].file_id,
            caption=caption,
            reply_markup=keyboard
        )
        await update.message.reply_text(t(context, 'receipt_sent'))
        context.user_data['pending_pkg'] = None
    except Exception as e:
        logger.error(f"Chek yuborishda xato: {e}")
        await update.message.reply_text(t(context, 'error'))


def _parse_analysis(text):
    """Gemini javobini foiz / qisqa / to'liq qismlarga ajratadi.
    Agar teglar topilmasa, butun matnni ikkalasiga ham ishlatadi (xavfsiz)."""
    foiz = 0
    m = re.search(r'\[FOIZ\](.*?)\[/FOIZ\]', text, re.DOTALL)
    if m:
        digits = re.sub(r'[^0-9]', '', m.group(1))
        if digits:
            foiz = max(0, min(100, int(digits[:3])))
    qm = re.search(r'\[QISQA\](.*?)\[/QISQA\]', text, re.DOTALL)
    tm = re.search(r'\[TOLIQ\](.*?)\[/TOLIQ\]', text, re.DOTALL)
    qisqa = qm.group(1).strip() if qm else None
    toliq = tm.group(1).strip() if tm else None
    # Zaxira: teglar topilmasa, butun matnni ishlatamiz
    if not qisqa and not toliq:
        clean = re.sub(r'\[/?FOIZ\].*?(\n|$)', '', text).strip()
        qisqa = toliq = clean or text.strip()
    elif not toliq:
        toliq = qisqa
    elif not qisqa:
        qisqa = toliq
    return foiz, qisqa, toliq


async def video_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    user_id = message.from_user.id

    # Balans tekshirish
    if get_balance(user_id) <= 0:
        await message.reply_text(t(context, 'no_balance'), reply_markup=package_keyboard(context))
        return

    wait_msg = await message.reply_text(t(context, 'received'))
    tmp_path = None
    uploaded_file = None
    try:
        if message.video:
            video = message.video
        elif message.document and message.document.mime_type and 'video' in message.document.mime_type:
            video = message.document
        else:
            await wait_msg.edit_text(t(context, 'wrong_format'))
            return

        if video.file_size and video.file_size > MAX_VIDEO_BYTES:
            await wait_msg.edit_text(t(context, 'too_big'))
            return

        # Navbat: bir vaqtda faqat MAX_CONCURRENT ta tahlil ishlaydi.
        # Hamma slot band bo'lsa, foydalanuvchiga "navbatda" deb bildiramiz.
        if _video_semaphore.locked():
            await wait_msg.edit_text(t(context, 'queued'))

        async with _video_semaphore:
            tmp_path = os.path.join("/tmp", f"{uuid.uuid4().hex}.mp4")
            is_big = bool(video.file_size and video.file_size > 20 * 1024 * 1024)

            # Asosiy yo'l: Pyrogram orqali yuklab olish (20MB cheklovi yo'q, 2GB gacha)
            downloaded = None
            if pyro is not None:
                try:
                    downloaded = await pyro.download_media(video.file_id, file_name=tmp_path)
                except Exception as e:
                    logger.warning(f"Pyrogram yuklab olishda xato: {e}")

            if downloaded:
                tmp_path = downloaded
            else:
                # Zaxira yo'l: Pyrogram yo'q/ishlamadi.
                # Katta video bo'lsa Bot API ham eplay olmaydi -> ogohlantiramiz.
                if is_big:
                    await wait_msg.edit_text(t(context, 'too_big'))
                    return
                file = await context.bot.get_file(video.file_id)
                await file.download_to_drive(tmp_path)

            await wait_msg.edit_text(t(context, 'uploading'))
            prompt = PROMPT_RU if get_lang(context) == 'ru' else PROMPT_UZ

            await wait_msg.edit_text(t(context, 'analyzing'))
            # MUHIM: Gemini ishi alohida thread'da bajariladi -> bot muzlamaydi,
            # Pyrogram uzilmaydi, bir nechta odam bir vaqtda ishlatsa ham ishlaydi.
            tahlil = await asyncio.to_thread(_gemini_process, tmp_path, prompt)

            if not tahlil or not tahlil.strip():
                raise Exception("Tahlil bo'sh keldi")

            await wait_msg.edit_text(t(context, 'ready'))
            # Balans FAQAT muvaffaqiyatli tahlildan keyin yechiladi (xatoda yechilmaydi)
            use_balance(user_id)

            # Tahlilni qism qism ajratamiz: foiz, qisqa, to'liq
            foiz, qisqa, toliq = _parse_analysis(tahlil)
            uname = message.from_user.username or message.from_user.first_name or ""
            aid = save_analysis(user_id, username=uname, kind="video",
                                file_id=video.file_id, foiz=foiz, qisqa=qisqa, toliq=toliq)

            # Mijozga QISQA tahlil + "To'liq tahlilni ko'rish" tugmasi
            kb = None
            if aid:
                kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton(t(context, 'full_btn'), callback_data=f"full_{aid}")
                ]])
            if len(qisqa) <= 4000:
                await message.reply_text(qisqa, reply_markup=kb)
            else:
                parts = [qisqa[i:i+4000] for i in range(0, len(qisqa), 4000)]
                for idx, chunk in enumerate(parts):
                    await message.reply_text(chunk, reply_markup=(kb if idx == len(parts) - 1 else None))

    except Exception as e:
        logger.error(f"Yakuniy xato: {e}")
        await wait_msg.edit_text(t(context, 'error'))
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text in (TEXTS['uz']['menu_video'], TEXTS['ru']['menu_video']):
        context.user_data['mode'] = None
        await update.message.reply_text(t(context, 'send_video'))
    elif text in (TEXTS['uz']['menu_profile'], TEXTS['ru']['menu_profile']):
        user_id = update.effective_user.id
        if get_balance(user_id) <= 0:
            await update.message.reply_text(t(context, 'no_balance'), reply_markup=package_keyboard(context))
            return
        context.user_data['mode'] = 'profile'
        context.user_data['profile_imgs'] = []
        context.user_data['pending_pkg'] = None
        await update.message.reply_text(t(context, 'profile_instr'))
    elif text in (TEXTS['uz']['menu_balance'], TEXTS['ru']['menu_balance']):
        bal = get_balance(update.effective_user.id)
        await update.message.reply_text(t(context, 'balance_info').format(n=bal))
    elif text in (TEXTS['uz']['menu_lang'], TEXTS['ru']['menu_lang']):
        await update.message.reply_text("🌐 Tilni tanlang / Выберите язык:", reply_markup=lang_keyboard())
    elif text in (TEXTS['uz']['menu_help'], TEXTS['ru']['menu_help']):
        await update.message.reply_text(t(context, 'help_text'))
    else:
        await update.message.reply_text(t(context, 'send_video'))


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(t(context, 'help_text'))


async def til_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🌐 Tilni tanlang / Выберите язык:", reply_markup=lang_keyboard())


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    total_users, total_analyses, today_analyses, revenue = get_stats()
    text = (
        "📊 ADMIN STATISTIKA\n\n"
        f"👥 Jami foydalanuvchilar: {total_users}\n"
        f"🎬 Jami tahlillar: {total_analyses}\n"
        f"📅 Bugungi tahlillar: {today_analyses}\n"
        f"💰 Tasdiqlangan daromad: {revenue:,} so'm"
    )
    await update.message.reply_text(text)


async def top_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    rows = get_top_today(10)
    if not rows:
        await update.message.reply_text("📊 Bugun hali tahlil qilingan video yo'q.")
        return
    await update.message.reply_text(
        f"🏆 BUGUNGI TOP {len(rows)} VIDEO (rekka chiqish ehtimoli bo'yicha)\n\n"
        "Quyida har birini videosi bilan yuboraman 👇"
    )
    for i, row in enumerate(rows, 1):
        aid, uid, uname, foiz, file_id, created = row
        who = f"@{uname}" if uname else f"ID {uid}"
        caption = f"#{i} • 📈 Rekka ehtimoli: {foiz}%\n👤 {who}\n🕐 {created}"
        try:
            await context.bot.send_video(update.effective_chat.id, file_id, caption=caption)
        except Exception as e:
            logger.warning(f"Top video yuborishda xato (id={aid}): {e}")
            await update.message.reply_text(caption + "\n⚠️ (videoni yuborib bo'lmadi)")


async def post_init(application):
    # Pyrogram yuklab oluvchini ishga tushiramiz (agar sozlangan bo'lsa).
    # Ishga tushmasa ham bot to'xtamaydi — kichik videolar Bot API orqali ishlaydi.
    if pyro is not None:
        try:
            await pyro.start()
            logger.info("Pyrogram yuklab oluvchi ishga tushdi (2GB gacha)")
        except Exception as e:
            logger.error(f"Pyrogram ishga tushmadi, kichik videolar rejimida davom: {e}")
    await application.bot.set_my_commands([
        BotCommand("start", "🚀 Boshlash / Начать"),
        BotCommand("help", "ℹ️ Yordam / Помощь"),
        BotCommand("til", "🌐 Til / Язык"),
    ])
    # Bot tavsifi (profilda va Start oldida ko'rinadi) - bir marta o'rnatiladi
    try:
        await application.bot.set_my_short_description(
            short_description="Instagram video va profilingizni AI bilan tahlil qiladi 🎬📊"
        )
        await application.bot.set_my_description(
            description=("🎬 Instagram videongizni AI bilan tahlil qilaman: hook, "
                         "vizual, audio, montaj va rekka chiqish ehtimoli (%).\n\n"
                         "📊 Profil tahlili ham bor.\n\nBoshlash uchun «Start» 👇")
        )
    except Exception as e:
        logger.warning(f"Tavsif o'rnatishda xato: {e}")


async def post_shutdown(application):
    if pyro is not None:
        try:
            await pyro.stop()
        except Exception:
            pass


def main():
    init_db()
    app = (Application.builder()
           .token(TELEGRAM_TOKEN)
           .concurrent_updates(True)   # MUHIM: har bir foydalanuvchi alohida ishlanadi
           .post_init(post_init)
           .post_shutdown(post_shutdown)
           .build())
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("til", til_command))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("top", top_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, video_handler))
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    logger.info("Bot ishga tushdi!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
