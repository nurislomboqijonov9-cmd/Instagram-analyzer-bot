import os
import logging
import tempfile
import base64
import anthropic
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler

# === SOZLAMALAR ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# === TAHLIL PROMPTI ===
TAHLIL_PROMPT = """Sen Instagram video tahlil qiluvchi mutaxassis AI assistantsan.
Yuborilgan videoni diqqat bilan ko'r va eshit, keyin quyidagilarni O'ZBEK tilida tahlil qil:

1. 🎣 HOOK TAHLILI (0-3 sekund)
   - Birinchi 3 sekund qanchalik e'tiborni tortadi?
   - Tomoshabin davom ettirib ko'rishi ehtimoli?
   - Hook kuchli yoki zaifmi va nima uchun?

2. 🎬 VIZUAL SIFAT
   - Video sifati: yoritish, kamera barqarorligi, kompozitsiya
   - Montaj va dinamika
   - Professional darajasi

3. 🗣️ AUDIO VA NUTQ
   - Ovoz toni va ishonch darajasi
   - Asosiy xabar nima?
   - Fon shovqini yoki musiqa

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
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "🤖 *Instagram Analyzer Bot ga xush kelibsiz!*\n\n"
        "Bu bot sizning Instagram videolaringizni Claude AI yordamida professional tahlil qiladi.\n\n"
        "📌 *Xizmatlar:*\n"
        "🎬 *Video tahlil* — 3,990 so'm\n"
        "• Hook, vizual, audio, kontent tahlili\n"
        "• Rekka chiqish ehtimoli\n"
        "• Kamchiliklar va tavsiyalar\n\n"
        "📊 *Profil tahlil* — 5,990 so'm\n"
        "• Tez orada!\n\n"
        "👇 Boshlash uchun videoni yuboring:",
        parse_mode='Markdown',
        reply_markup=reply_markup
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == 'video_info':
        await query.message.reply_text(
            "🎬 *Video tahlil*\n\n"
            "Videongizni shu yerga yuboring — Claude AI tahlil qiladi.\n\n"
            "⚠️ Hozircha *bepul* sinov rejimida ishlayapti!",
            parse_mode='Markdown'
        )
    elif query.data == 'profil_info':
        await query.message.reply_text(
            "📊 *Profil tahlil*\n\n"
            "Bu xizmat tez orada ishga tushadi!\n"
            "Hozircha video tahlildan foydalaning.",
            parse_mode='Markdown'
        )


async def video_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message

    wait_msg = await message.reply_text(
        "⏳ Video qabul qilindi! Tahlil boshlanmoqda...\n"
        "Bu 30-90 sekund vaqt olishi mumkin."
    )

    try:
        # Video faylni aniqlash
        if message.video:
            video = message.video
        elif message.document and message.document.mime_type and 'video' in message.document.mime_type:
            video = message.document
        else:
            await wait_msg.edit_text("❌ Video formatini tanib olmadim. MP4 yoki MOV formatida yuboring.")
            return

        # Hajm tekshirish (Claude limiti ~30MB base64)
        if video.file_size and video.file_size > 20 * 1024 * 1024:
            await wait_msg.edit_text(
                "❌ Video juda katta (20MB dan oshmasligi kerak).\n"
                "Iltimos, qisqaroq yoki kichikroq video yuboring."
            )
            return

        file = await context.bot.get_file(video.file_id)

        with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False) as tmp_file:
            tmp_path = tmp_file.name

        await file.download_to_drive(tmp_path)
        await wait_msg.edit_text("🧠 Claude AI video tahlil qilinmoqda...")

        # Videoni base64 ga o'girish
        with open(tmp_path, 'rb') as f:
            video_data = base64.standard_b64encode(f.read()).decode('utf-8')

        # Claude API ga yuborish
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2000,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "video/mp4",
                            "data": video_data
                        }
                    },
                    {"type": "text", "text": TAHLIL_PROMPT}
                ]
            }]
        )

        tahlil = response.content[0].text
        await wait_msg.edit_text("✅ Tahlil tayyor!")

        if len(tahlil) <= 4000:
            await message.reply_text(tahlil)
        else:
            chunks = [tahlil[i:i+4000] for i in range(0, len(tahlil), 4000)]
            for i, chunk in enumerate(chunks):
                await message.reply_text(f"📋 Qism {i+1}/{len(chunks)}\n\n{chunk}")

        os.unlink(tmp_path)

    except Exception as e:
        logger.error(f"Xato: {e}")
        await wait_msg.edit_text(
            f"❌ Xato yuz berdi. Qaytadan urinib ko'ring.\n\nXato: {str(e)[:300]}"
        )


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎬 Tahlil qilish uchun videoni yuboring!\n\n/start — Bosh menyu"
    )


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
    
