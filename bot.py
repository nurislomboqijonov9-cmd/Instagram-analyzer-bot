import os
import logging
import tempfile
import time
from google import genai
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

client = genai.Client(api_key=GEMINI_API_KEY)

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ============ TAHLIL PROMPTLARI (2 til) ============

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

# ============ MATNLAR (2 til) ============

TEXTS = {
    'uz': {
        'choose_lang': "🌐 Tilni tanlang / Выберите язык:",
        'welcome': (
            "🩺 INSTADOKTOR — Instagram tahlil boti\n\n"
            "Men videolaringizni halol va xolis tahlil qilaman — "
            "maqtov emas, haqiqat aytaman. 💯\n\n"
            "🎬 Video tahlil — 3,990 so'm\n"
            "• 🎣 Hook tahlili\n"
            "• 🎥 Vizual va montaj\n"
            "• 🎙️ Audio va nutq\n"
            "• 🎯 Rekka chiqish ehtimoli\n"
            "• ❌ Kamchiliklar + 💡 tavsiyalar\n\n"
            "📤 Videongizni yuboring (20MB gacha):"
        ),
        'btn_video': "🎬 Video tahlil — 3,990 so'm",
        'btn_profil': "📊 Profil tahlil — 5,990 so'm",
        'video_info': (
            "🎬 Videongizni shu yerga yuboring — men to'liq tahlil qilaman.\n\n"
            "📏 Hozircha video 20MB dan kichik bo'lsin.\n"
            "⚠️ Hozircha bepul sinov rejimida!"
        ),
        'profil_info': "📊 Profil tahlil tez orada ishga tushadi! 🔜",
        'received': "⏳ Video qabul qilindi! Tahlil boshlanmoqda... ⚡",
        'too_big': "❌ Video juda katta (20MB dan oshmasligi kerak). 📏\n\nIltimos, qisqaroq yuboring.",
        'wrong_format': "❌ Video formatini tanimadim. MP4 yoki MOV yuboring. 📹",
        'uploading': "📤 Video yuklanmoqda...",
        'analyzing': "🧠 AI tahlil qilinmoqda (vizual + audio)... ⚡",
        'ready': "✅ Tahlil tayyor!",
        'error': "😔 Kechirasiz, tahlil qilib bo'lmadi. Iltimos, videoni qayta yuboring. 🔄",
        'send_video': "🎬 Tahlil uchun videoni yuboring! 📤\n\n/start — Bosh menyu",
    },
    'ru': {
        'choose_lang': "🌐 Tilni tanlang / Выберите язык:",
        'welcome': (
            "🩺 INSTADOKTOR — бот анализа Instagram\n\n"
            "Я честно и объективно анализирую ваши видео — "
            "не хвалю, а говорю правду. 💯\n\n"
            "🎬 Анализ видео — 3 990 сум\n"
            "• 🎣 Анализ хука\n"
            "• 🎥 Визуал и монтаж\n"
            "• 🎙️ Аудио и речь\n"
            "• 🎯 Вероятность в рекомендациях\n"
            "• ❌ Недостатки + 💡 советы\n\n"
            "📤 Отправьте ваше видео (до 20МБ):"
        ),
        'btn_video': "🎬 Анализ видео — 3 990 сум",
        'btn_profil': "📊 Анализ профиля — 5 990 сум",
        'video_info': (
            "🎬 Отправьте видео сюда — я сделаю полный анализ.\n\n"
            "📏 Пока видео должно быть меньше 20МБ.\n"
            "⚠️ Сейчас работает в бесплатном тестовом режиме!"
        ),
        'profil_info': "📊 Анализ профиля скоро заработает! 🔜",
        'received': "⏳ Видео получено! Начинаю анализ... ⚡",
        'too_big': "❌ Видео слишком большое (не более 20МБ). 📏\n\nПожалуйста, отправьте покороче.",
        'wrong_format': "❌ Не распознал формат. Отправьте MP4 или MOV. 📹",
        'uploading': "📤 Видео загружается...",
        'analyzing': "🧠 ИИ анализирует (визуал + аудио)... ⚡",
        'ready': "✅ Анализ готов!",
        'error': "😔 Извините, не удалось проанализировать. Отправьте видео ещё раз. 🔄",
        'send_video': "🎬 Отправьте видео для анализа! 📤\n\n/start — Главное меню",
    }
}


def get_lang(context):
    return context.user_data.get('lang', 'uz')


def t(context, key):
    return TEXTS[get_lang(context)][key]


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🇺🇿 O'zbekcha", callback_data='lang_uz')],
        [InlineKeyboardButton("🇷🇺 Русский", callback_data='lang_ru')],
    ]
    await update.message.reply_text(
        "🌐 Tilni tanlang / Выберите язык:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def show_menu(message, context):
    keyboard = [
        [InlineKeyboardButton(t(context, 'btn_video'), callback_data='video_info')],
        [InlineKeyboardButton(t(context, 'btn_profil'), callback_data='profil_info')],
    ]
    await message.reply_text(
        t(context, 'welcome'),
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == 'lang_uz':
        context.user_data['lang'] = 'uz'
        await show_menu(query.message, context)
    elif query.data == 'lang_ru':
        context.user_data['lang'] = 'ru'
        await show_menu(query.message, context)
    elif query.data == 'video_info':
        await query.message.reply_text(t(context, 'video_info'))
    elif query.data == 'profil_info':
        await query.message.reply_text(t(context, 'profil_info'))


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
    await update.message.reply_text(t(context, 'send_video'))


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, video_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    logger.info("Bot ishga tushdi!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
    
