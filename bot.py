import os
import re
import io
import wave
import uuid
import base64
import asyncio
import logging
import tempfile
import time
import psycopg
from psycopg_pool import ConnectionPool
from datetime import datetime, timedelta
from aiohttp import web
from google import genai
from telegram import (Update, InlineKeyboardButton, InlineKeyboardMarkup,
                      ReplyKeyboardMarkup, KeyboardButton, BotCommand, LabeledPrice)
from telegram.ext import (Application, CommandHandler, MessageHandler, filters,
                          ContextTypes, CallbackQueryHandler, PreCheckoutQueryHandler)
from pyrogram import Client as PyroClient

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
# my.telegram.org dan olinadi (katta video yuklab olish uchun)
API_ID = int(os.getenv("API_ID", "0") or "0")
API_HASH = os.getenv("API_HASH", "")
# Maksimal qabul qilinadigan video hajmi (2GB). Kerak bo'lsa pasaytiring.
MAX_VIDEO_BYTES = 2 * 1024 * 1024 * 1024
# Video o'lcham chegaralari (xarajat nazorati uchun)
FREE_VIDEO_MB = int(os.getenv("FREE_VIDEO_MB", "100"))    # bepul: 100 MB
PAID_VIDEO_MB = int(os.getenv("PAID_VIDEO_MB", "1000"))   # pullik: 1000 MB (1 GB)
FREE_VIDEO_BYTES = FREE_VIDEO_MB * 1024 * 1024
PAID_VIDEO_BYTES = PAID_VIDEO_MB * 1024 * 1024
# Bir vaqtda nechta video tahlil qilinishi mumkin (qolganlar navbatda kutadi).
# Pullik Gemini (Tier 2) + kuchli server. Tier 2 quvvatli - navbat deyarli bo'lmaydi.
# Railway Variables'dan MAX_CONCURRENT ni xohlagancha oshirish mumkin.
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT", "80") or "80")
# Navbat mexanizmi: bir vaqtda MAX_CONCURRENT ta tahlil ishlaydi (hammaga bitta pool).
_video_semaphore = asyncio.Semaphore(MAX_CONCURRENT)
ADMIN_ID = 7589459697
# Barcha adminlar (cheksiz tahlil, /top, tannarx hisoboti va h.k.)
ADMIN_IDS = [7589459697, 5808245573, 356530813]
# So'rov javoblari (fikrlar) yuboriladigan guruh ID (Railway'dan ham o'zgartirish mumkin)
FIKR_GROUP_ID = os.getenv("FIKR_GROUP_ID", "-1003784847158")
# Cheklar va barcha to'lovlar boradigan guruh (3 admin nazorat qiladi)
CHEK_GROUP_ID = os.getenv("CHEK_GROUP_ID", "-1004458514532")
# Karta orqali to'lov uchun karta ma'lumotlari
CARD_NUMBER = os.getenv("CARD_NUMBER", "6262 7300 6521 3151")
CARD_HOLDER = os.getenv("CARD_HOLDER", "Boqijonov Nurislom")
# Ishonch videosi (jamoa haqida) + fikr bildirib 1 bepul olish
VIDEO_FILE_ID = os.getenv("VIDEO_FILE_ID", "BAACAgIAAxkBAAEDdblqO1vKqmm-sO5EpO4ithiAdFI0ogACdaEAAh442EmljRTrS2sTqjwE")
# Marafon 3-kun uchun kanal (admin o'z kanalini qo'yadi: @username yoki link)
MARAFON_KANAL = os.getenv("MARAFON_KANAL", "@InstadoctorAI")


def is_admin(user_id):
    return user_id in ADMIN_IDS
CARD_NUMBER = "6262 7300 6521 3151"
CARD_NAME = "Boqijonov Nurislom"

# Obuna (1 oylik): narx (so'm) va kun
SUB_PRICE = 29900
SUB_DAYS = 30
# 1 martalik tahlil narxi (so'm)
ONE_PRICE = 5090
# Haftalik test paketi: 7 kun / 6990 so'm
TEST_PRICE = 6990
TEST_DAYS = 7
# ===== Payme Merchant API =====
PAYME_MERCHANT_ID = os.getenv("PAYME_MERCHANT_ID", "")   # kassa ID (Payme kabinetdan)
PAYME_KEY = os.getenv("PAYME_KEY", "")                    # maxfiy kalit (Payme kabinetdan)
PAYME_TEST_KEY = os.getenv("PAYME_TEST_KEY", "")          # test kaliti (sandbox)
# ===== Click Merchant API =====
CLICK_SERVICE_ID = os.getenv("CLICK_SERVICE_ID", "")      # Click service ID
CLICK_MERCHANT_ID = os.getenv("CLICK_MERCHANT_ID", "")    # Click merchant ID
CLICK_SECRET_KEY = os.getenv("CLICK_SECRET_KEY", "")      # Click maxfiy kalit (imzo uchun)
CLICK_MERCHANT_USER_ID = os.getenv("CLICK_MERCHANT_USER_ID", "")  # merchant user id
WEB_PORT = int(os.getenv("PORT", "8080"))                 # Railway web port
_bot_app = None                                           # global bot ilovasi (Payme tabrigi uchun)
_main_loop = None                                         # asosiy event loop (web thread'dan xabar yuborish uchun)

# Juma aksiyasi uchun eskirmaydigan maslahatlar (har juma bittasi aylanadi)
JUMA_MASLAHATLAR = [
    "🎬 <b>Birinchi 3 soniya — eng muhim!</b> Video boshida tomoshabinni darrov \"ushlang\": "
    "savol bering, qiziq kadr ko'rsating yoki natijani oldindan ayting. Aks holda ular scroll qiladi.",

    "⏰ <b>Joylashtirish vaqti muhim.</b> Auditoriyangiz eng faol bo'lgan paytda post qiling — "
    "odatda ertalab 7-9 yoki kechqurun 19-22. Bir necha vaqtni sinab, qaysi biri ko'proq ko'rishini ko'ring.",

    "🎵 <b>Trend ovoz/musiqa ishlating.</b> Instagram algoritmi trenddagi ovozlarni ko'proq ko'rsatadi. "
    "Reels yaratishdan oldin, qaysi ovoz hozir ko'p ishlatilayotganini kuzating.",

    "📝 <b>Birinchi qatorda ilmoq bo'lsin.</b> Tavsif (caption) boshida qiziqarli savol yoki "
    "va'da yozing — \"...ni bilasizmi?\" yoki \"3 ta sirni aytaman\". Bu tomoshabinni to'xtatadi.",

    "🔁 <b>Takror ko'riladigan video yarating.</b> Qisqa, dinamik, oxiri boshiga ulanadigan videolar "
    "ko'p marta ko'riladi — bu algoritm uchun kuchli signal. Reelni 7-15 soniya qiling.",

    "💬 <b>Izohlarni rag'batlantiring.</b> Video oxirida savol bering yoki fikr so'rang. "
    "Ko'p izoh = algoritm videongizni ko'proq odamga ko'rsatadi. Faollik — kalit!",
]
# Payme to'lov holatlari
PAYME_STATE_CREATED = 1       # transaksiya yaratilgan (to'lov kutilmoqda)
PAYME_STATE_PERFORMED = 2     # to'lov amalga oshdi
PAYME_STATE_CANCELED = -1     # bekor qilingan (to'lovsiz)
PAYME_STATE_CANCELED_AFTER = -2  # to'lovdan keyin bekor (qaytarilgan)

# Yangi foydalanuvchiga beriladigan BEPUL tahlil soni
FREE_TRIAL = 1

# Payme (Telegram Payments) provider token - Railway Variables'dan olinadi
PROVIDER_TOKEN = os.getenv("PROVIDER_TOKEN", "")

# ===== GEMINI CLIENT: Vertex AI (barqaror, 503 kam) yoki AI Studio (zaxira) =====
GCP_PROJECT_ID = os.getenv("GCP_PROJECT_ID", "")
GCP_LOCATION = os.getenv("GCP_LOCATION", "us-central1")
GCS_BUCKET = os.getenv("GCS_BUCKET", "instadoctor-videos-2026")
_gcp_json = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON", "")

client = None
_using_vertex = False
_gcs_client = None
if GCP_PROJECT_ID and _gcp_json:
    try:
        _cred_path = "/tmp/gcp_creds.json"
        with open(_cred_path, "w") as _f:
            _f.write(_gcp_json)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = _cred_path
        client = genai.Client(vertexai=True, project=GCP_PROJECT_ID, location=GCP_LOCATION)
        _using_vertex = True
        print(f"[INFO] Vertex AI ishga tushdi (project={GCP_PROJECT_ID}, loc={GCP_LOCATION})")
        # GCS client (video saqlash uchun)
        try:
            from google.cloud import storage as _gcs_storage
            _gcs_client = _gcs_storage.Client(project=GCP_PROJECT_ID)
            print(f"[INFO] GCS bucket ulandi: {GCS_BUCKET}")
        except Exception as _ge:
            print(f"[ERROR] GCS ulanmadi ({_ge}); Vertex video ishlamasligi mumkin")
    except Exception as _e:
        print(f"[ERROR] Vertex AI ulanmadi ({_e}); AI Studio'ga qaytamiz")
        client = None
        _using_vertex = False

if client is None:
    client = genai.Client(api_key=GEMINI_API_KEY)
    print("[INFO] AI Studio API kaliti ishlatilmoqda (zaxira)")

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
                cur.execute("""CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY, value TEXT)""")
                cur.execute("""CREATE TABLE IF NOT EXISTS eslatmalar (
                    id SERIAL PRIMARY KEY, user_id BIGINT, matn TEXT,
                    eslatma_vaqt TEXT, yuborildi BOOLEAN DEFAULT FALSE, created TEXT)""")
                cur.execute("""CREATE TABLE IF NOT EXISTS sorov_javoblar (
                    id SERIAL PRIMARY KEY, user_id BIGINT, username TEXT,
                    javob1 TEXT, javob2 TEXT, created TEXT)""")
                # Payme Merchant API transaksiyalari
                cur.execute("""CREATE TABLE IF NOT EXISTS payme_transactions (
                    id SERIAL PRIMARY KEY,
                    payme_id TEXT UNIQUE,
                    user_id BIGINT,
                    package TEXT,
                    amount BIGINT,
                    state INTEGER DEFAULT 1,
                    reason INTEGER,
                    create_time BIGINT DEFAULT 0,
                    perform_time BIGINT DEFAULT 0,
                    cancel_time BIGINT DEFAULT 0,
                    created TEXT)""")
                # users jadvaliga yangi ustunlar (obuna + referral)
                for col, typ in [("sub_until", "TEXT"),
                                 ("referred_by", "BIGINT"),
                                 ("ref_credited", "BOOLEAN DEFAULT FALSE"),
                                 ("ref_reward_given", "BOOLEAN DEFAULT FALSE"),
                                 ("aksiya_given", "BOOLEAN DEFAULT FALSE"),
                                 ("obuna_taklif_given", "BOOLEAN DEFAULT FALSE"),
                                 ("test_taklif_given", "BOOLEAN DEFAULT FALSE"),
                                 ("test_sorov_given", "BOOLEAN DEFAULT FALSE"),
                                 ("eslatma_given", "BOOLEAN DEFAULT FALSE"),
                                 ("yangilik_given", "BOOLEAN DEFAULT FALSE"),
                                 ("juma_balance", "INTEGER DEFAULT 0"),
                                 ("juma_sana", "TEXT"),
                                 ("video_fikr_given", "BOOLEAN DEFAULT FALSE"),
                                 ("premium_fikr_given", "BOOLEAN DEFAULT FALSE"),
                                 ("video_xabar_given", "BOOLEAN DEFAULT FALSE"),
                                 ("video_sovga_olindi", "BOOLEAN DEFAULT FALSE"),
                                 ("tarif7_sana", "TEXT"),
                                 ("drip2_given", "BOOLEAN DEFAULT FALSE"),
                                 ("drip3_given", "BOOLEAN DEFAULT FALSE"),
                                 ("sotuv1_given", "BOOLEAN DEFAULT FALSE"),
                                 ("sotuv1b_given", "BOOLEAN DEFAULT FALSE"),
                                 ("sotuv3_given", "BOOLEAN DEFAULT FALSE"),
                                 ("sotuv4_given", "BOOLEAN DEFAULT FALSE"),
                                 ("sotuv5_given", "BOOLEAN DEFAULT FALSE"),
                                 ("sotuv_bepul_olindi", "TEXT"),
                                 ("premium_balance", "INTEGER DEFAULT 0"),
                                 ("bloklangan", "BOOLEAN DEFAULT FALSE"),
                                 ("renewal_eslatma_given", "BOOLEAN DEFAULT FALSE"),
                                 ("test_renewal_given", "BOOLEAN DEFAULT FALSE"),
                                 ("renewal_chegirma_sana", "TEXT"),
                                 ("tugash_xabar_given", "BOOLEAN DEFAULT FALSE"),
                                 ("marafon_kun", "INTEGER DEFAULT 0"),
                                 ("marafon_start", "TEXT"),
                                 ("marafon_kunlik_sana", "TEXT"),
                                 ("marafon_tugadi", "BOOLEAN DEFAULT FALSE"),
                                 ("marafon_bajarilgan", "INTEGER DEFAULT 0"),
                                 ("marafon_oxirgi_bajarilgan", "TEXT"),
                                 ("marafon_19900_sana", "TEXT"),
                                 ("marafon_6990_given", "BOOLEAN DEFAULT FALSE"),
                                 ("studiya_oy", "TEXT"),
                                 ("studiya_marta", "INTEGER DEFAULT 0"),
                                 ("goya_oy", "TEXT"),
                                 ("goya_marta", "INTEGER DEFAULT 0"),
                                 ("sorov_given", "BOOLEAN DEFAULT FALSE"),
                                 ("sorov_reward", "BOOLEAN DEFAULT FALSE"),
                                 ("chegirma_kun", "TEXT")]:
                    cur.execute(f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {col} {typ}")
                # analyses jadvaliga yangi ustunlar (bor bo'lsa - tegmaydi)
                for col, typ in [("username", "TEXT"), ("kind", "TEXT"),
                                 ("file_id", "TEXT"), ("foiz", "INTEGER DEFAULT 0"),
                                 ("qisqa", "TEXT"), ("toliq", "TEXT"),
                                 ("tokens", "INTEGER DEFAULT 0"),
                                 ("narx", "REAL DEFAULT 0")]:
                    cur.execute(f"ALTER TABLE analyses ADD COLUMN IF NOT EXISTS {col} {typ}")
        logger.info("PostgreSQL baza tayyor (jadvallar mavjud)")
    except Exception as e:
        logger.error(f"init_db xato: {e}")


def save_user(user_id, username, first_name):
    # Yangi foydalanuvchiga FREE_TRIAL ta BEPUL tahlil. ON CONFLICT DO NOTHING
    # tufayli faqat BIRINCHI marta beriladi - qayta /start bossa qayta berilmaydi.
    _db_execute(
        "INSERT INTO users (user_id, username, first_name, joined, balance) "
        "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (user_id) DO NOTHING",
        (user_id, username or "", first_name or "", datetime.now().strftime("%Y-%m-%d %H:%M"), FREE_TRIAL)
    )


def user_exists(user_id):
    row = _db_execute("SELECT 1 FROM users WHERE user_id = %s", (user_id,), fetch='one')
    return row is not None


def get_balance(user_id):
    if is_admin(user_id):
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
    if is_admin(user_id):
        return
    _db_execute("UPDATE users SET balance = balance - 1 WHERE user_id = %s AND balance > 0", (user_id,))


def get_premium_balance(user_id):
    """Premium balans - 1 tahlillik TO'LIQ premium (hook, ovoz bilan)."""
    if is_admin(user_id):
        return 999999
    row = _db_execute("SELECT premium_balance FROM users WHERE user_id = %s", (user_id,), fetch='one')
    return (row[0] or 0) if row else 0


def is_blocked(user_id):
    """Foydalanuvchi bloklanganmi?"""
    if is_admin(user_id):
        return False
    row = _db_execute("SELECT bloklangan FROM users WHERE user_id = %s", (user_id,), fetch='one')
    return bool(row and row[0])


def add_premium_balance(user_id, amount):
    _db_execute(
        "INSERT INTO users (user_id, username, first_name, joined, balance) "
        "VALUES (%s, '', '', %s, 0) ON CONFLICT (user_id) DO NOTHING",
        (user_id, datetime.now().strftime("%Y-%m-%d %H:%M"))
    )
    _db_execute("UPDATE users SET premium_balance = COALESCE(premium_balance,0) + %s WHERE user_id = %s",
                (amount, user_id))


def use_premium_balance(user_id):
    if is_admin(user_id):
        return
    _db_execute("UPDATE users SET premium_balance = premium_balance - 1 "
                "WHERE user_id = %s AND premium_balance > 0", (user_id,))


def _parse_dt(s):
    """sub_until ni turli formatlarda o'qiydi (DB'da format har xil bo'lishi mumkin)."""
    if not s:
        return None
    s = str(s).strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            continue
    # Oxirgi urinish: ISO (mikrosekund, timezone)
    try:
        return datetime.fromisoformat(s.split("+")[0].split(".")[0].strip())
    except Exception:
        return None


def sub_active(user_id):
    """Obuna faolmi? (sub_until hozirgi vaqtdan keyinmi)"""
    if is_admin(user_id):
        return True
    row = _db_execute("SELECT sub_until FROM users WHERE user_id = %s", (user_id,), fetch='one')
    if not row or not row[0]:
        return False
    d = _parse_dt(row[0])
    if d is None:
        return False
    return d > datetime.now()


def sub_until_str(user_id):
    row = _db_execute("SELECT sub_until FROM users WHERE user_id = %s", (user_id,), fetch='one')
    return row[0] if row and row[0] else None


def get_juma_balance(user_id):
    """Bugungi juma-bepul balansi (faqat juma kuni amal qiladi)."""
    bugun = datetime.now().strftime("%Y-%m-%d")
    row = _db_execute(
        "SELECT juma_balance, juma_sana FROM users WHERE user_id = %s", (user_id,), fetch='one')
    if row and row[0] and row[0] > 0 and row[1] == bugun:
        return row[0]
    return 0


def kreator_status(tahlil_soni):
    """Tahlil soniga qarab (emoji, nom, keyingi_daraja_uchun_kerak, keyingi_nom) qaytaradi."""
    if tahlil_soni == 0:
        return ("🆕", "Yangi boshlovchi", 1, "O'suvchi bloger")
    if tahlil_soni <= 4:
        return ("📈", "O'suvchi bloger", 5 - tahlil_soni, "Reels-meyker")
    if tahlil_soni <= 9:
        return ("🚀", "Reels-meyker", 10 - tahlil_soni, "Kontent Profi")
    if tahlil_soni <= 15:
        return ("🔥", "Kontent Profi", 16 - tahlil_soni, "Algoritmlar Bossi")
    return ("👑", "Algoritmlar Bossi", 0, None)


def kreator_progress_bar(tahlil_soni):
    """Daraja ichidagi progressni bar qilib qaytaradi."""
    # Har daraja chegarasi
    if tahlil_soni == 0:
        foiz = 0
    elif tahlil_soni <= 4:
        foiz = int(tahlil_soni / 5 * 100)
    elif tahlil_soni <= 9:
        foiz = int((tahlil_soni - 5) / 5 * 100)
    elif tahlil_soni <= 15:
        foiz = int((tahlil_soni - 10) / 6 * 100)
    else:
        foiz = 100
    tuldi = int(foiz / 10)
    return "█" * tuldi + "░" * (10 - tuldi), foiz


def has_access(user_id):
    """'admin' | 'sub' (obuna faol) | 'credit' (bepul tahlil bor) | 'juma' | 'none'"""
    if is_admin(user_id):
        return 'admin'
    if sub_active(user_id):
        return 'sub'
    if get_premium_balance(user_id) > 0:
        return 'sub'   # premium balans = to'liq premium (hook, ovoz ochiq)
    if get_balance(user_id) > 0:
        return 'credit'
    if get_juma_balance(user_id) > 0:
        return 'juma'
    return 'none'


def activate_subscription(user_id, days=SUB_DAYS):
    """Obunani yoqadi/uzaytiradi. Yangi tugash sanasini qaytaradi."""
    _db_execute(
        "INSERT INTO users (user_id, username, first_name, joined, balance) "
        "VALUES (%s, '', '', %s, 0) ON CONFLICT (user_id) DO NOTHING",
        (user_id, datetime.now().strftime("%Y-%m-%d %H:%M"))
    )
    base = datetime.now()
    cur = sub_until_str(user_id)
    if cur:
        try:
            d = datetime.strptime(cur, "%Y-%m-%d %H:%M")
            if d > base:
                base = d  # hali tugamagan obunaga qo'shamiz
        except Exception:
            pass
    new_until = (base + timedelta(days=days)).strftime("%Y-%m-%d %H:%M")
    # Yangi obuna - renewal eslatma bayrog'ini tiklaymiz (keyingi tugashda yana eslatamiz)
    _db_execute("UPDATE users SET sub_until = %s, renewal_eslatma_given = FALSE, tugash_xabar_given = FALSE WHERE user_id = %s",
                (new_until, user_id))
    return new_until


def set_referrer(user_id, referrer_id):
    """Faqat referred_by bo'sh bo'lsa va o'ziga o'zi emas bo'lsa o'rnatadi."""
    if not referrer_id or referrer_id == user_id:
        return
    row = _db_execute("SELECT referred_by FROM users WHERE user_id = %s", (user_id,), fetch='one')
    if row is not None and row[0] is None:
        _db_execute("UPDATE users SET referred_by = %s WHERE user_id = %s", (referrer_id, user_id))


def get_setting(key, default=None):
    row = _db_execute("SELECT value FROM settings WHERE key = %s", (key,), fetch='one')
    return row[0] if row else default


def set_setting(key, value):
    _db_execute(
        "INSERT INTO settings (key, value) VALUES (%s, %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (key, str(value))
    )


def tarif7_tugadimi(user_id=None):
    """7 kunlik aksiya tugaganmi? (1) qo'lda o'chirilgan, (2) global vaqt o'tgan,
    (3) shu user uchun shaxsiy 24 soat o'tgan. True = tugagan (ko'rsatmaymiz)."""
    if get_setting("tarif7_aksiya", "on") == "off":
        return True
    # Global tugash vaqti
    _tugash = get_setting("tarif7_tugash", "")
    if _tugash:
        try:
            UZ_OFF = int(os.getenv("UZ_TZ_OFFSET", "5"))
            hozir_uz = datetime.utcnow() + timedelta(hours=UZ_OFF)
            if hozir_uz > datetime.strptime(_tugash, "%Y-%m-%d %H:%M"):
                return True
        except Exception:
            pass
    # Shaxsiy 24 soat (drip foydalanuvchisi)
    if user_id is not None:
        _urow = _db_execute("SELECT tarif7_sana FROM users WHERE user_id=%s", (user_id,), fetch='one')
        if _urow and _urow[0]:
            try:
                olingan = datetime.strptime(_urow[0], "%Y-%m-%d %H:%M")
                if (datetime.now() - olingan).total_seconds() > 24 * 3600:
                    return True
            except Exception:
                pass
    return False


def auto_aksiya_on():
    """Avtomatik +2 aksiya yoqilganmi? (default: o'chiq)"""
    return get_setting("auto_aksiya", "off") == "on"


SUB_PRICE_DISCOUNT = 19900  # Chegirma narxi (29900 -> 19900, sotuv voronkasi rejasi)
SUB_PRICE_RENEWAL = 23900   # Renewal chegirma (29900 -> 23900, 20% - obuna uzaytirish)

def discount_active():
    """Chegirma faolmi? (yoqilgan va 24 soat o'tmagan)"""
    until = get_setting("chegirma_until", "")
    if not until:
        return False
    try:
        until_dt = datetime.strptime(until, "%Y-%m-%d %H:%M:%S")
        return datetime.now() < until_dt
    except Exception:
        return False


def current_sub_price():
    """Oddiy menyu narxi - DOIM to'liq (29,900).
    Chegirma (19,900/23,900) faqat maxsus tugmalarda (buy_sub_19, buy_sub_20)."""
    return SUB_PRICE


def sub_btn_label(context):
    """Obuna tugmasi matni - joriy narx bilan (chegirma bo'lsa 19 900, aks holda 29 900)."""
    narx = f"{current_sub_price():,}".replace(",", " ")
    return t(context, 'sub_btn_price').format(narx=narx)


def get_sotuv_msg():
    """Sotuv xabari matni: agar admin o'zgartirgan bo'lsa - o'sha, aks holda koddagi default."""
    custom = get_setting("sotuv_matn", "")
    if custom:
        return custom
    return TEXTS['uz']['sotuv_msg']


def grant_auto_aksiya(user_id):
    """1 ta bepulni ishlatib, balansi tugagan, aksiyani hali olmagan userga
    avtomatik +1 beradi va xabar yuborish kerakligini bildiradi (True).
    Faqat avtomatik aksiya YOQILGAN bo'lsa ishlaydi."""
    if is_admin(user_id):
        return False
    if not auto_aksiya_on():
        return False
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    row = _db_execute(
        "SELECT 1 FROM users WHERE user_id = %s "
        "AND COALESCE(balance,0) <= 0 "
        "AND (sub_until IS NULL OR sub_until <= %s) "
        "AND COALESCE(aksiya_given, FALSE) = FALSE",
        (user_id, now), fetch='one'
    )
    if row:
        add_balance(user_id, 1)
        _db_execute("UPDATE users SET aksiya_given = TRUE WHERE user_id = %s", (user_id,))
        return True
    return False


def should_send_auto_offer(user_id):
    """Avtomatik obuna taklifi yuborilsinmi? Shartlar:
    - aksiya (+2) olgan
    - hozir balansi 0
    - obunasi yo'q
    - bu taklifni hali olmagan
    Mos kelsa True qaytaradi va belgilab qo'yadi (takror bo'lmasin)."""
    if is_admin(user_id):
        return False
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    row = _db_execute(
        "SELECT 1 FROM users WHERE user_id = %s "
        "AND COALESCE(aksiya_given, FALSE) = TRUE "
        "AND COALESCE(balance,0) <= 0 "
        "AND (sub_until IS NULL OR sub_until <= %s) "
        "AND COALESCE(obuna_taklif_given, FALSE) = FALSE",
        (user_id, now), fetch='one'
    )
    if row:
        _db_execute("UPDATE users SET obuna_taklif_given = TRUE WHERE user_id = %s", (user_id,))
        return True
    return False


def consume_access(user_id):
    """Video tahlil muvaffaqiyatli bo'lganda chaqiriladi.
    Obuna faol bo'lsa - hech narsa yechilmaydi (cheksiz).
    Aks holda 1 ta bepul tahlil yechiladi.
    Agar bu user referral orqali kelgan bo'lsa va hali bonus berilmagan bo'lsa,
    TAKLIF QILGAN odamga +1 bepul tahlil qo'shadi va uning ID sini qaytaradi (xabar berish uchun)."""
    if is_admin(user_id):
        return None
    if not sub_active(user_id):
        # Avval PREMIUM balansdan (sotuv bepul) - u to'liq premium edi
        if get_premium_balance(user_id) > 0:
            _db_execute("UPDATE users SET premium_balance = premium_balance - 1 WHERE user_id = %s AND premium_balance > 0", (user_id,))
        # Keyin JUMA balansidan yechamiz (u kuyadi, shuning uchun birinchi ishlatilsin)
        elif get_juma_balance(user_id) > 0:
            _db_execute("UPDATE users SET juma_balance = juma_balance - 1 WHERE user_id = %s AND juma_balance > 0", (user_id,))
        else:
            _db_execute("UPDATE users SET balance = balance - 1 WHERE user_id = %s AND balance > 0", (user_id,))
    # Referral mukofoti: do'st BIRINCHI tahlil qilganda, TAKLIF QILGAN odamga +1.
    # Lekin har taklif qiluvchi umrida FAQAT 1 MARTA bonus oladi (1 do'st uchun).
    row = _db_execute("SELECT referred_by, ref_credited FROM users WHERE user_id = %s", (user_id,), fetch='one')
    if row and row[0] and not row[1]:
        referrer = row[0]
        # Bu do'st bo'yicha boshqa hisoblanmasin
        _db_execute("UPDATE users SET ref_credited = TRUE WHERE user_id = %s", (user_id,))
        # Taklif qiluvchi avval bonus olmagan bo'lsagina beramiz (umrida 1 marta)
        rrow = _db_execute("SELECT ref_reward_given FROM users WHERE user_id = %s", (referrer,), fetch='one')
        already = bool(rrow and rrow[0])
        if not already:
            _db_execute("UPDATE users SET ref_reward_given = TRUE WHERE user_id = %s", (referrer,))
            add_balance(referrer, 1)
            return referrer
    return None


def save_analysis(user_id, username="", kind="video", file_id=None, foiz=0, qisqa=None, toliq=None, tokens=0, narx=0):
    """Tahlilni saqlaydi va yangi yozuv ID sini qaytaradi (To'liq tugmasi uchun)."""
    row = _db_execute(
        "INSERT INTO analyses (user_id, username, kind, file_id, foiz, qisqa, toliq, tokens, narx, created) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (user_id, username or "", kind, file_id, foiz, qisqa, toliq, tokens, narx,
         datetime.now().strftime("%Y-%m-%d %H:%M")),
        fetch='one'
    )
    return row[0] if row else None


def get_full_analysis(analysis_id):
    """To'liq tahlil matnini qaytaradi (tugma bosilganda)."""
    row = _db_execute("SELECT toliq FROM analyses WHERE id = %s", (analysis_id,), fetch='one')
    return row[0] if row and row[0] else None


def get_qisqa_analysis(analysis_id):
    """Qisqa tahlil matnini qaytaradi (ovozli eshitish uchun)."""
    row = _db_execute("SELECT qisqa FROM analyses WHERE id = %s", (analysis_id,), fetch='one')
    return row[0] if row and row[0] else None


def get_analysis_video(analysis_id):
    """Tahlilning video file_id, foiz va user ma'lumotini qaytaradi (admin nazorati uchun)."""
    row = _db_execute("SELECT file_id, foiz, user_id, username FROM analyses WHERE id = %s",
                      (analysis_id,), fetch='one')
    if not row:
        return None
    return {"file_id": row[0], "foiz": row[1], "user_id": row[2], "username": row[3]}


def get_last_video_foiz(user_id):
    """Foydalanuvchining eng oxirgi (avvalgi) video tahlili foizini qaytaradi."""
    row = _db_execute(
        "SELECT foiz FROM analyses WHERE user_id = %s AND kind = 'video' ORDER BY id DESC LIMIT 1",
        (user_id,), fetch='one')
    return row[0] if row and row[0] is not None else None


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


def get_all_videos(limit=100):
    """BARCHA tahlil qilingan videolar (admin uchun), eng yangisi birinchi."""
    rows = _db_execute(
        "SELECT id, user_id, username, foiz, file_id, created FROM analyses "
        "WHERE kind = 'video' AND file_id IS NOT NULL "
        "ORDER BY id DESC LIMIT %s",
        (limit,), fetch='all'
    )
    return rows or []


def get_today_all(limit=200):
    """BUGUNGI barcha tahlil qilingan videolar (eng yangisi birinchi)."""
    today = datetime.now().strftime("%Y-%m-%d")
    rows = _db_execute(
        "SELECT id, user_id, username, foiz, file_id, created FROM analyses "
        "WHERE kind = 'video' AND file_id IS NOT NULL AND created LIKE %s "
        "ORDER BY id DESC LIMIT %s",
        (today + "%", limit), fetch='all'
    )
    return rows or []


def create_payment(user_id, package, amount, status='approved'):
    row = _db_execute(
        "INSERT INTO payments (user_id, package, amount, status, created) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (user_id, package, amount, status, datetime.now().strftime("%Y-%m-%d %H:%M")),
        fetch='one'
    )
    return row[0] if row else None


def has_paid_ever(user_id):
    """Foydalanuvchi biror marta haqiqiy to'lov qilganmi? (obuna yoki 1 martalik)"""
    row = _db_execute(
        "SELECT 1 FROM payments WHERE user_id = %s AND status = 'approved' LIMIT 1",
        (user_id,), fetch='one'
    )
    return bool(row)


def get_stats():
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        total_users = (_db_execute("SELECT COUNT(*) FROM users", fetch='one') or [0])[0]
        total_analyses = (_db_execute("SELECT COUNT(*) FROM analyses", fetch='one') or [0])[0]
        today_analyses = (_db_execute("SELECT COUNT(*) FROM analyses WHERE created LIKE %s",
                                      (today + "%",), fetch='one') or [0])[0]
        revenue = (_db_execute("SELECT COALESCE(SUM(amount),0) FROM payments WHERE status = 'approved'",
                               fetch='one') or [0])[0]
        # Obunachilar (obunasi hozir faol)
        subs = (_db_execute("SELECT COUNT(*) FROM users WHERE sub_until IS NOT NULL AND sub_until > %s",
                            (now,), fetch='one') or [0])[0]
        # Bepul tahlili borlar (balansda > 0)
        free_bal = (_db_execute("SELECT COUNT(*) FROM users WHERE balance > 0", fetch='one') or [0])[0]
        # Aktiv (kamida 1 marta video tahlil qilgan)
        active = (_db_execute("SELECT COUNT(DISTINCT user_id) FROM analyses WHERE kind = 'video'",
                              fetch='one') or [0])[0]
        return total_users, total_analyses, today_analyses, revenue, subs, free_bal, active
    except Exception as e:
        logger.error(f"get_stats xato: {e}")
        return 0, 0, 0, 0, 0, 0, 0


PROMPT_UZ = """Sen tajribali, xolis Instagram kontent tahlilchisisan. Blogger videosini HALOL va OBJEKTIV bahola. Faqat HAQIQATNI ayt — yaxshi bo'lsa yaxshi, kuchsiz bo'lsa kuchsiz de.

OHANG: Sen ekspertsan, lekin sovuq emas — DO'STONA, ILIQ va RUHLANTIRUVCHI ohangda yoz. Kamchilikni ham aytsang, uni shunday ayt: blogger tushkunlikka tushmasin, balki "shuni tuzatsang — zo'r bo'ladi!" deb motivatsiya olsin. LEKIN ortiqcha maqtov (paxta) QILMA — yolg'on iltifot yomon. Halol ekspert bahosini ILIQ ohangda yetkaz: aniq, foydali, lekin odamni o'stiradigan, ruhini ko'taradigan tarzda. Tanqid ham bo'lsin, lekin "do'st maslahati" kabi — qo'pol emas, ilhomlantiruvchi.

Javobingni ANIQ shu formatda, shu teglar bilan ber (teglarni o'zgartirma, teglardan tashqarida hech narsa yozma):

[FOIZ]videoning rekkaga (rekomendatsiyaga) chiqish ehtimoli — faqat 0-100 oralig'idagi bitta son.
MUHIM: bu foizni quyidagi mezonlarni BIRGA hisoblab chiqar (faqat montaj/sifatga qarama):
- Hook kuchi (boshlanishi ushlab tursa) — 25%
- Niche va auditoriya kengligi (mavzu KENG ommaga qiziqmi yoki TOR doiraga? Tor niche bo'lsa foiz PAST bo'lsin, montaj zo'r bo'lsa ham) — 30%
- Saqlanish/ulashish/qayta ko'rish ehtimoli — 25%
- Texnik sifat (vizual, audio, montaj) — 20%
Tor niche (kam odamga qiziq) videolar montaji ideal bo'lsa ham 30-50% dan oshmasin. Keng ommaga mos, viral potensialli videolar yuqori foiz olsin. Bir xil video uchun har safar BIR XIL foiz chiqar (mezonlarga qat'iy amal qil).

ENG MUHIM QOIDA: foiz yuqoridagi bo'lim BALLARIGA MOS bo'lishi SHART. Agar Hook, Audio, Vizual yoki Montaj ballari PAST bo'lsa (masalan 5/10 dan past), rekka foizi ham PAST bo'lishi MAJBURIY — baland chiqmasin. Hira, sifatsiz, sustkash, e'tibor tortmaydigan videoga YUQORI foiz BERMA. Ballar o'rtachasi past bo'lsa, foiz ham past (masalan 20-40%) bo'lsin. Faqat HAQIQATAN sifatli VA keng ommabop video yuqori foiz (70%+) olsin. Past sifat = past foiz, bu qat'iy.[/FOIZ]

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
TO'LIQ, chuqur tahlil (har bo'lim bir necha jumla, ko'p emoji bilan). MUHIM: har bo'lim orasiga BITTA BO'SH QATOR qo'y, bo'limlar bir-biriga yopishmasin, o'qishga oson bo'lsin.
UZUNLIK CHEGARASI: butun to'liq tahlil JAMI 3500 belgidan OSHMASIN. Har bo'limni aniq, to'yimli, lekin ortiqcha cho'zmasdan yoz — qisqa va lo'nda bo'lsin.
JUDA MUHIM: har bo'limda BALL HAR DOIM ENG BOSHIDA, sarlavhadan keyin darrov tursin (izohdan OLDIN). Ballni jumlaning oxiriga QO'YMA. Format aniq shunday: "sarlavha — ⭐ _/10 — keyin batafsil izoh".
🎣 HOOK (0-3 sekund) — ⭐ _/10 — e'tiborni tortadimi? batafsil izoh
🎬 VIZUAL VA MONTAJ — ⭐ _/10 — 🎥 yoritish, kamera, montaj bo'yicha batafsil izoh
🗣️ AUDIO VA NUTQ — ⭐ _/10 — 🎙️ nima gapirildi, ovoz toni bo'yicha batafsil izoh
📝 KONTENT VA QIYMAT — ⭐ _/10 — 💬 xabar, CTA bo'yicha batafsil izoh
📊 REKKA CHIQISH EHTIMOLI — 🎯 _% va batafsil sabablar.
✅ KUCHLI TOMONLARI — faqat haqiqiy kuchli joylar.
❌ KAMCHILIKLAR — barcha jiddiy kamchiliklar, ochiq ayt.
💡 TAVSIYALAR — 🚀 5 ta aniq, amaliy qadam.
🇺🇿 O'ZBEK BOZORI MASLAHATI — bu video O'ZBEK Instagram auditoriyasi uchun qanchalik mos? O'zbekistonda qaysi vaqtda (masalan kechqurun 19:00-23:00) post qilish, mahalliy trendlar, o'zbek tomoshabin nimani yoqtirishi, qaysi hashtaglar va mahalliy kontekst bo'yicha 2-3 ta ANIQ maslahat ber.
[/TOLIQ]

Baho videoning haqiqiy sifatiga MOS bo'lsin. Foiz REAL bo'lsin. Sen oddiy AI emas, O'ZBEK Instagram bozorini chuqur biladigan ekspertsan — mahalliy, aniq, foydali maslahat ber. Halol baho bloggerni o'stiradi."""

PROMPT_RU = """Ты опытный, объективный аналитик Instagram-контента. Оцени видео блогера ЧЕСТНО. Говори только ПРАВДУ.

ТОН: Ты эксперт, но не холодный — пиши ДРУЖЕЛЮБНО, ТЕПЛО и ВДОХНОВЛЯЮЩЕ. Даже указывая на недостаток, подавай его так, чтобы блогер не падал духом, а получал мотивацию: "исправишь это — будет супер!". НО не льсти и не хвали попусту — фальшивые комплименты это плохо. Передавай честную экспертную оценку в ТЁПЛОМ тоне: точно, полезно, но так, чтобы человек рос и воодушевлялся. Критика нужна, но как "совет друга" — не грубо, а вдохновляюще.

Ответ дай СТРОГО в этом формате с этими тегами (не меняй теги, вне тегов ничего не пиши):

[FOIZ]вероятность попадания видео в рекомендации — только одно число 0-100.
ВАЖНО: считай этот процент по критериям ВМЕСТЕ (не только монтаж/качество):
- Сила хука — 25%
- Ниша и широта аудитории (тема интересна ШИРОКОЙ публике или УЗКОМУ кругу? Узкая ниша — процент НИЗКИЙ, даже если монтаж отличный) — 30%
- Вероятность сохранений/репостов/пересмотров — 25%
- Техническое качество (визуал, аудио, монтаж) — 20%
Видео узкой ниши не должны превышать 30-50%, даже при идеальном монтаже. Высокий процент — у видео с виральным потенциалом для широкой аудитории. Для одного и того же видео выдавай ОДИН И ТОТ ЖЕ процент.

ГЛАВНОЕ ПРАВИЛО: процент ОБЯЗАН соответствовать БАЛЛАМ разделов. Если баллы Хука, Аудио, Визуала или Монтажа НИЗКИЕ (например ниже 5/10), процент попадания в рекомендации тоже ОБЯЗАН быть НИЗКИМ. Не давай высокий процент тусклому, некачественному, вялому видео. Если средний балл низкий — процент тоже низкий (например 20-40%). Только ДЕЙСТВИТЕЛЬНО качественное И массовое видео получает высокий процент (70%+). Низкое качество = низкий процент, это строго.[/FOIZ]

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
ПОЛНЫЙ, глубокий анализ (каждый раздел в несколько предложений, с эмодзи). ВАЖНО: между разделами оставляй ОДНУ ПУСТУЮ СТРОКУ, чтобы разделы не слипались и легко читались.
ОГРАНИЧЕНИЕ ДЛИНЫ: весь полный анализ ВСЕГО не более 3500 символов. Каждый раздел пиши точно и ёмко, без лишней воды — коротко и по делу.
ОЧЕНЬ ВАЖНО: в каждом разделе БАЛЛ ВСЕГДА В САМОМ НАЧАЛЕ, сразу после заголовка (ПЕРЕД пояснением). НЕ ставь балл в конец предложения. Формат строго такой: "заголовок — ⭐ _/10 — затем подробное пояснение".
🎣 ХУК (0-3 сек) — ⭐ _/10 — цепляет? подробное пояснение
🎬 ВИЗУАЛ И МОНТАЖ — ⭐ _/10 — 🎥 свет, камера, монтаж, подробно
🗣️ АУДИО И РЕЧЬ — ⭐ _/10 — 🎙️ что сказано, тон, подробно
📝 КОНТЕНТ И ЦЕННОСТЬ — ⭐ _/10 — 💬 посыл, призыв, подробно
📊 ВЕРОЯТНОСТЬ В РЕКОМЕНДАЦИИ — 🎯 _% и причины.
✅ СИЛЬНЫЕ СТОРОНЫ — реальные плюсы.
❌ НЕДОСТАТКИ — все серьёзные минусы.
💡 РЕКОМЕНДАЦИИ — 🚀 5 конкретных шагов.
🇺🇿 СОВЕТ ДЛЯ УЗБЕКСКОГО РЫНКА — насколько видео подходит для УЗБЕКСКОЙ аудитории Instagram? Дай 2-3 конкретных совета: когда постить в Узбекистане (например вечером 19:00-23:00), местные тренды, что любит узбекский зритель, хештеги и местный контекст.
[/TOLIQ]

Оценка должна соответствовать реальному качеству. Процент реальный. Ты не обычный AI, а эксперт, глубоко знающий узбекский рынок Instagram — давай местные, конкретные, полезные советы."""


# ===== PREMIUM QO'SHIMCHA: hook ko'tarish (baho + muammo + yechim + tayyor variant) =====
PROMPT_PREMIUM_UZ = """

[PREMIUM_BONUS]
Bu PREMIUM foydalanuvchi. Yuqoridagi tahlilga QO'SHIMCHA, eng oxirida quyidagi maxsus bo'limni ham yoz (faqat to'liq tahlilda):

🔥 HOOKNI KUCHAYTIRISH (PREMIUM)
Videoning hozirgi hook'ini chuqur tahlil qil va shu 3 qismni ber:
1️⃣ MUAMMO: Hozirgi hook nega kuchsiz/o'rtacha — aniq sabab (1-2 jumla).
2️⃣ YECHIM: Hook'ni qanday kuchaytirish — amaliy maslahat (1-2 jumla).
3️⃣ TAYYOR VARIANTLAR: Shu video uchun 3 ta TAYYOR, kuchli hook matni yoz (foydalanuvchi to'g'ridan ko'chirib ishlatsa bo'ladigan, o'zbek auditoriyasiga mos, e'tibor tortadigan). Har birini alohida qatorda, "▪️" bilan boshlab yoz.

Bu bo'lim foydalanuvchiga ANIQ, ko'chirib ishlatса bo'ladigan qiymat bersin — umumiy gap emas, aniq tayyor matnlar."""

PROMPT_PREMIUM_RU = """

[PREMIUM_BONUS]
Это PREMIUM пользователь. В ДОПОЛНЕНИЕ к анализу выше, в самом конце добавь специальный раздел (только в полном анализе):

🔥 УСИЛЕНИЕ ХУКА (PREMIUM)
Глубоко проанализируй текущий хук видео и дай 3 части:
1️⃣ ПРОБЛЕМА: почему текущий хук слабый/средний — конкретная причина (1-2 предложения).
2️⃣ РЕШЕНИЕ: как усилить хук — практический совет (1-2 предложения).
3️⃣ ГОТОВЫЕ ВАРИАНТЫ: напиши 3 ГОТОВЫХ сильных текста хука для этого видео (которые можно скопировать и использовать). Каждый с новой строки, начиная с "▪️".

Дай конкретную ценность — не общие слова, а готовые тексты."""


PROMPT_PROFILE_UZ = """Sen Instagram bo'yicha tajribali, xolis ekspertsan. Senga foydalanuvchining Instagram profili va/yoki statistikasi (Insights) skrinshot(lar)i berildi.

AVVAL TEKSHIR: Agar skrinshot(lar)dan profil MAVZUSINI (nima haqida ekanini) aniq tushuna olmasang (rasm xira, kam ma'lumot, bio yo'q, postlar ko'rinmaydi) — boshqa hech narsa yozma, FAQAT shu so'zni yoz: [MAVZU_KERAK]
Agar mavzuni tushunsang — quyidagi to'liq tahlilni ber.

OHANG: Sen ekspertsan, lekin sovuq emas — DO'STONA, ILIQ va RUHLANTIRUVCHI yoz. Kamchilikni ham aytsang, blogger tushkunlikka tushmasin, "shuni tuzatsang — zo'r bo'ladi!" deb motivatsiya olsin. LEKIN ortiqcha maqtov (paxta) QILMA. Halol ekspert bahosini iliq ohangda yetkaz. KO'P EMOJI ishlat — tahlil jonli, qiziqarli, o'qishga yoqimli bo'lsin.

Tahlil formati (har bo'limga BALL ber, sarlavhadan keyin darrov):
📊 <b>UMUMIY TAASSUROT</b> — profil haqida qisqacha, jonli.
👤 <b>BIO VA PROFIL</b> — ⭐ _/10 — ✍️ ism, bio, profil rasmi, havola: nimasi yaxshi, nimasi kam.
🎯 <b>KONTENT</b> — ⭐ _/10 — 🎬 postlar mavzusi, sifati, izchilligi.
🎨 <b>VIZUAL USLUB</b> — ⭐ _/10 — 🌈 ranglar, estetika, umumiy ko'rinish.
📈 <b>FAOLLIK (ENGAGEMENT)</b> — ⭐ _/10 — 💬 layk, izoh, ko'rishlar bo'yicha (agar ko'rinsa).
✅ <b>KUCHLI TOMONLAR</b> — 💪 faqat real plyuslar.
❌ <b>KAMCHILIKLAR</b> — 🔧 jiddiy minuslar, ochiq lekin samimiy.
💡 <b>TAVSIYALAR</b> — 🚀 5 ta aniq, amaliy qadam.
🇺🇿 <b>O'ZBEK BOZORI</b> — 🎯 O'zbek auditoriyasi uchun 2-3 ta aniq maslahat.
🏆 <b>UMUMIY BAHO: _/100</b> — profilning umumiy kuchi va TOPga tayyorligi (motivatsion izoh bilan).

Faqat skrinshotda haqiqatan ko'ringan narsaga asoslan, ko'rinmaganini o'ylab topma."""


PROMPT_PROFILE_RU = """Ты опытный, объективный эксперт по Instagram. Тебе дали скриншот(ы) профиля и/или статистики (Insights) пользователя.

СНАЧАЛА ПРОВЕРЬ: Если по скриншоту(ам) не можешь точно понять ТЕМУ профиля (о чём он) — изображение размытое, мало данных, нет био, не видно постов — не пиши больше ничего, напиши ТОЛЬКО это слово: [MAVZU_KERAK]
Если тему понимаешь — дай полный анализ ниже.

ТОН: Ты эксперт, но не холодный — пиши ДРУЖЕЛЮБНО, ТЕПЛО и ВДОХНОВЛЯЮЩЕ. Даже указывая на недостаток, подавай так, чтобы блогер не падал духом: "исправишь это — будет супер!". НО не льсти попусту. Честную оценку передавай в тёплом тоне. Используй МНОГО ЭМОДЗИ — анализ живой, интересный, приятный для чтения.

Формат анализа (к каждому разделу дай БАЛЛ, сразу после заголовка):
📊 <b>ОБЩЕЕ ВПЕЧАТЛЕНИЕ</b> — кратко о профиле, живо.
👤 <b>БИО И ПРОФИЛЬ</b> — ⭐ _/10 — ✍️ имя, био, аватар, ссылка: что хорошо, чего не хватает.
🎯 <b>КОНТЕНТ</b> — ⭐ _/10 — 🎬 тема, качество, регулярность постов.
🎨 <b>ВИЗУАЛЬНЫЙ СТИЛЬ</b> — ⭐ _/10 — 🌈 цвета, эстетика, общий вид.
📈 <b>ВОВЛЕЧЁННОСТЬ</b> — ⭐ _/10 — 💬 лайки, комментарии, просмотры (если видно).
✅ <b>СИЛЬНЫЕ СТОРОНЫ</b> — 💪 только реальные плюсы.
❌ <b>НЕДОСТАТКИ</b> — 🔧 серьёзные минусы, честно но дружелюбно.
💡 <b>РЕКОМЕНДАЦИИ</b> — 🚀 5 конкретных шагов.
🇺🇿 <b>УЗБЕКСКИЙ РЫНОК</b> — 🎯 2-3 совета для узбекской аудитории.
🏆 <b>ОБЩАЯ ОЦЕНКА: _/100</b> — общая сила профиля и готовность к ТОПу (с мотивирующим комментарием).

Опирайся только на то, что реально видно на скриншоте, не выдумывай."""

TEXTS = {
    'uz': {
        'welcome': (
            "🩺 <b>INSTADOCTOR AI</b> tizimiga xush kelibsiz!\n\n"
            "Men sizning Reels/Shorts videolaringizni Instagram algoritmlari bo'yicha "
            "tahlil qilib, <b>TOPga chiqish ehtimolini</b> 📈 hisoblab beraman.\n\n"
            "🎁 <b>Ilk qadam uchun 1 ta BEPUL CHUQUR TAHLIL sovg'a!</b>\n\n"
            "👇 Hoziroq videongizni yuboring (2GB gacha)"
        ),
        'gift_new': ("🎁 SOVG'A! Sizga 1 ta BEPUL tahlil berildi!\n\n"
                     "🎬 Hoziroq videongizni yuboring va sun'iy intellekt tahlilini "
                     "BEPUL sinab ko'ring. Hech narsa to'lash shart emas! 👇"),
        'cmp_up': "📈 O'SISH! Oldingi videongiz {prev}% edi, bu safar {now}% — {d}% ga yaxshilandi! Zo'r ketyapsiz! 🔥",
        'cmp_down': "📉 Bu safar {now}% (oldingi videongiz {prev}% edi, {d}% ga pasaydi). Tavsiyalarga e'tibor bering! 💪",
        'cmp_same': "➡️ Bu video ham {now}% — oldingi darajada. Keyingi videoda yuqoriroq natijaga harakat qiling! 🚀",
        'menu_video': "🎬 Video tahlil",
        'aksiya_msg': ("📣 Algoritmlarni yangiladik va sizga yana 1 ta bepul tahlil sovg'a qilamiz!\n\n"
                       "Salom! Bitta video — bu shunchaki sinov. Haqiqiy natija va topga chiqish "
                       "masofada, xatolar ustida ishlaganda ko'rinadi.\n\n"
                       "Balansingizga yana 1 ta bepul imkoniyat qo'shdik 🎁\n\n"
                       "Oldingi tavsiyamizga qarab, videongizni xuki (boshlanishi) yoki yakunini "
                       "o'zgartirib, qaytadan yuklab ko'ring. Keling, videongizni ideal holatga keltiramiz! 🚀"),
        'obuna_taklif_msg': ("Sizning Reels'dagi salohiyatingiz juda katta! 🚀\n\n"
                             "Cheksiz video tahlil qilish, har bir hookni ideal holatga keltirish va "
                             "ko'rishlarda barqaror o'sish uchun — obunani faollashtiring.\n\n"
                             "Bu atigi oyiga 29 900 so'm — kuniga 1 000 so'mdan ham kam, "
                             "shaxsiy AI-prodyuseringiz uchun! 💎"),
        'obuna_taklif_btn': "💎 Obunani faollashtirish",
        'test_taklif_msg': ("🎉 <b>10 000+ blogger InstaDoctor'dan foydalanmoqda — navbat sizda!</b>\n\n"
                            "Hali to'liq obunaga shoshilmayapsizmi? Tushunamiz! 😊\n"
                            "Avval <b>kichik qadamdan</b> boshlang:\n\n"
                            "⚡️ <b>7 KUNLIK PREMIUM — atigi 6 990 so'm</b>\n"
                            "━━━━━━━━━━━━━\n"
                            "🔍 <b>CHUQURROQ TAHLIL</b> — eng aniq, professional baho\n"
                            "♾ <b>CHEKSIZ video</b> — 7 kun limitsiz\n"
                            "🎙 <b>OVOZLI MASLAHAT</b> + kuchli xeshteglar\n"
                            "📊 <b>PROFIL TAHLILI</b> — shaxsiy tavsiyalar\n"
                            "📈 <b>REK EHTIMOLI</b> — TOPga chiqish % larda\n"
                            "━━━━━━━━━━━━━\n"
                            "⏰ <b>DIQQAT: bu taklif faqat 24 SOAT amal qiladi!</b>\n"
                            "Keyin bu narx yo'qoladi — shoshiling! 🔥\n\n"
                            "Bir hafta sinab ko'ring — yoqsa, to'liq obunaga o'tasiz! 🚀"),
        'test_taklif_btn': "⚡ 7 kunlik Premium — faollashtirish",
        'sorov_msg': ("🆘 Yordamingiz kerak! Evaziga BONUS sovg'a qilamiz 🎁\n\n"
                      "🎉 Do'stlar, qisqa vaqt ichida botimizdan foydalanuvchilar soni {n} tadan oshdi!\n\n"
                      "Biz sizga yanada ko'proq foyda keltirishni va videolaringizni REKga "
                      "chiqishiga yordam berishni xohlaymiz 🚀\n\n"
                      "Buning uchun fikringiz juda muhim 🙏 Bor-yo'g'i 2 ta savol.\n"
                      "Javob bergan har kimga 🎁 +1 BEPUL tahlil!\n\n"
                      "Boshlash uchun pastdagi tugmani bosing 👇"),
        'sorov_q1_ask': ("1️⃣ Botimizda sizga aynan nima yoqdi?\n"
                         "(Masalan: tushuntirishlar, tezlik, aniqlik...) ✨\n\n"
                         "Javobingizni yozib qoldiring 👇"),
        'sorov_q2': ("Rahmat! 🙏 Endi 2-savol:\n\n"
                     "2️⃣ Botda qanday kamchiliklar bor yoki nimani o'zgartirish, "
                     "qo'shishni maslahat berasiz? 💡\n\n"
                     "Javobingizni yozib qoldiring 👇"),
        'sorov_start_btn': "✍️ Javob berish",
        'test_sorov_msg': ("📋 Sizning fikringiz biz uchun muhim! 🙏\n\n"
                           "Yangi g'oyamiz bor, fikringizni bilmoqchimiz:\n\n"
                           "💡 <b>7 KUNLIK TEST PREMIUM</b> ni atigi <b>6 990 so'mga</b> qo'shmoqchimiz!\n\n"
                           "Premium imkoniyatlari:\n"
                           "🔍 <b>2 barobar kuchli tahlil</b>\n"
                           "♾ <b>Cheksiz tahlil</b> imkoniyati\n"
                           "🎙 <b>Ovozli maslahatlar</b>\n"
                           "🔥 <b>Eng kuchli xeshteglar</b>\n"
                           "📈 <b>Yashirin REK ehtimoli</b>\n\n"
                           "2 ta savolga javob bering 👇"),
        'test_sorov_btn': "✍️ Fikr bildirish",
        'test_sorov_q1': ("1️⃣ <b>7 kunlik test Premium (atigi 6 990 so'm) qo'shmoqchimiz</b> — "
                          "sizga qiziqmi, sinab ko'rarmidingiz?\n\nFikringizni yozing 👇"),
        'test_sorov_q2': ("Rahmat! 🙏 Endi 2-savol:\n\n"
                          "2️⃣ Yaqinda botni yangiladik — <b>tezlik va sifatni oshirdik</b>. "
                          "O'zgarishlar yoqdimi? 👇"),
        'test_sorov_done': "✅ Rahmat! Fikringiz biz uchun juda muhim 🙏",
        'sorov_thanks_reward': "Katta rahmat fikringiz uchun! ❤️🎁 Balansingizga +1 ta BEPUL tahlil qo'shdik. Video yuboring! 🎬",
        'sorov_thanks': "Katta rahmat fikringiz uchun! ❤️🙏",
        'menu_balance': "💰 Balansim",
        'menu_lang': "🌐 Til",
        'menu_status': "🏆 Statusim/Balansim",
        'menu_arxiv': "📂 Shaxsiy hisobotim",
        'menu_help': "ℹ️ Yordam",
        'menu_fikr': "💬 Fikr va takliflar",
        'menu_premium': "💎 Premiumga o'tish",
        'eslatma_uz': ("🛠 <b>Hurmatli foydalanuvchi!</b>\n\n"
                       "So'nggi kunlarda InstaDoctor'ni yanada <b>tezroq va kuchliroq</b> qilish uchun yangiladik. ⚡\n\n"
                       "Yangilanish paytida ba'zi videolar tahlil qilinmagan bo'lishi mumkin — buning uchun <b>uzr so'raymiz</b> 🙏\n\n"
                       "✅ <b>Hozir bot to'liq ishlayapti!</b>\n\n"
                       "Agar videongiz javob olmagan bo'lsa — iltimos, <b>qaytadan yuboring</b>. Endi hammasi tez va aniq ishlaydi! 🎯\n\n"
                       "Tushunganingiz uchun rahmat! ❤️\n\n"
                       "📩 Savol yoki muammo bo'lsa: @Nurislom_admin"),
        'eslatma_ru': ("🛠 <b>Уважаемый пользователь!</b>\n\n"
                       "В последние дни мы обновили InstaDoctor, чтобы он стал <b>быстрее и мощнее</b>. ⚡\n\n"
                       "Во время обновления некоторые видео могли не обработаться — <b>приносим извинения</b> 🙏\n\n"
                       "✅ <b>Сейчас бот работает полностью!</b>\n\n"
                       "Если ваше видео осталось без ответа — пожалуйста, <b>отправьте его заново</b>. Теперь всё работает быстро и точно! 🎯\n\n"
                       "Спасибо за понимание! ❤️\n\n"
                       "📩 Вопросы или проблемы: @Nurislom_admin"),
        'eslatma_ru_btn': "🇷🇺 Русский",
        'eslatma_uz_btn': "🇺🇿 O'zbekcha",
        'yangilik_msg': ("🎉 <b>InstaDoctor KATTA YANGILANDI!</b>\n\n"
                         "Bot endi ancha <b>kuchli va barqaror</b> — videolaringiz "
                         "<b>uzilishlarsiz, ishonchli</b> tahlil qilinadi! ✨\n\n"
                         "━━━━━━━━━━━━━\n\n"
                         "💳 <b>To'lov endi yanada oson!</b>\n\n"
                         "Payme ishlamasa ham xavotir olmang — endi <b>karta orqali</b> "
                         "ham to'lashingiz mumkin:\n\n"
                         "1️⃣ Quyidagi <b>kartaga to'lang</b>\n"
                         "2️⃣ <b>Chek (skrinshot)ni shu botga yuboring</b>\n"
                         "3️⃣ Premium <b>tez orada faollashtiriladi!</b> ✅\n\n"
                         "💳 Karta: <code>6262 7300 6521 3151</code>\n"
                         "👤 <b>Boqijonov Nurislom</b>\n\n"
                         "━━━━━━━━━━━━━\n\n"
                         "✨ <b>Yangiliklar:</b>\n\n"
                         "📊 <b>Profil tahlili</b> — ballar va shaxsiy tavsiyalar\n"
                         "🎯 <b>Aniqroq tahlil</b> — natijalar yanada ishonchli\n"
                         "🔍 <b>Chuqurroq tahlil</b> — Premium'da batafsil, professional\n"
                         "🎤 <b>Ovozli tahlil</b> — tahlilni eshiting\n\n"
                         "━━━━━━━━━━━━━\n\n"
                         "👇 <b>Yangilangan botni ochish uchun tugmani bosing:</b>\n\n"
                         "💬 Yordam kerakmi? @Nurislom_admin"),
        'yangilik_btn': "🚀 Botni ishga tushirish",
        'video_intro': ("🔥 Bu loyiha ortida aslida kimlar turganini bilmoqchimisiz? 😊\n\n"
                        "Quyidagi videoni albatta ko'ring 👇"),
        'video_caption': ("🎁 <b>Chin dildan kichik bir sovg'a:</b>\n\n"
                          "Videolaringiz doim TOPda yurishini xohlaganimiz uchun, hech qanday "
                          "shartlarsiz sizga yana <b>1 TA BEPUL CHUQUR TAHLIL</b> hadya qilamiz. 🤍\n\n"
                          "👇"),
        'video_2btn_sovga': "🎁 SOVG'ANI OLISH",
        'video_2btn_fikr': "💬 Fikr bildirish",
        'tarif7_2soat': ("⏰ <b>DIQQAT! Atigi 2 SOAT qoldi!</b>\n\n"
                         "🎯 <b>7 KUNLIK PREMIUM — atigi 6 990 so'm</b> aksiyasi "
                         "bugun soat 22:00 da tugaydi!\n\n"
                         "Bu narx boshqa bo'lmaydi — ulgurib qoling! 🔥\n\n"
                         "👇 Hoziroq oling:"),
        'tarif7_2soat_btn': "⚡️ 7 kunlik Premium olish (6 990)",
        'drip2_empatiya': ("🤍 <b>Sizni eslab qoldik!</b>\n\n"
                           "Kontent yaratish — oson ish emas, buni bilamiz. Shuning uchun "
                           "biz har bir blogerga yordam berishni xohlaymiz.\n\n"
                           "🎁 Sizga <b>yana 1 ta BEPUL tahlil</b> sovg'a qildik!\n\n"
                           "Quyidagi videoda — loyiha ortida turgan jonli jamoa bilan tanishing 👇"),
        'video_fikr_btn': "💬 Fikringizni bildirish",
        'premium_fikr_btn': "💬 Fikr qoldirish",
        'premium_fikr_msg': ("Assalomu alaykum! 😊\n\n"
                             "Siz InstaDoctor Premium'dan foydalanyapsiz — bu biz uchun katta "
                             "mas'uliyat va quvonch! 🤍\n\n"
                             "Xizmatimizni yanada yaxshilash uchun sizning samimiy fikringiz juda "
                             "muhim. Bir necha soniya vaqt ajratsangiz:\n\n"
                             "✨ Premium'da sizga eng ko'p nima yoqdi?\n"
                             "📈 Kontentingizga qanday yordam berdi?\n"
                             "💡 Nimani yaxshilashimizni xohlaysiz?\n\n"
                             "👇 Tugmani bosib, fikringizni yozib qoldiring — har bir fikringiz "
                             "biz uchun qadrli! 🙏"),
        'premium_fikr_ask': ("💬 Fikringizni yozing — Premium'da nima yoqdi, qanday yordam berdi, "
                             "nimani yaxshilash kerak?\n\nHar bir fikringiz biz uchun qadrli! 🤍"),
        'premium_fikr_thanks': ("🤍 <b>Fikringiz uchun katta rahmat!</b>\n\n"
                                "Har bir fikr xizmatimizni yaxshiroq qiladi. Sizdek faol "
                                "foydalanuvchilarimiz borligi — biz uchun eng katta baxt! 🙏"),
        'video_fikr_ask': ("💬 Video yoki bot haqida fikringizni yozing.\n\n"
                           "Fikringiz biz uchun juda muhim — botni yaxshilashga yordam beradi! 🤍"),
        'video_fikr_thanks': ("🎁 <b>Чин дилдан кичик бир совға:</b>\n\n"
                              "Видеоларингиз доим ТОПда юришини хоҳлаганимиз учун, ҳеч қандай "
                              "шартларсиз сизга яна <b>1 ТА БЕПУЛ ЧУҚУР ТАҲЛИЛ</b> ҳадя қиламиз.\n\n"
                              "👇 Тугмани босинг:"),
        'video_sovga_btn': "🎁 СОВҒАНИ ОЛИШ ВА ТАҲЛИЛ ҚИЛИШ",
        'video_sovga_olindi': ("🎉 <b>Sovg'a qo'lingizda!</b>\n\n"
                               "Sizga <b>1 ta BEPUL tahlil</b> qo'shildi! 🎁\n"
                               "Endi videongizni yuboring — tahlil qilamiz! 🚀"),
        'video_fikr_already': ("😊 Siz allaqachon fikr bildirib, bepul tahlilingizni olgansiz. "
                               "Rahmat! Yana foydalanmoqchi bo'lsangiz — Premium oling. 💎"),
        'juma_boshlandi': ("🤍 <b>Hafta davomida mehnat qildingiz...</b>\n\n"
                           "Bilamiz — kontent yaratish oson emas. Har bir video ortida "
                           "sizning kuchingiz, vaqtingiz va orzularingiz turibdi. Biz buni "
                           "ko'ramiz va qadrlaymiz. 🙏\n\n"
                           "🎁 <b>Shuning uchun bugun — JUMA SOVG'ASI:</b>\n"
                           "Sizga <b>1 ta BEPUL tahlil</b>! Sizni qo'llab-quvvatlaymiz. 💪\n\n"
                           "⏰ <i>Faqat bugun (juma) amal qiladi — ertaga yo'qoladi.</i>\n\n"
                           "━━━━━━━━━━━━━\n"
                           "💡 <b>Bugungi maslahat:</b>\n{maslahat}\n"
                           "━━━━━━━━━━━━━\n\n"
                           "👇 Hoziroq videongizni yuboring — biz yordam beramiz! 🎬"),
        'juma_eslatma': ("⏰ <b>JUMA BEPUL TAHLILINGIZ YO'QOLMOQDA!</b>\n\n"
                         "Bugungi bepul tahlilingizni hali ishlatmadingiz. "
                         "Yarim tundan keyin u <b>yo'qoladi</b>! 😱\n\n"
                         "👇 Hoziroq video yuboring — bepul tahlil qiling!"),
        'tarif7_msg': ("🎯 <b>MAXSUS TAKLIF — 7 KUNLIK PREMIUM!</b>\n\n"
                       "Atigi <b>6 990 so'm</b>ga 7 kun davomida to'liq Premium'dan foydalaning! 🔥\n\n"
                       "✨ <b>Sizga ochiladi:</b>\n"
                       "♾ Cheksiz video tahlil\n"
                       "🔍 Chuqur, professional tahlil\n"
                       "🎤 Ovozli tahlil\n"
                       "📊 Profil tahlili\n"
                       "🔥 Heshteg tavsiyalari\n"
                       "⚡️ Navbatsiz, tezkor xizmat\n\n"
                       "💡 <b>Bir hafta sinab ko'ring</b> — natijani his qiling, keyin qaror qiling!\n\n"
                       "👇 Hoziroq boshlang:"),
        'tarif7_btn_payme': "⚡️ Payme orqali to'lash (6 990)",
        'tarif7_btn_card': "💳 Karta orqali to'lash (6 990)",
        'sotuv_msg': ("Bilasizmi, nega ba'zi bloggerlar doimo TOPda? 🤔\n\n"
                      "Chunki ular har bir videoni joylashdan oldin kamchiliklarini to'g'rilashadi. "
                      "Lekin algoritmlar to'xtab turmaydi — har kuni tahlil qilish va trendda bo'lish kerak! 📊\n\n"
                      "💎 PREMIUM tarifda nimalarga ega bo'lasiz?\n\n"
                      "⚡ <b>Maksimal tezlik</b> — navbatsiz, soniyalarda tahlil\n"
                      "♾ <b>Cheksiz tahlil</b> — kuniga xohlagancha video\n"
                      "🗣 <b>Audio</b> — tahlilni ovozli eshitish\n"
                      "🎯 <b>Hook tahlili</b> — soniyama-soniya + 3 ta tayyor variant\n"
                      "📈 <b>Yashirin trendlar</b> — algoritm yangiliklari birinchi sizga\n\n"
                      "💰 Narx: <b>29 900 so'm/oy</b>\n"
                      "(Kuniga atigi 1000 so'm — bir choydan ham arzon! ☕️)\n\n"
                      "Bitta REKka chiqqan video bu pulni qoplaydi! 🚀\n\n"
                      "👇 Hoziroq faollashtiring — kontentingizni TOPga chiqaring!"),
        'fikr_ask': ("💬 Fikr yoki taklifingizni yozib qoldiring 👇\n"
                     "Biz uchun har bir fikr muhim! 🙏\n\n"
                     "📩 Yoki to'g'ridan-to'g'ri yozishingiz mumkin: @Nurislom_admin"),
        'fikr_thanks': "Rahmat fikringiz uchun! ❤️🙏 Biz uni albatta ko'rib chiqamiz.",
        'menu_profile': "📊 Profil tahlili",
        'profile_instr': ("📊 <b>PROFIL TAHLILI</b>\n\n"
                          "Instagram profilingizni ekspert darajasida tahlil qilaman — "
                          "kuchli/kuchsiz tomonlari, kontent strategiyasi va aniq tavsiyalar.\n\n"
                          "📸 <b>Qaysi skrinshotlarni yuborasiz:</b>\n\n"
                          "<b>1️⃣ Profil bosh sahifasi</b> (eng muhim!)\n"
                          "Instagram'da profilingizni oching → ekran skrinshotini oling. "
                          "Unda <b>bio, ism, follower soni va birinchi postlar</b> ko'rinsin.\n\n"
                          "<b>2️⃣ Postlar to'ri (gallery)</b>\n"
                          "Pastga tushib, postlaringiz to'rini (9-12 ta post) skrin oling — "
                          "uslubingiz va mavzuingiz ko'rinsin.\n\n"
                          "<b>3️⃣ Statistika (ixtiyoriy, lekin foydali)</b>\n"
                          "Agar biznes akkaunt bo'lsa: <b>Insights / Statistika</b> sahifasini oching "
                          "(qamrov, erishish, eng yaxshi vaqtlar) → skrin oling.\n\n"
                          "✅ Skrinshot(lar)ni shu yerga yuboring → '✅ Tahlil qilish' tugmasini bosing.\n\n"
                          "💡 Qancha aniq skrinshot — shuncha kuchli tahlil!\n\n"
                          "Boshlash uchun skrinshot yuboring 👇"),
        'profile_got': "✅ Rasm qabul qilindi ({n} ta).\n\nYana yuboring yoki tahlilni boshlang 👇",
        'profile_btn': "✅ Tahlil qilish",
        'profile_none': "❌ Avval profil skrinshotini yuboring.",
        'profile_mavzu_ask': ("🤔 Skrinshotdan profil mavzusini aniq tushunolmadim.\n\n"
                              "Iltimos, profilingiz qaysi mavzuda ekanini qisqacha yozing "
                              "(masalan: fitnes, oshxona, biznes, go'zallik) 👇"),
        'profile_analyzing': "🧠 Profil tahlil qilinmoqda... ⚡",
        'full_btn': "📖 To'liq tahlilni ko'rish",
        'qisqa_btn': "🔙 Qisqa tahlilga qaytish",
        'maxsus_2700_msg': ("🎯 <b>Siz — bizning faol foydalanuvchimizsiz!</b>\n\n"
                            "InstaDoctor'ni bir necha marta ishlatdingiz — demak kontentingizni "
                            "jiddiy rivojlantiryapsiz 👏\n\n"
                            "Aynan shunday izlanuvchilar uchun <b>maxsus taklif</b> tayyorladik 👇\n\n"
                            "💎 <b>1 oylik Premium — 19,900 so'm</b> (29,900 o'rniga)\n\n"
                            "Premium'da sizni kutadi:\n"
                            "✅ <b>Cheksiz</b> tahlil\n"
                            "🔥 <b>\"Qanday yaxshilash?\"</b> — hook + 3 ta tayyor variant\n"
                            "🎙 <b>Ovozli</b> maslahat\n"
                            "📊 <b>Profil tahlili</b> — shaxsiy strategiya\n\n"
                            "⏰ Bu narx faqat <b>bugun 22:00 gacha</b>!\n\n"
                            "👇 Premium oling va kontentingizni TOPga chiqaring!"),
        # ===== SOTUV VORONKASI XABARLARI =====
        'sotuv1_msg': ("Botni 5 martadan ko'p ishlatibsiz, demak InstaDoctor sizga foyda beryapti, "
                       "bundan juda xursandman! 🤩\n\n"
                       "Lekin bir narsa meni o'ylantirib qo'ydi: nega Premium tarifga o'tmadingiz? 🤔 "
                       "Balki narxidadir, funksiya yetishmayotgandir yoki shunchaki ishonch kamdir?\n\n"
                       "Rostini aytsangiz, juda minnatdor bo'lardim. Javob yozgan har bir kishiga "
                       "Premium tahlildan BEPUL foydalanish imkoniyatini sovg'a qilaman! 🎁"),
        'sotuv1b_msg': ("Siz botning eng faol foydalanuvchisisiz — <b>top 1%</b> 🏆\n\n"
                        "Shuning uchun faqat sizga maxsus narx:\n"
                        "💎 Premium 1 oy: <s>29,900</s> → <b>19,900 so'm</b>\n\n"
                        "⏳ Faqat bugun! Ertaga 29,900 ga qaytadi.\n\n"
                        "👇 Premium oling va cheksiz tahlildan foydalaning!"),
        'sotuv3_msg': ("Siz botni sinab ko'rdingiz — lekin eng kuchli qismini hali ko'rmadingiz 👀\n\n"
                       "🔥 Premium videongiz <b>hook'ini</b> soniyama-soniya ochib beradi va "
                       "<b>3 ta tayyor yaxshi variant</b> yozadi.\n\n"
                       "🎁 Pastdagi tugmani bosing — sizga <b>1 ta BEPUL PREMIUM tahlil</b> beramiz, "
                       "farqni o'zingiz ko'rasiz! 👇"),
        'sotuv4_msg': ("Bitta video tashlab ketdingiz — keyin qaytmadingiz 😔\n\n"
                       "Balki birinchi tahlil sizni ishontirmagandir. Endi bot ancha kuchli: "
                       "hook'ni soniyama-soniya, strukturani chuqur tahlil qiladi.\n\n"
                       "🎁 Sizga <b>1 ta BEPUL PREMIUM tahlil</b> sovg'a qildik — pastdagi tugmani "
                       "bosing va videongizni yuboring, farqni ko'ring 🎬"),
        'sotuv5_msg': ("Botga kirdingiz, lekin bitta ham video tashlamadingiz 🎬\n\n"
                       "10 soniya — bitta Reels tashlang, professional tahlil oling: "
                       "hook ishlaydimi, qayerda odamlar chiqib ketadi, qanday tuzatish.\n\n"
                       "Mutlaqo <b>bepul</b>. Hozir sinab ko'ring 👇"),
        'tts_btn': "🔊 Qisqa eshitish",
        'yaxshilash_btn': "🔥 Qanday yaxshilash?",
        'yaxshilash_premium': ("🔒 <b>\"Qanday yaxshilash?\"</b> — bu PREMIUM funksiya!\n\n"
                               "Premium'da videongiz uchun:\n"
                               "✅ Hook nega kuchsiz — aniq sabab\n"
                               "✅ Qanday kuchaytirish — amaliy yechim\n"
                               "✅ <b>3 ta TAYYOR hook</b> — ko'chirib ishlatasiz!\n\n"
                               "👇 Premium oling va kontentingizni TOPga chiqaring:"),
        'yaxshilash_loading': "🔥 Hook tahlili tayyorlanmoqda... ⏳",
        'tts_full_btn': "🔊 Ovozli eshitish",
        'inv_sub_title': "InstaDoctor — 1 oylik obuna",
        'inv_sub_desc': "1 oy davomida cheksiz video tahlil. 🔒 To'lov Payme orqali xavfsiz. To'lovdan so'ng obuna avtomatik faollashadi.",
        'inv_one_title': "InstaDoctor — 1 ta tahlil",
        'inv_one_desc': "1 ta video tahlil. 🔒 To'lov Payme orqali xavfsiz. To'lovdan so'ng avtomatik qo'shiladi.",
        'pay_safety': ("🔒 XAVFSIZ TO'LOV\n\n"
                       "💳 To'lov Payme orqali amalga oshiriladi.\n"
                       "✅ Karta ma'lumotlaringiz botda saqlanmaydi — to'g'ridan-to'g'ri Payme himoyasida.\n"
                       "⚡ To'lovdan so'ng xizmat avtomatik faollashadi.\n\n"
                       "Quyidagi to'lov oynasi orqali davom eting 👇"),
        'pay_unavailable': "⚠️ To'lov tizimi hozircha mavjud emas. Birozdan so'ng urinib ko'ring yoki admin bilan bog'laning.",
        'pay_ok_sub': "✅ To'lov qabul qilindi! Obunangiz faollashtirildi — {until} gacha cheksiz tahlil. Rahmat! 🎉",
        'pay_ok_one': "✅ To'lov qabul qilindi! Sizga +1 tahlil qo'shildi. Endi video yuboring! 🎉",
        'celebrate_sub': ("🎉🎊 <b>TABRIKLAYMIZ!</b> 🎊🎉\n\n"
                          "💎 Siz endi <b>PREMIUM</b> a'zosiz!\n\n"
                          "✨ Sizga ochildi:\n"
                          "♾ Cheksiz tahlil\n"
                          "🔍 Chuqurroq, professional tahlil\n"
                          "🗣 Ovozli tahlil\n"
                          "🔥 Heshteg tavsiyalari\n"
                          "⚡ Navbatsiz, tez xizmat\n\n"
                          "📈 Obunangiz: <b>{until}</b> gacha\n\n"
                          "Endi videolaringizni yuboring — eng yaxshi natijani oling! 🚀"),
        'celebrate_test': ("🎉🎊 <b>TABRIKLAYMIZ!</b> 🎊🎉\n\n"
                           "💎 7 kunlik <b>PREMIUM test</b> faollashtirildi!\n\n"
                           "✨ Sizga ochildi:\n"
                           "♾ Cheksiz tahlil\n"
                           "🔍 Chuqurroq tahlil\n"
                           "🗣 Ovozli tahlil\n"
                           "⚡ Navbatsiz xizmat\n\n"
                           "📈 Muddat: <b>{until}</b> gacha\n\n"
                           "Sinab ko'ring — yoqsa, to'liq obunaga o'ting! 🚀"),
        'celebrate_one': ("🎉 <b>TO'LOV QABUL QILINDI!</b> 🎉\n\n"
                          "✅ Sizga <b>+1 tahlil</b> qo'shildi!\n\n"
                          "Endi videongizni yuboring — professional tahlil oling! 🚀"),
        'analyzed_footer': "\n\n━━━━━━━━━━\n🩺 Analizni AI ekspert InstaDoctor bajardi\n👉 @Instadoctorai_bot",
        'tts_loading': "🔊 Ovoz tayyorlanmoqda... ⏳",
        'tts_premium': ("🔒 Bu funksiya faqat PREMIUM obunachilar uchun!\n\n"
                        "🔊 Audio (ovozli tahlil) — obuna afzalligi.\n"
                        "💎 Obuna oling va barcha tahlillarni ovozli eshiting (va cheksiz tahlil qiling)!"),
        'tts_fail': "😔 Ovozni tayyorlab bo'lmadi. Keyinroq urinib ko'ring.",
        'full_gone': "❌ To'liq tahlil topilmadi (eski bo'lishi mumkin).",
        'profil_info': "📊 Profil tahlili hozircha ishlamayapti — tez orada qo'shiladi! 🔜\n\nHozircha 🎬 Video tahlil xizmatidan foydalanishingiz mumkin.",
        'profile_premium': ("🔒 <b>Profil tahlili — faqat PREMIUM uchun!</b>\n\n"
                            "Premium bilan butun Instagram profilingizni chuqur tahlil qilamiz:\n"
                            "👤 Bio va profil — nimasi yaxshi, nimasi kam\n"
                            "🎨 Umumiy uslub va kontent\n"
                            "📈 TOPga chiqish uchun aniq strategiya\n"
                            "🇺🇿 O'zbek bozori uchun maslahatlar\n\n"
                            "👇 Premiumga o'ting va profilingizni yangi bosqichga olib chiqing!"),
        'help_text': ("ℹ️ INSTADOKTOR — Yordam\n\n"
                      "🎬 Video tahlil — videongizni yuboring, men uni to'liq tahlil qilaman: "
                      "hook, vizual, audio, montaj va rekka chiqish ehtimoli.\n\n"
                      "💰 Balansim — qancha tahlil qolganini ko'rish.\n\n"
                      "🌐 Til — tilni o'zgartirish.\n\n"
                      "📏 Video 2GB dan kichik bo'lsin.\n\n"
                      "📩 Kamchiliklar yoki takliflar bo'yicha: @Nurislom_admin"),
        'lang_changed': "✅ Til o'zgartirildi!",
        'received': "⏳ Video qabul qilindi! Tahlil boshlanmoqda... ⚡",
        'free_wait_promo': ("⏳ Bepul tahlil navbati... {sec} soniya kuting.\n\n"
                            "💎 Premium bilan — NAVBATSIZ, soniyalarda tahlil! ⚡\n"
                            "Instagram algoritmlari kutib turmaydi! 🚀"),
        'analysis_timeout': ("⏳ Kechirasiz, tahlil biroz cho'zilib ketdi. Iltimos, videoni "
                             "qayta yuboring — bu safar tezroq bo'ladi! 🔄\n\n"
                             "(Balansingiz kamaymadi)"),
        'queued': "⏳ Hozir navbat bandroq, videongiz navbatga qo'yildi. Bir oz kuting... 🕐",
        'queued_promo': ("⏳ Ko'p kutishdan charchadingizmi?\n\n"
                         "Instagram algoritmlari KUTIB TURMAYDI! ⚡ Har soniya muhim.\n\n"
                         "💎 PREMIUM bilan nimalarga ega bo'lasiz:\n"
                         "⚡ Navbatsiz — soniyalarda tahlil\n"
                         "♾ Cheksiz video tahlil\n"
                         "🗣 Ovozli tahlil — maslahatlarni eshitasiz\n"
                         "🔥 Eng kuchli heshteglar va trendlar\n"
                         "📈 Yashirin REK ehtimoli (% larda)\n\n"
                         "👇 Hoziroq obunani faollashtiring"),
        'too_big': "❌ Video juda katta (2GB dan oshmasligi kerak). 📏\n\nIltimos, qisqaroq yuboring.",
        'wrong_format': "❌ Video formatini tanimadim. MP4 yoki MOV yuboring. 📹",
        'uploading': "📤 Video yuklanmoqda...",
        'analyzing': "🧠 InstaDoctor tahlil qilmoqda... ⚡",
        'time_upsell': ("💎 Bu — qisqacha bepul tahlil edi.\n\n"
                        "Premium bilan SIZ quyidagilarni olasiz:\n"
                        "🔍 Yanada CHUQURROQ va batafsil tahlil\n"
                        "🗣 Ovozli javob — maslahatlarni eshitasiz\n"
                        "🔥 Eng kuchli heshteglar va yashirin trendlar\n"
                        "♾ Cheksiz tahlil — har kuni xohlagancha\n\n"
                        "👇 Premiumga o'ting — to'liq imkoniyatdan foydalaning!"),
        'analyzing_live': "🧠 InstaDoctor AI videongizni tahlil qilmoqda",
        'ready': "✅ Tahlil tayyor!",
        'error': "😔 Kechirasiz, tahlil qilib bo'lmadi. Iltimos, videoni qayta yuboring. 🔄",
        'busy_quota': ("⏳ Hozir tizimda yuklama juda yuqori. Iltimos, biroz (5-10 daqiqa) "
                       "kutib, qaytadan urinib ko'ring. Balansingiz yechilmadi. 🙏"),
        'send_video': "🎬 Tahlil uchun videoni yuboring! 📤",
        'no_balance': ("🎬 <b>Bepul tahlillaringiz tugadi!</b>\n\n"
                       "Lekin siz endigina boshladingiz 😊 Premium'da sizni nimalar kutyapti:\n\n"
                       "✅ <b>Cheksiz</b> tahlil — limitsiz\n"
                       "🔥 <b>\"Qanday yaxshilash?\"</b> — hook + 3 ta tayyor variant\n"
                       "🎙 <b>Ovozli</b> maslahat\n"
                       "📊 <b>Profil tahlili</b> — shaxsiy strategiya\n"
                       "⚡️ <b>Kuchli model</b> — eng aniq baho\n"
                       "━━━━━━━━━━━━━\n"
                       "💎 <b>1 oylik — 29,900 so'm</b> (cheksiz)\n"
                       "📍 <b>1 ta tahlil — 5,090 so'm</b>\n\n"
                       "👇 Tanlang va kontentingizni TOPga chiqaring!"),
        'balance_info': "💰 Sizda {n} ta bepul tahlil bor.",
        'choose_pkg': "💳 Obuna:",
        'pay_instr': ("💳 1 OYLIK OBUNA — cheksiz video tahlil (30 kun)\n\n"
                      "💰 Summa: {amount:,} so'm\n"
                      "💳 Karta: {card}\n"
                      "👤 Ega: {name}\n\n"
                      "✅ To'lagach, CHEK SKRINSHOTINI shu yerga yuboring.\n"
                      "Admin tekshirib, obunangizni faollashtiradi. ⏳"),
        'receipt_sent': "✅ Chekingiz adminga yuborildi. Tez orada tasdiqlanadi! ⏳",
        'too_big_free': ("🎬 Bu video biroz uzun/katta ekan.\n\n"
                         "💎 <b>Premium</b> oling — <b>uzunroq va kattaroq videolarni</b> "
                         "bemalol tahlil qiling!\n\n"
                         "Yoki hozircha biroz qisqaroq video yuboring. 😊"),
        'too_big_paid': ("🎬 Bu video juda katta ekan.\n\n"
                         "Iltimos, biroz qisqaroq video yuboring. 😊"),
        'card_pay_instr': ("💳 <b>KARTA ORQALI TO'LOV</b>\n\n"
                           "📦 Tanlangan paket: <b>{paket}</b>\n"
                           "💰 To'lov summasi: <b>{summa} so'm</b>\n\n"
                           "1️⃣ Quyidagi kartaga <b>{summa} so'm</b> o'tkazing:\n\n"
                           "💳 <code>{karta}</code>\n"
                           "👤 <b>{egasi}</b>\n\n"
                           "2️⃣ To'lagach, <b>chek (skrinshot)ni shu yerga rasm qilib yuboring</b>\n\n"
                           "3️⃣ Tez orada tasdiqlanadi va Premium faollashadi! ✅"),
        'card_choose': "💳 Karta orqali to'lash",
        'payme_choose': "⚡️ Payme (avtomatik)",
        'click_choose': "🔵 Click (avtomatik)",
        'approved': "🎉 To'lovingiz tasdiqlandi!\n✅ Obunangiz faol — {until} gacha.\nEndi cheksiz video tahlil qilishingiz mumkin! 🎬",
        'sub_btn': "💳 Obuna sotib olish",
        'sub_btn_price': "💳 Obuna sotib olish (30 kun / {narx} so'm)",
        'one_btn': "🎬 1 ta tahlil (5,090 so'm)",
        'pay_instr_one': ("🎬 1 TA VIDEO TAHLIL\n\n"
                          "💰 Summa: {amount:,} so'm\n"
                          "💳 Karta: {card}\n"
                          "👤 Ega: {name}\n\n"
                          "✅ To'lagach, CHEK SKRINSHOTINI shu yerga yuboring.\n"
                          "Admin tekshirib, hisobingizga 1 ta tahlil qo'shadi. ⏳"),
        'sub_active': "✅ Obunangiz faol — {until} gacha.\nCheksiz video tahlil! 🎬",
        'sub_offer': ("💎 <b>InstaDoctor PREMIUM</b>\n"
                      "━━━━━━━━━━━━━━━\n\n"
                      "Bloggerlik — bu raqobat. TOPga chiqqanlar oddiy emas, "
                      "ular har videoni puxta tahlil qilib, xatolarini tuzatib boradi.\n\n"
                      "<b>Premium sizga nima beradi:</b>\n"
                      "♾ <b>Cheksiz tahlil</b> — har kuni xohlagancha\n"
                      "🔍 <b>Chuqur tahlil</b> — eng aniq, batafsil baho\n"
                      "🗣 <b>Ovozli javob</b> — maslahatlarni eshitasiz\n"
                      "🔥 <b>Eng kuchli heshteglar</b> va yashirin trendlar\n"
                      "📈 <b>REK ehtimoli</b> — % larda aniq ko'rsatkich\n"
                      "⚡ <b>Navbatsiz</b> — kutishsiz xizmat\n\n"
                      "💰 <b>Narxi: 29 900 so'm / oy</b>\n"
                      "(kuniga 1 000 so'mdan ham kam — bir choydan arzon! ☕️)\n\n"
                      "👇 Hoziroq boshlang — natijani his qiling!"),
        'ref_info': ("🔗 DO'STLARNI TAKLIF QILING!\n\n"
                     "Quyidagi havolani do'stingizga yuboring. Do'stingiz kirib "
                     "BIRINCHI video tahlilini qilsa — sizga +1 bepul tahlil qo'shamiz! 🎁\n\n"
                     "👇 Sizning havolangiz:\n{link}"),
        'ref_reward': "🎁 Tabriklaymiz! Siz taklif qilgan do'stingiz tahlil qildi — sizga +1 bepul tahlil qo'shildi!",
        'menu_ref': "🔗 Do'st taklif qilish",
    },
    'ru': {
        'welcome': (
            "🩺 Добро пожаловать в <b>INSTADOCTOR AI</b>!\n\n"
            "Я анализирую ваши Reels/Shorts по алгоритмам Instagram и рассчитываю "
            "<b>вероятность попадания в ТОП</b> 📈.\n\n"
            "🎁 <b>В подарок — 1 БЕСПЛАТНЫЙ ГЛУБОКИЙ АНАЛИЗ!</b>\n\n"
            "👇 Отправьте ваше видео прямо сейчас (до 2ГБ)"
        ),
        'gift_new': ("🎁 ПОДАРОК! Вам начислен 1 БЕСПЛАТНЫЙ анализ!\n\n"
                     "🎬 Отправьте видео прямо сейчас и попробуйте анализ "
                     "искусственным интеллектом БЕСПЛАТНО. Платить не нужно! 👇"),
        'cmp_up': "📈 РОСТ! Прошлое видео было {prev}%, сейчас {now}% — на {d}% лучше! Так держать! 🔥",
        'cmp_down': "📉 Сейчас {now}% (прошлое видео было {prev}%, на {d}% ниже). Обратите внимание на рекомендации! 💪",
        'cmp_same': "➡️ Это видео тоже {now}% — на прежнем уровне. В следующем постарайтесь выше! 🚀",
        'menu_video': "🎬 Анализ видео",
        'aksiya_msg': ("📣 Мы обновили алгоритмы и дарим тебе ещё 1 анализ бесплатно!\n\n"
                       "Привет! Одно видео — это только тест. Настоящий результат и выход в топ "
                       "видны на дистанции, при работе над ошибками.\n\n"
                       "Мы начислили тебе ещё 1 бесплатный анализ 🎁\n\n"
                       "Измени хук (начало) или концовку по нашей рекомендации и пришли видео снова. "
                       "Давай доведём твоё видео до идеала! 🚀"),
        'obuna_taklif_msg': ("Твой потенциал в Reels огромен! 🚀\n\n"
                             "Чтобы анализировать неограниченное количество видео, докручивать каждый "
                             "хук до идеала и стабильно расти в охватах — активируй подписку.\n\n"
                             "Это всего 29 900 сумов в месяц — меньше 1 000 сумов в день "
                             "за личного AI-продюсера! 💎"),
        'obuna_taklif_btn': "💎 Активировать подписку",
        'test_taklif_msg': ("🎉 <b>10 000+ блогеров используют InstaDoctor — теперь ваша очередь!</b>\n\n"
                            "Ещё не готовы к полной подписке? Понимаем! 😊\n"
                            "Начните с <b>малого шага</b>:\n\n"
                            "⚡️ <b>7 ДНЕЙ PREMIUM — всего 6 990 сум</b>\n"
                            "━━━━━━━━━━━━━\n"
                            "🚀 <b>VIP СКОРОСТЬ</b> — без очереди\n"
                            "🔍 <b>ГЛУБОКИЙ АНАЛИЗ</b> — точная оценка\n"
                            "♾ <b>БЕЗЛИМИТ</b> — 7 дней без ограничений\n"
                            "🎙 <b>АУДИО-СОВЕТЫ</b> + сильные хештеги\n"
                            "📈 <b>ВЕРОЯТНОСТЬ РЕК</b> — выход в ТОП в %\n"
                            "━━━━━━━━━━━━━\n"
                            "Попробуйте неделю — понравится, перейдёте на полную! 🔥"),
        'test_taklif_btn': "⚡ 7 дней Premium — активировать",
        'sorov_msg': ("🆘 Нам нужна ваша помощь! Взамен дарим БОНУС 🎁\n\n"
                      "🎉 Друзья, за короткое время число пользователей бота превысило {n}!\n\n"
                      "Мы хотим приносить вам ещё больше пользы и помогать вашим видео "
                      "выходить в РЕК 🚀\n\n"
                      "Ваше мнение очень важно 🙏 Всего 2 вопроса.\n"
                      "Каждому ответившему 🎁 +1 БЕСПЛАТНЫЙ анализ!\n\n"
                      "Нажмите кнопку ниже, чтобы начать 👇"),
        'sorov_q1_ask': ("1️⃣ Что именно вам понравилось в боте?\n"
                         "(Например: объяснения, скорость, точность...) ✨\n\n"
                         "Напишите ответ ниже 👇"),
        'sorov_q2': ("Спасибо! 🙏 Теперь 2-й вопрос:\n\n"
                     "2️⃣ Какие недостатки есть или что бы вы изменили / добавили? 💡\n\n"
                     "Напишите ответ ниже 👇"),
        'sorov_start_btn': "✍️ Ответить",
        'test_sorov_msg': ("📋 Ваше мнение очень важно для нас! 🙏\n\n"
                           "У нас есть новая идея, и мы хотим узнать ваше мнение:\n\n"
                           "💡 <b>7-ДНЕВНЫЙ ТЕСТ PREMIUM</b> хотим добавить всего за <b>6 990 сум</b>!\n\n"
                           "Возможности Premium:\n"
                           "🔍 <b>В 2 раза мощнее анализ</b>\n"
                           "♾ <b>Безлимитный анализ</b>\n"
                           "🎙 <b>Аудио-советы</b>\n"
                           "🔥 <b>Самые сильные хештеги</b>\n"
                           "📈 <b>Скрытая вероятность РЕК</b>\n\n"
                           "Ответьте на 2 вопроса 👇"),
        'test_sorov_btn': "✍️ Оставить отзыв",
        'test_sorov_q1': ("1️⃣ <b>Хотим добавить 7-дневный тест Premium (всего 6 990 сум)</b> — "
                          "вам интересно, попробовали бы?\n\nНапишите ваше мнение 👇"),
        'test_sorov_q2': ("Спасибо! 🙏 Теперь 2-й вопрос:\n\n"
                          "2️⃣ Недавно мы обновили бота — <b>повысили скорость и качество</b>. "
                          "Понравились изменения? 👇"),
        'test_sorov_done': "✅ Спасибо! Мы очень ценим ваше мнение 🙏",
        'sorov_thanks_reward': "Большое спасибо за отзыв! ❤️🎁 Мы добавили +1 БЕСПЛАТНЫЙ анализ. Отправьте видео! 🎬",
        'sorov_thanks': "Большое спасибо за отзыв! ❤️🙏",
        'menu_balance': "💰 Мой баланс",
        'menu_lang': "🌐 Язык",
        'menu_status': "🏆 Мой статус/баланс",
        'menu_arxiv': "📂 Мой отчёт",
        'menu_help': "ℹ️ Помощь",
        'menu_fikr': "💬 Отзывы и предложения",
        'menu_premium': "💎 Перейти на Premium",
        'sotuv_msg': ("Знаете, почему некоторые блогеры всегда в ТОПе? 🤔\n\n"
                      "Потому что они исправляют недостатки видео перед публикацией. "
                      "Но алгоритмы не стоят на месте — нужно анализировать каждый день и быть в тренде! 📊\n\n"
                      "💎 Что вы получите в PREMIUM?\n\n"
                      "⚡ <b>Максимальная скорость</b> — без очереди, анализ за секунды\n"
                      "♾ <b>Безлимитный анализ</b> — сколько угодно видео в день\n"
                      "🗣 <b>Аудио</b> — озвучка анализа\n"
                      "📈 <b>Скрытые тренды</b> — новинки алгоритмов первыми для вас\n\n"
                      "🔥 ТОЛЬКО СЕГОДНЯ — СКИДКА!\n"
                      "Текущая цена: <s>29 900 сум</s>\n"
                      "Только сегодня: <b>19 900 сум/мес</b> 🎉\n"
                      "(Всего 650 сум в день! ☕️ дешевле чашки чая)\n\n"
                      "⏳ Торопитесь — цена только СЕГОДНЯ ДО 22:00! Потом снова поднимется.\n\n"
                      "Одно видео в РЕК окупит эту сумму! 🚀\n\n"
                      "👇 Активируйте сейчас — не упустите шанс!"),
        'fikr_ask': ("💬 Напишите ваш отзыв или предложение 👇\n"
                     "Каждое мнение важно для нас! 🙏\n\n"
                     "📩 Или напишите напрямую: @Nurislom_admin"),
        'fikr_thanks': "Спасибо за отзыв! ❤️🙏 Мы обязательно его рассмотрим.",
        'menu_profile': "📊 Анализ профиля",
        'profile_instr': ("📊 <b>АНАЛИЗ ПРОФИЛЯ</b>\n\n"
                          "Проанализирую ваш Instagram-профиль на экспертном уровне — "
                          "сильные/слабые стороны, стратегия контента и конкретные рекомендации.\n\n"
                          "📸 <b>Какие скриншоты прислать:</b>\n\n"
                          "<b>1️⃣ Главная страница профиля</b> (самое важное!)\n"
                          "Откройте свой профиль в Instagram → сделайте скриншот. "
                          "Чтобы было видно <b>био, имя, число подписчиков и первые посты</b>.\n\n"
                          "<b>2️⃣ Сетка постов (галерея)</b>\n"
                          "Прокрутите вниз и сделайте скриншот сетки постов (9-12 штук) — "
                          "чтобы был виден ваш стиль и тематика.\n\n"
                          "<b>3️⃣ Статистика (по желанию, но полезно)</b>\n"
                          "Если бизнес-аккаунт: откройте <b>Insights / Статистику</b> "
                          "(охват, показы, лучшее время) → сделайте скриншот.\n\n"
                          "✅ Отправьте скриншот(ы) сюда → нажмите '✅ Анализировать'.\n\n"
                          "💡 Чем точнее скриншоты — тем сильнее анализ!\n\n"
                          "Для начала отправьте скриншот 👇"),
        'profile_got': "✅ Изображение получено ({n} шт.).\n\nОтправьте ещё или начните анализ 👇",
        'profile_btn': "✅ Анализировать",
        'profile_none': "❌ Сначала отправьте скриншот профиля.",
        'profile_mavzu_ask': ("🤔 Не смог точно понять тему профиля по скриншоту.\n\n"
                              "Пожалуйста, кратко напишите, на какую тему ваш профиль "
                              "(например: фитнес, кухня, бизнес, красота) 👇"),
        'profile_analyzing': "🧠 Анализирую профиль... ⚡",
        'full_btn': "📖 Посмотреть полный анализ",
        'qisqa_btn': "🔙 Вернуться к краткому",
        'maxsus_2700_msg': ("🎯 <b>Вы — наш активный пользователь!</b>\n\n"
                            "Вы использовали InstaDoctor несколько раз — значит, серьёзно "
                            "развиваете свой контент 👏\n\n"
                            "Именно для таких мы подготовили <b>специальное предложение</b> 👇\n\n"
                            "💎 <b>1 месяц Premium — 19 900 сум</b> (вместо 29 900)\n\n"
                            "В Premium вас ждёт:\n"
                            "✅ <b>Безлимитный</b> анализ\n"
                            "🔥 <b>\"Как улучшить?\"</b> — хук + 3 готовых варианта\n"
                            "🎙 <b>Голосовой</b> совет\n"
                            "📊 <b>Анализ профиля</b> — личная стратегия\n\n"
                            "⏰ Эта цена только <b>сегодня до 22:00</b>!\n\n"
                            "👇 Оформите Premium и выводите контент в ТОП!"),
        'tts_btn': "🔊 Кратко голосом",
        'yaxshilash_btn': "🔥 Как улучшить?",
        'yaxshilash_premium': ("🔒 <b>\"Как улучшить?\"</b> — это PREMIUM функция!\n\n"
                               "В Premium для вашего видео:\n"
                               "✅ Почему хук слабый — точная причина\n"
                               "✅ Как усилить — практическое решение\n"
                               "✅ <b>3 ГОТОВЫХ хука</b> — копируй и используй!\n\n"
                               "👇 Оформите Premium и выводите контент в ТОП:"),
        'yaxshilash_loading': "🔥 Готовлю анализ хука... ⏳",
        'tts_full_btn': "🔊 Прослушать",
        'inv_sub_title': "InstaDoctor — подписка на 1 месяц",
        'inv_sub_desc': "Безлимитный анализ видео в течение 1 месяца. 🔒 Оплата через Payme безопасна. После оплаты подписка активируется автоматически.",
        'inv_one_title': "InstaDoctor — 1 анализ",
        'inv_one_desc': "1 анализ видео. 🔒 Оплата через Payme безопасна. После оплаты добавляется автоматически.",
        'pay_safety': ("🔒 БЕЗОПАСНАЯ ОПЛАТА\n\n"
                       "💳 Оплата производится через Payme.\n"
                       "✅ Данные вашей карты не хранятся в боте — напрямую под защитой Payme.\n"
                       "⚡ После оплаты услуга активируется автоматически.\n\n"
                       "Продолжите через окно оплаты ниже 👇"),
        'pay_unavailable': "⚠️ Оплата пока недоступна. Попробуйте позже или свяжитесь с админом.",
        'pay_ok_sub': "✅ Оплата принята! Подписка активирована — безлимит до {until}. Спасибо! 🎉",
        'pay_ok_one': "✅ Оплата принята! Вам добавлен +1 анализ. Отправляйте видео! 🎉",
        'celebrate_sub': ("🎉🎊 <b>ПОЗДРАВЛЯЕМ!</b> 🎊🎉\n\n"
                          "💎 Теперь вы <b>PREMIUM</b> участник!\n\n"
                          "✨ Вам открыто:\n"
                          "♾ Безлимитный анализ\n"
                          "🔍 Глубокий, профессиональный анализ\n"
                          "🗣 Голосовой анализ\n"
                          "🔥 Рекомендации хештегов\n"
                          "⚡ Без очереди, быстро\n\n"
                          "📈 Подписка: до <b>{until}</b>\n\n"
                          "Отправляйте видео — получите лучший результат! 🚀"),
        'celebrate_test': ("🎉🎊 <b>ПОЗДРАВЛЯЕМ!</b> 🎊🎉\n\n"
                           "💎 Активирован 7-дневный <b>PREMIUM тест</b>!\n\n"
                           "✨ Вам открыто:\n"
                           "♾ Безлимитный анализ\n"
                           "🔍 Глубокий анализ\n"
                           "🗣 Голосовой анализ\n"
                           "⚡ Без очереди\n\n"
                           "📈 Срок: до <b>{until}</b>\n\n"
                           "Попробуйте — понравится, переходите на полную подписку! 🚀"),
        'celebrate_one': ("🎉 <b>ОПЛАТА ПРИНЯТА!</b> 🎉\n\n"
                          "✅ Вам добавлен <b>+1 анализ</b>!\n\n"
                          "Отправляйте видео — получите профессиональный анализ! 🚀"),
        'analyzed_footer': "\n\n━━━━━━━━━━\n🩺 Анализ выполнен ИИ экспертом InstaDoctor\n👉 @Instadoctorai_bot",
        'tts_loading': "🔊 Готовлю озвучку... ⏳",
        'tts_premium': ("🔒 Эта функция только для PREMIUM подписчиков!\n\n"
                        "🔊 Аудио (озвучка анализа) — преимущество подписки.\n"
                        "💎 Оформите подписку и слушайте все анализы голосом (и анализируйте без лимита)!"),
        'tts_fail': "😔 Не удалось озвучить. Попробуйте позже.",
        'full_gone': "❌ Полный анализ не найден (возможно, старый).",
        'profil_info': "📊 Анализ профиля пока не работает — скоро добавим! 🔜\n\nПока можете воспользоваться 🎬 Анализом видео.",
        'profile_premium': ("🔒 <b>Анализ профиля — только для PREMIUM!</b>\n\n"
                            "С Premium мы глубоко проанализируем весь ваш Instagram-профиль:\n"
                            "👤 Био и профиль — что хорошо, что улучшить\n"
                            "🎨 Общий стиль и контент\n"
                            "📈 Точная стратегия выхода в ТОП\n"
                            "🇺🇿 Советы для узбекского рынка\n\n"
                            "👇 Перейдите на Premium и выведите профиль на новый уровень!"),
        'help_text': ("ℹ️ INSTADOKTOR — Помощь\n\n"
                      "🎬 Анализ видео — отправьте видео, я полностью его проанализирую.\n\n"
                      "💰 Мой баланс — сколько анализов осталось.\n\n"
                      "🌐 Язык — сменить язык.\n\n"
                      "📏 Видео должно быть меньше 2ГБ.\n\n"
                      "📩 По вопросам и предложениям: @Nurislom_admin"),
        'lang_changed': "✅ Язык изменён!",
        'received': "⏳ Видео получено! Начинаю анализ... ⚡",
        'free_wait_promo': ("⏳ Очередь бесплатного анализа... подождите {sec} сек.\n\n"
                            "💎 С Premium — БЕЗ ОЧЕРЕДИ, анализ за секунды! ⚡\n"
                            "Алгоритмы Instagram не ждут! 🚀"),
        'analysis_timeout': ("⏳ Извините, анализ немного затянулся. Пожалуйста, отправьте видео "
                             "заново — в этот раз будет быстрее! 🔄\n\n"
                             "(Баланс не уменьшился)"),
        'queued': "⏳ Сейчас очередь занята, ваше видео в очереди. Немного подождите... 🕐",
        'queued_promo': ("⏳ Устали долго ждать?\n\n"
                         "Алгоритмы Instagram НЕ ЖДУТ! ⚡ Каждая секунда важна.\n\n"
                         "💎 Что вы получите с PREMIUM:\n"
                         "⚡ Без очереди — анализ за секунды\n"
                         "♾ Безлимитный анализ видео\n"
                         "🗣 Аудио-анализ — слушайте советы\n"
                         "🔥 Самые сильные хештеги и тренды\n"
                         "📈 Скрытая вероятность РЕК (в %)\n\n"
                         "👇 Активируйте подписку сейчас"),
        'too_big': "❌ Видео слишком большое (не более 2ГБ). 📏\n\nПожалуйста, отправьте покороче.",
        'wrong_format': "❌ Не распознал формат. Отправьте MP4 или MOV. 📹",
        'uploading': "📤 Видео загружается...",
        'analyzing': "🧠 InstaDoctor анализирует... ⚡",
        'time_upsell': ("💎 Это был краткий бесплатный анализ.\n\n"
                        "С Premium вы получите:\n"
                        "🔍 Ещё более ГЛУБОКИЙ и подробный анализ\n"
                        "🗣 Аудио-ответ — слушайте советы\n"
                        "🔥 Сильнейшие хештеги и скрытые тренды\n"
                        "♾ Безлимитный анализ — каждый день сколько хотите\n\n"
                        "👇 Перейдите на Premium — раскройте все возможности!"),
        'analyzing_live': "🧠 InstaDoctor AI анализирует ваше видео",
        'ready': "✅ Анализ готов!",
        'error': "😔 Извините, не удалось проанализировать. Отправьте видео ещё раз. 🔄",
        'busy_quota': ("⏳ Сейчас система сильно загружена. Пожалуйста, подождите немного "
                       "(5-10 минут) и попробуйте снова. Баланс не списан. 🙏"),
        'send_video': "🎬 Отправьте видео для анализа! 📤",
        'no_balance': ("🎬 <b>Ваши бесплатные анализы закончились!</b>\n\n"
                       "Но вы только начали 😊 В Premium вас ждёт:\n\n"
                       "✅ <b>Безлимитный</b> анализ\n"
                       "🔥 <b>\"Как улучшить?\"</b> — хук + 3 готовых варианта\n"
                       "🎙 <b>Голосовой</b> совет\n"
                       "📊 <b>Анализ профиля</b> — личная стратегия\n"
                       "⚡️ <b>Мощная модель</b> — точная оценка\n"
                       "━━━━━━━━━━━━━\n"
                       "💎 <b>1 месяц — 29 900 сум</b> (безлимит)\n"
                       "📍 <b>1 анализ — 5 090 сум</b>\n\n"
                       "👇 Выберите и выводите контент в ТОП!"),
        'balance_info': "💰 У вас {n} бесплатных анализов.",
        'choose_pkg': "💳 Подписка:",
        'pay_instr': ("💳 ПОДПИСКА НА 1 МЕСЯЦ — безлимитный анализ видео (30 дней)\n\n"
                      "💰 Сумма: {amount:,} сум\n"
                      "💳 Карта: {card}\n"
                      "👤 Владелец: {name}\n\n"
                      "✅ После оплаты отправьте СКРИНШОТ ЧЕКА сюда.\n"
                      "Админ проверит и активирует подписку. ⏳"),
        'receipt_sent': "✅ Ваш чек отправлен админу. Скоро подтвердим! ⏳",
        'too_big_free': ("🎬 Это видео немного длинное/большое.\n\n"
                         "💎 Оформите <b>Premium</b> — анализируйте <b>длинные и большие видео</b> "
                         "без ограничений!\n\n"
                         "Или пока отправьте видео покороче. 😊"),
        'too_big_paid': ("🎬 Это видео слишком большое.\n\n"
                         "Пожалуйста, отправьте видео покороче. 😊"),
        'card_pay_instr': ("💳 <b>ОПЛАТА ПО КАРТЕ</b>\n\n"
                           "📦 Выбранный пакет: <b>{paket}</b>\n"
                           "💰 Сумма: <b>{summa} сум</b>\n\n"
                           "1️⃣ Переведите <b>{summa} сум</b> на карту:\n\n"
                           "💳 <code>{karta}</code>\n"
                           "👤 <b>{egasi}</b>\n\n"
                           "2️⃣ После оплаты <b>отправьте чек (скриншот) сюда картинкой</b>\n\n"
                           "3️⃣ Скоро подтвердим и Premium активируется! ✅"),
        'card_choose': "💳 Оплата по карте",
        'payme_choose': "⚡️ Payme (автоматически)",
        'click_choose': "🔵 Click (автоматически)",
        'approved': "🎉 Оплата подтверждена!\n✅ Подписка активна — до {until}.\nТеперь безлимитный анализ видео! 🎬",
        'sub_btn': "💳 Оформить подписку",
        'sub_btn_price': "💳 Оформить подписку (30 дней / {narx} сум)",
        'one_btn': "🎬 1 анализ (5 090 сум)",
        'pay_instr_one': ("🎬 1 АНАЛИЗ ВИДЕО\n\n"
                          "💰 Сумма: {amount:,} сум\n"
                          "💳 Карта: {card}\n"
                          "👤 Владелец: {name}\n\n"
                          "✅ После оплаты отправьте СКРИНШОТ ЧЕКА сюда.\n"
                          "Админ проверит и добавит 1 анализ на счёт. ⏳"),
        'sub_active': "✅ Подписка активна — до {until}.\nБезлимитный анализ видео! 🎬",
        'sub_offer': ("💎 <b>InstaDoctor PREMIUM</b>\n"
                      "━━━━━━━━━━━━━━━\n\n"
                      "Блогинг — это конкуренция. Те, кто в ТОПе, тщательно "
                      "анализируют каждое видео и работают над ошибками.\n\n"
                      "<b>Что даёт Premium:</b>\n"
                      "♾ <b>Безлимитный анализ</b> — каждый день сколько хотите\n"
                      "🔍 <b>Глубокий анализ</b> — самая точная, подробная оценка\n"
                      "🗣 <b>Аудио-ответ</b> — слушайте советы\n"
                      "🔥 <b>Сильнейшие хештеги</b> и скрытые тренды\n"
                      "📈 <b>Вероятность РЕК</b> — точный показатель в %\n"
                      "⚡ <b>Без очереди</b> — обслуживание без ожидания\n\n"
                      "💰 <b>Цена: 29 900 сум / месяц</b>\n"
                      "(меньше 1 000 сум в день — дешевле чашки чая! ☕️)\n\n"
                      "👇 Начните прямо сейчас — почувствуйте результат!"),
        'ref_info': ("🔗 ПРИГЛАШАЙТЕ ДРУЗЕЙ!\n\n"
                     "Отправьте эту ссылку другу. Когда друг зайдёт и сделает "
                     "ПЕРВЫЙ анализ видео — вам начислим +1 бесплатный анализ! 🎁\n\n"
                     "👇 Ваша ссылка:\n{link}"),
        'ref_reward': "🎁 Поздравляем! Приглашённый вами друг сделал анализ — вам начислен +1 бесплатный анализ!",
        'menu_ref': "🔗 Пригласить друга",
    }
}


def get_lang(context):
    return context.user_data.get('lang', 'uz')


def t(context, key):
    return TEXTS[get_lang(context)][key]


def main_keyboard(context, uid=None):
    # ADMIN uchun TEST menyu (Statusim/Balansim + Arxiv)
    if uid and is_admin(uid):
        return ReplyKeyboardMarkup(
            [
                [KeyboardButton(t(context, 'menu_video')), KeyboardButton(t(context, 'menu_profile'))],
                [KeyboardButton(t(context, 'menu_status')), KeyboardButton(t(context, 'menu_premium'))],
                [KeyboardButton(t(context, 'menu_arxiv')), KeyboardButton(t(context, 'menu_ref'))],
                [KeyboardButton(t(context, 'menu_help')), KeyboardButton(t(context, 'menu_fikr'))],
            ],
            resize_keyboard=True
        )
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(t(context, 'menu_video')), KeyboardButton(t(context, 'menu_profile'))],
            [KeyboardButton(t(context, 'menu_status')), KeyboardButton(t(context, 'menu_premium'))],
            [KeyboardButton(t(context, 'menu_balance')), KeyboardButton(t(context, 'menu_ref'))],
            [KeyboardButton(t(context, 'menu_help')), KeyboardButton(t(context, 'menu_fikr'))],
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
        [InlineKeyboardButton(sub_btn_label(context), callback_data='buy_sub')],
        [InlineKeyboardButton(t(context, 'one_btn'), callback_data='buy_one')],
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_blocked(user.id):
        return
    was_new = not user_exists(user.id)
    save_user(user.id, user.username, user.first_name)
    context.user_data['is_new'] = was_new  # yangi user uchun sovg'a xabari ko'rsatamiz
    # Referral: havola t.me/bot?start=ref_<id> orqali kelgan bo'lsa va YANGI user bo'lsa
    if was_new and context.args:
        arg = context.args[0]
        if arg.startswith("ref_"):
            try:
                referrer_id = int(arg[4:])
                set_referrer(user.id, referrer_id)
            except Exception:
                pass
    await update.message.reply_text("🌐 Tilni tanlang / Выберите язык:", reply_markup=lang_keyboard())


async def show_menu(message, context):
    # Bitta toza xabar (welcome ichida sovg'a ham bor - 'Wall of Text' bo'lmasin)
    await message.reply_text(t(context, 'welcome'), reply_markup=main_keyboard(context, message.chat.id), parse_mode="HTML")
    # Marafon yoqilgan bo'lsa va bu yangi user bo'lsa - marafonni boshlaymiz
    if context.user_data.get('is_new') and get_setting("marafon_aktiv", "off") == "on":
        try:
            uid = message.chat.id
            _m = _db_execute("SELECT marafon_kun FROM users WHERE user_id = %s", (uid,), fetch='one')
            if not _m or not _m[0]:  # hali marafonda emas
                await asyncio.sleep(1)
                await marafon_boshla(uid, context)
        except Exception:
            pass
    context.user_data['is_new'] = False


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == 'boshlash':
        # "Boshlash" tugmasi (yangilik xabaridan) - til tanlashni ko'rsatadi (menyu yangilanadi)
        await query.message.reply_text("🌐 Tilni tanlang / Выберите язык:", reply_markup=lang_keyboard())
        return
    if data == 'lang_uz':
        context.user_data['lang'] = 'uz'
        await show_menu(query.message, context)
    elif data == 'lang_ru':
        context.user_data['lang'] = 'ru'
        await show_menu(query.message, context)
    elif data == 'buy_sub_20':
        # Renewal 20% chegirma (23,900). 24 soat ichida - chegirma, keyin to'liq 29,900
        uid = query.from_user.id
        _sana = _db_execute("SELECT renewal_chegirma_sana FROM users WHERE user_id = %s", (uid,), fetch='one')
        chegirma_faol = False
        if _sana and _sana[0]:
            d = _parse_dt(_sana[0])
            if d and (datetime.now() - d).total_seconds() <= 24 * 3600:
                chegirma_faol = True
        if chegirma_faol:
            # 23,900 chegirma to'lovi
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton(t(context, 'payme_choose'), callback_data='pm_renewal')],
                [InlineKeyboardButton(t(context, 'click_choose'), callback_data='cl_renewal')],
                [InlineKeyboardButton(t(context, 'card_choose'), callback_data='card_renewal')],
            ])
            await query.message.reply_text(
                "💎 <b>1 oylik Premium — 23,900 so'm</b> (20% chegirma)\n\n👇 To'lov turini tanlang:",
                reply_markup=kb, parse_mode="HTML")
        else:
            # Chegirma tugadi - to'liq narx
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("💎 1 oylik olish (29,900)", callback_data='buy_sub')],
            ])
            await query.message.reply_text(
                "⏰ <b>Chegirma vaqti tugadi!</b>\n\n"
                "20% chegirma muddati o'tdi. Lekin siz to'liq narxda davom etishingiz mumkin: "
                "<b>29,900 so'm</b> 💎",
                reply_markup=kb, parse_mode="HTML")
    elif data == 'buy_sub_19':
        # sotuv1b 19,900 chegirma. Chegirma vaqti (chegirma_until) tugaganmi tekshiramiz
        if discount_active():
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton(t(context, 'payme_choose'), callback_data='pm_sub')],
                [InlineKeyboardButton(t(context, 'click_choose'), callback_data='cl_sub')],
                [InlineKeyboardButton(t(context, 'card_choose'), callback_data='card_sub')],
            ])
            await query.message.reply_text(
                "💎 <b>1 oylik Premium — 19,900 so'm</b> (chegirma)\n\n👇 To'lov turini tanlang:",
                reply_markup=kb, parse_mode="HTML")
        else:
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("💎 1 oylik olish (29,900)", callback_data='buy_sub')],
            ])
            await query.message.reply_text(
                "⏰ <b>Chegirma vaqti tugadi!</b>\n\n"
                "19,900 chegirma muddati o'tdi. Lekin siz to'liq narxda davom etishingiz mumkin: "
                "<b>29,900 so'm</b> 💎",
                reply_markup=kb, parse_mode="HTML")
    elif data == 'buy_sub':
        # To'lov turini tanlash: Payme yoki Karta
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(t(context, 'payme_choose'), callback_data='pm_sub')],
            [InlineKeyboardButton(t(context, 'click_choose'), callback_data='cl_sub')],
            [InlineKeyboardButton(t(context, 'card_choose'), callback_data='card_sub')],
        ])
        await query.message.reply_text("💳 To'lov turini tanlang:", reply_markup=kb)
    elif data == 'buy_test':
        # Aksiya tugagan bo'lsa - 7 minglik o'chirilgan, to'liq obunaga yo'naltiramiz
        # Aksiya tugaganmi? (1) qo'lda o'chirilgan, YOKI (2) tugash vaqti o'tgan
        _aksiya_tugadi = (get_setting("tarif7_aksiya", "on") == "off")
        if not _aksiya_tugadi:
            _tugash = get_setting("tarif7_tugash", "")
            if _tugash:
                try:
                    UZ_OFF = int(os.getenv("UZ_TZ_OFFSET", "5"))
                    hozir_uz = datetime.utcnow() + timedelta(hours=UZ_OFF)
                    if hozir_uz > datetime.strptime(_tugash, "%Y-%m-%d %H:%M"):
                        _aksiya_tugadi = True
                except Exception:
                    pass
        # DRIP foydalanuvchisi: shaxsiy 24 soat (drip aksiya olgan vaqtidan)
        if not _aksiya_tugadi:
            _urow = _db_execute("SELECT tarif7_sana FROM users WHERE user_id=%s",
                                (query.from_user.id,), fetch='one')
            if _urow and _urow[0]:
                try:
                    olingan = datetime.strptime(_urow[0], "%Y-%m-%d %H:%M")
                    if (datetime.now() - olingan).total_seconds() > 24 * 3600:
                        _aksiya_tugadi = True
                except Exception:
                    pass
        if _aksiya_tugadi:
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton(sub_btn_label(context), callback_data='buy_sub')],
            ])
            await query.message.reply_text(
                "⏰ <b>Aksiya tugadi!</b>\n\n"
                "7 kunlik maxsus narx muddati o'tdi. Lekin siz to'liq obunadan "
                "foydalanishingiz mumkin — barcha imkoniyatlar ochiq! 💎",
                reply_markup=kb, parse_mode="HTML")
            return
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(t(context, 'payme_choose'), callback_data='pm_test')],
            [InlineKeyboardButton(t(context, 'click_choose'), callback_data='cl_test')],
            [InlineKeyboardButton(t(context, 'card_choose'), callback_data='card_test')],
        ])
        await query.message.reply_text("💳 To'lov turini tanlang:", reply_markup=kb)
    elif data == 'buy_one':
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(t(context, 'payme_choose'), callback_data='pm_one')],
            [InlineKeyboardButton(t(context, 'click_choose'), callback_data='cl_one')],
            [InlineKeyboardButton(t(context, 'card_choose'), callback_data='card_one')],
        ])
        await query.message.reply_text("💳 To'lov turini tanlang:", reply_markup=kb)
    # ===== PAYME tanlandi (GET havola -> Payme sahifasi, Merchant API avtomatik) =====
    elif data in ('pm_sub', 'pm_test', 'pm_one', 'pm_renewal'):
        if not PAYME_MERCHANT_ID:
            await query.message.reply_text(t(context, 'pay_unavailable'))
            return
        if data == 'pm_sub':
            summa, nom = current_sub_price(), "1 oylik obuna"
        elif data == 'pm_renewal':
            summa, nom = SUB_PRICE_RENEWAL, "1 oylik obuna (20% chegirma)"
        elif data == 'pm_test':
            if tarif7_tugadimi(query.from_user.id):
                kb = InlineKeyboardMarkup([[InlineKeyboardButton(sub_btn_label(context), callback_data='buy_sub')]])
                await query.message.reply_text(
                    "⏰ <b>Aksiya tugadi!</b>\n\n7 kunlik maxsus narx muddati o'tdi. "
                    "To'liq obunadan foydalaning — barcha imkoniyatlar ochiq! 💎",
                    reply_markup=kb, parse_mode="HTML")
                return
            summa, nom = TEST_PRICE, "7 kunlik Premium"
        else:
            summa, nom = ONE_PRICE, "1 ta tahlil"
        link = _payme_checkout_link(query.from_user.id, summa)
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"💳 {summa:,} so'm to'lash", url=link)
        ]])
        await query.message.reply_text(
            f"💳 <b>{nom}</b> — <b>{summa:,} so'm</b>\n\n"
            f"Quyidagi tugmani bosing — <b>Payme</b> orqali xavfsiz to'lang.\n"
            f"To'lovdan so'ng Premium <b>avtomatik</b> faollashadi! ✅",
            reply_markup=kb, parse_mode="HTML"
        )
    # ===== CLICK tanlandi (avtomatik havola) =====
    elif data in ('cl_sub', 'cl_test', 'cl_one', 'cl_renewal'):
        if not CLICK_SERVICE_ID or not CLICK_MERCHANT_ID:
            await query.message.reply_text(t(context, 'pay_unavailable'))
            return
        if data == 'cl_sub':
            summa, nom, pkg = current_sub_price(), "1 oylik obuna", "sub_1month"
        elif data == 'cl_renewal':
            summa, nom, pkg = SUB_PRICE_RENEWAL, "1 oylik obuna (20% chegirma)", "sub_1month_renewal"
        elif data == 'cl_test':
            if tarif7_tugadimi(query.from_user.id):
                kb = InlineKeyboardMarkup([[InlineKeyboardButton(sub_btn_label(context), callback_data='buy_sub')]])
                await query.message.reply_text(
                    "⏰ <b>Aksiya tugadi!</b>\n\n7 kunlik maxsus narx muddati o'tdi. "
                    "To'liq obunadan foydalaning — barcha imkoniyatlar ochiq! 💎",
                    reply_markup=kb, parse_mode="HTML")
                return
            summa, nom, pkg = TEST_PRICE, "7 kunlik Premium", "test_7day"
        else:
            summa, nom, pkg = ONE_PRICE, "1 ta tahlil", "one_1"
        link = _click_checkout_link(query.from_user.id, summa, pkg)
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"🔵 {summa:,} so'm to'lash", url=link)
        ]])
        await query.message.reply_text(
            f"🔵 <b>{nom}</b> — <b>{summa:,} so'm</b>\n\n"
            f"Quyidagi tugmani bosing — <b>Click</b> orqali xavfsiz to'lang.\n"
            f"To'lovdan so'ng Premium <b>avtomatik</b> faollashadi! ✅",
            reply_markup=kb, parse_mode="HTML"
        )
    # ===== KARTA tanlandi (chek yuborish) =====
    elif data in ('card_sub', 'card_test', 'card_one', 'card_renewal'):
        if data == 'card_sub':
            paket_nom, summa, pkg = "1 oylik obuna", current_sub_price(), "sub_1month"
        elif data == 'card_renewal':
            paket_nom, summa, pkg = "1 oylik obuna (20% chegirma)", SUB_PRICE_RENEWAL, "sub_1month_renewal"
        elif data == 'card_test':
            if tarif7_tugadimi(query.from_user.id):
                kb = InlineKeyboardMarkup([[InlineKeyboardButton(sub_btn_label(context), callback_data='buy_sub')]])
                await query.message.reply_text(
                    "⏰ <b>Aksiya tugadi!</b>\n\n7 kunlik maxsus narx muddati o'tdi. "
                    "To'liq obunadan foydalaning — barcha imkoniyatlar ochiq! 💎",
                    reply_markup=kb, parse_mode="HTML")
                return
            paket_nom, summa, pkg = "7 kunlik Premium", TEST_PRICE, "test_7day"
        else:
            paket_nom, summa, pkg = "1 ta tahlil", ONE_PRICE, "one_1"
        # Paketni eslab qolamiz (chek kelganda kerak)
        context.user_data['card_paket'] = pkg
        context.user_data['card_paket_nom'] = paket_nom
        context.user_data['card_summa'] = summa
        context.user_data['mode'] = 'card_receipt'
        await query.message.reply_text(
            t(context, 'card_pay_instr').format(
                paket=paket_nom, summa=f"{summa:,}", karta=CARD_NUMBER, egasi=CARD_HOLDER),
            parse_mode="HTML"
        )
    elif data.startswith('chok_'):
        # Chek tasdiqlash: chok_{user_id}_{package}
        if not is_admin(query.from_user.id):
            await query.answer("Faqat adminlar tasdiqlay oladi", show_alert=True)
            return
        parts = data.split('_')
        target_user = int(parts[1])
        package = '_'.join(parts[2:]) if len(parts) > 2 else 'sub_1month'
        admin_name = query.from_user.username or query.from_user.first_name or "admin"
        if package == 'one_1':
            add_balance(target_user, 1)
            create_payment(target_user, 'one_1', ONE_PRICE)
            try:
                await _send_celebration_uid(target_user, TEXTS['uz']['celebrate_one'])
            except Exception:
                pass
            natija = "1 ta tahlil qo'shildi"
        elif package == 'test_7day':
            until = activate_subscription(target_user, TEST_DAYS)
            create_payment(target_user, 'test_7day', TEST_PRICE)
            try:
                await _send_celebration_uid(target_user, TEXTS['uz']['celebrate_test'].format(until=until))
            except Exception:
                pass
            natija = f"7 kunlik Premium ({until} gacha)"
        else:
            until = activate_subscription(target_user, SUB_DAYS)
            create_payment(target_user, 'sub_1month', current_sub_price())
            try:
                await _send_celebration_uid(target_user, TEXTS['uz']['celebrate_sub'].format(until=until))
            except Exception:
                pass
            natija = f"1 oylik obuna ({until} gacha)"
        # Guruhdagi xabarni yangilaymiz (tugmalarni olib tashlaymiz)
        try:
            await query.edit_message_caption(
                caption=f"✅ TASDIQLANDI\n👤 ID: {target_user}\n📦 {natija}\n"
                        f"👮 Tasdiqladi: @{admin_name}"
            )
        except Exception:
            pass
    elif data.startswith('chrad_'):
        # Chek rad etish: chrad_{user_id}
        if not is_admin(query.from_user.id):
            await query.answer("Faqat adminlar", show_alert=True)
            return
        target_user = int(data.split('_')[1])
        admin_name = query.from_user.username or query.from_user.first_name or "admin"
        try:
            await context.bot.send_message(
                target_user,
                "❌ Kechirasiz, chekingiz tasdiqlanmadi. "
                "To'lov to'g'ri amalga oshganini tekshiring yoki @Nurislom_admin ga murojaat qiling."
            )
        except Exception:
            pass
        try:
            await query.edit_message_caption(
                caption=f"❌ RAD ETILDI\n👤 ID: {target_user}\n👮 @{admin_name}"
            )
        except Exception:
            pass
    elif data == 'video_sovga_ol':
        uid = query.from_user.id
        # Sovg'a allaqachon olinganmi? (sovga_olindi belgisi)
        row = _db_execute("SELECT COALESCE(video_sovga_olindi, FALSE) FROM users WHERE user_id = %s",
                          (uid,), fetch='one')
        if row and row[0]:
            await query.message.reply_text(t(context, 'video_fikr_already'), parse_mode="HTML")
            return
        add_balance(uid, 1)
        _db_execute("UPDATE users SET video_sovga_olindi = TRUE WHERE user_id = %s", (uid,))
        await query.message.reply_text(t(context, 'video_sovga_olindi'), parse_mode="HTML")
    elif data == 'video_fikr':
        context.user_data['mode'] = 'video_fikr'
        await query.message.reply_text(t(context, 'video_fikr_ask'), parse_mode="HTML")
    elif data == 'premium_fikr':
        context.user_data['mode'] = 'premium_fikr'
        await query.message.reply_text(t(context, 'premium_fikr_ask'), parse_mode="HTML")
    elif data == 'sotuv1_fikr':
        context.user_data['mode'] = 'sotuv1_fikr'
        await query.answer()
        await query.message.reply_text(
            "Juda yaxshi! 🤍 Iltimos, sababini yozing — nega premium olmadingiz?\n\n"
            "(Narx, funksiya, ishonch yoki boshqa sabab — qandayini yozing, "
            "javobingiz uchun darhol 1 ta BEPUL PREMIUM tahlil qo'shiladi!)"
        )
    elif data == 'sotuv3_bepul':
        uid = update.effective_user.id
        _chk = _db_execute("SELECT sotuv_bepul_olindi FROM users WHERE user_id = %s", (uid,), fetch='one')
        if _chk and _chk[0]:
            await query.answer("Siz allaqachon bepul premium oldingiz! 🎁", show_alert=True)
            return
        add_premium_balance(uid, 1)
        _db_execute("UPDATE users SET sotuv_bepul_olindi = %s WHERE user_id = %s",
                    ("sotuv3: " + datetime.now().strftime("%Y-%m-%d %H:%M"), uid))
        uname = update.effective_user.username or update.effective_user.first_name or ""
        for aid in ADMIN_IDS:
            try:
                who = f"@{uname}" if uname else f"ID {uid}"
                await context.bot.send_message(aid, f"🎁 PREMIUM berildi (sotuv3)\n👤 {who}")
            except Exception:
                pass
        await query.answer("✅ Premium qo'shildi!")
        await query.message.reply_text(
            "🎁 Zo'r! Hisobingizga <b>1 ta BEPUL PREMIUM tahlil</b> qo'shildi!\n"
            "🔊 Ovozli eshitish va 🔥 Hook yaxshilash ham ochiq.\n\n"
            "Hoziroq videongizni yuboring — premium kuchini ko'ring! 🎬",
            parse_mode="HTML"
        )
    elif data == 'sotuv4_bepul':
        uid = update.effective_user.id
        # HIMOYA: allaqachon olganmi? (qayta bosishni to'sish)
        _chk = _db_execute("SELECT sotuv_bepul_olindi FROM users WHERE user_id = %s", (uid,), fetch='one')
        if _chk and _chk[0]:
            await query.answer("Siz allaqachon bepul premium oldingiz! 🎁", show_alert=True)
            return
        add_premium_balance(uid, 1)
        _db_execute("UPDATE users SET sotuv_bepul_olindi = %s WHERE user_id = %s",
                    ("sotuv4: " + datetime.now().strftime("%Y-%m-%d %H:%M"), uid))
        uname = update.effective_user.username or update.effective_user.first_name or ""
        for aid in ADMIN_IDS:
            try:
                who = f"@{uname}" if uname else f"ID {uid}"
                await context.bot.send_message(aid, f"🎁 PREMIUM berildi (sotuv4)\n👤 {who}")
            except Exception:
                pass
        await query.answer("✅ Premium qo'shildi!")
        await query.message.reply_text(
            "🎁 Zo'r! Hisobingizga <b>1 ta BEPUL PREMIUM tahlil</b> qo'shildi!\n"
            "🔊 Ovozli eshitish va 🔥 Hook yaxshilash ham ochiq.\n\n"
            "Hoziroq videongizni yuboring 🎬",
            parse_mode="HTML"
        )
    elif data == 'aksiya_video':
        await query.message.reply_text(
            "🎬 Zo'r! Videongizni shu yerga yuboring — men uni to'liq tahlil qilaman 👇"
        )
    elif data == 'marafon_kanal':
        # 3-kun: kanalga obuna tekshiriladi, obuna bo'lsa bepul tahlil beriladi
        uid = query.from_user.id
        try:
            member = await context.bot.get_chat_member(MARAFON_KANAL, uid)
            obuna = member.status in ("member", "administrator", "creator")
        except Exception:
            obuna = False
        if obuna:
            # Bugun kanal bepulini bergunmi tekshiramiz (takror bermaslik)
            bugun = datetime.now().strftime("%Y-%m-%d")
            _ch = _db_execute("SELECT marafon_kunlik_sana, marafon_kun FROM users WHERE user_id = %s", (uid,), fetch='one')
            # 3-kun bepulini faqat bir marta beramiz - alohida belgi (marafon_start ishlatmaymiz)
            already = _db_execute("SELECT sotuv_bepul_olindi FROM users WHERE user_id = %s", (uid,), fetch='one')
            key = "marafon_kanal_bonus"
            if already and already[0] == key:
                await query.answer("✅ Siz allaqachon bugungi tahlilingizni oldingiz!", show_alert=True)
                await query.message.reply_text("🎬 Videongizni yuboring — tahlil qilaman 👇")
            else:
                add_balance(uid, 1)
                _db_execute("UPDATE users SET sotuv_bepul_olindi = %s WHERE user_id = %s", (key, uid))
                # 3-kun "o'tgan" deb sanaymiz (kanal obuna + bepul oldi)
                _oxk = _db_execute("SELECT marafon_oxirgi_bajarilgan, marafon_kun, marafon_tugadi FROM users WHERE user_id = %s", (uid,), fetch='one')
                if _oxk and _oxk[1] and _oxk[1] >= 1 and not _oxk[2] and _oxk[0] != bugun:
                    _db_execute(
                        "UPDATE users SET marafon_bajarilgan = COALESCE(marafon_bajarilgan,0) + 1, "
                        "marafon_oxirgi_bajarilgan = %s WHERE user_id = %s", (bugun, uid))
                await query.answer("✅ Rahmat! Bepul tahlilingiz ochildi! 🎉")
                await query.message.reply_text(
                    "🎉 Zo'r! Obuna uchun rahmat! 💙\n\n"
                    "🎬 Endi videongizni yuboring — bugungi bepul tahlilingizni olaman 👇")
        else:
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("📢 Kanalga o'tish", url=f"https://t.me/{MARAFON_KANAL.lstrip('@')}")],
                [InlineKeyboardButton("✅ Obuna bo'ldim", callback_data="marafon_kanal")],
            ])
            await query.answer("Avval kanalga obuna bo'ling 👆", show_alert=True)
            await query.message.reply_text(
                f"📢 Bepul tahlil uchun avval kanalimizga obuna bo'ling:\n{MARAFON_KANAL}\n\n"
                "Obuna bo'lgach, pastdagi '✅ Obuna bo'ldim' tugmasini bosing 👇",
                reply_markup=kb)
    elif data == 'menyu_yangila':
        # Eski userlar menyuni yangilaydi
        await query.answer("✅ Menyu yangilandi!")
        await query.message.reply_text(
            "✅ <b>Menyu yangilandi!</b>\n\n"
            "Endi pastdagi yangi tugmalardan foydalaning:\n"
            "🏆 <b>Statusim</b> — kreator darajangiz va progressingiz\n"
            "💰 Balansim, tahlil va boshqalar!",
            reply_markup=main_keyboard(context, query.from_user.id), parse_mode="HTML")
    elif data == 'marafon_fikr':
        # 4-kun: fikr so'raymiz (majburiy - fikr yozsa 4-kun bajariladi)
        uid = query.from_user.id
        await query.answer()
        bugun = datetime.now().strftime("%Y-%m-%d")
        _ox = _db_execute("SELECT marafon_oxirgi_bajarilgan FROM users WHERE user_id = %s", (uid,), fetch='one')
        if _ox and _ox[0] == bugun:
            await query.message.reply_text("✅ Bugungi fikringizni oldik, rahmat! Videongizni yuboring 👇")
        else:
            context.user_data['mode'] = 'marafon_fikr'
            await query.message.reply_text(
                "✍️ <b>Fikringizni yozing:</b>\n\n"
                "Bot sizga qanday yordam berdi? Nima yoqdi? Nima yaxshilash kerak?\n\n"
                "(Fikringizni yozsangiz — bugungi bepul tahlilingiz ochiladi va 4-kun bajariladi! 🎁)",
                parse_mode="HTML")
    elif data == 'marafon_tahlil':
        # Marafon kunlik tahlil - balans bor-yo'qligini tekshiramiz
        uid = query.from_user.id
        await query.answer()
        if get_balance(uid) > 0 or get_premium_balance(uid) > 0 or sub_active(uid) or is_admin(uid):
            # Bepulni oldi (tugma bosdi) - bu kunni "o'tgan" deb sanaymiz (kuniga 1 marta)
            bugun = datetime.now().strftime("%Y-%m-%d")
            _ox = _db_execute("SELECT marafon_oxirgi_bajarilgan, marafon_kun, marafon_tugadi FROM users WHERE user_id = %s", (uid,), fetch='one')
            if _ox and _ox[1] and _ox[1] >= 1 and not _ox[2] and _ox[0] != bugun:
                _db_execute(
                    "UPDATE users SET marafon_bajarilgan = COALESCE(marafon_bajarilgan,0) + 1, "
                    "marafon_oxirgi_bajarilgan = %s WHERE user_id = %s", (bugun, uid))
            await query.message.reply_text(
                "🎬 Zo'r! Videongizni shu yerga yuboring — bugungi bepul tahlilingizni olaman 👇")
        else:
            # Balans kuygan (23:00 o'tgan) - halol tushuntiramiz
            await query.message.reply_text(
                "⏰ <b>Bugungi bepul tahlil vaqti tugadi!</b>\n\n"
                "Kunlik bepul tahlil faqat o'sha kuni ishlaydi 🌙\n\n"
                "🌅 Ertaga soat 11:00 da yangi bepul tahlilingiz keladi — kuting!\n\n"
                "💡 Yoki hoziroq davom etmoqchi bo'lsangiz — Premium oling 💎",
                parse_mode="HTML")
    elif data == 'marafon_premium':
        # 5-kun: PREMIUM sovg'a - FAQAT 5 kunni to'liq o'tganga
        uid = query.from_user.id
        _tekshir = _db_execute("SELECT marafon_tugadi, COALESCE(marafon_bajarilgan,0) FROM users WHERE user_id = %s", (uid,), fetch='one')
        allaqachon = _tekshir and _tekshir[0]
        bajarilgan = _tekshir[1] if _tekshir else 0
        if allaqachon:
            await query.answer("Siz allaqachon PREMIUM sovg'angizni oldingiz! 🎁", show_alert=True)
            await query.message.reply_text("🎬 Videongizni yuboring — PREMIUM tahlil qilaman! 👇")
        elif bajarilgan < 5:
            # 5 kunni to'liq o'tmagan - premium berilmaydi
            await query.answer("Marafon to'liq bajarilmagan 😔", show_alert=True)
            await query.message.reply_text(
                f"😔 <b>Afsuski, PREMIUM sovg'a berilmaydi.</b>\n\n"
                f"Siz {bajarilgan}/5 kunni bajardingiz. PREMIUM sovg'a uchun "
                f"HAR 5 kunni ham to'liq o'tish kerak edi (biz buni boshida ogohlantirgandik).\n\n"
                f"Lekin xafa bo'lmang! 💙 Siz baribir ko'p narsa o'rgandingiz. "
                f"Premium'ni sinab ko'rish uchun 7 kunlik tarifdan foydalanishingiz mumkin 👇",
                parse_mode="HTML")
            await asyncio.sleep(1)
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("⚡ 7 kunlik Premium — 6,990", callback_data="marafon_7kun")]])
            await query.message.reply_text(
                "🔥 <b>Premium'ning KUCHINI 1 HAFTA sinab ko'ring!</b>\n\n"
                "Tasavvur qiling: har bir videongiz uchun 👇\n"
                "🎯 Hook nega ishlamayotgani — aniq sabab\n"
                "🔊 Ovozli professional maslahat\n"
                "✍️ 3 ta TAYYOR hook — ko'chirib ishlatasiz!\n"
                "♾ Cheksiz tahlil — xohlagancha!\n\n"
                "📈 Marafondan o'tган bloglar aynan shu bilan ko'rishini 3-5 barobar oshirmoqda.\n\n"
                "💎 Atigi <b>6,990 so'm</b> — bir chashka kofe puliga 1 hafta Premium!",
                reply_markup=kb, parse_mode="HTML")
            return
        else:
            # 5 kun TO'LIQ bajarildi - premium beriladi!
            add_premium_balance(uid, 1)
            # 19,900 chegirma yoqamiz (24 soat) + sanani belgilaymiz (48 soat keyin 6,990 uchun)
            hozir = datetime.now()
            _db_execute("UPDATE users SET marafon_tugadi = TRUE, marafon_19900_sana = %s WHERE user_id = %s",
                        (hozir.strftime("%Y-%m-%d %H:%M:%S"), uid))
            set_setting("chegirma_until", (hozir + timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S"))
            await query.answer("✅ PREMIUM tahlil qo'shildi!")
            for aid in ADMIN_IDS:
                try:
                    uname = query.from_user.username or query.from_user.first_name or ""
                    who = f"@{uname}" if uname else f"ID {uid}"
                    await context.bot.send_message(aid, f"🏁 MARAFON TUGATDI (5/5, premium oldi)\n👤 {who}")
                except Exception:
                    pass
            await query.message.reply_text(
                "🎬 Zo'r! Videongizni yuboring — bu 1 ta PREMIUM tahlil (hook + ovoz)! 👇")
            await asyncio.sleep(1)
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("💎 19,900 ga 1 oylik olish", callback_data="buy_sub_19")
            ]])
            await query.message.reply_text(
                "🔥 <b>Marafonni tugatdingiz — PREMIUM kuchini his qildingiz!</b> 🎉\n\n"
                "Endi to'liq imkoniyatni oching:\n"
                "♾ Cheksiz tahlil — kuniga xohlagancha! 🎬\n"
                "🎯 Har video uchun hook + ovoz + 3 tayyor variant! 🔊\n"
                "📈 Marafonchilar ko'rishini 3-5 barobar oshirmoqda! 🚀\n\n"
                "🎁 <b>MAXSUS MARAFON SOVG'ASI:</b>\n"
                "1 oylik Premium\n"
                "❌ <s>29,900</s> → ✅ <b>19,900 so'm</b> 🔥\n\n"
                "⏰ Bu chegirma faqat 24 soat — marafonni tugatganingiz uchun! 💪\n"
                "Pastdagi tugmani bosing 👇",
                reply_markup=kb, parse_mode="HTML")
    elif data == 'marafon_7kun':
        # 7 kunlik to'lov (faqat marafon oxirida) - to'lov turlarini ko'rsatamiz
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(t(context, 'payme_choose'), callback_data='pm_test')],
            [InlineKeyboardButton(t(context, 'click_choose'), callback_data='cl_test')],
            [InlineKeyboardButton(t(context, 'card_choose'), callback_data='card_test')],
        ])
        await query.message.reply_text(
            "💎 <b>7 kunlik Premium — 6,990 so'm</b>\n\n👇 To'lov turini tanlang:",
            reply_markup=kb, parse_mode="HTML")
    elif data == 'test_sorov_start':
        # Test so'rovi boshlanadi - so'rov xabarini O'CHIRAMIZ (chat toza qolsin)
        try:
            await query.message.delete()
        except Exception:
            pass
        context.user_data['mode'] = 'test_sorov_q1'
        await query.message.chat.send_message(t(context, 'test_sorov_q1'), parse_mode="HTML")
    elif data == 'sorov_start':
        # So'rovga javob berishni boshlaydi - 1-savol javobini kutamiz
        context.user_data['mode'] = 'sorov_q1'
        await query.message.reply_text(t(context, 'sorov_q1_ask'))
    elif data == 'eslatma_ru':
        # Eslatmani ruschaga o'zgartiramiz (o'sha xabar)
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(TEXTS['uz']['eslatma_uz_btn'], callback_data="eslatma_uz")
        ]])
        try:
            await query.edit_message_text(TEXTS['uz']['eslatma_ru'], reply_markup=kb, parse_mode="HTML")
        except Exception:
            pass
    elif data == 'eslatma_uz':
        # Eslatmani o'zbekchaga qaytaramiz
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(TEXTS['uz']['eslatma_ru_btn'], callback_data="eslatma_ru")
        ]])
        try:
            await query.edit_message_text(TEXTS['uz']['eslatma_uz'], reply_markup=kb, parse_mode="HTML")
        except Exception:
            pass
    elif data.startswith('full_'):
        try:
            aid = int(data.split('_', 1)[1])
        except Exception:
            return
        toliq = get_full_analysis(aid)
        if not toliq:
            await query.message.reply_text(t(context, 'full_gone'))
            return
        # Oxiriga bot havolasini qo'shamiz (ulashsa - reklama)
        toliq_text = toliq + t(context, 'analyzed_footer')
        # Tugmalar: Qisqaga qaytish + audio
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(t(context, 'qisqa_btn'), callback_data=f"qisqa_{aid}")],
            [InlineKeyboardButton(t(context, 'yaxshilash_btn'), callback_data=f"yax_{aid}")],
            [InlineKeyboardButton(t(context, 'tts_full_btn'), callback_data=f"ttsf_{aid}")],
        ])
        # O'SHA xabarni to'liqqa o'zgartiramiz (yangi xabar emas, kasha bo'lmasin).
        # Agar juda uzun bo'lsa - 4000 belgiga kesamiz (bitta xabar, edit ishlaydi).
        if len(toliq_text) > 4000:
            toliq_text = toliq_text[:3950] + "\n\n… (to'liq tahlil)"
        try:
            await query.edit_message_text(toliq_text, reply_markup=kb)
        except Exception:
            try:
                await query.message.reply_text(toliq_text, reply_markup=kb)
            except Exception:
                pass
    elif data.startswith('goya_'):
        # ADMIN TEST: 5 ta Reels g'oyasi (premium, oyda 10 marta)
        uid = query.from_user.id
        premium = is_admin(uid) or sub_active(uid)
        if not premium:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("💎 Premiumga o'tish — 29,900", callback_data="buy_sub")]])
            await query.message.reply_text(
                "💡 <b>Reels g'oyalari</b> — bu Premium funksiya! 💎\n\n"
                "Bot sizga video mavzusi asosida 5 ta yangi Reels g'oyasi beradi 🎬✨\n\n"
                "Premium'ga o'tib, g'oyalar oqimini oching! 🚀",
                reply_markup=kb, parse_mode="HTML")
            return
        try:
            aid = int(data.split('_', 1)[1])
        except Exception:
            return
        # Oyda 10 marta cheklovi
        shu_oy = datetime.now().strftime("%Y-%m")
        _st = _db_execute("SELECT goya_oy, COALESCE(goya_marta,0) FROM users WHERE user_id = %s", (uid,), fetch='one')
        oy_saqlangan = _st[0] if _st else None
        marta = _st[1] if _st else 0
        if oy_saqlangan != shu_oy:
            marta = 0
        if marta >= 10 and not is_admin(uid):
            await query.message.reply_text(
                "💡 <b>Reels g'oyalari</b>\n\nBu oy limitni ishlatib bo'lgansiz (oyiga 10 marta). "
                "Keyingi oy yana foydalaning! 🗓", parse_mode="HTML")
            return
        # Tahlil matnini olamiz (mavzu uchun)
        qisqa = get_qisqa_analysis(aid) or ""
        await query.message.reply_text("💡 Sizga mos Reels g'oyalarini o'ylayapman... ⏳")
        prompt = (
            f"Sen Instagram Reels bo'yicha ijodiy mutaxassissan. Quyida bir blogerning videosi tahlili:\n"
            f"{qisqa[:500]}\n\n"
            f"Shu blogerning mavzusi/uslubiga MOS 5 ta YANGI Reels g'oyasi ber (o'zbek tilida, emoji bilan).\n"
            f"Har bir g'oya: qisqa sarlavha + 1 jumla tushuntirish + taxminiy hook.\n"
            f"Qiziqarli, amaliy, trendga mos bo'lsin. Faqat 5 ta g'oya, boshqa gap yo'q.")
        try:
            javob = _generate(prompt)
            if not javob or len(javob.strip()) < 20:
                await query.message.reply_text("⚠️ Hozir g'oya berib bo'lmadi, biroz keyin urinib ko'ring.")
                return
            _db_execute("UPDATE users SET goya_oy = %s, goya_marta = %s WHERE user_id = %s",
                        (shu_oy, marta + 1, uid))
            await query.message.reply_text(
                f"💡 <b>SIZGA 5 TA REELS G'OYASI</b> 🎬\n━━━━━━━━━━━\n\n{javob}",
                parse_mode="HTML")
        except Exception:
            await query.message.reply_text("⚠️ Xatolik yuz berdi, biroz keyin urinib ko'ring.")
    elif data.startswith('eslat_'):
        # ADMIN TEST: Eslatma qo'shish (kalendar) - kun tanlash
        try:
            aid = int(data.split('_', 1)[1])
        except Exception:
            return
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📅 Ertaga", callback_data=f"esk_1_{aid}"),
             InlineKeyboardButton("📅 2 kun", callback_data=f"esk_2_{aid}")],
            [InlineKeyboardButton("📅 3 kun", callback_data=f"esk_3_{aid}"),
             InlineKeyboardButton("📅 1 hafta", callback_data=f"esk_7_{aid}")],
        ])
        await query.message.reply_text(
            "📅 <b>Eslatma qo'shish</b>\n\nBu g'oyani qachon suratga olishni rejalashtiryapsiz? "
            "Bot o'sha kuni eslatadi! 👇", reply_markup=kb, parse_mode="HTML")
    elif data.startswith('esk_'):
        # Eslatma kun tanlandi: esk_KUN_aid
        uid = query.from_user.id
        try:
            _, kun_s, aid_s = data.split('_', 2)
            kun = int(kun_s)
        except Exception:
            return
        eslatma_vaqt = (datetime.now() + timedelta(days=kun)).strftime("%Y-%m-%d %H:%M:%S")
        _db_execute(
            "INSERT INTO eslatmalar (user_id, matn, eslatma_vaqt, yuborildi, created) "
            "VALUES (%s, %s, %s, FALSE, %s)",
            (uid, "Reels g'oyasi", eslatma_vaqt,
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        kun_matn = {1: "ertaga", 2: "2 kundan keyin", 3: "3 kundan keyin", 7: "1 haftadan keyin"}.get(kun, f"{kun} kundan keyin")
        await query.answer("✅ Eslatma qo'shildi!")
        await query.message.reply_text(
            f"✅ <b>Eslatma qo'shildi!</b> 📅\n\n"
            f"Bot sizga <b>{kun_matn}</b> eslatadi — Reels suratga olishni unutmang! 🎬🔥",
            parse_mode="HTML")
    elif data.startswith('qisqa_'):
        # To'liqdan qisqaga qaytish - o'sha xabarni qisqa tahlilga o'zgartiramiz
        try:
            aid = int(data.split('_', 1)[1])
        except Exception:
            return
        qisqa = get_qisqa_analysis(aid)
        if not qisqa:
            return
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(t(context, 'full_btn'), callback_data=f"full_{aid}")],
            [InlineKeyboardButton(t(context, 'yaxshilash_btn'), callback_data=f"yax_{aid}")],
            [InlineKeyboardButton(t(context, 'tts_full_btn'), callback_data=f"ttsf_{aid}")],
        ])
        if len(qisqa) <= 4000:
            try:
                await query.edit_message_text(qisqa, reply_markup=kb)
            except Exception:
                await query.message.reply_text(qisqa, reply_markup=kb)
        else:
            await query.message.reply_text(qisqa, reply_markup=kb)
    elif data.startswith('advid_'):
        # Admin: tahlil qilingan videoni ko'rish (nazorat uchun)
        if not is_admin(query.from_user.id):
            return
        try:
            aid = int(data.split('_', 1)[1])
        except Exception:
            return
        info = get_analysis_video(aid)
        if not info or not info.get("file_id"):
            await query.message.reply_text("😔 Video topilmadi (eski tahlil bo'lishi mumkin).")
            return
        try:
            cap = f"🎬 Tahlil #{aid}\n👤 @{info.get('username') or '-'} (ID: {info.get('user_id')})\n📈 Natija: {info.get('foiz')}%"
            await query.message.reply_video(info["file_id"], caption=cap)
        except Exception as e:
            logger.warning(f"Admin video ko'rsatishda xato: {e}")
            await query.message.reply_text("😔 Videoni yuborib bo'lmadi (muddati o'tgan bo'lishi mumkin).")
        return
    elif data.startswith('yax_'):
        # "Qanday yaxshilash?" - faqat PREMIUM (admin yoki obunachi)
        if not (is_admin(query.from_user.id) or has_access(query.from_user.id) in ('admin', 'sub')):
            premium_kb = InlineKeyboardMarkup([[
                InlineKeyboardButton(t(context, 'obuna_taklif_btn'), callback_data="buy_sub")
            ]])
            await query.message.reply_text(t(context, 'yaxshilash_premium'),
                                           reply_markup=premium_kb, parse_mode="HTML")
            return
        try:
            aid = int(data.split('_', 1)[1])
        except Exception:
            return
        toliq = get_full_analysis(aid) or get_qisqa_analysis(aid)
        if not toliq:
            await query.message.reply_text(t(context, 'full_gone'))
            return
        wait = await query.message.reply_text(t(context, 'yaxshilash_loading'))
        try:
            lang = get_lang(context)
            bonus_prompt = (PROMPT_PREMIUM_RU if lang == 'ru' else PROMPT_PREMIUM_UZ)
            # Saqlangan tahlil asosida hook taklifini so'raymiz (video qayta kerak emas)
            asos = (f"Quyida videoning tahlili berilgan. Shu asosda hook yaxshilash bo'limini yoz.\n\n"
                    f"TAHLIL:\n{toliq}\n") if lang != 'ru' else (
                    f"Ниже анализ видео. На его основе напиши раздел улучшения хука.\n\nАНАЛИЗ:\n{toliq}\n")
            txt = await asyncio.to_thread(_generate, [asos + bonus_prompt],
                                          3, "gemini-2.5-flash")
            await wait.edit_text(txt if txt else "😔 Hozir tayyorlab bo'lmadi, qayta urinib ko'ring.")
        except Exception as e:
            logger.warning(f"Yaxshilash xato: {e}")
            await wait.edit_text("😔 Hozir tayyorlab bo'lmadi, qayta urinib ko'ring.")
        return
    elif data.startswith('tts_'):
        # Audio faqat PREMIUM (admin yoki obunachi) uchun
        if not (is_admin(query.from_user.id) or has_access(query.from_user.id) in ('admin', 'sub')):
            premium_kb = InlineKeyboardMarkup([[
                InlineKeyboardButton(t(context, 'obuna_taklif_btn'), callback_data="buy_sub")
            ]])
            await query.message.reply_text(t(context, 'tts_premium'), reply_markup=premium_kb)
            return
        try:
            aid = int(data.split('_', 1)[1])
        except Exception:
            return
        qisqa = get_qisqa_analysis(aid)
        if not qisqa:
            await query.message.reply_text(t(context, 'full_gone'))
            return
        loading = await query.message.reply_text(t(context, 'tts_loading'))
        # TTS tarmoq ishi - alohida thread'da (botni muzlatmaslik uchun)
        wav = await asyncio.to_thread(_gemini_tts, qisqa)
        if not wav:
            await loading.edit_text(t(context, 'tts_fail'))
            return
        try:
            bio = io.BytesIO(wav)
            bio.name = "tahlil.wav"
            await context.bot.send_audio(query.message.chat_id, audio=bio,
                                         title="InstaDoctor tahlili")
            await loading.delete()
        except Exception as e:
            logger.error(f"Audio yuborishda xato: {e}")
            await loading.edit_text(t(context, 'tts_fail'))
    elif data.startswith('ttsf_'):
        # Audio faqat PREMIUM (admin yoki obunachi) uchun
        if not (is_admin(query.from_user.id) or has_access(query.from_user.id) in ('admin', 'sub')):
            premium_kb = InlineKeyboardMarkup([[
                InlineKeyboardButton(t(context, 'obuna_taklif_btn'), callback_data="buy_sub")
            ]])
            await query.message.reply_text(t(context, 'tts_premium'), reply_markup=premium_kb)
            return
        try:
            aid = int(data.split('_', 1)[1])
        except Exception:
            return
        toliq = get_full_analysis(aid)
        if not toliq:
            await query.message.reply_text(t(context, 'full_gone'))
            return
        loading = await query.message.reply_text(t(context, 'tts_loading'))
        # To'liq tahlil uchun ko'proq matn (3000 belgigacha)
        wav = await asyncio.to_thread(_gemini_tts, toliq, 3000)
        if not wav:
            await loading.edit_text(t(context, 'tts_fail'))
            return
        try:
            bio = io.BytesIO(wav)
            bio.name = "toliq_tahlil.wav"
            await context.bot.send_audio(query.message.chat_id, audio=bio,
                                         title="InstaDoctor to'liq tahlil")
            await loading.delete()
        except Exception as e:
            logger.error(f"To'liq audio yuborishda xato: {e}")
            await loading.edit_text(t(context, 'tts_fail'))
    elif data == 'profile_analyze':
        user_id = query.from_user.id
        imgs = context.user_data.get('profile_imgs', [])
        if not imgs:
            await query.message.reply_text(t(context, 'profile_none'))
            return
        # Profil tahlil FAQAT PREMIUM (balansdan yechilmaydi)
        if not (is_admin(user_id) or has_access(user_id) in ('admin', 'sub')):
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton(t(context, 'obuna_taklif_btn'), callback_data="buy_sub")
            ]])
            await query.message.reply_text(t(context, 'profile_premium'), reply_markup=kb, parse_mode="HTML")
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
                # Mavzu qo'lda berilgan bo'lsa - promptga qo'shamiz
                mavzu = context.user_data.get('profile_mavzu')
                if mavzu:
                    prompt = prompt + f"\n\nFoydalanuvchi profil mavzusini aytdi: {mavzu}"
                tahlil, _pusage = await asyncio.to_thread(_gemini_process_images, tmp_paths, prompt)

                if not tahlil or not tahlil.strip():
                    raise Exception("Profil tahlili bo'sh keldi")

                # Gemini mavzuni tushunmagan bo'lsa - mavzu so'raymiz (faqat 1 marta)
                if "[MAVZU_KERAK]" in tahlil and not mavzu:
                    await wait_msg.delete()
                    context.user_data['mode'] = 'profile_mavzu'
                    await query.message.reply_text(t(context, 'profile_mavzu_ask'))
                    return
                # Mavzu berilgan, lekin Gemini yana [MAVZU_KERAK] qaytargan bo'lsa -
                # uni tozalaymiz va mavzuni hisobga olib qayta yo'naltiramiz (mijozga ko'rinmasin)
                if "[MAVZU_KERAK]" in tahlil:
                    tahlil = tahlil.replace("[MAVZU_KERAK]", "").strip()
                    if not tahlil:
                        # Bo'sh qoldi - mavzu bilan qayta urinish
                        prompt2 = (PROMPT_PROFILE_RU if get_lang(context) == 'ru' else PROMPT_PROFILE_UZ)
                        prompt2 += (f"\n\nFoydalanuvchi profil mavzusini aniq aytdi: {mavzu}. "
                                    f"Endi [MAVZU_KERAK] YOZMA — shu mavzu asosida to'liq tahlil ber.")
                        tahlil, _pusage = await asyncio.to_thread(_gemini_process_images, tmp_paths, prompt2)
                        tahlil = (tahlil or "").replace("[MAVZU_KERAK]", "").strip()
                    if not tahlil:
                        await wait_msg.edit_text("😔 Profilni tahlil qilib bo'lmadi. Aniqroq skrinshot bilan qayta urinib ko'ring.")
                        context.user_data.pop('profile_mavzu', None)
                        return

                await wait_msg.edit_text(t(context, 'ready'))
                _uname = query.from_user.username or query.from_user.first_name or ""
                # Token va tannarxni hisoblaymiz (premium_xarajat hisobida ko'rinishi uchun)
                _p_tok = _pusage.get("prompt", 0)
                _o_tok = _pusage.get("output", 0)
                _tot = _pusage.get("total", 0) or (_p_tok + _o_tok)
                _usd, _uzs = _cost_uzs(_p_tok, _o_tok, _pusage.get("model", "gemini-2.5-flash"))
                save_analysis(user_id, username=_uname, kind="profile", toliq=tahlil,
                              tokens=_tot, narx=_uzs)
                # Adminlarga profil tannarx hisoboti
                try:
                    _rep = (f"📊 Tannarx (PROFIL tahlil)\n👤 @{_uname} (ID: {user_id})\n"
                            f"🔢 Jami: {_tot:,} token\n💰 ≈ {_uzs:,.0f} so'm")
                    for _a in ADMIN_IDS:
                        try:
                            await context.bot.send_message(_a, _rep)
                        except Exception:
                            pass
                except Exception:
                    pass
                context.user_data['mode'] = None
                context.user_data['profile_imgs'] = []
                context.user_data['profile_mavzu'] = None

                if len(tahlil) <= 4000:
                    try:
                        await query.message.reply_text(tahlil, parse_mode="HTML")
                    except Exception:
                        await query.message.reply_text(tahlil)
                else:
                    for i in range(0, len(tahlil), 4000):
                        try:
                            await query.message.reply_text(tahlil[i:i+4000], parse_mode="HTML")
                        except Exception:
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
        package = '_'.join(parts[2:]) if len(parts) > 2 else ''
        if package.startswith('one'):
            # 1 martalik tahlil: hisobga +1 qo'shamiz
            add_balance(target_user, 1)
            new_bal = get_balance(target_user)
            try:
                await context.bot.send_message(
                    target_user,
                    f"🎉 To'lovingiz tasdiqlandi! Hisobingizga 1 ta tahlil qo'shildi.\n💰 Jami bepul tahlillar: {new_bal} ta"
                )
            except Exception as e:
                logger.error(f"Foydalanuvchiga xabar yuborilmadi: {e}")
            await query.message.reply_text(f"✅ Tasdiqlandi! {target_user} ga 1 ta tahlil qo'shildi.")
        else:
            # Obunani faollashtiramiz (30 kun, faol bo'lsa uzaytiriladi)
            until = activate_subscription(target_user, SUB_DAYS)
            try:
                await context.bot.send_message(
                    target_user,
                    TEXTS['uz']['approved'].format(until=until)
                )
            except Exception as e:
                logger.error(f"Foydalanuvchiga xabar yuborilmadi: {e}")
            await query.message.reply_text(f"✅ Tasdiqlandi! {target_user} ning obunasi {until} gacha faollashtirildi.")


def _upload_and_wait(tmp_path, max_retries=3, max_wait=300):
    """Videoni Gemini'ga tayyorlaydi.
    - Vertex AI: video GCS bucket'ga yuklanadi, GCS URI (Part) qaytadi.
    - AI Studio: client.files.upload (eski usul), ACTIVE bo'lguncha kutadi.
    QAYTARADI: Vertex'da (gcs_uri, blob_name) tuple, AI Studio'da uploaded obyekt."""
    last_error = None
    # ===== VERTEX AI: GCS orqali =====
    if _using_vertex and _gcs_client is not None:
        for attempt in range(max_retries):
            try:
                bucket = _gcs_client.bucket(GCS_BUCKET)
                blob_name = f"videos/{uuid.uuid4().hex}.mp4"
                blob = bucket.blob(blob_name)
                blob.upload_from_filename(tmp_path)
                gcs_uri = f"gs://{GCS_BUCKET}/{blob_name}"
                return (gcs_uri, blob_name)
            except Exception as e:
                last_error = e
                logger.warning(f"GCS upload urinish {attempt+1}/{max_retries}: {e}")
                time.sleep((attempt + 1) * 3)
        raise last_error
    # ===== AI STUDIO: eski usul =====
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


def _gcs_delete(blob_name):
    """GCS'dan videoni o'chiradi (tahlildan keyin, joy band qilmasin)."""
    try:
        if _gcs_client is not None and blob_name:
            _gcs_client.bucket(GCS_BUCKET).blob(blob_name).delete()
    except Exception as e:
        logger.warning(f"GCS o'chirishda xato: {e}")


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


TTS_VOICE = os.getenv("TTS_VOICE", "Kore")  # Gemini ovoz nomi (Railway'dan o'zgartirsa bo'ladi)


def _clean_for_tts(text):
    """TTS uchun matnni tozalaydi: emoji va ortiqcha belgilarni olib tashlaydi."""
    # \w Unicode harflar (lotin/kirill) + raqamlarni qoldiradi; emoji/belgilar olib tashlanadi
    text = re.sub(r"[^\w\s.,!?%:;'\-]", " ", text, flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _gemini_tts(text, max_chars=1500):
    """Matnni Gemini TTS bilan ovozga aylantiradi. WAV bytes qaytaradi yoki None."""
    if genai_types is None:
        return None
    try:
        text = _clean_for_tts(text)
        if not text:
            return None
        if len(text) > max_chars:        # uzun matnni qisqartiramiz (narx va limit uchun)
            text = text[:max_chars]
        config = genai_types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=genai_types.SpeechConfig(
                voice_config=genai_types.VoiceConfig(
                    prebuilt_voice_config=genai_types.PrebuiltVoiceConfig(voice_name=TTS_VOICE)
                )
            ),
        )
        resp = client.models.generate_content(
            model="gemini-2.5-flash-preview-tts",
            contents=text,
            config=config,
        )
        pcm = resp.candidates[0].content.parts[0].inline_data.data  # 24kHz 16-bit mono PCM
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(24000)
            wf.writeframes(pcm)
        buf.seek(0)
        return buf.read()
    except Exception as e:
        logger.error(f"Gemini TTS xato: {e}")
        return None


# --- Tannarx hisobi (Gemini 2.5 Flash rasmiy narxi) ---
# 1 million token narxi (dollar): kiruvchi $0.30, chiquvchi $2.50
PRICE_IN_PER_TOKEN = 0.30 / 1_000_000
PRICE_OUT_PER_TOKEN = 2.50 / 1_000_000
# Flash-Lite narxi (arzon): kiruvchi $0.10, chiquvchi $0.40
PRICE_IN_LITE = 0.10 / 1_000_000
PRICE_OUT_LITE = 0.40 / 1_000_000
# Dollar kursi (so'm) - Railway'dan o'zgartirsa bo'ladi
USD_TO_UZS = float(os.getenv("USD_TO_UZS", "12000"))
# Oxirgi so'rovning token sarfi va modeli (admin hisobotida ishlatiladi)
_last_usage = {"prompt": 0, "output": 0, "total": 0, "model": "gemini-2.5-flash"}

# ===== XATO HISOBLAGICH (avtomatik kuzatuv) =====
_xato_hisob = {"503": 0, "timeout": 0, "fayl": 0, "boshqa": 0, "sana": ""}
_xato_oxirgi_ogoh = {"vaqt": None}  # oxirgi ogohlantirish vaqti (spam bo'lmasin)


def _xato_qayd(tur):
    """Xatoni sanab boradi. tur: '503' | 'timeout' | 'fayl' | 'boshqa'."""
    bugun = datetime.now().strftime("%Y-%m-%d")
    if _xato_hisob.get("sana") != bugun:
        # Yangi kun - hisobni nolga tushiramiz
        _xato_hisob["503"] = 0
        _xato_hisob["timeout"] = 0
        _xato_hisob["fayl"] = 0
        _xato_hisob["boshqa"] = 0
        _xato_hisob["sana"] = bugun
    _xato_hisob[tur] = _xato_hisob.get(tur, 0) + 1


async def _xato_ogohlantir(context):
    """503 ko'paysa adminga avtomatik ogohlantirish (10 daqiqada 1 marta, spam emas)."""
    try:
        now = datetime.now()
        oxirgi = _xato_oxirgi_ogoh.get("vaqt")
        if oxirgi and (now - oxirgi).total_seconds() < 600:
            return  # 10 daqiqa ichida ogohlantirgan - takrorlamaymiz
        # 503 bugun 30 dan oshsa - ogohlantiramiz
        if _xato_hisob.get("503", 0) > 0 and _xato_hisob["503"] % 30 == 0:
            _xato_oxirgi_ogoh["vaqt"] = now
            for aid in ADMIN_IDS:
                try:
                    await context.bot.send_message(
                        aid, f"⚠️ DIQQAT: Gemini 503 (band) xatosi ko'paydi!\n"
                             f"Bugun: {_xato_hisob['503']} marta 503.\n"
                             f"Bu Google tomonidagi yuklama. Bot Flash'ga o'tib ishlamoqda.")
                except Exception:
                    pass
    except Exception:
        pass


def _cost_uzs(prompt_tokens, output_tokens, model="gemini-2.5-flash"):
    """Token sonidan taxminiy tannarxni (so'm) hisoblaydi. VAT (~11%) ham qo'shiladi.
    Flash-Lite uchun arzon narx ishlatiladi."""
    if "lite" in model:
        usd = prompt_tokens * PRICE_IN_LITE + output_tokens * PRICE_OUT_LITE
    else:
        usd = prompt_tokens * PRICE_IN_PER_TOKEN + output_tokens * PRICE_OUT_PER_TOKEN
    usd_with_vat = usd * 1.11  # Google VAT (soliq) ~11%
    return usd_with_vat, usd_with_vat * USD_TO_UZS


def _generate(contents, max_retries=3, model="gemini-2.5-flash"):
    """Gemini'ga so'rov yuboradi (qayta urinish + bo'sh javobni ushlash + safety bilan).
    503 (band) bo'lsa - qisqa kutib qayta urinadi. Uzoq kutmaymiz (tez javob muhim).
    model: 'gemini-2.5-flash' (sifatli) yoki 'gemini-2.5-flash-lite' (arzon)."""
    last_error = None
    for attempt in range(max_retries):
        try:
            kwargs = {"model": model, "contents": contents}
            if genai_types is not None:
                try:
                    if SAFETY_SETTINGS is not None:
                        kwargs["config"] = genai_types.GenerateContentConfig(
                            safety_settings=SAFETY_SETTINGS, temperature=0.3)
                    else:
                        kwargs["config"] = genai_types.GenerateContentConfig(temperature=0.3)
                except Exception:
                    pass
            response = client.models.generate_content(**kwargs)
            try:
                um = getattr(response, "usage_metadata", None)
                if um is not None:
                    p_tok = getattr(um, "prompt_token_count", 0) or 0
                    c_tok = getattr(um, "candidates_token_count", 0) or 0
                    think_tok = getattr(um, "thoughts_token_count", 0) or 0
                    _last_usage["prompt"] = p_tok
                    _last_usage["output"] = c_tok + think_tok
                    _last_usage["total"] = getattr(um, "total_token_count", 0) or (p_tok + c_tok + think_tok)
                    _last_usage["model"] = model
            except Exception:
                pass
            text = _extract_text(response)
            if text:
                return text
            raise Exception("Bo'sh javob keldi")
        except Exception as e:
            last_error = e
            logger.warning(f"Generate urinish {attempt+1}/{max_retries} (model={model}): {e}")
            if attempt < max_retries - 1:
                time.sleep((attempt + 1) * 2)  # 2, 4 sek (qisqa - tez javob)
    raise last_error


def _analyze(uploaded_file, prompt, max_retries=4, model="gemini-2.5-flash"):
    """Bitta video uchun tahlil.
    Vertex: uploaded_file = (gcs_uri, blob_name) -> GCS URI Part yasaymiz.
    AI Studio: uploaded_file = uploaded obyekt (to'g'ridan ishlatamiz)."""
    if _using_vertex and isinstance(uploaded_file, tuple):
        gcs_uri = uploaded_file[0]
        # Vertex uchun: GCS URI'dan video Part yasaymiz
        try:
            video_part = genai_types.Part.from_uri(file_uri=gcs_uri, mime_type="video/mp4")
        except Exception:
            # Ba'zi SDK versiyalarida boshqacha
            video_part = genai_types.Part(file_data=genai_types.FileData(
                file_uri=gcs_uri, mime_type="video/mp4"))
        return _generate([video_part, prompt], max_retries=max_retries, model=model)
    return _generate([uploaded_file, prompt], max_retries=max_retries, model=model)


def _gemini_process_images(image_paths, prompt):
    """BLOKLAYDIGAN: bir nechta rasm (skrinshot)ni Gemini bilan tahlil qiladi.
    Faqat alohida thread'da chaqiriladi (asyncio.to_thread).
    QAYTARADI: (matn, usage_dict) - global aralashmasin."""
    uploaded = []
    try:
        for p in image_paths:
            uploaded.append(_upload_and_wait(p))
        txt = _generate(uploaded + [prompt])
        usage = {
            "prompt": _last_usage.get("prompt", 0),
            "output": _last_usage.get("output", 0),
            "total": _last_usage.get("total", 0),
            "model": _last_usage.get("model", "gemini-2.5-flash"),
        }
        return txt, usage
    finally:
        for u in uploaded:
            try:
                client.files.delete(name=u.name)
            except Exception:
                pass


def _gemini_process(tmp_path, prompt, model="gemini-2.5-flash"):
    """BLOKLAYDIGAN to'liq Gemini ishi. Faqat alohida thread'da chaqiriladi
    (asyncio.to_thread), shunda bot muzlamaydi va Pyrogram uzilmaydi.
    model: bepul -> flash-lite (arzon), pullik -> flash (sifatli).
    MUHIM: model O'ZGARMAYDI (fallback yo'q). Chunki model almashsa, foiz (%) ham
    o'zgaradi -> bir xil video har xil natija beradi -> foydalanuvchi ishonmaydi.
    Shuning uchun 503 (band) bo'lsa - SHU modelda qayta urinamiz (foiz barqaror).
    QAYTARADI: (matn, usage_dict) - usage shu tahlilniki (global aralashmasin)."""
    uploaded = None
    used_model = model
    try:
        uploaded = _upload_and_wait(tmp_path)
        # Vertex AI'da 503 (band) deyarli yo'q. Shuning uchun model O'ZGARMAYDI:
        # Flash-Lite (bepul) o'z modelida qoladi -> foiz barqaror + xarajat kam
        # (Flash'ga o'tib qimmatlashmaydi). 503 bo'lsa shu modelda qayta urinadi.
        txt = _analyze(uploaded, prompt, model=model, max_retries=3)
        # Token sarfini shu yerda NUSXALAB olamiz (global _last_usage aralashmasin)
        usage = {
            "prompt": _last_usage.get("prompt", 0),
            "output": _last_usage.get("output", 0),
            "total": _last_usage.get("total", 0),
            "model": used_model,  # haqiqatda ishlatilgan model
        }
        return txt, usage
    finally:
        if uploaded is not None:
            if _using_vertex and isinstance(uploaded, tuple):
                # Vertex: GCS'dan videoni o'chiramiz (blob_name = uploaded[1])
                _gcs_delete(uploaded[1])
            else:
                try:
                    client.files.delete(name=uploaded.name)
                except Exception:
                    pass


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Profil skrinshoti YOKI to'lov cheki (karta orqali)."""
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
    # KARTA CHEKI REJIMI: rasm = to'lov cheki -> guruhga uzatamiz
    if context.user_data.get('mode') == 'card_receipt':
        user = update.effective_user
        pkg = context.user_data.get('card_paket', 'sub_1month')
        paket_nom = context.user_data.get('card_paket_nom', '1 oylik obuna')
        summa = context.user_data.get('card_summa', SUB_PRICE)
        uname = user.username or user.first_name or "—"
        photo_id = update.message.photo[-1].file_id
        # Foydalanuvchiga tasdiq
        await update.message.reply_text(t(context, 'receipt_sent'))
        # Guruhga chek + tasdiqlash tugmalari (admin tanlaydi/o'zgartiradi)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"✅ Tasdiqlash ({paket_nom})",
                                  callback_data=f"chok_{user.id}_{pkg}")],
            [InlineKeyboardButton("1 oylik", callback_data=f"chok_{user.id}_sub_1month"),
             InlineKeyboardButton("7 kun", callback_data=f"chok_{user.id}_test_7day"),
             InlineKeyboardButton("1 ta", callback_data=f"chok_{user.id}_one_1")],
            [InlineKeyboardButton("❌ Rad etish", callback_data=f"chrad_{user.id}")],
        ])
        caption = (f"💳 YANGI CHEK (karta to'lov)\n"
                   f"👤 @{uname} (ID: {user.id})\n"
                   f"📦 Tanlangan: {paket_nom}\n"
                   f"💰 Summa: {summa:,} so'm\n\n"
                   f"Tekshiring va tasdiqlang (yoki paketni o'zgartiring):")
        try:
            await context.bot.send_photo(CHEK_GROUP_ID, photo_id, caption=caption,
                                         reply_markup=kb)
        except Exception as e:
            logger.error(f"Chek guruhga yuborilmadi: {e}")
        context.user_data['mode'] = None
        return
    # Aks holda: rasmni e'tiborsiz qoldiramiz
    return


async def precheckout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Payme pre-checkout: to'lovni tasdiqlaymiz (10 soniya ichida javob berish shart)."""
    q = update.pre_checkout_query
    try:
        if q.invoice_payload in ("sub_1month", "one_1", "test_7day"):
            await q.answer(ok=True)
        else:
            await q.answer(ok=False, error_message="Noma'lum to'lov. Qaytadan urinib ko'ring.")
    except Exception as e:
        logger.error(f"Pre-checkout xato: {e}")
        try:
            await q.answer(ok=False, error_message="Xatolik. Qaytadan urinib ko'ring.")
        except Exception:
            pass


# Telegram konfetti xabar effekti (premium olganda kayf uchun) - YOQDI
PAYME_FIREWORKS_EFFECT = "5046509860389126442"  # 🎉 party popper (konfetti - ekranda sochiladi)
PAYME_EFFECT_CROWN = "5089422278802801345"      # 👑 toj
PAYME_EFFECT_DIAMOND = "4965608262968804599"    # 💎 olmos


async def _send_celebration(context, chat_id, text):
    """Tabrik xabarini fireworks effekti bilan yuboradi (ishlamasa - oddiy)."""
    try:
        # Telegram message effect (fireworks) - yangi Bot API
        await context.bot.send_message(
            chat_id, text, parse_mode="HTML",
            message_effect_id=PAYME_FIREWORKS_EFFECT
        )
        return
    except Exception:
        pass
    # Effekt ishlamasa - 🎉 animatsiyali emoji bilan oddiy xabar
    try:
        await context.bot.send_message(chat_id, text, parse_mode="HTML")
    except Exception:
        pass


async def successful_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """To'lov muvaffaqiyatli — obunani/tahlilni AVTOMATIK faollashtiramiz."""
    sp = update.message.successful_payment
    user = update.effective_user
    payload = sp.invoice_payload
    uname = user.username or user.first_name or ""
    # To'langan summa (tiyin -> so'm)
    paid_uzs = int(getattr(sp, "total_amount", 0)) // 100

    try:
        if payload == "sub_1month":
            new_until = activate_subscription(user.id, SUB_DAYS)
            await _send_celebration(context, update.effective_chat.id,
                                    t(context, 'celebrate_sub').format(until=new_until))
            try:
                create_payment(user.id, 'sub_1month', current_sub_price())
            except Exception:
                pass
            admin_txt = (f"💰 YANGI TO'LOV (Payme)\n👤 @{uname} (ID: {user.id})\n"
                         f"📦 1 oylik obuna\n💵 {paid_uzs:,} so'm\n✅ Obuna {new_until} gacha yoqildi")
        elif payload == "test_7day":
            new_until = activate_subscription(user.id, TEST_DAYS)
            await _send_celebration(context, update.effective_chat.id,
                                    t(context, 'celebrate_test').format(until=new_until))
            try:
                create_payment(user.id, 'test_7day', TEST_PRICE)
            except Exception:
                pass
            admin_txt = (f"💰 YANGI TO'LOV (Payme)\n👤 @{uname} (ID: {user.id})\n"
                         f"📦 7 kunlik test Premium\n💵 {paid_uzs:,} so'm\n✅ Obuna {new_until} gacha yoqildi")
        elif payload == "one_1":
            add_balance(user.id, 1)
            await _send_celebration(context, update.effective_chat.id,
                                    t(context, 'celebrate_one'))
            try:
                create_payment(user.id, 'one_1', ONE_PRICE)
            except Exception:
                pass
            admin_txt = (f"💰 YANGI TO'LOV (Payme)\n👤 @{uname} (ID: {user.id})\n"
                         f"📦 1 ta tahlil\n💵 {paid_uzs:,} so'm\n✅ +1 tahlil qo'shildi")
        else:
            logger.warning(f"Noma'lum to'lov payload: {payload}")
            return
        # Adminlarga xabar
        for _aid in ADMIN_IDS:
            try:
                await context.bot.send_message(_aid, admin_txt)
            except Exception:
                pass
    except Exception as e:
        logger.error(f"To'lovni faollashtirishda xato: {e}")
        await update.message.reply_text(t(context, 'error'))


def _recalc_foiz(qisqa_text, fallback_foiz):
    """QISQA'dagi 4 ta balldan (Hook, Vizual, Audio, Kontent) foizni hisoblaydi.
    Past ballar -> past foiz (Gemini'ning saxiy foizini kuchsizlantiramiz).
    Egri (1.3-daraja): yomon video pastroq, zo'r video balandroq tushadi."""
    scores = re.findall(r'(\d+(?:\.\d+)?)\s*/\s*10', qisqa_text)
    vals = [float(s) for s in scores if 0 <= float(s) <= 10]
    if not vals:
        return fallback_foiz
    avg = sum(vals) / len(vals)          # 0..10
    foiz = int(round((avg / 10.0) ** 1.3 * 100))
    return max(5, min(95, foiz))


def _replace_foiz_line(text, foiz):
    """Matndagi foiz qatorini yangi foizga almashtiradi (qisqa va to'liq uchun).
    Qisqa '📈 ... NN%' ishlatadi, to'liq '📊 ... NN%' — ikkalasini ham almashtiramiz."""
    if not text:
        return text
    out = []
    for line in text.split('\n'):
        # Qisqa (📈) yoki to'liq (📊) foiz qatorida % ni yangilaymiz
        if ('📈' in line or '📊' in line) and '%' in line:
            line = re.sub(r'\d+\s*%', f'{foiz}%', line, count=1)
        out.append(line)
    return '\n'.join(out)


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

    # ADMIN: /videoid rejimi - video kodini (file_id) ko'rsatadi (tahlil qilmaydi)
    if is_admin(user_id) and context.user_data.get('mode') == 'get_videoid':
        context.user_data['mode'] = None
        vid = message.video
        if vid:
            await message.reply_text(
                f"🎬 Video kodi (file_id):\n\n<code>{vid.file_id}</code>\n\n"
                f"Shu kodni menga (Claude) ayting — tahlildan keyin chiqadigan video qilaman.",
                parse_mode="HTML"
            )
        else:
            await message.reply_text("⚠️ Bu video emas. Qaytadan /videoid bosib, video yuboring.")
        return

    # Bloklangan foydalanuvchi - hech narsa qilmaymiz
    if is_blocked(user_id):
        return

    # Kirish tekshiruvi: admin / obuna faol / bepul tahlil bor bo'lsa - o'tadi
    if has_access(user_id) == 'none':
        await message.reply_text(t(context, 'no_balance'), reply_markup=package_keyboard(context), parse_mode="HTML")
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

        # Flash (sifatli) + reklamasiz FAQAT admin va FAOL OBUNA uchun.
        # Balans (credit) = bepul tahlil (referral/sovg'a) -> bepul kabi: Flash-Lite + reklama.
        _access = has_access(user_id)
        _is_priority = _access in ('admin', 'sub')

        # Video o'lcham chegarasi: admin cheksiz, pullik 300MB, bepul 100MB
        if not is_admin(user_id) and video.file_size:
            if _is_priority:
                if video.file_size > PAID_VIDEO_BYTES:
                    await wait_msg.edit_text(t(context, 'too_big_paid'), parse_mode="HTML")
                    return
            else:
                if video.file_size > FREE_VIDEO_BYTES:
                    kb = InlineKeyboardMarkup([[
                        InlineKeyboardButton(t(context, 'menu_premium'), callback_data="buy_sub")
                    ]])
                    await wait_msg.edit_text(t(context, 'too_big_free'),
                                             reply_markup=kb, parse_mode="HTML")
                    return

        _chosen_sem = _video_semaphore

        async with _chosen_sem:
            tmp_path = os.path.join("/tmp", f"{uuid.uuid4().hex}.mp4")
            is_big = bool(video.file_size and video.file_size > 20 * 1024 * 1024)
            prompt = PROMPT_RU if get_lang(context) == 'ru' else PROMPT_UZ
            # Hook bonus endi alohida "Qanday yaxshilash?" tugma orqali beriladi

            # Tahlil boshlanish vaqti
            _analiz_start = datetime.now()
            # Jonli progress mexanizmi - DARROV boshlanadi (yuklash paytida ham
            # sanoq ketadi, "qotgan" kabi ko'rinmaydi).
            _progress_stop = asyncio.Event()

            # Bepul uchun reklama matni (progress o'rtasida ko'rsatiladi)
            if not _is_priority:
                if discount_active():
                    _narx_q = ("\n\n🔥 FAQAT BUGUN — CHEGIRMA!\n"
                               "Eski narx: <s>29 900 so'm</s>\n"
                               "Yangi narx: <b>19 900 so'm/oy</b> 🎉\n"
                               "Bu — kuniga 650 so'mdan ham emas! ☕️")
                else:
                    _narx_q = "\n\n💎 Atigi 29 900 so'm/oy — kuniga 1 000 so'mdan ham emas! ☕️"
                _promo_kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton(t(context, 'obuna_taklif_btn'), callback_data="buy_sub")
                ]])

            async def _show_progress():
                steps = [
                    "🔍 Video ko'rilmoqda...",
                    "🎬 Vizual tahlil qilinmoqda...",
                    "🗣 Audio tinglanmoqda...",
                    "📊 Ballar hisoblanmoqda...",
                    "📈 REK ehtimoli aniqlanmoqda...",
                    "✍️ Tavsiyalar tayyorlanmoqda...",
                ]
                i = 0
                secs = 1  # darrov boshlanadi
                shown_ad = False
                try:
                    while not _progress_stop.is_set():
                        # BEPUL: reklamani BIRINCHI siklda darrov ko'rsatamiz.
                        if (not _is_priority) and (not shown_ad):
                            shown_ad = True
                            try:
                                _pm = await message.reply_text(
                                    t(context, 'queued_promo') + _narx_q,
                                    reply_markup=_promo_kb, parse_mode="HTML"
                                )
                                context.user_data['_promo_msg_id'] = _pm.message_id
                            except Exception:
                                pass
                        await asyncio.sleep(4)
                        if _progress_stop.is_set():
                            break
                        secs += 4
                        msg_step = steps[i % len(steps)]
                        i += 1
                        try:
                            await wait_msg.edit_text(f"{msg_step}\n⌛ {secs} soniya...")
                        except Exception:
                            pass
                except asyncio.CancelledError:
                    pass

            # Progressni DARROV ishga tushiramiz (yuklashdan oldin)
            progress_task = asyncio.create_task(_show_progress())

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
                if is_big:
                    _progress_stop.set()
                    progress_task.cancel()
                    await wait_msg.edit_text(t(context, 'too_big'))
                    return
                file = await context.bot.get_file(video.file_id)
                await file.download_to_drive(tmp_path)

            # MUHIM: video fayl YAXSHI yuklanganini tekshiramiz (Gemini'ga buzuq/bo'sh
            # fayl ketib, uzoq qotib qolmasligi uchun).
            if not tmp_path or not os.path.exists(tmp_path) or os.path.getsize(tmp_path) < 1024:
                _progress_stop.set()
                progress_task.cancel()
                _xato_qayd("fayl")
                logger.warning(f"Video yuklanmadi yoki bo'sh (uid={user_id}, path={tmp_path})")
                await wait_msg.edit_text(
                    "⚠️ Video yuklab olishda muammo bo'ldi. Iltimos, videoni qaytadan yuboring. 🙏")
                return

            # Model tanlash: pullik (admin/obuna/to'lagan) -> 2.5 Flash (sifatli);
            # bepul -> 2.5 Flash-Lite (4-5 barobar arzon, sifat biroz pastroq).
            _model = "gemini-2.5-flash" if _is_priority else "gemini-2.5-flash-lite"
            # MUHIM: Gemini ishi alohida thread'da bajariladi -> bot muzlamaydi.
            try:
                # TIMEOUT 5 daqiqa: agar Gemini osilib qolsa - majburan bekor,
                # semaphore bo'shaydi, boshqa videolar qotmaydi.
                tahlil, _usage = await asyncio.wait_for(
                    asyncio.to_thread(_gemini_process, tmp_path, prompt, _model),
                    timeout=300
                )
                # BEPUL: tahlil juda tez tugasa, sanoq biroz (12 sek) ko'rinsin
                # (reklama ulgursin). Lekin uzoq kutmaymiz - tez javob yaxshi.
                if not _is_priority:
                    elapsed = (datetime.now() - _analiz_start).total_seconds()
                    if elapsed < 12:
                        await asyncio.sleep(12 - elapsed)
            except asyncio.TimeoutError:
                _progress_stop.set()
                progress_task.cancel()
                _xato_qayd("timeout")
                logger.warning(f"Gemini timeout (5 daqiqa) - bekor qilindi (uid={user_id})")
                await wait_msg.edit_text(t(context, 'analysis_timeout'))
                return
            finally:
                _progress_stop.set()
                progress_task.cancel()

            if not tahlil or not tahlil.strip():
                raise Exception("Tahlil bo'sh keldi")

            await wait_msg.edit_text(t(context, 'ready'))
            # Faqat muvaffaqiyatda hisoblanadi (xatoda hech narsa yechilmaydi).
            # Obuna faol bo'lsa - hech narsa yechilmaydi (cheksiz);
            # aks holda 1 ta bepul tahlil yechiladi.
            # Agar referral orqali kelgan bo'lsa, taklif qilgan odamga +1 qo'shiladi.
            referrer = consume_access(user_id)
            if referrer:
                try:
                    await context.bot.send_message(referrer, TEXTS['uz']['ref_reward'])
                except Exception as e:
                    logger.warning(f"Referrerga xabar yuborilmadi: {e}")

            # Tahlilni qism qism ajratamiz: foiz, qisqa, to'liq
            foiz, qisqa, toliq = _parse_analysis(tahlil)
            # MUHIM: foizni ballardan qayta hisoblaymiz (Gemini'ning saxiy foiziga ishonmaymiz)
            foiz = _recalc_foiz(qisqa, foiz)
            qisqa = _replace_foiz_line(qisqa, foiz)
            toliq = _replace_foiz_line(toliq, foiz)
            # Oldingi video bilan taqqoslash (yangisini saqlashdan OLDIN olamiz)
            prev_foiz = get_last_video_foiz(user_id)
            uname = message.from_user.username or message.from_user.first_name or ""

            # Token va tannarxni hisoblaymiz (bazaga saqlash + admin hisoboti uchun)
            # _usage - SHU tahlilniki (global aralashmaydi)
            p_tok = _usage.get("prompt", 0)
            o_tok = _usage.get("output", 0)
            tot = _usage.get("total", 0) or (p_tok + o_tok)
            usd, uzs = _cost_uzs(p_tok, o_tok, _usage.get("model", "gemini-2.5-flash"))

            aid = save_analysis(user_id, username=uname, kind="video",
                                file_id=video.file_id, foiz=foiz, qisqa=qisqa, toliq=toliq,
                                tokens=tot, narx=uzs)

            # Adminlarga tannarx hisoboti (token + so'm + video hajmi/uzunligi)
            try:
                # Video hajmi (MB) va uzunligi (soniya)
                _mb = (video.file_size / (1024 * 1024)) if getattr(video, "file_size", None) else 0
                _dur = getattr(video, "duration", 0) or 0
                _dur_txt = f"{_dur // 60}:{_dur % 60:02d}" if _dur >= 60 else f"{_dur} sek"
                report = (
                    f"📊 Tannarx hisobi (video tahlil)\n"
                    f"👤 @{uname} (ID: {user_id})\n"
                    f"🎬 Video: {_mb:.1f} MB, {_dur_txt}\n"
                    f"📈 Natija: {foiz}%\n"
                    f"🔢 Kiruvchi: {p_tok:,} token\n"
                    f"🔢 Javob: {o_tok:,} token\n"
                    f"🔢 Jami: {tot:,} token\n"
                    f"💵 ≈ ${usd:.4f}\n"
                    f"💰 ≈ {uzs:,.0f} so'm"
                )
                _admin_kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton("🎬 Videoni ko'rish", callback_data=f"advid_{aid}")
                ]])
                for _aid in ADMIN_IDS:
                    try:
                        await context.bot.send_message(_aid, report, reply_markup=_admin_kb)
                    except Exception:
                        pass
            except Exception as e:
                logger.warning(f"Admin tannarx hisobotini yuborishda xato: {e}")

            # Taqqoslash xabarini qisqa tahlilga qo'shamiz
            if prev_foiz is not None:
                if foiz > prev_foiz:
                    qisqa = qisqa + "\n\n" + t(context, 'cmp_up').format(prev=prev_foiz, now=foiz, d=foiz - prev_foiz)
                elif foiz < prev_foiz:
                    qisqa = qisqa + "\n\n" + t(context, 'cmp_down').format(prev=prev_foiz, now=foiz, d=prev_foiz - foiz)
                else:
                    qisqa = qisqa + "\n\n" + t(context, 'cmp_same').format(now=foiz)

            # Tahlil oxiriga bot havolasini qo'shamiz (boshqaga ulashsa - reklama)
            qisqa = qisqa + t(context, 'analyzed_footer')

            # GEMINI MASLAHATI: reklama bannerini O'CHIRAMIZ (tahlil tugadi, endi to'smasin)
            _pmid = context.user_data.pop('_promo_msg_id', None)
            if _pmid:
                try:
                    await context.bot.delete_message(message.chat_id, _pmid)
                except Exception:
                    pass

            # Mijozga QISQA tahlil + tugmalar (soddalashtirilgan - chalkashlik bo'lmasin)
            kb = None
            if aid:
                _btns = [
                    [InlineKeyboardButton(t(context, 'full_btn'), callback_data=f"full_{aid}")],
                    [InlineKeyboardButton(t(context, 'yaxshilash_btn'), callback_data=f"yax_{aid}")],
                    [InlineKeyboardButton(t(context, 'tts_full_btn'), callback_data=f"ttsf_{aid}")],
                ]
                # ADMIN uchun TEST tugmalar (Reels g'oyalari + Eslatma)
                if is_admin(user_id):
                    _btns.append([InlineKeyboardButton("💡 5 ta Reels g'oyasi", callback_data=f"goya_{aid}")])
                    _btns.append([InlineKeyboardButton("📅 Eslatma qo'shish", callback_data=f"eslat_{aid}")])
                kb = InlineKeyboardMarkup(_btns)
            if len(qisqa) <= 4000:
                await message.reply_text(qisqa, reply_markup=kb)
            else:
                parts = [qisqa[i:i+4000] for i in range(0, len(qisqa), 4000)]
                for idx, chunk in enumerate(parts):
                    await message.reply_text(chunk, reply_markup=(kb if idx == len(parts) - 1 else None))

            # BEPULGA: tugagandan keyingi reklama OLIB TASHLANDI (o'rtadagi reklama yetarli)

            # AVTOMATIK +2 AKSIYA: 1 ta bepulni ishlatib, balansi tugagan bo'lsa
            # (faqat avtomatik aksiya YOQILGAN bo'lsa). Har odam 1 marta.
            try:
                if grant_auto_aksiya(user_id):
                    aksiya_kb = InlineKeyboardMarkup([[
                        InlineKeyboardButton(t(context, 'menu_video'), callback_data="aksiya_video")
                    ]])
                    await message.reply_text(t(context, 'aksiya_msg'), reply_markup=aksiya_kb)
            except Exception as e:
                logger.warning(f"Avtomatik +2 aksiya yuborishda xato: {e}")

            # AVTOMATIK obuna taklifi: +2 ni ham ishlatib, balansi tugagan bo'lsa
            try:
                if should_send_auto_offer(user_id):
                    offer_kb = InlineKeyboardMarkup([[
                        InlineKeyboardButton(t(context, 'obuna_taklif_btn'), callback_data="buy_sub")
                    ]])
                    await message.reply_text(t(context, 'obuna_taklif_msg'), reply_markup=offer_kb)
            except Exception as e:
                logger.warning(f"Avtomatik obuna taklifini yuborishda xato: {e}")

    except Exception as e:
        logger.error(f"Yakuniy xato: {e}")
        emsg = str(e)
        if "RESOURCE_EXHAUSTED" in emsg or "429" in emsg or "quota" in emsg.lower():
            _xato_qayd("boshqa")
            await wait_msg.edit_text(t(context, 'busy_quota'))
        elif "UNAVAILABLE" in emsg or "503" in emsg or "high demand" in emsg.lower():
            _xato_qayd("503")
            await _xato_ogohlantir(context)
            await wait_msg.edit_text(t(context, 'busy_quota'))
        else:
            _xato_qayd("boshqa")
            await wait_msg.edit_text(t(context, 'error'))
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    # Agar foydalanuvchi MENU tugmasini bossa, har qanday "kutish" rejimini bekor qilamiz.
    # (Aks holda fikr/so'rov rejimida menu tugmasi "fikr" deb qabul qilinardi - bu bug edi.)
    _menu_tugmalar = set()
    for _lng in ('uz', 'ru'):
        for _k in ('menu_video', 'menu_profile', 'menu_status', 'menu_premium', 'menu_arxiv',
                   'menu_ref', 'menu_lang', 'menu_help', 'menu_fikr'):
            _v = TEXTS.get(_lng, {}).get(_k)
            if _v:
                _menu_tugmalar.add(_v)
    if text in _menu_tugmalar and context.user_data.get('mode'):
        context.user_data['mode'] = None  # rejimni bekor qilamiz, menu ishlaydi

    # So'rov 1-savol javobini kutyapmizmi?
    if context.user_data.get('mode') == 'profile_mavzu':
        context.user_data['profile_mavzu'] = (text or "").strip()
        context.user_data['mode'] = 'profile'
        # Qayta tahlil - profile_analyze tugmasini ko'rsatamiz
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(t(context, 'profile_btn'), callback_data='profile_analyze')
        ]])
        await update.message.reply_text(
            "✅ Rahmat! Endi tahlilni boshlaymiz 👇", reply_markup=kb
        )
        return
    if context.user_data.get('mode') == 'test_sorov_q1':
        context.user_data['test_sorov_a1'] = (text or "").strip()
        context.user_data['mode'] = 'test_sorov_q2'
        await update.message.reply_text(t(context, 'test_sorov_q2'), parse_mode="HTML")
        return
    if context.user_data.get('mode') == 'test_sorov_q2':
        context.user_data['mode'] = None
        uid = update.effective_user.id
        a1 = context.user_data.get('test_sorov_a1', '')
        a2 = (text or "").strip()
        uname = update.effective_user.username or update.effective_user.first_name or ""
        # Bazaga saqlaymiz (/javoblar da ko'rinadi)
        try:
            _db_execute(
                "INSERT INTO sorov_javoblar (user_id, username, javob1, javob2, created) "
                "VALUES (%s, %s, %s, %s, %s)",
                (uid, uname, "[7KUN] " + a1, a2, datetime.now().strftime("%Y-%m-%d %H:%M"))
            )
        except Exception as e:
            logger.warning(f"Test so'rov javobini saqlashda xato: {e}")
        # Fikrlar guruhiga yuboramiz
        if FIKR_GROUP_ID:
            try:
                who = f"@{uname}" if uname else f"ID {uid}"
                grp_txt = (f"📋 7 KUNLIK SO'ROV\n👤 {who}\n\n"
                           f"1️⃣ Test paket: {a1}\n2️⃣ Yangilanish: {a2}")
                await context.bot.send_message(FIKR_GROUP_ID, grp_txt)
            except Exception as e:
                logger.warning(f"Test fikrni guruhga yuborishda xato: {e}")
        await update.message.reply_text(t(context, 'test_sorov_done'))
        return
    if context.user_data.get('mode') == 'sorov_q1':
        context.user_data['sorov_a1'] = (text or "").strip()
        context.user_data['mode'] = 'sorov_q2'
        await update.message.reply_text(t(context, 'sorov_q2'))
        return
    # So'rov 2-savol javobini kutyapmizmi?
    if context.user_data.get('mode') == 'sorov_q2':
        context.user_data['mode'] = None
        uid = update.effective_user.id
        a1 = context.user_data.get('sorov_a1', '')
        a2 = (text or "").strip()
        uname = update.effective_user.username or update.effective_user.first_name or ""
        # Adminlarga BITTALAB yubormaymiz (kasha bo'lmasin) - bazaga saqlaymiz.
        # /javoblar buyrug'i bilan hammasi bir joyda ko'riladi.
        try:
            _db_execute(
                "INSERT INTO sorov_javoblar (user_id, username, javob1, javob2, created) "
                "VALUES (%s, %s, %s, %s, %s)",
                (uid, uname, a1, a2, datetime.now().strftime("%Y-%m-%d %H:%M"))
            )
        except Exception as e:
            logger.warning(f"So'rov javobini saqlashda xato: {e}")
        # Fikrlar guruhiga ham yuboramiz (agar guruh ID sozlangan bo'lsa)
        if FIKR_GROUP_ID:
            try:
                who = f"@{uname}" if uname else f"ID {uid}"
                grp_txt = (f"📝 YANGI FIKR\n👤 {who}\n\n"
                           f"1️⃣ Yoqdi: {a1}\n2️⃣ Taklif: {a2}")
                await context.bot.send_message(FIKR_GROUP_ID, grp_txt)
            except Exception as e:
                logger.warning(f"Fikrni guruhga yuborishda xato: {e}")
        # 2 savolga javob bergan har kimga 1 ta bepul (faqat 1 marta)
        row = _db_execute("SELECT COALESCE(sorov_reward, FALSE) FROM users WHERE user_id = %s", (uid,), fetch='one')
        already = row[0] if row else False
        if not already:
            add_balance(uid, 1)
            _db_execute("UPDATE users SET sorov_reward = TRUE WHERE user_id = %s", (uid,))
            await update.message.reply_text(t(context, 'sorov_thanks_reward'))
        else:
            await update.message.reply_text(t(context, 'sorov_thanks'))
        return
    # Video "Fikr bildirish" rejimi - fikr guruhga boradi (bepul YO'Q, sovg'a alohida tugmada)
    if context.user_data.get('mode') == 'marafon_fikr':
        context.user_data['mode'] = None
        uid = update.effective_user.id
        fikr = (text or "").strip()
        uname = update.effective_user.username or update.effective_user.first_name or ""
        # Fikrni FIKR_GROUP ga yuboramiz
        if FIKR_GROUP_ID:
            try:
                who = f"@{uname}" if uname else f"ID {uid}"
                await context.bot.send_message(FIKR_GROUP_ID, f"🏃 MARAFON FIKRI (4-kun)\n👤 {who}\n\n{fikr}")
            except Exception:
                pass
        # Bepul tahlil + 4-kun bajarilgan deb sanaymiz
        add_balance(uid, 1)
        bugun = datetime.now().strftime("%Y-%m-%d")
        _ox = _db_execute("SELECT marafon_oxirgi_bajarilgan, marafon_kun, marafon_tugadi FROM users WHERE user_id = %s", (uid,), fetch='one')
        if _ox and _ox[1] and _ox[1] >= 1 and not _ox[2] and _ox[0] != bugun:
            _db_execute(
                "UPDATE users SET marafon_bajarilgan = COALESCE(marafon_bajarilgan,0) + 1, "
                "marafon_oxirgi_bajarilgan = %s WHERE user_id = %s", (bugun, uid))
        await update.message.reply_text(
            "🙏 Rahmat, fikringiz biz uchun juda qimmatli! 💙\n\n"
            "🎁 Bugungi bepul tahlilingiz ochildi! Videongizni yuboring 👇\n\n"
            "💎 Ertaga — PREMIUM sovg'a kutmoqda!",
            parse_mode="HTML")
        return
    if context.user_data.get('mode') == 'sotuv1_fikr':
        context.user_data['mode'] = None
        uid = update.effective_user.id
        fikr = (text or "").strip()
        uname = update.effective_user.username or update.effective_user.first_name or ""
        # Fikrni admin guruhiga yuboramiz
        if FIKR_GROUP_ID:
            try:
                who = f"@{uname}" if uname else f"ID {uid}"
                grp_txt = f"💬 SOTUV FIKRI (5+ ishlatgan, to'lamagan)\n👤 {who}\n\n{fikr}"
                await context.bot.send_message(FIKR_GROUP_ID, grp_txt)
            except Exception as e:
                logger.warning(f"sotuv1 fikrini guruhga yuborishda xato: {e}")
        # PREMIUM tahlil beramiz (hook+ovoz), FAQAT 1 marta
        _chk = _db_execute("SELECT sotuv_bepul_olindi FROM users WHERE user_id = %s", (uid,), fetch='one')
        if not (_chk and _chk[0]):
            add_premium_balance(uid, 1)
            _db_execute("UPDATE users SET sotuv_bepul_olindi = %s WHERE user_id = %s",
                        ("sotuv1: " + datetime.now().strftime("%Y-%m-%d %H:%M"), uid))
            for aid in ADMIN_IDS:
                try:
                    who = f"@{uname}" if uname else f"ID {uid}"
                    await context.bot.send_message(aid, f"🎁 PREMIUM berildi (sotuv1 fikr)\n👤 {who}")
                except Exception:
                    pass
            await update.message.reply_text(
                "Rahmat, fikringiz uchun juda minnatdorman! 🤍\n\n"
                "✅ Hisobingizga <b>1 ta BEPUL PREMIUM tahlil</b> qo'shildi!\n"
                "🔊 Ovozli eshitish va 🔥 Hook yaxshilash ham ochiq.\n\n"
                "Hoziroq videongizni yuboring — enjoy! 🎬",
                parse_mode="HTML"
            )
        else:
            # Allaqachon premium olgan - faqat fikr uchun rahmat (qayta premium yo'q)
            await update.message.reply_text(
                "Rahmat, fikringiz uchun juda minnatdorman! 🤍", parse_mode="HTML")
        return
    if context.user_data.get('mode') == 'premium_fikr':
        context.user_data['mode'] = None
        uid = update.effective_user.id
        fikr = (text or "").strip()
        uname = update.effective_user.username or update.effective_user.first_name or ""
        if FIKR_GROUP_ID:
            try:
                who = f"@{uname}" if uname else f"ID {uid}"
                grp_txt = f"💎 PREMIUM FIKRI\n👤 {who}\n\n{fikr}"
                await context.bot.send_message(FIKR_GROUP_ID, grp_txt)
            except Exception as e:
                logger.warning(f"Premium fikrini guruhga yuborishda xato: {e}")
        await update.message.reply_text(t(context, 'premium_fikr_thanks'), parse_mode="HTML")
        return
    if context.user_data.get('mode') == 'video_fikr':
        context.user_data['mode'] = None
        uid = update.effective_user.id
        fikr = (text or "").strip()
        uname = update.effective_user.username or update.effective_user.first_name or ""
        if FIKR_GROUP_ID:
            try:
                who = f"@{uname}" if uname else f"ID {uid}"
                grp_txt = f"🎬 VIDEO FIKRI\n👤 {who}\n\n{fikr}"
                await context.bot.send_message(FIKR_GROUP_ID, grp_txt)
            except Exception as e:
                logger.warning(f"Video fikrini guruhga yuborishda xato: {e}")
        await update.message.reply_text("🙏 Раҳмат фикрингиз учун! Биз уни албатта ҳисобга оламиз. 🤍")
        return
    if context.user_data.get('mode') == 'fikr':
        context.user_data['mode'] = None
        uid = update.effective_user.id
        fikr = (text or "").strip()
        uname = update.effective_user.username or update.effective_user.first_name or ""
        # Bazaga saqlaymiz (javob2 bo'sh - bu menyu fikri)
        try:
            _db_execute(
                "INSERT INTO sorov_javoblar (user_id, username, javob1, javob2, created) "
                "VALUES (%s, %s, %s, %s, %s)",
                (uid, uname, "(menyu fikri) " + fikr, "", datetime.now().strftime("%Y-%m-%d %H:%M"))
            )
        except Exception as e:
            logger.warning(f"Menyu fikrini saqlashda xato: {e}")
        # Guruhga yuboramiz
        if FIKR_GROUP_ID:
            try:
                who = f"@{uname}" if uname else f"ID {uid}"
                grp_txt = f"💬 YANGI FIKR (menyu)\n👤 {who}\n\n{fikr}"
                await context.bot.send_message(FIKR_GROUP_ID, grp_txt)
            except Exception as e:
                logger.warning(f"Menyu fikrini guruhga yuborishda xato: {e}")
        await update.message.reply_text(t(context, 'fikr_thanks'))
        return
    if text in (TEXTS['uz']['menu_video'], TEXTS['ru']['menu_video']):
        context.user_data['mode'] = None
        await update.message.reply_text(t(context, 'send_video'))
    elif text in (TEXTS['uz']['menu_profile'], TEXTS['ru']['menu_profile']):
        uid = update.effective_user.id
        # Profil tahlili FAQAT PREMIUM (admin yoki obunachi) uchun
        if is_admin(uid) or has_access(uid) in ('admin', 'sub'):
            context.user_data['mode'] = 'profile'
            context.user_data['profile_imgs'] = []
            await update.message.reply_text(t(context, 'profile_instr'), parse_mode="HTML")
        else:
            # Bepul -> premium kerak
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton(t(context, 'obuna_taklif_btn'), callback_data="buy_sub")
            ]])
            await update.message.reply_text(t(context, 'profile_premium'), reply_markup=kb, parse_mode="HTML")
    elif text in (TEXTS['uz']['menu_status'], TEXTS['ru']['menu_status']):
        uid = update.effective_user.id
        row = _db_execute("SELECT COUNT(*) FROM analyses WHERE user_id = %s AND kind='video'", (uid,), fetch='one')
        tahlil = (row[0] if row and row[0] else 0)
        emoji, nom, kerak, keyingi = kreator_status(tahlil)
        bar, foiz = kreator_progress_bar(tahlil)
        premium = is_admin(uid) or sub_active(uid)
        txt = "👤 <b>KREATOR PROFILINGIZ</b>\n━━━━━━━━━━━\n\n"
        txt += f"🎯 Status: {emoji} <b>{nom}</b>\n"
        txt += f"📊 Progress: [{bar}] {foiz}%\n"
        txt += f"🎬 Jami tahlil: {tahlil} ta\n\n"
        if keyingi and kerak > 0:
            txt += f"🔥 Keyingi <b>\"{keyingi}\"</b> darajasiga yana {kerak} ta tahlil kerak!\n\n"
        elif not keyingi:
            txt += "🏆 Siz eng yuqori darajaga yetdingiz — zo'rsiz! 👑\n\n"
        # Balans/premium holati
        if premium:
            txt += f"💎 Premium: <b>Faol ✅</b> ({sub_until_str(uid)} gacha)\nCheksiz tahlil, hook, ovoz ochiq!"
            await update.message.reply_text(txt, parse_mode="HTML")
        elif get_premium_balance(uid) > 0:
            txt += f"💎 Sizda <b>{get_premium_balance(uid)} ta PREMIUM tahlil</b> bor!\nVideo yuboring 🎬"
            await update.message.reply_text(txt, parse_mode="HTML")
        else:
            bal = get_balance(uid)
            txt += f"🎫 Bepul tahlil balansi: <b>{bal} ta</b>\n\n"
            txt += "💎 Premium: <b>Faol emas</b>\nPremiumga o'tib hamma imkoniyatni oching!"
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("💎 Premiumga o'tish — 29,900", callback_data="buy_sub")]])
            await update.message.reply_text(txt, reply_markup=kb, parse_mode="HTML")
    elif text in (TEXTS['uz']['menu_arxiv'], TEXTS['ru']['menu_arxiv']):
        # "Mening tahlillarim" - AI o'sish + kuchli/zaif tomonlar (premium, oyda 2 marta, 5+ tahlil)
        uid = update.effective_user.id
        premium = is_admin(uid) or sub_active(uid)
        if not premium:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("💎 Premiumga o'tish — 29,900", callback_data="buy_sub")]])
            await update.message.reply_text(
                "📂 <b>Shaxsiy hisobotim</b> — bu maxsus Premium funksiya! 💎\n\n"
                "Bot barcha tahlillaringizni o'rganib, o'sishingizni va kuchli/zaif "
                "tomonlaringizni chuqur tahlil qiladi 📊✨\n\n"
                "Premium'ga o'tib, shaxsiy o'sish hisobotingizni oling! 🚀",
                reply_markup=kb, parse_mode="HTML")
            return
        # Kamida 5 tahlil kerak
        rows = _db_execute(
            "SELECT foiz, qisqa, created FROM analyses WHERE user_id = %s AND kind='video' "
            "AND toliq IS NOT NULL ORDER BY id ASC", (uid,), fetch='all') or []
        if len(rows) < 5:
            await update.message.reply_text(
                f"📂 <b>Shaxsiy hisobotim</b>\n\n"
                f"Bu funksiya kamida 5 ta tahlildan keyin ishlaydi 📊\n"
                f"Sizda hozir {len(rows)} ta tahlil bor. Yana {5-len(rows)} ta video yuboring! 🎬",
                parse_mode="HTML")
            return
        # Oyda 2 marta cheklovi
        shu_oy = datetime.now().strftime("%Y-%m")
        _st = _db_execute("SELECT studiya_oy, COALESCE(studiya_marta,0) FROM users WHERE user_id = %s", (uid,), fetch='one')
        oy_saqlangan = _st[0] if _st else None
        marta = _st[1] if _st else 0
        if oy_saqlangan != shu_oy:
            marta = 0  # yangi oy - reset
        if marta >= 2 and not is_admin(uid):
            await update.message.reply_text(
                "📂 <b>Shaxsiy hisobotim</b>\n\n"
                "Bu funksiyani oyiga 2 marta ishlatish mumkin 📊\n"
                "Bu oy limitni ishlatib bo'lgansiz. Keyingi oy yana foydalaning! 🗓",
                parse_mode="HTML")
            return
        await update.message.reply_text("📊 Tahlillaringizni o'rganyapman... biroz kuting ⏳")
        # Oxirgi 10 tahlilni AI'ga beramiz
        son = len(rows)
        birinchi_foiz = rows[0][0] if rows[0][0] else 0
        oxirgi_foiz = rows[-1][0] if rows[-1][0] else 0
        tahlillar_matn = ""
        for i, r in enumerate(rows[-10:], 1):
            f = r[0] if r[0] else 0
            q = (r[1] or "")[:300]
            tahlillar_matn += f"\n{i}-tahlil (sifat {f}%): {q}\n"
        prompt = (
            f"Sen Instagram Reels bo'yicha mutaxassissan. Quyida bir bloger {son} ta videosining tahlillari bor "
            f"(birinchi {birinchi_foiz}%, oxirgi {oxirgi_foiz}%).\n{tahlillar_matn}\n\n"
            f"Blogerga SHAXSIY, ILIQ o'sish hisoboti yoz (o'zbek tilida, emoji bilan, 'siz' deb murojaat qil):\n"
            f"1. O'sish: sifat qanday o'zgargan (raqamlar bilan, motivatsiya)\n"
            f"2. Kuchli tomonlaringiz (2-3 ta)\n"
            f"3. Yaxshilash kerak bo'lgan tomonlar (2-3 ta, aniq maslahat)\n"
            f"4. Qisqa rag'batlantiruvchi xulosa\n"
            f"Jami 250-350 so'z, do'stona, motivatsion.")
        try:
            javob = _generate(prompt)
            if not javob or len(javob.strip()) < 20:
                await update.message.reply_text("⚠️ Hozir tahlil qilib bo'lmadi, biroz keyin urinib ko'ring.")
                return
            # Cheklovni yangilaymiz
            _db_execute("UPDATE users SET studiya_oy = %s, studiya_marta = %s WHERE user_id = %s",
                        (shu_oy, marta + 1, uid))
            qolgan = 2 - (marta + 1)
            await update.message.reply_text(
                f"📂 <b>SIZNING O'SISH HISOBOTINGIZ</b> 📈\n━━━━━━━━━━━\n\n{javob}\n\n"
                f"━━━━━━━━━━━\n💡 Bu oy yana {qolgan} marta ishlatishingiz mumkin.",
                parse_mode="HTML")
        except Exception:
            await update.message.reply_text("⚠️ Xatolik yuz berdi, biroz keyin urinib ko'ring.")
    elif text in (TEXTS['uz']['menu_balance'], TEXTS['ru']['menu_balance']):
        uid = update.effective_user.id
        if is_admin(uid):
            await update.message.reply_text("👑 Admin — cheksiz tahlil.")
        elif sub_active(uid):
            await update.message.reply_text(t(context, 'sub_active').format(until=sub_until_str(uid)))
        elif get_premium_balance(uid) > 0:
            await update.message.reply_text(
                f"💎 Sizda {get_premium_balance(uid)} ta PREMIUM tahlil bor!\n"
                f"🔊 Ovozli + 🔥 Hook ochiq. Video yuboring 🎬")
        else:
            bal = get_balance(uid)
            await update.message.reply_text(
                t(context, 'balance_info').format(n=bal),
                reply_markup=package_keyboard(context)
            )
    elif text in (TEXTS['uz']['menu_ref'], TEXTS['ru']['menu_ref']):
        uid = update.effective_user.id
        bot_username = context.bot_data.get('bot_username') or (await context.bot.get_me()).username
        link = f"https://t.me/{bot_username}?start=ref_{uid}"
        await update.message.reply_text(t(context, 'ref_info').format(link=link))
    elif text in (TEXTS['uz']['menu_help'], TEXTS['ru']['menu_help']):
        await update.message.reply_text(t(context, 'help_text'))
    elif text in (TEXTS['uz']['menu_fikr'], TEXTS['ru']['menu_fikr']):
        context.user_data['mode'] = 'fikr'
        await update.message.reply_text(t(context, 'fikr_ask'))
    elif text in (TEXTS['uz']['menu_premium'], TEXTS['ru']['menu_premium']):
        uid = update.effective_user.id
        if is_admin(uid):
            await update.message.reply_text("👑 Admin — cheksiz tahlil.")
        elif sub_active(uid):
            await update.message.reply_text(t(context, 'sub_active').format(until=sub_until_str(uid)))
        else:
            # Premium taklifi + sotib olish tugmasi
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton(sub_btn_label(context), callback_data='buy_sub')
            ]])
            # Chegirma faol bo'lsa - sotuv xabari, aks holda oddiy taklif
            if discount_active():
                await update.message.reply_text(get_sotuv_msg(), reply_markup=kb, parse_mode="HTML")
            else:
                await update.message.reply_text(t(context, 'sub_offer'), reply_markup=kb, parse_mode="HTML")
    else:
        await update.message.reply_text(t(context, 'send_video'))


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(t(context, 'help_text'))


async def til_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🌐 Tilni tanlang / Выберите язык:", reply_markup=lang_keyboard())


async def kim_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: ID dan username topadi. Ishlatish: /kim 123456789"""
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Ishlatish: /kim <ID>\nMasalan: /kim 8660237405")
        return
    try:
        uid = int(context.args[0])
    except Exception:
        await update.message.reply_text("❌ ID raqam bo'lishi kerak.")
        return
    row = _db_execute("SELECT username, first_name FROM users WHERE user_id = %s", (uid,), fetch='one')
    if not row:
        await update.message.reply_text(f"📭 ID {uid} bazada topilmadi (bot bilan ishlashmagan).")
        return
    uname = row[0]
    fname = row[1] if len(row) > 1 else ""
    if uname:
        await update.message.reply_text(f"👤 @{uname}")
    elif fname:
        await update.message.reply_text(f"👤 {fname} (username yo'q)")
    else:
        await update.message.reply_text(f"👤 ID {uid} — username va ism yo'q.")


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    total_users, total_analyses, today_analyses, revenue, subs, free_bal, active = get_stats()
    text = (
        "📊 ADMIN STATISTIKA\n\n"
        f"👥 Jami foydalanuvchilar: {total_users}\n"
        f"👑 Obunachilar (faol): {subs}\n"
        f"🎁 Bepul tahlili borlar: {free_bal}\n"
        f"📊 Aktiv (tahlil qilgan): {active}\n\n"
        f"🎬 Jami tahlillar: {total_analyses}\n"
        f"📅 Bugungi tahlillar: {today_analyses}\n"
        f"💰 Tasdiqlangan daromad: {revenue:,} so'm"
    )
    await update.message.reply_text(text)


async def yoz_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: ID bo'yicha foydalanuvchiga bot orqali xabar yuboradi.
    Foydalanish: /yoz <ID> <xabar>"""
    if not is_admin(update.effective_user.id):
        return
    full = update.message.text or ""
    parts = full.split(None, 2)  # ['/yoz', 'ID', 'xabar...']
    if len(parts) < 3:
        await update.message.reply_text(
            "✍️ Foydalanish: /yoz <ID> <xabar>\n\n"
            "Masalan:\n/yoz 8431988310 Assalomu alaykum! Obunangiz bo'yicha..."
        )
        return
    try:
        target_id = int(parts[1])
    except ValueError:
        await update.message.reply_text("❌ ID raqam bo'lishi kerak.")
        return
    xabar = parts[2].strip()
    try:
        await context.bot.send_message(
            target_id,
            f"📩 InstaDoctor jamoasidan xabar:\n\n{xabar}\n\n"
            f"💬 Javob berish uchun shu yerga yozing — biz ko'ramiz."
        )
        # Javobni kutish rejimi (foydalanuvchi yozsa - adminlarga boradi)
        await update.message.reply_text(f"✅ Xabar yuborildi (ID: {target_id})")
    except Exception as e:
        await update.message.reply_text(
            f"❌ Yuborib bo'lmadi: {e}\n\n"
            f"Sabab: foydalanuvchi botni bloklagan yoki ID noto'g'ri bo'lishi mumkin."
        )


async def berbalans_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: ID bo'yicha bepul tahlil (balans) qo'shadi.
    Foydalanish: /berbalans <ID> <son>  (son yozilmasa - 1 ta)"""
    if not is_admin(update.effective_user.id):
        return
    parts = (update.message.text or "").split()
    if len(parts) < 2:
        await update.message.reply_text(
            "✍️ Foydalanish: /berbalans <ID> <son>\n\n"
            "Masalan:\n/berbalans 8431988310 1  (1 ta bepul tahlil)\n"
            "/berbalans 8431988310 5  (5 ta bepul tahlil)"
        )
        return
    try:
        target_id = int(parts[1])
    except ValueError:
        await update.message.reply_text("❌ ID raqam bo'lishi kerak.")
        return
    son = 1
    if len(parts) >= 3:
        try:
            son = int(parts[2])
        except ValueError:
            son = 1
    add_balance(target_id, son)
    await update.message.reply_text(f"✅ {target_id} ga {son} ta bepul tahlil qo'shildi.")
    # Foydalanuvchini xabardor qilamiz
    try:
        await context.bot.send_message(
            target_id,
            f"🎁 Sizga {son} ta BEPUL tahlil qo'shildi! 🎬\n\nVideo yuboring va sinab ko'ring!"
        )
    except Exception:
        pass


async def kop_balans_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: balansi KO'P (shubhali - qayta-qayta olgan) odamlarni ko'rsatadi."""
    if not is_admin(update.effective_user.id):
        return
    # premium_balance yoki balance > 1 bo'lgan, obunachi bo'lmaganlar (shubhali)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    rows = _db_execute(
        "SELECT user_id, username, COALESCE(balance,0), COALESCE(premium_balance,0) "
        "FROM users WHERE (COALESCE(balance,0) > 1 OR COALESCE(premium_balance,0) > 1) "
        "AND (sub_until IS NULL OR sub_until <= %s) "
        "ORDER BY (COALESCE(balance,0) + COALESCE(premium_balance,0)) DESC LIMIT 40",
        (now_str,), fetch='all') or []
    if not rows:
        await update.message.reply_text("✅ Balansi ko'p (shubhali) odam yo'q.")
        return
    txt = f"⚠️ <b>BALANSI KO'P (shubhali)</b> — {len(rows)} ta\n\n"
    for r in rows:
        uid, uname, bal, pbal = r[0], r[1], r[2], r[3]
        who = f"@{uname}" if uname else f"ID {uid}"
        txt += f"• {who} (ID {uid}) — oddiy: {bal}, premium: {pbal}\n"
    txt += "\n🧹 Tozalash: /balans_0 &lt;ID&gt;\nHammasini: /sotuv_balans_0"
    await update.message.reply_text(txt, parse_mode="HTML")


async def sotuv_balans_0_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: sotuv orqali berilgan HAMMA ortiqcha balansni tozalaydi.
    Obunachi bo'lmaganlarning balance/premium_balansini 0 qiladi. Tasdiq: /sotuv_balans_0 HA"""
    if not is_admin(update.effective_user.id):
        return
    args = context.args
    if not args or args[0].upper() != "HA":
        # Avval nechta odam ta'sirlanishini ko'rsatamiz
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        cnt = _db_execute(
            "SELECT COUNT(*) FROM users WHERE (COALESCE(balance,0) > 0 OR COALESCE(premium_balance,0) > 0) "
            "AND (sub_until IS NULL OR sub_until <= %s)", (now_str,), fetch='one')
        n = cnt[0] if cnt else 0
        await update.message.reply_text(
            f"⚠️ DIQQAT! Bu {n} ta obunachi BO'LMAGAN odamning HAMMA balansini (oddiy+premium) 0 qiladi.\n\n"
            f"(Obunachilar/adminlar tegilmaydi)\n\n"
            f"Tasdiqlash uchun: /sotuv_balans_0 HA")
        return
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    _db_execute(
        "UPDATE users SET balance = 0, premium_balance = 0 "
        "WHERE (sub_until IS NULL OR sub_until <= %s)", (now_str,))
    await update.message.reply_text(
        "✅ Tozalandi! Obunachi bo'lmaganlarning balansi 0 qilindi.\n"
        "Endi sotuv buyruqlarini toza qayta yuborishingiz mumkin.")


async def balans_0_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: ID bo'yicha HAMMA balansni (oddiy+premium+juma) 0 qiladi. /balans_0 <ID>"""
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("✍️ Foydalanish: /balans_0 <ID>\nMasalan: /balans_0 8431988310")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ ID raqam bo'lishi kerak.")
        return
    _db_execute(
        "UPDATE users SET balance = 0, premium_balance = 0, juma_balance = 0 WHERE user_id = %s",
        (target_id,))
    await update.message.reply_text(f"✅ {target_id} ning HAMMA balansi 0 qilindi (oddiy+premium+juma).")


async def blok_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: ID bo'yicha foydalanuvchini bloklaydi. /blok <ID>"""
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("✍️ Foydalanish: /blok <ID>\nMasalan: /blok 8431988310")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ ID raqam bo'lishi kerak.")
        return
    _db_execute("UPDATE users SET bloklangan = TRUE WHERE user_id = %s", (target_id,))
    await update.message.reply_text(f"🚫 {target_id} bloklandi. Endi bot bilan ishlatolmaydi.")


async def blok_ochir_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: ID ni blokdan chiqaradi. /blok_ochir <ID>"""
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("✍️ Foydalanish: /blok_ochir <ID>")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ ID raqam bo'lishi kerak.")
        return
    _db_execute("UPDATE users SET bloklangan = FALSE WHERE user_id = %s", (target_id,))
    await update.message.reply_text(f"✅ {target_id} blokdan chiqarildi.")


async def berbalans_premium_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: ID bo'yicha PREMIUM balans (to'liq premium: hook+ovoz) qo'shadi.
    Foydalanish: /berbalans_premium <ID> <son>  (son yozilmasa - 1 ta)"""
    if not is_admin(update.effective_user.id):
        return
    parts = (update.message.text or "").split()
    if len(parts) < 2:
        await update.message.reply_text(
            "✍️ Foydalanish: /berbalans_premium <ID> <son>\n\n"
            "Masalan:\n/berbalans_premium 8431988310 1  (1 ta PREMIUM tahlil)\n\n"
            "💎 Premium balans = to'liq premium (hook yaxshilash + ovoz ochiq)")
        return
    try:
        target_id = int(parts[1])
    except ValueError:
        await update.message.reply_text("❌ ID raqam bo'lishi kerak.")
        return
    son = 1
    if len(parts) >= 3:
        try:
            son = int(parts[2])
        except ValueError:
            son = 1
    add_premium_balance(target_id, son)
    await update.message.reply_text(f"✅ {target_id} ga {son} ta PREMIUM tahlil qo'shildi (hook+ovoz ochiq).")
    try:
        await context.bot.send_message(
            target_id,
            f"🎁 Sizga {son} ta BEPUL PREMIUM tahlil qo'shildi! 💎\n\n"
            f"🔊 Ovozli eshitish va 🔥 Hook yaxshilash ham ochiq!\n\n"
            f"Video yuboring va premium kuchini ko'ring! 🎬")
    except Exception:
        pass


async def berobuna_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: berilgan ID ga obuna yoqadi. Foydalanish: /berobuna <ID> <kun>
    Misol: /berobuna 5722018971 30  (30 kunlik obuna beradi)"""
    if not is_admin(update.effective_user.id):
        return
    args = context.args or []
    if len(args) < 1:
        await update.message.reply_text(
            "ℹ️ Foydalanish: /berobuna <ID> <kun>\n"
            "Misol: /berobuna 5722018971 30\n"
            "(kun yozilmasa — 30 kun)"
        )
        return
    try:
        target_id = int(args[0])
    except ValueError:
        await update.message.reply_text("⚠️ ID noto'g'ri. Faqat raqam yozing.\nMisol: /berobuna 5722018971 30")
        return
    days = 30
    if len(args) >= 2:
        try:
            days = int(args[1])
            if days < 1 or days > 3650:
                await update.message.reply_text("⚠️ Kun 1 dan 3650 gacha bo'lsin.")
                return
        except ValueError:
            await update.message.reply_text("⚠️ Kun noto'g'ri. Faqat raqam yozing.")
            return
    try:
        new_until = activate_subscription(target_id, days)
        await update.message.reply_text(
            f"✅ Obuna yoqildi!\n👤 ID: {target_id}\n📅 {days} kun\n⏳ Tugash: {new_until}"
        )
        # Foydalanuvchiga fireworks bilan tabrik
        try:
            await _send_celebration(context, target_id,
                                    TEXTS['uz']['celebrate_sub'].format(until=new_until))
        except Exception:
            await update.message.reply_text(
                "ℹ️ Obuna yoqildi, lekin foydalanuvchiga xabar yuborib bo'lmadi "
                "(u botni hali ishga tushirmagan bo'lishi mumkin)."
            )
    except Exception as e:
        logger.error(f"berobuna xato: {e}")
        await update.message.reply_text("⚠️ Xatolik yuz berdi. Qaytadan urinib ko'ring.")


async def aksiya_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: video tahlil qilib, balansi tugaganlarga 'yana 2 ta bepul' aksiya yuboradi.
    Har foydalanuvchiga FAQAT 1 marta (aksiya_given bilan belgilanadi). Sekin yuboradi."""
    if not is_admin(update.effective_user.id):
        return
    # Nishon: video tahlil qilgan, hozir obunasi yo'q, balansi 0, va aksiyani hali olmagan
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    rows = _db_execute(
        "SELECT DISTINCT u.user_id FROM users u "
        "JOIN analyses a ON a.user_id = u.user_id AND a.kind = 'video' "
        "WHERE COALESCE(u.balance,0) <= 0 "
        "AND (u.sub_until IS NULL OR u.sub_until <= %s) "
        "AND COALESCE(u.aksiya_given, FALSE) = FALSE",
        (now,), fetch='all'
    ) or []
    if not rows:
        await update.message.reply_text("📭 Aksiya yuboriladigan foydalanuvchi yo'q (hammasi olgan yoki balansi bor).")
        return
    await update.message.reply_text(f"📣 Aksiya boshlandi: {len(rows)} ta foydalanuvchiga yuborilmoqda...\n(Sekin yuboriladi, kuting)")

    sent, failed = 0, 0
    for row in rows:
        uid = row[0]
        try:
            # +1 tahlil va belgilab qo'yamiz (takror bo'lmasin)
            add_balance(uid, 1)
            _db_execute("UPDATE users SET aksiya_given = TRUE WHERE user_id = %s", (uid,))
            # Xabar + tugma (foydalanuvchi tilini bilmasak, uz default)
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("🎬 Video tahlil qilish", callback_data="aksiya_video")
            ]])
            await context.bot.send_message(uid, TEXTS['uz']['aksiya_msg'], reply_markup=kb)
            sent += 1
        except Exception as e:
            failed += 1
            logger.warning(f"Aksiya yuborishda xato (uid={uid}): {e}")
        await asyncio.sleep(0.4)  # Telegram limitidan oshmaslik uchun sekin

    await update.message.reply_text(
        f"✅ Aksiya tugadi!\n📨 Yuborildi: {sent}\n⚠️ Yuborilmadi: {failed} (bloklagan yoki botni o'chirgan)"
    )


async def aksiya_tugadi_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: +2 aksiyani OLIB, ISHLATIB bo'lganlar (balansi 0, obunasi yo'q) ro'yxati."""
    if not is_admin(update.effective_user.id):
        return
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    rows = _db_execute(
        "SELECT user_id, username FROM users "
        "WHERE COALESCE(aksiya_given, FALSE) = TRUE "
        "AND COALESCE(balance,0) <= 0 "
        "AND (sub_until IS NULL OR sub_until <= %s) "
        "ORDER BY user_id DESC",
        (now,), fetch='all'
    ) or []
    if not rows:
        await update.message.reply_text("📭 +2 aksiyani olib, ishlatib bo'lgan foydalanuvchi yo'q.")
        return
    lines = [f"📊 +2 AKSIYANI ISHLATIB BO'LGANLAR ({len(rows)} ta):\n"]
    for i, (uid, uname) in enumerate(rows, 1):
        who = f"@{uname}" if uname else f"(username yo'q)"
        lines.append(f"{i}. {who} — ID: {uid}")
    text = "\n".join(lines)
    # Telegram xabar limiti ~4000 belgi - bo'lib yuboramiz
    for i in range(0, len(text), 4000):
        await update.message.reply_text(text[i:i+4000])


async def avto_aksiya_yoq_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: avtomatik +2 aksiyani YOQADI (1 ta bepul tugaganlarga o'zi beradi)."""
    if not is_admin(update.effective_user.id):
        return
    set_setting("auto_aksiya", "on")
    await update.message.reply_text(
        "✅ Avtomatik +2 aksiya YOQILDI.\n\n"
        "Endi 1 ta bepulni ishlatib, balansi tugagan har bir foydalanuvchiga "
        "avtomatik +2 bepul tahlil beriladi (har biriga 1 marta).\n\n"
        "⚠️ Diqqat: bu Gemini xarajatini oshiradi. Kerak bo'lmasa /avto_aksiya_ochir bilan o'chiring."
    )


async def avto_aksiya_ochir_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: avtomatik +2 aksiyani O'CHIRADI."""
    if not is_admin(update.effective_user.id):
        return
    set_setting("auto_aksiya", "off")
    await update.message.reply_text(
        "🛑 Avtomatik +2 aksiya O'CHIRILDI.\n\n"
        "Endi yangi foydalanuvchilarga avtomatik +2 berilmaydi. "
        "Kerak bo'lganda /aksiya (qo'lda) yoki /avto_aksiya_yoq ishlatishingiz mumkin."
    )


async def javoblar_bugun_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: faqat BUGUNGI so'rov javoblari (eng yangisi birinchi)."""
    if not is_admin(update.effective_user.id):
        return
    if update.effective_chat.type != "private":
        return  # Guruhda ishlamaydi - faqat shaxsiy chatda
    today = datetime.now().strftime("%Y-%m-%d")
    rows = _db_execute(
        "SELECT username, javob1, javob2, created FROM sorov_javoblar "
        "WHERE created LIKE %s ORDER BY id DESC LIMIT 300",
        (today + "%",), fetch='all'
    ) or []
    if not rows:
        await update.message.reply_text("📭 Bugun hali so'rov javobi yo'q.")
        return
    header = f"📝 BUGUNGI SO'ROV JAVOBLARI ({len(rows)} ta):\n\n"
    blocks = []
    for i, (uname, j1, j2, created) in enumerate(rows, 1):
        who = f"@{uname}" if uname else "(no username)"
        blocks.append(
            f"{i}. {who} • {created}\n"
            f"   1️⃣ Yoqdi: {j1}\n"
            f"   2️⃣ Taklif: {j2}"
        )
    text = header + "\n\n".join(blocks)
    for i in range(0, len(text), 4000):
        await update.message.reply_text(text[i:i+4000])


async def javoblar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: barcha so'rov javoblarini bir joyda ko'rsatadi (eng yangisi birinchi)."""
    if not is_admin(update.effective_user.id):
        return
    if update.effective_chat.type != "private":
        return  # Guruhda ishlamaydi - faqat shaxsiy chatda
    rows = _db_execute(
        "SELECT username, javob1, javob2, created FROM sorov_javoblar ORDER BY id DESC LIMIT 300",
        fetch='all'
    ) or []
    if not rows:
        await update.message.reply_text("📭 Hali so'rov javobi yo'q.")
        return
    header = f"📝 SO'ROV JAVOBLARI ({len(rows)} ta, eng yangisi birinchi):\n\n"
    blocks = []
    for i, (uname, j1, j2, created) in enumerate(rows, 1):
        who = f"@{uname}" if uname else "(no username)"
        blocks.append(
            f"{i}. {who} • {created}\n"
            f"   1️⃣ Yoqdi: {j1}\n"
            f"   2️⃣ Taklif: {j2}"
        )
    text = header + "\n\n".join(blocks)
    # Telegram limiti ~4000 belgi - bo'lib yuboramiz
    for i in range(0, len(text), 4000):
        await update.message.reply_text(text[i:i+4000])


async def yangilik_test_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: yangilik xabarini FAQAT o'ziga yuboradi (ko'rib olish uchun, hammaga ketmaydi)."""
    if not is_admin(update.effective_user.id):
        return
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(TEXTS['uz']['yangilik_btn'], callback_data="boshlash")
    ]])
    await context.bot.send_message(
        update.effective_chat.id, TEXTS['uz']['yangilik_msg'],
        reply_markup=kb, parse_mode="HTML"
    )
    await update.message.reply_text(
        "👆 Yangilik xabari shunday ko'rinadi (faqat sizga yuborildi).\n\n"
        "Hammaga yuborish uchun: /yangilik"
    )


async def juma_ochir_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: juma aksiyasini YOQADI."""
    if not is_admin(update.effective_user.id):
        return
    set_setting("juma_aksiya", "on")
    await update.message.reply_text(
        "✅ Juma aksiyasi YOQILDI!\n\n"
        "Endi har juma:\n"
        "• 10:00 — premium olmaganlarga 1 bepul + xabar\n"
        "• 20:00 — eslatma\n"
        "• Shanba — ishlatilmagan kuyadi\n\n"
        "O'chirish: /juma_yoq"
    )


async def juma_yoq_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: juma aksiyasini O'CHIRADI."""
    if not is_admin(update.effective_user.id):
        return
    set_setting("juma_aksiya", "off")
    await update.message.reply_text(
        "🔴 Juma aksiyasi O'CHIRILDI.\n\n"
        "Endi juma kuni avtomatik bepul berilmaydi.\n"
        "Yoqish: /juma_ochir"
    )


async def video_test_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: ishonch videosini FAQAT o'ziga yuboradi (ko'rish/test uchun)."""
    if not is_admin(update.effective_user.id):
        return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(TEXTS['uz']['video_2btn_sovga'], callback_data="video_sovga_ol")],
        [InlineKeyboardButton(TEXTS['uz']['video_2btn_fikr'], callback_data="video_fikr")],
    ])
    try:
        # Avval intro xabar (video'dan oldin)
        await context.bot.send_message(update.effective_chat.id, TEXTS['uz']['video_intro'])
        await asyncio.sleep(0.5)
        await context.bot.send_video(
            update.effective_chat.id, VIDEO_FILE_ID,
            caption=TEXTS['uz']['video_caption'], reply_markup=kb, parse_mode="HTML"
        )
        await update.message.reply_text(
            "👆 Video shunday ko'rinadi. Yoqsa — /video_xabar bilan hammaga yuboring."
        )
    except Exception as e:
        await update.message.reply_text(f"⚠️ Video yuborilmadi: {e}\nVIDEO_FILE_ID to'g'rimi tekshiring.")


async def video_xabar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: ishonch videosini HAMMAga yuboradi (1 marta, video_xabar_given)."""
    if not is_admin(update.effective_user.id):
        return
    rows = _db_execute(
        "SELECT user_id FROM users WHERE COALESCE(video_xabar_given, FALSE) = FALSE",
        fetch='all') or []
    # Premium (admin/obuna) larga yubormaymiz - ular allaqachon ishongan
    rows = [r for r in rows if not is_admin(r[0]) and not sub_active(r[0])]
    if not rows:
        await update.message.reply_text("📭 Video yuboriladigan foydalanuvchi yo'q (hammasi olgan).")
        return
    await update.message.reply_text(
        f"🎬 Video yuborish boshlandi: {len(rows)} ta foydalanuvchiga...\n(Sekin yuboriladi, kuting)")
    sent, failed = 0, 0
    for r in rows:
        uid = r[0]
        try:
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton(TEXTS['uz']['video_2btn_sovga'], callback_data="video_sovga_ol")],
                [InlineKeyboardButton(TEXTS['uz']['video_2btn_fikr'], callback_data="video_fikr")],
            ])
            # Avval intro xabar (video'dan oldin)
            await context.bot.send_message(uid, TEXTS['uz']['video_intro'])
            await asyncio.sleep(0.3)
            await context.bot.send_video(uid, VIDEO_FILE_ID,
                                         caption=TEXTS['uz']['video_caption'], reply_markup=kb,
                                         parse_mode="HTML")
            _db_execute("UPDATE users SET video_xabar_given = TRUE WHERE user_id = %s", (uid,))
            sent += 1
        except Exception as e:
            failed += 1
            logger.warning(f"Video yuborishda xato (uid={uid}): {e}")
        await asyncio.sleep(0.4)
    await update.message.reply_text(
        f"✅ Video yuborildi!\n📨 Yuborildi: {sent}\n⚠️ Yuborilmadi: {failed}")


async def videoid_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: keyingi yuborilgan videoning kodini (file_id) ko'rsatadi."""
    if not is_admin(update.effective_user.id):
        return
    context.user_data['mode'] = 'get_videoid'
    await update.message.reply_text(
        "🎬 Endi videongizni yuboring — men uning kodini (file_id) ko'rsataman.\n"
        "(Bu video tahlil qilinmaydi, faqat kod olinadi)"
    )


async def xatolar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: bugungi xatolar hisoboti."""
    if not is_admin(update.effective_user.id):
        return
    bugun = datetime.now().strftime("%Y-%m-%d")
    if _xato_hisob.get("sana") != bugun:
        await update.message.reply_text("✅ Bugun hali xato qayd etilmagan (yoki bot endi ishga tushgan).")
        return
    s503 = _xato_hisob.get("503", 0)
    stime = _xato_hisob.get("timeout", 0)
    sfayl = _xato_hisob.get("fayl", 0)
    sbosh = _xato_hisob.get("boshqa", 0)
    jami = s503 + stime + sfayl + sbosh
    txt = (
        f"📊 <b>BUGUNGI XATOLAR</b> ({bugun})\n\n"
        f"🔴 503 (Gemini band): <b>{s503}</b>\n"
        f"⏳ Timeout (juda sekin): <b>{stime}</b>\n"
        f"📁 Fayl (video yuklanmadi): <b>{sfayl}</b>\n"
        f"❓ Boshqa: <b>{sbosh}</b>\n"
        f"━━━━━━━━━━━━━\n"
        f"Jami: <b>{jami}</b> ta xato\n\n"
    )
    if s503 > 50:
        txt += "⚠️ 503 ko'p — Google tomonida yuklama. Bot Flash'ga o'tib ishlamoqda."
    elif sfayl > 20:
        txt += "⚠️ Fayl xato ko'p — internet yoki Pyrogram muammosi bo'lishi mumkin."
    elif jami < 10:
        txt += "✅ Hammasi yaxshi — xato kam."
    await update.message.reply_text(txt, parse_mode="HTML")


async def drip_on_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: drip tizimini yoqadi (bugundan keyin kirganlar uchun)."""
    if not is_admin(update.effective_user.id):
        return
    bugun = datetime.now().strftime("%Y-%m-%d")
    set_setting("drip_start", bugun)
    await update.message.reply_text(
        f"✅ Drip tizimi YOQILDI! (boshlanish: {bugun})\n\n"
        "Bugundan keyin kirgan yangi foydalanuvchilar uchun:\n"
        "• 2-kun: jamoa videosi + 1 bepul\n"
        "• 3-kun: 7 kunlik aksiya\n\n"
        "Eski 10 000 foydalanuvchiga BORMAYDI (faqat yangi).\n"
        "Har kuni 19:00 da avtomatik ishlaydi.\n"
        "O'chirish: /drip_off")


async def drip_holat_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: drip statistikasi - necha kishi drip2/drip3 oldi (ishlayaptimi)."""
    if not is_admin(update.effective_user.id):
        return
    drip_start = get_setting("drip_start", "")
    d2 = _db_execute("SELECT COUNT(*) FROM users WHERE drip2_given = TRUE", fetch='one')
    d3 = _db_execute("SELECT COUNT(*) FROM users WHERE drip3_given = TRUE", fetch='one')
    d2n = d2[0] if d2 else 0
    d3n = d3[0] if d3 else 0
    yangi = 0
    if drip_start:
        rows = _db_execute("SELECT joined FROM users WHERE joined IS NOT NULL", fetch='all') or []
        try:
            ds = datetime.strptime(drip_start, "%Y-%m-%d")
            for r in rows:
                try:
                    if datetime.strptime(r[0][:10], "%Y-%m-%d") >= ds:
                        yangi += 1
                except Exception:
                    continue
        except Exception:
            pass
    holat = "🟢 YOQILGAN" if drip_start else "🔴 O'CHIQ"
    natija = "✅ Drip ishlayapti!" if (d2n + d3n) > 0 else "⏳ Hali xabar yuborilmagan (yangilar 2-kunga yetmagan yoki drip yangi yoqilgan)."
    await update.message.reply_text(
        f"📊 DRIP HOLATI\n\n"
        f"Holat: {holat}\n"
        f"Boshlanish: {drip_start or '—'}\n"
        f"Drip boshlangach kirgan: {yangi} ta\n\n"
        f"📤 Yuborilgan:\n"
        f"• 2-kun (jamoa video): {d2n} ta\n"
        f"• 3-kun (7 kunlik aksiya): {d3n} ta\n\n"
        f"{natija}")


async def drip_off_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: drip tizimini o'chiradi."""
    if not is_admin(update.effective_user.id):
        return
    set_setting("drip_start", "")
    await update.message.reply_text("🔴 Drip tizimi o'chirildi. Yoqish: /drip_on")


async def aksiya_ber_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: bitta foydalanuvchiga (ID bo'yicha) 7 kunlik aksiyani yuboradi."""
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Ishlatish: /aksiya_ber <ID>\nMasalan: /aksiya_ber 123456789")
        return
    try:
        uid = int(context.args[0])
    except Exception:
        await update.message.reply_text("⚠️ ID raqam bo'lishi kerak. Masalan: /aksiya_ber 123456789")
        return
    # Aksiyani yoqamiz (tugma ishlasin)
    set_setting("tarif7_aksiya", "on")
    try:
        payme_link = _payme_checkout_link(uid, TEST_PRICE)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(TEXTS['uz']['tarif7_2soat_btn'], url=payme_link)],
            [InlineKeyboardButton(TEXTS['uz']['tarif7_btn_card'], callback_data='card_test')],
        ])
        await context.bot.send_message(uid, TEXTS['uz']['test_taklif_msg'],
                                       reply_markup=kb, parse_mode="HTML")
        await update.message.reply_text(f"✅ {uid} ga 7 kunlik aksiya yuborildi.")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Yuborilmadi: {e}\n(Foydalanuvchi botni bloklagan bo'lishi mumkin)")


async def aksiya_off_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: 7 kunlik aksiyani DARROV o'chiradi."""
    if not is_admin(update.effective_user.id):
        return
    set_setting("tarif7_aksiya", "off")
    set_setting("tarif7_tugash", "")
    await update.message.reply_text(
        "🔴 7 kunlik aksiya DARROV o'chirildi!\n\n"
        "Endi 7 minglik tugma bosilsa — 'aksiya tugadi, to'liq obuna' chiqadi.\n"
        "Qayta yoqish: /aksiya_on yoki /tarif7")


async def aksiya_on_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: 7 kunlik aksiyani qayta yoqadi (yubormasdan, faqat tugma ishlasin)."""
    if not is_admin(update.effective_user.id):
        return
    set_setting("tarif7_aksiya", "on")
    set_setting("tarif7_tugash", "")  # vaqt cheksiz (qo'lda o'chirguncha)
    await update.message.reply_text(
        "✅ 7 kunlik aksiya yoqildi (tugma ishlaydi).\n"
        "Hammaga yuborish: /tarif7\nO'chirish: /aksiya_off")


async def buyruqlar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: barcha admin buyruqlari ro'yxati."""
    if not is_admin(update.effective_user.id):
        return
    txt = (
        "🛠 <b>ADMIN BUYRUQLARI</b>\n\n"
        "📊 <b>STATISTIKA</b>\n"
        "/admin — umumiy statistika\n"
        "/voronka — konversiya voronkasi\n"
        "/aktiv — aktivlik + eng faol soatlar\n"
        "/top — eng faol foydalanuvchilar\n"
        "/bugun — bugungi statistika\n"
        "/obunachilar — obunachilar ro'yxati\n"
        "/premium_xarajat — xarajat vs to'lov\n"
        "/aksiya_tugadi — aksiya statistikasi\n\n"
        "🎁 <b>SOVG'A / OBUNA</b>\n"
        "/berbalans &lt;ID&gt; &lt;son&gt; — bepul tahlil berish\n"
        "/berbalans_premium &lt;ID&gt; &lt;son&gt; — PREMIUM tahlil (hook+ovoz)\n"
        "/balans_0 &lt;ID&gt; — hamma balansni 0 qilish\n"
        "/kop_balans — balansi ko'p (shubhali) odamlar\n"
        "/sotuv_balans_0 — hamma ortiqcha balansni tozalash\n"
        "/blok &lt;ID&gt; — foydalanuvchini bloklash\n"
        "/blok_ochir &lt;ID&gt; — blokdan chiqarish\n"
        "/berobuna &lt;ID&gt; &lt;kun&gt; — obuna berish\n"
        "/obunaochir &lt;ID&gt; — obunani bekor qilish\n"
        "/yoz &lt;ID&gt; &lt;xabar&gt; — xabar yuborish\n\n"
        "📢 <b>BROADCAST</b>\n"
        "/yangilik — yangilanish xabari (1 marta)\n"
        "/yangilik_test — yangilanishni o'zingga ko'r\n"
        "/tarif7 — 7 kunlik taklif (qayta-qayta)\n"
        "/eslatma — eslatma (uz/ru, 1 marta)\n"
        "/aksiya — aksiya (+1 tahlil)\n"
        "/obuna_taklif — obuna taklifi\n"
        "/test_sorov — 7 kunlik so'rov\n"
        "/test_taklif — 7 kunlik taklif\n"
        "/sorov — so'rov (+1 bonus)\n\n"
        "💳 <b>TO'LOV / EFFEKT TEST</b>\n"
        "/paymetest — Payme havolani sinash\n"
        "/fireworks_test — konfetti sinash\n\n"
        "🎁 <b>JUMA AKSIYASI</b>\n"
        "/juma_ochir — juma aksiyasini yoqish\n"
        "/juma_yoq — juma aksiyasini o'chirish\n\n"
        "🎬 <b>VIDEO</b>\n"
        "/videoid — video kodini (file_id) olish\n\n"
        "🤖 <b>AVTO</b>\n"
        "/avto_aksiya_ochir — avto-aksiya yoqish\n"
        "/avto_aksiya_yoq — avto-aksiya o'chirish\n\n"
        "💬 <b>JAVOBLAR</b>\n"
        "/javoblar — so'rov javoblari\n"
        "/javoblar_bugun — bugungi javoblar\n\n"
        "💰 <b>CHEGIRMA / SOTUV</b>\n"
        "/chegirma — chegirma yoqish (22:00 gacha)\n"
        "/chegirma_ochir — chegirmani o'chirish\n"
        "/sotuv_matn &lt;matn&gt; — sotuv matnini o'zgartirish\n"
        "/sotuv_korish — sotuv matnini ko'rish\n"
        "/sotuv_matn_tikla — sotuv matnini tiklash\n\n"
        "🚀 <b>SOTUV VORONKASI</b> (real vaqtda, takrorsiz)\n"
        "/sotuv_test — hammasini FAQAT o'zingizga (sinash)\n"
        "/sotuv_natija — har sotuv natijasi (bosdi, to'ladi)\n"
        "/renewal_hozir — obuna tugash eslatmasini sinash\n"
        "/renewal_royxat — kimga renewal/chegirma yuborilgan\n"
        "/test_tugaganlar — 7 kunligi tugaganlarga 20% taklif\n"
        "/sotuv_reset &lt;raqam&gt; — sotuv flagini tiklash (qayta yuborish)\n"
        "/bepul_royxat — kim bepul premium oldi\n"
        "/sotuv1 — 5+ ishlatgan, to'lamaganga SAVOL (+bepul)\n"
        "/sotuv1b — 5+ ishlatganga TAKLIF (19,900)\n"
        "/sotuv3 — 2-4 ishlatganga premium sinash (+bepul)\n"
        "/sotuv4 — 1 marta ishlatganga (+bepul premium)\n"
        "/sotuv5 — ishlatmaganlarga (video tugma)\n"
        "/tahlil_faol — faol↔to'lovchi tahlili\n"
        "/tolovchilar — aktiv obunachilar (ID, paket, tugash)\n\n"
        "🔧 <b>QO'SHIMCHA</b>\n"
        "/kim &lt;ID&gt; — ID dan username topish\n"
        "/drip_holat — drip statistikasi"
    )
    await update.message.reply_text(txt, parse_mode="HTML")


async def tarif7_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: 7 kunlik Premium taklifini premium OLMAGAN hammaga yuboradi.
    Qayta-qayta yuborish mumkin (belgilanmaydi)."""
    if not is_admin(update.effective_user.id):
        return
    # Aksiyani YOQAMIZ (buy_test ishlasin) + ertaga 20:00 eslatma, 22:00 o'chirish jadvali
    set_setting("tarif7_aksiya", "on")
    # Aksiya tugash vaqtini ham saqlaymiz (restart bo'lsa ham ishlasin - vaqtga bog'liq).
    # Ertaga 22:00 (UZ) gacha. UZ->UTC.
    try:
        UZ_OFF = int(os.getenv("UZ_TZ_OFFSET", "5"))
        tugash_uz = (datetime.utcnow() + timedelta(hours=UZ_OFF) + timedelta(days=1)).replace(
            hour=22, minute=0, second=0, microsecond=0)
        set_setting("tarif7_tugash", tugash_uz.strftime("%Y-%m-%d %H:%M"))
    except Exception:
        pass
    try:
        jq = context.application.job_queue
        if jq is not None:
            UZ_OFF = int(os.getenv("UZ_TZ_OFFSET", "5"))
            now_utc = datetime.utcnow()
            # Ertaga (UZ) 20:00 va 22:00 ni UTC ga o'giramiz
            # UZ 20:00 = UTC (20 - UZ_OFF), UZ 22:00 = UTC (22 - UZ_OFF)
            def _ertaga_utc(uz_soat):
                utc_soat = (uz_soat - UZ_OFF) % 24
                target = (now_utc + timedelta(days=1)).replace(
                    hour=utc_soat, minute=0, second=0, microsecond=0)
                # Agar allaqachon o'tib ketgan bo'lsa (UTC), keyingi kunga suramiz emas - ertaga aniq
                delay = (target - now_utc).total_seconds()
                if delay < 0:
                    delay += 24 * 3600
                return delay
            jq.run_once(tarif7_2soat_eslatma, when=_ertaga_utc(20))
            jq.run_once(tarif7_ochirish, when=_ertaga_utc(22))
            logger.info("tarif7: ertaga 20:00 eslatma, 22:00 o'chirish rejalashtirildi")
    except Exception as e:
        logger.error(f"tarif7 jadval xato: {e}")
    rows = _db_execute("SELECT user_id, tarif7_sana FROM users", fetch='all') or []
    # 3 kun ichida olganlarga QAYTA yubormaymiz (bezovta qilmaslik uchun)
    bugun = datetime.now()
    targets = []
    for r in rows:
        uid = r[0]
        if is_admin(uid) or sub_active(uid):
            continue
        oxirgi = r[1]  # tarif7_sana (oxirgi yuborilgan sana)
        if oxirgi:
            try:
                farq = (bugun - datetime.strptime(oxirgi, "%Y-%m-%d %H:%M")).total_seconds()
                if farq < 3 * 24 * 3600:  # 3 kundan kam -> o'tkazamiz
                    continue
            except Exception:
                pass
        targets.append(uid)
    if not targets:
        await update.message.reply_text("📭 Yuboriladigan foydalanuvchi yo'q (hammasi 3 kun ichida olgan).")
        return
    await update.message.reply_text(
        f"🎯 7 kunlik taklif boshlandi: {len(targets)} ta foydalanuvchiga...\n(Sekin yuboriladi, kuting)")
    sent, failed = 0, 0
    for uid in targets:
        try:
            payme_link = _payme_checkout_link(uid, TEST_PRICE)
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton(TEXTS['uz']['tarif7_btn_payme'], url=payme_link)],
                [InlineKeyboardButton(TEXTS['uz']['tarif7_btn_card'], callback_data='card_test')],
            ])
            await context.bot.send_message(uid, TEXTS['uz']['test_taklif_msg'],
                                           reply_markup=kb, parse_mode="HTML")
            _db_execute("UPDATE users SET tarif7_sana = %s WHERE user_id = %s",
                        (bugun.strftime("%Y-%m-%d %H:%M"), uid))
            sent += 1
        except Exception as e:
            failed += 1
            logger.warning(f"tarif7 yuborishda xato (uid={uid}): {e}")
        await asyncio.sleep(0.4)
    await update.message.reply_text(
        f"✅ 7 kunlik taklif tugadi!\n📨 Yuborildi: {sent}\n⚠️ Yuborilmadi: {failed}\n\n"
        f"(Istalgan vaqt /tarif7 bilan qayta yuborishingiz mumkin)"
    )


async def paymetest_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: Payme GET havolani sinab ko'rish (5090 so'm = 1 martalik)."""
    if not is_admin(update.effective_user.id):
        return
    if not PAYME_MERCHANT_ID:
        await update.message.reply_text("⚠️ PAYME_MERCHANT_ID o'rnatilmagan (Railway Variables).")
        return
    summa = ONE_PRICE  # 5090 so'm = 1 martalik paket
    link = _payme_checkout_link(update.effective_user.id, summa)
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"💳 {summa:,} so'm to'lash (TEST)", url=link)
    ]])
    await update.message.reply_text(
        f"🧪 <b>PAYME TEST</b>\n\n"
        f"Summa: <b>{summa:,} so'm</b> (1 martalik paket)\n\n"
        f"Tugmani bosing → Payme sahifasi ochiladi → to'lang.\n"
        f"To'lov o'tsa: <b>+1 tahlil</b> qo'shiladi va <b>konfetti</b> chiqadi.\n\n"
        f"🔗 Havola:\n<code>{link}</code>",
        reply_markup=kb, parse_mode="HTML"
    )


async def tahlil_faol_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: 5+ faol VA to'lovchilar kesishmasi. 'Faol->to'laydi' gipotezasini tekshiradi."""
    if not is_admin(update.effective_user.id):
        return
    faol_rows = _db_execute(
        "SELECT user_id FROM analyses GROUP BY user_id HAVING COUNT(*) >= 5", fetch='all') or []
    faol_set = set(r[0] for r in faol_rows)
    paid_rows = _db_execute(
        "SELECT DISTINCT user_id FROM payments WHERE status = 'approved'", fetch='all') or []
    paid_set = set(r[0] for r in paid_rows)
    faol_n = len(faol_set)
    paid_n = len(paid_set)
    faol_va_paid = len(faol_set & paid_set)
    faol_emas_paid = len(paid_set - faol_set)
    faol_paid_emas = len(faol_set - paid_set)
    faol_conv = (faol_va_paid / faol_n * 100) if faol_n else 0
    paid_faol_pct = (faol_va_paid / paid_n * 100) if paid_n else 0
    txt = "🔬 <b>FAOL ↔ TO'LOVCHI TAHLILI</b>\n\n"
    txt += f"🔥 5+ tahlil (faol): <b>{faol_n}</b>\n"
    txt += f"💰 To'lovchilar: <b>{paid_n}</b>\n\n"
    txt += "━━━━━━━━━━━━━\n"
    txt += f"✅ 5+ VA to'lagan: <b>{faol_va_paid}</b>\n"
    txt += f"🔥 5+, to'lamagan: <b>{faol_paid_emas}</b>\n"
    txt += f"💸 To'lagan, 5+ EMAS: <b>{faol_emas_paid}</b>\n\n"
    txt += "━━━━━━━━━━━━━\n📈 <b>XULOSA:</b>\n"
    txt += f"• 5+ faollarning <b>{faol_conv:.0f}%</b> to'lagan\n"
    txt += f"• To'lovchilarning <b>{paid_faol_pct:.0f}%</b> 5+ faol edi\n\n"
    if paid_faol_pct >= 60:
        txt += "✅ Gipoteza TASDIQLANDI: to'lovchilar asosan 5+ faollardan!\n→ Strategiya: odamlarni 5+ ga yetkazish."
    elif faol_conv >= 60:
        txt += "✅ 5+ faollar yaxshi to'laydi, lekin to'lovchilarning ko'pi boshqa yo'ldan.\n→ Faollik + boshqa kanallar."
    else:
        txt += "⚠️ To'lovchilar 5+ faollardan EMAS.\n→ Boshqa omillarni izlash kerak."
    await update.message.reply_text(txt, parse_mode="HTML")


async def voronka_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: TO'LIQ dashboard - voronka + pul + vaqt + segment + to'lov usullari + xarajat."""
    if not is_admin(update.effective_user.id):
        return
    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M")
    bugun = now.strftime("%Y-%m-%d")
    oy = now.strftime("%Y-%m")
    hafta_boshi = (now - timedelta(days=7)).strftime("%Y-%m-%d %H:%M")

    def one(q, p=None):
        r = _db_execute(q, p, fetch='one') if p else _db_execute(q, fetch='one')
        return (r[0] if r and r[0] is not None else 0)

    def pct(a, b):
        return f"{(a/b*100):.1f}%" if b else "0%"

    # ==== VORONKA ====
    jami = one("SELECT COUNT(*) FROM users")
    tahlil_qilgan = one("SELECT COUNT(DISTINCT user_id) FROM analyses")
    qaytgan = one("SELECT COUNT(*) FROM (SELECT user_id FROM analyses GROUP BY user_id HAVING COUNT(*) >= 2) t")
    faol = one("SELECT COUNT(*) FROM (SELECT user_id FROM analyses GROUP BY user_id HAVING COUNT(*) >= 5) t")
    tolagan = one("SELECT COUNT(DISTINCT user_id) FROM payments WHERE status = 'approved'")
    aktiv_sub = one("SELECT COUNT(*) FROM users WHERE sub_until IS NOT NULL AND sub_until > %s", (now_str,))

    # ==== PUL ====
    jami_daromad = one("SELECT COALESCE(SUM(amount),0) FROM payments WHERE status = 'approved'")
    oy_daromad = one("SELECT COALESCE(SUM(amount),0) FROM payments WHERE status = 'approved' AND created LIKE %s", (oy + "%",))
    bugun_daromad = one("SELECT COALESCE(SUM(amount),0) FROM payments WHERE status = 'approved' AND created LIKE %s", (bugun + "%",))
    tolov_soni = one("SELECT COUNT(*) FROM payments WHERE status = 'approved'")
    ort_tolov = (jami_daromad / tolov_soni) if tolov_soni else 0

    # ==== XARAJAT (tannarx) ====
    jami_xarajat = one("SELECT COALESCE(SUM(narx),0) FROM analyses")
    oy_xarajat = one("SELECT COALESCE(SUM(narx),0) FROM analyses WHERE created LIKE %s", (oy + "%",))
    foyda = jami_daromad - jami_xarajat

    # ==== VAQT ====
    bugun_yangi = one("SELECT COUNT(*) FROM users WHERE joined LIKE %s", (bugun + "%",))
    hafta_yangi = one("SELECT COUNT(*) FROM users WHERE joined >= %s", (hafta_boshi,))
    oy_yangi = one("SELECT COUNT(*) FROM users WHERE joined LIKE %s", (oy + "%",))
    bugun_tahlil = one("SELECT COUNT(*) FROM analyses WHERE created LIKE %s", (bugun + "%",))
    hafta_tahlil = one("SELECT COUNT(*) FROM analyses WHERE created >= %s", (hafta_boshi,))
    bugun_tolov = one("SELECT COUNT(*) FROM payments WHERE status='approved' AND created LIKE %s", (bugun + "%",))

    # ==== SEGMENTLAR (sotuv uchun) ====
    faol_set = set(r[0] for r in (_db_execute("SELECT user_id FROM analyses GROUP BY user_id HAVING COUNT(*) >= 5", fetch='all') or []))
    paid_set = set(r[0] for r in (_db_execute("SELECT DISTINCT user_id FROM payments WHERE status='approved'", fetch='all') or []))
    issiq = len(faol_set - paid_set)          # 5+ lekin to'lamagan
    tolagan_5emas = len(paid_set - faol_set)  # to'lagan lekin 5+ emas
    faol_tolagan = len(faol_set & paid_set)
    ishlatmagan = one("SELECT COUNT(*) FROM users u LEFT JOIN (SELECT DISTINCT user_id FROM analyses) a ON a.user_id=u.user_id WHERE a.user_id IS NULL")

    # ==== TO'LOV USULLARI ====
    total_analiz = one("SELECT COUNT(*) FROM analyses")
    ort_tahlil = (total_analiz / tahlil_qilgan) if tahlil_qilgan else 0

    txt = "📊 <b>TO'LIQ DASHBOARD</b>\n"
    txt += "━━━━━━━━━━━━━\n"
    txt += "<b>🔻 VORONKA</b>\n"
    txt += f"👥 Ro'yxat: {jami:,}\n"
    txt += f"🎬 Tahlil qilgan: {tahlil_qilgan:,} ({pct(tahlil_qilgan, jami)})\n"
    txt += f"🔁 Qaytgan (2+): {qaytgan:,} ({pct(qaytgan, jami)})\n"
    txt += f"🔥 Faol (5+): {faol:,} ({pct(faol, jami)})\n"
    txt += f"💰 To'lagan: {tolagan:,} ({pct(tolagan, jami)})\n"
    txt += f"💎 Aktiv obuna: {aktiv_sub:,}\n\n"

    txt += "<b>💰 PUL</b>\n"
    txt += f"Jami daromad: {jami_daromad:,} so'm\n"
    txt += f"Shu oy: {oy_daromad:,} so'm\n"
    txt += f"Bugun: {bugun_daromad:,} so'm\n"
    txt += f"To'lovlar soni: {tolov_soni:,}\n"
    txt += f"O'rtacha to'lov: {ort_tolov:,.0f} so'm\n\n"

    txt += "<b>📉 XARAJAT / FOYDA</b>\n"
    txt += f"Jami xarajat (AI): {jami_xarajat:,} so'm\n"
    txt += f"Shu oy xarajat: {oy_xarajat:,} so'm\n"
    txt += f"💵 Sof foyda: {foyda:,} so'm\n\n"

    txt += "<b>📅 VAQT (yangi/faollik)</b>\n"
    txt += f"Bugun yangi: {bugun_yangi:,} | tahlil: {bugun_tahlil:,} | to'lov: {bugun_tolov:,}\n"
    txt += f"Hafta yangi: {hafta_yangi:,} | tahlil: {hafta_tahlil:,}\n"
    txt += f"Oy yangi: {oy_yangi:,}\n"
    txt += f"O'rtacha tahlil/user: {ort_tahlil:.1f}\n\n"

    txt += "<b>🎯 SEGMENTLAR (sotuv)</b>\n"
    txt += f"🔥 5+ to'lamagan (issiq): {issiq}\n"
    txt += f"💸 To'lagan, 5+ emas: {tolagan_5emas}\n"
    txt += f"✅ 5+ va to'lagan: {faol_tolagan}\n"
    txt += f"❄️ Umuman ishlatmagan: {ishlatmagan:,}\n\n"

    txt += "━━━━━━━━━━━━━\n<b>📈 XULOSA</b>\n"
    if jami:
        r_tahlil = tahlil_qilgan / jami
        r_qaytish = qaytgan / tahlil_qilgan if tahlil_qilgan else 0
        r_tolov = tolagan / qaytgan if qaytgan else 0
        if r_tahlil < 0.5:
            txt += "⚠️ Ko'p odam tahlil HAM qilmagan — onboarding zaif.\n"
        if r_qaytish < 0.3:
            txt += "⚠️ Qaytish zaif (retention).\n"
        else:
            txt += "✅ Qaytish yaxshi.\n"
        if r_tolov < 0.05:
            txt += "⚠️ To'lov past — qiymat/narx/ishonch.\n"
        else:
            txt += "✅ To'lov yomon emas.\n"
        if issiq > 20:
            txt += f"💡 {issiq} ta issiq (5+ to'lamagan) bor — /sotuv1 yubor!\n"
    await update.message.reply_text(txt, parse_mode="HTML")


async def yangilik_test_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: yangilik xabarini FAQAT o'ziga yuboradi (ko'rish/test uchun)."""
    if not is_admin(update.effective_user.id):
        return
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(TEXTS['uz']['yangilik_btn'], callback_data="boshlash")
    ]])
    await context.bot.send_message(update.effective_chat.id, TEXTS['uz']['yangilik_msg'],
                                   reply_markup=kb, parse_mode="HTML")
    await update.message.reply_text(
        "👆 Yangilik xabari shunday ko'rinadi. Yoqsa — /yangilik bilan hammaga yuboring."
    )


async def yangilik_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: yangilanish xabari HAMMAga (1 marta) + 'Boshlash' tugmasi (menyu yangilanadi)."""
    if not is_admin(update.effective_user.id):
        return
    rows = _db_execute(
        "SELECT user_id FROM users WHERE COALESCE(yangilik_given, FALSE) = FALSE",
        fetch='all'
    ) or []
    if not rows:
        await update.message.reply_text("📭 Yangilik yuboriladigan foydalanuvchi yo'q (hammasi olgan).")
        return
    await update.message.reply_text(f"🎉 Yangilik boshlandi: {len(rows)} ta foydalanuvchiga...\n(Sekin yuboriladi, kuting)")

    sent, failed = 0, 0
    for row in rows:
        uid = row[0]
        try:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton(TEXTS['uz']['yangilik_btn'], callback_data="boshlash")
            ]])
            await context.bot.send_message(uid, TEXTS['uz']['yangilik_msg'],
                                           reply_markup=kb, parse_mode="HTML")
            _db_execute("UPDATE users SET yangilik_given = TRUE WHERE user_id = %s", (uid,))
            sent += 1
        except Exception as e:
            failed += 1
            logger.warning(f"Yangilik yuborishda xato (uid={uid}): {e}")
        await asyncio.sleep(0.4)

    await update.message.reply_text(
        f"✅ Yangilik tugadi!\n📨 Yuborildi: {sent}\n⚠️ Yuborilmadi: {failed}"
    )


async def eslatma_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: yangilanish/uzr eslatmasini HAMMAga yuboradi (ikki tilli, 1 marta)."""
    if not is_admin(update.effective_user.id):
        return
    rows = _db_execute(
        "SELECT user_id FROM users WHERE COALESCE(eslatma_given, FALSE) = FALSE",
        fetch='all'
    ) or []
    if not rows:
        await update.message.reply_text("📭 Eslatma yuboriladigan foydalanuvchi yo'q (hammasi olgan).")
        return
    await update.message.reply_text(f"🛠 Eslatma boshlandi: {len(rows)} ta foydalanuvchiga...\n(Sekin yuboriladi, kuting)")

    sent, failed = 0, 0
    for row in rows:
        uid = row[0]
        try:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton(TEXTS['uz']['eslatma_ru_btn'], callback_data="eslatma_ru")
            ]])
            await context.bot.send_message(uid, TEXTS['uz']['eslatma_uz'],
                                           reply_markup=kb, parse_mode="HTML")
            _db_execute("UPDATE users SET eslatma_given = TRUE WHERE user_id = %s", (uid,))
            sent += 1
        except Exception as e:
            failed += 1
            logger.warning(f"Eslatma yuborishda xato (uid={uid}): {e}")
        await asyncio.sleep(0.4)

    await update.message.reply_text(
        f"✅ Eslatma tugadi!\n📨 Yuborildi: {sent}\n⚠️ Yuborilmadi: {failed} (bloklagan yoki o'chirgan)"
    )


async def test_sorov_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: 7 kunlik test paketi haqida so'rov - premiumi yo'q HAMMAga.
    Har foydalanuvchiga FAQAT 1 marta. Sekin yuboradi."""
    if not is_admin(update.effective_user.id):
        return
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    rows = _db_execute(
        "SELECT user_id FROM users "
        "WHERE (sub_until IS NULL OR sub_until <= %s) "
        "AND COALESCE(test_sorov_given, FALSE) = FALSE",
        (now,), fetch='all'
    ) or []
    # Adminlar va faol premiumlarni chiqarib tashlaymiz
    rows = [r for r in rows if not is_admin(r[0]) and not sub_active(r[0])]
    if not rows:
        await update.message.reply_text("📭 So'rov yuboriladigan foydalanuvchi yo'q (hammasi olgan).")
        return
    await update.message.reply_text(f"📋 7 kunlik so'rov boshlandi: {len(rows)} ta foydalanuvchiga...\n(Sekin yuboriladi, kuting)")

    sent, failed = 0, 0
    for row in rows:
        uid = row[0]
        try:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton(TEXTS['uz']['test_sorov_btn'], callback_data="test_sorov_start")
            ]])
            await context.bot.send_message(uid, TEXTS['uz']['test_sorov_msg'],
                                           reply_markup=kb, parse_mode="HTML")
            _db_execute("UPDATE users SET test_sorov_given = TRUE WHERE user_id = %s", (uid,))
            sent += 1
        except Exception as e:
            failed += 1
            logger.warning(f"Test so'rov yuborishda xato (uid={uid}): {e}")
        await asyncio.sleep(0.4)

    await update.message.reply_text(
        f"✅ So'rov tugadi!\n📨 Yuborildi: {sent}\n⚠️ Yuborilmadi: {failed} (bloklagan yoki botni o'chirgan)"
    )


async def sorov_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: video tahlil qilgan hamma foydalanuvchiga so'rov yuboradi (Ha/Yo'q + sabab).
    Har foydalanuvchiga FAQAT 1 marta. Sekin yuboradi."""
    if not is_admin(update.effective_user.id):
        return
    rows = _db_execute(
        "SELECT DISTINCT u.user_id FROM users u "
        "JOIN analyses a ON a.user_id = u.user_id AND a.kind = 'video' "
        "WHERE COALESCE(u.sorov_given, FALSE) = FALSE",
        fetch='all'
    ) or []
    if not rows:
        await update.message.reply_text("📭 So'rov yuboriladigan foydalanuvchi yo'q (hammasi olgan).")
        return
    await update.message.reply_text(f"📊 So'rov boshlandi: {len(rows)} ta foydalanuvchiga...\n(Sekin yuboriladi, kuting)")

    # Foydalanuvchilar sonini yuzlikka yumaloqlab "2500+" ko'rinishida ko'rsatamiz
    total_users = (_db_execute("SELECT COUNT(*) FROM users", fetch='one') or [0])[0]
    rounded = (total_users // 100) * 100  # 2547 -> 2500
    if rounded < 100:
        rounded = total_users
    count_str = f"{rounded:,}"

    sent, failed = 0, 0
    for row in rows:
        uid = row[0]
        try:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton(TEXTS['uz']['sorov_start_btn'], callback_data="sorov_start")
            ]])
            await context.bot.send_message(uid, TEXTS['uz']['sorov_msg'].format(n=count_str), reply_markup=kb)
            _db_execute("UPDATE users SET sorov_given = TRUE WHERE user_id = %s", (uid,))
            sent += 1
        except Exception as e:
            failed += 1
            logger.warning(f"So'rov yuborishda xato (uid={uid}): {e}")
        await asyncio.sleep(0.4)

    await update.message.reply_text(
        f"✅ So'rov tugadi!\n📨 Yuborildi: {sent}\n⚠️ Yuborilmadi: {failed} (bloklagan yoki botni o'chirgan)"
    )


async def premium_xarajat_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: har obunachining tahlil soni + jami xarajati (foyda/zarar).
    Faqat shaxsiy chatda. Bugundan boshlab to'plangan ma'lumot."""
    if not is_admin(update.effective_user.id):
        return
    if update.effective_chat.type != "private":
        await update.message.reply_text("🔒 Bu buyruq faqat shaxsiy chatda ishlaydi.")
        return
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    # Faol obunachilar
    subs = _db_execute(
        "SELECT user_id, username, sub_until FROM users "
        "WHERE sub_until IS NOT NULL AND sub_until > %s ORDER BY sub_until DESC",
        (now,), fetch='all'
    ) or []
    if not subs:
        await update.message.reply_text("📭 Hozir faol obunachi yo'q.")
        return
    lines = ["💎 PREMIUM XARAJAT (saqlanган ma'lumot bo'yicha):\n"]
    jami_tahlil, jami_narx = 0, 0.0
    for uid, uname, sub_until in subs:
        row = _db_execute(
            "SELECT COUNT(*), COALESCE(SUM(narx),0) FROM analyses WHERE user_id = %s",
            (uid,), fetch='one'
        )
        cnt = row[0] if row else 0
        narx = row[1] if row and row[1] else 0
        jami_tahlil += cnt
        jami_narx += narx
        # To'lov (oxirgi approved)
        tolov_row = _db_execute(
            "SELECT COALESCE(SUM(amount),0) FROM payments WHERE user_id = %s AND status = 'approved'",
            (uid,), fetch='one'
        )
        tolov = tolov_row[0] if tolov_row and tolov_row[0] else 0
        belgi = "✅" if narx <= tolov else "❌ ZARAR"
        uname_str = f"@{uname}" if uname else f"ID:{uid}"
        lines.append(f"{uname_str} — {cnt} tahlil — {narx:,.0f} so'm (to'lov: {tolov:,} so'm) {belgi}")
    lines.append(f"\n📊 JAMI: {jami_tahlil} tahlil, {jami_narx:,.0f} so'm xarajat")
    text = "\n".join(lines)
    # Telegram 4096 belgi cheklovi
    if len(text) > 4000:
        text = text[:3990] + "\n…"
    await update.message.reply_text(text)


async def obunachilar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: faol obunachilar ro'yxati (kim, qachongacha)."""
    if not is_admin(update.effective_user.id):
        return
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    rows = _db_execute(
        "SELECT user_id, username, sub_until FROM users "
        "WHERE sub_until IS NOT NULL AND sub_until > %s "
        "ORDER BY sub_until DESC",
        (now,), fetch='all'
    ) or []
    if not rows:
        await update.message.reply_text("📭 Hozircha faol obunachi yo'q.")
        return
    lines = [f"👑 FAOL OBUNACHILAR ({len(rows)} ta):\n"]
    for i, (uid, uname, until) in enumerate(rows, 1):
        who = f"@{uname}" if uname else f"(username yo'q)"
        lines.append(f"{i}. {who} — ID: {uid}\n   ⏳ {until} gacha")
    text = "\n".join(lines)
    for i in range(0, len(text), 4000):
        await update.message.reply_text(text[i:i+4000])


async def test_taklif_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: balansi 0, obunasi yo'q hammaga 7 kunlik test paketi taklifini yuboradi.
    Har foydalanuvchiga FAQAT 1 marta. Sekin yuboradi."""
    if not is_admin(update.effective_user.id):
        return
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    rows = _db_execute(
        "SELECT user_id FROM users "
        "WHERE COALESCE(balance,0) <= 0 "
        "AND (sub_until IS NULL OR sub_until <= %s) "
        "AND COALESCE(test_taklif_given, FALSE) = FALSE",
        (now,), fetch='all'
    ) or []
    # Adminlar va faol premiumlarni chiqarib tashlaymiz
    rows = [r for r in rows if not is_admin(r[0]) and not sub_active(r[0])]
    if not rows:
        await update.message.reply_text("📭 Test taklifi yuboriladigan foydalanuvchi yo'q.")
        return
    await update.message.reply_text(f"⚡ Test taklifi boshlandi: {len(rows)} ta foydalanuvchiga...\n(Sekin yuboriladi, kuting)")

    sent, failed = 0, 0
    for row in rows:
        uid = row[0]
        try:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton(TEXTS['uz']['test_taklif_btn'], callback_data="buy_test")
            ]])
            await context.bot.send_message(uid, TEXTS['uz']['test_taklif_msg'],
                                           reply_markup=kb, parse_mode="HTML")
            _db_execute("UPDATE users SET test_taklif_given = TRUE WHERE user_id = %s", (uid,))
            sent += 1
        except Exception as e:
            failed += 1
            logger.warning(f"Test taklifini yuborishda xato (uid={uid}): {e}")
        await asyncio.sleep(0.4)

    await update.message.reply_text(
        f"✅ Test taklifi tugadi!\n📨 Yuborildi: {sent}\n⚠️ Yuborilmadi: {failed} (bloklagan yoki botni o'chirgan)"
    )


async def obuna_taklif_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: +2 aksiyani ham ishlatib bo'lganlarga (balansi 0, obunasi yo'q) obuna taklifini yuboradi.
    Har foydalanuvchiga FAQAT 1 marta. Sekin yuboradi."""
    if not is_admin(update.effective_user.id):
        return
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    # Nishon: +2 aksiyani olgan, balansi 0, obunasi yo'q, va bu taklifni hali olmagan
    rows = _db_execute(
        "SELECT user_id FROM users "
        "WHERE COALESCE(aksiya_given, FALSE) = TRUE "
        "AND COALESCE(balance,0) <= 0 "
        "AND (sub_until IS NULL OR sub_until <= %s) "
        "AND COALESCE(obuna_taklif_given, FALSE) = FALSE",
        (now,), fetch='all'
    ) or []
    if not rows:
        await update.message.reply_text("📭 Obuna taklifi yuboriladigan foydalanuvchi yo'q.")
        return
    await update.message.reply_text(f"💎 Obuna taklifi boshlandi: {len(rows)} ta foydalanuvchiga...\n(Sekin yuboriladi, kuting)")

    sent, failed = 0, 0
    for row in rows:
        uid = row[0]
        try:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton(TEXTS['uz']['obuna_taklif_btn'], callback_data="buy_sub")
            ]])
            await context.bot.send_message(uid, TEXTS['uz']['obuna_taklif_msg'], reply_markup=kb)
            _db_execute("UPDATE users SET obuna_taklif_given = TRUE WHERE user_id = %s", (uid,))
            sent += 1
        except Exception as e:
            failed += 1
            logger.warning(f"Obuna taklifini yuborishda xato (uid={uid}): {e}")
        await asyncio.sleep(0.4)

    await update.message.reply_text(
        f"✅ Obuna taklifi tugadi!\n📨 Yuborildi: {sent}\n⚠️ Yuborilmadi: {failed} (bloklagan yoki botni o'chirgan)"
    )


async def sotuv_matn_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: sotuv (chegirma) xabari matnini o'zgartiradi.
    Foydalanish: /sotuv_matn keyin yangi matn (bitta xabarda)."""
    if not is_admin(update.effective_user.id):
        return
    # Buyruqdan keyingi matnni olamiz
    full = update.message.text or ""
    parts = full.split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        await update.message.reply_text(
            "✍️ Yangi sotuv matnini buyruq bilan birga yuboring.\n\n"
            "Masalan:\n/sotuv_matn Bizning yangi taklif...\n\n"
            "💡 Qalin uchun <b>matn</b>, chizilgan uchun <s>matn</s> ishlating.\n"
            "Joriy matnni ko'rish: /sotuv_korish"
        )
        return
    yangi = parts[1].strip()
    set_setting("sotuv_matn", yangi)
    await update.message.reply_text(
        "✅ Sotuv matni yangilandi!\n\nKo'rish: /sotuv_korish\nYuborish: /chegirma"
    )


async def sotuv_korish_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: joriy sotuv matnini ko'rsatadi (qanday ko'rinishini)."""
    if not is_admin(update.effective_user.id):
        return
    msg = get_sotuv_msg()
    try:
        await update.message.reply_text("👁 Joriy sotuv matni:\n\n" + msg, parse_mode="HTML")
    except Exception:
        await update.message.reply_text("👁 Joriy sotuv matni (formatsiz):\n\n" + msg)


async def sotuv_matn_tikla_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: sotuv matnini koddagi asl (default) holatiga qaytaradi."""
    if not is_admin(update.effective_user.id):
        return
    set_setting("sotuv_matn", "")
    await update.message.reply_text("♻️ Sotuv matni asl holatiga qaytarildi.")


async def premium_fikr_reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: premium_fikr_given ni HAMMADA FALSE qiladi (qaytadan so'rash uchun)."""
    if not is_admin(update.effective_user.id):
        return
    _db_execute("UPDATE users SET premium_fikr_given = FALSE")
    await update.message.reply_text(
        "♻️ Tayyor! Hammada qayta tiklandi. Endi /premium_fikr bosing.")


async def premium_fikr_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: AKTIV PREMIUM (obunasi hali tugamagan) foydalanuvchilardan fikr so'raydi.
    Har bir premium'ga FAQAT BIR MARTA boradi (premium_fikr_given orqali)."""
    if not is_admin(update.effective_user.id):
        return
    # 1) Hamma userni olamiz (faqat user_id - lang DB'da yo'q, ishlatmaymiz)
    rows = _db_execute("SELECT user_id FROM users", fetch='all') or []
    # 2) Faqat AKTIV PREMIUM + hali fikr so'ralmaganlarni ajratamiz
    targets = []
    for r in rows:
        uid = r[0]
        if is_admin(uid):
            continue
        if not sub_active(uid):   # obuna faol emas -> tashlab ketamiz
            continue
        # fikr allaqachon so'ralganmi? (NULL yoki FALSE bo'lsa - so'ralmagan)
        gr = _db_execute("SELECT COALESCE(premium_fikr_given, FALSE) FROM users WHERE user_id = %s",
                         (uid,), fetch='one')
        if gr and gr[0]:
            continue   # allaqachon so'ralgan
        targets.append(uid)
    if not targets:
        await update.message.reply_text(
            "📭 Yangi premium foydalanuvchi yo'q.\n"
            "(Hammasidan so'ralgan bo'lishi mumkin — qayta so'rash: /premium_fikr_reset)")
        return
    # 3) Yuboramiz (har biriga bir marta) va belgilab boramiz
    await update.message.reply_text(
        f"💬 Premium fikr so'rovi: {len(targets)} ta obunachiga yuborilmoqda...\n"
        f"(Sekin yuboriladi, kuting)")
    sent, failed = 0, 0
    for uid in targets:
        try:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton(TEXTS['uz']['premium_fikr_btn'], callback_data="premium_fikr")
            ]])
            await context.bot.send_message(uid, TEXTS['uz']['premium_fikr_msg'], reply_markup=kb)
            # Yuborildi -> belgilaymiz (qayta bormasin)
            _db_execute("UPDATE users SET premium_fikr_given = TRUE WHERE user_id = %s", (uid,))
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.4)
    for aid in ADMIN_IDS:
        try:
            await context.bot.send_message(aid, f"✅ Premium fikr: {sent} yuborildi, {failed} xato.")
        except Exception:
            pass


async def _sotuv_broadcast(update, context, target_uids, flag_col, msg_key, with_buy_btn=True):
    """Sotuv voronkasi yordamchisi: target_uids ga msg yuboradi (flag bilan, takrorsiz).
    flag_col - DB ustun (masalan 'sotuv1_given'). Yuborilgach TRUE bo'ladi."""
    if not target_uids:
        await update.message.reply_text("📭 Mos foydalanuvchi yo'q (yoki hammasiga yuborilgan).")
        return
    await update.message.reply_text(
        f"📤 Yuborilmoqda: {len(target_uids)} ta\n(Har biriga FAQAT bir marta. Kuting...)")
    sent, failed = 0, 0
    for uid in target_uids:
        try:
            if with_buy_btn:
                kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton(TEXTS['uz']['obuna_taklif_btn'], callback_data="buy_sub")
                ]])
            else:
                kb = None
            await context.bot.send_message(uid, TEXTS['uz'][msg_key], reply_markup=kb, parse_mode="HTML")
            _db_execute(f"UPDATE users SET {flag_col} = TRUE WHERE user_id = %s", (uid,))
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.4)
    for aid in ADMIN_IDS:
        try:
            await context.bot.send_message(aid, f"✅ Yuborildi: {sent}, xato: {failed}.")
        except Exception:
            pass


def _kup_tahlil_uids(min_count, flag_col):
    """min_count+ video tahlil qilgan, premium EMAS, flag hali FALSE bo'lganlar."""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    rows = _db_execute(
        f"SELECT u.user_id FROM users u "
        f"JOIN (SELECT user_id, COUNT(*) c FROM analyses WHERE kind='video' "
        f"GROUP BY user_id HAVING COUNT(*) >= %s) a ON a.user_id = u.user_id "
        f"WHERE (u.sub_until IS NULL OR u.sub_until <= %s) "
        f"AND (u.{flag_col} IS NULL OR u.{flag_col} = FALSE)",
        (min_count, now_str), fetch='all') or []
    return [r[0] for r in rows if not is_admin(r[0])]


def _aniq_tahlil_uids(exact_count, flag_col):
    """AYNAN exact_count marta tahlil qilgan, premium emas, flag FALSE."""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    rows = _db_execute(
        f"SELECT u.user_id FROM users u "
        f"JOIN (SELECT user_id, COUNT(*) c FROM analyses WHERE kind='video' "
        f"GROUP BY user_id HAVING COUNT(*) = %s) a ON a.user_id = u.user_id "
        f"WHERE (u.sub_until IS NULL OR u.sub_until <= %s) "
        f"AND (u.{flag_col} IS NULL OR u.{flag_col} = FALSE)",
        (exact_count, now_str), fetch='all') or []
    return [r[0] for r in rows if not is_admin(r[0])]


def _ishlatmagan_uids(flag_col):
    """Ro'yxatdan o'tib, BITTA HAM video tahlil qilmagan, flag FALSE."""
    rows = _db_execute(
        f"SELECT u.user_id FROM users u "
        f"LEFT JOIN (SELECT DISTINCT user_id FROM analyses WHERE kind='video') a "
        f"ON a.user_id = u.user_id WHERE a.user_id IS NULL "
        f"AND (u.{flag_col} IS NULL OR u.{flag_col} = FALSE)",
        fetch='all') or []
    return [r[0] for r in rows if not is_admin(r[0])]


async def bepul_royxat_command(update, context):
    """Admin: kim sotuv voronkasidan BEPUL PREMIUM olganini ko'rsatadi."""
    if not is_admin(update.effective_user.id):
        return
    rows = _db_execute(
        "SELECT user_id, username, sotuv_bepul_olindi FROM users "
        "WHERE sotuv_bepul_olindi IS NOT NULL ORDER BY sotuv_bepul_olindi DESC",
        fetch='all') or []
    if not rows:
        await update.message.reply_text("📭 Hali hech kim bepul premium olmagan.")
        return
    txt = f"🎁 <b>BEPUL PREMIUM OLGANLAR</b> ({len(rows)} ta)\n\n"
    for r in rows[:60]:
        uid, uname, manba = r[0], r[1], r[2]
        who = f"@{uname}" if uname else f"ID {uid}"
        txt += f"• {who} — {manba}\n"
    if len(rows) > 60:
        txt += f"\n... va yana {len(rows)-60} ta"
    await update.message.reply_text(txt, parse_mode="HTML")


async def sotuv_reset_command(update, context):
    """Admin: sotuv flaglarini tiklaydi. /sotuv_reset 1  (yoki 1b, 3, 4, 5)
    Tiklangач o'sha buyruq qayta yuborilganda HAMMAGA qayta boradi."""
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text(
            "Ishlatish: /sotuv_reset <raqam>\n"
            "Masalan: /sotuv_reset 1  (sotuv1 ni tiklaydi)\n"
            "Variantlar: 1, 1b, 3, 4, 5")
        return
    key = context.args[0].strip().lower()
    col_map = {"1": "sotuv1_given", "1b": "sotuv1b_given", "3": "sotuv3_given",
               "4": "sotuv4_given", "5": "sotuv5_given"}
    col = col_map.get(key)
    if not col:
        await update.message.reply_text("❌ Noto'g'ri. Variantlar: 1, 1b, 3, 4, 5")
        return
    _db_execute(f"UPDATE users SET {col} = FALSE")
    await update.message.reply_text(
        f"♻️ sotuv{key} tiklandi! Endi /sotuv{key} bosing — HAMMAGA qayta boradi.")


async def sotuv1_command(update, context):
    """Qadam 1 — 5+ ishlatgan, to'lamagan: SAVOL + Fikr bildirish tugmasi."""
    if not is_admin(update.effective_user.id):
        return
    uids = _kup_tahlil_uids(5, "sotuv1_given")
    if not uids:
        await update.message.reply_text("📭 Mos foydalanuvchi yo'q.")
        return
    await update.message.reply_text(
        f"📤 Yuborilmoqda: {len(uids)} ta\n(Har biriga FAQAT bir marta. Kuting...)")
    sent, failed = 0, 0
    for uid in uids:
        try:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("💬 Fikr bildirish (va bepul tahlil oling!)",
                                     callback_data="sotuv1_fikr")
            ]])
            await context.bot.send_message(
                uid, TEXTS['uz']['sotuv1_msg'], reply_markup=kb, parse_mode="HTML")
            _db_execute("UPDATE users SET sotuv1_given = TRUE WHERE user_id = %s", (uid,))
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.4)
    for aid in ADMIN_IDS:
        try:
            await context.bot.send_message(aid, f"✅ sotuv1: {sent} yuborildi, {failed} xato.")
        except Exception:
            pass


async def sotuv1b_command(update, context):
    """Qadam 1b — 5+ ishlatgan, to'lamagan: TAKLIF (19,900 chegirma)."""
    if not is_admin(update.effective_user.id):
        return
    # Chegirma 19,900 yoqamiz (24 soat)
    now = datetime.now()
    until_dt = now + timedelta(hours=24)
    set_setting("chegirma_until", until_dt.strftime("%Y-%m-%d %H:%M:%S"))
    uids = _kup_tahlil_uids(5, "sotuv1b_given")
    if not uids:
        await update.message.reply_text("📭 Mos foydalanuvchi yo'q.")
        return
    await update.message.reply_text(
        f"📤 Yuborilmoqda: {len(uids)} ta (chegirma 24 soat). Kuting...")
    sent, failed = 0, 0
    for uid in uids:
        try:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔥 19,900 ga Premium olish", callback_data="buy_sub_19")
            ]])
            await context.bot.send_message(uid, TEXTS['uz']['sotuv1b_msg'], reply_markup=kb, parse_mode="HTML")
            _db_execute("UPDATE users SET sotuv1b_given = TRUE WHERE user_id = %s", (uid,))
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.4)
    for aid in ADMIN_IDS:
        try:
            await context.bot.send_message(aid, f"✅ sotuv1b: {sent} yuborildi, {failed} xato.")
        except Exception:
            pass


async def sotuv3_command(update, context):
    """Qadam 3 — 2-4 tahlil (qiziqqan, 5+ EMAS): premium kuchini bepul sinash tugmasi."""
    if not is_admin(update.effective_user.id):
        return
    # Faqat 2-4 tahlil (5+ larni CHIQARIB TASHLAB - ular sotuv1 guruhida)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    rows = _db_execute(
        "SELECT u.user_id FROM users u "
        "JOIN (SELECT user_id FROM analyses WHERE kind='video' "
        "GROUP BY user_id HAVING COUNT(*) >= 2 AND COUNT(*) < 5) a ON a.user_id = u.user_id "
        "WHERE (u.sub_until IS NULL OR u.sub_until <= %s) "
        "AND (u.sotuv3_given IS NULL OR u.sotuv3_given = FALSE)",
        (now_str,), fetch='all') or []
    uids = [r[0] for r in rows if not is_admin(r[0])]
    if not uids:
        await update.message.reply_text("📭 Mos foydalanuvchi yo'q.")
        return
    await update.message.reply_text(
        f"📤 Yuborilmoqda: {len(uids)} ta\n(Har biriga FAQAT bir marta. Kuting...)")
    sent, failed = 0, 0
    for uid in uids:
        try:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔥 Premium kuchini sinab ko'rish", callback_data="sotuv3_bepul")
            ]])
            await context.bot.send_message(
                uid, TEXTS['uz']['sotuv3_msg'], reply_markup=kb, parse_mode="HTML")
            _db_execute("UPDATE users SET sotuv3_given = TRUE WHERE user_id = %s", (uid,))
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.4)
    for aid in ADMIN_IDS:
        try:
            await context.bot.send_message(aid, f"✅ sotuv3: {sent} yuborildi, {failed} xato.")
        except Exception:
            pass


async def sotuv4_command(update, context):
    """Qadam 4 — aynan 1 marta ishlatgan (qaytmagan): bepul premium tugma."""
    if not is_admin(update.effective_user.id):
        return
    uids = _aniq_tahlil_uids(1, "sotuv4_given")
    if not uids:
        await update.message.reply_text("📭 Mos foydalanuvchi yo'q.")
        return
    await update.message.reply_text(
        f"📤 Yuborilmoqda: {len(uids)} ta\n(Har biriga FAQAT bir marta. Kuting...)")
    sent, failed = 0, 0
    for uid in uids:
        try:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("🎁 Bepul premium olish", callback_data="sotuv4_bepul")
            ]])
            await context.bot.send_message(
                uid, TEXTS['uz']['sotuv4_msg'], reply_markup=kb, parse_mode="HTML")
            _db_execute("UPDATE users SET sotuv4_given = TRUE WHERE user_id = %s", (uid,))
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.4)
    for aid in ADMIN_IDS:
        try:
            await context.bot.send_message(aid, f"✅ sotuv4: {sent} yuborildi, {failed} xato.")
        except Exception:
            pass


async def sotuv5_command(update, context):
    """Qadam 5 — ishlatmagan (sovuq): video tashlash tugmasi."""
    if not is_admin(update.effective_user.id):
        return
    uids = _ishlatmagan_uids("sotuv5_given")
    if not uids:
        await update.message.reply_text("📭 Mos foydalanuvchi yo'q.")
        return
    await update.message.reply_text(
        f"📤 Yuborilmoqda: {len(uids)} ta\n(Har biriga FAQAT bir marta. Kuting...)")
    sent, failed = 0, 0
    for uid in uids:
        try:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("🎬 Videoni tahlil qilish", callback_data="aksiya_video")
            ]])
            await context.bot.send_message(
                uid, TEXTS['uz']['sotuv5_msg'], reply_markup=kb, parse_mode="HTML")
            _db_execute("UPDATE users SET sotuv5_given = TRUE WHERE user_id = %s", (uid,))
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.4)
    for aid in ADMIN_IDS:
        try:
            await context.bot.send_message(aid, f"✅ sotuv5: {sent} yuborildi, {failed} xato.")
        except Exception:
            pass


async def test_tugaganlar_command(update, context):
    """Admin: 7 kunlik (test_7day) olib, obunasi TUGAGAN odamlarga
    20% chegirma bilan 1 oylik taklif. /test_tugaganlar (ko'rish) yoki /test_tugaganlar YUBOR"""
    if not is_admin(update.effective_user.id):
        return
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    rows = _db_execute(
        "SELECT DISTINCT u.user_id, u.username FROM users u "
        "JOIN payments p ON p.user_id = u.user_id AND p.status='approved' AND p.package='test_7day' "
        "WHERE (u.sub_until IS NULL OR u.sub_until <= %s) "
        "AND (u.test_renewal_given IS NULL OR u.test_renewal_given = FALSE)",
        (now_str,), fetch='all') or []
    uids = [(r[0], r[1]) for r in rows if not is_admin(r[0])]
    if not uids:
        await update.message.reply_text("📭 7 kunligi tugagan (taklif kutayotgan) odam yo'q.")
        return
    yubor = context.args and context.args[0].upper() == "YUBOR"
    if not yubor:
        txt = f"📋 <b>7 KUNLIGI TUGAGAN</b> — {len(uids)} ta\n(20% chegirma taklifi kutmoqda)\n\n"
        for i, (uid, uname) in enumerate(uids[:50], 1):
            who = f"@{uname}" if uname else f"ID {uid}"
            txt += f"{i}. {who} (ID {uid})\n"
        txt += "\n📤 Yuborish: /test_tugaganlar YUBOR"
        await update.message.reply_text(txt, parse_mode="HTML")
        return
    await update.message.reply_text(f"📤 {len(uids)} ta odamga 20% chegirma taklifi yuborilmoqda...")
    sent, failed = 0, 0
    for uid, uname in uids:
        try:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("💎 1 oylik olish (20% chegirma)", callback_data="buy_sub_20")
            ]])
            await context.bot.send_message(
                uid,
                "⏳ <b>7 kunlik Premiumingiz tugadi!</b>\n\n"
                "Yoqdimi? 😊 Kontentingiz o'sishni boshladi — to'xtatmang!\n\n"
                "🎁 Faqat sizga: 1 oylik Premium <s>29,900</s> → <b>23,900 so'm</b> (20% chegirma)\n\n"
                "⏰ Chegirma faqat 24 soat!\n\n👇 Davom eting!",
                reply_markup=kb, parse_mode="HTML")
            _db_execute("UPDATE users SET test_renewal_given = TRUE, renewal_chegirma_sana = %s WHERE user_id = %s",
                        (datetime.now().strftime("%Y-%m-%d %H:%M"), uid))
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.4)
    await update.message.reply_text(f"✅ Yuborildi: {sent}, xato: {failed}.")


async def renewal_royxat_command(update, context):
    """Admin: kimlarga renewal/chegirma taklifi yuborilganini ko'rsatadi."""
    if not is_admin(update.effective_user.id):
        return
    # 1) Obuna tugashiga 1 kun eslatmasi olganlar (renewal_eslatma_given)
    r1 = _db_execute(
        "SELECT user_id, username, sub_until FROM users WHERE renewal_eslatma_given = TRUE "
        "ORDER BY sub_until DESC", fetch='all') or []
    # 2) 7 kunlik tugab, 20% (23,900) taklif olganlar (test_renewal_given + sana)
    r2 = _db_execute(
        "SELECT user_id, username, renewal_chegirma_sana FROM users WHERE test_renewal_given = TRUE "
        "ORDER BY renewal_chegirma_sana DESC", fetch='all') or []
    txt = "🔄 <b>RENEWAL / CHEGIRMA TAKLIFLARI</b>\n━━━━━━━━━━━━━\n\n"
    txt += f"<b>⏳ 'Ertaga tugaydi' eslatmasi olganlar:</b> {len(r1)} ta\n"
    for i, r in enumerate(r1[:30], 1):
        uid, uname, su = r[0], r[1], r[2]
        who = f"@{uname}" if uname else f"ID {uid}"
        txt += f"{i}. {who} (ID {uid}) — tugash: {su}\n"
    if len(r1) > 30:
        txt += f"... yana {len(r1)-30} ta\n"
    txt += f"\n<b>🎁 20% (23,900) taklif olganlar:</b> {len(r2)} ta\n"
    for i, r in enumerate(r2[:30], 1):
        uid, uname, sana = r[0], r[1], r[2]
        who = f"@{uname}" if uname else f"ID {uid}"
        # 24 soat holati
        holat = ""
        d = _parse_dt(sana) if sana else None
        if d:
            otdi = (datetime.now() - d).total_seconds()
            holat = " (chegirma FAOL)" if otdi <= 24*3600 else " (chegirma tugagan)"
        txt += f"{i}. {who} (ID {uid}) — yuborilgan: {sana or '—'}{holat}\n"
    if len(r2) > 30:
        txt += f"... yana {len(r2)-30} ta\n"
    if not r1 and not r2:
        txt += "\n📭 Hali hech kimga renewal taklifi yuborilmagan.\n"
        txt += "(Avtomatik: har kuni 11:00 'ertaga tugaydi'ganlarga boradi.\n"
        txt += "Qo'lda: /renewal_hozir yoki /test_tugaganlar YUBOR)"
    # Bo'laklab yuborish (uzun bo'lsa)
    if len(txt) <= 4000:
        await update.message.reply_text(txt, parse_mode="HTML")
    else:
        bloklar = txt.split("\n")
        qism = ""
        for blok in bloklar:
            if len(qism) + len(blok) + 1 > 3500:
                await update.message.reply_text(qism, parse_mode="HTML")
                qism = ""
            qism += blok + "\n"
        if qism.strip():
            await update.message.reply_text(qism, parse_mode="HTML")


async def marafon_on_command(update, context):
    """Admin: marafonni YOQADI (yangi kirganlar avtomatik marafonga tushadi)."""
    if not is_admin(update.effective_user.id):
        return
    set_setting("marafon_aktiv", "on")
    await update.message.reply_text(
        "✅ MARAFON YOQILDI!\n\n"
        "Endi yangi kirgan har bir foydalanuvchi 5 kunlik marafonga tushadi:\n"
        "• Har kuni 11:00 - keyingi kun xabari + bepul tahlil\n"
        "• 23:00 - ishlatilmagan kunlik bepul kuyadi\n"
        "• 5-kun - PREMIUM sovg'a + 7 kunlik taklif\n\n"
        "O'chirish: /marafon_off")


async def marafon_off_command(update, context):
    """Admin: marafonni O'CHIRADI (yangi kirganlar marafonsiz)."""
    if not is_admin(update.effective_user.id):
        return
    set_setting("marafon_aktiv", "off")
    await update.message.reply_text("🛑 Marafon o'chirildi. Yangi kirganlar marafonsiz (oddiy).")


async def avto_on_command(update, context):
    """Admin: avtomatik sotuv + 3 mahal hisobotni YOQADI."""
    if not is_admin(update.effective_user.id):
        return
    set_setting("avto_sotuv_aktiv", "on")
    set_setting("hisobot_aktiv", "on")
    await update.message.reply_text(
        "✅ AVTOMATLASHTIRISH YOQILDI!\n\n"
        "🤖 Avto-sotuv (har kuni 12:00):\n"
        "• Yangi 5+ larga → sotuv1\n"
        "• Yangi 2-4 larga → sotuv3\n"
        "• Yangi 1 martalik → sotuv4\n"
        "• Spam himoyasi: kuniga max 150 ta\n\n"
        "📊 Hisobot (kuniga 3 marta):\n"
        "• 09:00 — kechagi yakun\n"
        "• 14:00 — bugun hozircha\n"
        "• 21:00 — bugun yakuni\n\n"
        "O'chirish: /avto_off")


async def avto_off_command(update, context):
    """Admin: avtomatik sotuv + hisobotni O'CHIRADI."""
    if not is_admin(update.effective_user.id):
        return
    set_setting("avto_sotuv_aktiv", "off")
    set_setting("hisobot_aktiv", "off")
    await update.message.reply_text("🛑 Avtomatlashtirish o'chirildi (sotuv + hisobot qo'lda).")


async def avto_test_command(update, context):
    """Admin: hisobotni HOZIR ko'rsatadi (sinash)."""
    if not is_admin(update.effective_user.id):
        return
    for davr in ["kecha", "bugun_yarim", "bugun_yakun"]:
        matn = await _hisobot_matni(davr)
        await update.message.reply_text(matn, parse_mode="HTML")
        await asyncio.sleep(0.3)
    await update.message.reply_text(
        "☝️ 3 mahal hisobot namunasi.\n\n"
        "Avto-sotuvni sinash uchun: /avto_sotuv_hozir")


async def avto_sotuv_hozir_command(update, context):
    """Admin: avto-sotuvni HOZIR ishga tushiradi (sinash)."""
    if not is_admin(update.effective_user.id):
        return
    if get_setting("avto_sotuv_aktiv", "off") != "on":
        await update.message.reply_text("⚠️ Avval /avto_on bosing (avto-sotuv o'chiq).")
        return
    await update.message.reply_text("🤖 Avto-sotuv hozir ishga tushmoqda...")
    await avto_sotuv(context)
    await update.message.reply_text("✅ Tayyor!")


async def chegirma_test_command(update, context):
    """Admin: chegirma (24 soat) sinash.
    /chegirma_test yoq - chegirmani YOQADI (24 soat, 19,900 ko'rasiz)
    /chegirma_test tugat - chegirmani TUGATADI (29,900 ko'rasiz, 24 soat o'tgandek)
    /chegirma_test holat - hozirgi holatni ko'rsatadi"""
    if not is_admin(update.effective_user.id):
        return
    arg = (context.args[0] if context.args else "holat").lower()
    if arg == "yoq":
        set_setting("chegirma_until", (datetime.now() + timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S"))
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔥 19,900 ga olish (sinov)", callback_data="buy_sub_19")]])
        await update.message.reply_text(
            "✅ Chegirma YOQILDI (24 soat).\n\n"
            "Endi pastdagi tugmani bosing — 19,900 chiqishi kerak 👇", reply_markup=kb)
    elif arg == "tugat":
        # Chegirmani o'tgan qilib qo'yamiz (24 soatdan oldin)
        set_setting("chegirma_until", (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"))
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔥 19,900 ga olish (sinov)", callback_data="buy_sub_19")]])
        await update.message.reply_text(
            "⏰ Chegirma TUGATILDI (24 soat o'tgandek).\n\n"
            "Endi pastdagi tugmani bosing — '29,900, chegirma tugadi' chiqishi kerak 👇", reply_markup=kb)
    else:
        faol = discount_active()
        until = get_setting("chegirma_until", "yo'q")
        await update.message.reply_text(
            f"📊 Chegirma holati:\n"
            f"• Faol: {'✅ HA (19,900)' if faol else '❌ YO`Q (29,900)'}\n"
            f"• Tugash vaqti: {until}\n\n"
            f"Sinash:\n/chegirma_test yoq — yoqish\n/chegirma_test tugat — tugatish")


async def marafon_eski_tiq_command(update, context):
    """Admin: 1-2 tahlil qilgan ESKI userlarni marafonga tiqadi (o'rtadan).
    1 tahlil -> 2-kundan, 2 tahlil -> 3-kundan. Balans 0 (premium olmaganlar).
    /marafon_eski_tiq (test - hisoblab ko'rsatadi) yoki /marafon_eski_tiq YUBOR (haqiqiy)."""
    if not is_admin(update.effective_user.id):
        return
    arg = (context.args[0] if context.args else "").upper()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    # 1-2 video tahlil qilgan, premium EMAS, marafonda EMAS, obuna EMAS
    rows = _db_execute(
        "SELECT u.user_id, a.c FROM users u "
        "JOIN (SELECT user_id, COUNT(*) c FROM analyses WHERE kind='video' "
        "GROUP BY user_id HAVING COUNT(*) IN (1,2)) a ON a.user_id = u.user_id "
        "WHERE (u.marafon_kun IS NULL OR u.marafon_kun = 0) "
        "AND (u.sub_until IS NULL OR u.sub_until <= %s) "
        "AND COALESCE(u.premium_balance,0) = 0",
        (now_str,), fetch='all') or []
    rows = [r for r in rows if not is_admin(r[0])]
    if arg != "YUBOR":
        bir = sum(1 for r in rows if r[1] == 1)
        ikki = sum(1 for r in rows if r[1] == 2)
        await update.message.reply_text(
            f"🧪 <b>MARAFON ESKI BAZA (test)</b>\n━━━━━━━━━━━\n\n"
            f"Jami mos: {len(rows)} ta\n"
            f"• 1 tahlil qilgan: {bir} ta → 2-kundan boshlanadi\n"
            f"• 2 tahlil qilgan: {ikki} ta → 3-kundan boshlanadi\n\n"
            f"Ular marafonga tiqiladi (o'rtadan), balansi 0 qilinadi.\n"
            f"Avto-sotuv ularga bormaydi (marafonda).\n\n"
            f"⚠️ Haqiqiy yuborish: /marafon_eski_tiq YUBOR\n"
            f"(Diqqat: {len(rows)} ta userga xabar boradi!)",
            parse_mode="HTML")
        return
    await update.message.reply_text(f"📤 {len(rows)} ta eski user marafonga tiqilmoqda...")
    bugun = datetime.now().strftime("%Y-%m-%d")
    tiqildi = 0
    for r in rows:
        uid, tahlil = r[0], r[1]
        if is_blocked(uid):
            continue
        # 1 tahlil -> kun=2, bajarilgan=1 | 2 tahlil -> kun=3, bajarilgan=2
        bosh_kun = tahlil + 1
        try:
            # Balansni 0 qilamiz + marafonga tiqamiz
            # bajarilgan = bosh_kun (tiqilgan kun ham sanaladi, 5-kunda 5/5 bo'lishi uchun)
            _db_execute(
                "UPDATE users SET balance = 0, marafon_kun = %s, marafon_start = %s, "
                "marafon_kunlik_sana = %s, marafon_tugadi = FALSE, "
                "marafon_bajarilgan = %s, marafon_oxirgi_bajarilgan = %s WHERE user_id = %s",
                (bosh_kun, bugun, bugun, bosh_kun, bugun, uid))
            # Bugungi bepul beramiz (o'sha kun uchun)
            add_balance(uid, 1)
            # Xabar (o'sha kun matni)
            matn, tugma = marafon_kun_matni(bosh_kun)
            kb = marafon_kb(bosh_kun, tugma)
            # Eski userga moslashtirilgan kirish
            kirish = (f"🎉 <b>Xush kelibsiz qaytganingiz bilan!</b>\n\n"
                      f"Siz allaqachon {tahlil} ta tahlil qilgansiz — zo'r boshlangan! 💪\n"
                      f"Sizni to'g'ridan-to'g'ri <b>{bosh_kun}-kunga</b> qo'shdik. "
                      f"Marafonni tugatib, PREMIUM sovg'ani oling! 🎁\n\n")
            if bosh_kun == 2:
                try:
                    await context.bot.send_video(uid, VIDEO_FILE_ID)
                except Exception:
                    pass
            await context.bot.send_message(uid, kirish + matn, reply_markup=kb, parse_mode="HTML")
            tiqildi += 1
            if tiqildi % 100 == 0:
                await asyncio.sleep(1)
            else:
                await asyncio.sleep(0.1)
        except Exception:
            pass
    await update.message.reply_text(f"✅ Tayyor! {tiqildi} ta eski user marafonga tiqildi.")


async def yangilik_xabar_command(update, context):
    """Admin: eski userlarga 'yangilik bor' xabari + menyu yangilash tugmasi.
    Ishlatish: /yangilik_xabar (test - o'zingizga) yoki /yangilik_xabar YUBOR (hammaga)."""
    if not is_admin(update.effective_user.id):
        return
    arg = (context.args[0] if context.args else "").upper()
    matn = (
        "🎉 <b>InstaDoctor'da YANGILIKLAR!</b>\n\n"
        "Biz botni yanada kuchli qildik:\n"
        "🏆 <b>Statusim</b> — kreator darajangizni kuzating va rivojlaning\n"
        "📊 Yangilangan tahlil va ko'proq imkoniyatlar!\n"
        "💎 Premium funksiyalar yanada kuchaydi\n\n"
        "👇 Yangi menyuni ochish uchun tugmani bosing:")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Yangi menyuni ochish", callback_data="menyu_yangila")]])
    if arg != "YUBOR":
        # Test - faqat adminga
        await update.message.reply_text(matn, reply_markup=kb, parse_mode="HTML")
        await update.message.reply_text(
            "☝️ Test namunasi. Hammaga yuborish uchun: /yangilik_xabar YUBOR\n"
            "⚠️ Diqqat: bu barcha foydalanuvchilarga xabar yuboradi!")
        return
    # Hammaga yuborish
    rows = _db_execute("SELECT user_id FROM users", fetch='all') or []
    await update.message.reply_text(f"📤 {len(rows)} ta userga yuborilmoqda... (biroz vaqt oladi)")
    yuborildi = 0
    for r in rows:
        uid = r[0]
        if is_blocked(uid):
            continue
        try:
            await context.bot.send_message(uid, matn, reply_markup=kb, parse_mode="HTML")
            yuborildi += 1
            if yuborildi % 100 == 0:
                await asyncio.sleep(1)
            else:
                await asyncio.sleep(0.05)
        except Exception:
            pass
    await update.message.reply_text(f"✅ Tayyor! {yuborildi} ta userga yuborildi.")


async def profile_command(update, context):
    """Foydalanuvchi: kreator profili - status, progress, tahlil soni, premium."""
    uid = update.effective_user.id
    row = _db_execute("SELECT COUNT(*) FROM analyses WHERE user_id = %s AND kind='video'", (uid,), fetch='one')
    tahlil = (row[0] if row and row[0] else 0)
    emoji, nom, kerak, keyingi = kreator_status(tahlil)
    bar, foiz = kreator_progress_bar(tahlil)
    premium = is_admin(uid) or sub_active(uid)

    txt = "👤 <b>KREATOR PROFILINGIZ</b>\n━━━━━━━━━━━\n\n"
    txt += f"🎯 Status: {emoji} <b>{nom}</b>\n"
    txt += f"📊 Progress: [{bar}] {foiz}%\n"
    txt += f"🎬 Jami tahlil: {tahlil} ta\n\n"
    if keyingi and kerak > 0:
        txt += f"🔥 Keyingi <b>\"{keyingi}\"</b> darajasiga yana {kerak} ta tahlil kerak!\n\n"
    elif not keyingi:
        txt += "🏆 Siz eng yuqori darajaga yetdingiz — zo'rsiz! 👑\n\n"
    if premium:
        txt += "💎 Premium: <b>Faol ✅</b>\nCheksiz tahlil, hook, ovoz — hammasi ochiq!"
        await update.message.reply_text(txt, parse_mode="HTML")
    else:
        txt += "💎 Premium: <b>Faol emas</b>\nPremiumga o'tib, hamma imkoniyatni oching!"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("💎 Premiumga o'tish — 29,900", callback_data="buy_sub")]])
        await update.message.reply_text(txt, reply_markup=kb, parse_mode="HTML")


async def marafon_tuzat_command(update, context):
    """Admin: marafonga TIQILGAN eski userlarni tuzatadi (2-3 kundan boshlaganlar).
    Faqat video tahlili BOR (eski, tiqilgan) userlar. Tiqilgan kun sanalmagan -> bajarilgan = kun.
    /marafon_tuzat (test) yoki /marafon_tuzat HA (tuzatadi)."""
    if not is_admin(update.effective_user.id):
        return
    arg = (context.args[0] if context.args else "").upper()
    # Marafonda, tugamagan, bajarilgan < kun, VA video tahlili bor (eski tiqilgan)
    rows = _db_execute(
        "SELECT u.user_id, u.username, u.marafon_kun, COALESCE(u.marafon_bajarilgan,0), a.c "
        "FROM users u "
        "JOIN (SELECT user_id, COUNT(*) c FROM analyses WHERE kind='video' GROUP BY user_id) a "
        "ON a.user_id = u.user_id "
        "WHERE u.marafon_kun >= 2 AND u.marafon_tugadi = FALSE "
        "AND COALESCE(u.marafon_bajarilgan,0) < u.marafon_kun",
        fetch='all') or []
    if not rows:
        await update.message.reply_text("✅ Tuzatish kerak bo'lgan tiqilgan marafonchi yo'q.")
        return
    if arg != "HA":
        txt = f"🔧 <b>MARAFON TUZATISH (test)</b>\n━━━━━━━━━━━\n\n"
        txt += f"Tiqilgan eski marafonchilar: {len(rows)} ta\n\n"
        for r in rows[:20]:
            uid, uname, kun, baj, tahlil = r[0], r[1], r[2], r[3], r[4]
            who = f"@{uname}" if uname else f"ID {uid}"
            txt += f"• {who}: {tahlil} tahlil, {kun}-kun, hozir {baj}/5 → {kun}/5\n"
        if len(rows) > 20:
            txt += f"... yana {len(rows)-20} ta\n"
        txt += f"\n⚠️ Tuzatish: /marafon_tuzat HA"
        await update.message.reply_text(txt, parse_mode="HTML")
        return
    # Tuzatamiz: bajarilgan = marafon_kun (tiqilgan kunlar ham sanaladi)
    tuzatildi = 0
    for r in rows:
        uid, kun = r[0], r[2]
        _db_execute("UPDATE users SET marafon_bajarilgan = %s WHERE user_id = %s", (kun, uid))
        tuzatildi += 1
    await update.message.reply_text(
        f"✅ Tayyor! {tuzatildi} ta tiqilgan marafonchi tuzatildi.\n"
        f"Endi ular 5-kunga yetganda 5/5 bo'ladi (premium oladi).")


async def sodiq_command(update, context):
    """Admin: 10+ tahlil ishlatgan PREMIUM a'zolar (eng sodiq mijozlar).
    Har birining fikrini bilish uchun ro'yxat + ID (yozish uchun)."""
    if not is_admin(update.effective_user.id):
        return
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    # 10+ video tahlil qilgan VA obuna faol (premium)
    rows = _db_execute(
        "SELECT u.user_id, u.username, a.c FROM users u "
        "JOIN (SELECT user_id, COUNT(*) c FROM analyses WHERE kind='video' "
        "GROUP BY user_id HAVING COUNT(*) >= 10) a ON a.user_id = u.user_id "
        "WHERE u.sub_until IS NOT NULL AND u.sub_until > %s "
        "ORDER BY a.c DESC",
        (now_str,), fetch='all') or []
    rows = [r for r in rows if not is_admin(r[0])]
    if not rows:
        await update.message.reply_text("📭 10+ tahlil ishlatgan premium a'zo yo'q hozircha.")
        return
    txt = f"💎 <b>SODIQ MIJOZLAR</b> (10+ tahlil, premium)\n━━━━━━━━━━━\n"
    txt += f"Jami: {len(rows)} ta\n\n"
    for i, r in enumerate(rows, 1):
        uid, uname, cnt = r[0], r[1], r[2]
        who = f"@{uname}" if uname else "username yo'q"
        txt += f"{i}. {who} — {cnt} ta tahlil\n   ID: <code>{uid}</code>\n"
    txt += "\n💡 Fikr so'rash uchun: /yoz [ID] [xabar]"
    # Uzun bo'lsa bo'laklab yuboramiz
    if len(txt) <= 4000:
        await update.message.reply_text(txt, parse_mode="HTML")
    else:
        qism = f"💎 <b>SODIQ MIJOZLAR</b> ({len(rows)} ta)\n\n"
        for i, r in enumerate(rows, 1):
            uid, uname, cnt = r[0], r[1], r[2]
            who = f"@{uname}" if uname else "username yo'q"
            qator = f"{i}. {who} — {cnt} ta\n   ID: <code>{uid}</code>\n"
            if len(qism) + len(qator) > 3800:
                await update.message.reply_text(qism, parse_mode="HTML")
                qism = ""
            qism += qator
        if qism.strip():
            await update.message.reply_text(qism + "\n💡 Fikr: /yoz [ID] [xabar]", parse_mode="HTML")


async def marafon_kim_command(update, context):
    """Admin: aynan KIMLAR marafonda va qaysi kunda - to'liq ro'yxat."""
    if not is_admin(update.effective_user.id):
        return
    rows = _db_execute(
        "SELECT user_id, username, marafon_kun, COALESCE(marafon_bajarilgan,0), marafon_tugadi "
        "FROM users WHERE marafon_kun >= 1 "
        "ORDER BY marafon_tugadi ASC, marafon_kun DESC", fetch='all') or []
    if not rows:
        await update.message.reply_text("📭 Marafonda hech kim yo'q.")
        return
    # Kun bo'yicha guruhlaymiz
    kunlar = {1: [], 2: [], 3: [], 4: [], 5: []}
    tugatgan = []
    for r in rows:
        uid, uname, kun, bajarilgan, tugadi = r[0], r[1], r[2], r[3], r[4]
        who = f"@{uname}" if uname else f"ID {uid}"
        if tugadi:
            tugatgan.append(f"{who} (ID {uid})")
        elif kun in kunlar:
            kunlar[kun].append(f"{who} — {bajarilgan}/5 bajardi")
    txt = "🏃 <b>MARAFON — KIM QAYSI KUNDA</b>\n━━━━━━━━━━━\n\n"
    for kun in range(1, 6):
        odamlar = kunlar[kun]
        txt += f"<b>📆 {kun}-KUN</b> ({len(odamlar)} ta)\n"
        if odamlar:
            for o in odamlar[:20]:
                txt += f"  • {o}\n"
            if len(odamlar) > 20:
                txt += f"  ... yana {len(odamlar)-20} ta\n"
        else:
            txt += "  —\n"
        txt += "\n"
    txt += f"<b>🏁 TUGATGAN</b> ({len(tugatgan)} ta)\n"
    for o in tugatgan[:20]:
        txt += f"  • {o}\n"
    if len(tugatgan) > 20:
        txt += f"  ... yana {len(tugatgan)-20} ta\n"
    # Uzun bo'lsa bo'laklab yuboramiz
    if len(txt) <= 4000:
        await update.message.reply_text(txt, parse_mode="HTML")
    else:
        bloklar = txt.split("\n\n")
        qism = ""
        for blok in bloklar:
            if len(qism) + len(blok) + 2 > 3500:
                await update.message.reply_text(qism, parse_mode="HTML")
                qism = ""
            qism += blok + "\n\n"
        if qism.strip():
            await update.message.reply_text(qism, parse_mode="HTML")


async def marafon_holat_command(update, context):
    """Admin: marafon statistikasi."""
    if not is_admin(update.effective_user.id):
        return
    aktiv = get_setting("marafon_aktiv", "off")
    def cnt(q):
        r = _db_execute(q, fetch='one')
        return (r[0] if r and r[0] else 0)
    jami = cnt("SELECT COUNT(*) FROM users WHERE marafon_kun >= 1")
    k1 = cnt("SELECT COUNT(*) FROM users WHERE marafon_kun = 1 AND marafon_tugadi = FALSE")
    k2 = cnt("SELECT COUNT(*) FROM users WHERE marafon_kun = 2 AND marafon_tugadi = FALSE")
    k3 = cnt("SELECT COUNT(*) FROM users WHERE marafon_kun = 3 AND marafon_tugadi = FALSE")
    k4 = cnt("SELECT COUNT(*) FROM users WHERE marafon_kun = 4 AND marafon_tugadi = FALSE")
    tugatgan = cnt("SELECT COUNT(*) FROM users WHERE marafon_tugadi = TRUE")
    txt = f"🏃 <b>MARAFON HOLATI</b>\n━━━━━━━━━━━━━\n\n"
    txt += f"Holat: {'✅ YOQILGAN' if aktiv == 'on' else '🛑 O`chirilgan'}\n\n"
    txt += f"Jami marafonda bo'lgan: {jami}\n"
    txt += f"1-kunda: {k1}\n2-kunda: {k2}\n3-kunda: {k3}\n4-kunda: {k4}\n"
    txt += f"🏁 Tugatgan (5-kun): {tugatgan}\n"
    if jami:
        txt += f"\n📈 Tugatish darajasi: {(tugatgan/jami*100):.1f}%"
    await update.message.reply_text(txt, parse_mode="HTML")


async def marafon_test_command(update, context):
    """Admin: 5 kun marafon xabarlarini FAQAT o'zingizga ketma-ket ko'rsatadi (sinash)."""
    if not is_admin(update.effective_user.id):
        return
    aid = update.effective_user.id
    await update.message.reply_text("🧪 Marafon test: 5 kun xabari sizga ketma-ket keladi (video ham)...")
    for kun in range(1, 6):
        matn, tugma = marafon_kun_matni(kun)
        # 2-kun: jamoa videosi
        if kun == 2:
            try:
                await context.bot.send_video(aid, VIDEO_FILE_ID)
            except Exception:
                await context.bot.send_message(aid, "⚠️ [Jamoa videosi bu yerda ko'rinadi]")
        kb = marafon_kb(kun, tugma)
        await context.bot.send_message(aid, f"[TEST {kun}-KUN]\n\n" + matn, reply_markup=kb, parse_mode="HTML")
        await asyncio.sleep(0.6)
    await update.message.reply_text("✅ 5 kun ko'rsatildi! Tugmalarni bosib sinang.")


async def renewal_hozir_command(update, context):
    """Admin: renewal eslatmasini HOZIR ishga tushiradi (kutmasdan sinash)."""
    if not is_admin(update.effective_user.id):
        return
    # Avval nechta odam "ertaga tugaydi" holatida ekanini ko'rsatamiz
    now = datetime.now()
    ertaga_boshi = (now + timedelta(days=1)).strftime("%Y-%m-%d 00:00")
    ertaga_oxiri = (now + timedelta(days=1)).strftime("%Y-%m-%d 23:59")
    rows = _db_execute(
        "SELECT COUNT(*) FROM users WHERE sub_until IS NOT NULL "
        "AND sub_until >= %s AND sub_until <= %s "
        "AND (renewal_eslatma_given IS NULL OR renewal_eslatma_given = FALSE)",
        (ertaga_boshi, ertaga_oxiri), fetch='one')
    n = rows[0] if rows else 0
    if n == 0:
        await update.message.reply_text(
            "📭 Hozir 'ertaga tugaydigan' (eslatma olmagan) obunachi YO'Q.\n\n"
            "ℹ️ Renewal avtomatik har kuni 11:00 da ishlaydi.\n"
            "Kimga yuborilganini ko'rish: /renewal_royxat")
        return
    await update.message.reply_text(f"🔄 {n} ta obunachiga (ertaga tugaydi) eslatma yuborilmoqda...")
    await obuna_tugash_eslatma(context)
    await update.message.reply_text("✅ Tayyor! Natija: /renewal_royxat")


async def tolovchilar_command(update, context):
    """Admin: TO'LOVCHILAR ro'yxati - ID, paket (29900/7kunlik), qachon tugashi."""
    if not is_admin(update.effective_user.id):
        return
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    # Aktiv obunachilar: eng oxirgi to'lovi bilan
    rows = _db_execute(
        "SELECT u.user_id, u.username, u.sub_until, "
        "(SELECT package FROM payments p WHERE p.user_id = u.user_id AND p.status='approved' "
        " ORDER BY p.created DESC LIMIT 1) AS son_paket "
        "FROM users u WHERE u.sub_until IS NOT NULL AND u.sub_until > %s "
        "ORDER BY u.sub_until ASC",
        (now_str,), fetch='all') or []
    if not rows:
        await update.message.reply_text("📭 Hozir aktiv obunachi yo'q.")
        return
    # Paket nomlarini chiroyli qilamiz
    paket_nom = {
        "sub_1month": "1 oylik (29,900)",
        "sub_1month_discount": "1 oylik chegirma (19,900)",
        "test_7day": "7 kunlik (6,990)",
        "one_1": "1 tahlil",
    }
    txt = f"💎 <b>AKTIV OBUNACHILAR</b> ({len(rows)} ta)\n"
    txt += "<i>Tugash sanasi bo'yicha (yaqin → uzoq)</i>\n━━━━━━━━━━━━━\n\n"
    for i, r in enumerate(rows, 1):
        uid, uname, sub_until, paket = r[0], r[1], r[2], r[3]
        who = f"@{uname}" if uname else f"ID {uid}"
        pnom = paket_nom.get(paket, paket or "—")
        # Tugashiga necha kun qolgan
        qolgan = ""
        d = _parse_dt(sub_until)
        if d:
            kun = (d - datetime.now()).days
            qolgan = f" ({kun} kun qoldi)" if kun >= 0 else ""
        txt += f"{i}. {who} (ID {uid})\n   📦 {pnom}\n   ⏳ Tugaydi: {sub_until}{qolgan}\n\n"
    # Telegram xabar limiti 4096 - bo'lib yuboramiz
    # Telegram limiti 4096 - xavfsiz bo'laklab yuboramiz (har bo'lak to'liq userlar)
    bloklar = txt.split("\n\n")
    qism = ""
    for blok in bloklar:
        if len(qism) + len(blok) + 2 > 3500:
            if qism:
                await update.message.reply_text(qism, parse_mode="HTML")
            qism = ""
        qism += blok + "\n\n"
    if qism.strip():
        await update.message.reply_text(qism, parse_mode="HTML")


async def sotuv_natija_command(update, context):
    """Admin: har sotuv (1,1b,3,4,5) natijasi - yuborildi, bepul oldi, to'ladi."""
    if not is_admin(update.effective_user.id):
        return

    def cnt(q, p=None):
        r = _db_execute(q, p, fetch='one') if p else _db_execute(q, fetch='one')
        return (r[0] if r and r[0] is not None else 0)

    # Har sotuv uchun: yuborildi (given=TRUE), bepul oldi (sotuv_bepul_olindi LIKE)
    steps = [
        ("1️⃣ sotuv1 (5+ savol)", "sotuv1_given", "sotuv1:"),
        ("2️⃣ sotuv1b (taklif)", "sotuv1b_given", None),
        ("3️⃣ sotuv3 (2-4 tatish)", "sotuv3_given", "sotuv3:"),
        ("4️⃣ sotuv4 (1 marta)", "sotuv4_given", "sotuv4:"),
        ("5️⃣ sotuv5 (ishlatmagan)", "sotuv5_given", None),
    ]
    txt = "📊 <b>SOTUV VORONKASI NATIJASI</b>\n━━━━━━━━━━━━━\n\n"
    for nom, given_col, bepul_prefix in steps:
        yuborildi = cnt(f"SELECT COUNT(*) FROM users WHERE {given_col} = TRUE")
        txt += f"<b>{nom}</b>\n"
        txt += f"  📤 Yuborildi: {yuborildi}\n"
        if bepul_prefix:
            oldi = cnt("SELECT COUNT(*) FROM users WHERE sotuv_bepul_olindi LIKE %s", (bepul_prefix + "%",))
            txt += f"  🎁 Bepul premium oldi: {oldi}\n"
            if yuborildi:
                txt += f"  📈 Bosish darajasi: {(oldi/yuborildi*100):.1f}%\n"
        # Bu guruhdan to'laganlar: given=TRUE VA keyin to'lagan
        tolagan = cnt(
            f"SELECT COUNT(DISTINCT u.user_id) FROM users u "
            f"JOIN payments p ON p.user_id = u.user_id AND p.status='approved' "
            f"WHERE u.{given_col} = TRUE")
        txt += f"  💰 To'lagan: {tolagan}\n"
        if yuborildi:
            txt += f"  💵 Konversiya: {(tolagan/yuborildi*100):.1f}%\n"
        txt += "\n"
    txt += "━━━━━━━━━━━━━\n"
    txt += "💡 Konversiya yuqori guruhga ko'proq kuch bering!"
    await update.message.reply_text(txt, parse_mode="HTML")


async def sotuv_test_command(update, context):
    """Admin: HAMMA sotuv xabarini (1,1b,3,4,5) faqat O'ZINGIZGA yuboradi (sinash uchun)."""
    if not is_admin(update.effective_user.id):
        return
    aid = update.effective_user.id
    await update.message.reply_text("🧪 Test: hamma sotuv xabari sizga yuboriladi (5 ta)...")
    # sotuv1
    kb1 = InlineKeyboardMarkup([[InlineKeyboardButton(
        "💬 Fikr bildirish (va bepul tahlil oling!)", callback_data="sotuv1_fikr")]])
    await context.bot.send_message(aid, "1️⃣ SOTUV1 (5+ ishlatgan):\n\n" + TEXTS['uz']['sotuv1_msg'],
                                   reply_markup=kb1, parse_mode="HTML")
    await asyncio.sleep(0.3)
    # sotuv1b
    kb1b = InlineKeyboardMarkup([[InlineKeyboardButton(
        "🔥 19,900 ga Premium olish", callback_data="buy_sub_19")]])
    await context.bot.send_message(aid, "2️⃣ SOTUV1b (taklif 19,900):\n\n" + TEXTS['uz']['sotuv1b_msg'],
                                   reply_markup=kb1b, parse_mode="HTML")
    await asyncio.sleep(0.3)
    # sotuv3
    kb3 = InlineKeyboardMarkup([[InlineKeyboardButton(
        "🔥 Premium kuchini sinab ko'rish", callback_data="sotuv3_bepul")]])
    await context.bot.send_message(aid, "3️⃣ SOTUV3 (2-4 tahlil):\n\n" + TEXTS['uz']['sotuv3_msg'],
                                   reply_markup=kb3, parse_mode="HTML")
    await asyncio.sleep(0.3)
    # sotuv4
    kb4 = InlineKeyboardMarkup([[InlineKeyboardButton(
        "🎁 Bepul premium olish", callback_data="sotuv4_bepul")]])
    await context.bot.send_message(aid, "4️⃣ SOTUV4 (1 marta):\n\n" + TEXTS['uz']['sotuv4_msg'],
                                   reply_markup=kb4, parse_mode="HTML")
    await asyncio.sleep(0.3)
    # sotuv5
    kb5 = InlineKeyboardMarkup([[InlineKeyboardButton(
        "🎬 Videoni tahlil qilish", callback_data="aksiya_video")]])
    await context.bot.send_message(aid, "5️⃣ SOTUV5 (ishlatmagan):\n\n" + TEXTS['uz']['sotuv5_msg'],
                                   reply_markup=kb5, parse_mode="HTML")
    await update.message.reply_text("✅ Hammasi yuborildi! Tugmalarni bosib sinab ko'ring.")


async def maxsus_2700_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: 2+ marta tahlil qilgan, premium OLMAGAN faol foydalanuvchilarga
    maxsus taklif + chegirma (19 900, bugun 22:00 gacha)."""
    if not is_admin(update.effective_user.id):
        return
    # Chegirmani 22:00 gacha yoqamiz (narx 19 900 bo'lsin)
    now = datetime.now()
    until_dt = now.replace(hour=22, minute=0, second=0, microsecond=0)
    if until_dt <= now:
        until_dt = until_dt + timedelta(days=1)
    set_setting("chegirma_until", until_dt.strftime("%Y-%m-%d %H:%M:%S"))
    now_str = now.strftime("%Y-%m-%d %H:%M")
    # Nishon: 2+ marta video tahlil qilgan, premium emas
    rows = _db_execute(
        "SELECT u.user_id FROM users u "
        "JOIN (SELECT user_id, COUNT(*) c FROM analyses WHERE kind='video' "
        "GROUP BY user_id HAVING COUNT(*) >= 2) a ON a.user_id = u.user_id "
        "WHERE (u.sub_until IS NULL OR u.sub_until <= %s)",
        (now_str,), fetch='all'
    ) or []
    targets = [r[0] for r in rows if not is_admin(r[0])]
    if not targets:
        await update.message.reply_text("📭 Mos foydalanuvchi yo'q.")
        return
    await update.message.reply_text(
        f"🎯 Maxsus taklif (2+ tahlil, premium emas): {len(targets)} ta\n"
        f"Chegirma 19 900 (bugun 22:00 gacha) yoqildi.\n(Sekin yuboriladi, kuting)")
    sent, failed = 0, 0
    for uid in targets:
        try:
            ulang = _db_execute("SELECT lang FROM users WHERE user_id=%s", (uid,), fetch='one')
            lang = (ulang[0] if ulang and ulang[0] else 'uz')
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton(TEXTS[lang]['obuna_taklif_btn'], callback_data="buy_sub")
            ]])
            await context.bot.send_message(uid, TEXTS[lang]['maxsus_2700_msg'],
                                           reply_markup=kb, parse_mode="HTML")
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.4)
    for aid in ADMIN_IDS:
        try:
            await context.bot.send_message(aid, f"✅ Maxsus 2700: {sent} yuborildi, {failed} xato.")
        except Exception:
            pass


async def chegirma_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: chegirmani BUGUN 22:00 gacha YOQADI (19 900) va sotuv xabarini yuboradi.
    Agar hozir 22:00 dan o'tgan bo'lsa - ERTAGA 22:00 gacha."""
    if not is_admin(update.effective_user.id):
        return
    # Chegirmani BUGUN soat 22:00 gacha yoqamiz
    now = datetime.now()
    until_dt = now.replace(hour=22, minute=0, second=0, microsecond=0)
    if until_dt <= now:
        # 22:00 o'tib ketgan bo'lsa - ertaga 22:00
        until_dt = until_dt + timedelta(days=1)
    set_setting("chegirma_until", until_dt.strftime("%Y-%m-%d %H:%M:%S"))
    await update.message.reply_text(
        f"🔥 Chegirma YOQILDI! (19 900 so'm)\n"
        f"⏳ Tugash: {until_dt.strftime('%Y-%m-%d %H:%M')} (bugun soat 22:00)\n"
        f"Keyin avtomatik 29 900 ga qaytadi.\n\n"
        f"📤 Sotuv xabari obunasizlarga yuborilmoqda..."
    )
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    today = datetime.now().strftime("%Y-%m-%d")
    # Nishon: video tahlil qilgan, obunasi yo'q, BUGUN chegirma olmagan
    rows = _db_execute(
        "SELECT DISTINCT u.user_id FROM users u "
        "JOIN analyses a ON a.user_id = u.user_id AND a.kind = 'video' "
        "WHERE (u.sub_until IS NULL OR u.sub_until <= %s) "
        "AND (u.chegirma_kun IS NULL OR u.chegirma_kun <> %s)",
        (now, today), fetch='all'
    ) or []
    sent, failed = 0, 0
    for row in rows:
        uid = row[0]
        try:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton(TEXTS['uz']['obuna_taklif_btn'], callback_data="buy_sub")
            ]])
            await context.bot.send_message(uid, get_sotuv_msg(),
                                           reply_markup=kb, parse_mode="HTML")
            # Bugun olgani belgilaymiz (kuniga 1 marta)
            _db_execute("UPDATE users SET chegirma_kun = %s WHERE user_id = %s", (today, uid))
            sent += 1
        except Exception as e:
            failed += 1
            logger.warning(f"Sotuv xabarini yuborishda xato (uid={uid}): {e}")
        await asyncio.sleep(0.4)
    await update.message.reply_text(
        f"✅ Sotuv xabari tugadi!\n📨 Yuborildi: {sent}\n⚠️ Yuborilmadi: {failed}\n"
        f"(Bugun olganlarga takror yuborilmadi)"
    )


async def chegirma_ochir_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: chegirmani qo'lda O'CHIRADI (29 900 ga qaytadi)."""
    if not is_admin(update.effective_user.id):
        return
    set_setting("chegirma_until", "")
    await update.message.reply_text("🛑 Chegirma o'chirildi. Narx 29 900 ga qaytdi.")


async def top_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bugungi barcha tahlil qilingan videolarni ko'rsatadi (eng yangisi birinchi)."""
    if not is_admin(update.effective_user.id):
        return
    rows = get_today_all(200)
    if not rows:
        await update.message.reply_text("📊 Bugun hali tahlil qilingan video yo'q.")
        return
    await update.message.reply_text(
        f"🎬 BUGUNGI TAHLILLAR ({len(rows)} ta, eng yangisi birinchi)\n\n"
        "Quyida har birini videosi bilan yuboraman 👇"
    )
    for i, row in enumerate(rows, 1):
        aid, uid, uname, foiz, file_id, created = row
        who = f"@{uname}" if uname else f"ID {uid}"
        caption = f"#{i} • 📈 Rekka ehtimoli: {foiz}%\n👤 {who}\n🕐 {created}"
        try:
            await context.bot.send_video(update.effective_chat.id, file_id, caption=caption)
        except Exception as e:
            logger.warning(f"Video yuborishda xato (id={aid}): {e}")
            await update.message.reply_text(caption + "\n⚠️ (videoni yuborib bo'lmadi)")


async def aktiv_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: hozirgi aktivlik + bugungi/haftalik eng aktiv soatlar."""
    if not is_admin(update.effective_user.id):
        return

    # Vaqt zonasi: server (UTC bo'lishi mumkin) -> O'zbekiston (UTC+5)
    # created formati: 'YYYY-MM-DD HH:MM' (server vaqti). +5 soat qo'shib UZ vaqti.
    UZ_OFFSET = int(os.getenv("UZ_TZ_OFFSET", "5"))  # Railway server UTC bo'lsa +5
    now_srv = datetime.now()
    now_uz = now_srv + timedelta(hours=UZ_OFFSET)

    # Hozirgi aktivlik (server vaqti bilan solishtiramiz, chunki created server vaqtida)
    def _count_since(minutes):
        chegara = (now_srv - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M")
        row = _db_execute(
            "SELECT COUNT(DISTINCT user_id), COUNT(*) FROM analyses WHERE created >= %s",
            (chegara,), fetch='one'
        )
        return (row[0] or 0, row[1] or 0) if row else (0, 0)

    a5_u, a5_a = _count_since(5)
    a60_u, a60_a = _count_since(60)

    # Bugun (server sanasi)
    bugun_srv = now_srv.strftime("%Y-%m-%d")
    row = _db_execute(
        "SELECT COUNT(DISTINCT user_id), COUNT(*) FROM analyses WHERE created LIKE %s",
        (bugun_srv + "%",), fetch='one'
    )
    bugun_u, bugun_a = (row[0] or 0, row[1] or 0) if row else (0, 0)

    # Bugungi soatlik taqsimot (created'dan soatni olamiz, +5 UZ vaqtiga)
    rows = _db_execute(
        "SELECT created FROM analyses WHERE created LIKE %s",
        (bugun_srv + "%",), fetch='all'
    ) or []
    soatlar = {}
    for r in rows:
        try:
            dt = datetime.strptime(r[0], "%Y-%m-%d %H:%M") + timedelta(hours=UZ_OFFSET)
            h = dt.hour
            soatlar[h] = soatlar.get(h, 0) + 1
        except Exception:
            pass

    # Haftalik (oxirgi 7 kun) soatlik taqsimot
    hafta_chegara = (now_srv - timedelta(days=7)).strftime("%Y-%m-%d %H:%M")
    wrows = _db_execute(
        "SELECT created FROM analyses WHERE created >= %s",
        (hafta_chegara,), fetch='all'
    ) or []
    hafta_soat = {}
    hafta_jami = 0
    for r in wrows:
        try:
            dt = datetime.strptime(r[0], "%Y-%m-%d %H:%M") + timedelta(hours=UZ_OFFSET)
            h = dt.hour
            hafta_soat[h] = hafta_soat.get(h, 0) + 1
            hafta_jami += 1
        except Exception:
            pass

    # Matn tuzamiz
    txt = "📊 BOT AKTIVLIGI\n\n"
    txt += "🟢 HOZIR:\n"
    txt += f"• Oxirgi 5 daqiqa: {a5_u} odam, {a5_a} tahlil\n"
    txt += f"• Oxirgi 1 soat: {a60_u} odam, {a60_a} tahlil\n"
    txt += f"• Bugun jami: {bugun_u} odam, {bugun_a} tahlil\n\n"

    # Bugungi eng aktiv soatlar (top 5)
    txt += "🕐 BUGUNGI ENG AKTIV SOATLAR (O'zbekiston vaqti):\n"
    if soatlar:
        top_soat = sorted(soatlar.items(), key=lambda x: x[1], reverse=True)[:5]
        for h, c in top_soat:
            txt += f"• {h:02d}:00–{h:02d}:59 — {c} tahlil\n"
    else:
        txt += "• Bugun hali tahlil yo'q\n"

    # Haftalik eng aktiv soat
    txt += f"\n📅 SHU HAFTA ({hafta_jami} tahlil) ENG AKTIV SOATLAR:\n"
    if hafta_soat:
        top_w = sorted(hafta_soat.items(), key=lambda x: x[1], reverse=True)[:5]
        for h, c in top_w:
            txt += f"• {h:02d}:00–{h:02d}:59 — {c} tahlil\n"
        eng = top_w[0]
        txt += f"\n🏆 Eng aktiv payt: soat {eng[0]:02d}:00 atrofida"
    else:
        txt += "• Bu hafta tahlil yo'q\n"

    txt += f"\n\n🕒 Hozir (UZ): {now_uz.strftime('%H:%M')}"
    await update.message.reply_text(txt)


async def obunaochir_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: berilgan ID ning obunasini BEKOR qiladi. Foydalanish: /obunaochir <ID>"""
    if not is_admin(update.effective_user.id):
        return
    args = context.args or []
    if len(args) < 1:
        await update.message.reply_text("Foydalanish: /obunaochir <ID>\nMisol: /obunaochir 7589459697")
        return
    try:
        target_id = int(args[0])
    except Exception:
        await update.message.reply_text("⚠️ ID raqam bo'lishi kerak. Misol: /obunaochir 7589459697")
        return
    try:
        # sub_until ni NULL qilamiz (obuna bekor)
        _db_execute("UPDATE users SET sub_until = NULL WHERE user_id = %s", (target_id,))
        await update.message.reply_text(
            f"✅ Obuna bekor qilindi!\n👤 ID: {target_id}\n"
            f"Endi bu foydalanuvchi bepul (Flash-Lite) holatda."
        )
    except Exception as e:
        logger.error(f"obunaochir xato: {e}")
        await update.message.reply_text("⚠️ Xatolik yuz berdi. Qaytadan urinib ko'ring.")


async def fireworks_test_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: tabrik (konfetti) effektini sinab ko'rish."""
    if not is_admin(update.effective_user.id):
        return
    await _send_celebration(context, update.effective_chat.id,
                            TEXTS['uz']['celebrate_sub'].format(until="2026-12-31"))
    await update.message.reply_text(
        "👆 Premium olganda shu ko'rinishda chiqadi (konfetti + tabrik). "
        "Konfetti ekranda sochilsa — ishlayapti! 🎉"
    )


async def bugun_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Faqat bugungi top videolar (rekka foizi bo'yicha)."""
    if not is_admin(update.effective_user.id):
        return
    rows = get_top_today(50)
    if not rows:
        await update.message.reply_text("📊 Bugun hali tahlil qilingan video yo'q.")
        return
    await update.message.reply_text(
        f"🏆 BUGUNGI TOP ({len(rows)} ta, rekka ehtimoli bo'yicha)\n\n"
        "Quyida har birini videosi bilan yuboraman 👇"
    )
    for i, row in enumerate(rows, 1):
        aid, uid, uname, foiz, file_id, created = row
        who = f"@{uname}" if uname else f"ID {uid}"
        caption = f"#{i} • 📈 Rekka ehtimoli: {foiz}%\n👤 {who}\n🕐 {created}"
        try:
            await context.bot.send_video(update.effective_chat.id, file_id, caption=caption)
        except Exception as e:
            logger.warning(f"Bugungi video yuborishda xato (id={aid}): {e}")
            await update.message.reply_text(caption + "\n⚠️ (videoni yuborib bo'lmadi)")


async def post_init(application):
    # Bot username'ini saqlab qo'yamiz (referral havolalari uchun)
    try:
        me = await application.bot.get_me()
        application.bot_data['bot_username'] = me.username
    except Exception as e:
        logger.warning(f"get_me xato: {e}")
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


# ============================================================
# ===== PAYME MERCHANT API (web server, JSON-RPC 2.0) =====
# ============================================================
# Payme bizning serverga so'rov yuboradi (/payme endpoint).
# 6 metod: CheckPerformTransaction, CreateTransaction, PerformTransaction,
# CancelTransaction, CheckTransaction, GetStatement.
# Avtorizatsiya: Authorization: Basic base64("Paycom:KEY")

# Payme xato kodlari
PAYME_ERR_AUTH = -32504           # avtorizatsiya xatosi
PAYME_ERR_METHOD = -32601         # metod topilmadi
PAYME_ERR_AMOUNT = -31001         # noto'g'ri summa
PAYME_ERR_ACCOUNT = -31050        # account (user_id) topilmadi
PAYME_ERR_TX_NOT_FOUND = -31003   # transaksiya topilmadi
PAYME_ERR_CANT_PERFORM = -31008   # operatsiyani bajarib bo'lmaydi
PAYME_ERR_CANT_CANCEL = -31007    # bekor qilib bo'lmaydi


def _payme_now_ms():
    """Joriy vaqt millisekundda."""
    return int(datetime.now().timestamp() * 1000)


def _payme_check_auth(auth_header):
    """Authorization: Basic base64('Paycom:KEY') ni tekshiradi."""
    if not auth_header or not auth_header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
        # format: "Paycom:KEY"
        login, _, key = decoded.partition(":")
        # Jonli kalit yoki test kalit to'g'ri kelsa - ruxsat
        if key and (key == PAYME_KEY or (PAYME_TEST_KEY and key == PAYME_TEST_KEY)):
            return True
    except Exception as e:
        logger.warning(f"Payme auth dekod xatosi: {e}")
    return False


def _payme_error(req_id, code, message_ru, data=None):
    """Payme uchun xato javobi (JSON-RPC)."""
    err = {"code": code, "message": {"ru": message_ru, "uz": message_ru, "en": message_ru}}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


def _payme_result(req_id, result):
    """Payme uchun muvaffaqiyatli javob (JSON-RPC)."""
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


# Paket narxlari (tiyinda) -> qaysi paket ekanini aniqlash uchun
def _payme_package_by_amount(amount_tiyin):
    """Summa (tiyinda) bo'yicha paketni aniqlaydi."""
    som = amount_tiyin // 100
    if som == SUB_PRICE:
        return ("sub_1month", SUB_DAYS)
    if som == SUB_PRICE_DISCOUNT:
        return ("sub_1month_discount", SUB_DAYS)
    if som == SUB_PRICE_RENEWAL:
        return ("sub_1month_renewal", SUB_DAYS)
    if som == TEST_PRICE:
        return ("test_7day", TEST_DAYS)
    if som == ONE_PRICE:
        return ("one_1", 0)
    return (None, None)


def _payme_checkout_link(user_id, amount_som):
    """Payme GET to'lov havolasini yasaydi (checkout.paycom.uz).
    user_id - mijoz Telegram ID, amount_som - summa (so'mda)."""
    amount_tiyin = amount_som * 100  # so'm -> tiyin
    params = f"m={PAYME_MERCHANT_ID};ac.user_id={user_id};a={amount_tiyin}"
    encoded = base64.b64encode(params.encode("utf-8")).decode("utf-8")
    return f"https://checkout.paycom.uz/{encoded}"


def _click_checkout_link(user_id, amount_som, pkg):
    """Click to'lov havolasini yasaydi (my.click.uz).
    transaction_param = "uid_pkg" -> Complete'da uid va pkg ajratamiz."""
    trans_param = f"{user_id}_{pkg}"
    return (f"https://my.click.uz/services/pay?service_id={CLICK_SERVICE_ID}"
            f"&merchant_id={CLICK_MERCHANT_ID}&amount={amount_som}"
            f"&transaction_param={trans_param}")


def _payme_handle(body):
    """Payme JSON-RPC so'rovini qayta ishlaydi. body - dict. Javob - dict."""
    req_id = body.get("id")
    method = body.get("method")
    params = body.get("params", {}) or {}

    # ---- CheckPerformTransaction: to'lov mumkinmi? ----
    if method == "CheckPerformTransaction":
        amount = params.get("amount", 0)
        account = params.get("account", {}) or {}
        uid = account.get("user_id")
        # 1) Avval account (user_id) ni tekshiramiz
        if not uid:
            return _payme_error(req_id, -31050, "Foydalanuvchi ID kiritilmagan", data="user_id")
        try:
            uid_int = int(uid)
        except Exception:
            return _payme_error(req_id, -31050, "Noto'g'ri foydalanuvchi ID", data="user_id")
        # Foydalanuvchi bazada bormi?
        if not user_exists(uid_int):
            return _payme_error(req_id, -31050, "Bunday foydalanuvchi topilmadi", data="user_id")
        # 2) Keyin summani tekshiramiz
        pkg, days = _payme_package_by_amount(amount)
        if pkg is None:
            return _payme_error(req_id, PAYME_ERR_AMOUNT, "Noto'g'ri summa")
        return _payme_result(req_id, {"allow": True})

    # ---- CreateTransaction: transaksiya yaratish ----
    if method == "CreateTransaction":
        payme_id = params.get("id")
        amount = params.get("amount", 0)
        time_ms = params.get("time", _payme_now_ms())
        account = params.get("account", {}) or {}
        uid = account.get("user_id")
        # 1) Account (user_id) tekshiruvi
        if not uid:
            return _payme_error(req_id, -31050, "Foydalanuvchi ID kiritilmagan", data="user_id")
        try:
            uid_int = int(uid)
        except Exception:
            return _payme_error(req_id, -31050, "Noto'g'ri foydalanuvchi ID", data="user_id")
        if not user_exists(uid_int):
            return _payme_error(req_id, -31050, "Bunday foydalanuvchi topilmadi", data="user_id")
        # 2) Summa tekshiruvi
        pkg, days = _payme_package_by_amount(amount)
        if pkg is None:
            return _payme_error(req_id, PAYME_ERR_AMOUNT, "Noto'g'ri summa")
        # Bu payme_id allaqachon bormi?
        row = _db_execute(
            "SELECT payme_id, state, create_time FROM payme_transactions WHERE payme_id = %s",
            (payme_id,), fetch='one'
        )
        if row:
            # Mavjud - o'sha holatni qaytaramiz
            return _payme_result(req_id, {
                "create_time": row[2],
                "transaction": payme_id,
                "state": row[1],
            })
        # Yangi transaksiya yaratamiz
        _db_execute(
            "INSERT INTO payme_transactions (payme_id, user_id, package, amount, state, create_time, created) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (payme_id, int(uid), pkg, amount, PAYME_STATE_CREATED, time_ms,
             datetime.now().strftime("%Y-%m-%d %H:%M"))
        )
        return _payme_result(req_id, {
            "create_time": time_ms,
            "transaction": payme_id,
            "state": PAYME_STATE_CREATED,
        })

    # ---- PerformTransaction: to'lovni amalga oshirish (PUL TUSHDI) ----
    if method == "PerformTransaction":
        payme_id = params.get("id")
        row = _db_execute(
            "SELECT user_id, package, amount, state, perform_time FROM payme_transactions WHERE payme_id = %s",
            (payme_id,), fetch='one'
        )
        if not row:
            return _payme_error(req_id, PAYME_ERR_TX_NOT_FOUND, "Transaksiya topilmadi")
        uid, pkg, amount, state, perform_time = row
        if state == PAYME_STATE_PERFORMED:
            # Allaqachon bajarilgan
            return _payme_result(req_id, {
                "transaction": payme_id,
                "perform_time": perform_time,
                "state": PAYME_STATE_PERFORMED,
            })
        if state != PAYME_STATE_CREATED:
            return _payme_error(req_id, PAYME_ERR_CANT_PERFORM, "Operatsiyani bajarib bo'lmaydi")
        # TO'LOV TASDIQLANDI - obunani yoqamiz!
        now_ms = _payme_now_ms()
        _db_execute(
            "UPDATE payme_transactions SET state = %s, perform_time = %s WHERE payme_id = %s",
            (PAYME_STATE_PERFORMED, now_ms, payme_id)
        )
        # Paketga qarab obuna/balans beramiz
        _payme_activate(uid, pkg, amount)
        return _payme_result(req_id, {
            "transaction": payme_id,
            "perform_time": now_ms,
            "state": PAYME_STATE_PERFORMED,
        })

    # ---- CancelTransaction: bekor qilish ----
    if method == "CancelTransaction":
        payme_id = params.get("id")
        reason = params.get("reason")
        row = _db_execute(
            "SELECT state, cancel_time FROM payme_transactions WHERE payme_id = %s",
            (payme_id,), fetch='one'
        )
        if not row:
            return _payme_error(req_id, PAYME_ERR_TX_NOT_FOUND, "Transaksiya topilmadi")
        state, cancel_time = row
        now_ms = _payme_now_ms()
        if state == PAYME_STATE_CREATED:
            new_state = PAYME_STATE_CANCELED
        elif state == PAYME_STATE_PERFORMED:
            new_state = PAYME_STATE_CANCELED_AFTER
        else:
            # Allaqachon bekor qilingan
            return _payme_result(req_id, {
                "transaction": payme_id,
                "cancel_time": cancel_time or now_ms,
                "state": state,
            })
        if not cancel_time:
            cancel_time = now_ms
        _db_execute(
            "UPDATE payme_transactions SET state = %s, reason = %s, cancel_time = %s WHERE payme_id = %s",
            (new_state, reason, cancel_time, payme_id)
        )
        return _payme_result(req_id, {
            "transaction": payme_id,
            "cancel_time": cancel_time,
            "state": new_state,
        })

    # ---- CheckTransaction: holatni tekshirish ----
    if method == "CheckTransaction":
        payme_id = params.get("id")
        row = _db_execute(
            "SELECT state, reason, create_time, perform_time, cancel_time FROM payme_transactions WHERE payme_id = %s",
            (payme_id,), fetch='one'
        )
        if not row:
            return _payme_error(req_id, PAYME_ERR_TX_NOT_FOUND, "Transaksiya topilmadi")
        state, reason, create_time, perform_time, cancel_time = row
        return _payme_result(req_id, {
            "create_time": create_time or 0,
            "perform_time": perform_time or 0,
            "cancel_time": cancel_time or 0,
            "transaction": payme_id,
            "state": state,
            "reason": reason,
        })

    # ---- GetStatement: davr bo'yicha transaksiyalar ro'yxati ----
    if method == "GetStatement":
        frm = params.get("from", 0)
        to = params.get("to", _payme_now_ms())
        rows = _db_execute(
            "SELECT payme_id, user_id, amount, state, reason, create_time, perform_time, cancel_time "
            "FROM payme_transactions WHERE create_time >= %s AND create_time <= %s ORDER BY create_time ASC",
            (frm, to), fetch='all'
        ) or []
        txs = []
        for r in rows:
            txs.append({
                "id": r[0],
                "time": r[5] or 0,
                "amount": r[2],
                "account": {"user_id": str(r[1])},
                "create_time": r[5] or 0,
                "perform_time": r[6] or 0,
                "cancel_time": r[7] or 0,
                "transaction": r[0],
                "state": r[3],
                "reason": r[4],
            })
        return _payme_result(req_id, {"transactions": txs})

    # Noma'lum metod
    return _payme_error(req_id, PAYME_ERR_METHOD, "Metod topilmadi")


def _payme_activate(uid, pkg, amount):
    """To'lov tasdiqlangach obuna/balans beradi + tabrik (fireworks) yuboradi."""
    celebrate_key = None
    until = None
    try:
        if pkg in ("sub_1month", "sub_1month_discount", "sub_1month_renewal"):
            until = activate_subscription(uid, SUB_DAYS)
            create_payment(uid, pkg, amount // 100)
            celebrate_key = "celebrate_sub"
        elif pkg == "test_7day":
            until = activate_subscription(uid, TEST_DAYS)
            create_payment(uid, pkg, amount // 100)
            celebrate_key = "celebrate_test"
        elif pkg == "one_1":
            add_balance(uid, 1)
            create_payment(uid, pkg, amount // 100)
            celebrate_key = "celebrate_one"
        logger.info(f"Payme to'lov faollashtirildi: uid={uid}, paket={pkg}")
        # Tabrik xabarini fireworks bilan yuboramiz (bot loop'iga)
        if celebrate_key and _bot_app is not None and _main_loop is not None:
            try:
                txt = TEXTS['uz'][celebrate_key]
                if until:
                    txt = txt.format(until=until)
                asyncio.run_coroutine_threadsafe(_send_celebration_uid(uid, txt), _main_loop)
            except Exception as e:
                logger.warning(f"Payme tabrik yuborishda xato (uid={uid}): {e}")
        # Adminlarga ham xabar
        if _bot_app is not None and _main_loop is not None:
            try:
                som = amount // 100
                admin_txt = (f"💰 YANGI TO'LOV (Payme Merchant)\n👤 ID: {uid}\n"
                             f"📦 {pkg}\n💵 {som:,} so'm\n✅ Faollashtirildi")
                for _aid in ADMIN_IDS:
                    asyncio.run_coroutine_threadsafe(
                        _bot_app.bot.send_message(_aid, admin_txt), _main_loop)
            except Exception:
                pass
    except Exception as e:
        logger.error(f"Payme activate xato (uid={uid}): {e}")


async def _send_celebration_uid(uid, text):
    """Tabrikni fireworks bilan yuboradi (uid bo'yicha)."""
    try:
        await _bot_app.bot.send_message(uid, text, parse_mode="HTML",
                                        message_effect_id=PAYME_FIREWORKS_EFFECT)
        return
    except Exception:
        pass
    try:
        await _bot_app.bot.send_message(uid, text, parse_mode="HTML")
    except Exception:
        pass


async def payme_web_handler(request):
    """aiohttp: /payme endpoint - Payme so'rovlarini qabul qiladi."""
    # Avtorizatsiya
    auth = request.headers.get("Authorization", "")
    try:
        body = await request.json()
    except Exception:
        body = {}
    req_id = body.get("id") if isinstance(body, dict) else None
    if not _payme_check_auth(auth):
        return web.json_response(
            _payme_error(req_id, PAYME_ERR_AUTH, "Avtorizatsiya xatosi"),
            status=200
        )
    # So'rovni qayta ishlaymiz (DB - sinxron, alohida thread'da)
    try:
        result = await asyncio.to_thread(_payme_handle, body)
    except Exception as e:
        logger.error(f"Payme handler xato: {e}")
        result = _payme_error(req_id, -32400, "Sistema xatosi")
    return web.json_response(result, status=200)


async def health_handler(request):
    """Oddiy health-check (Railway uchun)."""
    return web.Response(text="OK")


# ===== MINI APP (Telegram WebApp) =====
MINIAPP_HTML = """<!DOCTYPE html>
<html lang="uz"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>InstaDoctor AI</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
:root{--bg:#0B0E14;--card:#151A23;--accent:#00E5A0;--accent2:#7C5CFF;--text:#EAF0F6;--muted:#8A94A6;--line:#232C38;--warn:#FFB020}
*{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:var(--bg);color:var(--text);padding:16px 14px 40px;max-width:520px;margin:0 auto;-webkit-font-smoothing:antialiased}
.hdr{display:flex;align-items:center;gap:12px;margin-bottom:18px}
.logo{width:46px;height:46px;border-radius:14px;background:linear-gradient(135deg,var(--accent),var(--accent2));display:flex;align-items:center;justify-content:center;font-size:24px;flex-shrink:0}
.hdr h1{font-size:19px;font-weight:700;letter-spacing:-0.3px}.hdr p{font-size:12.5px;color:var(--muted);margin-top:2px}
.status{border-radius:16px;padding:16px;margin-bottom:14px;background:linear-gradient(135deg,rgba(0,229,160,0.12),rgba(124,92,255,0.10));border:1px solid var(--line)}
.status.free{background:linear-gradient(135deg,rgba(255,176,32,0.10),rgba(255,92,92,0.06))}
.status .row{display:flex;justify-content:space-between;align-items:center}
.status .badge{font-size:12px;font-weight:700;padding:5px 11px;border-radius:999px;background:var(--accent);color:#05221A}
.status.free .badge{background:var(--warn);color:#2A1C00}
.status .big{font-size:26px;font-weight:800;margin-top:10px;letter-spacing:-0.5px}.status .sub{font-size:12.5px;color:var(--muted);margin-top:3px}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:9px;margin-bottom:16px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:13px;padding:13px 10px;text-align:center}
.stat .n{font-size:21px;font-weight:800;letter-spacing:-0.5px}.stat .n.accent{color:var(--accent)}.stat .l{font-size:10.5px;color:var(--muted);margin-top:3px}
.sec-title{font-size:13px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:0.6px;margin:20px 4px 11px}
.btn-primary{width:100%;border:none;border-radius:14px;padding:16px;background:linear-gradient(135deg,var(--accent),#00C98D);color:#05221A;font-size:16px;font-weight:800;cursor:pointer;display:flex;align-items:center;justify-content:center;gap:8px;box-shadow:0 6px 20px rgba(0,229,160,0.25)}
.btn-primary:active{transform:scale(0.985)}
.tariffs{display:flex;flex-direction:column;gap:10px}
.tariff{background:var(--card);border:1px solid var(--line);border-radius:15px;padding:15px;cursor:pointer;position:relative}
.tariff.best{border-color:var(--accent)}
.tariff .tag{position:absolute;top:-9px;right:14px;background:var(--accent);color:#05221A;font-size:10.5px;font-weight:800;padding:3px 10px;border-radius:999px}
.tariff .row{display:flex;justify-content:space-between;align-items:flex-start}
.tariff .name{font-size:15.5px;font-weight:700}.tariff .desc{font-size:12px;color:var(--muted);margin-top:4px;line-height:1.5}
.tariff .price{font-size:19px;font-weight:800;white-space:nowrap}.tariff .price small{font-size:11px;color:var(--muted);font-weight:500}
.tariff .old{font-size:12px;color:var(--muted);text-decoration:line-through;display:block;text-align:right}
.feat{display:flex;flex-direction:column;gap:9px}
.frow{display:flex;align-items:center;gap:11px;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 13px}
.frow .ic{font-size:19px;width:24px;text-align:center;flex-shrink:0}.frow .ft{font-size:13.5px;font-weight:600}.frow .fd{font-size:11.5px;color:var(--muted);margin-top:1px}.frow .lock{margin-left:auto;font-size:13px;color:var(--warn)}
.foot{text-align:center;font-size:11px;color:var(--muted);margin-top:26px;line-height:1.6}
</style></head><body>
<div class="hdr"><div class="logo">\U0001FA7A</div><div><h1>InstaDoctor AI</h1><p id="greeting">Reels tahlilchingiz</p></div></div>
<div id="statusCard" class="status"><div class="row"><span class="badge" id="statusBadge">PREMIUM</span><span class="sub" id="statusDate"></span></div><div class="big" id="statusBig">Premium faol</div><div class="sub" id="statusSub">Cheksiz tahlil ochiq</div></div>
<div class="stats"><div class="stat"><div class="n accent" id="stTotal">0</div><div class="l">Tahlil</div></div><div class="stat"><div class="n" id="stBest">\u2014</div><div class="l">Eng yuqori</div></div><div class="stat"><div class="n" id="stBalance">0</div><div class="l">Bepul qoldi</div></div></div>
<button class="btn-primary" onclick="pickVideo()">\U0001F3AC Video tahlil qilish</button>
<input type="file" id="vfile" accept="video/*" style="display:none">
<div id="vmsg" style="font-size:12.5px;color:var(--muted);text-align:center;margin-top:8px;display:none"></div>
<div class="sec-title">Premium imkoniyatlar</div>
<div class="feat">
<div class="frow"><span class="ic">\u267E\uFE0F</span><div><div class="ft">Cheksiz tahlil</div><div class="fd">Limitsiz video va profil</div></div><span class="lock" id="l1"></span></div>
<div class="frow"><span class="ic">\U0001F525</span><div><div class="ft">Qanday yaxshilash?</div><div class="fd">Hook + 3 ta tayyor variant</div></div><span class="lock" id="l2"></span></div>
<div class="frow"><span class="ic">\U0001F399\uFE0F</span><div><div class="ft">Ovozli maslahat</div><div class="fd">Tahlilni eshiting</div></div><span class="lock" id="l3"></span></div>
<div class="frow"><span class="ic">\U0001F4CA</span><div><div class="ft">Profil tahlili</div><div class="fd">Shaxsiy strategiya</div></div><span class="lock" id="l4"></span></div>
</div>
<div id="tariffSection"><div class="sec-title">Tariflar</div><div class="tariffs">
<div class="tariff best" onclick="sendAction('buy_sub')"><div class="tag">ENG MASHHUR</div><div class="row"><div><div class="name">1 oylik Premium</div><div class="desc">Cheksiz tahlil + barcha imkoniyatlar</div></div><div><span class="old" id="subOld"></span><div class="price" id="subPrice">29 900<small> so'm</small></div></div></div></div>
<div class="tariff" onclick="sendAction('buy_one')"><div class="row"><div><div class="name">1 ta tahlil</div><div class="desc">Bir martalik chuqur tahlil</div></div><div class="price">5 090<small> so'm</small></div></div></div>
</div></div>
<div class="foot">InstaDoctor AI \u2014 Instagram algoritmlari bo'yicha tahlil<br>Yordam: @Nurislom_admin</div>
<script>
const tg=window.Telegram?window.Telegram.WebApp:null;if(tg){tg.ready();tg.expand();}
const p=new URLSearchParams(location.search);
const isPremium=p.get('premium')==='1';const total=p.get('total')||'0';const best=p.get('best')||'\u2014';const balance=p.get('balance')||'0';const name=p.get('name')||'';const price=p.get('price')||'29 900';const oldPrice=p.get('old')||'';const subUntil=p.get('until')||'';
if(name)document.getElementById('greeting').textContent=name+', xush kelibsiz!';
document.getElementById('stTotal').textContent=total;document.getElementById('stBest').textContent=best==='\u2014'?'\u2014':best+'%';document.getElementById('stBalance').textContent=balance;
const card=document.getElementById('statusCard'),badge=document.getElementById('statusBadge'),big=document.getElementById('statusBig'),sub=document.getElementById('statusSub'),sdate=document.getElementById('statusDate');
if(isPremium){card.classList.remove('free');badge.textContent='PREMIUM';big.textContent='Premium faol \u2728';sub.textContent='Barcha imkoniyatlar ochiq';if(subUntil)sdate.textContent='Tugaydi: '+subUntil;document.getElementById('tariffSection').style.display='none';['l1','l2','l3','l4'].forEach(id=>document.getElementById(id).textContent='\u2713');}
else{card.classList.add('free');badge.textContent='BEPUL';big.textContent='Bepul rejim';sub.textContent=balance>0?balance+' ta bepul tahlil qoldi':'Bepul tahlillar tugadi';['l1','l2','l3','l4'].forEach(id=>document.getElementById(id).textContent='\U0001F512');document.getElementById('subPrice').innerHTML=price+"<small> so'm</small>";if(oldPrice)document.getElementById('subOld').textContent=oldPrice+" so'm";}
function sendAction(action){if(tg){tg.HapticFeedback&&tg.HapticFeedback.impactOccurred('medium');tg.sendData(JSON.stringify({action:action}));tg.close();}else{alert('Telegram ichida ishlaydi: '+action);}}
var LIMIT_MB=(isPremium?500:100);
function pickVideo(){document.getElementById('vfile').click();}
document.getElementById('vfile').addEventListener('change',function(e){
  var f=e.target.files[0];if(!f)return;
  var mb=f.size/(1024*1024);
  var msg=document.getElementById('vmsg');msg.style.display='block';
  if(mb>LIMIT_MB){
    msg.style.color='#FFB020';
    msg.innerHTML='\u26A0\uFE0F Bu video katta ('+mb.toFixed(1)+' MB). '+(isPremium?'Premium\\'da 500 MB gacha mumkin. ':'Bepul 100 MB gacha. ')+'Kattaroq videolarni to\\'g\\'ridan botga tashlang \u2014 ilova yopiladi.';
    if(tg){tg.HapticFeedback&&tg.HapticFeedback.notificationOccurred('warning');setTimeout(function(){tg.sendData(JSON.stringify({action:'analyze'}));tg.close();},2200);}
  }else{
    msg.style.color='#00E5A0';
    msg.innerHTML='\u2705 Video tanlandi ('+mb.toFixed(1)+' MB). Yuklanmoqda... \u23F3';
    if(tg){tg.HapticFeedback&&tg.HapticFeedback.impactOccurred('medium');}
    var uid=(tg&&tg.initDataUnsafe&&tg.initDataUnsafe.user)?tg.initDataUnsafe.user.id:p.get('uid');
    if(!uid){msg.style.color='#FF5C5C';msg.innerHTML='\u274C Foydalanuvchi aniqlanmadi. Botga to\\'g\\'ridan tashlang.';return;}
    var fd=new FormData();fd.append('user_id',uid);fd.append('video',f);
    fetch('/upload',{method:'POST',body:fd}).then(function(r){return r.json();}).then(function(d){
      if(d.ok){msg.style.color='#00E5A0';msg.innerHTML='\u2705 Yuklandi! Tahlil botda boshlandi \u2014 ilova yopilmoqda...';if(tg){tg.HapticFeedback&&tg.HapticFeedback.notificationOccurred('success');setTimeout(function(){tg.close();},1600);}}
      else{msg.style.color='#FF5C5C';msg.innerHTML='\u274C Xato: '+(d.error||'')+'. Botga to\\'g\\'ridan tashlang.';}
    }).catch(function(){msg.style.color='#FF5C5C';msg.innerHTML='\u274C Yuklab bo\\'lmadi. Botga to\\'g\\'ridan tashlang.';});
  }
});
</script></body></html>"""


async def miniapp_handler(request):
    """Mini App sahifasini ko'rsatadi (Telegram WebApp)."""
    return web.Response(text=MINIAPP_HTML, content_type="text/html")


async def upload_handler(request):
    """Mini App'dan kelgan videoni qabul qiladi va foydalanuvchi chatiga yuboradi.
    Bot videoni avtomatik tahlil qiladi (mavjud oqim)."""
    try:
        reader = await request.multipart()
        uid = None
        video_bytes = None
        filename = "video.mp4"
        while True:
            field = await reader.next()
            if field is None:
                break
            if field.name == "user_id":
                uid = (await field.read()).decode().strip()
            elif field.name == "video":
                filename = field.filename or "video.mp4"
                video_bytes = await field.read()
        if not uid or not video_bytes:
            return web.json_response({"ok": False, "error": "uid yoki video yo'q"}, status=400)
        try:
            uid_int = int(uid)
        except Exception:
            return web.json_response({"ok": False, "error": "uid xato"}, status=400)
        # Vaqtincha saqlash
        import tempfile, os as _os
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4", dir="/tmp")
        tmp.write(video_bytes)
        tmp.close()
        # Foydalanuvchi chatiga video yuboramiz (bot uni tahlil qiladi)
        try:
            if _bot_app and _main_loop:
                async def _send():
                    with open(tmp.name, "rb") as vf:
                        await _bot_app.bot.send_video(uid_int, vf,
                            caption="🎬 Mini App'dan kelgan video — tahlil qilinmoqda...")
                fut = asyncio.run_coroutine_threadsafe(_send(), _main_loop) if False else None
                await _send()
            else:
                return web.json_response({"ok": False, "error": "bot tayyor emas"}, status=500)
        finally:
            try:
                _os.unlink(tmp.name)
            except Exception:
                pass
        return web.json_response({"ok": True})
    except Exception as e:
        logger.error(f"Upload xato: {e}")
        return web.json_response({"ok": False, "error": str(e)}, status=500)


# ===== CLICK Merchant API (Prepare + Complete) =====
def _click_signature(data, secret_key):
    """Click imzosini (MD5) tekshiradi/yasaydi.
    Prepare: md5(click_trans_id + service_id + secret_key + merchant_trans_id +
             amount + action + sign_time)
    Complete: md5(click_trans_id + service_id + secret_key + merchant_trans_id +
             merchant_prepare_id + amount + action + sign_time)"""
    import hashlib
    action = str(data.get("action", ""))
    parts = [
        str(data.get("click_trans_id", "")),
        str(data.get("service_id", "")),
        secret_key,
        str(data.get("merchant_trans_id", "")),
    ]
    if action == "1":  # Complete'da merchant_prepare_id ham bor
        parts.append(str(data.get("merchant_prepare_id", "")))
    parts.append(str(data.get("amount", "")))
    parts.append(action)
    parts.append(str(data.get("sign_time", "")))
    return hashlib.md5("".join(parts).encode()).hexdigest()


def _click_handle(data):
    """Click so'rovini qayta ishlaydi (Prepare action=0, Complete action=1).
    merchant_trans_id = "uid_pkg" formatida (masalan "123456_test_7day")."""
    action = str(data.get("action", ""))
    # Imzo tekshirish
    sign = data.get("sign_string", "")
    if sign != _click_signature(data, CLICK_SECRET_KEY):
        return {"error": -1, "error_note": "SIGN CHECK FAILED"}
    merchant_trans_id = str(data.get("merchant_trans_id", ""))
    click_trans_id = data.get("click_trans_id", "")
    amount = data.get("amount", "")
    # merchant_trans_id'dan uid va pkg ajratamiz
    try:
        parts = merchant_trans_id.split("_", 1)
        uid = int(parts[0])
        pkg = parts[1] if len(parts) > 1 else "one_1"
    except Exception:
        return {"error": -5, "error_note": "Foydalanuvchi topilmadi"}

    if action == "0":  # PREPARE
        return {
            "click_trans_id": click_trans_id,
            "merchant_trans_id": merchant_trans_id,
            "merchant_prepare_id": int(time.time()),  # tayyorlov ID
            "error": 0,
            "error_note": "Success",
        }
    elif action == "1":  # COMPLETE
        error_code = data.get("error", "0")
        if str(error_code) == "0":
            # To'lov muvaffaqiyatli -> obuna/balans beramiz (som)
            try:
                amount_som = int(float(amount))
            except Exception:
                amount_som = 0
            _payme_activate(uid, pkg, amount_som * 100)  # _payme_activate som*100 kutadi
        return {
            "click_trans_id": click_trans_id,
            "merchant_trans_id": merchant_trans_id,
            "merchant_confirm_id": int(time.time()),
            "error": 0,
            "error_note": "Success",
        }
    return {"error": -3, "error_note": "Action topilmadi"}


async def click_web_handler(request):
    """aiohttp: /click endpoint - Click so'rovlarini qabul qiladi."""
    try:
        # Click form-data yuboradi (JSON emas)
        data = await request.post()
        data = dict(data)
    except Exception:
        data = {}
    try:
        result = await asyncio.to_thread(_click_handle, data)
    except Exception as e:
        logger.error(f"Click handler xato: {e}")
        result = {"error": -9, "error_note": "Sistema xatosi"}
    return web.json_response(result, status=200)


async def run_web_server():
    """aiohttp web serverni ishga tushiradi (Payme + Click uchun)."""
    web_app = web.Application()
    web_app.router.add_post("/payme", payme_web_handler)
    web_app.router.add_post("/click", click_web_handler)
    web_app.router.add_get("/", health_handler)
    web_app.router.add_get("/health", health_handler)
    web_app.router.add_get("/app", miniapp_handler)
    web_app.router.add_post("/upload", upload_handler)
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WEB_PORT)
    await site.start()
    logger.info(f"Web server ishga tushdi (port {WEB_PORT}) - Payme /payme + Click /click tayyor")


async def sticker_id_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin stiker yuborsa - uning file_id'sini ko'rsatadi (tabrik stikeri tanlash uchun)."""
    if not is_admin(update.effective_user.id):
        return
    try:
        st = update.message.sticker
        if st:
            await update.message.reply_text(
                f"🎨 Stiker ID:\n<code>{st.file_id}</code>\n\n"
                f"Animatsiyali: {st.is_animated or st.is_video}\n\n"
                f"Shu stikerni tabrik uchun ishlatishni xohlasangiz — menga shu ID'ni ayting.",
                parse_mode="HTML"
            )
    except Exception as e:
        logger.warning(f"sticker_id xato: {e}")


async def tarif7_2soat_eslatma(context: ContextTypes.DEFAULT_TYPE):
    """Aksiya tugashiga 2 soat qolganda premium olmagan hammaga eslatma."""
    if get_setting("tarif7_aksiya", "on") != "on":
        return  # aksiya o'chirilgan - eslatma yubormaymiz
    rows = _db_execute("SELECT user_id FROM users", fetch='all') or []
    targets = [r[0] for r in rows if not is_admin(r[0]) and not sub_active(r[0])]
    sent = 0
    for uid in targets:
        try:
            payme_link = _payme_checkout_link(uid, TEST_PRICE)
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton(TEXTS['uz']['tarif7_2soat_btn'], url=payme_link)],
                [InlineKeyboardButton(TEXTS['uz']['tarif7_btn_card'], callback_data='card_test')],
            ])
            await context.bot.send_message(uid, TEXTS['uz']['tarif7_2soat'],
                                           reply_markup=kb, parse_mode="HTML")
            sent += 1
        except Exception:
            pass
        await asyncio.sleep(0.4)
    logger.info(f"tarif7 2-soat eslatma: {sent} ta yuborildi")
    for aid in ADMIN_IDS:
        try:
            await context.bot.send_message(aid, f"⏰ Aksiya '2 soat qoldi' eslatmasi: {sent} ta yuborildi.")
        except Exception:
            pass


async def tarif7_ochirish(context: ContextTypes.DEFAULT_TYPE):
    """Aksiya vaqti tugadi - 7 minglikni o'chiramiz."""
    set_setting("tarif7_aksiya", "off")
    logger.info("tarif7 aksiyasi avtomatik o'chirildi (vaqt tugadi)")
    for aid in ADMIN_IDS:
        try:
            await context.bot.send_message(aid, "🔴 7 kunlik aksiya tugadi (avtomatik o'chirildi).")
        except Exception:
            pass


async def obuna_tugadi_xabar(context: ContextTypes.DEFAULT_TYPE):
    """Har kuni: obunasi endigina TUGAGAN userlar haqida adminga xabar beradi.
    Har user uchun 1 marta (tugash_xabar_given)."""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    # Obunasi o'tган (tugagan), lekin hali xabar berilmagan, sub_until bor
    rows = _db_execute(
        "SELECT u.user_id, u.username, u.sub_until, "
        "(SELECT package FROM payments p WHERE p.user_id=u.user_id AND p.status='approved' "
        " ORDER BY p.created DESC LIMIT 1) "
        "FROM users u WHERE u.sub_until IS NOT NULL AND u.sub_until <= %s "
        "AND (u.tugash_xabar_given IS NULL OR u.tugash_xabar_given = FALSE)",
        (now_str,), fetch='all') or []
    if not rows:
        return
    paket_nom = {"sub_1month": "1 oylik", "sub_1month_discount": "1 oylik (chegirma)",
                 "sub_1month_renewal": "1 oylik (renewal)", "test_7day": "7 kunlik", "one_1": "1 tahlil"}
    for r in rows:
        uid, uname, sub_until, paket = r[0], r[1], r[2], r[3]
        who = f"@{uname}" if uname else f"ID {uid}"
        pnom = paket_nom.get(paket, paket or "obuna")
        for aid in ADMIN_IDS:
            try:
                await context.bot.send_message(
                    aid, f"⌛️ <b>OBUNA TUGADI</b>\n📦 {pnom}\n👤 {who} (ID {uid})",
                    parse_mode="HTML")
            except Exception:
                pass
        _db_execute("UPDATE users SET tugash_xabar_given = TRUE WHERE user_id = %s", (uid,))
        await asyncio.sleep(0.2)


async def obuna_tugash_eslatma(context: ContextTypes.DEFAULT_TYPE):
    """Har kuni: obunasi 1 kundan keyin tugaydigan foydalanuvchilarga
    1 oylik taklif yuboradi (renewal). Har userga 1 marta (renewal_eslatma_given)."""
    now = datetime.now()
    ertaga_boshi = (now + timedelta(days=1)).strftime("%Y-%m-%d 00:00")
    ertaga_oxiri = (now + timedelta(days=1)).strftime("%Y-%m-%d 23:59")
    # Obunasi ertaga tugaydigan, hali eslatma olmagan userlar
    rows = _db_execute(
        "SELECT user_id FROM users WHERE sub_until IS NOT NULL "
        "AND sub_until >= %s AND sub_until <= %s "
        "AND (renewal_eslatma_given IS NULL OR renewal_eslatma_given = FALSE)",
        (ertaga_boshi, ertaga_oxiri), fetch='all') or []
    if not rows:
        return
    sent = 0
    for r in rows:
        uid = r[0]
        if is_admin(uid):
            continue
        try:
            # Chegirma yoqamiz (24 soat) - shu userga 19,900
            set_setting("chegirma_until", (datetime.now() + timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S"))
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔥 19,900 ga 1 oylik olish", callback_data="buy_sub_19")
            ]])
            await context.bot.send_message(
                uid,
                "⏳ <b>Premiumingiz ertaga tugaydi!</b> 😱\n\n"
                "Endigina qizigan edi... Kontentingiz o'sishni boshladi 📈 — "
                "aynan hozir to'xtash — eng katta xato!\n\n"
                "🎁 <b>Biz sizning rivojlanishingizni xohlaymiz</b>, shuning uchun maxsus taklif:\n\n"
                "💎 1 oylik Premium\n"
                "<s>29,900</s> → <b>19,900 so'm</b> 🔥\n\n"
                "♾ Cheksiz tahlil, hook, ovoz — hammasi 1 oy davomida!\n\n"
                "⏰ Bu narx faqat 24 soat! Davom eting 👇",
                reply_markup=kb, parse_mode="HTML")
            _db_execute("UPDATE users SET renewal_eslatma_given = TRUE WHERE user_id = %s", (uid,))
            sent += 1
        except Exception:
            pass
        await asyncio.sleep(0.4)
    for aid in ADMIN_IDS:
        try:
            await context.bot.send_message(aid, f"🔄 Renewal eslatma: {sent} ta obunachiga yuborildi (ertaga tugaydi).")
        except Exception:
            pass


def marafon_progress(kun):
    """Progress bar emoji: kun (1-5) -> ▓▓▓▓░░░░░░ 40%"""
    foiz = int(kun / 5 * 100)
    tuldi = int(kun * 2)  # 5 kun = 10 blok
    bosh = "▓" * tuldi + "░" * (10 - tuldi)
    return f"{bosh} {foiz}%"


def marafon_kun_matni(kun):
    """Har kun uchun (matn, tugma_matni) qaytaradi."""
    progress = marafon_progress(kun)
    if kun == 1:
        return (
            "🎬✨ <b>REELS MARAFONI BOSHLANDI!</b> ✨🎬\n"
            "🏁 <b>1-KUN</b>\n\n"
            "👋 Salom, kelajakdagi TOP bloger! 🌟\n\n"
            "Rostini aytamanmi? 🤔 Hozircha siz — <b>boshlang'ich kreator</b>siz. "
            "Lekin bu yomon emas! 💪 Har bir TOP bloger shundan boshlagan. 🚀\n\n"
            "❗️ Bitta video sizni mashhur qilmaydi — algoritmga <b>TIZIM</b> kerak. 🧠\n\n"
            "🎯 Shuning uchun men siz uchun <b>5 KUNLIK MARAFON</b> tayyorladim:\n"
            "📆 Har kuni 1 ta video tahlil qilasiz\n"
            "📈 Har kuni bir pog'ona o'sasiz\n"
            "🎁 5-kuni — <b>PREMIUM sovg'a</b> + shaxsiy strategiya!\n\n"
            f"📊 {progress} (1/5)\n\n"
            "🔥 Keling, boshladik! Bugungi bepul tahlilingizni oling 👇\n"
            "⏰ <i>Diqqat: bepul tahlil FAQAT bugun ishlaydi, kechga kuyadi!</i>\n\n"
            "⚠️ <b>MUHIM:</b> PREMIUM sovg'ani olish uchun HAR 5 KUNI ham "
            "kirib bepul tahlilingizni olishingiz shart! Bir kun o'tkazib yuborsangiz — "
            "5-kun sovg'asi berilmaydi. 💪",
            "🎬 Bepul tahlilimni olish")
    if kun == 2:
        return (
            "🔥🔥 <b>2-KUN — ZO'R KETYAPSIZ!</b> 🔥🔥\n\n"
            "👏 Kecha birinchi qadamni tashladingiz — bu allaqачon g'alaba! 🎉\n\n"
            "🤝 Bugun sizni <b>InstaDoctor jamoasi</b> bilan tanishtirmoqchiman. "
            "Bu bot ortida jonli odamlar turibdi — biz sizning o'sishingizni chin dildan xohlaymiz! 💙\n\n"
            "🎥 Yuqoridagi videoni ko'ring — biz kimmiz, nega bu ishni qilyapmiz 👆\n\n"
            f"📊 {progress} (2/5)\n\n"
            "🎁 Videoni ko'rgach, bugungi bepul tahlilingizni oling! ⏰ <i>Faqat bugun!</i>",
            "🎬 Bugungi tahlilimni olish")
    if kun == 3:
        return (
            "💪✨ <b>3-KUN — YARIM YO'LDASIZ!</b> ✨💪\n\n"
            "🎊 Ko'pchilik shu yerda tashlab ketadi... lekin SIZ emas! 🦁 "
            "Siz haqiqiy kreator ekaningizni isbotlayapsiz! 🔥\n\n"
            "🎁 Bugun sizga maxsus sovg'a bor! Lekin bitta kichik shart: 👇\n\n"
            "📢 <b>Kanalimizga obuna bo'ling</b> — u yerda har kuni "
            "TOP bloglar sirlarini, viral hooklar va reels fokuslarini ulashamiz! 🤫🔥\n\n"
            "✅ Obuna bo'lgach — bugungi <b>bepul tahlilingiz</b> ochiladi! 🎬\n\n"
            f"📊 {progress} (3/5)",
            "📢 Kanalga obuna bo'ldim ✅")
    if kun == 4:
        return (
            "⚡️🚀 <b>4-KUN — DEYARLI TAYYOR!</b> 🚀⚡️\n\n"
            "🎉 4 kundan beri InstaDoctor bilan birgasiz — bu ajoyib! 💙\n\n"
            "🙏 Bugun sizdan bitta iltimos: <b>fikringizni yozing!</b>\n"
            "Bot sizga yordam berdimi? Nima yoqdi, nima yaxshilash kerak? 🤔\n\n"
            "✍️ Fikringizni yozganingizdan so'ng — bugungi bepul tahlilingizni olasiz!\n\n"
            f"📊 {progress} (4/5)\n\n"
            "💎 Ertaga esa — PREMIUM sovg'a kutmoqda! Pastdagi tugmani bosing 👇",
            "✍️ Fikr bildirish")
    # 5-kun
    return (
        "🎉🎊 <b>TABRIKLAYMIZ! MARAFON TUGADI!</b> 🎊🎉\n\n"
        "👑✨ Siz <b>\"Ilg'or Kreator\"</b> darajasiga yetdingiz! ✨👑\n\n"
        "▓▓▓▓▓▓▓▓▓▓ 💯% ✅\n\n"
        "🔥 5 kun davomida siz o'zingizni isbotladingiz — endi sovg'a vaqti! 🎁\n\n"
        "💎 Sizga <b>1 ta PREMIUM tahlil</b> sovg'a qilaman (bir martalik sinov):\n"
        "🎯 Hook soniyama-soniya\n"
        "🔊 Ovozli maslahat\n"
        "✍️ 3 ta tayyor hook!\n\n"
        "🎬 Pastdagi tugmani bosing va PREMIUM kuchini his qiling! 👇",
        "💎 PREMIUM tahlilimni olish")


async def marafon_boshla(uid, context):
    """Foydalanuvchini marafonga qo'shadi (1-kun) va 1-xabar yuboradi."""
    bugun = datetime.now().strftime("%Y-%m-%d")
    _db_execute(
        "UPDATE users SET marafon_kun = 1, marafon_start = %s, marafon_kunlik_sana = %s, "
        "marafon_tugadi = FALSE WHERE user_id = %s",
        (bugun, bugun, uid))
    add_balance(uid, 1)  # 1-kun bepul tahlil
    # 1-kun avtomatik "o'tgan" deb sanaladi (kirdi + bepul oldi)
    _db_execute("UPDATE users SET marafon_bajarilgan = 1, marafon_oxirgi_bajarilgan = %s WHERE user_id = %s",
                (bugun, uid))
    matn, tugma = marafon_kun_matni(1)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(tugma, callback_data="marafon_tahlil")]])
    try:
        await context.bot.send_message(uid, matn, reply_markup=kb, parse_mode="HTML")
    except Exception:
        pass


def marafon_kb(kun, tugma):
    """Marafon kuni uchun tugma(lar). 3-kun: kanalga o'tish + obuna bo'ldim (2 tugma)."""
    if kun == 3:
        kanal_url = f"https://t.me/{MARAFON_KANAL.lstrip('@')}"
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 Kanalga o'tish", url=kanal_url)],
            [InlineKeyboardButton("✅ Obuna bo'ldim", callback_data="marafon_kanal")],
        ])
    if kun == 5:
        return InlineKeyboardMarkup([[InlineKeyboardButton(tugma, callback_data="marafon_premium")]])
    if kun == 4:
        return InlineKeyboardMarkup([[InlineKeyboardButton(tugma, callback_data="marafon_fikr")]])
    return InlineKeyboardMarkup([[InlineKeyboardButton(tugma, callback_data="marafon_tahlil")]])


async def marafon_kunlik(context: ContextTypes.DEFAULT_TYPE):
    """Har kuni 11:00 - marafondagilarga keyingi kun xabari + yangi bepul tahlil.
    Kunlik bepul o'sha kuni ishlatilmasa 23:00 da kuyadi (marafon_kuydir)."""
    bugun = datetime.now().strftime("%Y-%m-%d")
    # Marafonda (kun 1-4), tugamagan, bugun hali kunlik olmagan
    rows = _db_execute(
        "SELECT user_id, marafon_kun FROM users "
        "WHERE marafon_kun >= 1 AND marafon_kun < 5 AND marafon_tugadi = FALSE "
        "AND (marafon_kunlik_sana IS NULL OR marafon_kunlik_sana <> %s)",
        (bugun,), fetch='all') or []
    for r in rows:
        uid, kun = r[0], r[1]
        yangi_kun = kun + 1
        # Yangi kunlik bepul (eski kuygan bo'lsa ham yangi beramiz)
        # 3-kun: bepul FAQAT kanalga obuna bosgach beriladi (hozir bermaymiz)
        if yangi_kun != 3:
            add_balance(uid, 1)
        _db_execute(
            "UPDATE users SET marafon_kun = %s, marafon_kunlik_sana = %s WHERE user_id = %s",
            (yangi_kun, bugun, uid))
        matn, tugma = marafon_kun_matni(yangi_kun)
        try:
            # 2-kun: jamoa videosini yuboramiz + keyin xabar
            if yangi_kun == 2:
                try:
                    await context.bot.send_video(uid, VIDEO_FILE_ID)
                except Exception:
                    pass
            kb = marafon_kb(yangi_kun, tugma)
            await context.bot.send_message(uid, matn, reply_markup=kb, parse_mode="HTML")
        except Exception:
            pass
        await asyncio.sleep(0.3)


async def avto_sotuv(context: ContextTypes.DEFAULT_TYPE):
    """Har kuni 12:00 - avtomatik sotuv (sotuv1, sotuv3, sotuv4).
    Yangi mos bo'lganlarga o'zi yuboradi. Spam himoyasi: kuniga max 150 ta."""
    if get_setting("avto_sotuv_aktiv", "off") != "on":
        return
    MAX_KUN = 150  # kuniga maksimal xabar (spam himoyasi)
    yuborildi_jami = 0
    hisobot = {"sotuv1": 0, "sotuv3": 0, "sotuv4": 0}

    async def _yubor(uid, msg_key, flag_col, tugma_matni, cb):
        nonlocal yuborildi_jami
        if yuborildi_jami >= MAX_KUN:
            return False
        if is_blocked(uid):
            return False
        # Marafonda bo'lgan (tugatmagan) odamlarga avto-sotuv YUBORMAYMIZ
        # (ular allaqachon marafon orqali bepul olyapti - chalkashmasin)
        _mar = _db_execute("SELECT marafon_kun, marafon_tugadi FROM users WHERE user_id = %s", (uid,), fetch='one')
        if _mar and _mar[0] and _mar[0] >= 1 and not _mar[1]:
            return False  # marafonda - o'tkazib yuboramiz
        try:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton(tugma_matni, callback_data=cb)]])
            await context.bot.send_message(uid, TEXTS['uz'][msg_key], reply_markup=kb, parse_mode="HTML")
            _db_execute(f"UPDATE users SET {flag_col} = TRUE WHERE user_id = %s", (uid,))
            yuborildi_jami += 1
            return True
        except Exception:
            return False

    # sotuv1 - 5+ ishlatgan, to'lamagan
    for uid in _kup_tahlil_uids(5, "sotuv1_given"):
        if await _yubor(uid, "sotuv1_msg", "sotuv1_given",
                        "💬 Fikr bildirish (va bepul tahlil oling!)", "sotuv1_fikr"):
            hisobot["sotuv1"] += 1
        await asyncio.sleep(0.4)
    # sotuv3 - 2-4 tahlil
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    rows = _db_execute(
        "SELECT u.user_id FROM users u "
        "JOIN (SELECT user_id FROM analyses WHERE kind='video' "
        "GROUP BY user_id HAVING COUNT(*) >= 2 AND COUNT(*) < 5) a ON a.user_id = u.user_id "
        "WHERE (u.sub_until IS NULL OR u.sub_until <= %s) "
        "AND (u.sotuv3_given IS NULL OR u.sotuv3_given = FALSE)", (now_str,), fetch='all') or []
    for r in rows:
        if is_admin(r[0]):
            continue
        if await _yubor(r[0], "sotuv3_msg", "sotuv3_given",
                        "🔥 Premium kuchini sinab ko'rish", "sotuv3_bepul"):
            hisobot["sotuv3"] += 1
        await asyncio.sleep(0.4)
    # sotuv4 - aynan 1 marta
    for uid in _aniq_tahlil_uids(1, "sotuv4_given"):
        if await _yubor(uid, "sotuv4_msg", "sotuv4_given",
                        "🎁 Bepul premium olish", "sotuv4_bepul"):
            hisobot["sotuv4"] += 1
        await asyncio.sleep(0.4)
    # Adminlarga qisqa xabar
    if yuborildi_jami > 0:
        for aid in ADMIN_IDS:
            try:
                await context.bot.send_message(
                    aid, f"🤖 Avto-sotuv: sotuv1={hisobot['sotuv1']}, "
                    f"sotuv3={hisobot['sotuv3']}, sotuv4={hisobot['sotuv4']} (jami {yuborildi_jami})")
            except Exception:
                pass


async def _hisobot_matni(davr):
    """Hisobot matnini tayyorlaydi. davr: 'kecha', 'bugun_yarim', 'bugun_yakun'."""
    now = datetime.now()
    bugun = now.strftime("%Y-%m-%d")
    kecha = (now - timedelta(days=1)).strftime("%Y-%m-%d")

    def cnt(q, p=None):
        r = _db_execute(q, p, fetch='one') if p else _db_execute(q, fetch='one')
        return (r[0] if r and r[0] is not None else 0)

    if davr == "kecha":
        yangi = cnt("SELECT COUNT(*) FROM users WHERE joined LIKE %s", (kecha + "%",))
        tahlil = cnt("SELECT COUNT(*) FROM analyses WHERE created LIKE %s", (kecha + "%",))
        sotuv = cnt("SELECT COUNT(*) FROM payments WHERE status='approved' AND created LIKE %s", (kecha + "%",))
        pul = cnt("SELECT COALESCE(SUM(amount),0) FROM payments WHERE status='approved' AND created LIKE %s", (kecha + "%",))
        marafon = cnt("SELECT COUNT(*) FROM users WHERE marafon_kun >= 1 AND marafon_tugadi = FALSE")
        issiq = cnt("SELECT COUNT(*) FROM (SELECT u.user_id FROM users u JOIN (SELECT user_id FROM analyses WHERE kind='video' GROUP BY user_id HAVING COUNT(*)>=5) a ON a.user_id=u.user_id LEFT JOIN payments p ON p.user_id=u.user_id AND p.status='approved' WHERE p.user_id IS NULL) t")
        return (f"🌅 <b>KECHAGI HISOBOT</b> ({kecha})\n━━━━━━━━━━━\n"
                f"👥 Yangi user: {yangi}\n🎬 Tahlil: {tahlil}\n"
                f"💰 Sotuv: {sotuv} ta ({pul:,} so'm)\n"
                f"🏃 Marafonda: {marafon}\n🔥 Issiq (5+ to'lamagan): {issiq}")
    if davr == "bugun_yarim":
        sotuv = cnt("SELECT COUNT(*) FROM payments WHERE status='approved' AND created LIKE %s", (bugun + "%",))
        pul = cnt("SELECT COALESCE(SUM(amount),0) FROM payments WHERE status='approved' AND created LIKE %s", (bugun + "%",))
        tahlil = cnt("SELECT COUNT(*) FROM analyses WHERE created LIKE %s", (bugun + "%",))
        yangi = cnt("SELECT COUNT(*) FROM users WHERE joined LIKE %s", (bugun + "%",))
        return (f"☀️ <b>BUGUN (hozircha)</b> ({bugun})\n━━━━━━━━━━━\n"
                f"👥 Yangi: {yangi}\n🎬 Tahlil: {tahlil}\n"
                f"💰 Sotuv: {sotuv} ta ({pul:,} so'm)")
    # bugun_yakun
    sotuv = cnt("SELECT COUNT(*) FROM payments WHERE status='approved' AND created LIKE %s", (bugun + "%",))
    pul = cnt("SELECT COALESCE(SUM(amount),0) FROM payments WHERE status='approved' AND created LIKE %s", (bugun + "%",))
    tahlil = cnt("SELECT COUNT(*) FROM analyses WHERE created LIKE %s", (bugun + "%",))
    yangi = cnt("SELECT COUNT(*) FROM users WHERE joined LIKE %s", (bugun + "%",))
    marafon = cnt("SELECT COUNT(*) FROM users WHERE marafon_kun >= 1 AND marafon_tugadi = FALSE")
    tugatgan = cnt("SELECT COUNT(*) FROM users WHERE marafon_tugadi = TRUE")
    xul = f"🌙 <b>BUGUN YAKUNI</b> ({bugun})\n━━━━━━━━━━━\n"
    xul += f"👥 Yangi: {yangi}\n🎬 Tahlil: {tahlil}\n💰 Sotuv: {sotuv} ta ({pul:,} so'm)\n"
    xul += f"🏃 Marafonda: {marafon} | 🏁 Tugatgan: {tugatgan}\n"
    if sotuv == 0:
        xul += "\n⚠️ Bugun sotuv yo'q — sotuv voronkasini tekshiring!"
    return xul


async def hisobot_ertalab(context: ContextTypes.DEFAULT_TYPE):
    if get_setting("hisobot_aktiv", "off") != "on":
        return
    matn = await _hisobot_matni("kecha")
    for aid in ADMIN_IDS:
        try:
            await context.bot.send_message(aid, matn, parse_mode="HTML")
        except Exception:
            pass


async def hisobot_tushlik(context: ContextTypes.DEFAULT_TYPE):
    if get_setting("hisobot_aktiv", "off") != "on":
        return
    matn = await _hisobot_matni("bugun_yarim")
    for aid in ADMIN_IDS:
        try:
            await context.bot.send_message(aid, matn, parse_mode="HTML")
        except Exception:
            pass


async def hisobot_kech(context: ContextTypes.DEFAULT_TYPE):
    if get_setting("hisobot_aktiv", "off") != "on":
        return
    matn = await _hisobot_matni("bugun_yakun")
    for aid in ADMIN_IDS:
        try:
            await context.bot.send_message(aid, matn, parse_mode="HTML")
        except Exception:
            pass


async def eslatma_yubor(context: ContextTypes.DEFAULT_TYPE):
    """Har soat tekshiradi: vaqti kelgan eslatmalarni yuboradi (Reels suratga olish)."""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = _db_execute(
        "SELECT id, user_id FROM eslatmalar "
        "WHERE COALESCE(yuborildi, FALSE) = FALSE AND eslatma_vaqt <= %s",
        (now_str,), fetch='all') or []
    for r in rows:
        eid, uid = r[0], r[1]
        if is_blocked(uid):
            _db_execute("UPDATE eslatmalar SET yuborildi = TRUE WHERE id = %s", (eid,))
            continue
        try:
            await context.bot.send_message(
                uid,
                "📅 <b>Salom! Eslatma vaqti keldi!</b> 🎬\n\n"
                "Siz Reels suratga olishni rejalashtirgandingiz — vaqt keldi! 🔥\n\n"
                "G'oyangizni hayotga tatbiq eting, natijasini InstaDoctor'da tekshiring! 🚀💎",
                parse_mode="HTML")
            _db_execute("UPDATE eslatmalar SET yuborildi = TRUE WHERE id = %s", (eid,))
        except Exception:
            _db_execute("UPDATE eslatmalar SET yuborildi = TRUE WHERE id = %s", (eid,))
        await asyncio.sleep(0.2)


async def marafon_6990_taklif(context: ContextTypes.DEFAULT_TYPE):
    """Har kuni tekshiradi: marafon 5/5 tugatgan, 19,900 taklifdan 48 soat o'tgan,
    obuna OLMAGAN, 6,990 hali olmagan -> 6,990 (7 kunlik) taklifi yuboradi."""
    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M")
    rows = _db_execute(
        "SELECT user_id, marafon_19900_sana FROM users "
        "WHERE marafon_tugadi = TRUE AND marafon_19900_sana IS NOT NULL "
        "AND COALESCE(marafon_6990_given, FALSE) = FALSE "
        "AND (sub_until IS NULL OR sub_until <= %s)",
        (now_str,), fetch='all') or []
    for r in rows:
        uid, sana = r[0], r[1]
        if is_admin(uid) or is_blocked(uid):
            continue
        # 48 soat o'tganmi?
        try:
            sana_dt = datetime.strptime(sana, "%Y-%m-%d %H:%M:%S")
        except Exception:
            continue
        if (now - sana_dt).total_seconds() < 48 * 3600:
            continue  # hali 48 soat o'tmagan
        try:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("⚡ 7 kunlik Premium — 6,990", callback_data="marafon_7kun")
            ]])
            await context.bot.send_message(
                uid,
                "🤔💭 1 oylik Premiumga hali shoshilmayapsiz — tushunaman! 🙂\n\n"
                "Balki katta qadamdan oldin sinab ko'rmoqchidirsiz? 👀\n"
                "To'g'ri fikr! 💡 Shuning uchun sizga eng arzon yo'l:\n\n"
                "⚡️🔥 <b>7 KUNLIK PREMIUM — atigi 6,990 so'm!</b> 🔥⚡️\n"
                "☕️ (Bir chashka kofe puliga — 1 hafta to'liq Premium!)\n\n"
                "✅ ♾ Cheksiz tahlil — kuniga xohlagancha! 🎬\n"
                "✅ 🎯 Hook soniyama-soniya + ovoz + 3 tayyor variant! 🔊\n"
                "✅ 🚀 Marafonchilar buni 3-5 barobar o'sish uchun ishlatmoqda! 📈\n"
                "✅ 🆓 Hech qanday majburiyat — yoqmasa to'xtatasiz! 👌\n\n"
                "💎 Premium'ni to'liq sinab ko'ring — keyin o'zingiz qaror qilasiz! 😎👇",
                reply_markup=kb, parse_mode="HTML")
            _db_execute("UPDATE users SET marafon_6990_given = TRUE WHERE user_id = %s", (uid,))
        except Exception:
            pass
        await asyncio.sleep(0.3)


async def marafon_eslatma(context: ContextTypes.DEFAULT_TYPE):
    """Har kuni 18:00 - marafondagilar bugungi bepulni HALI ishlatmagan bo'lsa,
    kuyishдан oldin 'disiplina' eslatmasi (yumshoq turtki)."""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    # Marafonda (tugamagan), bugungi bepul HALI turibdi (balance > 0, ishlatmagan)
    rows = _db_execute(
        "SELECT user_id FROM users "
        "WHERE marafon_kun >= 1 AND marafon_tugadi = FALSE "
        "AND COALESCE(balance,0) > 0 "
        "AND (sub_until IS NULL OR sub_until <= %s) "
        "AND COALESCE(premium_balance,0) = 0",
        (now_str,), fetch='all') or []
    for r in rows:
        uid = r[0]
        if is_admin(uid) or is_blocked(uid):
            continue
        try:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("🎬 Tahlilimni olish", callback_data="marafon_tahlil")
            ]])
            await context.bot.send_message(
                uid,
                "⏳ <b>Marafon kuningiz kuyib ketyapti!</b>\n\n"
                "O'zingizga so'z bergandingiz-ku — muntazam blog yuritib, o'sishga? 💪\n\n"
                "Bugungi bepul tahlilingiz yana bir necha soat faol (yarim tunda kuyadi). "
                "Istaган videoni yoki g'oyani yuboring — marafon progressingizni yo'qotmang! 🔥",
                reply_markup=kb, parse_mode="HTML")
        except Exception:
            pass
        await asyncio.sleep(0.3)


async def marafon_kuydir(context: ContextTypes.DEFAULT_TYPE):
    """Har kuni 23:00 - o'sha kuni ishlatilmagan kunlik bepul tahlilni kuydiradi.
    Marafondagilarning oddiy balansini 0 qiladi (marafon davom etadi)."""
    # Marafonda bo'lgan (kun 1-5), obunachi/premium bo'lmaganlarning oddiy balansi kuyadi
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    _db_execute(
        "UPDATE users SET balance = 0 "
        "WHERE marafon_kun >= 1 AND marafon_tugadi = FALSE "
        "AND (sub_until IS NULL OR sub_until <= %s) "
        "AND COALESCE(premium_balance,0) = 0",
        (now_str,))


async def drip_kunlik(context: ContextTypes.DEFAULT_TYPE):
    """Har kuni: yangi kirgan (deploy'dan keyin) foydalanuvchilarni izchil isitadi.
    1-kun: bepul (mavjud). 2-kun: jamoa videosi + empatiya +1 bepul. 3-kun: 7 kunlik aksiya.
    FAQAT yangi kirganlarga (drip_start sanadan keyin) - eski 10000 ga emas.
    MUHIM: marafon yoqilgan bo'lsa - drip ISHLAMAYDI (ustma-ust tushmasin)."""
    if get_setting("marafon_aktiv", "off") == "on":
        return  # Marafon yoqilgan - drip kerak emas
    drip_start = get_setting("drip_start", "")
    if not drip_start:
        return  # drip hali yoqilmagan
    try:
        drip_start_dt = datetime.strptime(drip_start, "%Y-%m-%d")
    except Exception:
        return
    bugun = datetime.now()
    rows = _db_execute(
        "SELECT user_id, joined, COALESCE(drip2_given,FALSE), COALESCE(drip3_given,FALSE) "
        "FROM users", fetch='all') or []
    drip2_sent, drip3_sent = 0, 0
    for r in rows:
        uid, joined, d2, d3 = r[0], r[1], r[2], r[3]
        lang = 'uz'  # lang DB'da yo'q - default uz
        if is_admin(uid) or sub_active(uid):
            continue
        if not joined:
            continue
        try:
            joined_dt = datetime.strptime(joined[:10], "%Y-%m-%d")
        except Exception:
            continue
        # FAQAT yangi (drip_start dan keyin kirgan) - eski 10000 ga emas
        if joined_dt < drip_start_dt:
            continue
        kun = (bugun.date() - joined_dt.date()).days
        # 2-KUN: jamoa videosi + empatiya + 1 bepul
        if kun == 1 and not d2:
            try:
                add_balance(uid, 1)
                _db_execute("UPDATE users SET drip2_given=TRUE WHERE user_id=%s", (uid,))
                await context.bot.send_message(uid, TEXTS['uz']['drip2_empatiya'], parse_mode="HTML")
                await asyncio.sleep(0.3)
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton(TEXTS['uz']['video_2btn_sovga'], callback_data="video_sovga_ol")],
                    [InlineKeyboardButton(TEXTS['uz']['video_2btn_fikr'], callback_data="video_fikr")],
                ])
                await context.bot.send_video(uid, VIDEO_FILE_ID,
                                             caption=TEXTS['uz']['video_caption'],
                                             reply_markup=kb, parse_mode="HTML")
                drip2_sent += 1
            except Exception as e:
                logger.warning(f"drip2 xato (uid={uid}): {e}")
            await asyncio.sleep(0.4)
        # 3-KUN: 7 kunlik aksiya (24 soat taymer)
        elif kun == 2 and not d3:
            try:
                _db_execute("UPDATE users SET drip3_given=TRUE, tarif7_sana=%s WHERE user_id=%s",
                            (bugun.strftime("%Y-%m-%d %H:%M"), uid))
                payme_link = _payme_checkout_link(uid, TEST_PRICE)
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton(TEXTS['uz']['tarif7_2soat_btn'], url=payme_link)],
                    [InlineKeyboardButton(TEXTS['uz']['tarif7_btn_card'], callback_data='card_test')],
                ])
                await context.bot.send_message(uid, TEXTS['uz']['test_taklif_msg'],
                                               reply_markup=kb, parse_mode="HTML")
                drip3_sent += 1
            except Exception as e:
                logger.warning(f"drip3 xato (uid={uid}): {e}")
            await asyncio.sleep(0.4)
    logger.info(f"Drip kunlik: 2-kun {drip2_sent} ta, 3-kun {drip3_sent} ta")
    if drip2_sent or drip3_sent:
        for aid in ADMIN_IDS:
            try:
                await context.bot.send_message(
                    aid, f"🔄 Drip: 2-kun (video) {drip2_sent} ta, 3-kun (aksiya) {drip3_sent} ta yuborildi.")
            except Exception:
                pass


async def juma_aksiya_boshla(context: ContextTypes.DEFAULT_TYPE):
    """Juma ertalab: premium olmaganlarga 1 juma-bepul beradi + xabar."""
    # Juma aksiyasi YOQILGANmi? (default: o'chirilgan)
    if get_setting("juma_aksiya", "off") != "on":
        logger.info("Juma aksiyasi o'chirilgan - o'tkazib yuborildi")
        return
    bugun = datetime.now().strftime("%Y-%m-%d")
    # Maslahatni hafta raqamiga qarab aylantiramiz (har juma boshqasi)
    hafta_raqam = datetime.now().isocalendar()[1]
    maslahat = JUMA_MASLAHATLAR[hafta_raqam % len(JUMA_MASLAHATLAR)]
    xabar = TEXTS['uz']['juma_boshlandi'].format(maslahat=maslahat)
    rows = _db_execute("SELECT user_id FROM users", fetch='all') or []
    targets = [r[0] for r in rows if not is_admin(r[0]) and not sub_active(r[0])]
    logger.info(f"Juma aksiyasi: {len(targets)} ta foydalanuvchiga beriladi")
    sent = 0
    for uid in targets:
        try:
            _db_execute("UPDATE users SET juma_balance = 1, juma_sana = %s WHERE user_id = %s",
                        (bugun, uid))
            await context.bot.send_message(uid, xabar, parse_mode="HTML")
            sent += 1
        except Exception as e:
            logger.warning(f"Juma aksiya xato (uid={uid}): {e}")
        await asyncio.sleep(0.4)
    logger.info(f"Juma aksiyasi tugadi: {sent} ta yuborildi")
    for aid in ADMIN_IDS:
        try:
            await context.bot.send_message(aid, f"🎁 Juma aksiyasi: {sent} ta foydalanuvchiga berildi.")
        except Exception:
            pass


async def juma_eslatma_yubor(context: ContextTypes.DEFAULT_TYPE):
    """Juma kechqurun: ishlatmaganlarga 'yo'qoladi' eslatmasi."""
    if get_setting("juma_aksiya", "off") != "on":
        return
    bugun = datetime.now().strftime("%Y-%m-%d")
    rows = _db_execute(
        "SELECT user_id FROM users WHERE juma_balance > 0 AND juma_sana = %s",
        (bugun,), fetch='all') or []
    sent = 0
    for r in rows:
        uid = r[0]
        if is_admin(uid) or sub_active(uid):
            continue
        try:
            await context.bot.send_message(uid, TEXTS['uz']['juma_eslatma'], parse_mode="HTML")
            sent += 1
        except Exception:
            pass
        await asyncio.sleep(0.4)
    logger.info(f"Juma eslatma: {sent} ta yuborildi")


async def juma_kuydir(context: ContextTypes.DEFAULT_TYPE):
    """Shanba: ishlatilmagan juma-balansni kuydiradi (0 qiladi)."""
    _db_execute("UPDATE users SET juma_balance = 0 WHERE juma_balance > 0")
    logger.info("Juma balanslar kuydirildi (shanba)")


def main():
    init_db()
    global _bot_app
    app = (Application.builder()
           .token(TELEGRAM_TOKEN)
           .concurrent_updates(True)   # MUHIM: har bir foydalanuvchi alohida ishlanadi
           .post_init(post_init)
           .post_shutdown(post_shutdown)
           .build())
    _bot_app = app
    # ===== JUMA AKSIYASI (avtomatik scheduler) =====
    # Vaqt: O'zbekiston (UTC+5). Server UTC bo'lsa, UZ vaqtidan 5 soat ayiramiz.
    # Juma 10:00 UZ = 05:00 UTC | Juma 20:00 UZ = 15:00 UTC | Shanba 00:30 UZ = Juma 19:30 UTC
    try:
        from datetime import time as _dtime
        jq = app.job_queue
        if jq is not None:
            UZ_OFF = int(os.getenv("UZ_TZ_OFFSET", "5"))
            def uz_to_utc(h):
                return (h - UZ_OFF) % 24
            # Juma = day 4 (PTB: Monday=0 ... Friday=4)
            jq.run_daily(juma_aksiya_boshla, time=_dtime(hour=uz_to_utc(10), minute=0), days=(4,))
            jq.run_daily(juma_eslatma_yubor, time=_dtime(hour=uz_to_utc(20), minute=0), days=(4,))
            # Kuydirish: shanba 00:30 UZ = juma 19:30 UTC (agar UZ_OFF=5)
            jq.run_daily(juma_kuydir, time=_dtime(hour=uz_to_utc(0), minute=30), days=(5,))
            # Drip (3 kunlik isitish) - har kuni 19:00 UZ (aktiv payt)
            jq.run_daily(drip_kunlik, time=_dtime(hour=uz_to_utc(19), minute=0))
            # Obuna tugash eslatmasi (renewal) - har kuni 11:00 UZ
            jq.run_daily(obuna_tugash_eslatma, time=_dtime(hour=uz_to_utc(11), minute=0))
            # Obuna tugadi -> adminga xabar (har kuni 11:05 UZ)
            jq.run_daily(obuna_tugadi_xabar, time=_dtime(hour=uz_to_utc(11), minute=5))
            # Marafon: har kuni 11:00 kunlik xabar+bepul, 23:00 kuydir
            jq.run_daily(marafon_kunlik, time=_dtime(hour=uz_to_utc(11), minute=0))
            # Marafon eslatma (18:00) - ishlatmaganlarga kuyishдан oldin
            jq.run_daily(marafon_eslatma, time=_dtime(hour=uz_to_utc(18), minute=0))
            # Marafon 6,990 taklif (5/5 tugatib 19,900 olmaganlarga 48 soat keyin) - kuniga 12:30
            jq.run_daily(marafon_6990_taklif, time=_dtime(hour=uz_to_utc(12), minute=30))
            # Eslatmalar (Reels suratga olish) - har soat tekshiradi
            jq.run_repeating(eslatma_yubor, interval=3600, first=120)
            jq.run_daily(marafon_kuydir, time=_dtime(hour=uz_to_utc(23), minute=59))
            # Avto-sotuv (12:00) + 3 mahal hisobot (09:00, 14:00, 21:00)
            jq.run_daily(avto_sotuv, time=_dtime(hour=uz_to_utc(12), minute=0))
            jq.run_daily(hisobot_ertalab, time=_dtime(hour=uz_to_utc(9), minute=0))
            jq.run_daily(hisobot_tushlik, time=_dtime(hour=uz_to_utc(14), minute=0))
            jq.run_daily(hisobot_kech, time=_dtime(hour=uz_to_utc(21), minute=0))
            logger.info("Juma + Drip scheduler o'rnatildi")
        else:
            logger.warning("job_queue yo'q - juma aksiyasi ishlamaydi (requirements: job-queue kerak)")
    except Exception as e:
        logger.error(f"Juma scheduler o'rnatishda xato: {e}")
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("til", til_command))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("kim", kim_command))
    app.add_handler(CommandHandler("berobuna", berobuna_command))
    app.add_handler(CommandHandler("berbalans", berbalans_command))
    app.add_handler(CommandHandler("berbalans_premium", berbalans_premium_command))
    app.add_handler(CommandHandler("balans_0", balans_0_command))
    app.add_handler(CommandHandler("kop_balans", kop_balans_command))
    app.add_handler(CommandHandler("sotuv_balans_0", sotuv_balans_0_command))
    app.add_handler(CommandHandler("blok", blok_command))
    app.add_handler(CommandHandler("blok_ochir", blok_ochir_command))
    app.add_handler(CommandHandler("yoz", yoz_command))
    app.add_handler(CommandHandler("aksiya", aksiya_command))
    app.add_handler(CommandHandler("aksiya_tugadi", aksiya_tugadi_command))
    app.add_handler(CommandHandler("obunachilar", obunachilar_command))
    app.add_handler(CommandHandler("premium_xarajat", premium_xarajat_command))
    app.add_handler(CommandHandler("sorov", sorov_command))
    app.add_handler(CommandHandler("test_sorov", test_sorov_command))
    app.add_handler(CommandHandler("eslatma", eslatma_command))
    app.add_handler(CommandHandler("yangilik", yangilik_command))
    app.add_handler(CommandHandler("yangilik_test", yangilik_test_command))
    app.add_handler(CommandHandler("voronka", voronka_command))
    app.add_handler(CommandHandler("tahlil_faol", tahlil_faol_command))
    app.add_handler(CommandHandler("paymetest", paymetest_command))
    app.add_handler(CommandHandler("tarif7", tarif7_command))
    app.add_handler(CommandHandler("buyruqlar", buyruqlar_command))
    app.add_handler(CommandHandler("xatolar", xatolar_command))
    app.add_handler(CommandHandler("aksiya_off", aksiya_off_command))
    app.add_handler(CommandHandler("aksiya_on", aksiya_on_command))
    app.add_handler(CommandHandler("aksiya_ber", aksiya_ber_command))
    app.add_handler(CommandHandler("drip_on", drip_on_command))
    app.add_handler(CommandHandler("drip_off", drip_off_command))
    app.add_handler(CommandHandler("drip_holat", drip_holat_command))
    app.add_handler(CommandHandler("videoid", videoid_command))
    app.add_handler(CommandHandler("video_test", video_test_command))
    app.add_handler(CommandHandler("video_xabar", video_xabar_command))
    app.add_handler(CommandHandler("juma_ochir", juma_ochir_command))
    app.add_handler(CommandHandler("juma_yoq", juma_yoq_command))
    app.add_handler(CommandHandler("javoblar", javoblar_command))
    app.add_handler(CommandHandler("javoblar_bugun", javoblar_bugun_command))
    app.add_handler(CommandHandler("avto_aksiya_yoq", avto_aksiya_yoq_command))
    app.add_handler(CommandHandler("avto_aksiya_ochir", avto_aksiya_ochir_command))
    app.add_handler(CommandHandler("obuna_taklif", obuna_taklif_command))
    app.add_handler(CommandHandler("test_taklif", test_taklif_command))
    app.add_handler(CommandHandler("chegirma", chegirma_command))
    app.add_handler(CommandHandler("maxsus_2700", maxsus_2700_command))
    app.add_handler(CommandHandler("sotuv_test", sotuv_test_command))
    app.add_handler(CommandHandler("sotuv_natija", sotuv_natija_command))
    app.add_handler(CommandHandler("tolovchilar", tolovchilar_command))
    app.add_handler(CommandHandler("renewal_hozir", renewal_hozir_command))
    app.add_handler(CommandHandler("marafon_on", marafon_on_command))
    app.add_handler(CommandHandler("marafon_off", marafon_off_command))
    app.add_handler(CommandHandler("marafon_holat", marafon_holat_command))
    app.add_handler(CommandHandler("profile", profile_command))
    app.add_handler(CommandHandler("yangilik_xabar", yangilik_xabar_command))
    app.add_handler(CommandHandler("marafon_eski_tiq", marafon_eski_tiq_command))
    app.add_handler(CommandHandler("chegirma_test", chegirma_test_command))
    app.add_handler(CommandHandler("marafon_kim", marafon_kim_command))
    app.add_handler(CommandHandler("sodiq", sodiq_command))
    app.add_handler(CommandHandler("marafon_tuzat", marafon_tuzat_command))
    app.add_handler(CommandHandler("marafon_test", marafon_test_command))
    app.add_handler(CommandHandler("avto_on", avto_on_command))
    app.add_handler(CommandHandler("avto_off", avto_off_command))
    app.add_handler(CommandHandler("avto_test", avto_test_command))
    app.add_handler(CommandHandler("avto_sotuv_hozir", avto_sotuv_hozir_command))
    app.add_handler(CommandHandler("renewal_royxat", renewal_royxat_command))
    app.add_handler(CommandHandler("test_tugaganlar", test_tugaganlar_command))
    app.add_handler(CommandHandler("sotuv_reset", sotuv_reset_command))
    app.add_handler(CommandHandler("bepul_royxat", bepul_royxat_command))
    app.add_handler(CommandHandler("sotuv1", sotuv1_command))
    app.add_handler(CommandHandler("sotuv1b", sotuv1b_command))
    app.add_handler(CommandHandler("sotuv3", sotuv3_command))
    app.add_handler(CommandHandler("sotuv4", sotuv4_command))
    app.add_handler(CommandHandler("sotuv5", sotuv5_command))
    app.add_handler(CommandHandler("premium_fikr", premium_fikr_command))
    app.add_handler(CommandHandler("premium_fikr_reset", premium_fikr_reset_command))
    app.add_handler(CommandHandler("sotuv_matn", sotuv_matn_command))
    app.add_handler(CommandHandler("sotuv_korish", sotuv_korish_command))
    app.add_handler(CommandHandler("sotuv_matn_tikla", sotuv_matn_tikla_command))
    app.add_handler(CommandHandler("chegirma_ochir", chegirma_ochir_command))
    app.add_handler(CommandHandler("top", top_command))
    app.add_handler(CommandHandler("bugun", bugun_command))
    app.add_handler(CommandHandler("aktiv", aktiv_command))
    app.add_handler(CommandHandler("fireworks_test", fireworks_test_command))
    app.add_handler(CommandHandler("obunaochir", obunaochir_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(PreCheckoutQueryHandler(precheckout_handler))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, video_handler))
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    app.add_handler(MessageHandler(filters.Sticker.ALL, sticker_id_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    logger.info("Bot ishga tushdi!")
    # Telegram bot + Payme web server BIRGA ishlaydi
    async def _run_all():
        global _main_loop
        _main_loop = asyncio.get_running_loop()
        # MUHIM: asyncio.to_thread default 40 ta thread ishlatadi. Video upload
        # (_upload_and_wait) thread'ni uzoq band qiladi (PROCESSING kutish). Ko'p
        # video kelsa 40 thread to'lib, bot QOTADI ("bir necha upload'dan keyin"
        # muammosi). Shuning uchun thread pool'ni kattalashtiramiz.
        try:
            import concurrent.futures
            _executor = concurrent.futures.ThreadPoolExecutor(max_workers=256)
            _main_loop.set_default_executor(_executor)
            logger.info("Thread pool 256 ga oshirildi (ko'p video bir vaqtda)")
        except Exception as e:
            logger.warning(f"Thread pool sozlashda xato: {e}")
        await run_web_server()  # Payme web server (port 8080)
        async with app:
            await post_init(app)
            await app.start()
            await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
            await asyncio.Event().wait()
    asyncio.run(_run_all())


if __name__ == '__main__':
    main()
