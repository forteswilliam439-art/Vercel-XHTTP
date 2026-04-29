import os
import time
import asyncio
import sqlite3
import random
import requests
import threading
from functools import partial
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pyrogram.enums import ChatMemberStatus
from pyrogram.errors import UserNotParticipant

# ================= تنظیمات ==============
API_ID = 20293679 
API_HASH = "e79617d740ae6defd2bb3e1d6d9d77d0"
BOT_TOKEN = "7979418740:AAHpLGN3CK5p9PKewa4Pw1GRGkGRVRTbx0M"

BALE_BOT_TOKEN = "645415116:c_wey0febjqfaxgzqCJ0r0fGLjwdr6U-tss"
BALE_BOT_USERNAME = "Hadika_bot" 

ADMINS = [8177026946, 6075131517] 
SUPPORT_ID = "grlmor"

# ================ دیتابیس و Thread-Safety ================
conn = sqlite3.connect("bot_database.db", check_same_thread=False)
cursor = conn.cursor()
db_lock = asyncio.Lock()

cursor.execute('''CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, max_upload_mb REAL)''')
cursor.execute('''CREATE TABLE IF NOT EXISTS charge_log (user_id INTEGER PRIMARY KEY, total_charged_mb REAL DEFAULT 0, total_used_mb REAL DEFAULT 0)''')
cursor.execute('''CREATE TABLE IF NOT EXISTS banned_users (user_id INTEGER PRIMARY KEY)''')
cursor.execute('''CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)''')
cursor.execute('''CREATE TABLE IF NOT EXISTS force_channels (chat_id TEXT PRIMARY KEY, link TEXT)''') 
cursor.execute('''CREATE TABLE IF NOT EXISTS linked_accounts (tg_user_id INTEGER PRIMARY KEY, bale_user_id TEXT)''') 
cursor.execute('''CREATE TABLE IF NOT EXISTS referrals (user_id INTEGER PRIMARY KEY, inviter_id INTEGER, rewarded INTEGER DEFAULT 0)''') 
cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('help_text', 'متن راهنمای پیش‌فرض.')")
conn.commit()

# --- توابع دیتابیس ---
async def get_linked_bale_account(tg_user_id):
    async with db_lock:
        cursor.execute("SELECT bale_user_id FROM linked_accounts WHERE tg_user_id = ?", (tg_user_id,))
        row = cursor.fetchone()
        return row[0] if row else None

async def set_user_limit(user_id, max_mb):
    async with db_lock:
        cursor.execute("REPLACE INTO users (user_id, max_upload_mb) VALUES (?, ?)", (user_id, float(max_mb)))
        cursor.execute("""
            INSERT INTO charge_log (user_id, total_charged_mb, total_used_mb) VALUES (?, ?, 0)
            ON CONFLICT(user_id) DO UPDATE SET total_charged_mb = total_charged_mb + ?
        """, (user_id, float(max_mb), float(max_mb)))
        conn.commit()

async def add_user_limit(user_id, amount_mb):
    async with db_lock:
        cursor.execute("SELECT max_upload_mb FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        if row:
            new_limit = row[0] + amount_mb
            cursor.execute("UPDATE users SET max_upload_mb = ? WHERE user_id = ?", (new_limit, user_id))
        else:
            new_limit = amount_mb
            cursor.execute("INSERT INTO users (user_id, max_upload_mb) VALUES (?, ?)", (user_id, new_limit))
            
        cursor.execute("""
            INSERT INTO charge_log (user_id, total_charged_mb, total_used_mb) VALUES (?, ?, 0)
            ON CONFLICT(user_id) DO UPDATE SET total_charged_mb = total_charged_mb + ?
        """, (user_id, amount_mb, amount_mb))
        conn.commit()

async def get_user_limit(user_id):
    async with db_lock:
        cursor.execute("SELECT max_upload_mb FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        return row[0] if row else 0.0

async def reduce_user_limit(user_id, amount_mb):
    async with db_lock:
        cursor.execute("SELECT max_upload_mb FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        current = row[0] if row else 0.0
        new_limit = max(0.0, current - amount_mb)
        
        cursor.execute("UPDATE users SET max_upload_mb = ? WHERE user_id = ?", (new_limit, user_id))
        cursor.execute("""
            INSERT INTO charge_log (user_id, total_charged_mb, total_used_mb) VALUES (?, 0, ?)
            ON CONFLICT(user_id) DO UPDATE SET total_used_mb = total_used_mb + ?
        """, (user_id, amount_mb, amount_mb))
        conn.commit()
        return new_limit

async def get_all_users_usage():
    async with db_lock:
        cursor.execute("""
            SELECT u.user_id, COALESCE(c.total_charged_mb, 0), COALESCE(c.total_used_mb, 0), u.max_upload_mb
            FROM users u LEFT JOIN charge_log c ON u.user_id = c.user_id ORDER BY u.user_id
        """)
        return cursor.fetchall()

async def get_all_user_ids():
    async with db_lock:
        cursor.execute("SELECT user_id FROM users")
        return [row[0] for row in cursor.fetchall()]

async def get_user_charge_log(user_id):
    async with db_lock:
        cursor.execute("SELECT total_charged_mb, total_used_mb FROM charge_log WHERE user_id = ?", (user_id,))
        return cursor.fetchone()

async def is_banned(user_id):
    async with db_lock:
        cursor.execute("SELECT user_id FROM banned_users WHERE user_id = ?", (user_id,))
        return cursor.fetchone() is not None

async def set_ban_status(user_id, status):
    async with db_lock:
        if status: cursor.execute("REPLACE INTO banned_users (user_id) VALUES (?)", (user_id,))
        else: cursor.execute("DELETE FROM banned_users WHERE user_id = ?", (user_id,))
        conn.commit()

async def get_setting(key):
    async with db_lock:
        cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = cursor.fetchone()
        return row[0] if row else ""

async def set_setting(key, value):
    async with db_lock:
        cursor.execute("REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
        conn.commit()

async def check_and_gift_new_user(user_id):
    async with db_lock:
        cursor.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
        exists = cursor.fetchone()
    if not exists:
        await set_user_limit(user_id, 5.0) # حجم اولیه به ۵ مگابایت تغییر یافت
        return True
    return False

# --- توابع جوین اجباری و سیستم رفرال ---
async def get_force_channels():
    async with db_lock:
        cursor.execute("SELECT chat_id, link FROM force_channels")
        return cursor.fetchall()

async def add_force_channel(chat_id, link):
    async with db_lock:
        cursor.execute("REPLACE INTO force_channels (chat_id, link) VALUES (?, ?)", (chat_id, link))
        conn.commit()

async def remove_force_channel(chat_id):
    async with db_lock:
        cursor.execute("DELETE FROM force_channels WHERE chat_id = ?", (chat_id,))
        conn.commit()

async def check_membership(client, user_id):
    if user_id in ADMINS: return True, []
    channels = await get_force_channels()
    not_joined = []
    for chat_id, link in channels:
        try:
            member = await client.get_chat_member(chat_id, user_id)
            if member.status in [ChatMemberStatus.LEFT, ChatMemberStatus.BANNED]:
                not_joined.append((chat_id, link))
        except UserNotParticipant:
            not_joined.append((chat_id, link))
        except Exception:
            pass
    return len(not_joined) == 0, not_joined

async def process_referral_reward(client, user_id):
    async with db_lock:
        cursor.execute("SELECT inviter_id, rewarded FROM referrals WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        
    if row and row[1] == 0:
        inviter_id = row[0]
        reward_mb = random.randint(1, 30) # پاداش رندوم بین ۱ تا ۳۰ مگابایت
        
        async with db_lock:
            cursor.execute("UPDATE referrals SET rewarded = 1 WHERE user_id = ?", (user_id,))
            conn.commit()
            
        await add_user_limit(inviter_id, float(reward_mb))
        
        try:
            await client.send_message(inviter_id, f"🎉 تبریک! یک کاربر با لینک شما با موفقیت عضو ربات شد.\n🎁 **{reward_mb} مگابایت** حجم هدیه به حساب شما اضافه شد!")
        except:
            pass

# ================ کلاینت و سیستم صف‌بندی یک‌به‌یک ================
bot = Client("tg_bot_session", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
user_steps = {}

bale_session = requests.Session()

bale_lock = asyncio.Lock()    
rubika_lock = asyncio.Lock()  
beta_lock = asyncio.Lock()    
queue_counts = {"rubika": 0, "bale": 0, "beta": 0}

cancel_events = {} 

class DownloadCancelledError(Exception): pass

def get_cancel_event(user_id):
    if user_id not in cancel_events: cancel_events[user_id] = asyncio.Event()
    return cancel_events[user_id]

def reset_cancel_event(user_id):
    if user_id in cancel_events: cancel_events[user_id].clear()

def trigger_cancel(user_id):
    if user_id in cancel_events: cancel_events[user_id].set()

def is_cancelled(user_id):
    return cancel_events[user_id].is_set() if user_id in cancel_events else False

def get_cancel_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو عملیات", callback_data="cancel")]])

def get_upload_cancel_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🛑 لغو آپلود فوری", callback_data="cancel_upload")]])

async def download_progress(current, total, status_msg, last_update_time, user_id):
    if is_cancelled(user_id):
        raise DownloadCancelledError("Cancelled by user")
        
    now = time.time()
    if now - last_update_time[0] > 3:
        percentage = current * 100 / total
        try:
            await status_msg.edit_text(
                f"📥 در حال دانلود از تلگرام...\n"
                f"📊 پیشرفت: **{percentage:.1f}%**\n"
                f"💾 حجم: {round(current / (1024*1024), 2)} MB / {round(total / (1024*1024), 2)} MB",
                reply_markup=get_upload_cancel_keyboard()
            )
        except: pass
        last_update_time[0] = now

def split_file_sync(filepath):
    part_files = []
    chunk_size = 20 * 1024 * 1024  # حجم پارت‌های بله به ۲۰ مگابایت کاهش یافت
    part_num = 1
    with open(filepath, 'rb') as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            part_name = f"{filepath}.part{part_num:03d}"
            with open(part_name, 'wb') as p:
                p.write(chunk)
            part_files.append(part_name)
            part_num += 1
    return part_files

def upload_bale_sync_request(session, url, part_file, target_input, caption, is_single_part):
    with open(part_file, 'rb') as f:
        files = {'document': (os.path.basename(part_file), f)} 
        data = {'chat_id': target_input}
        if is_single_part:
            data['caption'] = caption
        return session.post(url, data=data, files=files, timeout=120) 

# ================ ماژول آپلود اصلی ================
async def process_file_upload(client, user_id, chat_id, file_msg_id, platform, target_input, file_size_mb, file_size_bytes, status_msg):
    target_lock = None
    p_name = ""
    if platform == "rubika":
        target_lock = rubika_lock
        p_name = "روبیکا 🔴"
    elif platform == "bale":
        target_lock = bale_lock
        p_name = "بله 🟢"
    elif platform == "beta":
        target_lock = beta_lock
        p_name = "سرور بتا 🧪"

    in_queue = False
    reset_cancel_event(user_id)

    if target_lock.locked():
        queue_counts[platform] += 1
        in_queue = True
        user_steps[user_id] = {"step": "waiting_in_queue"}
        try:
            await status_msg.edit_text(f"⏳ **ربات در حال سرویس‌دهی است.**\nشما نفر **{queue_counts[platform]}** در صف انتظار {p_name} هستید...", reply_markup=get_upload_cancel_keyboard())
        except: pass
    else:
        user_steps[user_id] = {"step": "uploading"}
        try:
            await status_msg.edit_text("⏳ در حال آماده‌سازی...", reply_markup=get_upload_cancel_keyboard())
        except: pass

    try:
        await target_lock.acquire()
    except Exception as e:
        if in_queue: queue_counts[platform] -= 1
        raise e
        
    if in_queue: queue_counts[platform] -= 1

    file_path = None
    files_to_upload = []

    try:
        if is_cancelled(user_id):
            try: await status_msg.edit_text("🛑 عملیات لغو شد.")
            except: pass
            return

        user_steps[user_id] = {"step": "uploading"}
        if in_queue:
            try: await status_msg.edit_text("✅ **نوبت شما رسید!** در حال شروع عملیات...", reply_markup=get_upload_cancel_keyboard())
            except: pass

        # ================== منطق روبیکا ==================
        if platform == "rubika":
            try: await status_msg.edit_text("🔍 استخراج GUID...", reply_markup=get_upload_cancel_keyboard())
            except: pass
            
            from rubpy import Client as RubikaClient
            async with RubikaClient("tiker") as rubika_app:
                if is_cancelled(user_id): return
                
                target_guid = None
                if "joing/" in target_input:
                    res = await rubika_app.join_group(target_input.split("/")[-1])
                    target_guid = res.group.group_guid
                elif "joinc/" in target_input:
                    try: res = await rubika_app.join_channel_by_link(target_input)
                    except: res = await rubika_app.join_channel_by_link(target_input.split("/")[-1])
                    target_guid = res.channel.channel_guid
                else:
                    username = target_input.split("/")[-1].replace("@", "")
                    res = await rubika_app.get_object_by_username(username)
                    if hasattr(res, 'user') and res.user: target_guid = res.user.user_guid
                    elif hasattr(res, 'channel') and res.channel: target_guid = res.channel.channel_guid
                    elif hasattr(res, 'group') and res.group: target_guid = res.group.group_guid
                    elif isinstance(res, dict):
                        if 'user' in res: target_guid = res['user']['user_guid']
                        elif 'channel' in res: target_guid = res['channel']['channel_guid']
                        elif 'group' in res: target_guid = res['group']['group_guid']

                if not target_guid:
                    try: await status_msg.edit_text("❌ یوزرنیم نامعتبر است.")
                    except: pass
                    return

                try: await status_msg.edit_text(f"📥 شروع دانلود از تلگرام...", reply_markup=get_upload_cancel_keyboard())
                except: pass
                
                try:
                    file_message = await client.get_messages(chat_id, file_msg_id)
                    if file_message is None or file_message.empty:
                        try: await status_msg.edit_text("❌ پیام اصلی در تلگرام یافت نشد.")
                        except: pass
                        return
                except Exception:
                    try: await status_msg.edit_text("❌ خطا در یافتن پیام.")
                    except: pass
                    return
                
                caption = file_message.caption if file_message.caption else ""
                last_update_time = [time.time()]
                
                try:
                    file_path = await file_message.download(
                        progress=download_progress,
                        progress_args=(status_msg, last_update_time, user_id)
                    )
                except DownloadCancelledError:
                    try: await status_msg.edit_text("🛑 آپلود لغو شد.")
                    except: pass
                    return
                except Exception:
                    try: await status_msg.edit_text("❌ خطا در دانلود.")
                    except: pass
                    return
                
                if not file_path or not os.path.exists(file_path): return
                
                downloaded_size = os.path.getsize(file_path)
                if file_size_bytes > 0 and downloaded_size != file_size_bytes:
                    try: await status_msg.edit_text("❌ دانلود ناقص انجام شد!")
                    except: pass
                    return

                filename, file_extension = os.path.splitext(file_path)
                if not file_extension:
                    new_file_path = file_path + ".dat"
                    os.rename(file_path, new_file_path)
                    file_path = new_file_path

                max_retries = 30 if user_id in ADMINS else (10 if file_size_mb <= 100 else 1)
                upload_success = False

                for attempt in range(1, max_retries + 1):
                    if is_cancelled(user_id):
                        try: await status_msg.edit_text("🛑 آپلود لغو شد.")
                        except: pass
                        return
                    try:
                        try: await status_msg.edit_text(f"📤 در حال آپلود به روبیکا...\n⏳ تلاش {attempt} از {max_retries}", reply_markup=get_upload_cancel_keyboard())
                        except: pass
                        
                        await rubika_app.send_document(target_guid, document=file_path)
                        upload_success = True
                        break
                    except Exception as e:
                        if attempt < max_retries: await asyncio.sleep(2)
                        else: raise e

        # ================== منطق بله / بتا ==================
        elif platform in ["bale", "beta"]:
            try: await status_msg.edit_text(f"📥 شروع دانلود از تلگرام...", reply_markup=get_upload_cancel_keyboard())
            except: pass
            
            try:
                file_message = await client.get_messages(chat_id, file_msg_id)
                if file_message is None or file_message.empty: return
            except Exception: return
            
            caption = file_message.caption if file_message.caption else ""
            last_update_time = [time.time()]
            
            try:
                file_path = await file_message.download(
                    progress=download_progress,
                    progress_args=(status_msg, last_update_time, user_id)
                )
            except DownloadCancelledError:
                try: await status_msg.edit_text("🛑 آپلود لغو شد.")
                except: pass
                return
            except Exception: return
            if not file_path or not os.path.exists(file_path): return

            filename, file_extension = os.path.splitext(file_path)
            if not file_extension:
                new_file_path = file_path + ".dat"
                os.rename(file_path, new_file_path)
                file_path = new_file_path

            if file_size_mb > 20: # بررسی نیاز به پارت‌بندی ۲۰ مگابایتی
                try: await status_msg.edit_text("✂️ در حال پارت‌بندی (۲۰ مگابایتی)...")
                except: pass
                loop = asyncio.get_event_loop()
                files_to_upload = await loop.run_in_executor(None, split_file_sync, file_path)
            else:
                files_to_upload = [file_path]
            
            upload_success = False
            url = f"https://tapi.bale.ai/bot{BALE_BOT_TOKEN}/sendDocument"
            total_parts = len(files_to_upload)
            
            for idx, part_file in enumerate(files_to_upload):
                part_success = False
                for attempt in range(1, 16):
                    if is_cancelled(user_id):
                        try: await status_msg.edit_text("🛑 آپلود لغو شد.")
                        except: pass
                        return
                    try:
                        try: await status_msg.edit_text(f"📤 در حال آپلود پارت {idx+1} از {total_parts} به {p_name}...\n⏳ تلاش {attempt}/15", reply_markup=get_upload_cancel_keyboard())
                        except: pass
                        
                        is_single = (total_parts == 1)
                        loop = asyncio.get_event_loop()
                        response = await loop.run_in_executor(
                            None, 
                            partial(upload_bale_sync_request, bale_session, url, part_file, target_input, caption, is_single)
                        )
                        if response.status_code == 200:
                            part_success = True
                            await asyncio.sleep(3)
                            break
                        else: await asyncio.sleep(4)
                    except Exception: await asyncio.sleep(4)
                if not part_success:
                    try: await status_msg.edit_text(f"❌ خطا در آپلود پارت {idx+1}. عملیات متوقف شد.")
                    except: pass
                    return
                    
            upload_success = True
            if total_parts > 1 or platform == "beta":
                msg_url = f"https://tapi.bale.ai/bot{BALE_BOT_TOKEN}/sendMessage"
                if platform == "beta": final_text = f"/DONE {user_id}\n{caption}"
                else: final_text = f"✅ تمامی {total_parts} پارت فایل با موفقیت دریافت شد.\n\n📝 کپشن فایل اصلی:\n{caption}"
                try: bale_session.post(msg_url, json={'chat_id': target_input, 'text': final_text}, timeout=10)
                except: pass

        # =================== پایان موفقیت‌آمیز ===================
        if upload_success:
            remaining = await get_user_limit(user_id)
            if user_id not in ADMINS: remaining = await reduce_user_limit(user_id, file_size_mb)
            success_text = f"✅ با موفقیت به {p_name} ارسال شد! 🎉\n"
            if platform in ["bale", "beta"] and len(files_to_upload) > 1:
                success_text += f"📦 تعداد پارت‌ها: {len(files_to_upload)} پارت\n"
            if user_id not in ADMINS:
                success_text += f"📊 حجم مصرفی: {round(file_size_mb, 2)} MB\n📉 باقیمانده: {round(remaining, 2)} MB"
            
            try: await status_msg.edit_text(success_text)
            except: pass

    except Exception as e:
        if not is_cancelled(user_id): 
            try: await status_msg.edit_text(f"❌ خطا:\n`{str(e)}`")
            except: pass
    finally:
        target_lock.release()
        user_steps[user_id] = {"step": "idle"}
        reset_cancel_event(user_id)
        if file_path and os.path.exists(file_path):
            try: os.remove(file_path)
            except: pass
        for pf in files_to_upload:
            if pf != file_path and os.path.exists(pf):
                try: os.remove(pf)
                except: pass

# ================ مدیریت پیام‌ها ================

@bot.on_message(filters.command("start") & filters.private)
async def start_command(client, message):
    user_id = message.from_user.id
    if await is_banned(user_id): return

    # ثبت معرف (رفرال)
    command_parts = message.text.split()
    if len(command_parts) > 1 and command_parts[1].startswith("ref_"):
        inviter_id = command_parts[1].split("_")[1]
        if inviter_id.isdigit() and int(inviter_id) != user_id:
            async with db_lock:
                cursor.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
                if not cursor.fetchone():
                    cursor.execute("INSERT OR IGNORE INTO referrals (user_id, inviter_id) VALUES (?, ?)", (user_id, int(inviter_id)))
                    conn.commit()

    is_member, not_joined = await check_membership(client, user_id)
    if not is_member:
        buttons = [[InlineKeyboardButton("عضویت در کانال 📢", url=link)] for _, link in not_joined]
        buttons.append([InlineKeyboardButton("تایید عضویت ✅", callback_data="check_join_callback")])
        await message.reply_text("⛔️ **برای استفاده از ربات لطفا ابتدا در کانال‌های زیر عضو شوید:**", reply_markup=InlineKeyboardMarkup(buttons))
        return

    # پردازش پاداش رفرال اگر کاربر از قبل عضو نبوده و الان تایید شده
    await process_referral_reward(client, user_id)

    user_steps[user_id] = {"step": "idle"}
    is_new = await check_and_gift_new_user(user_id)
    
    if user_id in ADMINS:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📤 آپلود فایل جدید", callback_data="start_upload")],
            [InlineKeyboardButton("⚙️ تنظیم حجم کاربر", callback_data="set_user_limit"), InlineKeyboardButton("📊 وضعیت کاربران", callback_data="admin_users_page_0")],
            [InlineKeyboardButton("📝 تنظیم متن راهنما", callback_data="set_help_text"), InlineKeyboardButton("📢 ارسال همگانی", callback_data="broadcast_msg")],
            [InlineKeyboardButton("🚫 بن کاربر", callback_data="ban_user"), InlineKeyboardButton("✅ حذف بن", callback_data="unban_user")],
            [InlineKeyboardButton("➕ افزودن کانال قفل", callback_data="add_channel"), InlineKeyboardButton("➖ حذف کانال قفل", callback_data="remove_channel")]
        ])
        text = "👋 ادمین عزیز خوش آمدید."
    else:
        user_limit = await get_user_limit(user_id)
        welcome_text = "🎁 به ربات خوش آمدید! شما **۵ مگابایت** حجم هدیه دریافت کردید برای افزایش حجم از طریق پشتیبانی حجم خریداری کنید.\n\n" if is_new else ""
        
        if user_limit <= 0:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("❓ راهنما", callback_data="show_help"), InlineKeyboardButton("👤 حساب کاربری", callback_data="user_profile")],
                [InlineKeyboardButton("🔗 دعوت دوستان (هدیه)", callback_data="my_invite_link")],
                [InlineKeyboardButton("📞 ارتباط با پشتیبانی برای شارژ", url=f"https://t.me/{SUPPORT_ID}")]
            ])
            text = (f"{welcome_text}⚠️ **کاربر عزیز، حجم فعلی شما برای آپلود به پایان رسیده است.**\n\n"
                    f"🔸 با دعوت از دوستان خود به صورت کاملاً رایگان حجم هدیه دریافت کنید!\n"
                    f"🔹 برای خرید اشتراک و افزایش حجم، به پشتیبانی پیام دهید:\n"
                    f"🆔 @{SUPPORT_ID}")
        else:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("📤 آپلود فایل", callback_data="start_upload")],
                [InlineKeyboardButton("❓ راهنما", callback_data="show_help"), InlineKeyboardButton("👤 حساب کاربری", callback_data="user_profile")],
                [InlineKeyboardButton("🔗 دعوت دوستان (هدیه)", callback_data="my_invite_link")],
                [InlineKeyboardButton("📞 پشتیبانی", url=f"https://t.me/{SUPPORT_ID}")]
            ])
            text = (f"{welcome_text}👋 کاربر عزیز، به ربات خوش آمدید.\n"
                    f"📊 حجم باقیمانده شما: **{round(user_limit, 2)} MB**\n\n"
                    f"🔸 در صورت نیاز به شارژ مجدد، لطفاً ابتدا بخش **راهنما** را مطالعه کرده و سپس به آیدی زیر پیام دهید:\n"
                    f"🆔 @{SUPPORT_ID}")

    await message.reply_text(text, reply_markup=keyboard)

@bot.on_callback_query()
async def handle_callbacks(client, callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    if await is_banned(user_id):
        await callback_query.answer("⛔ شما مسدود شده‌اید.", show_alert=True)
        return

    data = callback_query.data

    if data == "check_join_callback":
        is_member, not_joined = await check_membership(client, user_id)
        if is_member:
            await process_referral_reward(client, user_id) # اهدای پاداش دعوت‌کننده در صورت عضویت موفق
            await callback_query.message.delete()
            await start_command(client, callback_query.message)
        else:
            await callback_query.answer("❌ شما هنوز در تمام کانال‌ها عضو نشده‌اید!", show_alert=True)

    elif data == "my_invite_link":
        bot_info = await client.get_me()
        bot_username = bot_info.username
        invite_link = f"https://t.me/{bot_username}?start=ref_{user_id}"
        
        text = (f"🎁 **سیستم دعوت دوستان**\n\n"
                f"با ارسال لینک زیر برای دوستانتان، پس از اینکه آنها در ربات استارت زدند و در کانال‌های اجباری عضو شدند، "
                f"به صورت **تصادفی بین ۱ تا ۳۰ مگابایت** حجم رایگان به حساب شما واریز می‌شود!\n\n"
                f"🔗 لینک اختصاصی شما جهت کپی کردن:\n`{invite_link}`")
        try: await callback_query.message.edit_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="cancel")]]))
        except: pass

    elif data == "cancel":
        user_steps[user_id] = {"step": "idle"}
        try: await callback_query.message.edit_text("🚫 عملیات لغو شد. /start را بفرستید.")
        except: pass

    elif data == "cancel_upload":
        current_step = user_steps.get(user_id, {}).get("step", "idle")
        if current_step in ("uploading", "waiting_in_queue"):
            trigger_cancel(user_id)
            user_steps[user_id] = {"step": "idle"}
            try: await callback_query.message.edit_text("🛑 **عملیات لغو شد!**\n/start را بزنید.")
            except: pass
        elif current_step in ("waiting_for_file", "waiting_for_link", "waiting_for_platform"):
            user_steps[user_id] = {"step": "idle"}
            try: await callback_query.message.edit_text("🚫 عملیات لغو شد.")
            except: pass
        else:
            await callback_query.answer("⚠️ عملیاتی در حال اجرا نیست.", show_alert=True)
            
    elif data == "show_help":
        help_text = await get_setting("help_text")
        try: await callback_query.message.edit_text(f"📖 **راهنما:**\n\n{help_text}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="cancel")]]))
        except: pass

    elif data == "user_profile":
        limit = await get_user_limit(user_id)
        row = await get_user_charge_log(user_id)
        charged, used = (row[0], row[1]) if row else (0, 0)
        
        text = f"👤 **حساب کاربری شما**\n🆔 آیدی: `{user_id}`\n💰 کل حجم دریافتی: **{round(charged, 2)} MB**\n📤 کل حجم مصرف شده: **{round(used, 2)} MB**\n📦 حجم باقیمانده: **{round(limit, 2)} MB**"
        try: await callback_query.message.edit_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="cancel")]]))
        except: pass

    elif data.startswith("platform_"):
        user_data = user_steps.get(user_id, {})
        if user_data.get("step") != "waiting_for_platform": return
        
        platform = data.split("_")[1] 
        file_size_mb = user_data.get("file_size_mb", 0)

        if platform == "bale":
            limit = await get_user_limit(user_id)
            if user_id not in ADMINS and limit < 500:
                await callback_query.answer("❌ آپلود در بله مخصوص کاربرانی است که بیشتر از ۵۰۰ مگابایت اشتراک دارند.", show_alert=True)
                return
            if file_size_mb > 1000:
                await callback_query.answer("❌ در پلتفرم بله نهایتاً تا حجم ۱ گیگابایت پشتیبانی می‌شود.", show_alert=True)
                return

            bale_account = await get_linked_bale_account(user_id)
            if not bale_account:
                link = f"https://ble.ir/{BALE_BOT_USERNAME}?start={user_id}"
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔗 ۱. کلیک برای اتصال به حساب بله", url=link)],
                    [InlineKeyboardButton("🔄 ۲. حساب را متصل کردم (بروزرسانی)", callback_data="platform_bale")],
                    [InlineKeyboardButton("🔙 بازگشت و لغو", callback_data="cancel")]
                ])
                try: await callback_query.message.edit_text(
                    "⚠️ **شما هنوز حساب بله خود را متصل نکرده‌اید!**\n\n"
                    "برای اینکه ربات فایل را مستقیماً برای شما در بله بفرستد:\n"
                    "۱. روی دکمه **اتصال به حساب بله** کلیک کنید.\n"
                    "۲. در ربات بله دکمه **Start** (شروع) را بزنید.\n"
                    "۳. به همینجا برگردید و روی دکمه **بروزرسانی** کلیک کنید.",
                    reply_markup=keyboard
                )
                except: pass
                return
            
            user_steps[user_id]["step"] = "ready_to_upload_bale"
            user_steps[user_id]["platform"] = "bale"
            user_steps[user_id]["bale_target"] = bale_account
            
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("📤 تایید و ارسال مستقیم به بله", callback_data="confirm_upload_bale")],
                [InlineKeyboardButton("❌ لغو", callback_data="cancel")]
            ])
            try: await callback_query.message.edit_text("✅ حساب بله شما متصل است.\nآیا فایل به صورت مستقیم (با قابلیت پارت‌بندی خودکار) برای بله شما ارسال شود؟", reply_markup=keyboard)
            except: pass

        elif platform == "beta":
            user_steps[user_id]["step"] = "waiting_for_link"
            user_steps[user_id]["platform"] = "beta"
            try: await callback_query.message.edit_text("🧪 **حالت بتا (پارت‌بندی اختصاصی سرور)**\n🔗 لطفاً آیدی عددی یا یوزرنیم ربات/کانال مقصد خود در بله را بفرستید:", reply_markup=get_cancel_keyboard())
            except: pass

        elif platform == "rubika":
            user_steps[user_id]["step"] = "waiting_for_link"
            user_steps[user_id]["platform"] = "rubika"
            try: await callback_query.message.edit_text(f"✅ پلتفرم **روبیکا 🔴** انتخاب شد.\n🔗 حالا آیدی، یوزرنیم یا لینک مقصد را بفرستید:", reply_markup=get_cancel_keyboard())
            except: pass

    elif data == "confirm_upload_bale":
        user_data = user_steps.get(user_id, {})
        if user_data.get("step") != "ready_to_upload_bale": return
        
        target_input = user_data["bale_target"]
        platform = "bale"
        file_msg_id = user_data["message_id"]
        file_size_mb = user_data["file_size_mb"]
        file_size_bytes = user_data["file_size_bytes"]
        
        status_msg = await callback_query.message.edit_text("⏳ ارتباط با سرور...")
        asyncio.create_task(process_file_upload(client, user_id, callback_query.message.chat.id, file_msg_id, platform, target_input, file_size_mb, file_size_bytes, status_msg))

    elif data == "set_help_text" and user_id in ADMINS:
        user_steps[user_id] = {"step": "waiting_for_help_text"}
        try: await callback_query.message.edit_text("📝 متن جدید راهنما را بفرستید:", reply_markup=get_cancel_keyboard())
        except: pass

    elif data == "ban_user" and user_id in ADMINS:
        user_steps[user_id] = {"step": "waiting_for_ban_id"}
        try: await callback_query.message.edit_text("🚫 آیدی عددی کاربر مسدودی:", reply_markup=get_cancel_keyboard())
        except: pass

    elif data == "unban_user" and user_id in ADMINS:
        user_steps[user_id] = {"step": "waiting_for_unban_id"}
        try: await callback_query.message.edit_text("✅ آیدی عددی برای رفع مسدودیت:", reply_markup=get_cancel_keyboard())
        except: pass

    elif data == "set_user_limit" and user_id in ADMINS:
        user_steps[user_id] = {"step": "waiting_for_user_id_to_limit"}
        try: await callback_query.message.edit_text("👤 آیدی عددی کاربر تلگرام:", reply_markup=get_cancel_keyboard())
        except: pass

    elif data == "add_channel" and user_id in ADMINS:
        user_steps[user_id] = {"step": "waiting_for_channel_add"}
        try: await callback_query.message.edit_text("📢 لطفاً لینک یا آیدی کانال را بفرستید:\n(مثال: `@MyChannel` یا `https://t.me/MyChannel`)", reply_markup=get_cancel_keyboard())
        except: pass

    elif data == "remove_channel" and user_id in ADMINS:
        user_steps[user_id] = {"step": "waiting_for_channel_remove"}
        try: await callback_query.message.edit_text("📢 لطفاً آیدی کانالی که می‌خواهید حذف شود را بفرستید (مثال `@MyChannel`):", reply_markup=get_cancel_keyboard())
        except: pass

    elif data.startswith("admin_users_page_") and user_id in ADMINS:
        page = int(data.split("_")[3])
        users_data = await get_all_users_usage()
        
        if not users_data:
            try: await callback_query.message.edit_text("📭 هیچ کاربری نیست.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="cancel")]]))
            except: pass
            return
        
        items_per_page = 10
        total_pages = max(1, (len(users_data) - 1) // items_per_page + 1)
        start_idx = page * items_per_page
        end_idx = start_idx + items_per_page
        page_users = users_data[start_idx:end_idx]

        text = f"📊 **وضعیت کاربران (صفحه {page + 1} از {total_pages}):**\n"
        for i, (uid, charged, used, remaining) in enumerate(page_users, start=start_idx + 1):
            status = "🚫 مسدود" if await is_banned(uid) else "✅ فعال"
            text += f"\n👤 کاربر #{i} | {status}\n🆔: `{uid}`\n💰 شارژ: {round(charged, 2)}MB | 📤 مصرف: {round(used, 2)}MB | 📦 مانده: {round(remaining, 2)}MB\n"
        
        nav_buttons = []
        if page > 0:
            nav_buttons.append(InlineKeyboardButton("⬅️ قبلی", callback_data=f"admin_users_page_{page-1}"))
        if page < total_pages - 1:
            nav_buttons.append(InlineKeyboardButton("بعدی ➡️", callback_data=f"admin_users_page_{page+1}"))
            
        keyboard = []
        if nav_buttons:
            keyboard.append(nav_buttons)
        keyboard.append([InlineKeyboardButton("🔄 بروزرسانی صفحه", callback_data=f"admin_users_page_{page}")])
        keyboard.append([InlineKeyboardButton("🔙 بازگشت", callback_data="cancel")])
        
        try: await callback_query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        except: pass

    elif data == "broadcast_msg" and user_id in ADMINS:
        user_steps[user_id] = {"step": "waiting_for_broadcast"}
        try: await callback_query.message.edit_text("📢 لطفاً پیام خود را (متن، عکس، ویدیو و...) ارسال کنید:", reply_markup=get_cancel_keyboard())
        except: pass

    elif data == "start_upload":
        if user_id not in ADMINS and await get_user_limit(user_id) <= 0:
            await callback_query.answer("⚠️ حجم شما تمام شده!", show_alert=True)
            return
        user_steps[user_id] = {"step": "waiting_for_file"}
        try: await callback_query.message.edit_text("📥 فایل را بفرستید:", reply_markup=get_cancel_keyboard())
        except: pass

@bot.on_message(filters.private & ~filters.command("start"))
async def handle_messages(client, message):
    user_id = message.from_user.id
    if await is_banned(user_id): return
    
    is_member, not_joined = await check_membership(client, user_id)
    if not is_member:
        await message.reply_text("⛔️ لطفاً ابتدا از طریق /start در کانال‌ها عضو شوید.")
        return
    
    user_data = user_steps.get(user_id, {"step": "idle"})
    step = user_data.get("step", "idle")

    if step == "waiting_for_channel_add" and user_id in ADMINS:
        try:
            link_input = message.text.strip()
            if "t.me/" in link_input:
                chat_id = "@" + link_input.split("t.me/")[-1].replace("/", "")
                link = link_input
            elif link_input.startswith("@"):
                chat_id = link_input
                link = "https://t.me/" + link_input[1:]
            else:
                chat_id = "@" + link_input
                link = "https://t.me/" + link_input

            await add_force_channel(chat_id, link)
            user_steps[user_id] = {"step": "idle"}
            await message.reply_text(f"✅ کانال `{chat_id}` با موفقیت اضافه شد.")
        except:
            await message.reply_text("❌ خطا در ثبت کانال.")

    elif step == "waiting_for_channel_remove" and user_id in ADMINS:
        target = message.text.strip()
        if not target.startswith("@") and not target.startswith("-"):
            target = "@" + target
        await remove_force_channel(target)
        user_steps[user_id] = {"step": "idle"}
        await message.reply_text(f"✅ کانال {target} حذف شد.")

    elif step == "waiting_for_help_text" and user_id in ADMINS:
        if not message.text: return
        await set_setting("help_text", message.text)
        user_steps[user_id] = {"step": "idle"}
        await message.reply_text("✅ ثبت شد.")

    elif step == "waiting_for_ban_id" and user_id in ADMINS:
        if not message.text or not message.text.isdigit(): return
        await set_ban_status(int(message.text), True)
        user_steps[user_id] = {"step": "idle"}
        await message.reply_text(f"🚫 مسدود شد.")

    elif step == "waiting_for_unban_id" and user_id in ADMINS:
        if not message.text or not message.text.isdigit(): return
        await set_ban_status(int(message.text), False)
        user_steps[user_id] = {"step": "idle"}
        await message.reply_text(f"✅ باز شد.")

    elif step == "waiting_for_user_id_to_limit" and user_id in ADMINS:
        if not message.text or not message.text.isdigit(): return
        user_steps[user_id] = {"step": "waiting_for_limit_amount", "target_user_id": int(message.text)}
        await message.reply_text("📊 مقدار حجم (MB):", reply_markup=get_cancel_keyboard())

    elif step == "waiting_for_limit_amount" and user_id in ADMINS:
        if not message.text or not message.text.replace('.', '', 1).isdigit(): return
        await set_user_limit(user_data["target_user_id"], float(message.text))
        await message.reply_text(f"✅ تغییر یافت.")
        user_steps[user_id] = {"step": "idle"}

    elif step == "waiting_for_broadcast" and user_id in ADMINS:
        user_steps[user_id] = {"step": "idle"}
        status_msg = await message.reply_text("⏳ در حال ارسال همگانی...")
        
        all_users = await get_all_user_ids()
        success_count = 0
        block_count = 0
        
        for uid in all_users:
            try:
                await message.copy(chat_id=uid)
                success_count += 1
            except Exception:
                block_count += 1
            await asyncio.sleep(random.uniform(1.0, 3.0))
            
        await status_msg.edit_text(f"✅ پایان ارسال!\nموفق: {success_count}\nناموفق: {block_count}")

    elif step == "waiting_for_file":
        if not message.media and not message.document: return
        file_size = getattr(message.document or message.video or message.audio or message.photo, "file_size", 0)
        file_size_mb = file_size / (1024 * 1024)
        
        if user_id not in ADMINS and file_size_mb > await get_user_limit(user_id):
            await message.reply_text(f"❌ حجم فایل ({round(file_size_mb, 2)} MB) بیشتر از موجودی است.", reply_markup=get_cancel_keyboard())
            return

        user_steps[user_id] = {"step": "waiting_for_platform", "message_id": message.id, "file_size_mb": file_size_mb, "file_size_bytes": file_size}
        
        kb_buttons = [
            [InlineKeyboardButton("روبیکا 🔴", callback_data="platform_rubika"), InlineKeyboardButton("بله 🟢", callback_data="platform_bale")]
        ]
        if user_id in ADMINS: kb_buttons.append([InlineKeyboardButton("بتا (پارت‌بندی ادمین) 🧪", callback_data="platform_beta")])
        kb_buttons.append([InlineKeyboardButton("❌ لغو", callback_data="cancel")])
        
        await message.reply_text(f"✅ فایل دریافت شد. لطفاً پلتفرم مقصد را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(kb_buttons))

    elif step == "waiting_for_link":
        if not message.text: return
        target_input = message.text.strip()
        platform = user_data.get("platform", "rubika")
        
        if user_id not in ADMINS and platform == "rubika":
            lower_input = target_input.lower()
            if any(x in lower_input for x in ["joing", "joinc", "join", "http", "rubika.ir/"]):
                await message.reply_text("❌ ارسال فقط به پیوی مجاز است.", reply_markup=get_cancel_keyboard())
                return

        file_msg_id = user_data["message_id"]
        file_size_mb = user_data["file_size_mb"]
        file_size_bytes = user_data["file_size_bytes"]
        
        status_msg = await message.reply_text("⏳ ارتباط با سرور...", reply_markup=get_upload_cancel_keyboard())
        asyncio.create_task(process_file_upload(client, user_id, message.chat.id, file_msg_id, platform, target_input, file_size_mb, file_size_bytes, status_msg))

# ================ تسک پس‌زمینه برای گوش دادن به درخواست‌های بله ================
def run_bale_polling():
    offset = 0
    while True:
        try:
            url = f"https://tapi.bale.ai/bot{BALE_BOT_TOKEN}/getUpdates"
            response = bale_session.get(url, params={"offset": offset, "timeout": 30}, timeout=40)
            if response.status_code == 200:
                data = response.json()
                if data.get("ok"):
                    for update in data.get("result", []):
                        offset = update["update_id"] + 1
                        if "message" in update and "text" in update["message"]:
                            msg = update["message"]
                            text = msg["text"]
                            bale_user_id = str(msg["from"]["id"])
                            
                            if text.startswith("/start "):
                                tg_user_id = text.split(" ")[1]
                                if tg_user_id.isdigit():
                                    with sqlite3.connect("bot_database.db", timeout=10) as local_conn:
                                        local_cursor = local_conn.cursor()
                                        local_cursor.execute("CREATE TABLE IF NOT EXISTS linked_accounts (tg_user_id INTEGER PRIMARY KEY, bale_user_id TEXT)")
                                        local_cursor.execute("REPLACE INTO linked_accounts (tg_user_id, bale_user_id) VALUES (?, ?)", (int(tg_user_id), bale_user_id))
                                        local_conn.commit()
                                        
                                    send_url = f"https://tapi.bale.ai/bot{BALE_BOT_TOKEN}/sendMessage"
                                    bale_session.post(send_url, json={
                                        "chat_id": bale_user_id,
                                        "text": "✅ حساب بله شما با موفقیت به ربات تلگرام متصل شد!\nحالا می‌توانید به تلگرام برگردید و روی دکمه «بروزرسانی» کلیک کنید."
                                    })
                            elif text == "/start":
                                send_url = f"https://tapi.bale.ai/bot{BALE_BOT_TOKEN}/sendMessage"
                                bale_session.post(send_url, json={
                                    "chat_id": bale_user_id,
                                    "text": f"👋 سلام!\nشناسه بله شما: `{bale_user_id}`\nبرای اتصال به تلگرام، لطفاً روی دکمه‌های داخل بات تلگرام کلیک کنید تا متصل شوید."
                                })
        except Exception:
            pass
        time.sleep(1)

threading.Thread(target=run_bale_polling, daemon=True).start()

if __name__ == "__main__":
    bot.run()