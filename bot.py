import os
import logging
import tempfile
import asyncio
import google.generativeai as genai
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler

# === SOZLAMALAR ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# Gemini sozlash
genai.configure(api_key=GEMINI_API_KEY)

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# === TAHLIL PROMPTI ===
TAHLIL_PROMPT = """Sen Instagram video tahlil qiluvchi mutaxassis AI assistantsan. 
Seni vazifang — blogger yuborgan videoni to'liq professional tahlil qilish.

Quyidagi bo'limlarni O'ZBEK tilida batafsil tahlil qil:

1. 🎣 HOOK TAHLILI (0-3 sekund)
   - Birinchi kadr qanchalik e'tiborni tortadi?
   - Tomoshabin davom ettirib ko'rishi ehtimoli?
   - Hook kuchli yoki zaifmi va nima uchun?

2. 🎬 VIZUAL SIFAT
   - Video sifati (yoritish, kamera barqarorligi, kompozitsiya)
   - Montaj va dinamika
   - Umumiy professional darajasi

3. 🗣️ AUDIO VA NUTQ
   - Ovoz toni va ishonch darajasi
   - Nima haqida gapirgan (agar ovoz bo'lsa)
   - Fon shovqini, musiqa

4. 📝 KONTENT SIFATI
   - Xabar aniq va tushunarli ekanmi?
   - CTA (chaqiruv) bor yoki yo'q?
   - Qiymat bermoqdami tomoshabinga?

5. 📊 REKKA CHIQISH EHTIMOLI
   - 0-100% oralig'ida baho ber
   - Asosiy sabablarni ayt

6. ✅ KUCHLI TOMONLARI
   - 3-5 ta aniq kuchli joy

7. ❌ KAMCHILIKLAR
   - 3-5 ta aniq kamchilik

8. 💡 TAVSIYALAR
   - Keyingi videoni yaxshilash uchun 5 ta aniq qadam

Tahlilni professional, aniq va foydali qil. Ortiqcha maqtov yoki tanqiddan saqlaning."""


# === START KOMANDASI ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🎬 Video tahlil — 1,990 so'm", callback_data='video_info')],
        [InlineKeyboardButton("📊 Profil tahlil — 3,490 so'm", callback_data='profil_info')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "🤖 *Instagram Analyzer Bot ga xush kelibsiz!*\n\n"
        "Bu bot sizning Instagram videolaringizni sun'iy intellekt yordamida tahlil qiladi.\n\n"
        "📌 *Xizmatlar:*\n"
        "🎬 *Video tahlil* — 1,990 so'm\n"
        "• Hook, vizual, audio, kontent tahlili\n"
        "• Rekka chiqish ehtimoli\n"
        "• Kamchiliklar va tavsiyalar\n\n"
        "📊 *Profil tahlil* — 3,490 so'm\n"
        "• Tez orada!\n\n"
        "👇 Boshlash uchun videoni yuboring:",
        parse_mode='Markdown',
        reply_markup=reply_markup
    )


# === TUGMA BOSILGANDA ===
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == 'video_info':
        await query.message.reply_text(
            "🎬 *Video tahlil*\n\n"
            "Videongizni shu yerga yuboring — bot tahlil qiladi.\n\n"
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


# === VIDEO QABUL QILISH ===
async def video_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    
    # Foydalanuvchiga xabar
    wait_msg = await message.reply_text(
        "⏳ Video qabul qilindi! Tahlil boshlanmoqda...\n"
        "Bu 30-60 sekund vaqt olishi mumkin."
    )
    
    try:
        # Video yuklab olish
        if message.video:
            file = await context.bot.get_file(message.video.file_id)
            file_ext = "mp4"
        elif message.document and message.document.mime_type and 'video' in message.document.mime_type:
            file = await context.bot.get_file(message.document.file_id)
            file_ext = "mp4"
        else:
            await wait_msg.edit_text("❌ Video formatini tanib olmadim. MP4 yoki MOV formatida yuboring.")
            return
        
        # Vaqtinchalik fayl saqlash
        with tempfile.NamedTemporaryFile(suffix=f'.{file_ext}', delete=False) as tmp_file:
            tmp_path = tmp_file.name
        
        await file.download_to_drive(tmp_path)
        
        await wait_msg.edit_text("🔍 Video tahlil qilinmoqda... (Gemini AI ishlamoqda)")
        
        # Gemini ga yuborish
        model = genai.GenerativeModel('gemini-2.0-flash')
        
        # Video faylni yuklash
        video_file = genai.upload_file(tmp_path, mime_type=f'video/{file_ext}')
        
        # Fayl tayyor bo'lishini kutish
        while video_file.state.name == "PROCESSING":
            await asyncio.sleep(2)
            video_file = genai.get_file(video_file.name)
        
        if video_file.state.name == "FAILED":
            await wait_msg.edit_text("❌ Video yuklanmadi. Qaytadan urinib ko'ring.")
            return
        
        # Tahlil so'rash
        response = model.generate_content([TAHLIL_PROMPT, video_file])
        
        tahlil = response.text
        
        # Javobni yuborish (agar uzun bo'lsa bo'laklarga bo'lish)
        await wait_msg.edit_text("✅ Tahlil tayyor!")
        
        if len(tahlil) <= 4000:
            await message.reply_text(tahlil)
        else:
            # Uzun javobni bo'laklarga bo'lish
            chunks = [tahlil[i:i+4000] for i in range(0, len(tahlil), 4000)]
            for i, chunk in enumerate(chunks):
                await message.reply_text(f"📋 *Qism {i+1}/{len(chunks)}*\n\n{chunk}", parse_mode='Markdown')
        
        # Faylni o'chirish
        os.unlink(tmp_path)
        genai.delete_file(video_file.name)
        
    except Exception as e:
        logger.error(f"Xato: {e}")
        await wait_msg.edit_text(
            f"❌ Xato yuz berdi. Qaytadan urinib ko'ring.\n\nXato: {str(e)[:200]}"
        )


# === MATN XABARLARI ===
async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎬 Tahlil qilish uchun videoni yuboring!\n\n"
        "/start — Bosh menyu"
    )


# === ASOSIY FUNKSIYA ===
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
