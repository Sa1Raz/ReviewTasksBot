#!/usr/bin/env python3
# coding: utf-8
"""
Main application server for ReviewCash (Flask + Telebot + SocketIO).
Configure via environment variables:
- BOT_TOKEN
- WEBAPP_URL
- CHANNEL_ID
- ADMIN_USER_IDS (comma-separated)
- ADMIN_USERNAMES (comma-separated)
- ADMIN_JWT_SECRET
- REDIS_URL (optional for socket message queue)
- BOT_FORCE_POLLING / BOT_USE_POLLING
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
    raise RuntimeError("Python package 'pytelegrambotapi' is not installed. Install: pip install pytelegrambotapi")

from flask import Flask, request, send_from_directory, jsonify, abort, make_response
import eventlet
eventlet.monkey_patch()
from flask_socketio import SocketIO, join_room, leave_room

# ========== CONFIG ==========
BOT_TOKEN = os.environ.get("8033069276:AAFv1-kdQ68LjvLEgLHj3ZXd5ehMqyUXOYU", "")  # prefer set in env, empty string by default
WEBAPP_URL = os.environ.get("WEBAPP_URL", "https://web-production-398fb.up.railway.app/")  # change to your public URL
CHANNEL_ID = os.environ.get("CHANNEL_ID", "@ReviewCashNews")

ADMIN_USER_IDS = [s.strip() for s in os.environ.get("ADMIN_USER_IDS", "").split(",") if s.strip()]
ADMIN_USERNAMES = [s.strip() for s in os.environ.get("ADMIN_USERNAMES", "").split(",") if s.strip()]

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

# bot (telebot) - do not start polling here; we'll choose webhook or polling depending on env
if not BOT_TOKEN:
    print("Warning: BOT_TOKEN is empty. Set BOT_TOKEN env variable to enable Telegram bot functionality.")
bot = telebot.TeleBot(BOT_TOKEN) if BOT_TOKEN else None

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
    if s in ADMIN_USER_IDS: return True
    if s in ADMIN_USERNAMES: return True
    return False

def is_ordinary_admin(uid_or_username):
    s = str(uid_or_username)
    return s in ordinary_admins

def add_ordinary_admin(identifier):
    s = str(identifier)
    if s in ordinary_admins: return False
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
    except Exception as e:
        return False, None

# ========== Telegram WebApp init_data verification ==========
def verify_telegram_init_data(init_data_str):
    """
    Verify Telegram WebApp initData (string). Returns (True, data_dict) if valid, else (False, None).
    Algorithm per Telegram docs:
      - parse key=value pairs from init_data_str (query string)
      - extract hash, remove it from params
      - build data_check_string = '\n'.join(sorted(["key=value"]))
      - secret_key = SHA256(BOT_TOKEN)
      - compute HMAC-SHA256(secret_key, data_check_string) -> hex digest
      - compare with provided hash (hex, lowercase)
    """
    if not init_data_str or not BOT_TOKEN:
        return False, None
    try:
        # parse_qsl will handle URL-decoding
        pairs = parse_qsl(init_data_str, keep_blank_values=True)
        params = dict(pairs)
        provided_hash = params.pop('hash', None)
        if not provided_hash:
            return False, None
        # build data_check_string with keys sorted lexicographically
        data_check_items = []
        for k in sorted(params.keys()):
            # use original values (already URL-decoded by parse_qsl)
            data_check_items.append(f"{k}={params[k]}")
        data_check_string = '\n'.join(data_check_items)
        # secret key is SHA256 of bot token (digest bytes)
        secret_key = hashlib.sha256(BOT_TOKEN.encode('utf-8')).digest()
        hmac_hash = hmac.new(secret_key, data_check_string.encode('utf-8'), hashlib.sha256).hexdigest()
        # compare hex digests in constant time
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
            # allow readonly connections (e.g., anonymous)
            return
        ok, payload = verify_admin_token(token)
        if not ok:
            return False
        uid = str(payload.get('uid') or '')
        username = str(payload.get('username') or '')
        if uid in ADMIN_USER_IDS or username in ADMIN_USERNAMES:
            join_room('admins_main')
        # join ordinary admin rooms if applicable
        if uid and uid in ordinary_admins:
            join_room('admins_ordinary')
            join_room(f'user:{uid}')
        if username and username in ordinary_admins:
            join_room('admins_ordinary')
            join_room(f'user_name:{username}')
        # always join personal rooms for this identity
        if uid:
            join_room(f'user:{uid}')
        if username:
            join_room(f'user_name:{username}')
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
                kb = telebot.types.InlineKeyboardMarkup()
                kb.add(telebot.types.InlineKeyboardButton(button_text, url=url))
                bot.send_message(int(admin), text, reply_markup=kb)
            else:
                token = generate_admin_token("", admin)
                url = f"{WEBAPP_URL}/mainadmin?token={quote_plus(token)}"
                kb = telebot.types.InlineKeyboardMarkup()
                kb.add(telebot.types.InlineKeyboardButton(button_text, url=url))
                try:
                    bot.send_message(f"@{admin}", text, reply_markup=kb)
                except Exception as e:
                    print("notify -> send to @username failed:", admin, e)
        except Exception as e:
            print("notify -> error for admin", admin, e)

def notify_admins_topup(topup):
    text = (f"Новая заявка на пополнение\n"
            f"Пользователь: {topup['user'].get('username','-')} ({topup['user'].get('id','-')})\n"
            f"Сумма: {topup.get('amount',0)} ₽\n"
            f"Код: {topup.get('code','-')}\n"
            f"Создано: {topup.get('created_at','-')}")
    notify_ordinary_admins_text(text, button_text="Проверить пополнение")

def notify_admins_withdraw(withdraw):
    text = (f"Новая заявка на вывод\n"
            f"Пользователь: {withdraw['user'].get('username','-')} ({withdraw['user'].get('id','-')})\n"
            f"Сумма: {withdraw.get('amount',0)} ₽\n"
            f"Реквизиты: {withdraw.get('card','-')} / {withdraw.get('bank','-')}\n"
            f"ФИО: {withdraw.get('name','-')}\n"
            f"Создано: {withdraw.get('created_at','-')}")
    notify_ordinary_admins_text(text, button_text="Проверить вывод")

def notify_admins_work(work):
    """
    Notify: send socket broadcast to main admins; choose one ordinary admin to notify via bot and socket.
    """
    text = (f"Новая заявка на выполнение задания\n"
            f"Пользователь: {work['user'].get('username','-')} ({work['user'].get('id','-')})\n"
            f"Задание: {work.get('task_title','-')}\n"
            f"Тип: {work.get('platform','-')} · Сумма: {work.get('amount',0)} ₽\n"
            f"Создано: {work.get('created_at','-')}")
    # notify via socket to main admins
    try:
        socketio.emit('new_work', work, room='admins_main')
    except Exception:
        pass

    if not ordinary_admins:
        # fallback to main admins via bot
        if bot:
            for ma in ADMIN_USER_IDS:
                try:
                    token = generate_admin_token(ma, "")
                    url = f"{WEBAPP_URL}/mainadmin?token={quote_plus(token)}"
                    kb = telebot.types.InlineKeyboardMarkup()
                    kb.add(telebot.types.InlineKeyboardButton("Проверить выполнение", url=url))
                    bot.send_message(int(ma), text, reply_markup=kb)
                except Exception as e:
                    print("notify_admins_work -> send to main admin failed:", ma, e)
        return

    # choose one ordinary admin randomly
    admin = random.choice(ordinary_admins)
    try:
        if bot:
            if admin.isdigit():
                token = generate_admin_token(admin, "")
                url = f"{WEBAPP_URL}/mainadmin?token={quote_plus(token)}"
                kb = telebot.types.InlineKeyboardMarkup()
                kb.add(telebot.types.InlineKeyboardButton("Проверить выполнение", url=url))
                bot.send_message(int(admin), text, reply_markup=kb)
                # socket notify for that admin room
                try:
                    socketio.emit('new_work', work, room=f'user:{admin}')
                except Exception:
                    pass
            else:
                token = generate_admin_token("", admin)
                url = f"{WEBAPP_URL}/mainadmin?token={quote_plus(token)}"
                kb = telebot.types.InlineKeyboardMarkup()
                kb.add(telebot.types.InlineKeyboardButton("Проверить выполнение", url=url))
                try:
                    bot.send_message(f"@{admin}", text, reply_markup=kb)
                except Exception as e:
                    print("notify_admins_work: can't send to @username", admin, e)
                try:
                    socketio.emit('new_work', work, room=f'user_name:{admin}')
                except Exception:
                    pass
    except Exception as e:
        print("notify_admins_work -> send to chosen ordinary admin failed:", e)
        # fallback: notify main admins
        if bot:
            for ma in ADMIN_USER_IDS:
                try:
                    token = generate_admin_token(ma, "")
                    url = f"{WEBAPP_URL}/mainadmin?token={quote_plus(token)}"
                    kb = telebot.types.InlineKeyboardMarkup()
                    kb.add(telebot.types.InlineKeyboardButton("Проверить выполнение", url=url))
                    bot.send_message(int(ma), text, reply_markup=kb)
                except Exception as e2:
                    print("notify_admins_work fallback send failed:", e2)

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

# ========== WEBAPP FEATURES: user flows (publish, submit, topup, withdraw) ==========
@app.route('/api/tasks_public', methods=['GET'])
def api_tasks_public():
    tasks = load_json_safe(TASKS_FILE, [])
    active = [t for t in tasks if t.get("status") == "active"]
    return jsonify(active)

@app.route('/api/works_pending', methods=['GET'])
def api_works_pending():
    arr = load_json_safe(WORKS_FILE, [])
    return jsonify(arr)

# Admin auth helpers
def get_token_from_request(req):
    t = req.args.get("token")
    if t:
        return t
    auth = req.headers.get("Authorization", "") or req.headers.get("authorization", "")
    if auth and auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip()
    return None

def require_admin_token(func):
    def wrapper(*args, **kwargs):
        token = get_token_from_request(request)
        if not token:
            return abort(401, "token required")
        ok, payload_or_reason = verify_admin_token(token)
        if not ok:
            if payload_or_reason == "expired":
                return abort(401, "token expired")
            return abort(403, "forbidden")
        return func(*args, **kwargs)
    wrapper.__name__ = func.__name__
    return wrapper

@app.route('/api/user_profile', methods=['GET'])
def api_user_profile():
    """
    Protected user profile endpoint.
    Protection modes:
      - If admin token is provided (as ?token= or Authorization: Bearer), allow admin to query arbitrary uid via uid param.
      - Otherwise, require Telegram WebApp initData (query param init_data or header X-Tg-InitData). Verify it, and use id from verified initData.
    Client (WebApp) should pass Telegram.WebApp.initData (exact string) as init_data parameter:
      GET /api/user_profile?init_data=<encodeURIComponent(Telegram.WebApp.initData)>
    Or provide header: X-Tg-InitData: <Telegram.WebApp.initData>
    """
    # 1) admin token path: allow admin to query any uid
    admin_token = get_token_from_request(request)
    if admin_token:
        ok, payload_or_reason = verify_admin_token(admin_token)
        if ok:
            uid = request.args.get('uid')
            if not uid:
                return jsonify({"ok": False, "reason": "missing_uid"}), 400
            rec = users.get(str(uid))
            if rec is None:
                rec = {"balance": 0, "tasks_done": 0, "total_earned": 0, "subscribed": False, "last_submissions": {}}
                users[str(uid)] = rec
                save_users()
            payload = {
                "balance": rec.get("balance", 0),
                "tasks_done": rec.get("tasks_done", 0),
                "total_earned": rec.get("total_earned", 0),
                "subscribed": rec.get("subscribed", False),
                "last_submissions": rec.get("last_submissions", {})
            }
            return jsonify({"ok": True, "user": payload})
        else:
            # invalid admin token: fall through to telegram init_data check (or block)
            # For clarity, reject
            return jsonify({"ok": False, "reason": "invalid_admin_token"}), 403

    # 2) Telegram WebApp init_data path
    init_data = request.args.get('init_data') or request.headers.get('X-Tg-InitData') or request.headers.get('X-Init-Data')
    if not init_data:
        return jsonify({"ok": False, "reason": "init_data_required"}), 401

    ok, params = verify_telegram_init_data(init_data)
    if not ok:
        return jsonify({"ok": False, "reason": "invalid_init_data"}), 403

    # extract user id from verified params: attempt common keys
    uid = params.get('id') or params.get('user_id') or params.get('user') or None
    if not uid:
        # some WebApp versions may embed user object; try to parse 'user' JSON if present
        try:
            user_field = params.get('user')
            if user_field:
                # user may be JSON-encoded
                uobj = json.loads(user_field)
                uid = uobj.get('id')
        except Exception:
            uid = None

    if not uid:
        return jsonify({"ok": False, "reason": "uid_not_found_in_init_data"}), 400

    # now safe to return profile for uid
    try:
        rec = users.get(str(uid))
        if rec is None:
            rec = {"balance": 0, "tasks_done": 0, "total_earned": 0, "subscribed": False, "last_submissions": {}}
            users[str(uid)] = rec
            save_users()
        payload = {
            "balance": rec.get("balance", 0),
            "tasks_done": rec.get("tasks_done", 0),
            "total_earned": rec.get("total_earned", 0),
            "subscribed": rec.get("subscribed", False),
            "last_submissions": rec.get("last_submissions", {})
        }
        return jsonify({"ok": True, "user": payload})
    except Exception as e:
        return jsonify({"ok": False, "reason": "error", "error": str(e)}), 500

# Admin-protected APIs (list/add/approve/reject) ...
# (The rest of the app routes remain unchanged; for brevity they are omitted here in this snippet,
#  but in your real file keep all previously implemented routes: /mainadmin, /api/topups, /api/withdraws, /api/tasks, /api/works, etc.)
# If you replace your existing app.py, ensure the remaining routes (as previously provided) are kept below.

# ========== WEBHOOK / POLLING SETUP ==========
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
                if use_polling_on_fail:
                    print("BOT_USE_POLLING enabled — starting polling fallback.")
                    start_polling_thread()
                else:
                    print("BOT_USE_POLLING not enabled — bot will not receive updates.")
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
