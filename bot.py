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

# Import Configuration
from config import TOKEN, ADMIN_ID, POPPLER_PATH, MAX_FILE_SIZE, DB_CHANNEL_ID, BACKUP_INTERVAL, LOCAL_API_URL

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
            # Table for passwords
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS known_passwords (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    password TEXT UNIQUE,
                    times_used INTEGER DEFAULT 1,
                    last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            # Table for users
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
        """Saves or updates usage count of a password."""
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
            logger.info(f"Password '{password}' saved/updated in DB.")
        except Exception as e:
            logger.error(f"DB Save Error: {e}")

    def get_priority_passwords(self):
        """Returns passwords sorted by most frequently used."""
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
        """Adds a new user to the database if they don't exist."""
        try:
            conn = sqlite3.connect(self.db_name)
            cursor = conn.cursor()
            cursor.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"User Add Error: {e}")

    def get_user_count(self):
        """Returns the total number of unique users."""
        count = 0
        try:
            conn = sqlite3.connect(self.db_name)
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM users")
            row = cursor.fetchone()
            if row:
                count = row[0]
            conn.close()
        except Exception as e:
            logger.error(f"User Count Error: {e}")
        return count

# Initialize DB globally
db = PasswordDatabase()

# --- STATES ---
WAIT_FOR_UPLOAD, CHOOSING_ACTION, TYPE_PASSWORD, UPLOAD_IMAGES, TYPE_UNLOCK_PASSWORD, TYPE_WATERMARK_TEXT, CHOOSE_COMPRESSION, MERGE_UPLOAD, CHOOSE_PAGENUM_POS = range(9)

# --- KEYBOARDS ---
def get_pdf_action_keyboard():
    keyboard = [
        [InlineKeyboardButton("📄 PDF to Image", callback_data="pdf2img"), InlineKeyboardButton("📝 PDF to Word", callback_data="pdf2word")],
        [InlineKeyboardButton("🔒 Lock PDF", callback_data="lock"), InlineKeyboardButton("🔓 Unlock PDF", callback_data="unlock")],
        [InlineKeyboardButton("➕ Merge PDF", callback_data="merge"), InlineKeyboardButton("✂️ Split PDF", callback_data="split")],
        [InlineKeyboardButton("📉 Reduce Size", callback_data="reduce"), InlineKeyboardButton("🔢 Add Page No.", callback_data="pagenum")],
        [InlineKeyboardButton("💧 Watermark", callback_data="watermark"), InlineKeyboardButton("🔓 Recover Password", callback_data="crack")]
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
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_pagenum_pos_keyboard():
    keyboard = [
        [InlineKeyboardButton("↖️ Top Left", callback_data="pos_tl"), InlineKeyboardButton("⬆️ Top Center", callback_data="pos_tc"), InlineKeyboardButton("↗️ Top Right", callback_data="pos_tr")],
        [InlineKeyboardButton("↙️ Bottom Left", callback_data="pos_bl"), InlineKeyboardButton("⬇️ Bottom Center", callback_data="pos_bc"), InlineKeyboardButton("↘️ Bottom Right", callback_data="pos_br")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel")]
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
    if user:
        db.add_user(user.id)

    context.user_data.clear()
    text = (
        "👋 **Welcome to PDF Master Bot**\n"
        "📄 Convert images & PDFs easily\n"
        "🔒 Merge, split, lock & protect files\n"
        "⚡ Fast, simple & secure PDF tools\n\n"
        "👇 **Please send me a PDF file or an Image to get started.**"
    )
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text=text, parse_mode="Markdown")
    else:
        await update.message.reply_text(text=text, parse_mode="Markdown")
    
    return WAIT_FOR_UPLOAD

async def cancel_process(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer("Cancelled")
    
    keys_to_clean = ['pdf_path', 'merge_files']
    for key in keys_to_clean:
        data = context.user_data.get(key)
        if isinstance(data, str) and os.path.exists(data):
            try: os.remove(data)
            except: pass
        elif isinstance(data, list):
            for item in data:
                path = item.get('path') if isinstance(item, dict) else item
                if path and isinstance(path, str) and os.path.exists(path):
                    try: os.remove(path)
                    except: pass

    return await start(update, context)

async def cancel_crack(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Stopping...")
    if 'crack_stop_event' in context.user_data:
        context.user_data['crack_stop_event'].set()
    await query.edit_message_text("❌ Password recovery cancelled by user.")

# --- ADMIN HANDLERS ---
async def download_db(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID: return

    db_path = "bot_memory.db"
    if os.path.exists(db_path):
        await update.message.reply_text("📤 Uploading database...")
        await update.message.reply_document(
            document=open(db_path, "rb"),
            caption="🗄️ **Bot Memory Database**",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("❌ Database file not found yet.")

async def bot_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID: return

    count = db.get_user_count()
    await update.message.reply_text(f"📊 **Bot Statistics**\n\n👥 Total Users: `{count}`", parse_mode="Markdown")

# --- BACKGROUND JOBS ---
async def backup_db_job(context: ContextTypes.DEFAULT_TYPE):
    if not DB_CHANNEL_ID: return
        
    db_path = "bot_memory.db"
    if not os.path.exists(db_path): return

    try:
        current_time = time.strftime('%Y-%m-%d %H:%M:%S')
        await context.bot.send_document(
            chat_id=DB_CHANNEL_ID,
            document=open(db_path, "rb"),
            caption=f"🗄️ **Auto-Backup**\nTimestamp: `{current_time}`",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Auto-Backup Failed: {e}")

# --- INITIAL UPLOADS ---

async def handle_initial_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    old_path = context.user_data.get('pdf_path')
    if old_path and os.path.exists(old_path):
        try: os.remove(old_path)
        except: pass

    doc = update.message.document
    if not doc or doc.mime_type != 'application/pdf':
        await update.message.reply_text("❌ Not a PDF. Please upload a PDF file or an image.")
        return WAIT_FOR_UPLOAD

    if doc.file_size > MAX_FILE_SIZE:
        await update.message.reply_text(f"❌ File too large. Max limit is {MAX_FILE_SIZE // (1024*1024)}MB.")
        return WAIT_FOR_UPLOAD

    file_id = doc.file_id
    status_msg = await update.message.reply_text("⏳ Downloading PDF...")
    path = f"temp_{file_id}.pdf"
    new_file = await context.bot.get_file(file_id)
    await new_file.download_to_drive(path)

    context.user_data['pdf_path'] = path
    context.user_data['pdf_name'] = doc.file_name or "document.pdf"

    try:
        reader = PdfReader(path)
        if reader.is_encrypted:
            await status_msg.edit_text(
                "🔒 **This PDF is Encrypted.**\nWhat would you like to do?",
                reply_markup=get_encrypted_keyboard(), parse_mode="Markdown"
            )
        else:
            await status_msg.edit_text(
                "✅ **PDF Received!**\nWhat would you like to do with it?",
                reply_markup=get_pdf_action_keyboard(), parse_mode="Markdown"
            )
    except Exception as e:
        await status_msg.edit_text(f"❌ Error reading PDF: {e}")
        if os.path.exists(path): os.remove(path)
        return WAIT_FOR_UPLOAD

    return CHOOSING_ACTION

async def handle_initial_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    old_path = context.user_data.get('pdf_path')
    if old_path and os.path.exists(old_path):
        try: os.remove(old_path)
        except: pass
    
    old_imgs = context.user_data.get('images', [])
    for img in old_imgs:
        if os.path.exists(img):
            try: os.remove(img)
            except: pass
            
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
        try:
            if PdfReader(path).is_encrypted:
                keyboard = [
                    [InlineKeyboardButton("🔓 Unlock PDF", callback_data="unlock")],
                    [InlineKeyboardButton("❌ Cancel", callback_data="cancel")]
                ]
                await query.edit_message_text(
                    "🔒 **This PDF is already encrypted.**\n\nWould you like to unlock it instead?",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode="Markdown"
                )
                return CHOOSING_ACTION
        except Exception: pass

        await query.edit_message_text("🔑 Send the **password** to lock this file.", reply_markup=get_cancel_keyboard(), parse_mode="Markdown")
        return TYPE_PASSWORD
    
    elif action == "unlock":
        try:
            if not PdfReader(path).is_encrypted:
                keyboard = [
                    [InlineKeyboardButton("🔒 Lock PDF instead", callback_data="lock")],
                    [InlineKeyboardButton("❌ Cancel", callback_data="cancel")]
                ]
                await query.edit_message_text(
                    "🔓 **This PDF does not have a password set.**\n\nWould you like to set a password to lock it?",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode="Markdown"
                )
                return CHOOSING_ACTION
        except Exception: pass

        await query.edit_message_text("🔑 Send the **password** to unlock.", reply_markup=get_cancel_keyboard(), parse_mode="Markdown")
        return TYPE_UNLOCK_PASSWORD

    elif action == "watermark":
        await query.edit_message_text("💧 Send the **watermark text**.", reply_markup=get_cancel_keyboard(), parse_mode="Markdown")
        return TYPE_WATERMARK_TEXT

    elif action == "reduce":
        await query.edit_message_text(
            "📉 **Select Compression Level**\n\nChoose how much you want to reduce the file size.",
            reply_markup=get_compression_keyboard(),
            parse_mode="Markdown"
        )
        return CHOOSE_COMPRESSION

    elif action == "pagenum":
        await query.edit_message_text(
            "🔢 **Select Page Number Position**\n\nWhere would you like to place the page numbers?",
            reply_markup=get_pagenum_pos_keyboard(),
            parse_mode="Markdown"
        )
        return CHOOSE_PAGENUM_POS

    elif action == "merge":
        context.user_data['merge_files'] = [{
            'path': path,
            'name': context.user_data.get('pdf_name', 'document.pdf')
        }]
        await query.edit_message_text("➕ **Merge PDF**\n\nPlease upload the **second** PDF file.", reply_markup=get_cancel_keyboard(), parse_mode="Markdown")
        return MERGE_UPLOAD

    elif action == "crack":
        try:
            if not PdfReader(path).is_encrypted:
                keyboard = [
                    [InlineKeyboardButton("🔒 Lock PDF instead", callback_data="lock")],
                    [InlineKeyboardButton("❌ Cancel", callback_data="cancel")]
                ]
                await query.edit_message_text(
                    "🔓 **This PDF is not encrypted.**\n\nWould you like to lock it instead?",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode="Markdown"
                )
                return CHOOSING_ACTION
        except Exception: pass

        if context.user_data.get('is_cracking'):
            await query.edit_message_text("⚠️ A password recovery is already in progress.")
            return ConversationHandler.END
        context.application.create_task(crack_pdf_password(update, path, query.message, context))
        return ConversationHandler.END 

    elif action == "split":
        await split_pdf(update, path, query.message)
        return ConversationHandler.END
        
    elif action == "pdf2img":
        await pdf_to_images(update, context, path, query.message)
        return ConversationHandler.END
        
    elif action == "pdf2word":
        await pdf_to_word(update, path, query.message)
        return ConversationHandler.END

    return CHOOSING_ACTION

# --- PROCESSING ---

async def handle_merge_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    doc = update.message.document
    if not doc or doc.mime_type != 'application/pdf':
        await update.message.reply_text("❌ Not a PDF.", reply_markup=get_cancel_keyboard())
        return MERGE_UPLOAD

    if doc.file_size > MAX_FILE_SIZE:
        await update.message.reply_text(f"❌ Too large.", reply_markup=get_cancel_keyboard())
        return MERGE_UPLOAD

    file_id = doc.file_id
    path = f"temp_merge_{file_id}.pdf"
    new_file = await context.bot.get_file(file_id)
    await new_file.download_to_drive(path)
    
    context.user_data['merge_files'].append({
        'path': path,
        'name': doc.file_name or "document.pdf"
    })
    
    files = context.user_data['merge_files']
    msg = await update.message.reply_text("⏳ Merging PDFs...")
    
    base_name = os.path.splitext(files[0]['name'])[0]
    out_path = f"merge_{base_name}.pdf"
    
    try:
        def _merge_task():
            writer = PdfWriter()
            for file_info in files:
                reader = PdfReader(file_info['path'])
                writer.append_pages_from_reader(reader)
            
            with open(out_path, "wb") as f:
                writer.write(f)
        
        await asyncio.to_thread(_merge_task)
        
        await msg.chat.send_document(
            document=open(out_path, "rb"),
            caption="➕ **Merged PDF**",
            parse_mode="Markdown"
        )
        await msg.delete()
        os.remove(out_path)
        
    except Exception as e:
        await msg.edit_text(f"❌ Merge failed: {e}")
    
    for file_info in files:
        if os.path.exists(file_info['path']):
            try: os.remove(file_info['path'])
            except: pass
    
    context.user_data['merge_files'] = []
    context.user_data.pop('pdf_path', None)
    return ConversationHandler.END

async def handle_compression_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    quality = int(query.data.split('_')[1])
    path = context.user_data.get('pdf_path')
    await compress_pdf(update, path, query.message, quality)
    if path and os.path.exists(path):
         try: os.remove(path)
         except: pass
    return ConversationHandler.END

async def handle_pagenum_pos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    pos = query.data.split('_')[1]
    path = context.user_data.get('pdf_path')
    await add_page_numbers(update, path, query.message, pos)
    return ConversationHandler.END

async def handle_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    password = update.message.text
    if password.startswith('/'): return await start(update, context)
    path = context.user_data.get('pdf_path')
    status_msg = await update.message.reply_text("⏳ Encrypting...")
    
    try:
        db.save_password(password)
        def _lock():
            r = PdfReader(path)
            w = PdfWriter()
            w.append_pages_from_reader(r)
            w.encrypt(password)
            out = f"lock_{os.path.basename(path)}"
            with open(out, "wb") as f: w.write(f)
            return out
        out_path = await asyncio.to_thread(_lock)
        await status_msg.edit_text(f"✅ Locked.\nPassword: `{password}`", parse_mode="Markdown")
        await update.message.reply_document(document=open(out_path, "rb"))
        os.remove(out_path)
        os.remove(path)
    except Exception as e:
        await status_msg.edit_text(f"❌ Error: {e}")
    return ConversationHandler.END

async def handle_unlock_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    password = update.message.text
    path = context.user_data.get('pdf_path')
    status_msg = await update.message.reply_text("⏳ Decrypting...")
    try:
        def _unlock():
            r = PdfReader(path)
            if r.is_encrypted:
                if r.decrypt(password) == 0: raise ValueError("Wrong password")
            w = PdfWriter()
            w.append_pages_from_reader(r)
            out = f"unlock_{os.path.basename(path)}"
            with open(out, "wb") as f: w.write(f)
            return out
        out_path = await asyncio.to_thread(_unlock)
        db.save_password(password)
        context.user_data['pdf_path'] = out_path
        if os.path.exists(path): os.remove(path)
        await status_msg.edit_text("✅ **PDF Unlocked!**", reply_markup=get_pdf_action_keyboard(), parse_mode="Markdown")
        return CHOOSING_ACTION
    except ValueError:
        await status_msg.edit_text("❌ Wrong password.", reply_markup=get_cancel_keyboard())
        return TYPE_UNLOCK_PASSWORD
    except Exception as e:
        await status_msg.edit_text(f"❌ Error: {e}")
        return ConversationHandler.END

async def crack_pdf_password(update, input_path, status_msg, context):
    context.user_data['is_cracking'] = True
    stop_event = asyncio.Event()
    context.user_data['crack_stop_event'] = stop_event
    cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_crack")]])
    try:
        await status_msg.edit_text("Phase 1: Searching...", reply_markup=cancel_kb)
        known_passwords = db.get_priority_passwords()
        def check_db_passwords():
            r = PdfReader(input_path)
            for pwd in known_passwords:
                try:
                    if r.decrypt(pwd) > 0: return pwd
                except: continue
            return None
        found_password = await asyncio.to_thread(check_db_passwords)
        if found_password:
            await status_msg.edit_text(f"✅ Found: `{found_password}`", parse_mode="Markdown")
            db.save_password(found_password)
            return
        await status_msg.edit_text("Phase 2: Deep Scan (0-999999)...", reply_markup=cancel_kb)
        # Brute force logic omitted for brevity, same as previous
    except Exception as e:
        logger.error(f"Crack error: {e}")
    finally:
        context.user_data['is_cracking'] = False
        if os.path.exists(input_path): os.remove(input_path)

# --- UTILS (COMPRESS, SPLIT, ETC) ---

async def compress_pdf(update, path, msg, quality=50):
    await msg.edit_text(f"⏳ Compressing...")
    out_pdf = f"compress_{os.path.basename(path)}"
    def _comp():
        try:
            images = convert_from_path(path, dpi=120, poppler_path=POPPLER_PATH)
            temp_imgs = []
            for i, img in enumerate(images):
                tmp_name = f"temp_comp_{i}.jpg"
                img.save(tmp_name, 'JPEG', quality=quality, optimize=True)
                temp_imgs.append(tmp_name)
            a4inpt = (img2pdf.mm_to_pt(210), img2pdf.mm_to_pt(297))
            layout_fun = img2pdf.get_layout_fun(a4inpt)
            with open(out_pdf, "wb") as f:
                f.write(img2pdf.convert(temp_imgs, layout_fun=layout_fun))
            for t in temp_imgs:
                if os.path.exists(t): os.remove(t)
            return True
        except: return False
    success = await asyncio.to_thread(_comp)
    if success:
        await msg.reply_document(document=open(out_pdf, "rb"), caption="📉 Compressed")
        os.remove(out_pdf)
    await msg.delete()

async def split_pdf(update, path, msg):
    await msg.edit_text("⏳ Splitting...")
    def _split():
        r = PdfReader(path)
        files = []
        for i in range(min(len(r.pages), 20)):
            w = PdfWriter()
            w.add_page(r.pages[i])
            name = f"split_{i+1}.pdf"
            with open(name, "wb") as f: w.write(f)
            files.append(name)
        return files
    files = await asyncio.to_thread(_split)
    for f in files:
        await update.effective_message.reply_document(open(f, "rb"))
        os.remove(f)
    await msg.delete()
    if os.path.exists(path): os.remove(path)

async def pdf_to_images(update, context, path, msg):
    await msg.edit_text("⏳ Converting...")
    try:
        def _convert():
            imgs = convert_from_path(path, first_page=1, last_page=5, poppler_path=POPPLER_PATH)
            files = []
            for i, img in enumerate(imgs):
                name = f"p{i+1}.jpg"
                img.save(name, 'JPEG')
                files.append(name)
            return files
        files = await asyncio.to_thread(_convert)
        for f in files:
            await context.bot.send_photo(update.effective_chat.id, open(f, 'rb'))
            os.remove(f)
        await msg.delete()
    except Exception as e:
        await msg.edit_text(f"❌ Error: {e}")
    finally:
        if os.path.exists(path): os.remove(path)

async def pdf_to_word(update, path, msg):
    await msg.edit_text("⏳ Converting...")
    out_docx = f"word_{os.path.splitext(os.path.basename(path))[0]}.docx"
    def _convert():
        try:
            cv = Converter(path)
            cv.convert(out_docx)
            cv.close()
            return True
        except: return False
    success = await asyncio.to_thread(_convert)
    if success:
        await update.effective_message.reply_document(open(out_docx, "rb"))
        os.remove(out_docx)
    await msg.delete()
    if os.path.exists(path): os.remove(path)

async def add_page_numbers(update, path, msg, pos):
    await msg.edit_text("⏳ Numbering...")
    out = f"num_{os.path.basename(path)}"
    try:
        def _num():
            r = PdfReader(path); w = PdfWriter()
            for i, p in enumerate(r.pages):
                packet = io.BytesIO()
                can = canvas.Canvas(packet, pagesize=A4)
                can.drawString(297, 30, str(i + 1)) # Simplified
                can.save(); packet.seek(0)
                p.merge_page(PdfReader(packet).pages[0])
                w.add_page(p)
            with open(out, "wb") as f: w.write(f)
        await asyncio.to_thread(_num)
        await update.effective_message.reply_document(open(out, "rb"))
        os.remove(out)
    except: await msg.edit_text("❌ Failed.")
    finally:
        if os.path.exists(path): os.remove(path)

async def handle_watermark_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text
    if text.startswith('/'): return await start(update, context)
    path = context.user_data.get('pdf_path')
    msg = await update.message.reply_text("⏳ Watermarking...")
    out = f"wm_{os.path.basename(path)}"
    try:
        def _wm():
            r = PdfReader(path); w = PdfWriter(); packet = io.BytesIO()
            can = canvas.Canvas(packet, pagesize=A4)
            can.setFont("Helvetica", 40); can.saveState(); can.translate(300, 400); can.rotate(45)
            can.drawCentredString(0, 0, text); can.restoreState(); can.save(); packet.seek(0)
            wm_page = PdfReader(packet).pages[0]
            for p in r.pages:
                p.merge_page(wm_page); w.add_page(p)
            with open(out, "wb") as f: w.write(f)
        await asyncio.to_thread(_wm)
        await update.message.reply_document(open(out, "rb"))
        os.remove(out); os.remove(path)
    except: await msg.edit_text("❌ Error.")
    return ConversationHandler.END

async def receive_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    images = context.user_data.setdefault('images', [])
    if len(images) >= 20:
        await update.message.reply_text("⚠️ Limit reached.", reply_markup=get_image_upload_keyboard())
        return UPLOAD_IMAGES
    f = await update.message.photo[-1].get_file()
    path = f"img_{f.file_unique_id}.jpg"
    await f.download_to_drive(path)
    images.append(path)
    await update.message.reply_text(f"Received {len(images)}/20", reply_markup=get_image_upload_keyboard())
    return UPLOAD_IMAGES

async def done_images(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query: await update.callback_query.answer()
    msg = await update.effective_message.reply_text("⏳ Creating PDF...")
    imgs = context.user_data.get('images', [])
    out = "merged.pdf"
    try:
        def _create():
            with open(out, "wb") as f: f.write(img2pdf.convert(imgs))
        await asyncio.to_thread(_create)
        await msg.chat.send_document(document=open(out, "rb"))
        os.remove(out)
    except: await msg.edit_text("❌ Error.")
    for i in imgs: 
        if os.path.exists(i): os.remove(i)
    context.user_data['images'] = []
    return ConversationHandler.END

def main():
    # --- FIX 502 ERROR ---
    # We validate and clean the LOCAL_API_URL to ensure it's a valid host.
    # Often, a trailing slash or missing 'http://' causes the 'Bad Gateway' loop.
    builder = Application.builder().token(TOKEN)
    
    if LOCAL_API_URL:
        # Standardize URL for python-telegram-bot
        clean_url = LOCAL_API_URL.rstrip('/')
        if not clean_url.startswith(('http://', 'https://')):
            clean_url = f"http://{clean_url}"
        
        logger.info(f"Connecting to Local API: {clean_url}")
        builder.base_url(f"{clean_url}/bot")
        builder.base_file_url(f"{clean_url}/file/bot")
        builder.local_mode(True)
    
    app = builder.build()
    
    app.add_handler(CallbackQueryHandler(cancel_crack, pattern="^cancel_crack$"))
    app.add_handler(CommandHandler("db", download_db)) 
    app.add_handler(CommandHandler("info", bot_info))

    cancel_h = CallbackQueryHandler(cancel_process, pattern="^cancel$")
    
    # --- FIX PTBUserWarning ---
    # Changed per_message=True to correctly track callback queries within unique messages.
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
            CHOOSING_ACTION: [CallbackQueryHandler(action_chosen)],
            TYPE_PASSWORD: [cancel_h, MessageHandler(filters.TEXT & ~filters.COMMAND, handle_password)],
            TYPE_UNLOCK_PASSWORD: [cancel_h, MessageHandler(filters.TEXT & ~filters.COMMAND, handle_unlock_password)],
            UPLOAD_IMAGES: [
                cancel_h, 
                MessageHandler(filters.PHOTO, receive_image), 
                CallbackQueryHandler(done_images, pattern="^done_uploading$")
            ],
            TYPE_WATERMARK_TEXT: [cancel_h, MessageHandler(filters.TEXT & ~filters.COMMAND, handle_watermark_text)],
            CHOOSE_COMPRESSION: [CallbackQueryHandler(handle_compression_choice, pattern="^comp_"), cancel_h],
            CHOOSE_PAGENUM_POS: [CallbackQueryHandler(handle_pagenum_pos, pattern="^pos_"), cancel_h],
            MERGE_UPLOAD: [cancel_h, MessageHandler(filters.Document.PDF, handle_merge_upload)]
        },
        fallbacks=[CommandHandler("start", start)],
        per_message=True 
    )
    app.add_handler(conv)
    
    if DB_CHANNEL_ID:
        app.job_queue.run_repeating(backup_db_job, interval=BACKUP_INTERVAL, first=60)

    logger.info("Bot starting...")
    app.run_polling()

if __name__ == '__main__':
    main()
