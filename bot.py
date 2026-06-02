# -*- coding: utf-8 -*-
"""
Instagram Video Tahlil Telegram Bot
Python + Gemini API (google-genai, gemini-2.5-flash)
python-telegram-bot==21.5, google-genai==1.2.0
"""

import os
import time
import sqlite3
import asyncio
import logging
import tempfile

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from google import genai

# ============================ SOZLAMALAR ============================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "7589459697"))

CARD_NUMBER = "6262 7300 6521 3151"
CARD_OWNER = "Boqijonov Nurislom"

DB_PATH = os.environ.get("DB_PATH", "bot.db")
MODEL_NAME = "gemini-2.5-flash"

PACKAGES = {
    "1": {"credits": 1, "price": 3990},
    "5": {"credits": 5, "price": 16000},
    "10": {"credits": 10, "price": 26000},
}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("ig-bot")

gemini_client = genai.Client(api_key=GEMINI_API_KEY)

# ============================ MA'LUMOTLAR BAZASI ============================


def db_exec(query, params=(), fetchone=False, fetchall=False):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(query, params)
    result = None
    if fetchone:
        result = cur.fetchone()
    elif fetchall:
        result = cur.fetchall()
    last_id = cur.lastrowid
    conn.commit()
    conn.close()
    if fetchone or fetchall:
        return result
    return last_id


def init_db():
    db_exec(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id   INTEGER PRIMARY KEY,
            username  TEXT,
            lang      TEXT DEFAULT 'uz',
            balance   INTEGER DEFAULT 0
        )
        """
    )
    db_exec(
        """
        CREATE TABLE IF NOT EXISTS payments (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER,
            package    TEXT,
            amount     INTEGER,
            credits    INTEGER,
            status     TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def create_user(user_id, username, lang="uz"):
    db_exec(
        "INSERT OR IGNORE INTO users (user_id, username, lang, balance) VALUES (?, ?, ?, 0)",
        (user_id, username, lang),
    )


def get_user(user_id):
    return db_exec("SELECT * FROM users WHERE user_id = ?", (user_id,), fetchone=True)


def get_lang(user_id):
    row = get_user(user_id)
    if row and row["lang"]:
        return row["lang"]
    return "uz"


def set_lang(user_id, lang):
    db_exec("UPDATE users SET lang = ? WHERE user_id = ?", (lang, user_id))


def get_balance(user_id):
    row = get_user(user_id)
    return row["balance"] if row else 0


def add_balance(user_id, amount):
    db_exec("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))


def deduct_balance(user_id, amount):
    db_exec("UPDATE users SET balance = balance - ? WHERE user_id = ?", (amount, user_id))


def create_payment(user_id, package, amount, credits):
    return db_exec(
        "INSERT INTO payments (user_id, package, amount, credits, status) VALUES (?, ?, ?, ?, 'pending')",
        (user_id, package, amount, credits),
    )


def get_payment(payment_id):
    return db_exec("SELECT * FROM payments WHERE id = ?", (payment_id,), fetchone=True)


def update_payment_status(payment_id, status):
    db_exec("UPDATE payments SET status = ? WHERE id = ?", (status, payment_id))


def get_pending_payments():
    return db_exec(
        "SELECT * FROM payments WHERE status = 'pending' ORDER BY id DESC",
        fetchall=True,
    )


# ============================ TAHLIL PROMPTLARI ============================

PROMPT_UZ = """Sen — professional Instagram Reels va video kontent tahlilchisisan. 🎬
Senga yuborilgan videoni diqqat bilan ko'rib chiq va quyidagi mezonlar bo'yicha HALOL, real baho ber.
Yaltiroq gaplar emas — aniq, foydali va to'g'ridan-to'g'ri bo'l. Agar video zaif bo'lsa, past ball qo'yishdan qo'rqma.
Ko'p emoji ishlat va chiroyli formatda yoz.

Quyidagi tartibda javob ber:

🎣 HOOK (birinchi 1-3 soniya, e'tiborni ushlash): ?/10 — qisqa izoh
🎨 VIZUAL (sifat, yorug'lik, rang, kompozitsiya): ?/10 — qisqa izoh
🔊 AUDIO (ovoz tiniqligi, musiqa, trend): ?/10 — qisqa izoh
✂️ MONTAJ (kesimlar, tezlik, ritm, dinamika): ?/10 — qisqa izoh
🚀 REKOMENDATSIYAGA TUSHISH EHTIMOLI: ?/10 — qisqa izoh

📊 UMUMIY BALL: ?/10

✅ KUCHLI TOMONLAR:
- ...

⚠️ ZAIF TOMONLAR:
- ...

💡 AMALIY TAVSIYALAR (qanday yaxshilash mumkin):
- ...

Halol bo'l — foydalanuvchiga haqiqatni ayt! 🔥"""

PROMPT_RU = """Ты — профессиональный аналитик Instagram Reels и видеоконтента. 🎬
Внимательно изучи отправленное видео и дай ЧЕСТНУЮ, реальную оценку по критериям ниже.
Без воды — конкретно, полезно и по делу. Если видео слабое, не бойся ставить низкие баллы.
Используй много эмодзи и красивое оформление.

Ответь строго в таком порядке:

🎣 ХУК (первые 1-3 секунды, удержание внимания): ?/10 — краткий комментарий
🎨 ВИЗУАЛ (качество, свет, цвет, композиция): ?/10 — краткий комментарий
🔊 АУДИО (чистота звука, музыка, тренд): ?/10 — краткий комментарий
✂️ МОНТАЖ (нарезка, темп, ритм, динамика): ?/10 — краткий комментарий
🚀 ВЕРОЯТНОСТЬ ПОПАСТЬ В РЕКОМЕНДАЦИИ: ?/10 — краткий комментарий

📊 ОБЩИЙ БАЛЛ: ?/10

✅ СИЛЬНЫЕ СТОРОНЫ:
- ...

⚠️ СЛАБЫЕ СТОРОНЫ:
- ...

💡 ПРАКТИЧЕСКИЕ РЕКОМЕНДАЦИИ (как улучшить):
- ...

Будь честным — скажи пользователю правду! 🔥"""

# ============================ MATNLAR (UZ / RU) ============================

BTN = {
    "uz": {
        "analyze": "📹 Video tahlil",
        "balance": "💰 Balans",
        "buy": "🛒 Sotib olish",
        "lang": "🌐 Til",
        "help": "❓ Yordam",
    },
    "ru": {
        "analyze": "📹 Анализ видео",
        "balance": "💰 Баланс",
        "buy": "🛒 Купить",
        "lang": "🌐 Язык",
        "help": "❓ Помощь",
    },
}

# Tugma matnidan -> harakat
LABEL_TO_ACTION = {}
for _lng in BTN:
    for _act, _lbl in BTN[_lng].items():
        LABEL_TO_ACTION[_lbl] = _act

TEXTS = {
    "uz": {
        "start": (
            "👋 Assalomu alaykum!\n\n"
            "🎬 Men <b>Instagram video tahlilchi</b> botman.\n"
            "Menga Reels yoki videongizni yuboring — men uni HOOK, vizual, audio, "
            "montaj va rekomendatsiyaga tushish ehtimoli bo'yicha tahlil qilaman. 🔥\n\n"
            "📥 Boshlash uchun shunchaki video yuboring yoki menyudan foydalaning 👇"
        ),
        "send_video": "📹 Tahlil uchun menga video (Reels) yuboring 👇",
        "balance": "💰 Sizning balansingiz: <b>{balance}</b> ta tahlil",
        "balance_admin": "💰 Sizning balansingiz: <b>♾ Cheksiz</b> (admin)",
        "balance_left": "Qolgan tahlillar",
        "no_balance": (
            "⚠️ Balansingizda tahlil qolmadi!\n\n"
            "🛒 Davom etish uchun paket sotib oling 👇"
        ),
        "analyzing": "⏳ Video tahlil qilinmoqda... Iltimos kuting 🎬\n(bu 30-60 soniya olishi mumkin)",
        "error": "❌ Xatolik yuz berdi. Iltimos videoni qaytadan yuboring yoki keyinroq urinib ko'ring.",
        "too_big": "❌ Video juda katta (Telegram limiti 20MB). Iltimos qisqaroq/kichikroq video yuboring.",
        "buy_title": (
            "🛒 <b>Paketni tanlang:</b>\n\n"
            "1️⃣ <b>1 tahlil</b> — 3 990 so'm\n"
            "5️⃣ <b>5 tahlil</b> — 16 000 so'm\n"
            "🔟 <b>10 tahlil</b> — 26 000 so'm"
        ),
        "pkg_btn": "{credits} ta tahlil — {price} so'm",
        "pay_info": (
            "💳 <b>To'lov uchun karta:</b>\n"
            "<code>{card}</code>\n"
            "👤 {owner}\n\n"
            "📦 Tanlangan paket: <b>{credits} ta tahlil</b>\n"
            "💵 To'lov summasi: <b>{price} so'm</b>\n\n"
            "✅ To'lovni amalga oshirgach, <b>chek skrinshotini</b> shu yerga yuboring.\n"
            "Admin tasdiqlagach, balansingizga avtomatik qo'shiladi. 🚀"
        ),
        "payment_sent": (
            "✅ Chek qabul qilindi!\n\n"
            "⏳ Admin tasdiqlashini kuting. Tasdiqlangach sizga xabar keladi va "
            "balansingiz to'ldiriladi. 🙌"
        ),
        "photo_no_context": (
            "🤔 Rasm qabul qilindi, lekin avval <b>paket tanlang</b>.\n"
            "🛒 Sotib olish tugmasini bosing 👇"
        ),
        "approved": (
            "🎉 To'lovingiz tasdiqlandi!\n\n"
            "➕ <b>{credits} ta tahlil</b> qo'shildi.\n"
            "💰 Joriy balans: <b>{balance}</b> ta tahlil\n\n"
            "📹 Endi video yuborishingiz mumkin! 🔥"
        ),
        "rejected": (
            "❌ Afsuski, to'lovingiz tasdiqlanmadi.\n\n"
            "Iltimos to'lov ma'lumotlarini tekshiring yoki admin bilan bog'laning."
        ),
        "lang_choose": "🌐 Tilni tanlang / Выберите язык:",
        "lang_set": "✅ Til o'zgartirildi: <b>O'zbek</b> 🇺🇿",
        "help": (
            "❓ <b>Yordam</b>\n\n"
            "📹 <b>Video tahlil</b> — Reels/video yuboring, men uni tahlil qilaman.\n"
            "💰 <b>Balans</b> — qolgan tahlillar soni.\n"
            "🛒 <b>Sotib olish</b> — paket sotib olish (chek orqali).\n"
            "🌐 <b>Til</b> — uzbek/rus tili.\n\n"
            "📊 Har bir tahlil 5 ta mezon bo'yicha 10 ballik baho beradi:\n"
            "🎣 Hook, 🎨 Vizual, 🔊 Audio, ✂️ Montaj, 🚀 Rekomendatsiya ehtimoli.\n\n"
            "Buyruqlar: /start /help /til /admin"
        ),
        "admin_only": "⛔ Bu buyruq faqat admin uchun.",
        "admin_no_pending": "✅ Kutilayotgan to'lovlar yo'q.",
        "admin_pending_title": "🧾 Kutilayotgan to'lovlar: <b>{count}</b> ta",
    },
    "ru": {
        "start": (
            "👋 Здравствуйте!\n\n"
            "🎬 Я — бот <b>анализатор Instagram-видео</b>.\n"
            "Отправьте мне Reels или видео — я проанализирую ХУК, визуал, аудио, "
            "монтаж и вероятность попасть в рекомендации. 🔥\n\n"
            "📥 Чтобы начать, просто отправьте видео или используйте меню 👇"
        ),
        "send_video": "📹 Отправьте мне видео (Reels) для анализа 👇",
        "balance": "💰 Ваш баланс: <b>{balance}</b> анализов",
        "balance_admin": "💰 Ваш баланс: <b>♾ Безлимит</b> (админ)",
        "balance_left": "Осталось анализов",
        "no_balance": (
            "⚠️ На вашем балансе закончились анализы!\n\n"
            "🛒 Купите пакет, чтобы продолжить 👇"
        ),
        "analyzing": "⏳ Анализирую видео... Пожалуйста, подождите 🎬\n(это может занять 30-60 секунд)",
        "error": "❌ Произошла ошибка. Отправьте видео ещё раз или попробуйте позже.",
        "too_big": "❌ Видео слишком большое (лимит Telegram 20MB). Отправьте видео покороче/полегче.",
        "buy_title": (
            "🛒 <b>Выберите пакет:</b>\n\n"
            "1️⃣ <b>1 анализ</b> — 3 990 сум\n"
            "5️⃣ <b>5 анализов</b> — 16 000 сум\n"
            "🔟 <b>10 анализов</b> — 26 000 сум"
        ),
        "pkg_btn": "{credits} анализов — {price} сум",
        "pay_info": (
            "💳 <b>Карта для оплаты:</b>\n"
            "<code>{card}</code>\n"
            "👤 {owner}\n\n"
            "📦 Выбранный пакет: <b>{credits} анализов</b>\n"
            "💵 Сумма к оплате: <b>{price} сум</b>\n\n"
            "✅ После оплаты отправьте сюда <b>скриншот чека</b>.\n"
            "После подтверждения админом баланс пополнится автоматически. 🚀"
        ),
        "payment_sent": (
            "✅ Чек принят!\n\n"
            "⏳ Ожидайте подтверждения админа. После подтверждения вам придёт "
            "уведомление и баланс пополнится. 🙌"
        ),
        "photo_no_context": (
            "🤔 Фото получено, но сначала <b>выберите пакет</b>.\n"
            "🛒 Нажмите кнопку Купить 👇"
        ),
        "approved": (
            "🎉 Ваш платёж подтверждён!\n\n"
            "➕ Добавлено <b>{credits} анализов</b>.\n"
            "💰 Текущий баланс: <b>{balance}</b> анализов\n\n"
            "📹 Теперь можете отправлять видео! 🔥"
        ),
        "rejected": (
            "❌ К сожалению, ваш платёж не подтверждён.\n\n"
            "Проверьте данные оплаты или свяжитесь с админом."
        ),
        "lang_choose": "🌐 Tilni tanlang / Выберите язык:",
        "lang_set": "✅ Язык изменён: <b>Русский</b> 🇷🇺",
        "help": (
            "❓ <b>Помощь</b>\n\n"
            "📹 <b>Анализ видео</b> — отправьте Reels/видео, я его проанализирую.\n"
            "💰 <b>Баланс</b> — количество оставшихся анализов.\n"
            "🛒 <b>Купить</b> — покупка пакета (через чек).\n"
            "🌐 <b>Язык</b> — узбекский/русский.\n\n"
            "📊 Каждый анализ оценивает по 5 критериям (10 баллов):\n"
            "🎣 Хук, 🎨 Визуал, 🔊 Аудио, ✂️ Монтаж, 🚀 Вероятность рекомендаций.\n\n"
            "Команды: /start /help /til /admin"
        ),
        "admin_only": "⛔ Эта команда только для админа.",
        "admin_no_pending": "✅ Нет ожидающих платежей.",
        "admin_pending_title": "🧾 Ожидающие платежи: <b>{count}</b>",
    },
}


def t(lang, key, **kwargs):
    text = TEXTS.get(lang, TEXTS["uz"]).get(key, "")
    if kwargs:
        return text.format(**kwargs)
    return text


# ============================ KLAVIATURALAR ============================


def main_menu(lang):
    b = BTN[lang]
    keyboard = [
        [b["analyze"]],
        [b["balance"], b["buy"]],
        [b["lang"], b["help"]],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


def packages_kb(lang):
    rows = []
    for key, p in PACKAGES.items():
        label = t(lang, "pkg_btn", credits=p["credits"], price=p["price"])
        rows.append([InlineKeyboardButton(label, callback_data=f"pkg_{key}")])
    return InlineKeyboardMarkup(rows)


def lang_kb():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🇺🇿 O'zbek", callback_data="setlang_uz"),
                InlineKeyboardButton("🇷🇺 Русский", callback_data="setlang_ru"),
            ]
        ]
    )


def buy_inline_kb(lang):
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🛒 " + BTN[lang]["buy"], callback_data="open_buy")]]
    )


# ============================ GEMINI TAHLIL (sync) ============================


def analyze_video_sync(path, prompt):
    """Bloklovchi Gemini chaqiruvlari — alohida threadda ishlaydi."""
    uploaded = gemini_client.files.upload(file=path)

    waited = 0
    while uploaded.state.name == "PROCESSING":
        if waited > 180:
            raise TimeoutError("Video processing timeout")
        time.sleep(2)
        waited += 2
        uploaded = gemini_client.files.get(name=uploaded.name)

    if uploaded.state.name != "ACTIVE":
        raise RuntimeError("Video processing failed")

    response = gemini_client.models.generate_content(
        model=MODEL_NAME,
        contents=[uploaded, prompt],
    )

    try:
        gemini_client.files.delete(name=uploaded.name)
    except Exception:
        pass

    return response.text


# ============================ HANDLERLAR ============================


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    create_user(user.id, user.username or user.first_name, "uz")
    lang = get_lang(user.id)
    await update.message.reply_text(
        t(lang, "start"),
        reply_markup=main_menu(lang),
        parse_mode=ParseMode.HTML,
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = get_lang(update.effective_user.id)
    await update.message.reply_text(
        t(lang, "help"),
        reply_markup=main_menu(lang),
        parse_mode=ParseMode.HTML,
    )


async def til_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = get_lang(update.effective_user.id)
    await update.message.reply_text(t(lang, "lang_choose"), reply_markup=lang_kb())


async def show_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = get_lang(uid)
    if uid == ADMIN_ID:
        await update.message.reply_text(t(lang, "balance_admin"), parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(
            t(lang, "balance", balance=get_balance(uid)),
            parse_mode=ParseMode.HTML,
        )


async def show_buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = get_lang(update.effective_user.id)
    await update.message.reply_text(
        t(lang, "buy_title"),
        reply_markup=packages_kb(lang),
        parse_mode=ParseMode.HTML,
    )


async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = get_lang(uid)
    if uid != ADMIN_ID:
        await update.message.reply_text(t(lang, "admin_only"))
        return

    pending = get_pending_payments()
    if not pending:
        await update.message.reply_text(t(lang, "admin_no_pending"))
        return

    await update.message.reply_text(
        t(lang, "admin_pending_title", count=len(pending)),
        parse_mode=ParseMode.HTML,
    )
    for pay in pending:
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Tasdiqlash", callback_data=f"approve_{pay['id']}"),
                    InlineKeyboardButton("❌ Rad etish", callback_data=f"reject_{pay['id']}"),
                ]
            ]
        )
        info = (
            f"🧾 To'lov #{pay['id']}\n"
            f"🆔 User: {pay['user_id']}\n"
            f"📦 Paket: {pay['credits']} tahlil\n"
            f"💵 Summa: {pay['amount']} so'm\n"
            f"🕒 {pay['created_at']}"
        )
        await update.message.reply_text(info, reply_markup=kb)


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    create_user(uid, update.effective_user.username or update.effective_user.first_name)
    lang = get_lang(uid)
    text = (update.message.text or "").strip()
    action = LABEL_TO_ACTION.get(text)

    if action == "analyze":
        await update.message.reply_text(t(lang, "send_video"))
    elif action == "balance":
        await show_balance(update, context)
    elif action == "buy":
        await show_buy(update, context)
    elif action == "lang":
        await update.message.reply_text(t(lang, "lang_choose"), reply_markup=lang_kb())
    elif action == "help":
        await help_cmd(update, context)
    else:
        await update.message.reply_text(t(lang, "send_video"))


async def video_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    create_user(uid, update.effective_user.username or update.effective_user.first_name)
    lang = get_lang(uid)
    is_admin = uid == ADMIN_ID

    if not is_admin and get_balance(uid) <= 0:
        await update.message.reply_text(
            t(lang, "no_balance"),
            reply_markup=buy_inline_kb(lang),
            parse_mode=ParseMode.HTML,
        )
        return

    msg = update.message
    media = msg.video or msg.video_note
    if media is None and msg.document is not None:
        mime = msg.document.mime_type or ""
        if mime.startswith("video"):
            media = msg.document
    if media is None:
        await update.message.reply_text(t(lang, "send_video"))
        return

    status_msg = await update.message.reply_text(t(lang, "analyzing"))

    try:
        tg_file = await media.get_file()
    except Exception:
        logger.exception("get_file failed")
        await status_msg.edit_text(t(lang, "too_big"))
        return

    fd, path = tempfile.mkstemp(suffix=".mp4")
    os.close(fd)

    try:
        await tg_file.download_to_drive(path)

        # ✅ XATO TUZATILDI: ternary
