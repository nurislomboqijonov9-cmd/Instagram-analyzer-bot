import os
import logging
import tempfile
import time
from google import genai
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# Yangi GenAI client
client = genai.Client(api_key=GEMINI_API_KEY)

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TAHLIL_PROMPT = """Sen Instagram video tahlil qiluvchi mutaxassis AI assistantsan.
Videoni TO'LIQ ko'r va eshit — vizualni ham, ovozni ham, harakatni ham, montajni ham.
Keyin quyidagilarni O'ZBEK tilida batafsil tahlil qil:

1. 🎣 HOOK TAHLILI (0-3 sekund)
   - Birinchi 3 sekund qanchalik e'tiborni tortadi?
   - Tomoshabin davom ettirib ko'rishi ehtimoli?
   - Hook kuchli yoki zaifmi va nima uchun?

2. 🎬 VIZUAL VA MONTAJ
   - Yoritish, kamera barqarorligi, kompozitsiya
   - Montaj tezligi va dinamikasi
   - Sahna o'zgarishlari, effektlar
   - Professional darajasi

3. 🗣️ AUDIO VA NUTQ
   - Videoda nima gapirildi? (asosiy gaplar)
   - Ovoz toni va ishonch darajasi
   - Fon musiqasi yoki shovqin
   - Nutq sifati va tezligi

4. 📝 KONTENT SIFATI
   - Xabar aniq va tushunarli ekanmi?
   - CTA (chaqiruv) bor yoki yo'q?
   - Tomoshabinga qiymat bermoqdami?

5. 📊 REKKA CHIQISH EHTIMOLI
   - 0-100% oralig'ida baho ber
   - Asosiy sabablarni ayt

6. ✅ KUCHLI TOMONLARI (3-5 ta)

7. ❌ KAMCHILIKLAR (3-5 ta)

8. 💡 TAVSIYALAR (5 ta aniq qadam)

Tahlilni professional, aniq va foydali qil."""


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🎬 Video tahlil — 3,990 so'm", callback_data='video_info')],
        [InlineKeyboardButton("📊 Profil tahlil — 5,990 so'm", callback_data='profil_info')],
    ]
    await update.message.reply_text(
        "🤖 *Instagram Analyzer Bot ga xush kelibsiz!*\n\n"
        "Bu bot videolaringizni sun'iy intellekt yordamida to'liq tahlil qiladi — "
        "vizual, audio, montaj va harakatigacha.\n\n"
        "🎬 *Video tahlil* — 3,990 so'm\n"
        "• Hook, vizual, audio, montaj tahlili\n"
        "• Rekka chiqish ehtimoli\n"
        "• Kamchiliklar va tavsiyalar\n\n"
        "👇 Boshlash uchun videoni yuboring:",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == 'video_info':
        await query.message.reply_text(
            "🎬 Videongizni shu yerga yuboring — AI to'liq tahlil qiladi.\n\n"
            "⚠️ Hozircha bepul sinov rejimida!"
        )
    elif query.data == 'profil_info':
        await query.message.reply_text("📊 Profil tahlil tez orada ishga tushadi!")


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

        file = await context.bot.get_file(video.file_id)
        with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False) as tmp_file:
            tmp_path = tmp_file.name
        await file.download_to_drive(tmp_path)

        await wait_msg.edit_text("📤 Video yuklanmoqda...")

        # Yangi SDK: faylni yuklash
        uploaded_file = client.files.upload(file=tmp_path)

        # Fayl tayyor bo'lishini kutish
        await wait_msg.edit_text("🧠 AI video tahlil qilinmoqda (vizual + audio)...")
        waited = 0
        while uploaded_file.state.name == "PROCESSING" and waited < 120:
            time.sleep(3)
            waited += 3
            uploaded_file = client.files.get(name=uploaded_file.name)

        if uploaded_file.state.name == "FAILED":
            await wait_msg.edit_text("❌ Video qayta ishlanmadi. Boshqa video yuboring.")
            return

        # Yangi SDK: kontent generatsiya
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[uploaded_file, TAHLIL_PROMPT]
        )

        tahlil = response.text
        await wait_msg.edit_text("✅ Tahlil tayyor!")

        if len(tahlil) <= 4000:
            await message.reply_text(tahlil)
        else:
            chunks = [tahlil[i:i+4000] for i in range(0, len(tahlil), 4000)]
            for i, chunk in enumerate(chunks):
                await message.reply_text(f"📋 Qism {i+1}/{len(chunks)}\n\n{chunk}")

    except Exception as e:
        logger.error(f"Xato: {e}")
        await wait_msg.edit_text(f"❌ Xato yuz berdi.\n\n{str(e)[:300]}")
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
