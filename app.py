#!/usr/bin/env python3
# coding: utf-8
"""
Main application server for ReviewCash (Flask + Telebot + SocketIO).
Configure via environment variables:
- BOT_TOKEN (your bot token)
- WEBAPP_URL (public URL, no trailing slash)
- CHANNEL_ID
- ADMIN_USER_IDS (comma-separated)
- ADMIN_USERNAMES (comma-separated)
- ADMIN_JWT_SECRET
- REDIS_URL (optional)
- BOT_FORCE_POLLING / BOT_USE_POLLING / BOT_AUTO_POLLING (to enable polling fallback)
"""
import os
import time
import json
import threading
import random
import socket as _socket
import hashlib
import hmac
from datetime import datetime, timedelta
from urllib.parse import quote_plus, parse_qsl

# --- Dependencies check ---
try:
    import jwt
except ImportError:
    raise RuntimeError("Python package 'PyJWT' is not installed. Install: pip install PyJWT")

try:
    import telebot
except ImportError:
    # allow server to run without bot in local testing
    telebot = None

from flask import Flask, request, send_from_directory, jsonify, abort
import eventlet
eventlet.monkey_patch()
from flask_socketio import SocketIO, join_room, leave_room

# ========== CONFIG ==========
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8033069276:AAFv1-kdQ68LjvLEgLHj3ZXd5ehMqyUXOYU")  # set your token in env
WEBAPP_URL = os.environ.get("WEBAPP_URL", "https://web-production-398fb.up.railway.app")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "@ReviewCashNews")

# Ensure your id is in main admins by default
ADMIN_USER_IDS = [s.strip() for s in os.environ.get("ADMIN_USER_IDS", "6482440657").split(",") if s.strip()]
ADMIN_USERNAMES = [s.strip() for s in os.environ.get("ADMIN_USERNAMES", "Sa1Raz").split(",") if s.strip()]

ADMIN_JWT_SECRET = os.environ.get("ADMIN_JWT_SECRET", "replace_with_strong_secret")
ADMIN_TOKEN_TTL_SECONDS = int(os.environ.get("ADMIN_TOKEN_TTL_SECONDS", 300))

DATA_DIR = os.environ.get("DATA_DIR", ".rc_data")
TOPUPS_FILE = os.path.join(DATA_DIR, "topups.json")
WITHDRAWS_FILE = os.path.join(DATA_DIR, "withdraws.json")
TASKS_FILE = os.path.join(DATA_DIR, "tasks.json")
WORKS_FILE = os.path.join(DATA_DIR, "works.json")
USERS_FILE = os.path.join(DATA_DIR, "users.json")
ADMINS_FILE = os.path.join(DATA_DIR, "admins.json")

os.makedirs(DATA_DIR, exist_ok=True)

# ========== HELPERS FOR STORAGE ==========
def load_json_safe(path, default):
    try:
        if not os.path.exists(path):
            return default
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def save_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)

def append_json(path, obj):
    arr = load_json_safe(path, [])
    arr.append(obj)
    save_json(path, arr)

# ========== APP, BOT, SOCKETIO ==========
app = Flask(__name__, static_folder='public')

# bot (telebot) may be None for local testing if BOT_TOKEN not provided
if BOT_TOKEN and telebot:
    bot = telebot.TeleBot(BOT_TOKEN)
else:
    bot = None
    if not BOT_TOKEN:
        print("Warning: BOT_TOKEN not set. Bot features (Telegram messages/webhook) are disabled until you set BOT_TOKEN.")

REDIS_URL = os.environ.get("REDIS_URL")
if REDIS_URL:
    socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet", message_queue=REDIS_URL)
else:
    socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

# persisted data
users = load_json_safe(USERS_FILE, {})  # keyed by uid string
ordinary_admins = load_json_safe(ADMINS_FILE, [])  # list of identifiers (id strings or usernames)

# ========== UTILITIES ==========
def quote(s):
    return quote_plus(s)

def save_users():
    save_json(USERS_FILE, users)

def get_user_record(uid):
    key = str(uid)
    if key not in users:
        users[key] = {"balance": 0, "tasks_done": 0, "total_earned": 0, "subscribed": False, "last_submissions": {}}
        save_users()
    return users[key]

def save_ordinary_admins():
    save_json(ADMINS_FILE, ordinary_admins)

def is_main_admin(uid_or_username):
    s = str(uid_or_username)
    return s in ADMIN_USER_IDS or s in ADMIN_USERNAMES

def is_ordinary_admin(uid_or_username):
    s = str(uid_or_username)
    return s in ordinary_admins

def add_ordinary_admin(identifier):
    s = str(identifier)
    if s in ordinary_admins:
        return False
    ordinary_admins.append(s)
    save_ordinary_admins()
    return True

def remove_ordinary_admin(identifier):
    s = str(identifier)
    if s in ordinary_admins:
        ordinary_admins.remove(s)
        save_ordinary_admins()
        return True
    return False

# ========== JWT admin tokens ==========
def generate_admin_token(uid, username):
    payload = {
        "uid": str(uid) if uid is not None else "",
        "username": username or "",
        "exp": datetime.utcnow() + timedelta(seconds=ADMIN_TOKEN_TTL_SECONDS),
        "iat": datetime.utcnow()
    }
    token = jwt.encode(payload, ADMIN_JWT_SECRET, algorithm="HS256")
    if isinstance(token, bytes):
        token = token.decode('utf-8')
    return token

def verify_admin_token(token):
    try:
        payload = jwt.decode(token, ADMIN_JWT_SECRET, algorithms=["HS256"])
        uid = str(payload.get("uid", ""))
        username = (payload.get("username") or "").strip()
        if uid and uid in ADMIN_USER_IDS:
            return True, payload
        if username and username in ADMIN_USERNAMES:
            return True, payload
        return False, None
    except jwt.ExpiredSignatureError:
        return False, "expired"
    except Exception:
        return False, None

# ========== Telegram WebApp init_data verification ==========
def verify_telegram_init_data(init_data_str):
    if not init_data_str or not BOT_TOKEN:
        return False, None
    try:
        pairs = parse_qsl(init_data_str, keep_blank_values=True)
        params = dict(pairs)
        provided_hash = params.pop('hash', None)
        if not provided_hash:
            return False, None
        data_check_items = []
        for k in sorted(params.keys()):
            data_check_items.append(f"{k}={params[k]}")
        data_check_string = '\n'.join(data_check_items)
        secret_key = hashlib.sha256(BOT_TOKEN.encode('utf-8')).digest()
        hmac_hash = hmac.new(secret_key, data_check_string.encode('utf-8'), hashlib.sha256).hexdigest()
        if hmac.compare_digest(hmac_hash, provided_hash):
            return True, params
        return False, None
    except Exception as e:
        print("verify_telegram_init_data error:", e)
        return False, None

# ========== SOCKET.IO handlers ==========
@socketio.on('connect')
def _on_connect(auth):
    try:
        token = None
        if isinstance(auth, dict):
            token = auth.get('token')
        if not token:
            return  # readonly
        ok, payload = verify_admin_token(token)
        if not ok:
            return False
        uid = str(payload.get('uid') or '')
        username = str(payload.get('username') or '')
        if uid in ADMIN_USER_IDS or username in ADMIN_USERNAMES:
            join_room('admins_main')
        if uid and uid in ordinary_admins:
            join_room('admins_ordinary'); join_room(f'user:{uid}')
        if username and username in ordinary_admins:
            join_room('admins_ordinary'); join_room(f'user_name:{username}')
        if uid: join_room(f'user:{uid}')
        if username: join_room(f'user_name:{username}')
    except Exception as e:
        print("socket connect error:", e)
        return False

@socketio.on('disconnect')
def _on_disconnect():
    pass

# ========== NOTIFY HELPERS ==========
def notify_new_topup(topup):
    try:
        socketio.emit('new_topup', topup, room='admins_ordinary')
        socketio.emit('new_topup', topup, room='admins_main')
        uid = topup.get('user', {}).get('id')
        if uid:
            socketio.emit('new_topup_user', topup, room=f'user:{uid}')
    except Exception as e:
        print("notify_new_topup error", e)

def notify_update_topup(topup):
    try:
        socketio.emit('update_topup', topup, room='admins_ordinary')
        socketio.emit('update_topup', topup, room='admins_main')
        uid = topup.get('user', {}).get('id')
        if uid:
            socketio.emit('update_topup_user', topup, room=f'user:{uid}')
    except Exception as e:
        print("notify_update_topup error", e)

def notify_new_withdraw(withdraw):
    try:
        socketio.emit('new_withdraw', withdraw, room='admins_ordinary')
        socketio.emit('new_withdraw', withdraw, room='admins_main')
        uid = withdraw.get('user', {}).get('id')
        if uid:
            socketio.emit('new_withdraw_user', withdraw, room=f'user:{uid}')
    except Exception as e:
        print("notify_new_withdraw error", e)

def notify_new_work(work):
    try:
        socketio.emit('new_work', work, room='admins_ordinary')
        socketio.emit('new_work', work, room='admins_main')
        uid = work.get('user', {}).get('id')
        if uid:
            socketio.emit('new_work_user', work, room=f'user:{uid}')
    except Exception as e:
        print("notify_new_work error", e)

def notify_ordinary_admins_text(text, button_text="Открыть панель"):
    if not bot:
        print("Bot not configured, skipping notify_ordinary_admins_text")
        return
    for admin in ordinary_admins:
        try:
            if admin.isdigit():
                token = generate_admin_token(admin, "")
                url = f"{WEBAPP_URL}/mainadmin?token={quote_plus(token)}"
                kb = telebot.types.InlineKeyboardMarkup(); kb.add(telebot.types.InlineKeyboardButton(button_text, url=url))
                bot.send_message(int(admin), text, reply_markup=kb)
            else:
                token = generate_admin_token("", admin)
                url = f"{WEBAPP_URL}/mainadmin?token={quote_plus(token)}"
                kb = telebot.types.InlineKeyboardMarkup(); kb.add(telebot.types.InlineKeyboardButton(button_text, url=url))
                try:
                    bot.send_message(f"@{admin}", text, reply_markup=kb)
                except Exception as e:
                    print("notify -> send to @username failed:", admin, e)
        except Exception as e:
            print("notify -> error for admin", admin, e)

# ========== FLASK ROUTES (public files) ==========
@app.route('/')
def index():
    return send_from_directory('public', 'index.html')

@app.route('/<path:path>')
def static_files(path):
    return send_from_directory('public', path)

@app.route('/webhook', methods=['POST'])
def webhook():
    if not bot:
        return 'bot not configured', 500
    if request.headers.get('content-type') == 'application/json':
        update = telebot.types.Update.de_json(request.get_data().decode('utf-8'))
        bot.process_new_updates([update])
        return '', 200
    return 'Invalid', 403

# (Other API routes remain the same as previously provided: /api/tasks_public, /api/works_pending, /api/user_profile, admin endpoints, etc.)
# For brevity in this snippet, those routes are unchanged — keep the full implementations you already have in your app.py

# ========== WEBHOOK / POLLING SETUP (with clearer logging and auto-polling option) ==========
def start_polling_thread():
    if not bot:
        print("Bot not configured, cannot start polling.")
        return None
    def _poll():
        try:
            print("Starting Telegram polling (in background)...")
            bot.infinity_polling(timeout=60, long_polling_timeout=50)
        except Exception as e:
            print("Polling thread error:", e)
    t = threading.Thread(target=_poll, daemon=True)
    t.start()
    return t

def setup_webhook():
    if not bot:
        print("Bot not configured, skipping webhook setup.")
        return
    time.sleep(3)
    max_attempts = int(os.environ.get("WEBHOOK_MAX_ATTEMPTS", "5"))
    base_backoff = float(os.environ.get("WEBHOOK_BASE_BACKOFF_SECONDS", "3"))
    use_polling_on_fail = os.environ.get("BOT_USE_POLLING", "").lower() in ("1", "true", "yes")
    force_polling = os.environ.get("BOT_FORCE_POLLING", "").lower() in ("1", "true", "yes")
    auto_poll = os.environ.get("BOT_AUTO_POLLING", "").lower() in ("1", "true", "yes")
    webhook_url = f"{WEBAPP_URL.rstrip('/')}/webhook"
    for attempt in range(1, max_attempts + 1):
        try:
            try:
                _socket.getaddrinfo('api.telegram.org', 443)
            except Exception as dns_err:
                print(f"[webhook setup] DNS resolution failed: {dns_err}")
                raise
            try:
                bot.remove_webhook()
            except Exception:
                pass
            time.sleep(1)
            bot.set_webhook(url=webhook_url)
            print(f"Webhook set successfully: {webhook_url}")
            return
        except Exception as e:
            print(f"set_webhook attempt {attempt}/{max_attempts} failed: {e}")
            if attempt >= max_attempts:
                print("Failed to set webhook after maximum attempts.")
                # Decide whether to start polling fallback
                if force_polling or use_polling_on_fail or auto_poll:
                    print("Starting polling fallback because BOT_FORCE_POLLING, BOT_USE_POLLING or BOT_AUTO_POLLING is set.")
                    start_polling_thread()
                else:
                    print("BOT_USE_POLLING / BOT_FORCE_POLLING not enabled — bot will not receive updates.")
                    print("To enable fallback polling set environment variable BOT_FORCE_POLLING=true (or BOT_USE_POLLING=true).")
                return
            backoff = base_backoff * (2 ** (attempt - 1))
            print(f"Retrying in {backoff} seconds...")
            time.sleep(backoff)

# ========== START ==========
if __name__ == '__main__':
    # choose webhook vs polling
    if os.environ.get("BOT_FORCE_POLLING", "").lower() in ("1", "true", "yes"):
        print("BOT_FORCE_POLLING set — starting polling.")
        start_polling_thread()
    else:
        threading.Thread(target=setup_webhook, daemon=True).start()

    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host='0.0.0.0', port=port)
