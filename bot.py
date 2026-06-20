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
# Pullik Gemini + kattaroq serverga o'tganda Railway'dan MAX_CONCURRENT ni oshiring.
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT", "10") or "10")
# Navbat mexanizmi: bir vaqtda faqat MAX_CONCURRENT ta TEKIN tahlil ishlaydi.
# Pullik (admin/obuna/to'lagan) uchun ALOHIDA kengroq navbat — ular tez xizmat oladi.
_video_semaphore = asyncio.Semaphore(MAX_CONCURRENT)
# Pullik foydalanuvchilar uchun alohida, kengroq slot (deyarli kutmaydi).
PRIORITY_CONCURRENT = int(os.getenv("PRIORITY_CONCURRENT", "20") or "20")
_priority_semaphore = asyncio.Semaphore(PRIORITY_CONCURRENT)
ADMIN_ID = 7589459697
# Barcha adminlar (cheksiz tahlil, /top, tannarx hisoboti va h.k.)
ADMIN_IDS = [7589459697, 5808245573, 356530813]


def is_admin(user_id):
    return user_id in ADMIN_IDS
CARD_NUMBER = "6262 7300 6521 3151"
CARD_NAME = "Boqijonov Nurislom"

# Obuna (1 oylik): narx (so'm) va kun
SUB_PRICE = 29900
SUB_DAYS = 30
# 1 martalik tahlil narxi (so'm)
ONE_PRICE = 5090

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
                # users jadvaliga yangi ustunlar (obuna + referral)
                for col, typ in [("sub_until", "TEXT"),
                                 ("referred_by", "BIGINT"),
                                 ("ref_credited", "BOOLEAN DEFAULT FALSE"),
                                 ("ref_reward_given", "BOOLEAN DEFAULT FALSE"),
                                 ("aksiya_given", "BOOLEAN DEFAULT FALSE"),
                                 ("obuna_taklif_given", "BOOLEAN DEFAULT FALSE"),
                                 ("sorov_given", "BOOLEAN DEFAULT FALSE"),
                                 ("sorov_reward", "BOOLEAN DEFAULT FALSE")]:
                    cur.execute(f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {col} {typ}")
                # analyses jadvaliga yangi ustunlar (bor bo'lsa - tegmaydi)
                for col, typ in [("username", "TEXT"), ("kind", "TEXT"),
                                 ("file_id", "TEXT"), ("foiz", "INTEGER DEFAULT 0"),
                                 ("qisqa", "TEXT"), ("toliq", "TEXT")]:
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


def grant_auto_aksiya(user_id):
    """1 ta bepulni ishlatib, balansi tugagan, aksiyani hali olmagan userga
    avtomatik +2 beradi va xabar yuborish kerakligini bildiradi (True).
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
        add_balance(user_id, 2)
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
TO'LIQ, chuqur tahlil (har bo'lim bir necha jumla, ko'p emoji bilan). MUHIM: har bo'lim orasiga BITTA BO'SH QATOR qo'y, bo'limlar bir-biriga yopishmasin, o'qishga oson bo'lsin:
🎣 HOOK (0-3 sekund) — e'tiborni tortadimi? ⭐ Ball: _/10 — batafsil izoh
🎬 VIZUAL VA MONTAJ — 🎥 yoritish, kamera, montaj. ⭐ Ball: _/10 — batafsil izoh
🗣️ AUDIO VA NUTQ — 🎙️ nima gapirildi, ovoz toni. ⭐ Ball: _/10 — batafsil izoh
📝 KONTENT VA QIYMAT — 💬 xabar, CTA. ⭐ Ball: _/10 — batafsil izoh
📊 REKKA CHIQISH EHTIMOLI — 🎯 _% va batafsil sabablar.
✅ KUCHLI TOMONLARI — faqat haqiqiy kuchli joylar.
❌ KAMCHILIKLAR — barcha jiddiy kamchiliklar, ochiq ayt.
💡 TAVSIYALAR — 🚀 5 ta aniq, amaliy qadam.
🇺🇿 O'ZBEK BOZORI MASLAHATI — bu video O'ZBEK Instagram auditoriyasi uchun qanchalik mos? O'zbekistonda qaysi vaqtda (masalan kechqurun 19:00-23:00) post qilish, mahalliy trendlar, o'zbek tomoshabin nimani yoqtirishi, qaysi hashtaglar va mahalliy kontekst bo'yicha 2-3 ta ANIQ maslahat ber.
[/TOLIQ]

Baho videoning haqiqiy sifatiga MOS bo'lsin. Foiz REAL bo'lsin. Sen oddiy AI emas, O'ZBEK Instagram bozorini chuqur biladigan ekspertsan — mahalliy, aniq, foydali maslahat ber. Halol baho bloggerni o'stiradi."""

PROMPT_RU = """Ты опытный, объективный аналитик Instagram-контента. Оцени видео блогера ЧЕСТНО. Говори только ПРАВДУ.

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
ПОЛНЫЙ, глубокий анализ (каждый раздел в несколько предложений, с эмодзи). ВАЖНО: между разделами оставляй ОДНУ ПУСТУЮ СТРОКУ, чтобы разделы не слипались и легко читались:
🎣 ХУК (0-3 сек) — цепляет? ⭐ Балл: _/10 — подробно
🎬 ВИЗУАЛ И МОНТАЖ — 🎥 свет, камера, монтаж. ⭐ Балл: _/10 — подробно
🗣️ АУДИО И РЕЧЬ — 🎙️ что сказано, тон. ⭐ Балл: _/10 — подробно
📝 КОНТЕНТ И ЦЕННОСТЬ — 💬 посыл, призыв. ⭐ Балл: _/10 — подробно
📊 ВЕРОЯТНОСТЬ В РЕКОМЕНДАЦИИ — 🎯 _% и причины.
✅ СИЛЬНЫЕ СТОРОНЫ — реальные плюсы.
❌ НЕДОСТАТКИ — все серьёзные минусы.
💡 РЕКОМЕНДАЦИИ — 🚀 5 конкретных шагов.
🇺🇿 СОВЕТ ДЛЯ УЗБЕКСКОГО РЫНКА — насколько видео подходит для УЗБЕКСКОЙ аудитории Instagram? Дай 2-3 конкретных совета: когда постить в Узбекистане (например вечером 19:00-23:00), местные тренды, что любит узбекский зритель, хештеги и местный контекст.
[/TOLIQ]

Оценка должна соответствовать реальному качеству. Процент реальный. Ты не обычный AI, а эксперт, глубоко знающий узбекский рынок Instagram — давай местные, конкретные, полезные советы."""


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
        'aksiya_msg': ("📣 Algoritmlarni yangiladik va sizga yana 2 ta bepul tahlil sovg'a qilamiz!\n\n"
                       "Salom! Bitta video — bu shunchaki sinov. Haqiqiy natija va topga chiqish "
                       "masofada, xatolar ustida ishlaganda ko'rinadi.\n\n"
                       "Balansingizga yana 2 ta bepul imkoniyat qo'shdik 🎁\n\n"
                       "Oldingi tavsiyamizga qarab, videongizni xuki (boshlanishi) yoki yakunini "
                       "o'zgartirib, qaytadan yuklab ko'ring. Keling, videongizni ideal holatga keltiramiz! 🚀"),
        'obuna_taklif_msg': ("Sizning Reels'dagi salohiyatingiz juda katta! 🚀\n\n"
                             "Cheksiz video tahlil qilish, har bir hookni ideal holatga keltirish va "
                             "ko'rishlarda barqaror o'sish uchun — obunani faollashtiring.\n\n"
                             "Bu atigi oyiga 29 900 so'm — kuniga 1 000 so'mdan ham kam, "
                             "shaxsiy AI-prodyuseringiz uchun! 💎"),
        'obuna_taklif_btn': "💎 Obunani faollashtirish",
        'sorov_msg': ("🙏 Bir daqiqa vaqtingizni ajrating!\n\n"
                      "🎉 Bizga allaqachon {n} dan ortiq foydalanuvchi qo'shildi!\n\n"
                      "Botimiz sizga foydali bo'ldimi? Fikringiz biz uchun juda muhim."),
        'sorov_yes': "👍 Ha, foydali",
        'sorov_no': "👎 Yo'q",
        'sorov_ask_yes': "Zo'r! 😊 Aytingchi, botimizning nimasi sizga yoqdi? Fikringizni yozib qoldiring 👇",
        'sorov_ask_no': "Afsus 🙏 Nega yoqmadi yoki nimasi yetishmadi? Iltimos, yozib qoldiring — biz yaxshilaymiz 👇",
        'sorov_thanks_reward': "Rahmat fikringiz uchun! 🎁 Sizga 1 ta BEPUL tahlil qo'shdik. Video yuboring! 🎬",
        'sorov_thanks': "Rahmat fikringiz uchun! 🙏",
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
        'analyzing': "🧠 InstaDoctor AI tahlil qilmoqda (vizual + audio)... ⚡",
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
        'sub_btn': "💳 Obuna sotib olish (30 kun / 29,900 so'm)",
        'one_btn': "🎬 1 ta tahlil (5,090 so'm)",
        'pay_instr_one': ("🎬 1 TA VIDEO TAHLIL\n\n"
                          "💰 Summa: {amount:,} so'm\n"
                          "💳 Karta: {card}\n"
                          "👤 Ega: {name}\n\n"
                          "✅ To'lagach, CHEK SKRINSHOTINI shu yerga yuboring.\n"
                          "Admin tekshirib, hisobingizga 1 ta tahlil qo'shadi. ⏳"),
        'sub_active': "✅ Obunangiz faol — {until} gacha.\nCheksiz video tahlil! 🎬",
        'sub_offer': ("🎬 1 oylik obuna bilan CHEKSIZ video tahlil!\n"
                      "💰 Narxi: 29,900 so'm / 30 kun\n\n"
                      "Sotib olish uchun pastdagi tugmani bosing 👇"),
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
        'aksiya_msg': ("📣 Мы обновили алгоритмы и дарим тебе ещё 2 анализа бесплатно!\n\n"
                       "Привет! Одно видео — это только тест. Настоящий результат и выход в топ "
                       "видны на дистанции, при работе над ошибками.\n\n"
                       "Мы начислили тебе ещё 2 бесплатных анализа 🎁\n\n"
                       "Измени хук (начало) или концовку по нашей рекомендации и пришли видео снова. "
                       "Давай доведём твоё видео до идеала! 🚀"),
        'obuna_taklif_msg': ("Твой потенциал в Reels огромен! 🚀\n\n"
                             "Чтобы анализировать неограниченное количество видео, докручивать каждый "
                             "хук до идеала и стабильно расти в охватах — активируй подписку.\n\n"
                             "Это всего 29 900 сумов в месяц — меньше 1 000 сумов в день "
                             "за личного AI-продюсера! 💎"),
        'obuna_taklif_btn': "💎 Активировать подписку",
        'sorov_msg': ("🙏 Уделите минуту!\n\n"
                      "🎉 К нам уже присоединилось более {n} пользователей!\n\n"
                      "Был ли наш бот полезен для вас? Ваше мнение очень важно для нас."),
        'sorov_yes': "👍 Да, полезен",
        'sorov_no': "👎 Нет",
        'sorov_ask_yes': "Отлично! 😊 Расскажите, что именно вам понравилось в боте? Напишите ниже 👇",
        'sorov_ask_no': "Жаль 🙏 Почему не понравилось или чего не хватило? Пожалуйста, напишите — мы улучшим 👇",
        'sorov_thanks_reward': "Спасибо за отзыв! 🎁 Мы добавили вам 1 БЕСПЛАТНЫЙ анализ. Отправьте видео! 🎬",
        'sorov_thanks': "Спасибо за отзыв! 🙏",
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
        'analyzing': "🧠 InstaDoctor AI анализирует (визуал + аудио)... ⚡",
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
        'sub_btn': "💳 Оформить подписку (30 дней / 29 900 сум)",
        'one_btn': "🎬 1 анализ (5 090 сум)",
        'pay_instr_one': ("🎬 1 АНАЛИЗ ВИДЕО\n\n"
                          "💰 Сумма: {amount:,} сум\n"
                          "💳 Карта: {card}\n"
                          "👤 Владелец: {name}\n\n"
                          "✅ После оплаты отправьте СКРИНШОТ ЧЕКА сюда.\n"
                          "Админ проверит и добавит 1 анализ на счёт. ⏳"),
        'sub_active': "✅ Подписка активна — до {until}.\nБезлимитный анализ видео! 🎬",
        'sub_offer': ("🎬 С подпиской на 1 месяц — БЕЗЛИМИТНЫЙ анализ видео!\n"
                      "💰 Цена: 29 900 сум / 30 дней\n\n"
                      "Нажмите кнопку ниже, чтобы оформить 👇"),
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
            [KeyboardButton(t(context, 'menu_balance')), KeyboardButton(t(context, 'menu_ref'))],
            [KeyboardButton(t(context, 'menu_lang')), KeyboardButton(t(context, 'menu_help'))],
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
        [InlineKeyboardButton(t(context, 'sub_btn'), callback_data='buy_sub')],
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
                prices=[LabeledPrice(t(context, 'inv_sub_title'), SUB_PRICE * 100)],
            )
        except Exception as e:
            logger.error(f"Invoice (sub) yuborishda xato: {e}")
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
    elif data in ('sorov_yes', 'sorov_no'):
        # So'rovga javob berdi - endi sababini so'raymiz
        context.user_data['sorov_answer'] = 'Ha' if data == 'sorov_yes' else "Yo'q"
        context.user_data['mode'] = 'sorov_sabab'
        if data == 'sorov_yes':
            await query.message.reply_text(t(context, 'sorov_ask_yes'))
        else:
            await query.message.reply_text(t(context, 'sorov_ask_no'))
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
        toliq = toliq + t(context, 'analyzed_footer')
        if len(toliq) <= 4000:
            await query.message.reply_text(toliq)
        else:
            for i in range(0, len(toliq), 4000):
                await query.message.reply_text(toliq[i:i+4000])
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
# Dollar kursi (so'm) - Railway'dan o'zgartirsa bo'ladi
USD_TO_UZS = float(os.getenv("USD_TO_UZS", "12600"))
# Oxirgi so'rovning token sarfi (admin hisobotida ishlatiladi)
_last_usage = {"prompt": 0, "output": 0, "total": 0}


def _cost_uzs(prompt_tokens, output_tokens):
    """Token sonidan taxminiy tannarxni (so'm) hisoblaydi."""
    usd = prompt_tokens * PRICE_IN_PER_TOKEN + output_tokens * PRICE_OUT_PER_TOKEN
    return usd, usd * USD_TO_UZS


def _generate(contents, max_retries=4):
    """Gemini'ga so'rov yuboradi (qayta urinish + bo'sh javobni ushlash + safety bilan)."""
    last_error = None
    for attempt in range(max_retries):
        try:
            kwargs = {"model": "gemini-2.5-flash", "contents": contents}
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
                    _last_usage["prompt"] = getattr(um, "prompt_token_count", 0) or 0
                    _last_usage["output"] = getattr(um, "candidates_token_count", 0) or 0
                    _last_usage["total"] = getattr(um, "total_token_count", 0) or 0
            except Exception:
                pass
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
        if q.invoice_payload in ("sub_1month", "one_1"):
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
                create_payment(user.id, 'sub_1month', SUB_PRICE)
            except Exception:
                pass
            admin_txt = (f"💰 YANGI TO'LOV (Payme)\n👤 @{uname} (ID: {user.id})\n"
                         f"📦 1 oylik obuna\n💵 {paid_uzs:,} so'm\n✅ Obuna {new_until} gacha yoqildi")
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
    """Matndagi '📈 ... NN%' qatorini yangi foizga almashtiradi (qisqa va to'liq uchun)."""
    if not text:
        return text
    out = []
    for line in text.split('\n'):
        if '📈' in line and '%' in line:
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

        # Navbat: pullik (admin/obuna/to'lagan) -> tez (priority) navbat;
        # bepul (hech to'lamagan) -> oddiy navbat (MAX_CONCURRENT bilan cheklangan).
        _is_priority = is_admin(user_id) or has_access(user_id) == 'sub' or has_paid_ever(user_id)
        _chosen_sem = _priority_semaphore if _is_priority else _video_semaphore
        # Faqat bepul foydalanuvchilarga "navbatda" deb bildiramiz (pullik deyarli kutmaydi)
        if (not _is_priority) and _video_semaphore.locked():
            await wait_msg.edit_text(t(context, 'queued'))

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

            # Jonli progress: tahlil davom etayotganini ko'rsatib turamiz.
            # Bot xabarni har bir necha soniyada yangilab turadi (⏳ -> ⏳⏳ -> ...).
            _progress_stop = asyncio.Event()

            async def _show_progress():
                base = t(context, 'analyzing_live')
                dots = 0
                secs = 0
                try:
                    while not _progress_stop.is_set():
                        await asyncio.sleep(4)
                        if _progress_stop.is_set():
                            break
                        dots = (dots % 4) + 1
                        secs += 4
                        try:
                            await wait_msg.edit_text(f"{base} {'⏳' * dots}\n⌛ {secs} soniya...")
                        except Exception:
                            pass  # bir xil matn yoki tahrir xatosi - e'tibor bermaymiz
                except asyncio.CancelledError:
                    pass

            progress_task = asyncio.create_task(_show_progress())
            # MUHIM: Gemini ishi alohida thread'da bajariladi -> bot muzlamaydi,
            # Pyrogram uzilmaydi, bir nechta odam bir vaqtda ishlatsa ham ishlaydi.
            try:
                tahlil = await asyncio.to_thread(_gemini_process, tmp_path, prompt)
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
            aid = save_analysis(user_id, username=uname, kind="video",
                                file_id=video.file_id, foiz=foiz, qisqa=qisqa, toliq=toliq)

            # Adminlarga tannarx hisoboti (token + so'm)
            try:
                p_tok = _last_usage.get("prompt", 0)
                o_tok = _last_usage.get("output", 0)
                tot = _last_usage.get("total", 0) or (p_tok + o_tok)
                usd, uzs = _cost_uzs(p_tok, o_tok)
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
    # So'rov sababini kutyapmizmi?
    if context.user_data.get('mode') == 'sorov_sabab':
        context.user_data['mode'] = None
        uid = update.effective_user.id
        answer = context.user_data.get('sorov_answer', '?')
        sabab = (text or "").strip()
        uname = update.effective_user.username or update.effective_user.first_name or ""
        # Adminlarga javobni yuboramiz
        emoji = "👍" if answer == "Ha" else "👎"
        report = f"📝 SO'ROV JAVOBI\n👤 @{uname} (ID: {uid})\n{emoji} {answer}\n💬 {sabab}"
        for _aid in ADMIN_IDS:
            try:
                await context.bot.send_message(_aid, report)
            except Exception:
                pass
        # Ha desa 10 harf, Yo'q desa 15 harfdan ko'p yozsa - 1 ta bepul (faqat 1 marta)
        row = _db_execute("SELECT COALESCE(sorov_reward, FALSE) FROM users WHERE user_id = %s", (uid,), fetch='one')
        already = row[0] if row else False
        min_len = 10 if answer == "Ha" else 15
        if len(sabab) > min_len and not already:
            add_balance(uid, 1)
            _db_execute("UPDATE users SET sorov_reward = TRUE WHERE user_id = %s", (uid,))
            await update.message.reply_text(t(context, 'sorov_thanks_reward'))
        else:
            await update.message.reply_text(t(context, 'sorov_thanks'))
        return
    if text in (TEXTS['uz']['menu_video'], TEXTS['ru']['menu_video']):
        context.user_data['mode'] = None
        await update.message.reply_text(t(context, 'send_video'))
    elif text in (TEXTS['uz']['menu_profile'], TEXTS['ru']['menu_profile']):
        # Profil tahlili hozircha vaqtincha o'chirilgan ("tez orada")
        context.user_data['mode'] = None
        await update.message.reply_text(t(context, 'profil_info'))
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
            # +2 tahlil va belgilab qo'yamiz (takror bo'lmasin)
            add_balance(uid, 2)
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
                InlineKeyboardButton(TEXTS['uz']['sorov_yes'], callback_data="sorov_yes"),
                InlineKeyboardButton(TEXTS['uz']['sorov_no'], callback_data="sorov_no"),
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
    app.add_handler(CommandHandler("aksiya", aksiya_command))
    app.add_handler(CommandHandler("aksiya_tugadi", aksiya_tugadi_command))
    app.add_handler(CommandHandler("obunachilar", obunachilar_command))
    app.add_handler(CommandHandler("sorov", sorov_command))
    app.add_handler(CommandHandler("avto_aksiya_yoq", avto_aksiya_yoq_command))
    app.add_handler(CommandHandler("avto_aksiya_ochir", avto_aksiya_ochir_command))
    app.add_handler(CommandHandler("obuna_taklif", obuna_taklif_command))
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
