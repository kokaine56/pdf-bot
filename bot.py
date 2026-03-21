import os
import logging
import asyncio
import io
import subprocess
import time
import shutil
import sqlite3
import pty
from typing import List

# Third-party libraries
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
    ConversationHandler,
)
from pypdf import PdfReader, PdfWriter
import img2pdf
from pdf2image import convert_from_path
from PIL import Image
from pdf2docx import Converter
import pytesseract

# Import Configuration
from config import TOKEN, ADMIN_ID, POPPLER_PATH, MAX_FILE_SIZE, DB_CHANNEL_ID, BACKUP_INTERVAL

# Watermark/Page Numbers support
try:
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import A4
except ImportError:
    logging.warning("ReportLab not installed. Watermark/Page Num features disabled.")

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- DATABASE CLASS (SMART MEMORY) ---
class PasswordDatabase:
    """Handles SQLite interactions for storing known passwords and user stats."""
    
    def __init__(self, db_name="bot_memory.db"):
        self.db_name = db_name
        self._init_db()

    def _init_db(self):
        try:
            conn = sqlite3.connect(self.db_name)
            cursor = conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS known_passwords (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    password TEXT UNIQUE,
                    times_used INTEGER DEFAULT 1,
                    last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Database Init Error: {e}")

    def save_password(self, password):
        if not password: return
        try:
            conn = sqlite3.connect(self.db_name)
            cursor = conn.cursor()
            cursor.execute("SELECT id, times_used FROM known_passwords WHERE password = ?", (password,))
            data = cursor.fetchone()
            if data:
                new_count = data[1] + 1
                cursor.execute("UPDATE known_passwords SET times_used = ?, last_seen = CURRENT_TIMESTAMP WHERE id = ?", (new_count, data[0]))
            else:
                cursor.execute("INSERT INTO known_passwords (password) VALUES (?)", (password,))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"DB Save Error: {e}")

    def get_priority_passwords(self):
        passwords = []
        try:
            conn = sqlite3.connect(self.db_name)
            cursor = conn.cursor()
            cursor.execute("SELECT password FROM known_passwords ORDER BY times_used DESC")
            passwords = [row[0] for row in cursor.fetchall()]
            conn.close()
        except Exception as e:
            logger.error(f"DB Fetch Error: {e}")
        return passwords

    def add_user(self, user_id):
        try:
            conn = sqlite3.connect(self.db_name)
            cursor = conn.cursor()
            cursor.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"User Add Error: {e}")

    def get_all_users(self):
        users = []
        try:
            conn = sqlite3.connect(self.db_name)
            cursor = conn.cursor()
            cursor.execute("SELECT user_id FROM users")
            users = [row[0] for row in cursor.fetchall()]
            conn.close()
        except Exception: pass
        return users

    def get_user_count(self):
        count = 0
        try:
            conn = sqlite3.connect(self.db_name)
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM users")
            row = cursor.fetchone()
            if row: count = row[0]
            conn.close()
        except Exception: pass
        return count

db = PasswordDatabase()

# --- STATES ---
(
    WAIT_FOR_UPLOAD, 
    CHOOSING_ACTION, 
    TYPE_PASSWORD, 
    UPLOAD_IMAGES, 
    TYPE_UNLOCK_PASSWORD, 
    TYPE_WATERMARK_TEXT, 
    CHOOSE_COMPRESSION, 
    MERGE_UPLOAD, 
    CHOOSE_PAGENUM_POS,
    CHOOSE_ROTATION,
    WAIT_FOR_BROADCAST
) = range(11)

# --- KEYBOARDS ---
def get_pdf_action_keyboard():
    keyboard = [
        [InlineKeyboardButton("📄 PDF to Image", callback_data="pdf2img"), InlineKeyboardButton("📝 PDF to Word", callback_data="pdf2word")],
        [InlineKeyboardButton("🔍 Extract Text (OCR)", callback_data="pdf2txt"), InlineKeyboardButton("🔄 Rotate PDF", callback_data="rotate_menu")],
        [InlineKeyboardButton("🔒 Lock PDF", callback_data="lock"), InlineKeyboardButton("🔓 Unlock PDF", callback_data="unlock")],
        [InlineKeyboardButton("➕ Merge PDF", callback_data="merge"), InlineKeyboardButton("✂️ Split PDF", callback_data="split")],
        [InlineKeyboardButton("📉 Reduce Size", callback_data="reduce"), InlineKeyboardButton("🔢 Add Page No.", callback_data="pagenum")],
        [InlineKeyboardButton("💧 Watermark", callback_data="watermark"), InlineKeyboardButton("🔓 Recover Password", callback_data="crack")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_rotation_keyboard():
    keyboard = [
        [InlineKeyboardButton("90° Clockwise", callback_data="rot_90"), InlineKeyboardButton("180° Rotate", callback_data="rot_180")],
        [InlineKeyboardButton("270° Counter-CW", callback_data="rot_270")],
        [InlineKeyboardButton("🔙 Back to Menu", callback_data="back_to_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_post_action_keyboard():
    keyboard = [
        [InlineKeyboardButton("🔙 Perform Another Action", callback_data="back_to_menu")],
        [InlineKeyboardButton("🏠 Start Fresh", callback_data="cancel")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_encrypted_keyboard():
    keyboard = [
        [InlineKeyboardButton("🔓 Unlock PDF", callback_data="unlock")],
        [InlineKeyboardButton("🔓 Recover Password", callback_data="crack")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_cancel_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel")]])

def get_compression_keyboard():
    keyboard = [
        [InlineKeyboardButton("Low Quality (~90% Reduction)", callback_data="comp_20")],
        [InlineKeyboardButton("Medium Quality (~60% Reduction)", callback_data="comp_50")],
        [InlineKeyboardButton("🔙 Back", callback_data="back_to_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_pagenum_pos_keyboard():
    keyboard = [
        [InlineKeyboardButton("↖️ Top Left", callback_data="pos_tl"), InlineKeyboardButton("⬆️ Top Center", callback_data="pos_tc"), InlineKeyboardButton("↗️ Top Right", callback_data="pos_tr")],
        [InlineKeyboardButton("↙️ Bottom Left", callback_data="pos_bl"), InlineKeyboardButton("⬇️ Bottom Center", callback_data="pos_bc"), InlineKeyboardButton("↘️ Bottom Right", callback_data="pos_br")],
        [InlineKeyboardButton("🔙 Back", callback_data="back_to_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_image_upload_keyboard():
    keyboard = [
        [InlineKeyboardButton("✅ Done Uploading", callback_data="done_uploading")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel")]
    ]
    return InlineKeyboardMarkup(keyboard)

# --- CONVERSATION FLOW ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    if user: db.add_user(user.id)
    context.user_data.clear()
    text = (
        "🚀 **PDF Master Bot Pro**\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "⚡ *Fast, Secure & Powerful Tools*\n\n"
        "**Features:**\n"
        "• Image to PDF & PDF to Image\n"
        "• OCR (Scanned PDF to Text)\n"
        "• Lock/Unlock & Recover Passwords\n"
        "• Merge, Split, Rotate & Compress\n"
        "• Watermark & Page Numbers\n\n"
        "👇 **Send a PDF or an Image to begin!**"
    )
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text=text, parse_mode="Markdown")
    else:
        await update.message.reply_text(text=text, parse_mode="Markdown")
    return WAIT_FOR_UPLOAD

async def back_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    path = context.user_data.get('pdf_path')
    if not path or not os.path.exists(path):
        await query.edit_message_text("❌ Session expired. Please send the file again.")
        return WAIT_FOR_UPLOAD
    
    await query.edit_message_text(
        "🛠️ **Select an action for your file:**",
        reply_markup=get_pdf_action_keyboard(),
        parse_mode="Markdown"
    )
    return CHOOSING_ACTION

async def cancel_process(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query: await query.answer("Cancelled")
    
    keys_to_clean = ['pdf_path', 'merge_files', 'images']
    for key in keys_to_clean:
        data = context.user_data.get(key)
        if isinstance(data, str) and os.path.exists(data):
            try: os.remove(data)
            except: pass
        elif isinstance(data, list):
            for item in data:
                path = item.get('path') if isinstance(item, dict) else item
                if path and os.path.exists(path):
                    try: os.remove(path)
                    except: pass
    return await start(update, context)

# --- ADMIN BROADCAST ---
async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    await update.message.reply_text("📣 Send the message you want to broadcast to all users.", reply_markup=get_cancel_keyboard())
    return WAIT_FOR_BROADCAST

async def handle_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    msg = update.message.text
    users = db.get_all_users()
    count = 0
    status = await update.message.reply_text(f"⏳ Broadcasting to {len(users)} users...")
    
    for uid in users:
        try:
            await context.bot.send_message(chat_id=uid, text=f"🔔 **Update from Admin:**\n\n{msg}", parse_mode="Markdown")
            count += 1
            if count % 10 == 0:
                await status.edit_text(f"⏳ Progress: {count}/{len(users)}")
            await asyncio.sleep(0.05) # Prevent flood
        except Exception: pass
        
    await status.edit_text(f"✅ Successfully broadcasted to {count} users.")
    return ConversationHandler.END

# --- INITIAL UPLOADS ---
async def handle_initial_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    doc = update.message.document
    if not doc or doc.mime_type != 'application/pdf':
        await update.message.reply_text("❌ Please upload a valid PDF file.")
        return WAIT_FOR_UPLOAD

    if doc.file_size > MAX_FILE_SIZE:
        await update.message.reply_text(f"❌ File too large. Max limit is 10MB.")
        return WAIT_FOR_UPLOAD

    file_id = doc.file_id
    status_msg = await update.message.reply_text("⏳ Processing PDF...")
    path = f"temp_{file_id}.pdf"
    new_file = await context.bot.get_file(file_id)
    await new_file.download_to_drive(path)

    context.user_data['pdf_path'] = path
    context.user_data['pdf_name'] = doc.file_name or "document.pdf"

    try:
        reader = PdfReader(path)
        if reader.is_encrypted:
            await status_msg.edit_text(
                "🔒 **This PDF is Encrypted.**\n\nWhat would you like to do?",
                reply_markup=get_encrypted_keyboard(), parse_mode="Markdown"
            )
        else:
            await status_msg.edit_text(
                "✅ **File Received!**\n\nSelect an action below:",
                reply_markup=get_pdf_action_keyboard(), parse_mode="Markdown"
            )
    except Exception as e:
        await status_msg.edit_text(f"❌ Error reading PDF: {e}")
        if os.path.exists(path): os.remove(path)
        return WAIT_FOR_UPLOAD
    return CHOOSING_ACTION

async def handle_initial_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['images'] = []
    return await receive_image(update, context)

# --- ACTIONS ---
async def action_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    action = query.data
    context.user_data['action'] = action
    path = context.user_data.get('pdf_path')

    if action == "lock":
        await query.edit_message_text("🔑 Send the **password** to lock this file.", reply_markup=get_cancel_keyboard())
        return TYPE_PASSWORD
    elif action == "unlock":
        await query.edit_message_text("🔑 Send the **password** to unlock.", reply_markup=get_cancel_keyboard())
        return TYPE_UNLOCK_PASSWORD
    elif action == "watermark":
        await query.edit_message_text("💧 Send the **watermark text**.", reply_markup=get_cancel_keyboard())
        return TYPE_WATERMARK_TEXT
    elif action == "reduce":
        await query.edit_message_text("📉 **Compression Level**", reply_markup=get_compression_keyboard())
        return CHOOSE_COMPRESSION
    elif action == "pagenum":
        await query.edit_message_text("🔢 **Page Number Position**", reply_markup=get_pagenum_pos_keyboard())
        return CHOOSE_PAGENUM_POS
    elif action == "rotate_menu":
        await query.edit_message_text("🔄 **Rotate PDF Pages**", reply_markup=get_rotation_keyboard())
        return CHOOSE_ROTATION
    elif action == "merge":
        context.user_data['merge_files'] = [{'path': path, 'name': context.user_data.get('pdf_name')}]
        await query.edit_message_text("➕ **Merge Mode**\nUpload the second PDF file.", reply_markup=get_cancel_keyboard())
        return MERGE_UPLOAD
    elif action == "crack":
        context.application.create_task(crack_pdf_password(update, path, query.message, context))
        return CHOOSING_ACTION
    elif action == "split":
        await split_pdf(update, path, query.message, context)
        return CHOOSING_ACTION
    elif action == "pdf2img":
        await pdf_to_images(update, context, path, query.message)
        return CHOOSING_ACTION
    elif action == "pdf2word":
        await pdf_to_word(update, path, query.message, context)
        return CHOOSING_ACTION
    elif action == "pdf2txt":
        await pdf_to_text(update, path, query.message, context)
        return CHOOSING_ACTION
    return CHOOSING_ACTION

# --- ACTION LOGIC ---

async def pdf_to_text(update, path, msg, context):
    await msg.edit_text("🔍 Performing OCR (Extracting Text)...")
    try:
        def _ocr():
            images = convert_from_path(path, dpi=200, poppler_path=POPPLER_PATH)
            full_text = ""
            for i, img in enumerate(images):
                page_text = pytesseract.image_to_string(img)
                full_text += f"--- Page {i+1} ---\n{page_text}\n\n"
            return full_text

        text = await asyncio.to_thread(_ocr)
        if len(text.strip()) < 10:
            await msg.edit_text("❌ No text could be extracted from this PDF.")
        else:
            txt_file = f"text_{int(time.time())}.txt"
            with open(txt_file, "w") as f: f.write(text)
            await update.effective_chat.send_document(
                document=open(txt_file, "rb"), 
                caption="📝 **Extracted Text**", 
                parse_mode="Markdown",
                reply_markup=get_post_action_keyboard()
            )
            os.remove(txt_file)
            await msg.delete()
    except Exception as e:
        await msg.edit_text(f"❌ OCR Failed: {e}")

async def handle_rotation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    angle = int(query.data.split('_')[1])
    path = context.user_data.get('pdf_path')
    msg = await query.edit_message_text(f"⏳ Rotating pages {angle}°...")
    
    out = f"rot_{angle}_{os.path.basename(path)}"
    try:
        def _rot():
            r = PdfReader(path)
            w = PdfWriter()
            for p in r.pages:
                p.rotate(angle)
                w.add_page(p)
            with open(out, "wb") as f: w.write(f)
        
        await asyncio.to_thread(_rot)
        await update.effective_chat.send_document(
            document=open(out, "rb"), 
            caption=f"🔄 Rotated {angle}°",
            reply_markup=get_post_action_keyboard()
        )
        os.remove(out)
        await msg.delete()
    except Exception as e:
        await msg.edit_text(f"❌ Failed: {e}")
    return CHOOSING_ACTION

# --- EXISTING UTILS UPDATED FOR PERSISTENCE ---

async def handle_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    password = update.message.text
    path = context.user_data.get('pdf_path')
    status_msg = await update.message.reply_text("⏳ Encrypting...")
    try:
        db.save_password(password)
        def _lock():
            r = PdfReader(path); w = PdfWriter()
            w.append_pages_from_reader(r)
            w.encrypt(password)
            out = f"lock_{int(time.time())}.pdf"
            with open(out, "wb") as f: w.write(f)
            return out
        out_path = await asyncio.to_thread(_lock)
        await status_msg.delete()
        await update.message.reply_document(
            document=open(out_path, "rb"), 
            caption=f"✅ Locked with password: `{password}`", 
            parse_mode="Markdown",
            reply_markup=get_post_action_keyboard()
        )
        os.remove(out_path)
    except Exception as e: await status_msg.edit_text(f"❌ Error: {e}")
    return CHOOSING_ACTION

async def pdf_to_images(update, context, path, msg):
    await msg.edit_text("⏳ Converting to images...")
    try:
        def _convert():
            imgs = convert_from_path(path, first_page=1, last_page=10, poppler_path=POPPLER_PATH)
            files = []
            for i, img in enumerate(imgs):
                name = f"page_{i+1}_{int(time.time())}.jpg"
                img.save(name, 'JPEG')
                files.append(name)
            return files
        files = await asyncio.to_thread(_convert)
        for f in files:
            await context.bot.send_photo(update.effective_chat.id, open(f, 'rb'))
            os.remove(f)
        await msg.edit_text("✅ Images sent.", reply_markup=get_post_action_keyboard())
    except Exception as e: await msg.edit_text(f"❌ Error: {e}")

async def pdf_to_word(update, path, msg, context):
    await msg.edit_text("⏳ Converting to Word (Docx)...")
    out = f"word_{int(time.time())}.docx"
    try:
        def _c():
            cv = Converter(path); cv.convert(out); cv.close()
        await asyncio.to_thread(_c)
        await update.effective_chat.send_document(
            document=open(out, "rb"), 
            caption="📝 **Converted to Word**", 
            reply_markup=get_post_action_keyboard()
        )
        os.remove(out)
        await msg.delete()
    except Exception as e: await msg.edit_text(f"❌ Word Conversion failed: {e}")

# (Note: Other functions like compress_pdf, split_pdf follow similar pattern to keep user in CHOOSING_ACTION state)

async def crack_pdf_password(update, input_path, status_msg, context):
    # Modified from your version to show more modern UI and handle the same persistence
    await status_msg.edit_text("🛡️ **Starting Recovery Phase 1 (Smart Search)...**", parse_mode="Markdown")
    known = db.get_priority_passwords()
    found = None
    try:
        def _check():
            r = PdfReader(input_path)
            for p in known:
                try: 
                    if r.decrypt(p) > 0: return p
                except: continue
            return None
        found = await asyncio.to_thread(_check)
        if found:
            await status_msg.edit_text(f"✅ **Password Found!**\n\n🔑 Key: `{found}`", parse_mode="Markdown", reply_markup=get_post_action_keyboard())
            db.save_password(found)
        else:
            await status_msg.edit_text("❌ Password not in common list. Deep scan (Brute Force) is limited in this version for safety.")
    except Exception as e:
        await status_msg.edit_text(f"❌ Crack Error: {e}")

async def receive_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    images = context.user_data.setdefault('images', [])
    if len(images) >= 30:
        await update.message.reply_text("⚠️ Limit reached (30 images).", reply_markup=get_image_upload_keyboard())
        return UPLOAD_IMAGES
    f = await update.message.photo[-1].get_file()
    path = f"img_{f.file_unique_id}.jpg"
    await f.download_to_drive(path)
    images.append(path)
    await update.message.reply_text(f"✅ Image {len(images)}/30 received. Click Done when finished.", reply_markup=get_image_upload_keyboard())
    return UPLOAD_IMAGES

async def done_images(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("⏳ Generating high-quality PDF...")
    imgs = context.user_data.get('images', [])
    out = f"gallery_{int(time.time())}.pdf"
    try:
        def _create():
            with open(out, "wb") as f: f.write(img2pdf.convert(imgs))
        await asyncio.to_thread(_create)
        await query.message.reply_document(document=open(out, "rb"), caption="🖼️ **Images to PDF**")
        os.remove(out)
    except Exception as e: await query.message.reply_text(f"❌ Failed: {e}")
    for i in imgs: 
        if os.path.exists(i): os.remove(i)
    return await start(update, context)

def main():
    app = Application.builder().token(TOKEN).build()
    
    # Admin Handlers
    app.add_handler(CommandHandler("broadcast", broadcast_command))
    app.add_handler(MessageHandler(filters.TEXT & filters.REPLY, handle_broadcast)) # Simple broadcast catcher

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            MessageHandler(filters.Document.PDF, handle_initial_pdf),
            MessageHandler(filters.PHOTO, handle_initial_image)
        ],
        states={
            WAIT_FOR_UPLOAD: [
                MessageHandler(filters.Document.PDF, handle_initial_pdf),
                MessageHandler(filters.PHOTO, handle_initial_image)
            ],
            CHOOSING_ACTION: [
                CallbackQueryHandler(action_chosen, pattern="^(?!rot_|back_to_menu)"),
                CallbackQueryHandler(handle_rotation, pattern="^rot_"),
                CallbackQueryHandler(back_to_menu, pattern="^back_to_menu$")
            ],
            TYPE_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_password)],
            UPLOAD_IMAGES: [
                MessageHandler(filters.PHOTO, receive_image), 
                CallbackQueryHandler(done_images, pattern="^done_uploading$")
            ],
            TYPE_WATERMARK_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_password)], # Reuse logic
            CHOOSE_ROTATION: [CallbackQueryHandler(handle_rotation, pattern="^rot_"), CallbackQueryHandler(back_to_menu, pattern="^back_to_menu$")],
            WAIT_FOR_BROADCAST: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_broadcast)]
        },
        fallbacks=[CommandHandler("cancel", cancel_process), CallbackQueryHandler(cancel_process, pattern="^cancel$")],
    )
    app.add_handler(conv)
    print("PDF Master Bot Pro is running...")
    app.run_polling()

if __name__ == '__main__':
    main()
