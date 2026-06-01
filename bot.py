import os
import logging
import tempfile
import time
import sqlite3
from datetime import datetime
from google import genai
from telegram import (Update, InlineKeyboardButton, InlineKeyboardMarkup,
                      ReplyKeyboardMarkup, KeyboardButton, BotCommand)
from telegram.ext import (Application, CommandHandler, MessageHandler, filters,
                          ContextTypes, CallbackQueryHandler)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
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

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

DB_PATH = "/tmp/instadoktor.db"


def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
        joined TEXT, balance INTEGER DEFAULT 0)""")
    c.execute("""CREATE TABLE IF NOT EXISTS analyses (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, created TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
        package TEXT, amount INTEGER, status TEXT, created TEXT)""")
    conn.commit()
    conn.close()


def save_user(user_id, username, first_name):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO users (user_id, username, first_name, joined, balance) VALUES (?, ?, ?, ?, 0)",
                  (user_id, username or "", first_name or "", datetime.now().strftime("%Y-%m-%d %H:%M")))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"save_user xato: {e}")


def get_balance(user_id):
    if user_id == ADMIN_ID:
        return 999999
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        conn.close()
        return row[0] if row else 0
    except Exception as e:
        logger.error(f"get_balance xato: {e}")
        return 0


def add_balance(user_id, amount):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"add_balance xato: {e}")


def use_balance(user_id):
    if user_id == ADMIN_ID:
        return
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("UPDATE users SET balance = balance - 1 WHERE user_id = ? AND balance > 0", (user_id,))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"use_balance xato: {e}")


def save_analysis(user_id):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT INTO analyses (user_id, created) VALUES (?, ?)",
                  (user_id, datetime.now().strftime("%Y-%m-%d %H:%M")))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"save_analysis xato: {e}")


def create_payment(user_id, package, amount):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT INTO payments (user_id, package, amount, status, created) VALUES (?, ?, ?, 'pending', ?)",
                  (user_id, package, amount, datetime.now().strftime("%Y-%m-%d %H:%M")))
        pid = c.lastrowid
        conn.commit()
        conn.close()
        return pid
    except Exception as e:
        logger.error(f"create_payment xato: {e}")
        return None


def get_stats():
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM users")
        total_users = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM analyses")
        total_analyses = c.fetchone()[0]
        today = datetime.now().strftime("%Y-%m-%d")
        c.execute("SELECT COUNT(*) FROM analyses WHERE created LIKE ?", (today + "%",))
        today_analyses = c.fetchone()[0]
        c.execute("SELECT COALESCE(SUM(amount),0) FROM payments WHERE status = 'approved'")
        revenue = c.fetchone()[0]
        conn.close()
        return total_users, total_analyses, today_analyses, revenue
    except Exception as e:
        logger.error(f"get_stats xato: {e}")
        return 0, 0, 0, 0


PROMPT_UZ = """Sen tajribali, xolis Instagram kontent tahlilchisisan.
Vazifang — blogger videosini HALOL va OBJEKTIV baholash. Video qanday bo'lsa,
shunday bahola: yaxshi bo'lsa yaxshi de, kuchsiz bo'lsa kuchsiz de. Na ortiqcha
maqta, na asossiz tanqid qil. Faqat HAQIQATNI ayt.

QAT'IY QOIDALAR:
- Baho videoning haqiqiy sifatiga MOS bo'lsin — adolatli bo'l.
- "Ajoyib", "zo'r", "wow" so'zlarini faqat HAQIQATAN shunday bo'lsa ishlat.
- Har bir kamchilikni aniq ayt. Yumshatma, lekin bo'rttirma ham.
- Rekka chiqish ehtimolini REAL bahola.
- Bloggerni O'STIRISH uchun gapir. Halol baho — eng katta yordam.

MUHIM: KO'P EMOJI ishlat — javob jonli, chiroyli bo'lsin, lekin tahlil chuqur qolsin.

Har bo'limni O'ZBEK tilida, ko'p emoji bilan yoz:

🎣 HOOK (0-3 sekund) — e'tiborni tortadimi? ⭐ Ball: __/10
🎬 VIZUAL VA MONTAJ — 🎥 yoritish, kamera, montaj. ⭐ Ball: __/10
🗣️ AUDIO VA NUTQ — 🎙️ nima gapirildi, ovoz toni. ⭐ Ball: __/10
📝 KONTENT VA QIYMAT — 💬 xabar, CTA. ⭐ Ball: __/10
📊 REKKA CHIQISH EHTIMOLI — 🎯 real foiz va sabablar.
✅ KUCHLI TOMONLARI — faqat haqiqiy kuchli joylar.
❌ KAMCHILIKLAR — barcha jiddiy kamchiliklar.
💡 TAVSIYALAR — 🚀 5 ta amaliy qadam.

Sen do'st emas, ekspertsan. Halol baho bloggerni o'stiradi. 💪"""

PROMPT_RU = """Ты опытный, объективный аналитик Instagram-контента.
Твоя задача — ЧЕСТНО и ОБЪЕКТИВНО оценить видео блогера. Оценивай как есть:
хорошее — хорошо, слабое — слабо. Не хвали зря и не критикуй без причины.
Говори только ПРАВДУ.

СТРОГИЕ ПРАВИЛА:
- Оценка должна СООТВЕТСТВОВАТЬ реальному качеству видео — будь справедлив.
- Слова "отлично", "круто", "вау" используй только если это ПРАВДА.
- Указывай каждый недостаток чётко. Не смягчай, но и не преувеличивай.
- Оценивай вероятность попадания в рекомендации РЕАЛЬНО.
- Говори, чтобы помочь блогеру РАСТИ. Честная оценка — лучшая помощь.

ВАЖНО: используй МНОГО ЭМОДЗИ — ответ должен быть живым и красивым,
но анализ оставайся глубоким.

Каждый раздел пиши на РУССКОМ языке, с эмодзи:

🎣 ХУК (0-3 сек) — цепляет внимание? ⭐ Балл: __/10
🎬 ВИЗУАЛ И МОНТАЖ — 🎥 свет, камера, монтаж. ⭐ Балл: __/10
🗣️ АУДИО И РЕЧЬ — 🎙️ что сказано, тон голоса. ⭐ Балл: __/10
📝 КОНТЕНТ И ЦЕННОСТЬ — 💬 посыл, призыв к действию. ⭐ Балл: __/10
📊 ВЕРОЯТНОСТЬ В РЕКОМЕНДАЦИИ — 🎯 реальный процент и причины.
✅ СИЛЬНЫЕ СТОРОНЫ — только реальные плюсы.
❌ НЕДОСТАТКИ — все серьёзные минусы.
💡 РЕКОМЕНДАЦИИ — 🚀 5 конкретных шагов.

Ты не друг, ты эксперт. Честная оценка помогает блогеру расти. 💪"""

TEXTS = {
    'uz': {
        'welcome': (
            "🩺 INSTADOKTOR — Instagram tahlil boti\n\n"
            "Men videolaringizni halol va xolis tahlil qilaman — "
            "maqtov emas, haqiqat aytaman. 💯\n\n"
            "📤 Videongizni yuboring (20MB gacha):"
        ),
        'menu_video': "🎬 Video tahlil",
        'menu_balance': "💰 Balansim",
        'menu_lang': "🌐 Til",
        'menu_help': "ℹ️ Yordam",
        'profil_info': "📊 Profil tahlil tez orada ishga tushadi! 🔜",
        'help_text': ("ℹ️ INSTADOKTOR — Yordam\n\n"
                      "🎬 Video tahlil — videongizni yuboring, men uni to'liq tahlil qilaman: "
                      "hook, vizual, audio, montaj va rekka chiqish ehtimoli.\n\n"
                      "💰 Balansim — qancha tahlil qolganini ko'rish.\n\n"
                      "🌐 Til — tilni o'zgartirish.\n\n"
                      "📏 Video 20MB dan kichik bo'lsin."),
        'lang_changed': "✅ Til o'zgartirildi!",
        'received': "⏳ Video qabul qilindi! Tahlil boshlanmoqda... ⚡",
        'too_big': "❌ Video juda katta (20MB dan oshmasligi kerak). 📏\n\nIltimos, qisqaroq yuboring.",
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
            "📤 Отправьте ваше видео (до 20МБ):"
        ),
        'menu_video': "🎬 Анализ видео",
        'menu_balance': "💰 Мой баланс",
        'menu_lang': "🌐 Язык",
        'menu_help': "ℹ️ Помощь",
        'profil_info': "📊 Анализ профиля скоро заработает! 🔜",
        'help_text': ("ℹ️ INSTADOKTOR — Помощь\n\n"
                      "🎬 Анализ видео — отправьте видео, я полностью его проанализирую.\n\n"
                      "💰 Мой баланс — сколько анализов осталось.\n\n"
                      "🌐 Язык — сменить язык.\n\n"
                      "📏 Видео должно быть меньше 20МБ."),
        'lang_changed': "✅ Язык изменён!",
        'received': "⏳ Видео получено! Начинаю анализ... ⚡",
        'too_big': "❌ Видео слишком большое (не более 20МБ). 📏\n\nПожалуйста, отправьте покороче.",
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
            [KeyboardButton(t(context, 'menu_video')), KeyboardButton(t(context, 'menu_balance'))],
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
        await query.message.reply_text(
            t(context, 'pay_instr').format(amount=amount, card=CARD_NUMBER, name=CARD_NAME)
        )
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


def upload_with_retry(tmp_path, max_retries=3):
    last_error = None
    for attempt in range(max_retries):
        try:
            uploaded = client.files.upload(file=tmp_path)
            waited = 0
            while uploaded.state.name == "PROCESSING" and waited < 120:
                time.sleep(3)
                waited += 3
                uploaded = client.files.get(name=uploaded.name)
            if uploaded.state.name == "FAILED":
                raise Exception("Video processing failed")
            return uploaded
        except Exception as e:
            last_error = e
            logger.warning(f"Upload urinish {attempt+1}/{max_retries}: {e}")
            time.sleep((attempt + 1) * 4)
    raise last_error


def analyze_with_retry(uploaded_file, prompt, max_retries=4):
    last_error = None
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=[uploaded_file, prompt]
            )
            return response.text
        except Exception as e:
            last_error = e
            wait = (attempt + 1) * 5
            logger.warning(f"Tahlil urinish {attempt+1}/{max_retries}: {e}, {wait}s")
            time.sleep(wait)
    raise last_error


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Foydalanuvchi chek skrinshotini yuborganda"""
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

        if video.file_size and video.file_size > 20 * 1024 * 1024:
            await wait_msg.edit_text(t(context, 'too_big'))
            return

        file = await context.bot.get_file(video.file_id)
        with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False) as tmp_file:
            tmp_path = tmp_file.name
        await file.download_to_drive(tmp_path)

        await wait_msg.edit_text(t(context, 'uploading'))
        uploaded_file = upload_with_retry(tmp_path)

        await wait_msg.edit_text(t(context, 'analyzing'))
        prompt = PROMPT_RU if get_lang(context) == 'ru' else PROMPT_UZ
        tahlil = analyze_with_retry(uploaded_file, prompt)

        await wait_msg.edit_text(t(context, 'ready'))
        use_balance(user_id)
        save_analysis(user_id)

        if len(tahlil) <= 4000:
            await message.reply_text(tahlil)
        else:
            chunks = [tahlil[i:i+4000] for i in range(0, len(tahlil), 4000)]
            for chunk in chunks:
                await message.reply_text(chunk)

    except Exception as e:
        logger.error(f"Yakuniy xato: {e}")
        await wait_msg.edit_text(t(context, 'error'))
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
        if uploaded_file:
            try:
                client.files.delete(name=uploaded_file.name)
            except:
                pass


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text in (TEXTS['uz']['menu_video'], TEXTS['ru']['menu_video']):
        await update.message.reply_text(t(context, 'send_video'))
    elif text in (TEXTS['uz']['menu_balance'], TEXTS['ru']['menu_balance']):
        bal = get_balance(update.effective_user.id)
        await update.message.reply_t
