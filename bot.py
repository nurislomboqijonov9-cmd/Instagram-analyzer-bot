import os
import logging
import tempfile
import base64
import subprocess
import json
import anthropic
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TAHLIL_PROMPT = """Sen Instagram video tahlil qiluvchi mutaxassis AI assistantsan.
Video kadrlarini diqqat bilan ko'r va quyidagilarni O'ZBEK tilida tahlil qil:

1. 🎣 HOOK TAHLILI (0-3 sekund)
   - Birinchi kadr qanchalik e'tiborni tortadi?
   - Tomoshabin davom ettirib ko'rishi ehtimoli?
   - Hook kuchli yoki zaifmi va nima uchun?

2. 🎬 VIZUAL SIFAT
   - Yoritish, kamera barqarorligi, kompozitsiya
   - Montaj va dinamika
   - Professional darajasi

3. 🗣️ AUDIO VA NUTQ
   - Videoda nima ko'rsatilgan va aytilgan deb o'ylaysan?
   - Asosiy xabar nima?

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


def extract_frames(video_path, num_frames=8):
    frames = []
    try:
        result = subprocess.run([
            'ffprobe', '-v', 'quiet', '-print_format', 'json',
            '-show_format', video_path
        ], capture_output=True, text=True)
        data = json.loads(result.stdout)
        duration = float(data['format'].get('duration', 30))

        for i in range(num_frames):
            timestamp = (duration / num_frames) * i + 0.3
            output_path = f'/tmp/frame_{i}.jpg'
            subprocess.run([
                'ffmpeg', '-ss', str(timestamp), '-i', video_path,
                '-vframes', '1', '-q:v', '4', '-vf', 'scale=640:-1',
                output_path, '-y'
            ], capture_output=True)
            if os.path.exists(output_path):
                with open(output_path, 'rb') as f:
                    frames.append(base64.standard_b64encode(f.read()).decode('utf-8'))
                os.unlink(output_path)
        logger.info(f"{len(frames)} ta kadr ajratildi")
    except Exception as e:
        logger.error(f"Kadr ajratishda xato: {e}")
    return frames


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🎬 Video tahlil — 3,990 so'm", callback_data='video_info')],
        [InlineKeyboardButton("📊 Profil tahlil — 5,990 so'm", callback_data='profil_info')],
    ]
    await update.message.reply_text(
        "🤖 *Instagram Analyzer Bot ga xush kelibsiz!*\n\n"
        "Bu bot videolaringizni Claude AI yordamida professional tahlil qiladi.\n\n"
        "🎬 *Video tahlil* — 3,990 so'm\n"
        "• Hook, vizual, kontent tahlili\n"
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
            "🎬 Videongizni shu yerga yuboring — Claude AI tahlil qiladi.\n\n"
            "⚠️ Hozircha bepul sinov rejimida!"
        )
    elif query.data == 'profil_info':
        await query.message.reply_text("📊 Profil tahlil tez orada ishga tushadi!")


async def video_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    wait_msg = await message.reply_text("⏳ Video qabul qilindi! Tahlil boshlanmoqda...")

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

        await wait_msg.edit_text("🔍 Video kadrlar ajratilmoqda...")
        frames = extract_frames(tmp_path, num_frames=8)

        if not frames:
            await wait_msg.edit_text("❌ Videodan kadr ajratib bo'lmadi. Boshqa video yuboring.")
            os.unlink(tmp_path)
            return

        await wait_msg.edit_text("🧠 Claude AI tahlil qilinmoqda...")

        content = [{"type": "text", "text": TAHLIL_PROMPT}]
        for i, frame in enumerate(frames):
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": frame}
            })

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2000,
            messages=[{"role": "user", "content": content}]
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
        await wait_msg.edit_text(f"❌ Xato yuz berdi.\n\n{str(e)[:300]}")


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
