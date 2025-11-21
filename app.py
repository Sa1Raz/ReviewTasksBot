# Вставьте эти строки В САМЫЙ НАЧАЛО файла (до импорта eventlet и других сетевых библиотек)
import os

# Отключаем greendns у eventlet через переменную окружения.
# Это должно быть установлено ДО импорта eventlet.
os.environ.setdefault('EVENTLET_NO_GREENDNS', 'true')

# Затем импортируем eventlet и патчим
import eventlet
# ВАЖНО: не передавайте dns=False, т.к. в некоторых версиях eventlet этот kwargs отсутствует
eventlet.monkey_patch()
#!/usr/bin/env python3
# coding: utf-8
# IMPORTANT: This file includes a defensive DNS fix for Eventlet greendns.
# Set EVENTLET_NO_GREENDNS before importing eventlet to force system resolver.

import os
# Ensure we disable eventlet greendns as early as possible
os.environ.setdefault('EVENTLET_NO_GREENDNS', 'true')

# Standard libs
import time
import json
import threading
import random
import socket as _socket
import hashlib
import hmac
from datetime import datetime, timedelta
from urllib.parse import quote_plus, parse_qsl

# Third-party libs (safe to import after we set EVENTLET_NO_GREENDNS)
try:
    import jwt
except Exception as e:
    raise RuntimeError("Missing PyJWT: pip install PyJWT") from e

# telebot may be optional for local testing
try:
    import telebot
except Exception:
    telebot = None

# Import eventlet AFTER ensuring EVENTLET_NO_GREENDNS is set
import eventlet
# Explicitly disable DNS greendns support in monkey_patch
# dns=False tells eventlet not to replace DNS functions
eventlet.monkey_patch(dns=False)

from flask import Flask, request, send_from_directory, jsonify, abort
# patching done, now safe to import socketio
from flask_socketio import SocketIO, join_room, leave_room

# ========= CONFIG =========
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
WEBAPP_URL = os.environ.get("WEBAPP_URL", "https://web-production-398fb.up.railway.app")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "@ReviewCashNews")

# Ensure admin id present by default
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

# ========== HELPERS ==========
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

app = Flask(__name__, static_folder='public')

# Initialize bot instance only if token present
if BOT_TOKEN and telebot:
    bot = telebot.TeleBot(BOT_TOKEN)
else:
    bot = None
    if not BOT_TOKEN:
        print("[startup] BOT_TOKEN not set — Telegram bot disabled until configured.")

# SocketIO init
REDIS_URL = os.environ.get("REDIS_URL")
if REDIS_URL:
    socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet", message_queue=REDIS_URL)
else:
    socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

# persisted storage
users = load_json_safe(USERS_FILE, {})
ordinary_admins = load_json_safe(ADMINS_FILE, [])

# ========== Utilities ==========
def save_users():
    save_json(USERS_FILE, users)

def get_user_record(uid):
    key = str(uid)
    if key not in users:
        users[key] = {"balance": 0, "tasks_done": 0, "total_earned": 0, "subscribed": False, "last_submissions": {}}
        save_users()
    return users[key]

def add_ordinary_admin(identifier):
    s = str(identifier)
    if s in ordinary_admins: return False
    ordinary_admins.append(s)
    save_json(ADMINS_FILE, ordinary_admins)
    return True

def remove_ordinary_admin(identifier):
    s = str(identifier)
    if s in ordinary_admins:
        ordinary_admins.remove(s)
        save_json(ADMINS_FILE, ordinary_admins)
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

# ========== DNS helper ==========
def can_resolve_host(host="api.telegram.org"):
    try:
        # Try using system resolver directly
        _socket.getaddrinfo(host, 443)
        return True
    except Exception as e:
        print(f"[net-check] DNS resolution failed for {host}: {e}")
        return False

# ========== Safe bot startup (webhook/polling) ==========
def start_polling_thread_safe():
    if os.environ.get("BOT_DISABLE_BOT", "").lower() in ("1", "true", "yes"):
        print("[bot] BOT_DISABLE_BOT is set — skipping Telegram bot startup.")
        return None
    if not bot:
        print("[bot] Bot not configured (no BOT_TOKEN) — skipping polling.")
        return None
    # If DNS doesn't resolve, skip starting polling to avoid noisy stacktraces
    if not can_resolve_host():
        print("[bot] api.telegram.org cannot be resolved — skipping polling startup.")
        return None

    def _poll_loop():
        while True:
            try:
                print("[bot] Starting Telegram polling (background)...")
                bot.infinity_polling(timeout=60, long_polling_timeout=50)
            except Exception as e:
                print("[bot] Polling error (will retry in 10s):", repr(e))
                time.sleep(10)
            else:
                print("[bot] Polling terminated unexpectedly; restarting in 5s")
                time.sleep(5)
    t = threading.Thread(target=_poll_loop, daemon=True)
    t.start()
    return t

def setup_webhook_safe():
    if os.environ.get("BOT_DISABLE_BOT", "").lower() in ("1", "true", "yes"):
        print("[bot] BOT_DISABLE_BOT is set — skipping webhook setup.")
        return
    if not bot:
        print("[bot] Bot not configured — skipping webhook setup.")
        return
    # check DNS first
    if not can_resolve_host():
        print("[webhook setup] api.telegram.org not resolvable — skipping webhook setup.")
        # fallback to polling if allowed by env
        if os.environ.get("BOT_FORCE_POLLING", "").lower() in ("1", "true", "yes") or os.environ.get("BOT_AUTO_POLLING", "").lower() in ("1","true","yes"):
            print("[webhook setup] BOT_FORCE_POLLING or BOT_AUTO_POLLING set — starting polling fallback.")
            start_polling_thread_safe()
        else:
            print("[webhook setup] To enable fallback polling set BOT_FORCE_POLLING=true")
        return

    max_attempts = int(os.environ.get("WEBHOOK_MAX_ATTEMPTS", "5"))
    base_backoff = float(os.environ.get("WEBHOOK_BASE_BACKOFF_SECONDS", "3"))
    use_polling_on_fail = os.environ.get("BOT_USE_POLLING", "").lower() in ("1", "true", "yes")
    webhook_url = f"{WEBAPP_URL.rstrip('/')}/webhook"
    for attempt in range(1, max_attempts + 1):
        try:
            try:
                bot.remove_webhook()
            except Exception:
                pass
            time.sleep(1)
            bot.set_webhook(url=webhook_url)
            print(f"[webhook setup] Webhook set: {webhook_url}")
            return
        except Exception as e:
            print(f"[webhook setup] set_webhook attempt {attempt}/{max_attempts} failed: {e}")
            if attempt >= max_attempts:
                print("[webhook setup] Failed to set webhook after max attempts.")
                if use_polling_on_fail or os.environ.get("BOT_FORCE_POLLING", "").lower() in ("1","true","yes"):
                    print("[webhook setup] Starting polling fallback.")
                    start_polling_thread_safe()
                else:
                    print("[webhook setup] Enable BOT_FORCE_POLLING to fallback to polling.")
                return
            backoff = base_backoff * (2 ** (attempt - 1))
            print(f"[webhook setup] Retrying in {backoff} seconds...")
            time.sleep(backoff)

# ========== SocketIO handlers and routes ==========
# (keep your existing handlers here; unchanged)

# ========== START ==========
if __name__ == '__main__':
    # If explicit polling requested
    if os.environ.get("BOT_FORCE_POLLING", "").lower() in ("1", "true", "yes"):
        print("[main] BOT_FORCE_POLLING set — starting polling.")
        start_polling_thread_safe()
    else:
        # try webhook setup safely
        threading.Thread(target=setup_webhook_safe, daemon=True).start()

    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host='0.0.0.0', port=port)
