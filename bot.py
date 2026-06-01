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
        user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, joined TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS analyses (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, created TEXT)""")
    conn.commit()
    conn.close()


def save_user(user_id, username, first_name):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO users (user_id, username, first_name, joined) VALUES (?, ?, ?, ?)",
                  (user_id, username or "", first_name or "", datetime.now().strftime("%Y-%m-%d %H:%M")))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"save_user xato: {e}")


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
        conn.close()
        return total_users, total_analyses, today_analyses
    except Exception as e:
        logger.error(f"get_stats xato: {e}")
        return 0, 0, 0


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
            "🎬 Video tahlil — 3,990 so'm\n"
            "• 🎣 Hook tahlili\n• 🎥 Vizual va montaj\n• 🎙️ Audio va nutq\n"
            "• 🎯 Rekka chiqish ehtimoli\n• ❌ Kamchiliklar + 💡 tavsiyalar\n\n"
            "📤 Videongizni yuboring (20MB gacha):"
        ),
        'menu_video': "🎬 Video tahlil",
        'menu_profil': "📊 Profil tahlil",
        'menu_lang': "🌐 Til",
        'menu_help': "ℹ️ Yordam",
        'video_info': ("🎬 Videongizni shu yerga yuboring — men to'liq tahlil qilaman.\n\n"
                       "📏 Hozircha video 20MB dan kichik bo'lsin.\n⚠️ Hozircha bepul sinov rejimida!"),
        'profil_info': "📊 Profil tahlil tez orada ishga tushadi! 🔜",
        'help_text': ("ℹ️ INSTADOKTOR — Yordam\n\n"
                      "🎬 Video tahlil — videongizni yuboring, men uni to'liq tahlil qilaman: "
                      "hook, vizual, audio, montaj va rekka chiqish ehtimoli.\n\n"
                      "📊 Profil tahlil — tez orada!\n\n🌐 Til — tilni o'zgartirish.\n\n"
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
    },
    'ru': {
        'welcome': (
            "🩺 INSTADOKTOR — бот анализа Instagram\n\n"
            "Я честно и объективно анализирую ваши видео — "
            "не хвалю, а говорю правду. 💯\n\n"
            "🎬 Анализ видео — 3 990 сум\n"
            "• 🎣 Анализ хука\n• 🎥 Визуал и монтаж\n• 🎙️ Аудио и речь\n"
            "• 🎯 Вероятность в рекомендациях\n• ❌ Недостатки + 💡 советы\n\n"
            "📤 Отправьте ваше видео (до 20МБ):"
        ),
        'menu_video': "🎬 Анализ видео",
        'menu_profil': "📊 Анализ профиля",
        'menu_lang': "🌐 Язык",
        'menu_help': "ℹ️ Помощь",
        'video_info': ("🎬 Отправьте видео сюда — я сделаю полный анализ.\n\n"
                       "📏 Пока видео должно быть меньше 20МБ.\n⚠️ Сейчас работает в бесплатном тестовом режиме!"),
        'profil_info': "📊 Анализ профиля скоро заработает! 🔜",
        'help_text': ("ℹ️ INSTADOKTOR — Помощь\n\n"
                      "🎬 Анализ видео — отправьте видео, я полностью его проанализирую: "
                      "хук, визуал, аудио, монтаж и вероятность попадания в рекомендации.\n\n"
                      "📊 Анализ профиля — скоро!\n\n🌐 Язык — сменить язык.\n\n"
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
    }
}


def get_lang(context):
    return context.user_data.get('lang', 'uz')


def t(context, key):
    return TEXTS[get_lang(context)][key]


def main_keyboard(context):
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(t(context, 'menu_video')), KeyboardButton(t(context, 'menu_profil'))],
            [KeyboardButton(t(context, 'menu_lang')), KeyboardButton(t(context, 'menu_help'))],
        ],
        resize_keyboard=True
    )


def lang_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🇺🇿 O'zbekcha", callback_data='lang_uz')],
        [InlineKeyboardButton("🇷🇺 Русский", callback_data='lang_ru')],
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    save_user(user.id, user.username, user.first_name)
    await update.message.reply_text(
        "🌐 Tilni tanlang / Выберите язык:",
        reply_markup=lang_keyboard()
    )


async def show_menu(message, context):
    await message.reply_text(t(context, 'welcome'), reply_markup=main_keyboard(context))


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == 'lang_uz':
        context.user_data['lang'] = 'uz'
        await query.message.reply_text(t(context, 'lang_changed'), reply_markup=main_keyboard(context))
        await show_menu(query.message, context)
    elif query.data == 'lang_ru':
        context.user_data['lang'] = 'ru'
        await query.message.reply_text(t(context, 'lang_changed'), reply_markup=main_keyboard(context))
        await show_menu(query.message, context)


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


async def video_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
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
        save_analysis(message.from_user.id)

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
        await update.message.reply_text(t(context, 'video_info'))
    elif text in (TEXTS['uz']['menu_profil'], TEXTS['ru']['menu_profil']):
        await update.message.reply_text(t(context, 'profil_info'))
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
    total_users, total_analyses, today_analyses = get_stats()
    text = (
        "📊 ADMIN STATISTIKA\n\n"
        f"👥 Jami foydalanuvchilar: {total_users}\n"
        f"🎬 Jami tahlillar: {total_analyses}\n"
        f"📅 Bugungi tahlillar: {today_analyses}\n\n"
        f"💰 Taxminiy daromad (3990 so'm): {total_analyses * 3990:,} so'm"
    )
    await update.message.reply_text(text)


async def post_init(application):
    await application.bot.set_my_commands([
        BotCommand("start", "🚀 Boshlash / Начать"),
        BotCommand("help", "ℹ️ Yordam / Помощь"),
        BotCommand("til", "🌐 Til / Язык"),
    ])


def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("til", til_command))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, video_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    logger.info("Bot ishga tushdi!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
