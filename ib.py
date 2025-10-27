import os
import requests
import cloudscraper
import threading
import time
import random
import re
from colorama import Fore, init
from datetime import datetime, timedelta 
from telebot import TeleBot, types
from flask import Flask, request
from collections import deque
import sqlite3 
from typing import Optional 
import json

# ==============================================================================
# 1. CẤU HÌNH BOT VÀ MÔI TRƯỜNG
# ==============================================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
SERVER_URL = os.environ.get("SERVER_URL", "YOUR_RENDER_EXTERNAL_URL") 
WEBHOOK_URL_PATH = f"/{BOT_TOKEN}"
WEBHOOK_PORT = int(os.environ.get("PORT", 5000))

bot = TeleBot(BOT_TOKEN, threaded=False)
app = Flask(__name__)

init(autoreset=True)

user_states_lock = threading.Lock()
USER_JOB_STATES = {}
GLOBAL_LOG_UPDATE_INTERVAL = 3
DB_FILE = 'user_tokens.db' 


# ==============================================================================
# PHẦN QUẢN LÝ PERSISTENT DATA BẰNG SQLITE3
# ==============================================================================

def init_db():
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_auth (
                chat_id INTEGER PRIMARY KEY,
                auth_token TEXT NOT NULL,
                ig_enabled INTEGER DEFAULT 1,
                th_enabled INTEGER DEFAULT 1
            )
        """)
        conn.commit()
        conn.close()
        print(f"✅ Database {DB_FILE} khởi tạo thành công.")
    except Exception as e:
        print(f"❌ Lỗi khởi tạo Database: {e}")

def get_auth_data(chat_id: int) -> Optional[dict]:
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT auth_token, ig_enabled, th_enabled FROM user_auth WHERE chat_id = ?", (chat_id,))
        row = cursor.fetchone()
        conn.close()
        if row: return {'auth_token': row[0],'platform_config': {'instagram': bool(row[1]), 'threads': bool(row[2])}}
    except Exception as e: print(f"❌ Lỗi đọc Database cho chat_id {chat_id}: {e}")
    return None

def save_auth_data(chat_id: int, auth_token: str, ig_enabled: bool, th_enabled: bool):
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO user_auth (chat_id, auth_token, ig_enabled, th_enabled) 
            VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                auth_token = excluded.auth_token,
                ig_enabled = excluded.ig_enabled,
                th_enabled = excluded.th_enabled
        """, (chat_id, auth_token, ig_enabled, th_enabled))
        conn.commit(); conn.close()
    except Exception as e: print(f"❌ Lỗi ghi Database cho chat_id {chat_id}: {e}")

def delete_auth_data(chat_id: int):
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM user_auth WHERE chat_id = ?", (chat_id,))
        conn.commit(); conn.close()
    except Exception as e: print(f"❌ Lỗi xóa Database cho chat_id {chat_id}: {e}")


# ==============================================================================
# 2. CLASS QUẢN LÝ TRẠNG THÁI VÀ LOG
# ==============================================================================

class UserJobState:
    def __init__(self, auth_token, chat_id, platform_config: dict):
        self.auth_token = auth_token; self.chat_id = chat_id
        self.is_running = False; self.threads = []
        self.platform_config = platform_config 
        self.total_money = 0; self.total_success = 0; self.total_failed = 0
        self.current_indexes = {'instagram': 0, 'threads': 0}
        self.status_update_event = threading.Event()
        self.status_updater_thread = None 
        self.last_status_message_id = None 
        self.activity_log = deque(maxlen=10) 
        self.money_lock = threading.Lock(); self.success_lock = threading.Lock(); self.failed_lock = threading.Lock(); self.account_lock = threading.Lock()
        self.last_no_job_log = {'instagram': time.time(), 'threads': time.time()}

    def signal_status_update(self): self.status_update_event.set()

    def send_log_message(self, message):
        timestamp = datetime.now().strftime("%H:%M:%S"); log_message = f"`[{timestamp}] {message}`"
        try: bot.send_message(self.chat_id, log_message, parse_mode='Markdown')
        except: pass
            
    def add_activity_log(self, message):
        vn_time = datetime.now() + timedelta(hours=7) 
        timestamp = vn_time.strftime("%H:%M:%S")
        self.activity_log.append(f"*{timestamp}*: {message}")

    def get_next_account(self, accounts, platform):
        with self.account_lock:
            if not accounts: return None
            if self.current_indexes[platform] >= len(accounts): self.current_indexes[platform] = 0
            account = accounts[self.current_indexes[platform]]
            self.current_indexes[platform] = (self.current_indexes[platform] + 1) % len(accounts)
            return account

    def generate_status_text(self):
        ig_config = '✅' if self.platform_config['instagram'] else '❌'; th_config = '✅' if self.platform_config['threads'] else '❌'
        status = "*🤖 GOLIKE ROTATOR STATUS *\n"
        if self.is_running:
            status += f"🟢 *Trạng thái:* ĐANG CHẠY\n"; ig_count = len([t for t in self.threads if 'INSTA_WORKER' in t.name]); th_count = len([t for t in self.threads if 'THREAD_WORKER' in t.name])
            status += f"Cấu hình: {ig_config} IG, {th_config} Threads\n"; status += f"Worker: `{len(self.threads)}` luồng (IG:{ig_count}, TH:{th_count})\n\n"
        else:
            status += f"🟡 *Trạng thái:* ĐÃ DỪNG\n"; status += f"Cấu hình: {ig_config} IG, {th_config} Threads\n"; status += f"Worker: `0` luồng\n\n"

        with self.money_lock: status += f"💰 *TỔNG THU NHẬP:* `{self.total_money}` xu\n"
        with self.success_lock: status += f"✅ Thành công: `{self.total_success}`\n"
        with self.failed_lock: status += f"❌ Thất bại: `{self.total_failed}`\n"
            
        status += "\n\n*🔔 LOG HOẠT ĐỘNG GẦN NHẤT (VN Time):*\n"
        status += '\n'.join(list(self.activity_log)[-5:]) if self.activity_log else "Chưa có hoạt động nào..."

        status += f"\n\n/stopjob để dừng, /config để cấu hình."
        status += f"\n*Tự động cập nhật mỗi {GLOBAL_LOG_UPDATE_INTERVAL}s (sau khi có Job thành công: Ngay lập tức).*."
        return status
        
    def update_status_message(self):
        if not self.last_status_message_id: return

        text = self.generate_status_text()
        
        keyboard = types.InlineKeyboardMarkup()
        if self.is_running: 
            keyboard.row(types.InlineKeyboardButton("⏹️ DỪNG JOB", callback_data="/stopjob"),
                         types.InlineKeyboardButton("🔄 REFRESH (LẤY DỮ LIỆU)", callback_data="/status"))
        else: 
             keyboard.row(types.InlineKeyboardButton("▶️ START JOB", callback_data="/startjob"))
        keyboard.row(types.InlineKeyboardButton("⚙️ CẤU HÌNH", callback_data="/config"), types.InlineKeyboardButton("🏠 MENU CHÍNH", callback_data="/start"))


        try:
            bot.edit_message_text(chat_id=self.chat_id, message_id=self.last_status_message_id, text=text, reply_markup=keyboard, parse_mode='Markdown')
        except Exception as e:
            if "message to edit not found" in str(e).lower(): self.last_status_message_id = None
            elif "message is not modified" not in str(e): pass
            pass

    def start_workers(self, instagram_accounts, threads_accounts):
        if not self.status_updater_thread or not self.status_updater_thread.is_alive():
             self.status_updater_thread = threading.Thread(target=status_updater_thread_func, args=(self,), daemon=True, name="STATUS_UPDATER")
             self.status_updater_thread.start()

        self.is_running = True; num_started = 0; self.threads = [] 
        
        if self.platform_config['instagram'] and instagram_accounts:
            t_ig = threading.Thread(target=worker_instagram_telebot, args=(self, instagram_accounts, 1), daemon=True, name="INSTA_WORKER_1")
            self.threads.append(t_ig); t_ig.start(); self.add_activity_log(f"Đã khởi chạy IG Worker ({len(instagram_accounts)} UID)"); num_started += 1
        if self.platform_config['threads'] and threads_accounts:
            t_th = threading.Thread(target=worker_threads_telebot, args=(self, threads_accounts, 1), daemon=True, name="THREAD_WORKER_1")
            self.threads.append(t_th); t_th.start(); self.add_activity_log(f"Đã khởi chạy Threads Worker ({len(threads_accounts)} UID)"); num_started += 1
        
        if not self.threads: self.is_running = False; self.add_activity_log("❌ Không có Worker nào được khởi chạy.")
        return num_started

    def stop_workers(self):
        self.is_running = False; self.current_indexes = {'instagram': 0, 'threads': 0}
        if self.threads: num_stopped = len(self.threads); self.threads = []; return num_stopped
        return 0


# ==============================================================================
# 3. WORKER VÀ CÁC HÀM GOLIKE (ĐÃ FIX LỖI THỤT LỀ)
# ==============================================================================

def get_headers(auth_token): return {'accept-language': 'vi,fr-FR;q=0.9,fr;q=0.8,en-US;q=0.7,en;q=0.6','authorization': auth_token,'content-type': 'application/json;charset=utf-8','origin': 'https://app.golike.net','priority': 'u=1, i','sec-ch-ua': '"Google Chrome";v="135", "Not-A.Brand";v="8", "Chromium";v="135"','sec-ch-ua-mobile': '?1','sec-ch-ua-platform': '"Android"','sec-fetch-dest': 'empty','sec-fetch-mode': 'cors','sec-fetch-site': 'same-site','t': 'VFZSak1FNTZWVFJOUkdkNFRrRTlQUT09',}
    
def get_accounts_from_api(auth_token, platform="instagram"): 
    headers = get_headers(auth_token); scraper = cloudscraper.create_scraper()
    try:
        url = "https://gateway.golike.net/api/instagram-account" if platform == "instagram" else "https://gateway.golike.net/api/threads-account"
        response = scraper.get(url, headers=headers, timeout=10)
    except Exception: return [], f"Lỗi khi lấy UID từ API {platform}: (Network Error)"
    if response.status_code == 200:
        data = response.json(); accounts = []
        if data.get('success') and 'data' in data:
            for acc in data['data']:
                if acc.get('status') == 1 and acc.get('is_banned') == 0:
                    name = acc.get(f'{platform}_username') or acc.get('username') or f"ID:{acc['id']}"
                    accounts.append({'id': acc['id'], 'platform': platform, 'name': name})
            return accounts, ""
        else: return [], f"Lỗi Golike API: {data.get('message', 'Không thể xác định danh sách tài khoản.')}"
    else: return [], f"Lỗi HTTP {response.status_code} khi lấy UID: {response.text}"
    
def nhan_xu_instagram(scraper, headers, uid_cauhinh, uid_job, price_per): 
    json_data = { 'instagram_users_advertising_id': uid_job, 'instagram_account_id': uid_cauhinh, 'async': True, 'data': None }
    try: response = scraper.post('https://gateway.golike.net/api/advertising/publishers/instagram/complete-jobs', headers=headers, json=json_data, timeout=5)
    except Exception: return False, 0
    if "thành công" in response.json().get('message', '').lower(): return True, price_per
    return False, 0

def nhan_xu_threads(scraper, headers, account_id, ads_id):
    json_data = { 'account_id': account_id, 'ads_id': ads_id }
    try: response = scraper.post('https://gateway.golike.net/api/advertising/publishers/threads/complete-jobs', headers=headers, json=json_data, timeout=5)
    except Exception: return False, 0
    data = response.json()
    if "thành công" in data.get('message', '').lower(): return True, data.get('data', {}).get('prices', 0)
    return False, 0
    
def nhan_job_instagram(scraper, headers, uid_cauhinh): 
    params = { 'instagram_account_id': f'{uid_cauhinh}', 'data': 'null' }
    try: response = scraper.get('https://gateway.golike.net/api/advertising/publishers/instagram/jobs', params=params, headers=headers, timeout=3)
    except Exception: return None
    data = response.json()
    if data.get('success') and 'data' in data and data['data'].get('status') == 0: 
        job_data = data['data']; return { 'id': job_data.get('id'), 'price_per': job_data.get('price_after_cost', job_data.get('price_per', 0)) }
    return None

def nhan_job_threads(scraper, headers, account_id): 
    params = { 'account_id': f'{account_id}' }
    try: response = scraper.get('https://gateway.golike.net/api/advertising/publishers/threads/jobs', params=params, headers=headers, timeout=3)
    except Exception: return None
    data = response.json()
    if data.get('success') and 'data' in data and 'lock' in data and data['lock'] is not None: 
        job_data = data['data']; return { 'id': job_data.get('id'), 'price_per': job_data.get('price_after_cost', job_data.get('price_per', 0)) }
    return None


def status_updater_thread_func(job_state: UserJobState):
    while job_state.is_running:
        job_state.status_update_event.wait(GLOBAL_LOG_UPDATE_INTERVAL)
        if not job_state.is_running: break
        job_state.update_status_message()
        job_state.status_update_event.clear()

def worker_instagram_telebot(job_state: UserJobState, accounts, worker_id):
    scraper = cloudscraper.create_scraper(browser={'browser': 'chrome','platform': 'android','mobile': True})
    headers = get_headers(job_state.auth_token); platform = 'instagram'
    while job_state.is_running:
        account = job_state.get_next_account(accounts, platform)
        if not account:
            if time.time() - job_state.last_no_job_log[platform] > 60: job_state.add_activity_log("⚠️ Instagram: Hết UID/cấu hình bị lỗi, tạm chờ 10s..."); job_state.last_no_job_log[platform] = time.time()
            time.sleep(10); continue
        account_id = account['id']; account_name = account['name']; job = nhan_job_instagram(scraper, headers, account_id)
        if job:
            success, money_earned = nhan_xu_instagram(scraper, headers, account_id, job['id'], job['price_per'])
            if success:
                with job_state.money_lock: job_state.total_money += money_earned
                with job_state.success_lock: job_state.total_success += 1
                job_state.add_activity_log(f"✅ INSTA `{account_name}` | +{money_earned} xu")
            else:
                with job_state.failed_lock: job_state.total_failed += 1
                job_state.add_activity_log(f"❌ INSTA `{account_name}` thất bại.")
            
            # PHỤC HỒI/GỬI TÍN HIỆU
            if job_state.status_updater_thread and not job_state.status_updater_thread.is_alive():
                job_state.status_updater_thread = threading.Thread(target=status_updater_thread_func, args=(job_state,), daemon=True, name="STATUS_UPDATER"); job_state.status_updater_thread.start()
            job_state.signal_status_update()
            time.sleep(random.uniform(8, 15))
        else: time.sleep(1)


def worker_threads_telebot(job_state: UserJobState, accounts, worker_id):
    scraper = cloudscraper.create_scraper(browser={'browser': 'chrome','platform': 'android','mobile': True})
    headers = get_headers(job_state.auth_token); platform = 'threads'
    while job_state.is_running:
        account = job_state.get_next_account(accounts, platform)
        if not account:
            if time.time() - job_state.last_no_job_log[platform] > 60: job_state.add_activity_log("⚠️ Threads: Hết UID/cấu hình bị lỗi, tạm chờ 10s..."); job_state.last_no_job_log[platform] = time.time()
            time.sleep(10); continue
        account_id = account['id']; account_name = account['name']; job = nhan_job_threads(scraper, headers, account_id)
        if job:
            success, money_earned = nhan_xu_threads(scraper, headers, account_id, job['id'])
            if success:
                with job_state.money_lock: job_state.total_money += money_earned
                with job_state.success_lock: job_state.total_success += 1
                job_state.add_activity_log(f"✅ THREADS `{account_name}` | +{money_earned} xu")
            else:
                with job_state.failed_lock: job_state.total_failed += 1
                job_state.add_activity_log(f"❌ THREADS `{account_name}` thất bại.")
            
            # PHỤC HỒI/GỬI TÍN HIỆU
            if job_state.status_updater_thread and not job_state.status_updater_thread.is_alive():
                job_state.status_updater_thread = threading.Thread(target=status_updater_thread_func, args=(job_state,), daemon=True, name="STATUS_UPDATER"); job_state.status_updater_thread.start()

            job_state.signal_status_update() 
            time.sleep(random.uniform(8, 15))
        else: time.sleep(1)

# ==============================================================================
# 4. CHỨC NĂNG LỆNH CỦA TELEBOT (Menu đã chỉnh sửa)
# ==============================================================================

def get_menu_keyboard():
    keyboard = types.InlineKeyboardMarkup()
    keyboard.row(types.InlineKeyboardButton("▶️ START JOB", callback_data="/startjob"), types.InlineKeyboardButton("⏹️ STOP JOB", callback_data="/stopjob"))
    keyboard.row(types.InlineKeyboardButton("📊 STATUS", callback_data="/status"), types.InlineKeyboardButton("⚙️ CẤU HÌNH", callback_data="/config"))
    keyboard.row(types.InlineKeyboardButton("🔑 THÊM AUTHEN", callback_data="/auth_hint"), types.InlineKeyboardButton("🗑️ XOÁ AUTHEN", callback_data="/xoaauthen"))
    return keyboard

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    text = ("🤖 *Chào mừng đến với Golike Rotator Bot!*\n\n"
        "Sử dụng các lệnh/nút sau để quản lý:\n"
        "`/auth <token>`: Thêm Auth Token Golike.\n"
        "`/config`: Chọn nền tảng chạy (IG, Threads, Cả 2).\n"
        "`/startjob`: Bắt đầu auto đa luồng.\n"
        "`/status`: Hiện/cập nhật tin nhắn thống kê chính (Log TỰ ĐỘNG thay đổi).\n\n"
        "⚠️ *LƯU Ý:* Token và Config đã được lưu lại để chống mất dữ liệu khi Service ngủ/Restart.")
    bot.send_message(message.chat.id, text, reply_markup=get_menu_keyboard(), parse_mode='Markdown')

def get_config_keyboard(config: dict):
    keyboard = types.InlineKeyboardMarkup()
    ig_emoji = "✅ IG" if config['instagram'] else " IG"; th_emoji = "✅ Threads" if config['threads'] else " Threads"
    keyboard.row(types.InlineKeyboardButton(ig_emoji, callback_data="config_toggle_instagram"), types.InlineKeyboardButton(th_emoji, callback_data="config_toggle_threads"))
    keyboard.row(types.InlineKeyboardButton("CẢ HAI", callback_data="config_set_both"), types.InlineKeyboardButton("❌ KHÔNG CHẠY", callback_data="config_set_none"))
    keyboard.row(types.InlineKeyboardButton("↩️ MENU CHÍNH", callback_data="/start"))
    return keyboard

@bot.message_handler(commands=['config'])
def handle_config(message):
    chat_id = message.chat.id
    with user_states_lock: job_state = USER_JOB_STATES.get(chat_id)
    if not job_state: 
        db_data = get_auth_data(chat_id)
        if not db_data: bot.send_message(chat_id, "⚠️ **Chưa có Auth Token.** Vui lòng dùng lệnh `/auth` trước.", parse_mode='Markdown'); return
        job_state = UserJobState(db_data['auth_token'], chat_id, db_data['platform_config'])
        USER_JOB_STATES[chat_id] = job_state
        job_state.add_activity_log("Dữ liệu cấu hình được khôi phục từ Database.")
        
    if job_state.is_running: bot.send_message(chat_id, "⚠️ **Phải dùng /stopjob** để dừng Job trước khi thay đổi cấu hình.", parse_mode='Markdown'); return

    current_config = job_state.platform_config
    text = "⚙️ *CHỌN NỀN TẢNG MUỐN CHẠY TRONG PHIÊN TIẾP THEO:*\n\n"; text += f"- Instagram: {'✅ Đang bật' if current_config['instagram'] else '❌ Đang tắt'}\n"
    text += f"- Threads: {'✅ Đang bật' if current_config['threads'] else '❌ Đang tắt'}\n"; text += "\nNhấn vào các nút bên dưới để chuyển đổi."

    bot.send_message(chat_id, text, reply_markup=get_config_keyboard(current_config), parse_mode='Markdown')


@bot.callback_query_handler(func=lambda call: call.data.startswith('config_'))
def handle_config_callback(call):
    chat_id = call.message.chat.id; config_action = call.data
    
    with user_states_lock:
        job_state = USER_JOB_STATES.get(chat_id)
        if not job_state or job_state.is_running: bot.answer_callback_query(call.id, "❌ Không thể thay đổi khi Job đang chạy hoặc chưa có Token.", show_alert=True); return
             
        current_config = job_state.platform_config
        if config_action == 'config_toggle_instagram': current_config['instagram'] = not current_config['instagram']; bot.answer_callback_query(call.id, f"IG đã chuyển sang {'BẬT' if current_config['instagram'] else 'TẮT'}")
        elif config_action == 'config_toggle_threads': current_config['threads'] = not current_config['threads']; bot.answer_callback_query(call.id, f"Threads đã chuyển sang {'BẬT' if current_config['threads'] else 'TẮT'}")
        elif config_action == 'config_set_both': current_config.update({'instagram': True, 'threads': True}); bot.answer_callback_query(call.id, "✅ Đã chọn CẢ HAI.")
        elif config_action == 'config_set_none': current_config.update({'instagram': False, 'threads': False}); bot.answer_callback_query(call.id, "❌ Đã chọn KHÔNG CHẠY CÁI NÀO.")
        
        save_auth_data(chat_id, job_state.auth_token, current_config['instagram'], current_config['threads'])
        
        new_text = "⚙️ *CHỌN NỀN TẢNG MUỐN CHẠY TRONG PHIÊN TIẾP THEO:*\n\n"
        new_text += f"- Instagram: {'✅ Đang bật' if current_config['instagram'] else '❌ Đang tắt'}\n"; new_text += f"- Threads: {'✅ Đang bật' if current_config['threads'] else '❌ Đang tắt'}\n"; new_text += "\nNhấn vào các nút bên dưới để chuyển đổi."
        
        try: bot.edit_message_text(chat_id=chat_id, message_id=call.message.message_id, text=new_text, reply_markup=get_config_keyboard(current_config), parse_mode='Markdown')
        except Exception as e:
            if "message is not modified" not in str(e): job_state.send_log_message(f"Lỗi cập nhật cấu hình: {e}")
        
    
@bot.callback_query_handler(func=lambda call: call.data in ['/startjob', '/stopjob', '/status', '/xoaauthen', '/auth_hint', '/config', '/start'])
def handle_callback_query(call):
    message = call.message
    bot.answer_callback_query(call.id) 
    if call.data == '/auth_hint': bot.send_message(message.chat.id, "Để thêm Auth Token, bạn gửi lệnh theo cú pháp sau:\n\n`/auth Bearer eyJ0eXAiOi...`\n\n*Bạn phải có khoảng trắng giữa /auth và Bearer.*", parse_mode='Markdown')
    elif call.data == '/startjob': handle_startjob(message)
    elif call.data == '/stopjob': handle_stopjob(message)
    elif call.data == '/status': handle_status(message)
    elif call.data == '/xoaauthen': handle_xoaauthen(message)
    elif call.data == '/config': handle_config(message) 
    elif call.data == '/start': send_welcome(message) 

@bot.message_handler(commands=['auth'])
def handle_auth(message):
    chat_id = message.chat.id; token_match = re.match(r'/auth\s+(Bearer\s+\S+)', message.text, re.DOTALL)
    
    if token_match:
        auth_token = token_match.group(1).strip()
        job_state = USER_JOB_STATES.get(chat_id)
        if job_state: job_state.send_log_message("🔍 Đang kiểm tra Auth Token và lấy danh sách tài khoản...")
        else: bot.send_message(chat_id, "`🔍 Đang kiểm tra Auth Token và lấy danh sách tài khoản...`", parse_mode='Markdown') # CŨNG LÀ TIN NHẮN ĐỘC LẬP.
        
        instagram_accounts, err_ig = get_accounts_from_api(auth_token, "instagram"); threads_accounts, err_th = get_accounts_from_api(auth_token, "threads")

        if err_ig.startswith('Lỗi HTTP 401') or err_th.startswith('Lỗi HTTP 401') : bot.send_message(chat_id, "❌ Auth Token bị từ chối (401 Unauthorized). *Token không hợp lệ hoặc đã hết hạn.*", parse_mode='Markdown'); return

        with user_states_lock:
            db_data = get_auth_data(chat_id); old_config = db_data['platform_config'] if db_data else {'instagram': True, 'threads': True}
            
            if chat_id in USER_JOB_STATES and USER_JOB_STATES[chat_id].is_running: USER_JOB_STATES[chat_id].stop_workers(); USER_JOB_STATES[chat_id].send_log_message("⚠️ Công việc cũ đã được dừng.")
                
            USER_JOB_STATES[chat_id] = UserJobState(auth_token, chat_id, old_config)
            job_state = USER_JOB_STATES[chat_id]
            job_state.add_activity_log(f"Token mới được thiết lập. IG:{len(instagram_accounts)}, TH:{len(threads_accounts)}")
            
            save_auth_data(chat_id, auth_token, old_config['instagram'], old_config['threads'])

        acc_info = f"✅ Lưu Auth Token thành công!\n\n"; acc_info += f"📸 Tìm thấy {len(instagram_accounts)} UID Instagram hoạt động.\n"; acc_info += f"🧵 Tìm thấy {len(threads_accounts)} UID Threads hoạt động."
            
        bot.send_message(chat_id, acc_info, reply_markup=get_menu_keyboard(), parse_mode='Markdown')
    else: bot.send_message(chat_id, "❌ Cú pháp lệnh sai. Vui lòng gửi theo mẫu:\n\n`/auth Bearer <Auth_Token>`", parse_mode='Markdown')

@bot.message_handler(commands=['xoaauthen'])
def handle_xoaauthen(message):
    chat_id = message.chat.id
    with user_states_lock: job_state = USER_JOB_STATES.get(chat_id)
    if not job_state and not get_auth_data(chat_id): bot.send_message(chat_id, "🤷 Auth Token chưa được thiết lập."); return

    if chat_id in USER_JOB_STATES: 
        if USER_JOB_STATES[chat_id].is_running: USER_JOB_STATES[chat_id].stop_workers(); USER_JOB_STATES[chat_id].send_log_message("⚠️ Job đang chạy đã được dừng trước khi xoá.")
        if USER_JOB_STATES[chat_id].last_status_message_id:
            try: bot.delete_message(chat_id, USER_JOB_STATES[chat_id].last_status_message_id)
            except: pass
            
    delete_auth_data(chat_id)
    if chat_id in USER_JOB_STATES: del USER_JOB_STATES[chat_id]

    bot.send_message(chat_id, "🗑️ Đã xoá Auth Token và dữ liệu phiên thành công. Bạn có thể thêm token mới bằng lệnh /auth.", reply_markup=get_menu_keyboard())

@bot.message_handler(commands=['startjob'])
def handle_startjob(message):
    chat_id = message.chat.id
    with user_states_lock: 
        job_state = USER_JOB_STATES.get(chat_id)
        if not job_state:
             db_data = get_auth_data(chat_id)
             if db_data: 
                 job_state = UserJobState(db_data['auth_token'], chat_id, db_data['platform_config'])
                 USER_JOB_STATES[chat_id] = job_state
             else:
                 bot.send_message(chat_id, "⚠️ **Auth Token đã bị mất (không tìm thấy trong Database/RAM).** Vui lòng dùng lệnh `/auth` để thiết lập lại.", parse_mode='Markdown'); return

    if job_state.is_running: bot.send_message(chat_id, "⚠️ Job đã và đang chạy rồi."); return
    if not any(job_state.platform_config.values()): bot.send_message(chat_id, "❌ Không có nền tảng nào được cấu hình chạy. Vui lòng dùng lệnh `/config` để bật Instagram, Threads, hoặc cả hai.", parse_mode='Markdown'); return

    job_state.send_log_message("🔄 Đang lấy danh sách UID hoạt động để chuẩn bị chạy job...")
    instagram_accounts, err_ig = get_accounts_from_api(job_state.auth_token, "instagram"); threads_accounts, err_th = get_accounts_from_api(job_state.auth_token, "threads")

    filtered_ig = instagram_accounts if job_state.platform_config['instagram'] else []; filtered_th = threads_accounts if job_state.platform_config['threads'] else []
    if not filtered_ig and not filtered_th: bot.send_message(chat_id, "❌ Không có tài khoản hoạt động nào để chạy với cấu hình hiện tại (kiểm tra trạng thái tài khoản trên Golike)."); return

    # Gửi tin nhắn Status BAN ĐẦU (để lấy ID)
    try:
         if job_state.last_status_message_id: 
            try: bot.delete_message(chat_id, job_state.last_status_message_id)
            except: pass 
         initial_message = bot.send_message(chat_id, job_state.generate_status_text(), parse_mode='Markdown') 
         job_state.last_status_message_id = initial_message.message_id
    except Exception as e: job_state.send_log_message(f"❌ Lỗi gửi tin nhắn Status ban đầu: {e}"); return

    num_workers = job_state.start_workers(filtered_ig, filtered_th)

    if num_workers > 0: job_state.add_activity_log(f"Đã khởi động Job Đa Luồng thành công với {num_workers} Worker."); job_state.update_status_message()
    else: job_state.send_log_message("❌ Không thể khởi động Worker nào. Có lỗi xảy ra.")

@bot.message_handler(commands=['stopjob'])
def handle_stopjob(message):
    chat_id = message.chat.id
    with user_states_lock: 
        job_state = USER_JOB_STATES.get(chat_id)
        if not job_state: bot.send_message(chat_id, "⚠️ **Job không được tìm thấy trong bộ nhớ RAM.** Đã bị dừng hoặc chưa chạy.", parse_mode='Markdown'); return

    if not job_state.is_running: bot.send_message(chat_id, "⚠️ Không có Job nào đang chạy để dừng."); return
        
    num_stopped = job_state.stop_workers()
    final_money = 0
    with job_state.money_lock: final_money = job_state.total_money

    job_state.add_activity_log(f"⏹️ Job đã dừng thành công {num_stopped} Worker. Tổng tiền: {final_money}")
    
    if job_state.last_status_message_id: job_state.update_status_message()
    
    bot.send_message(chat_id, f"✅ *Đã dừng thành công {num_stopped} Worker. Tổng thu nhập phiên này: {final_money} xu.*", parse_mode='Markdown', reply_markup=get_menu_keyboard())


@bot.message_handler(commands=['status'])
def handle_status(message):
    chat_id = message.chat.id
    with user_states_lock: job_state = USER_JOB_STATES.get(chat_id)
    
    if not job_state:
        db_data = get_auth_data(chat_id)
        if db_data: 
             job_state = UserJobState(db_data['auth_token'], chat_id, db_data['platform_config'])
             USER_JOB_STATES[chat_id] = job_state
             job_state.add_activity_log("Dữ liệu Status được khôi phục từ Database.")
        else:
            bot.send_message(chat_id, "❌ **Auth Token chưa được thiết lập** (hoặc đã bị mất hoàn toàn). Vui lòng dùng /auth.", parse_mode='Markdown', reply_markup=get_menu_keyboard()); return

    if job_state.is_running:
         if job_state.last_status_message_id:
             try: job_state.update_status_message(); return 
             except Exception as e:
                 if "message to edit not found" in str(e).lower(): job_state.last_status_message_id = None
                 else: job_state.send_log_message(f"❌ Lỗi cập nhật Status: {e}")
         
         if not job_state.last_status_message_id:
            try:
               initial_message = bot.send_message(chat_id, job_state.generate_status_text(), parse_mode='Markdown')
               job_state.last_status_message_id = initial_message.message_id
               return
            except Exception as e: job_state.send_log_message(f"❌ Lỗi hiển thị Status Log mới: {e}")

    else:
        status_text = f"🟡 *Trạng thái:* ĐÃ DỪNG\n"
        status_text += f"💰 Thu nhập phiên cuối: `{job_state.total_money}` xu\n"
        status_text += f"✅ Thành công: `{job_state.total_success}`\n"
        status_text += f"❌ Thất bại: `{job_state.total_failed}`\n"
        status_text += "\nNhấn /startjob để chạy lại."
        bot.send_message(chat_id, status_text, parse_mode='Markdown', reply_markup=get_menu_keyboard())


# ==============================================================================
# 5. KHỞI TẠO WEBHOOK VÀ CHẠY ỨNG DỤNG FLASK (Render)
# ==============================================================================

@app.route(WEBHOOK_URL_PATH, methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = types.Update.de_json(json_string) 
        bot.process_new_updates([update])
        return '', 200
    else: return '', 403

def setup_webhook():
    if not SERVER_URL or not SERVER_URL.startswith("https://"):
         print("❌ SERVER_URL CHƯA ĐƯỢC THIẾT LẬP HOẶC KHÔNG HỢP LỆ/KHÔNG HTTPS. KHÔNG THỂ THIẾT LẬP WEBHOOK."); return
    webhook_url = SERVER_URL + WEBHOOK_URL_PATH
    for attempt in range(3):
        try:
            bot.remove_webhook(); time.sleep(1) 
            if bot.set_webhook(url=webhook_url): print(f"✅ Webhook đã được thiết lập thành công tới: {webhook_url}"); return
            else: print(f"Lần {attempt+1}: set_webhook trả về False.")
        except Exception as e: print(f"Lần {attempt+1} - Lỗi khi thiết lập Webhook: {e}")
        time.sleep(2 ** attempt) 
    print("❌ THIẾT LẬP WEBHOOK THẤT BẠI HOÀN TOÀN.")
            
@app.route('/')
def home(): return "Golike Rotator Telebot đang hoạt động! Tương tác qua Telegram.", 200

if __name__ == '__main__':
    # Chú ý: Đổi tên file này thành bot.py nếu Start command của Render là python bot.py
    init_db()
    setup_webhook()
    print(f"Bot khởi động trên cổng: {WEBHOOK_PORT}")
    app.run(host="0.0.0.0", port=WEBHOOK_PORT)