import os
import re
import json
import sqlite3
import logging
import asyncio
import csv
import psutil
import gc
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, 
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, 
    CallbackQueryHandler, filters, ContextTypes, ConversationHandler
)
from playwright.async_api import async_playwright

# 🔧 تثبيت مجلد العمل
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE_DIR)

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# ----------------- 1. البيانات والإعدادات الأساسية -----------------
BOT_TOKEN = "8976163227:AAG0MeeWYG7DclvCZt7Y62uV112OSJ9UeRU"
DEFAULT_SITE_URL = "https://abroad.help.tiandycloud.com/product/#/login?redirect=%2Frecovepwd%2Frecover"

DEFAULT_USERNAME = "ALREYADA"
DEFAULT_PASSWORD = "qqqq1111"
DEFAULT_ADMIN_WHATSAPP = "+218000000000"

ADMIN_ID = 1935856158  # 👑 الـ Telegram User ID للأدمن الرئيسي

DATA_FILE = os.path.join(BASE_DIR, "bot_data.json")
DB_FILE = os.path.join(BASE_DIR, "tiandy_bot.db")

UNLIMITED_EXPIRY = "2099-12-31"

process_lock = asyncio.Lock()

USER_LAST_REQUEST = {}
COOLDOWN_SECONDS = 15

# حالات المحادثة (Conversation States)
(
    ADD_USER_ID, ADD_USER_NAME, ADD_USER_DAYS,
    BROADCAST_TEXT,
    SET_SITE_URL, SET_SITE_USER, SET_SITE_PASS, SET_ADMIN_WA,
    QUICK_ADD_DAYS
) = range(9)

# ----------------- 2. إدارة قواعد البيانات والبيانات المحلية -----------------

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS site_usage_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            user_name TEXT,
            site_username TEXT,
            security_code TEXT,
            timestamp TEXT
        )
    ''')
    cursor.execute("PRAGMA table_info(site_usage_logs)")
    columns = [column[1] for column in cursor.fetchall()]
    if 'user_id' not in columns:
        cursor.execute("ALTER TABLE site_usage_logs ADD COLUMN user_id TEXT")
    if 'user_name' not in columns:
        cursor.execute("ALTER TABLE site_usage_logs ADD COLUMN user_name TEXT")
    conn.commit()
    conn.close()

init_db()

def log_site_extraction_db(user_id, user_name, site_username, security_code):
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        current_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute('''
            INSERT INTO site_usage_logs (user_id, user_name, site_username, security_code, timestamp)
            VALUES (?, ?, ?, ?, ?)
        ''', (str(user_id), user_name, site_username, security_code, current_time_str))
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f"DB Logging Error: {e}")

def load_data():
    default_settings = {
        "site_url": DEFAULT_SITE_URL,
        "site_username": DEFAULT_USERNAME,
        "site_password": DEFAULT_PASSWORD,
        "admin_whatsapp": DEFAULT_ADMIN_WHATSAPP
    }
    default_data = {
        "settings": default_settings,
        "users": {
            str(ADMIN_ID): {
                "name": "الأدمن الرئيسي",
                "expiry": UNLIMITED_EXPIRY,
                "usage_count": 0,
                "last_used": "لم يستخدم بعد",
                "status": "active"
            }
        }
    }
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if "settings" not in data:
                    data["settings"] = default_settings
                else:
                    for k, v in default_settings.items():
                        if k not in data["settings"]:
                            data["settings"][k] = v
                return data
        except Exception as e:
            logging.error(f"Error loading JSON data: {e}")
            return default_data
    return default_data

BOT_DATA = load_data()

def save_data():
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(BOT_DATA, f, ensure_ascii=False, indent=4)
    except Exception as e:
        logging.error(f"Error saving JSON data: {e}")

def get_setting(key, default_val):
    return BOT_DATA.get("settings", {}).get(key, default_val)

def is_user_allowed(user_id: int) -> tuple[bool, str]:
    uid_str = str(user_id)
    if uid_str not in BOT_DATA.get("users", {}):
        return False, f"غير مسجل في المنظومة\n\n🆔 المعرف الخاص بك (ID):\n`{user_id}`\n\nيرجى إرسال هذا المعرف عبر الواتساب للأدمن الرئيسي لتفعيل اشتراكك."
    
    user_info = BOT_DATA["users"][uid_str]
    if user_info.get("status") == "blocked":
        return False, "حسابك معطل / محظور حالياً من قبل الإدارة."
    
    expiry_str = user_info.get("expiry", UNLIMITED_EXPIRY)
    if expiry_str == UNLIMITED_EXPIRY:
        return True, "غير محدودة"
    try:
        expiry_date = datetime.strptime(expiry_str, "%Y-%m-%d")
        if datetime.now() > expiry_date:
            return False, f"انتهت صلاحية اشتراكك بتاريخ ({expiry_str}). يرجى التواصل مع الأدمن للتجديد."
    except Exception:
        pass
    return True, expiry_str

# ----------------- 3. دالة رسم شريط التقدم -----------------

def generate_progress_bar(percent: int) -> str:
    total_blocks = 12
    filled_blocks = int((percent / 100) * total_blocks)
    empty_blocks = total_blocks - filled_blocks
    bar = "█" * filled_blocks + "░" * empty_blocks
    return f"`[{bar}] {percent}%`"

def get_stage_description(percent: int) -> str:
    if percent <= 25:
        return "⏳ **جاري فتح الاتصال الآمن للطلب...**"
    elif percent <= 50:
        return "⏳ **جاري قراءة البيانات وفحص البنية...**"
    elif percent <= 75:
        return "⏳ **جاري معالجة الشفرة عبر المنظومة...**"
    elif percent <= 95:
        return "⏳ **جاري تجميع البيانات وتأكيد الاستجابة...**"
    else:
        return "⏳ **جاري إنهاء التجهيز وتأكيد النتيجة...**"

async def smooth_progress_tracker(message, stop_event: asyncio.Event, max_cap: int = 92):
    current_percent = 5
    while not stop_event.is_set() and current_percent < max_cap:
        stage_text = get_stage_description(current_percent)
        bar_text = generate_progress_bar(current_percent)
        try:
            await message.edit_text(f"{stage_text}\n{bar_text}", parse_mode="Markdown")
        except Exception:
            pass
        current_percent += 5
        await asyncio.sleep(0.8)
    return current_percent

# ----------------- 4. لوحة التحكم والقوائم -----------------

def get_admin_reply_keyboard():
    keyboard = [
        [KeyboardButton("🎛️ لوحة التحكم"), KeyboardButton("⚙️ الإعدادات الديناميكية")],
        [KeyboardButton("📊 تقارير الاستخدام"), KeyboardButton("📜 إدارة المشتركين")],
        [KeyboardButton("➕ إضافة مشترك")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, is_persistent=True)

def get_admin_main_menu():
    total_users = len(BOT_DATA.get("users", {}))
    s_url = get_setting("site_url", DEFAULT_SITE_URL)
    admin_text = (
        f"👑 **لوحة تحكم الأدمن:**\n\n"
        f"👥 **إجمالي المشتركين:** `{total_users}`\n"
        f"🌐 **رابط المنظومة:** `{s_url}`"
    )
    keyboard = [
        [InlineKeyboardButton("⚙️ الإعدادات الديناميكية", callback_data="settings_menu")],
        [InlineKeyboardButton("👥 إدارة المشتركين وتصنيفهم", callback_data="manage_users")],
        [InlineKeyboardButton("📊 تقارير الاستخدام حسب المدة", callback_data="reports_menu")],
        [InlineKeyboardButton("🩺 فحص حالة المنظومة والذاكرة (Health Check)", callback_data="health_check")],
        [InlineKeyboardButton("➕ إضافة مشترك جديد", callback_data="add_user_start")],
        [InlineKeyboardButton("📢 إذاعة عامة للمشتركين", callback_data="broadcast_start")]
    ]
    return admin_text, InlineKeyboardMarkup(keyboard)

def get_settings_menu_markup():
    s = BOT_DATA.get("settings", {})
    text = (
        f"⚙️ **لوحة الإعدادات الديناميكية للمنظومة:**\n\n"
        f"🌐 **رابط المنظومة:**\n`{s.get('site_url', DEFAULT_SITE_URL)}`\n\n"
        f"👤 **اسم المستخدم (Site User):** `{s.get('site_username', DEFAULT_USERNAME)}`\n"
        f"🔑 **كلمة المرور (Site Pass):** `{s.get('site_password', DEFAULT_PASSWORD)}`\n"
        f"📱 **واتساب الدعم (Admin WA):** `{s.get('admin_whatsapp', DEFAULT_ADMIN_WHATSAPP)}`\n\n"
        f"اختر القيمة التي ترغب بتحديثها مباشرة:"
    )
    keyboard = [
        [InlineKeyboardButton("🔗 تعديل رابط المنظومة", callback_data="change_site_url")],
        [InlineKeyboardButton("👤 تعديل اسم مستخدم المنظومة", callback_data="change_site_user")],
        [InlineKeyboardButton("🔑 تعديل كلمة مرور المنظومة", callback_data="change_site_pass")],
        [InlineKeyboardButton("📱 تعديل رقم واتساب الأدمن", callback_data="change_admin_wa")],
        [InlineKeyboardButton("⬅️ رجوع للوحة الأدمن", callback_data="admin_panel")]
    ]
    return text, InlineKeyboardMarkup(keyboard)

def get_reports_menu_markup():
    text = "📊 **اختر الفترة الزمنية لعرض تقارير الاستخدام وفك كلمات السر:**"
    keyboard = [
        [InlineKeyboardButton("📅 آخر 24 ساعة (اليوم)", callback_data="report_day")],
        [InlineKeyboardButton("📅 آخر 7 أيام (أسبوع)", callback_data="report_week")],
        [InlineKeyboardButton("📅 آخر 30 يوماً (شهر)", callback_data="report_month")],
        [InlineKeyboardButton("📅 آخر سنة", callback_data="report_year")],
        [InlineKeyboardButton("♾ إجمالي الاستخدام (منذ البداية)", callback_data="report_all")],
        [InlineKeyboardButton("📥 تصدير السجل الكامل (CSV)", callback_data="export_csv")],
        [InlineKeyboardButton("⬅️ رجوع للوحة الأدمن", callback_data="admin_panel")]
    ]
    return text, InlineKeyboardMarkup(keyboard)

async def notify_admin_unauthorized(context: ContextTypes.DEFAULT_TYPE, user_id: int, full_name: str, username: str):
    try:
        user_mention = f"@{username}" if username else "لا يوجد"
        alert_text = (
            f"🚨 **تنبيه محاولة دخول غير مصرح بها!**\n\n"
            f"👤 **الاسم:** {full_name}\n"
            f"🆔 **المعرف (ID):** `{user_id}`\n"
            f"🔗 **اليوزر نيم:** {user_mention}\n\n"
            f"هل ترغب في إضافة هذا المشترك فوراً إلى المنظومة؟"
        )
        keyboard = [
            [InlineKeyboardButton("➕ إضافة وتفعيل فوري", callback_data=f"quick_add_{user_id}_{full_name[:15]}")],
            [InlineKeyboardButton("🚫 حظر آلي", callback_data=f"quick_block_{user_id}")]
        ]
        await context.bot.send_message(chat_id=ADMIN_ID, text=alert_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    except Exception as e:
        logging.error(f"Error sending alert to admin: {e}")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_name = update.effective_user.full_name or "غير معروف"
    username = update.effective_user.username
    allowed, status = is_user_allowed(user_id)
    
    if not allowed:
        admin_wa = get_setting("admin_whatsapp", DEFAULT_ADMIN_WHATSAPP)
        wa_link = f"https://wa.me/{admin_wa}?text=مرحباً أستاذ، أريد تفعيل حسابي في بوت Tiandy، معرفي هو: {user_id}"
        keyboard = [[InlineKeyboardButton("💬 مراسلة الأدمن عبر واتساب", url=wa_link)]]
        
        if update.message:
            await update.message.reply_text(f"⛔ **عذراً، غير مسموح بالدخول**\n\n{status}", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        
        if str(user_id) not in BOT_DATA.get("users", {}):
            await notify_admin_unauthorized(context, user_id, user_name, username)
        return ConversationHandler.END

    if user_id == ADMIN_ID:
        welcome_text, reply_markup = get_admin_main_menu()
        if update.message:
            await update.message.reply_text("🔄 جاري تحديث لوحة التحكم...", reply_markup=get_admin_reply_keyboard())
            await update.message.reply_text(welcome_text, reply_markup=reply_markup, parse_mode="Markdown")
        elif update.callback_query:
            await update.callback_query.message.edit_text(welcome_text, reply_markup=reply_markup, parse_mode="Markdown")
    else:
        welcome_text = (
            f"👋 **أهلاً بك في بوت فك كاميرات Tiandy (شركة الريادة)**\n\n"
            f"📌 **حالة اشتراكك:** `نشط ✅`\n"
            f"📂 قم بإرسال ملف التفكيك (مفرد أو مجموعة) مباشرة هنا في المحادثة."
        )
        if update.message:
            await update.message.reply_text(welcome_text, reply_markup=ReplyKeyboardRemove(), parse_mode="Markdown")
            
    return ConversationHandler.END

async def handle_text_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        return

    text = update.message.text
    
    if text in ["🎛️ لوحة التحكم", "لوحة التحكم"]:
        await start_command(update, context)
        
    elif text in ["⚙️ الإعدادات الديناميكية", "الإعدادات الديناميكية"]:
        txt, markup = get_settings_menu_markup()
        await update.message.reply_text(txt, reply_markup=markup, parse_mode="Markdown")

    elif text in ["📊 تقارير الاستخدام", "تقارير الاستخدام"]:
        txt, markup = get_reports_menu_markup()
        await update.message.reply_text(txt, reply_markup=markup, parse_mode="Markdown")

    elif text in ["📜 إدارة المشتركين", "إدارة المشتركين"]:
        await show_manage_users_menu(update.message, edit=False)
        
    elif text in ["➕ إضافة مشترك", "إضافة مشترك"]:
        context.user_data['new_user'] = {}
        cancel_btn = InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء والعودة", callback_data="admin_panel")]])
        await update.message.reply_text("➕ **أدخل المعرف (Telegram User ID) للمشترك الجديد:**", reply_markup=cancel_btn, parse_mode="Markdown")
        return ADD_USER_ID

async def generate_usage_report(days_limit=None) -> str:
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        if days_limit is not None:
            target_date = (datetime.now() - timedelta(days=days_limit)).strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute('''
                SELECT user_id, user_name, COUNT(*) as cnt 
                FROM site_usage_logs 
                WHERE timestamp >= ? 
                GROUP BY user_id, user_name 
                ORDER BY cnt DESC
            ''', (target_date,))
        else:
            cursor.execute('''
                SELECT user_id, user_name, COUNT(*) as cnt 
                FROM site_usage_logs 
                GROUP BY user_id, user_name 
                ORDER BY cnt DESC
            ''')
            
        rows = cursor.fetchall()
        conn.close()
        
        if not rows:
            return "📭 لا توجد أي عمليات مسجلة خلال هذه الفترة الزمنية."
            
        report_str = "📊 **تقرير عمليات فك كلمات السر:**\n\n"
        total_ops = 0
        for r in rows:
            uid, uname, count = r[0], r[1], r[2]
            total_ops += count
            report_str += f"• **{uname or 'مستخدم'}** (`{uid}`): `عدد العمليات = {count}`\n"
            
        report_str += f"\n📌 **إجمالي العمليات في هذه الفترة:** `{total_ops}`"
        return report_str
    except Exception as e:
        return f"❌ حدث خطأ أثناء جلب التقارير: {e}"

async def show_manage_users_menu(message_obj, edit=True):
    users = BOT_DATA.get("users", {})
    total_users = len(users)
    active_count = sum(1 for u in users.values() if u.get("status") == "active")
    blocked_count = sum(1 for u in users.values() if u.get("status") == "blocked")
    
    text = (
        f"👥 **إدارة المشتركين في المنظومة:**\n\n"
        f"• إجمالي المشتركين: `{total_users}`\n"
        f"• النشطين: `{active_count}` | المحظورين: `{blocked_count}`\n\n"
        f"اختر المشترك لعرض سجله الفردي أو التعديل عليه:"
    )
    
    keyboard = []
    for uid, info in list(users.items())[:10]:
        status_icon = "🟢" if info.get("status", "active") == "active" else "🔴"
        keyboard.append([InlineKeyboardButton(f"{status_icon} {info.get('name', 'مستخدم')} ({uid})", callback_data=f"edit_user_{uid}")])
    
    keyboard.append([InlineKeyboardButton("➕ إضافة مشترك جديد", callback_data="add_user_start")])
    keyboard.append([InlineKeyboardButton("⬅️ رجوع للوحة الأدمن", callback_data="admin_panel")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    if edit:
        await message_obj.edit_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    else:
        await message_obj.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")

async def cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    admin_text, admin_markup = get_admin_main_menu()
    await query.message.edit_text(admin_text, reply_markup=admin_markup, parse_mode="Markdown")
    return ConversationHandler.END

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    if user_id != ADMIN_ID:
        return

    data = query.data
    
    if data == "admin_panel" or data == "back_to_main":
        admin_text, admin_markup = get_admin_main_menu()
        await query.message.edit_text(admin_text, reply_markup=admin_markup, parse_mode="Markdown")
        
    elif data == "settings_menu":
        txt, markup = get_settings_menu_markup()
        await query.message.edit_text(txt, reply_markup=markup, parse_mode="Markdown")

    elif data == "change_site_url":
        cancel_btn = InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="admin_panel")]])
        await query.message.edit_text("🔗 **أدخل رابط المنظومة الجديد (URL):**", reply_markup=cancel_btn, parse_mode="Markdown")
        return SET_SITE_URL

    elif data == "change_site_user":
        cancel_btn = InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="admin_panel")]])
        await query.message.edit_text("👤 **أدخل اسم المستخدم الجديد لموقع المنظومة:**", reply_markup=cancel_btn, parse_mode="Markdown")
        return SET_SITE_USER

    elif data == "change_site_pass":
        cancel_btn = InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="admin_panel")]])
        await query.message.edit_text("🔑 **أدخل كلمة المرور الجديدة لموقع المنظومة:**", reply_markup=cancel_btn, parse_mode="Markdown")
        return SET_SITE_PASS

    elif data == "change_admin_wa":
        cancel_btn = InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="admin_panel")]])
        await query.message.edit_text("📱 **أدخل رقم واتساب الأدمن الجديد (مع المفتاح الدولي):**", reply_markup=cancel_btn, parse_mode="Markdown")
        return SET_ADMIN_WA

    elif data.startswith("quick_add_"):
        parts = data.split("_", 3)
        target_uid = parts[2]
        target_name = parts[3] if len(parts) > 3 else "مشترك جديد"
        
        context.user_data['quick_uid'] = target_uid
        context.user_data['quick_name'] = target_name
        
        cancel_btn = InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="admin_panel")]])
        await query.message.edit_text(
            f"➕ **تفعيل سريع للمشترك:** `{target_name}` (`{target_uid}`)\n\n"
            f"⏳ أدخل عدد أيام الاشتراك (أو اكتب `0` لاشتراك دائم غير محدود):",
            reply_markup=cancel_btn,
            parse_mode="Markdown"
        )
        return QUICK_ADD_DAYS

    elif data.startswith("quick_block_"):
        target_uid = data.replace("quick_block_", "")
        if "users" not in BOT_DATA:
            BOT_DATA["users"] = {}
        BOT_DATA["users"][target_uid] = {
            "name": f"محظور تلقائي ({target_uid})",
            "expiry": UNLIMITED_EXPIRY,
            "usage_count": 0,
            "last_used": "لم يستخدم",
            "status": "blocked"
        }
        save_data()
        await query.message.edit_text(f"🚫 تم حظر المستخدم `{target_uid}` بنجاح.", parse_mode="Markdown")

    elif data == "reports_menu":
        txt, markup = get_reports_menu_markup()
        await query.message.edit_text(txt, reply_markup=markup, parse_mode="Markdown")

    elif data.startswith("report_"):
        period = data.replace("report_", "")
        days_map = {"day": 1, "week": 7, "month": 30, "year": 365, "all": None}
        days = days_map.get(period)
        report_text = await generate_usage_report(days)
        keyboard = [[InlineKeyboardButton("⬅️ رجوع", callback_data="reports_menu")]]
        await query.message.edit_text(report_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "export_csv":
        await query.message.edit_text("⏳ **جاري إعداد ملف التصدير...**", parse_mode="Markdown")
        try:
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute("SELECT user_id, user_name, site_username, security_code, timestamp FROM site_usage_logs ORDER BY id DESC")
            rows = cursor.fetchall()
            conn.close()

            csv_file_path = os.path.join(BASE_DIR, "usage_reports.csv")
            with open(csv_file_path, mode="w", encoding="utf-8-sig", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["User ID", "User Name", "Site Username", "Security Code", "Timestamp"])
                for row in rows:
                    writer.writerow(row)

            with open(csv_file_path, "rb") as csv_f:
                await context.bot.send_document(
                    chat_id=user_id,
                    document=csv_f,
                    filename="tiandy_usage_report.csv",
                    caption="📂 **سجل العمليات والتقارير الشامل بصيغة CSV:**"
                )
            if os.path.exists(csv_file_path):
                os.remove(csv_file_path)

            await query.message.edit_text("✅ تم إرسال ملف التقارير بنجاح!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ رجوع", callback_data="reports_menu")]]))
        except Exception as e:
            await query.message.edit_text(f"❌ حدث خطأ أثناء تصدير الملف: {e}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ رجوع", callback_data="reports_menu")]]))

    elif data == "health_check":
        await query.message.edit_text("🩺 **جاري فحص اتصال الموقع واستهلاك الذاكرة...**", parse_mode="Markdown")
        site_url = get_setting("site_url", DEFAULT_SITE_URL)
        status_result = "❌ تعذر الاتصال بالموقع"
        response_time = 0
        
        start_t = asyncio.get_event_loop().time()
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage', '--single-process']
                )
                page = await browser.new_page()
                response = await page.goto(site_url, timeout=20000)
                if response and response.ok:
                    status_result = "✅ الموقع يعمل وبحالة ممتازة"
                else:
                    status_result = f"⚠ الموقع استجاب ولكن بكود استجابة: {response.status if response else 'Unknown'}"
                await browser.close()
        except Exception as e:
            status_result = f"❌ خطأ بالاتصال: {str(e)[:50]}"
            
        end_t = asyncio.get_event_loop().time()
        response_time = round((end_t - start_t), 2)

        # حساب استخدام الذاكرة (RAM) الفعلي
        process = psutil.Process(os.getpid())
        ram_mb = process.memory_info().rss / (1024 * 1024)
        sys_ram = psutil.virtual_memory()

        health_text = (
            f"🩺 **تقرير فحص صحة المنظومة والذاكرة:**\n\n"
            f"🌐 **الرابط المستهدف:** `{site_url}`\n"
            f"📌 **حالة الموقع:** {status_result}\n"
            f"⏱ **زمن الاستجابة:** `{response_time} ثانية`\n\n"
            f"🧠 **استهلاك البوت للذاكرة:** `{ram_mb:.1f} MB`\n"
            f"📊 **إجمالي رام السيرفر المستهلكة:** `{sys_ram.percent}%`"
        )
        keyboard = [[InlineKeyboardButton("⬅️ رجوع للوحة الأدمن", callback_data="admin_panel")]]
        await query.message.edit_text(health_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "manage_users":
        await show_manage_users_menu(query.message, edit=True)

    elif data.startswith("user_logs_menu_"):
        target_uid = data.replace("user_logs_menu_", "")
        uinfo = BOT_DATA.get("users", {}).get(target_uid, {})
        uname = uinfo.get('name', 'مستخدم')
        
        text = f"📜 **اختر الفترة الزمنية لعرض سجل عمليات الفني:**\n👤 **الفني:** {uname} (`{target_uid}`)"
        keyboard = [
            [InlineKeyboardButton("📅 آخر 24 ساعة (اليوم)", callback_data=f"ulogs_{target_uid}_day")],
            [InlineKeyboardButton("📅 آخر 7 أيام (أسبوع)", callback_data=f"ulogs_{target_uid}_week")],
            [InlineKeyboardButton("📅 آخر 30 يوماً (شهر)", callback_data=f"ulogs_{target_uid}_month")],
            [InlineKeyboardButton("📅 آخر سنة", callback_data=f"ulogs_{target_uid}_year")],
            [InlineKeyboardButton("♾ منذ الإنضمام (الإجمالي)", callback_data=f"ulogs_{target_uid}_all")],
            [InlineKeyboardButton("⬅️ رجوع", callback_data=f"edit_user_{target_uid}")]
        ]
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data.startswith("ulogs_"):
        parts = data.split("_")
        target_uid = parts[1]
        period = parts[2]
        
        days_map = {"day": 1, "week": 7, "month": 30, "year": 365, "all": None}
        days_limit = days_map.get(period)
        uinfo = BOT_DATA.get("users", {}).get(target_uid, {})
        uname = uinfo.get('name', 'مستخدم')
        
        try:
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            
            if days_limit is not None:
                target_date = (datetime.now() - timedelta(days=days_limit)).strftime("%Y-%m-%d %H:%M:%S")
                cursor.execute('''
                    SELECT security_code, timestamp FROM site_usage_logs 
                    WHERE user_id = ? AND timestamp >= ? 
                    ORDER BY id DESC
                ''', (target_uid, target_date))
            else:
                cursor.execute('''
                    SELECT security_code, timestamp FROM site_usage_logs 
                    WHERE user_id = ? 
                    ORDER BY id DESC
                ''', (target_uid,))
                
            logs = cursor.fetchall()
            conn.close()
            
            period_names = {
                "day": "آخر 24 ساعة",
                "week": "آخر 7 أيام",
                "month": "آخر 30 يوماً",
                "year": "آخر سنة",
                "all": "منذ الإنضمام (الإجمالي)"
            }
            
            logs_text = f"📜 **سجل الفني: {uname}**\n⏱️ **الفترة:** `{period_names.get(period, '')}`\n📊 **عدد العمليات:** `{len(logs)}`\n\n"
            
            if not logs:
                logs_text += "📭 لا توجد أي عمليات مسجلة لهذا الفني في هذه الفترة."
            else:
                for idx, l in enumerate(logs[:20], 1):
                    code, tstamp = l[0], l[1]
                    logs_text += f"{idx}. `🔑 {code}` — 🕒 `{tstamp}`\n"
                if len(logs) > 20:
                    logs_text += f"\n*(يتم عرض أحدث 20 عملية من إجمالي {len(logs)} عملية)*"
            
            keyboard = [[InlineKeyboardButton("⬅️ رجوع", callback_data=f"user_logs_menu_{target_uid}")]]
            await query.message.edit_text(logs_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        except Exception as e:
            await query.message.edit_text(f"❌ حدث خطأ في جلب السجل: {e}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ رجوع", callback_data=f"user_logs_menu_{target_uid}")]]))

    elif data.startswith("edit_user_"):
        target_uid = data.replace("edit_user_", "")
        uinfo = BOT_DATA.get("users", {}).get(target_uid, {})
        status = uinfo.get("status", "active")
        
        text = (
            f"👤 **إدارة بيانات المشترك:**\n\n"
            f"🆔 المعرف: `{target_uid}`\n"
            f"📌 الاسم: `{uinfo.get('name', 'بدون اسم')}`\n"
            f"⏳ انتهاء الاشتراك: `{uinfo.get('expiry')}`\n"
            f"🔒 الحالة الحالية: `{status}`"
        )
        
        toggle_text = "🔴 حظر المستخدم" if status == "active" else "🟢 تفعيل المستخدم"
        toggle_action = f"block_user_{target_uid}" if status == "active" else f"unblock_user_{target_uid}"
        
        keyboard = [
            [InlineKeyboardButton(toggle_text, callback_data=toggle_action)],
            [InlineKeyboardButton("📜 سجل العمليات الفردية", callback_data=f"user_logs_menu_{target_uid}")],
            [InlineKeyboardButton("⏳ تمديد 30 يوم", callback_data=f"extend_30_{target_uid}"), InlineKeyboardButton("♾️ جعل الاشتراك دائم", callback_data=f"extend_forever_{target_uid}")],
            [InlineKeyboardButton("🗑 حذف المشترك نهائياً", callback_data=f"delete_user_{target_uid}")],
            [InlineKeyboardButton("⬅️ رجوع للمشتركين", callback_data="manage_users")]
        ]
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data.startswith("block_user_") or data.startswith("unblock_user_"):
        is_block = data.startswith("block_user_")
        target_uid = data.replace("block_user_" if is_block else "unblock_user_", "")
        if target_uid in BOT_DATA.get("users", {}):
            BOT_DATA["users"][target_uid]["status"] = "blocked" if is_block else "active"
            save_data()
        await query.answer("✅ تم تحديث حالة المشترك بنجاح!", show_alert=True)
        await show_manage_users_menu(query.message, edit=True)

    elif data.startswith("extend_30_") or data.startswith("extend_forever_"):
        is_forever = "forever" in data
        target_uid = data.replace("extend_forever_" if is_forever else "extend_30_", "")
        if target_uid in BOT_DATA.get("users", {}):
            if is_forever:
                BOT_DATA["users"][target_uid]["expiry"] = UNLIMITED_EXPIRY
            else:
                try:
                    current_exp = datetime.strptime(BOT_DATA["users"][target_uid]["expiry"], "%Y-%m-%d")
                    if current_exp < datetime.now():
                        current_exp = datetime.now()
                    new_exp = current_exp + timedelta(days=30)
                    BOT_DATA["users"][target_uid]["expiry"] = new_exp.strftime("%Y-%m-%d")
                except Exception:
                    BOT_DATA["users"][target_uid]["expiry"] = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
            save_data()
        await query.answer("✅ تم تحديث مدة الاشتراك بنجاح!", show_alert=True)
        await show_manage_users_menu(query.message, edit=True)

    elif data.startswith("delete_user_"):
        target_uid = data.replace("delete_user_", "")
        if target_uid in BOT_DATA.get("users", {}):
            del BOT_DATA["users"][target_uid]
            save_data()
        await query.answer("🗑️ تم حذف المشترك نهائياً.", show_alert=True)
        await show_manage_users_menu(query.message, edit=True)

    elif data == "add_user_start":
        cancel_btn = InlineKeyboardMarkup([[InlineKeyboardButton("❌ إلغاء", callback_data="admin_panel")]])
        await query.message.edit_text("➕ **أدخل المعرف (Telegram User ID) للمشترك الجديد:**", reply_markup=cancel_btn, parse_mode="Markdown")
        context.user_data['new_user'] = {}
        return ADD_USER_ID

    elif data == "broadcast_start":
        keyboard = [[InlineKeyboardButton("⬅️ رجوع للوحة الأدمن", callback_data="admin_panel")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.message.edit_text("📢 **أرسل الآن نص الإذاعة أو الإعلان الذي تريد إرساله لجميع المشتركين:**", reply_markup=reply_markup, parse_mode="Markdown")
        return BROADCAST_TEXT

# ----------------- خطوات التفاعل والدخول الديناميكي -----------------

async def step_set_site_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    BOT_DATA["settings"]["site_url"] = text
    save_data()
    await update.message.reply_text(f"✅ **تم تحديث رابط المنظومة بنجاح إلى:**\n`{text}`", parse_mode="Markdown", reply_markup=get_admin_reply_keyboard())
    return ConversationHandler.END

async def step_set_site_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    BOT_DATA["settings"]["site_username"] = text
    save_data()
    await update.message.reply_text(f"✅ **تم تحديث اسم مستخدم المنظومة إلى:** `{text}`", parse_mode="Markdown", reply_markup=get_admin_reply_keyboard())
    return ConversationHandler.END

async def step_set_site_pass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    BOT_DATA["settings"]["site_password"] = text
    save_data()
    await update.message.reply_text(f"✅ **تم تحديث كلمة مرور المنظومة بنجاح!**", parse_mode="Markdown", reply_markup=get_admin_reply_keyboard())
    return ConversationHandler.END

async def step_set_admin_wa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    BOT_DATA["settings"]["admin_whatsapp"] = text
    save_data()
    await update.message.reply_text(f"✅ **تم تحديث رقم واتساب الأدمن إلى:** `{text}`", parse_mode="Markdown", reply_markup=get_admin_reply_keyboard())
    return ConversationHandler.END

async def step_quick_add_days(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    uid = context.user_data.get('quick_uid')
    name = context.user_data.get('quick_name', 'مشترك')
    
    try:
        days = int(text)
        expiry = UNLIMITED_EXPIRY if days == 0 else (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")
        
        if "users" not in BOT_DATA:
            BOT_DATA["users"] = {}
            
        BOT_DATA["users"][str(uid)] = {
            "name": name,
            "expiry": expiry,
            "usage_count": 0,
            "last_used": "لم يستخدم بعد",
            "status": "active"
        }
        save_data()
        
        await update.message.reply_text(
            f"✅ **تم تفعيل المشترك بنجاح!**\n\n🆔 الآيدي: `{uid}`\n👤 الاسم: `{name}`\n⏳ ينتهي في: `{expiry}`",
            parse_mode="Markdown",
            reply_markup=get_admin_reply_keyboard()
        )
        try:
            await context.bot.send_message(
                chat_id=int(uid),
                text=f"🎉 **مبروك! تم تفعيل حسابك بنجاح في بوت Tiandy.**\n⏳ تاريخ انتهاء الاشتراك: `{expiry}`\n\nيمكنك الآن إرسال ملفات التفكيك مباشرة لاستخراج الأكواد."
            )
        except Exception:
            pass
    except Exception as e:
        await update.message.reply_text(f"❌ خطأ في إضافة المدة: {e}", reply_markup=get_admin_reply_keyboard())
        
    return ConversationHandler.END

async def step_add_user_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text("❌ معرف غير صالح. يجب أن يتكون الآيدي من أرقام فقط. أعد المحاولة:")
        return ADD_USER_ID
    
    context.user_data['new_user']['id'] = text
    await update.message.reply_text("📌 **أدخل اسم الفني أو المشترك (مثال: فني صيانة طرابلس):**", parse_mode="Markdown")
    return ADD_USER_NAME

async def step_add_user_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['new_user']['name'] = update.message.text.strip()
    await update.message.reply_text("⏳ **أدخل عدد أيام الاشتراك (أو اكتب `0` لاشتراك دائم غير محدود):**", parse_mode="Markdown")
    return ADD_USER_DAYS

async def step_add_user_days(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    user_data_dict = context.user_data.get('new_user', {})
    uid = user_data_dict.get('id')
    name = user_data_dict.get('name')
    
    try:
        days = int(text)
        expiry = UNLIMITED_EXPIRY if days == 0 else (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")
        
        if "users" not in BOT_DATA:
            BOT_DATA["users"] = {}
            
        BOT_DATA["users"][uid] = {
            "name": name,
            "expiry": expiry,
            "usage_count": 0,
            "last_used": "لم يستخدم بعد",
            "status": "active"
        }
        save_data()
        
        await update.message.reply_text(
            f"✅ **تمت إضافة المشترك بنجاح!**\n\n"
            f"🆔 الآيدي: `{uid}`\n"
            f"👤 الاسم: `{name}`\n"
            f"⏳ ينتهي في: `{expiry}`",
            parse_mode="Markdown",
            reply_markup=get_admin_reply_keyboard()
        )
    except Exception as e:
        await update.message.reply_text(f"❌ حدث خطأ في معالجة المدة: {e}", reply_markup=get_admin_reply_keyboard())
        
    return ConversationHandler.END

async def step_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    broadcast_text = update.message.text
    users = BOT_DATA.get("users", {})
    
    success_count = 0
    status_msg = await update.message.reply_text("⏳ **جاري إرسال الإذاعة لجميع المشتركين...**", parse_mode="Markdown")
    
    for uid in users.keys():
        try:
            await context.bot.send_message(chat_id=int(uid), text=f"📢 **تنبيه من إدارة شركة الريادة:**\n\n{broadcast_text}", parse_mode="Markdown")
            success_count += 1
            await asyncio.sleep(0.1)
        except Exception:
            pass
            
    await status_msg.edit_text(f"✅ **تم إرسال الإذاعة بنجاح إلى (`{success_count}`) مشترك.**", parse_mode="Markdown")
    return ConversationHandler.END

async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ تم إلغاء العملية.", reply_markup=get_admin_reply_keyboard())
    return ConversationHandler.END

# ----------------- 5. استقبال المستندات وعملية الفك -----------------

def detect_file_type(file_path: str) -> str:
    try:
        file_size = os.path.getsize(file_path)
        with open(file_path, 'rb') as f:
            content_bytes = f.read()
        content_text = content_bytes.decode('utf-8', errors='ignore')
        dev_matches = len(re.findall(r'(?i)(device|camera|serial|sn)', content_text))
        
        if dev_matches > 2 or file_size > 3072:
            return "batch"
        return "single"
    except Exception:
        return "single"

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_name = update.effective_user.full_name or f"User {user_id}"
    username = update.effective_user.username

    allowed, status = is_user_allowed(user_id)
    if not allowed:
        admin_wa = get_setting("admin_whatsapp", DEFAULT_ADMIN_WHATSAPP)
        wa_link = f"https://wa.me/{admin_wa}?text=مرحباً أستاذ، أريد تفعيل حسابي في بوت Tiandy، معرفي هو: {user_id}"
        keyboard = [[InlineKeyboardButton("💬 مراسلة الأدمن عبر واتساب", url=wa_link)]]
        await update.message.reply_text(f"⛔ **عذراً، غير مسموح بالدخول**\n\n{status}", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        
        if str(user_id) not in BOT_DATA.get("users", {}):
            await notify_admin_unauthorized(context, user_id, user_name, username)
        return

    now = datetime.now()
    if user_id in USER_LAST_REQUEST:
        elapsed = (now - USER_LAST_REQUEST[user_id]).total_seconds()
        if elapsed < COOLDOWN_SECONDS:
            remaining = int(COOLDOWN_SECONDS - elapsed)
            await update.message.reply_text(f"⏱ **يرجى الانتظار (`{remaining}`) ثانية قبل إرسال طلب جديد.**\nللحفاظ على استقرار الخادم وسرعة الاستجابة.")
            return
            
    USER_LAST_REQUEST[user_id] = now

    document = update.message.document
    file_name = document.file_name or "export_file.dat"

    bin_dir = os.path.join(BASE_DIR, "bin")
    os.makedirs(bin_dir, exist_ok=True)
    file_path = os.path.join(bin_dir, f"{user_id}_{file_name}")

    try:
        file = await context.bot.get_file(document.file_id)
        await file.download_to_drive(file_path)
    except Exception as e:
        logging.error(f"Download Error: {e}")
        await update.message.reply_text("❌ حدث خطأ أثناء تحميل الملف من تيليجرام.")
        return

    file_type = detect_file_type(file_path)

    status_msg = await update.message.reply_text(
        f"📂 **تم استلام الملف:** `{file_name}`\n"
        f"🔍 **نوع الطلب:** `{'مجموعة كاميرات' if file_type == 'batch' else 'كاميرا واحدة'}`\n\n"
        f"{get_stage_description(5)}\n{generate_progress_bar(5)}",
        parse_mode="Markdown"
    )

    async with process_lock:
        stop_event = asyncio.Event()
        tracker_task = asyncio.create_task(smooth_progress_tracker(status_msg, stop_event))

        try:
            if file_type == "single":
                security_code, site_username = await process_single_file_on_website(file_path)
                
                stop_event.set()
                await tracker_task
                
                await status_msg.edit_text(
                    f"{get_stage_description(100)}\n{generate_progress_bar(100)}", 
                    parse_mode="Markdown"
                )
                await asyncio.sleep(0.5)

                if security_code:
                    uid_str = str(user_id)
                    stored_name = BOT_DATA.get("users", {}).get(uid_str, {}).get("name", user_name)
                    
                    log_site_extraction_db(user_id, stored_name, site_username, security_code)
                    
                    if uid_str in BOT_DATA.get("users", {}):
                        BOT_DATA["users"][uid_str]["usage_count"] = BOT_DATA["users"][uid_str].get("usage_count", 0) + 1
                        BOT_DATA["users"][uid_str]["last_used"] = datetime.now().strftime("%Y-%m-%d %H:%M")
                        save_data()

                    reply_text = (
                        f"✅ **تم استخراج كود الأمان بنجاح!**\n\n"
                        f"🔑 **Security Code:**\n`{security_code}`"
                    )
                    await status_msg.edit_text(reply_text, parse_mode="Markdown")

                    if user_id != ADMIN_ID:
                        try:
                            await context.bot.send_message(
                                chat_id=ADMIN_ID,
                                text=(
                                    f"ℹ **عملية فك (كاميرا واحدة):**\n"
                                    f"👤 **الفني:** {stored_name} (`{user_id}`)\n"
                                    f"🔑 **الكود:** `{security_code}`"
                                ),
                                parse_mode="Markdown"
                            )
                        except Exception:
                            pass
                else:
                    await status_msg.edit_text("❌ لم يتم استخراج الكود، يرجى التأكد من صحة الملف المرفوع.")
                    
                    if user_id != ADMIN_ID:
                        try:
                            uid_str = str(user_id)
                            stored_name = BOT_DATA.get("users", {}).get(uid_str, {}).get("name", user_name)
                            await context.bot.send_message(
                                chat_id=ADMIN_ID,
                                text=(
                                    f"⚠ **تنبيه فشل عملية فك:**\n"
                                    f"👤 **الفني:** {stored_name} (`{user_id}`)\n"
                                    f"📂 **اسم الملف:** `{file_name}`\n"
                                    f"❌ لم يتم العثور على الكود في الاستجابة."
                                ),
                                parse_mode="Markdown"
                            )
                        except Exception:
                            pass

            else:
                downloaded_file = await process_batch_file_on_website(file_path, bin_dir)

                stop_event.set()
                await tracker_task

                await status_msg.edit_text(
                    f"{get_stage_description(100)}\n{generate_progress_bar(100)}", 
                    parse_mode="Markdown"
                )
                await asyncio.sleep(0.5)

                if downloaded_file and os.path.exists(downloaded_file):
                    await status_msg.edit_text("✅ **تم تجهيز ملف التفكيك بنجاح! جاري الإرسال...**", parse_mode="Markdown")
                    
                    with open(downloaded_file, 'rb') as doc_to_send:
                        await context.bot.send_document(
                            chat_id=user_id,
                            document=doc_to_send,
                            filename=os.path.basename(downloaded_file),
                            caption=(
                                f"📂 **ملف تفكيك الكاميرات جاهز!**\n\n"
                                f"📌 قم بتنزيل هذا الملف واستيراده داخل برنامج Search Config Tool لتطبيق الفك على جميع الكاميرات مباشرة."
                            )
                        )
                    if os.path.exists(downloaded_file):
                        os.remove(downloaded_file)
                else:
                    await status_msg.edit_text("❌ **تعذر إكمال العملية.** يرجى التأكد من صحة الملف المرفوع ومحاولة الإرسال مجدداً.")

        except Exception as e:
            stop_event.set()
            logging.error(f"Processing error: {e}")
            await status_msg.edit_text("⚠ حدث خطأ غير متوقع أثناء معالجة الطلب.")
        finally:
            if os.path.exists(file_path):
                os.remove(file_path)
            # 🧹 تنظيف الذاكرة المؤقتة لـ Python وإجبارها على تحرير الـ RAM بعد انتهاء العملية
            gc.collect()

# ----------------- 6. الأتمتة عبر Playwright (مع تحسينات الذاكرة) -----------------

async def process_single_file_on_website(file_path: str) -> tuple[str, str]:
    site_url = get_setting("site_url", DEFAULT_SITE_URL)
    site_user = get_setting("site_username", DEFAULT_USERNAME)
    site_pass = get_setting("site_password", DEFAULT_PASSWORD)

    async with async_playwright() as p:
        # 🚀 إعدادات Chromium لتحسين استهلاك الذاكرة وتفادي مشاكل Linux /dev/shm
        browser = await p.chromium.launch(
            headless=True,
            args=[
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-dev-shm-usage',
                '--single-process'
            ]
        )
        page = await browser.new_page()

        try:
            await page.goto(site_url, timeout=60000)
            await page.wait_for_selector('input[type="text"]', timeout=15000)
            await page.fill('input[type="text"]', site_user)
            await page.fill('input[type="password"]', site_pass)
            await page.click('button[type="button"], button[type="submit"]')
            await page.wait_for_load_state('networkidle')

            await page.wait_for_selector('text="Password manage"', timeout=15000)
            await page.click('text="Password manage"')
            await page.wait_for_timeout(1000)
            await page.click('text="Reset password"')
            await page.wait_for_load_state('networkidle')

            async with page.expect_file_chooser() as fc_info:
                if await page.is_visible('text="Browse"'):
                    await page.click('text="Browse"')
                else:
                    await page.click('.el-upload, button:has-text("Browse")')
            
            file_chooser = await fc_info.value
            await file_chooser.set_files(file_path)
            await page.wait_for_timeout(1000)

            if await page.is_visible('text="Import"'):
                await page.click('text="Import"')
            else:
                await page.click('button:has-text("Import")')

            await page.wait_for_timeout(4000)

            body_text = await page.inner_text('body')
            match = re.search(r'Security code:\s*([a-zA-Z0-9]+)', body_text, re.IGNORECASE)

            sec_code = None
            if match:
                raw_code = match.group(1).strip()
                sec_code = re.sub(r'(?i)copy$', '', raw_code).strip()

            return sec_code, site_user
        finally:
            await browser.close()

async def process_batch_file_on_website(file_path: str, download_dir: str) -> str:
    site_url = get_setting("site_url", DEFAULT_SITE_URL)
    site_user = get_setting("site_username", DEFAULT_USERNAME)
    site_pass = get_setting("site_password", DEFAULT_PASSWORD)
    downloaded_file_path = None

    async with async_playwright() as p:
        # 🚀 إعدادات Chromium لتحسين استهلاك الذاكرة وتفادي مشاكل Linux /dev/shm
        browser = await p.chromium.launch(
            headless=True,
            args=[
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-dev-shm-usage',
                '--single-process'
            ]
        )
        context = await browser.new_context(accept_downloads=True)
        page = await context.new_page()

        try:
            await page.goto(site_url, timeout=60000)
            await page.wait_for_selector('input[type="text"]', timeout=15000)
            await page.fill('input[type="text"]', site_user)
            await page.fill('input[type="password"]', site_pass)
            await page.click('button[type="button"], button[type="submit"]')
            await page.wait_for_load_state('networkidle')

            await page.wait_for_selector('text="Password manage"', timeout=15000)
            await page.click('text="Password manage"')
            await page.wait_for_timeout(1000)
            await page.click('text="Reset password"')
            await page.wait_for_load_state('networkidle')

            async with page.expect_file_chooser() as fc_info:
                if await page.is_visible('text="Browse"'):
                    await page.click('text="Browse"')
                else:
                    await page.click('.el-upload, button:has-text("Browse")')
            
            file_chooser = await fc_info.value
            await file_chooser.set_files(file_path)
            await page.wait_for_timeout(1000)

            if await page.is_visible('text="Import"'):
                await page.click('text="Import"')
            else:
                await page.click('button:has-text("Import")')

            export_selector = 'button:has-text("Export"), .el-button--primary:has-text("Export")'
            await page.wait_for_selector(export_selector, timeout=25000)
            await page.wait_for_timeout(1500)

            async with page.expect_download(timeout=25000) as download_info:
                await page.click(export_selector)

            download = await download_info.value
            file_name = download.suggested_filename or "Batch_Export.dat"
            downloaded_file_path = os.path.join(download_dir, f"Decrypted_{file_name}")
            await download.save_as(downloaded_file_path)

            return downloaded_file_path

        except Exception as e:
            logging.error(f"Playwright Batch Error: {e}")
            return None
        finally:
            await browser.close()

# ----------------- 7. خادم الويب الوهمي لـ Render (Port Binding) -----------------

class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running successfully!")
    def log_message(self, format, *args):
        pass

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), SimpleHandler)
    server.serve_forever()

# ----------------- 8. التشغيل الرئيسي -----------------

def main():
    try:
        server_thread = threading.Thread(target=run_web_server, daemon=True)
        server_thread.start()

        app = Application.builder().token(BOT_TOKEN).build()

        conv_handler = ConversationHandler(
            entry_points=[
                CallbackQueryHandler(handle_callback, pattern="^add_user_start$"),
                CallbackQueryHandler(handle_callback, pattern="^broadcast_start$"),
                CallbackQueryHandler(handle_callback, pattern="^change_site_url$"),
                CallbackQueryHandler(handle_callback, pattern="^change_site_user$"),
                CallbackQueryHandler(handle_callback, pattern="^change_site_pass$"),
                CallbackQueryHandler(handle_callback, pattern="^change_admin_wa$"),
                CallbackQueryHandler(handle_callback, pattern="^quick_add_")
            ],
            states={
                ADD_USER_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_add_user_id)],
                ADD_USER_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_add_user_name)],
                ADD_USER_DAYS: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_add_user_days)],
                BROADCAST_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_broadcast)],
                SET_SITE_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_set_site_url)],
                SET_SITE_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_set_site_user)],
                SET_SITE_PASS: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_set_site_pass)],
                SET_ADMIN_WA: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_set_admin_wa)],
                QUICK_ADD_DAYS: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_quick_add_days)]
            },
            fallbacks=[
                CommandHandler("cancel", cancel_conversation),
                CallbackQueryHandler(cancel_callback, pattern="^admin_panel$"),
                MessageHandler(filters.Regex("^🎛️ لوحة التحكم$"), start_command)
            ],
            per_message=False
        )

        app.add_handler(conv_handler)
        app.add_handler(CommandHandler("start", start_command))
        app.add_handler(CallbackQueryHandler(handle_callback))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_buttons))
        app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

        print("=" * 60)
        print("🤖 تم تشغيل البوت ومعالجة التنقل والانتقال بين القوائم...")
        print("=" * 60)
        app.run_polling(drop_pending_updates=True)

    except Exception as e:
        print(f"\n❌ حدث خطأ عند التشغيل: {e}")

if __name__ == '__main__':
    main()
