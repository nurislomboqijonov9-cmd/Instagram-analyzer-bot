import os
import logging
import tempfile
import asyncio
import base64
import subprocess
from openai import OpenAI
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler

# === SOZLAMALAR ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

client = OpenAI(api_key=OPENAI_API_KEY)

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

Tahlilni professional, aniq va foydali qil."""


def extract_frames(video_path, num_frames=8):
    """Videodan kadrlar ajratib olish"""
    frames = []
    try:
        # Video davomiyligini olish
        result = subprocess.run([
            'ffprobe', '-v', 'quiet', '-print_format', 'json',
            '-show_format', video_path
        ], capture_output=True, text=True)
        
        import json
        data = json.loads(result.stdout)
        duration = float(data['format'].get('duration', 30))
        
        # Kadrlarni vaqt oralig'ida ajratib olish
        for i in range(num_frames):
            timestamp = (duration / num_frames) * i
            output_path = f'/tmp/frame_{i}.jpg'
            subprocess.run([
                'ffmpeg', '-ss', str(timestamp), '-i', video_path,
                '-vframes', '1', '-q:v', '3', '-s', '720x1280',
                output_path, '-y'
            ], capture_output=True)
            
            if os.path.exists(output_path):
                with open(output_path, 'rb') as f:
                    frame_data = base64.b64encode(f.read()).decode('utf-8')
                    frames.append(frame_data)
                os.unlink(output_path)
        
        logger.info(f"{len(frames)} ta kadr ajratildi")
    except Exception as e:
        logger.error(f"Kadr ajratishda xato: {e}")
    
    return frames


def extract_audio_text(video_path):
    """Videodan audio ajratib, matnga o'girish"""
    try:
        audio_path = '/tmp/audio_extract.mp3'
        subprocess.run([
            'ffmpeg', '-i', video_path, '-vn', '-acodec', 'mp3',
            '-ar', '16000', '-ac', '1', '-b:a', '64k',
            audio_path, '-y'
        ], capture_output=True)
        
        if os.path.exists(audio_path) and os.path.getsize(audio_path) > 1000:
            with open(audio_path, 'rb') as audio_file:
                # Whisper API orqali transkriptsiya
                transcript = client.audio.transcriptions.create(
                    model="whisper-1",
                    file=audio_file,
                    language="uz"  # O'zbek tili
                )
            os.unlink(audio_path)
            logger.info("Audio transkriptsiya muvaffaqiyatli")
            return transcript.text
    except Exception as e:
        logger.error(f"Audio tahlilida xato: {e}")
    
    return None


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
        
        with tempfile.NamedTemporaryFile(suffix=f'.{file_ext}', delete=False) as tmp_file:
            tmp_path = tmp_file.name
        
        await file.download_to_drive(tmp_path)
        await wait_msg.edit_text("🔍 Video kadrlar tahlil qilinmoqda...")
        
        # Kadrlarni ajratib olish
        frames = extract_frames(tmp_path, num_frames=8)
        
        # Audio transkriptsiya
        await wait_msg.edit_text("🎙️ Audio tahlil qilinmoqda...")
        audio_text = extract_audio_text(tmp_path)
        
        await wait_msg.edit_text("🧠 AI tahlil qilinmoqda...")
        
        # OpenAI Vision ga yuborish
        messages_content = [
            {"type": "text", "text": TAHLIL_PROMPT}
        ]
        
        # Kadrlarni qo'shish
        for frame in frames:
            messages_content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/jpeg;base64,{frame}",
                    "detail": "high"
                }
            })
        
        # Audio matni qo'shish
        if audio_text:
            messages_content.append({
                "type": "text",
                "text": f"\n\n🎙️ VIDEODAGI NUTQ MATNI:\n{audio_text}"
            })
        else:
            messages_content.append({
                "type": "text",
                "text": "\n\n🎙️ AUDIO: Videoda nutq aniqlanmadi yoki ovoz yo'q."
            })
        
        # GPT-4o ga yuborish
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "user", "content": messages_content}
            ],
            max_tokens=2000
        )
        
        tahlil = response.choices[0].message.content
        
        await wait_msg.edit_text("✅ Tahlil tayyor!")
        
        # Javobni yuborish
        if len(tahlil) <= 4000:
            await message.reply_text(tahlil)
        else:
            chunks = [tahlil[i:i+4000] for i in range(0, len(tahlil), 4000)]
            for i, chunk in enumerate(chunks):
                await message.reply_text(f"📋 *Qism {i+1}/{len(chunks)}*\n\n{chunk}", parse_mode='Markdown')
        
        os.unlink(tmp_path)
        
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
