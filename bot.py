import os
import re
import io
import wave
import uuid
import asyncio
import logging
import tempfile
import time
import psycopg
from psycopg_pool import ConnectionPool
from datetime import datetime, timedelta
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
# Bir vaqtda nechta video tahlil qilinishi mumkin (qolganlar navbatda kutadi).
# Pullik Gemini (Tier 2) + kuchli server. Bitta umumiy navbat (hammaga).
# Railway Variables'dan MAX_CONCURRENT ni xohlagancha oshirish mumkin.
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT", "100") or "100")
# Navbat mexanizmi: bir vaqtda MAX_CONCURRENT ta tahlil ishlaydi (hammaga bitta pool).
_video_semaphore = asyncio.Semaphore(MAX_CONCURRENT)
ADMIN_ID = 7589459697
# Barcha adminlar (cheksiz tahlil, /top, tannarx hisoboti va h.k.)
ADMIN_IDS = [7589459697, 5808245573, 356530813]
# So'rov javoblari (fikrlar) yuboriladigan guruh ID (Railway'dan ham o'zgartirish mumkin)
FIKR_GROUP_ID = os.getenv("FIKR_GROUP_ID", "-1003784847158")


def is_admin(user_id):
    return user_id in ADMIN_IDS
CARD_NUMBER = "6262 7300 6521 3151"
CARD_NAME = "Boqijonov Nurislom"

# Obuna (1 oylik): narx (so'm) va kun
SUB_PRICE = 29900
SUB_DAYS = 30
# 1 martalik tahlil narxi (so'm)
ONE_PRICE = 5090
# Haftalik test paketi: 7 kun / 7000 so'm
TEST_PRICE = 7000
TEST_DAYS = 7

# Yangi foydalanuvchiga beriladigan BEPUL tahlil soni
FREE_TRIAL = 1

# Payme (Telegram Payments) provider token - Railway Variables'dan olinadi
PROVIDER_TOKEN = os.getenv("PROVIDER_TOKEN", "")

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
                cur.execute("""CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY, value TEXT)""")
                cur.execute("""CREATE TABLE IF NOT EXISTS sorov_javoblar (
                    id SERIAL PRIMARY KEY, user_id BIGINT, username TEXT,
                    javob1 TEXT, javob2 TEXT, created TEXT)""")
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


def sub_active(user_id):
    """Obuna faolmi? (sub_until hozirgi vaqtdan keyinmi)"""
    if is_admin(user_id):
        return True
    row = _db_execute("SELECT sub_until FROM users WHERE user_id = %s", (user_id,), fetch='one')
    if not row or not row[0]:
        return False
    try:
        return datetime.strptime(row[0], "%Y-%m-%d %H:%M") > datetime.now()
    except Exception:
        return False


def sub_until_str(user_id):
    row = _db_execute("SELECT sub_until FROM users WHERE user_id = %s", (user_id,), fetch='one')
    return row[0] if row and row[0] else None


def has_access(user_id):
    """'admin' | 'sub' (obuna faol) | 'credit' (bepul tahlil bor) | 'none'"""
    if is_admin(user_id):
        return 'admin'
    if sub_active(user_id):
        return 'sub'
    if get_balance(user_id) > 0:
        return 'credit'
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
    _db_execute("UPDATE users SET sub_until = %s WHERE user_id = %s", (new_until, user_id))
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


def auto_aksiya_on():
    """Avtomatik +2 aksiya yoqilganmi? (default: o'chiq)"""
    return get_setting("auto_aksiya", "off") == "on"


SUB_PRICE_DISCOUNT = 19900  # Chegirma narxi

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
    """Obunaning joriy narxi (chegirma faol bo'lsa - chegirma narxi)."""
    return SUB_PRICE_DISCOUNT if discount_active() else SUB_PRICE


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
            "🩺 INSTADOKTOR\n\n"
            "🎬 Men videoyingizni Instagram algoritmlari bo'yicha 100% to'liq analiz qilib, "
            "sizga kamchiliklarini 📊 va rekga chiqish ehtimolini 📈 o'lchab, maslahatlar beraman! 💡\n\n"
            "📤 Videongizni yuboring (2GB gacha) 👇"
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
        'test_taklif_msg': ("Hali to'liq obunaga shoshilmayapsizmi? Tushunamiz! 😊\n"
                            "Avval <b>kichik qadamdan</b> boshlang:\n\n"
                            "⚡️ <b>7 KUNLIK PREMIUM — atigi 7 000 so'm</b>\n"
                            "━━━━━━━━━━━━━\n"
                            "🚀 <b>VIP TEZLIK</b> — navbatsiz tahlil\n"
                            "🔍 <b>CHUQURROQ TAHLIL</b> — eng aniq baho\n"
                            "♾ <b>CHEKSIZ video</b> — 7 kun limitsiz\n"
                            "🎙 <b>OVOZLI MASLAHAT</b> + kuchli xeshteglar\n"
                            "📈 <b>REK EHTIMOLI</b> — TOPga chiqish % larda\n"
                            "━━━━━━━━━━━━━\n"
                            "Bir hafta sinab ko'ring — yoqsa, to'liq obunaga o'tasiz! 🔥"),
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
                           "💡 <b>7 KUNLIK TEST PREMIUM</b> ni atigi <b>7 000 so'mga</b> qo'shmoqchimiz!\n\n"
                           "Premium imkoniyatlari:\n"
                           "🔍 <b>2 barobar kuchli tahlil</b>\n"
                           "♾ <b>Cheksiz tahlil</b> imkoniyati\n"
                           "🎙 <b>Ovozli maslahatlar</b>\n"
                           "🔥 <b>Eng kuchli xeshteglar</b>\n"
                           "📈 <b>Yashirin REK ehtimoli</b>\n\n"
                           "2 ta savolga javob bering 👇"),
        'test_sorov_btn': "✍️ Fikr bildirish",
        'test_sorov_q1': ("1️⃣ <b>7 kunlik test Premium (atigi 7 000 so'm) qo'shmoqchimiz</b> — "
                          "sizga qiziqmi, sinab ko'rarmidingiz?\n\nFikringizni yozing 👇"),
        'test_sorov_q2': ("Rahmat! 🙏 Endi 2-savol:\n\n"
                          "2️⃣ Yaqinda botni yangiladik — <b>tezlik va sifatni oshirdik</b>. "
                          "O'zgarishlar yoqdimi? 👇"),
        'test_sorov_done': "✅ Rahmat! Fikringiz biz uchun juda muhim 🙏",
        'sorov_thanks_reward': "Katta rahmat fikringiz uchun! ❤️🎁 Balansingizga +1 ta BEPUL tahlil qo'shdik. Video yuboring! 🎬",
        'sorov_thanks': "Katta rahmat fikringiz uchun! ❤️🙏",
        'menu_balance': "💰 Balansim",
        'menu_lang': "🌐 Til",
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
        'sotuv_msg': ("Bilasizmi, nega ba'zi bloggerlar doimo TOPda? 🤔\n\n"
                      "Chunki ular har bir videoni joylashdan oldin kamchiliklarini to'g'rilashadi. "
                      "Lekin algoritmlar to'xtab turmaydi — har kuni tahlil qilish va trendda bo'lish kerak! 📊\n\n"
                      "💎 PREMIUM tarifda nimalarga ega bo'lasiz?\n\n"
                      "⚡ <b>Maksimal tezlik</b> — navbatsiz, soniyalarda tahlil\n"
                      "♾ <b>Cheksiz tahlil</b> — kuniga xohlagancha video\n"
                      "🗣 <b>Audio</b> — tahlilni ovozli eshitish\n"
                      "📈 <b>Yashirin trendlar</b> — algoritm yangiliklari birinchi sizga\n\n"
                      "🔥 FAQAT BUGUN — MAXSUS CHEGIRMA!\n"
                      "Hozirgi narx: <s>29 900 so'm</s>\n"
                      "Faqat bugun: <b>19 900 so'm/oy</b> 🎉\n"
                      "(Kuniga atigi 650 so'm! ☕️ bir choydan ham arzon)\n\n"
                      "⏳ Shoshiling — bu narx faqat BUGUN SOAT 17:00 gacha! Keyin yana ko'tariladi.\n\n"
                      "Bitta REKka chiqqan video bu pulni qoplaydi! 🚀\n\n"
                      "👇 Hoziroq faollashtiring — imkoniyatni boy bermang!"),
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
        'tts_btn': "🔊 Qisqa eshitish",
        'tts_full_btn': "🔊 To'liq eshitish",
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
        'no_balance': ("💳 Sizda bepul tahlil qolmagan.\n\n"
                       "Davom etish uchun tanlang 👇\n"
                       "• 1 oylik obuna — cheksiz tahlil (29,900 so'm)\n"
                       "• 1 ta tahlil — 5,090 so'm"),
        'balance_info': "💰 Sizda {n} ta bepul tahlil bor.",
        'choose_pkg': "💳 Obuna:",
        'pay_instr': ("💳 1 OYLIK OBUNA — cheksiz video tahlil (30 kun)\n\n"
                      "💰 Summa: {amount:,} so'm\n"
                      "💳 Karta: {card}\n"
                      "👤 Ega: {name}\n\n"
                      "✅ To'lagach, CHEK SKRINSHOTINI shu yerga yuboring.\n"
                      "Admin tekshirib, obunangizni faollashtiradi. ⏳"),
        'receipt_sent': "✅ Chekingiz adminga yuborildi. Tez orada tasdiqlanadi! ⏳",
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
            "🩺 INSTADOKTOR\n\n"
            "🎬 Я анализирую ваше видео по алгоритмам Instagram на 100%, "
            "указываю недостатки 📊 и измеряю вероятность попадания в рекомендации 📈, даю советы! 💡\n\n"
            "📤 Отправьте ваше видео (до 2ГБ) 👇"
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
        'test_taklif_msg': ("Ещё не готовы к полной подписке? Понимаем! 😊\n"
                            "Начните с <b>малого шага</b>:\n\n"
                            "⚡️ <b>7 ДНЕЙ PREMIUM — всего 7 000 сум</b>\n"
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
                           "💡 <b>7-ДНЕВНЫЙ ТЕСТ PREMIUM</b> хотим добавить всего за <b>7 000 сум</b>!\n\n"
                           "Возможности Premium:\n"
                           "🔍 <b>В 2 раза мощнее анализ</b>\n"
                           "♾ <b>Безлимитный анализ</b>\n"
                           "🎙 <b>Аудио-советы</b>\n"
                           "🔥 <b>Самые сильные хештеги</b>\n"
                           "📈 <b>Скрытая вероятность РЕК</b>\n\n"
                           "Ответьте на 2 вопроса 👇"),
        'test_sorov_btn': "✍️ Оставить отзыв",
        'test_sorov_q1': ("1️⃣ <b>Хотим добавить 7-дневный тест Premium (всего 7 000 сум)</b> — "
                          "вам интересно, попробовали бы?\n\nНапишите ваше мнение 👇"),
        'test_sorov_q2': ("Спасибо! 🙏 Теперь 2-й вопрос:\n\n"
                          "2️⃣ Недавно мы обновили бота — <b>повысили скорость и качество</b>. "
                          "Понравились изменения? 👇"),
        'test_sorov_done': "✅ Спасибо! Мы очень ценим ваше мнение 🙏",
        'sorov_thanks_reward': "Большое спасибо за отзыв! ❤️🎁 Мы добавили +1 БЕСПЛАТНЫЙ анализ. Отправьте видео! 🎬",
        'sorov_thanks': "Большое спасибо за отзыв! ❤️🙏",
        'menu_balance': "💰 Мой баланс",
        'menu_lang': "🌐 Язык",
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
                      "⏳ Торопитесь — цена только СЕГОДНЯ ДО 17:00! Потом снова поднимется.\n\n"
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
        'tts_btn': "🔊 Кратко голосом",
        'tts_full_btn': "🔊 Полностью голосом",
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
        'no_balance': ("💳 У вас не осталось бесплатных анализов.\n\n"
                       "Выберите, чтобы продолжить 👇\n"
                       "• Подписка на 1 месяц — безлимит (29 900 сум)\n"
                       "• 1 анализ — 5 090 сум"),
        'balance_info': "💰 У вас {n} бесплатных анализов.",
        'choose_pkg': "💳 Подписка:",
        'pay_instr': ("💳 ПОДПИСКА НА 1 МЕСЯЦ — безлимитный анализ видео (30 дней)\n\n"
                      "💰 Сумма: {amount:,} сум\n"
                      "💳 Карта: {card}\n"
                      "👤 Владелец: {name}\n\n"
                      "✅ После оплаты отправьте СКРИНШОТ ЧЕКА сюда.\n"
                      "Админ проверит и активирует подписку. ⏳"),
        'receipt_sent': "✅ Ваш чек отправлен админу. Скоро подтвердим! ⏳",
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


def main_keyboard(context):
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(t(context, 'menu_video')), KeyboardButton(t(context, 'menu_profile'))],
            [KeyboardButton(t(context, 'menu_balance')), KeyboardButton(t(context, 'menu_premium'))],
            [KeyboardButton(t(context, 'menu_ref')), KeyboardButton(t(context, 'menu_lang'))],
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
    await message.reply_text(t(context, 'welcome'), reply_markup=main_keyboard(context))
    # Yangi foydalanuvchiga e'tiborli SOVG'A xabari
    if context.user_data.get('is_new'):
        context.user_data['is_new'] = False
        await message.reply_text(t(context, 'gift_new'))


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
    elif data == 'buy_sub':
        if not PROVIDER_TOKEN:
            await query.message.reply_text(t(context, 'pay_unavailable'))
            return
        try:
            await query.message.reply_text(t(context, 'pay_safety'))
            await context.bot.send_invoice(
                chat_id=query.from_user.id,
                title=t(context, 'inv_sub_title'),
                description=t(context, 'inv_sub_desc'),
                payload="sub_1month",
                provider_token=PROVIDER_TOKEN,
                currency="UZS",
                prices=[LabeledPrice(t(context, 'inv_sub_title'), current_sub_price() * 100)],
            )
        except Exception as e:
            logger.error(f"Invoice (sub) yuborishda xato: {e}")
            await query.message.reply_text(t(context, 'pay_unavailable'))
    elif data == 'buy_test':
        if not PROVIDER_TOKEN:
            await query.message.reply_text(t(context, 'pay_unavailable'))
            return
        try:
            await query.message.reply_text(t(context, 'pay_safety'))
            await context.bot.send_invoice(
                chat_id=query.from_user.id,
                title="7 kunlik Premium",
                description="7 kun davomida to'liq Premium: cheksiz tahlil, VIP tezlik, ovozli maslahatlar.",
                payload="test_7day",
                provider_token=PROVIDER_TOKEN,
                currency="UZS",
                prices=[LabeledPrice("7 kunlik Premium", TEST_PRICE * 100)],
            )
        except Exception as e:
            logger.error(f"Invoice (test) yuborishda xato: {e}")
            await query.message.reply_text(t(context, 'pay_unavailable'))
    elif data == 'buy_one':
        if not PROVIDER_TOKEN:
            await query.message.reply_text(t(context, 'pay_unavailable'))
            return
        try:
            await query.message.reply_text(t(context, 'pay_safety'))
            await context.bot.send_invoice(
                chat_id=query.from_user.id,
                title=t(context, 'inv_one_title'),
                description=t(context, 'inv_one_desc'),
                payload="one_1",
                provider_token=PROVIDER_TOKEN,
                currency="UZS",
                prices=[LabeledPrice(t(context, 'inv_one_title'), ONE_PRICE * 100)],
            )
        except Exception as e:
            logger.error(f"Invoice (one) yuborishda xato: {e}")
            await query.message.reply_text(t(context, 'pay_unavailable'))
    elif data == 'aksiya_video':
        await query.message.reply_text(
            "🎬 Zo'r! Videongizni shu yerga yuboring — men uni to'liq tahlil qilaman 👇"
        )
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
            [InlineKeyboardButton(t(context, 'tts_btn'), callback_data=f"tts_{aid}"),
             InlineKeyboardButton(t(context, 'tts_full_btn'), callback_data=f"ttsf_{aid}")],
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
            [InlineKeyboardButton(t(context, 'tts_btn'), callback_data=f"tts_{aid}"),
             InlineKeyboardButton(t(context, 'tts_full_btn'), callback_data=f"ttsf_{aid}")],
        ])
        if len(qisqa) <= 4000:
            try:
                await query.edit_message_text(qisqa, reply_markup=kb)
            except Exception:
                await query.message.reply_text(qisqa, reply_markup=kb)
        else:
            await query.message.reply_text(qisqa, reply_markup=kb)
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
                tahlil = await asyncio.to_thread(_gemini_process_images, tmp_paths, prompt)

                if not tahlil or not tahlil.strip():
                    raise Exception("Profil tahlili bo'sh keldi")

                # Gemini mavzuni tushunmagan bo'lsa - mavzu so'raymiz
                if "[MAVZU_KERAK]" in tahlil and not mavzu:
                    await wait_msg.delete()
                    context.user_data['mode'] = 'profile_mavzu'
                    await query.message.reply_text(t(context, 'profile_mavzu_ask'))
                    return

                await wait_msg.edit_text(t(context, 'ready'))
                _uname = query.from_user.username or query.from_user.first_name or ""
                save_analysis(user_id, username=_uname, kind="profile", toliq=tahlil)
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


def _cost_uzs(prompt_tokens, output_tokens, model="gemini-2.5-flash"):
    """Token sonidan taxminiy tannarxni (so'm) hisoblaydi. VAT (~11%) ham qo'shiladi.
    Flash-Lite uchun arzon narx ishlatiladi."""
    if "lite" in model:
        usd = prompt_tokens * PRICE_IN_LITE + output_tokens * PRICE_OUT_LITE
    else:
        usd = prompt_tokens * PRICE_IN_PER_TOKEN + output_tokens * PRICE_OUT_PER_TOKEN
    usd_with_vat = usd * 1.11  # Google VAT (soliq) ~11%
    return usd_with_vat, usd_with_vat * USD_TO_UZS


def _generate(contents, max_retries=4, model="gemini-2.5-flash"):
    """Gemini'ga so'rov yuboradi (qayta urinish + bo'sh javobni ushlash + safety bilan).
    model: 'gemini-2.5-flash' (sifatli, pullik) yoki 'gemini-2.5-flash-lite' (arzon, bepul)."""
    last_error = None
    for attempt in range(max_retries):
        try:
            kwargs = {"model": model, "contents": contents}
            if genai_types is not None:
                try:
                    # temperature=0.3 -> javob barqarorroq (foiz har safar deyarli bir xil)
                    if SAFETY_SETTINGS is not None:
                        kwargs["config"] = genai_types.GenerateContentConfig(
                            safety_settings=SAFETY_SETTINGS, temperature=0.3)
                    else:
                        kwargs["config"] = genai_types.GenerateContentConfig(temperature=0.3)
                except Exception:
                    pass
            response = client.models.generate_content(**kwargs)
            # Token sarfini ushlaymiz (admin tannarx hisoboti uchun)
            try:
                um = getattr(response, "usage_metadata", None)
                if um is not None:
                    p_tok = getattr(um, "prompt_token_count", 0) or 0
                    c_tok = getattr(um, "candidates_token_count", 0) or 0
                    # MUHIM: Gemini 2.5 "thinking" (fikrlash) tokeni ham PUL oladi
                    # (output narxida), lekin candidates_token_count ga kirmaydi.
                    think_tok = getattr(um, "thoughts_token_count", 0) or 0
                    _last_usage["prompt"] = p_tok
                    _last_usage["output"] = c_tok + think_tok  # thinking ham output narxida
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
            logger.warning(f"Generate urinish {attempt+1}/{max_retries}: {e}")
            time.sleep((attempt + 1) * 2)
    raise last_error


def _analyze(uploaded_file, prompt, max_retries=4, model="gemini-2.5-flash"):
    """Bitta video uchun tahlil."""
    return _generate([uploaded_file, prompt], max_retries=max_retries, model=model)


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


def _gemini_process(tmp_path, prompt, model="gemini-2.5-flash"):
    """BLOKLAYDIGAN to'liq Gemini ishi. Faqat alohida thread'da chaqiriladi
    (asyncio.to_thread), shunda bot muzlamaydi va Pyrogram uzilmaydi.
    model: bepul -> flash-lite (arzon), pullik -> flash (sifatli).
    FALLBACK: agar tanlangan model band (503) bo'lsa, boshqa modelga o'tadi."""
    uploaded = None
    try:
        uploaded = _upload_and_wait(tmp_path)
        try:
            return _analyze(uploaded, prompt, model=model, max_retries=2)
        except Exception as e:
            # 503/overload bo'lsa - boshqa modelga o'tib ko'ramiz (bittasi band bo'lsa ikkinchisi ishlaydi)
            fallback = "gemini-2.5-flash" if "lite" in model else "gemini-2.5-flash-lite"
            logger.warning(f"Model {model} ishlamadi ({e}); fallback: {fallback}")
            return _analyze(uploaded, prompt, model=fallback, max_retries=2)
    finally:
        if uploaded is not None:
            try:
                client.files.delete(name=uploaded.name)
            except Exception:
                pass


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Profil rejimida — profil skrinshoti. To'lovlar endi Payme orqali (chek kerak emas)."""
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
    # Aks holda: rasmni e'tiborsiz qoldiramiz (to'lov Payme orqali avtomatik)
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
            await update.message.reply_text(t(context, 'pay_ok_sub').format(until=new_until))
            try:
                create_payment(user.id, 'sub_1month', current_sub_price())
            except Exception:
                pass
            admin_txt = (f"💰 YANGI TO'LOV (Payme)\n👤 @{uname} (ID: {user.id})\n"
                         f"📦 1 oylik obuna\n💵 {paid_uzs:,} so'm\n✅ Obuna {new_until} gacha yoqildi")
        elif payload == "test_7day":
            new_until = activate_subscription(user.id, TEST_DAYS)
            await update.message.reply_text(t(context, 'pay_ok_sub').format(until=new_until))
            try:
                create_payment(user.id, 'test_7day', TEST_PRICE)
            except Exception:
                pass
            admin_txt = (f"💰 YANGI TO'LOV (Payme)\n👤 @{uname} (ID: {user.id})\n"
                         f"📦 7 kunlik test Premium\n💵 {paid_uzs:,} so'm\n✅ Obuna {new_until} gacha yoqildi")
        elif payload == "one_1":
            add_balance(user.id, 1)
            await update.message.reply_text(t(context, 'pay_ok_one'))
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

    # Kirish tekshiruvi: admin / obuna faol / bepul tahlil bor bo'lsa - o'tadi
    if has_access(user_id) == 'none':
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

        # Bitta umumiy navbat (hammaga). Farq: pullik (admin/obuna/to'lagan) ->
        # darrov tahlil; bepul -> 30 soniya "cho'ziladi" + reklama (pastda).
        _is_priority = is_admin(user_id) or has_access(user_id) == 'sub' or has_paid_ever(user_id)
        _chosen_sem = _video_semaphore

        async with _chosen_sem:
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

            # Tahlil boshlanish vaqti (tugagach "X daqiqa oldi" deyish uchun)
            _analiz_start = datetime.now()

            # "Tahlil boshlanmoqda" faqat 3 soniya turadi, keyin sanoq boshlanadi
            await asyncio.sleep(3)

            # Jonli progress mexanizmi
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
                secs = 3  # 3 soniyadan boshlaymiz (analyzing tugagandan keyin)
                shown_ad = False
                try:
                    while not _progress_stop.is_set():
                        await asyncio.sleep(4)
                        if _progress_stop.is_set():
                            break
                        secs += 4
                        # Sanoq HECH TO'XTAMAYDI - wait_msg'da doim yangilanadi
                        msg_step = steps[i % len(steps)]
                        i += 1
                        try:
                            await wait_msg.edit_text(f"{msg_step}\n⌛ {secs} soniya...")
                        except Exception:
                            pass
                        # BEPUL: o'rtada (15s) reklama ALOHIDA xabar bo'lib keladi (SMS kabi),
                        # bir marta. Sanoqqa xalal bermaydi (alohida xabar).
                        if (not _is_priority) and (not shown_ad) and secs >= 15:
                            shown_ad = True
                            try:
                                await message.reply_text(
                                    t(context, 'queued_promo') + _narx_q,
                                    reply_markup=_promo_kb, parse_mode="HTML"
                                )
                            except Exception:
                                pass
                except asyncio.CancelledError:
                    pass

            progress_task = asyncio.create_task(_show_progress())
            # Model tanlash: pullik (admin/obuna/to'lagan) -> 2.5 Flash (sifatli);
            # bepul -> 2.5 Flash-Lite (4-5 barobar arzon, sifat biroz pastroq).
            _model = "gemini-2.5-flash" if _is_priority else "gemini-2.5-flash-lite"
            # MUHIM: Gemini ishi alohida thread'da bajariladi -> bot muzlamaydi.
            try:
                # TIMEOUT 5 daqiqa: agar Gemini osilib qolsa - majburan bekor,
                # semaphore bo'shaydi, boshqa videolar qotmaydi.
                tahlil = await asyncio.wait_for(
                    asyncio.to_thread(_gemini_process, tmp_path, prompt, _model),
                    timeout=300
                )
                # BEPUL: tahlil tez tugasa ham, sanoq kamida 30 soniyagacha ketaveradi
                # (kutish bo'ladi, lekin sanoq to'xtamaydi - progress hali ishlab turibdi).
                if not _is_priority:
                    elapsed = (datetime.now() - _analiz_start).total_seconds()
                    if elapsed < 30:
                        await asyncio.sleep(30 - elapsed)
            except asyncio.TimeoutError:
                _progress_stop.set()
                progress_task.cancel()
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
            p_tok = _last_usage.get("prompt", 0)
            o_tok = _last_usage.get("output", 0)
            tot = _last_usage.get("total", 0) or (p_tok + o_tok)
            usd, uzs = _cost_uzs(p_tok, o_tok, _last_usage.get("model", "gemini-2.5-flash"))

            aid = save_analysis(user_id, username=uname, kind="video",
                                file_id=video.file_id, foiz=foiz, qisqa=qisqa, toliq=toliq,
                                tokens=tot, narx=uzs)

            # Adminlarga tannarx hisoboti (token + so'm)
            try:
                report = (
                    f"📊 Tannarx hisobi (video tahlil)\n"
                    f"👤 @{uname} (ID: {user_id})\n"
                    f"🔢 Kiruvchi: {p_tok:,} token\n"
                    f"🔢 Javob: {o_tok:,} token\n"
                    f"🔢 Jami: {tot:,} token\n"
                    f"💵 ≈ ${usd:.4f}\n"
                    f"💰 ≈ {uzs:,.0f} so'm"
                )
                for _aid in ADMIN_IDS:
                    try:
                        await context.bot.send_message(_aid, report)
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

            # Mijozga QISQA tahlil + tugmalar (To'liq ko'rish, Qisqa/To'liq eshitish)
            kb = None
            if aid:
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton(t(context, 'full_btn'), callback_data=f"full_{aid}")],
                    [InlineKeyboardButton(t(context, 'tts_btn'), callback_data=f"tts_{aid}"),
                     InlineKeyboardButton(t(context, 'tts_full_btn'), callback_data=f"ttsf_{aid}")],
                ])
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
        # 429 (kunlik limit) yoki 503 (server band) bo'lsa - boshqacha, aniqroq xabar
        emsg = str(e)
        if "RESOURCE_EXHAUSTED" in emsg or "429" in emsg or "quota" in emsg.lower():
            await wait_msg.edit_text(t(context, 'busy_quota'))
        elif "UNAVAILABLE" in emsg or "503" in emsg or "high demand" in emsg.lower():
            await wait_msg.edit_text(t(context, 'busy_quota'))
        else:
            await wait_msg.edit_text(t(context, 'error'))
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
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
    # Menyu "Fikr va takliflar" rejimi - foydalanuvchi erkin fikr yozadi (bonussiz)
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
    elif text in (TEXTS['uz']['menu_balance'], TEXTS['ru']['menu_balance']):
        uid = update.effective_user.id
        access = has_access(uid)
        if access == 'admin':
            await update.message.reply_text("👑 Admin — cheksiz tahlil.")
        elif access == 'sub':
            await update.message.reply_text(t(context, 'sub_active').format(until=sub_until_str(uid)))
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
    elif text in (TEXTS['uz']['menu_lang'], TEXTS['ru']['menu_lang']):
        await update.message.reply_text("🌐 Tilni tanlang / Выберите язык:", reply_markup=lang_keyboard())
    elif text in (TEXTS['uz']['menu_help'], TEXTS['ru']['menu_help']):
        await update.message.reply_text(t(context, 'help_text'))
    elif text in (TEXTS['uz']['menu_fikr'], TEXTS['ru']['menu_fikr']):
        context.user_data['mode'] = 'fikr'
        await update.message.reply_text(t(context, 'fikr_ask'))
    elif text in (TEXTS['uz']['menu_premium'], TEXTS['ru']['menu_premium']):
        uid = update.effective_user.id
        access = has_access(uid)
        if access == 'admin':
            await update.message.reply_text("👑 Admin — cheksiz tahlil.")
        elif access == 'sub':
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
        # Foydalanuvchiga ham xabar berishga harakat qilamiz
        try:
            await context.bot.send_message(
                target_id,
                f"🎁 Sizga {days} kunlik BEPUL obuna berildi!\n"
                f"⏳ {new_until} gacha cheksiz video tahlil qilishingiz mumkin.\n"
                f"Video yuboring — boshlaymiz! 🚀"
            )
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


async def chegirma_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: chegirmani BUGUN 17:00 gacha YOQADI (19 900) va sotuv xabarini yuboradi.
    Agar hozir 17:00 dan o'tgan bo'lsa - ERTAGA 17:00 gacha."""
    if not is_admin(update.effective_user.id):
        return
    # Chegirmani BUGUN soat 17:00 gacha yoqamiz
    now = datetime.now()
    until_dt = now.replace(hour=17, minute=0, second=0, microsecond=0)
    if until_dt <= now:
        # 17:00 o'tib ketgan bo'lsa - ertaga 17:00
        until_dt = until_dt + timedelta(days=1)
    set_setting("chegirma_until", until_dt.strftime("%Y-%m-%d %H:%M:%S"))
    await update.message.reply_text(
        f"🔥 Chegirma YOQILDI! (19 900 so'm)\n"
        f"⏳ Tugash: {until_dt.strftime('%Y-%m-%d %H:%M')} (bugun soat 17:00)\n"
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
    app.add_handler(CommandHandler("berobuna", berobuna_command))
    app.add_handler(CommandHandler("berbalans", berbalans_command))
    app.add_handler(CommandHandler("yoz", yoz_command))
    app.add_handler(CommandHandler("aksiya", aksiya_command))
    app.add_handler(CommandHandler("aksiya_tugadi", aksiya_tugadi_command))
    app.add_handler(CommandHandler("obunachilar", obunachilar_command))
    app.add_handler(CommandHandler("premium_xarajat", premium_xarajat_command))
    app.add_handler(CommandHandler("sorov", sorov_command))
    app.add_handler(CommandHandler("test_sorov", test_sorov_command))
    app.add_handler(CommandHandler("eslatma", eslatma_command))
    app.add_handler(CommandHandler("javoblar", javoblar_command))
    app.add_handler(CommandHandler("javoblar_bugun", javoblar_bugun_command))
    app.add_handler(CommandHandler("avto_aksiya_yoq", avto_aksiya_yoq_command))
    app.add_handler(CommandHandler("avto_aksiya_ochir", avto_aksiya_ochir_command))
    app.add_handler(CommandHandler("obuna_taklif", obuna_taklif_command))
    app.add_handler(CommandHandler("test_taklif", test_taklif_command))
    app.add_handler(CommandHandler("chegirma", chegirma_command))
    app.add_handler(CommandHandler("sotuv_matn", sotuv_matn_command))
    app.add_handler(CommandHandler("sotuv_korish", sotuv_korish_command))
    app.add_handler(CommandHandler("sotuv_matn_tikla", sotuv_matn_tikla_command))
    app.add_handler(CommandHandler("chegirma_ochir", chegirma_ochir_command))
    app.add_handler(CommandHandler("top", top_command))
    app.add_handler(CommandHandler("bugun", bugun_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(PreCheckoutQueryHandler(precheckout_handler))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, video_handler))
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    logger.info("Bot ishga tushdi!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
