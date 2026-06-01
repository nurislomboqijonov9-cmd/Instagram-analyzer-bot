import os
import logging
import tempfile
import time
from google import genai
from google.genai import errors as genai_errors
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

TAHLIL_PROMPT = """Sen tajribali, xolis Instagram kontent tahlilchisisan.
Vazifang — blogger videosini HALOL va OBJEKTIV baholash. Video qanday bo'lsa,
shunday bahola: yaxshi bo'lsa yaxshi de, kuchsiz bo'lsa kuchsiz de. Na ortiqcha
maqta, na asossiz tanqid qil. Faqat HAQIQATNI ayt.

QAT'IY QOIDALAR:
- Baho videoning haqiqiy sifatiga MOS bo'lsin — sun'iy past ham, ko'taringan
  maqtov ham emas. Adolatli bo'l.
- "Ajoyib", "zo'r", "wow" kabi so'zlarni faqat HAQIQATAN shunday bo'lsa ishlat.
- Har bir kamchilikni aniq ayt. Yumshatma, lekin bo'rttirma ham.
- Rekka chiqish ehtimolini REAL bahola — video nimaga loyiq bo'lsa, shu foizni ber.
- Bloggerga yoqish uchun emas, uni O'STIRISH uchun gapir. Halol baho — eng katta yordam.

Har bo'limni O'ZBEK tilida, emoji bilan chiroyli, lekin MAZMUNAN qattiq yoz:

🎣 *HOOK (0-3 sekund)*
Birinchi 3 sekund e'tiborni tortadimi? Aksariyat hooklar zaif — agar shunday bo'lsa,
to'g'ridan ayt. Ball: __/10

🎬 *VIZUAL VA MONTAJ*
Yoritish, kamera, kompozitsiya, montaj tezligi. Kamchiliklarni aniq ko'rsat. Ball: __/10

🗣️ *AUDIO VA NUTQ*
Nima gapirildi? Ovoz toni, nutq sifati. Sokin yoki zerikarli bo'lsa — ayt. Ball: __/10

📝 *KONTENT VA QIYMAT*
Xabar tushunarlimi? CTA bormi? Tomoshabin nima oladi? Ball: __/10

📊 *REKKA CHIQISH EHTIMOLI*
Real, adolatli foiz — video nimaga loyiq bo'lsa shuni ber. Sabablarini ayt.

✅ *KUCHLI TOMONLARI*
Faqat HAQIQIY kuchli tomonlar (bo'lmasa, "kam" deb ayt). Yolg'on maqtov yo'q.

❌ *KAMCHILIKLAR*
Eng muhim qism. Barcha jiddiy kamchiliklarni ro'yxatla. Yumshatma.

💡 *ANIQ TAVSIYALAR*
Keyingi video uchun 5 ta amaliy qadam. Umumiy gap emas — aniq harakat.

Esda tut: sen do'st emas, ekspertsan. Halol baho bloggerni o'stiradi, maqtov esa aldaydi."""


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🎬 Video tahlil — 3,990 so'm", callback_data='video_info')],
        [InlineKeyboardButton("📊 Profil tahlil — 5,990 so'm", callback_data='profil_info')],
    ]
    await update.message.reply_text(
        "🩺 *INSTADOKTOR — Instagram tahlil boti*\n\n"
        "Men sizning videolaringizni halol va qattiq tahlil qilaman — "
        "maqtov emas, haqiqat aytaman. 💯\n\n"
        "🎬 *Video tahlil* — 3,990 so'm\n"
        "• 🎣 Hook tahlili\n"
        "• 🎬 Vizual va montaj\n"
        "• 🗣️ Audio va nutq\n"
        "• 📊 Rekka chiqish ehtimoli\n"
        "• ❌ Kamchiliklar + 💡 tavsiyalar\n\n"
        "📤 Videongizni yuboring (20MB gacha):",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == 'video_info':
        await query.message.reply_text(
            "🎬 Videongizni shu yerga yuboring — men to'liq tahlil qilaman.\n\n"
            "📏 Hozircha video 20MB dan kichik bo'lsin.\n"
            "⚠️ Hozircha bepul sinov rejimida!"
        )
    elif query.data == 'profil_info':
        await query.message.reply_text("📊 Profil tahlil tez orada ishga tushadi! 🔜")


def analyze_with_retry(uploaded_file, max_retries=4):
    """503/server band bo'lsa avtomatik qayta urinish"""
    last_error = None
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=[uploaded_file, TAHLIL_PROMPT]
            )
            return response.text
        except genai_errors.APIError as e:
            last_error = e
            code = getattr(e, 'code', None)
            # 503 (band) yoki 429 (limit) bo'lsa kutib qayta urinamiz
            if code in (503, 429, 500):
                wait = (attempt + 1) * 5
                logger.warning(f"Server band ({code}), {wait}s kutib qayta urinish... ({attempt+1}/{max_retries})")
                time.sleep(wait)
                continue
            raise
    raise last_error


async def video_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    wait_msg = await message.reply_text("⏳ Video qabul qilindi! Tahlil boshlanmoqda...")

    tmp_path = None
    uploaded_file = None
    try:
        if message.video:
            video = message.video
        elif message.document and message.document.mime_type and 'video' in message.document.mime_type:
            video = message.document
        else:
            await wait_msg.edit_text("❌ Video formatini tanimadim. MP4 yoki MOV yuboring.")
            return

        if video.file_size and video.file_size > 20 * 1024 * 1024:
            await wait_msg.edit_text(
                "❌ Video juda katta (20MB dan oshmasligi kerak).\n\n"
                "📏 Iltimos, qisqaroq yoki sifatini biroz pasaytirib yuboring."
            )
            return

        file = await context.bot.get_file(video.file_id)
        with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False) as tmp_file:
            tmp_path = tmp_file.name
        await file.download_to_drive(tmp_path)

        await wait_msg.edit_text("📤 Video yuklanmoqda...")
        uploaded_file = client.files.upload(file=tmp_path)

        await wait_msg.edit_text("🧠 AI tahlil qilinmoqda (vizual + audio)...")
        waited = 0
        while uploaded_file.state.name == "PROCESSING" and waited < 120:
            time.sleep(3)
            waited += 3
            uploaded_file = client.files.get(name=uploaded_file.name)

        if uploaded_file.state.name == "FAILED":
            await wait_msg.edit_text("❌ Video qayta ishlanmadi. Boshqa video yuboring.")
            return

        # 503/429 ga qarshi qayta urinish bilan tahlil
        tahlil = analyze_with_retry(uploaded_file)

        await wait_msg.edit_text("✅ Tahlil tayyor!")

        if len(tahlil) <= 4000:
            await message.reply_text(tahlil, parse_mode='Markdown')
        else:
            chunks = [tahlil[i:i+4000] for i in range(0, len(tahlil), 4000)]
            for i, chunk in enumerate(chunks):
                await message.reply_text(chunk, parse_mode='Markdown')

    except genai_errors.APIError as e:
        code = getattr(e, 'code', None)
        logger.error(f"API xato: {e}")
        if code == 429:
            await wait_msg.edit_text(
                "⏳ Hozir juda ko'p so'rov bor. Iltimos, 1-2 daqiqadan keyin qayta urinib ko'ring."
            )
        elif code == 503:
            await wait_msg.edit_text(
                "⏳ AI server hozir band. Iltimos, bir oz kutib qayta yuboring."
            )
        else:
            await wait_msg.edit_text(f"❌ Xato yuz berdi. Qaytadan urinib ko'ring.")
    except Exception as e:
        logger.error(f"Xato: {e}")
        await wait_msg.edit_text(f"❌ Xato yuz berdi. Qaytadan urinib ko'ring.")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
        if uploaded_file:
            try:
                client.files.delete(name=uploaded_file.name)
            except:
                pass


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎬 Tahlil uchun videoni yuboring!\n\n/start — Bosh menyu")


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
    
