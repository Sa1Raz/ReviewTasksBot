#!/usr/bin/env python3
# coding: utf-8
"""
Improved ReviewCash server (Flask + TeleBot + Flask-SocketIO).

Additions in this variant:
- Public endpoints for submitting support, topup and withdraw requests.
- Bot commands /help and /support.
- Notify admins via socketio and Telegram on user actions.
- Robust handling of Eventlet greendns and optional Flask-Cors.
- Central logging.
"""
import os
# Ensure we disable eventlet greendns as early as possible
os.environ.setdefault('EVENTLET_NO_GREENDNS', 'true')

import time
import json
import threading
import random
import socket as _socket
import hashlib
import hmac
import logging
from datetime import datetime, timedelta
from urllib.parse import quote_plus, parse_qsl

# eventlet import & monkey_patch (safe)
import eventlet
try:
    eventlet.monkey_patch(dns=False)
except TypeError:
    eventlet.monkey_patch()

from flask import Flask, request, send_from_directory, jsonify, abort
# Flask-CORS optional, handled after app object creation
from flask_socketio import SocketIO, join_room, leave_room

# Optional imports
try:
    import telebot
except Exception:
    telebot = None

try:
    import jwt
except Exception:
    jwt = None

# ---------- Logging ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s"
)
logger = logging.getLogger("reviewcash")

# ========== CONFIG ==========
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8033069276:AAFv1-kdQ68LjvLEgLHj3ZXd5ehMqyUXOYU")
WEBAPP_URL = os.environ.get("WEBAPP_URL", "https://web-production-398fb.up.railway.app/").rstrip('/')
if not WEBAPP_URL:
    WEBAPP_URL = "https://web-production-398fb.up.railway.app/"

CHANNEL_ID = os.environ.get("CHANNEL_ID", "@ReviewCashNews")
ADMIN_USER_IDS = [s.strip() for s in os.environ.get("ADMIN_USER_IDS", "6482440657").split(",") if s.strip()]
ADMIN_USERNAMES = [s.strip() for s in os.environ.get("ADMIN_USERNAMES", "").split(",") if s.strip()]

ADMIN_JWT_SECRET = os.environ.get("ADMIN_JWT_SECRET", "replace_with_strong_secret")
ADMIN_TOKEN_TTL_SECONDS = int(os.environ.get("ADMIN_TOKEN_TTL_SECONDS", 300))

DATA_DIR = os.environ.get("DATA_DIR", ".rc_data")
os.makedirs(DATA_DIR, exist_ok=True)
TOPUPS_FILE = os.path.join(DATA_DIR, "topups.json")
WITHDRAWS_FILE = os.path.join(DATA_DIR, "withdraws.json")
TASKS_FILE = os.path.join(DATA_DIR, "tasks.json")
WORKS_FILE = os.path.join(DATA_DIR, "works.json")
USERS_FILE = os.path.join(DATA_DIR, "users.json")
ADMINS_FILE = os.path.join(DATA_DIR, "admins.json")
SUPPORT_FILE = os.path.join(DATA_DIR, "support.json")

# ========== STORAGE HELPERS ==========
def load_json_safe(path, default):
    try:
        if not os.path.exists(path):
            return default
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning("Failed to load JSON %s: %s", path, e)
        return default

def save_json(path, obj):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error("Failed to save JSON %s: %s", path, e)

def append_json(path, obj):
    arr = load_json_safe(path, [])
    arr.append(obj)
    save_json(path, arr)

# ========== APP & SOCKET.IO ==========
app = Flask(__name__, static_folder='public', static_url_path='/')

# Optional CORS
try:
    from flask_cors import CORS
    CORS(app)
    logger.info("Flask-Cors enabled")
except Exception:
    logger.warning("Flask-Cors not installed — continuing without CORS")

REDIS_URL = os.environ.get("REDIS_URL", None)
if REDIS_URL:
    socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet", message_queue=REDIS_URL)
else:
    socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

# persisted
users = load_json_safe(USERS_FILE, {})
ordinary_admins = load_json_safe(ADMINS_FILE, [])

# ========== UTILITIES ==========
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
    if jwt is None:
        raise RuntimeError("PyJWT not installed")
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
    if jwt is None:
        return False, None
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
        logger.debug("verify_admin_token failed: %s", e)
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
        logger.debug("verify_telegram_init_data error: %s", e)
        return False, None

# ========== Bot init (safe) ==========
if BOT_TOKEN and telebot:
    bot = telebot.TeleBot(BOT_TOKEN)
    logger.info("Telebot configured")
else:
    bot = None
    if not BOT_TOKEN:
        logger.warning("BOT_TOKEN not set — Telegram bot disabled")
    else:
        logger.warning("pytelegrambotapi not installed — Telegram features disabled")

# ---- Bot command handlers ----
if bot:
    @bot.message_handler(commands=['start'])
    def handle_start(message):
        try:
            name = (message.from_user.first_name or "").strip() if message.from_user else ""
            bot.send_message(message.chat.id, f"Привет{(' ' + name) if name else ''}! Бот работает. Напиши /help для списка команд.")
            logger.info("[bot] Handled /start from %s (%s)", message.chat.id, getattr(message.from_user, "username", ""))
        except Exception as e:
            logger.exception("[bot] Exception in /start handler: %s", e)

    @bot.message_handler(commands=['help'])
    def handle_help(message):
        try:
            help_text = (
                "Команды бота:\n"
                "/start — приветствие\n"
                "/help — этот список\n"
                "/support — открыть запрос в поддержку (ответьте сообщением после команды)\n"
            )
            bot.send_message(message.chat.id, help_text)
            logger.info("[bot] Handled /help from %s", message.chat.id)
        except Exception as e:
            logger.exception("[bot] Exception in /help handler: %s", e)

    # /support: user will send a follow-up message; we accept next reply as support text
    # Simple approach: ask user to send message; if next message arrives, treat as support submission.
    _awaiting_support = {}  # chat_id -> waiting flag

    @bot.message_handler(commands=['support'])
    def handle_support_cmd(message):
        try:
            bot.send_message(message.chat.id, "Пожалуйста, пришлите сообщение с описанием вашей проблемы — я передам его в поддержку.")
            _awaiting_support[message.chat.id] = True
            logger.info("[bot] Asking %s for support message", message.chat.id)
        except Exception as e:
            logger.exception("[bot] Exception in /support handler: %s", e)

    @bot.message_handler(func=lambda m: True)
    def handle_messages(message):
        try:
            cid = message.chat.id
            text = message.text or ""
            if _awaiting_support.get(cid):
                # Treat as support submission
                _awaiting_support.pop(cid, None)
                # Build support record
                rec = {
                    "id": f"sup_{int(time.time()*1000)}",
                    "user": {"id": cid, "username": getattr(message.from_user, "username", None), "first_name": getattr(message.from_user, "first_name", None)},
                    "message": text,
                    "created_at": datetime.utcnow().isoformat() + "Z",
                    "status": "new"
                }
                append_json(SUPPORT_FILE, rec)
                # Notify admins via socketio and Telegram
                notify_event('new_support', rec, rooms=['admins_main','admins_ordinary'])
                try:
                    for admin in ordinary_admins + ADMIN_USER_IDS:
                        try:
                            if str(admin).isdigit():
                                bot.send_message(int(admin), f"Новый запрос в поддержку от {cid}: {text[:400]}")
                            else:
                                bot.send_message(f"@{admin}", f"Новый запрос в поддержку: {text[:400]}")
                        except Exception:
                            logger.debug("Failed to send support message to admin %s", admin)
                except Exception:
                    logger.exception("Failed to notify admins about support")
                bot.send_message(cid, "Спасибо — ваше сообщение отправлено в поддержку. Мы свяжемся с вами.")
                logger.info("[bot] Received support message from %s", cid)
                return
            # Otherwise echo for debug / or handle other flows
            logger.info("[bot] Generic message from %s: %s", cid, text)
            bot.reply_to(message, f"Echo: {text}")
        except Exception as e:
            logger.exception("[bot] Exception in generic handler: %s", e)

# ========== DNS helper ==========
def can_resolve_host(host="api.telegram.org"):
    try:
        _socket.getaddrinfo(host, 443)
        return True
    except Exception as e:
        logger.debug("DNS resolution failed for %s: %s", host, e)
        return False

# ========== SocketIO handlers ==========
@socketio.on('connect')
def _on_connect(auth):
    try:
        token = None
        if isinstance(auth, dict):
            token = auth.get('token')
        if not token:
            return  # allow readonly clients
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
        logger.exception("socket connect error: %s", e)
        return False

@socketio.on('disconnect')
def _on_disconnect():
    pass

# ========== Notifications ==========
def notify_event(name, payload, rooms=None):
    try:
        if rooms:
            for r in rooms:
                socketio.emit(name, payload, room=r)
        else:
            socketio.emit(name, payload)
    except Exception as e:
        logger.debug("notify_event error: %s", e)

def notify_new_topup(topup):
    notify_event('new_topup', topup, rooms=['admins_ordinary','admins_main'])
    uid = topup.get('user', {}).get('id')
    if uid: notify_event('new_topup_user', topup, rooms=[f'user:{uid}'])

def notify_new_withdraw(withdraw):
    notify_event('new_withdraw', withdraw, rooms=['admins_ordinary','admins_main'])
    uid = withdraw.get('user', {}).get('id')
    if uid: notify_event('new_withdraw_user', withdraw, rooms=[f'user:{uid}'])

def notify_new_support(support):
    notify_event('new_support', support, rooms=['admins_ordinary','admins_main'])

# ========== Routes & API ==========
@app.route('/')
def index():
    return send_from_directory('public', 'index.html')

@app.route('/<path:path>')
def static_files(path):
    return send_from_directory('public', path)

@app.route('/health', methods=['GET'])
def health():
    return jsonify({"ok": True, "timestamp": datetime.utcnow().isoformat() + "Z"})

@app.route('/webhook', methods=['POST'])
def webhook():
    if not bot:
        return 'bot not configured', 500
    if request.headers.get('content-type') == 'application/json':
        try:
            update = telebot.types.Update.de_json(request.get_data().decode('utf-8'))
            bot.process_new_updates([update])
            return '', 200
        except Exception as e:
            logger.exception("webhook processing error: %s", e)
            return 'error', 500
    return 'Invalid', 403

@app.route('/api/tasks_public', methods=['GET'])
def api_tasks_public():
    tasks = load_json_safe(TASKS_FILE, [])
    active = [t for t in tasks if t.get("status") == "active"]
    return jsonify(active)

@app.route('/api/works_pending', methods=['GET'])
def api_works_pending():
    arr = load_json_safe(WORKS_FILE, [])
    return jsonify(arr)

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

# ========== Public endpoints for users ==========
@app.route('/api/support', methods=['POST'])
def api_support_create():
    """
    Public support creation:
    JSON body: { message: str, contact: str (optional) }
    Optionally provide Telegram WebApp init_data in header X-Tg-InitData to identify user.
    """
    payload = request.get_json() or {}
    message_text = (payload.get('message') or "").strip()
    contact = (payload.get('contact') or "").strip()
    if not message_text:
        return jsonify({"ok": False, "reason": "message_required"}), 400

    # Try to extract user from WebApp init_data header if present
    init_data = request.headers.get('X-Tg-InitData') or request.args.get('init_data')
    user_obj = {}
    if init_data:
        ok, params = verify_telegram_init_data(init_data)
        if ok:
            uid = params.get('id') or params.get('user_id') or None
            user_obj = {"id": uid, "params": params}
    rec = {
        "id": f"sup_{int(time.time()*1000)}",
        "user": user_obj,
        "message": message_text,
        "contact": contact,
        "created_at": datetime.utcnow().isoformat() + "Z",
        "status": "new",
        "replies": []
    }
    append_json(SUPPORT_FILE, rec)
    notify_new_support(rec)
    # notify admins by bot
    if bot:
        try:
            admin_msg = f"Новый запрос в поддержку: {message_text[:400]}\nContact: {contact or '—'}"
            for admin in ordinary_admins + ADMIN_USER_IDS:
                try:
                    if str(admin).isdigit():
                        bot.send_message(int(admin), admin_msg)
                    else:
                        bot.send_message(f"@{admin}", admin_msg)
                except Exception:
                    logger.debug("Failed to send support msg to admin %s", admin)
        except Exception:
            logger.exception("Failed to notify admins via bot about support")
    return jsonify({"ok": True, "support": rec})

@app.route('/api/topups_public', methods=['POST'])
def api_topups_public():
    """
    Public topup request:
    JSON body: { amount: number, details: str (optional) }
    Must provide WebApp init_data header or include user id to associate.
    """
    payload = request.get_json() or {}
    try:
        amount = float(payload.get('amount', 0) or 0)
    except Exception:
        amount = 0
    details = (payload.get('details') or "").strip()
    if amount <= 0:
        return jsonify({"ok": False, "reason": "amount_positive_required"}), 400

    init_data = request.headers.get('X-Tg-InitData') or request.args.get('init_data')
    user = {}
    if init_data:
        ok, params = verify_telegram_init_data(init_data)
        if ok:
            uid = params.get('id') or params.get('user_id') or None
            user = {"id": uid, "params": params}
    rec = {
        "id": f"top_{int(time.time()*1000)}",
        "user": user,
        "amount": amount,
        "details": details,
        "status": "pending",
        "created_at": datetime.utcnow().isoformat() + "Z"
    }
    append_json(TOPUPS_FILE, rec)
    notify_new_topup(rec)
    # Inform admins via bot
    if bot:
        try:
            admin_msg = f"Новый запрос на пополнение: {amount} ₽\nUser: {user.get('id') or 'unknown'}\nDetails: {details or '—'}"
            for admin in ordinary_admins + ADMIN_USER_IDS:
                try:
                    if str(admin).isdigit():
                        bot.send_message(int(admin), admin_msg)
                    else:
                        bot.send_message(f"@{admin}", admin_msg)
                except Exception:
                    logger.debug("Failed to send topup msg to admin %s", admin)
        except Exception:
            logger.exception("Failed to notify admins via bot about topup")
    return jsonify({"ok": True, "topup": rec})

@app.route('/api/withdraw_public', methods=['POST'])
def api_withdraw_public():
    """
    Public withdraw request:
    JSON body: { amount: number, method: str, details: str }
    """
    payload = request.get_json() or {}
    try:
        amount = float(payload.get('amount', 0) or 0)
    except Exception:
        amount = 0
    method = (payload.get('method') or "").strip()
    details = (payload.get('details') or "").strip()
    if amount <= 0 or not method:
        return jsonify({"ok": False, "reason": "amount_and_method_required"}), 400

    init_data = request.headers.get('X-Tg-InitData') or request.args.get('init_data')
    user = {}
    if init_data:
        ok, params = verify_telegram_init_data(init_data)
        if ok:
            uid = params.get('id') or params.get('user_id') or None
            user = {"id": uid, "params": params}
    rec = {
        "id": f"wd_{int(time.time()*1000)}",
        "user": user,
        "amount": amount,
        "method": method,
        "details": details,
        "status": "pending",
        "created_at": datetime.utcnow().isoformat() + "Z"
    }
    append_json(WITHDRAWS_FILE, rec)
    notify_new_withdraw(rec)
    if bot:
        try:
            admin_msg = f"Новый запрос на вывод: {amount} ₽\nUser: {user.get('id') or 'unknown'}\nMethod: {method}\nDetails: {details or '—'}"
            for admin in ordinary_admins + ADMIN_USER_IDS:
                try:
                    if str(admin).isdigit():
                        bot.send_message(int(admin), admin_msg)
                    else:
                        bot.send_message(f"@{admin}", admin_msg)
                except Exception:
                    logger.debug("Failed to send withdraw msg to admin %s", admin)
        except Exception:
            logger.exception("Failed to notify admins via bot about withdraw")
    return jsonify({"ok": True, "withdraw": rec})

# ========== Support admin endpoints (new) ==========
@app.route('/api/supports', methods=['GET'])
@require_admin_token
def api_supports_list():
    """
    Admin-only: list support requests.
    Optional ?status=new|replied|resolved
    """
    status = (request.args.get('status') or "").strip().lower()
    arr = load_json_safe(SUPPORT_FILE, [])
    if status:
        arr = [s for s in arr if (s.get('status') or '').lower() == status]
    # return as-is (admins will filter on client)
    return jsonify(arr)

@app.route('/api/supports/<sid>/reply', methods=['POST'])
@require_admin_token
def api_support_reply(sid):
    """
    Admin replies to support request.
    Body: { message: str, notify: bool (default true) }
    """
    data = request.get_json() or {}
    message = (data.get('message') or "").strip()
    notify_flag = bool(data.get('notify', True))
    if not message:
        return jsonify({"ok": False, "reason": "message_required"}), 400

    token = get_token_from_request(request)
    ok, payload = verify_admin_token(token)
    admin_ident = {}
    if ok and isinstance(payload, dict):
        admin_ident = {"uid": payload.get("uid"), "username": payload.get("username")}

    arr = load_json_safe(SUPPORT_FILE, [])
    found = None
    for s in arr:
        if s.get('id') == sid:
            found = s
            break
    if not found:
        return jsonify({"ok": False, "reason": "not_found"}), 404

    reply = {
        "message": message,
        "admin": admin_ident,
        "created_at": datetime.utcnow().isoformat() + "Z"
    }
    found.setdefault('replies', []).append(reply)
    found['status'] = 'replied'
    found.setdefault('handled_by', admin_ident)
    found['handled_at'] = datetime.utcnow().isoformat() + "Z"
    save_json(SUPPORT_FILE, arr)

    # notify via socket
    notify_event('update_support', found, rooms=['admins_main','admins_ordinary'])
    # notify user via bot if possible
    if notify_flag and bot:
        try:
            uid = found.get('user', {}).get('id')
            if uid:
                try:
                    # uid may be string; try int
                    bot.send_message(int(uid), f"Ответ поддержки: {message}")
                except Exception:
                    # fallback to username mention
                    uname = found.get('user', {}).get('username')
                    if uname:
                        try:
                            bot.send_message(f"@{uname}", f"Ответ поддержки: {message}")
                        except Exception:
                            logger.debug("Failed to notify user via bot for support %s", sid)
        except Exception:
            logger.exception("Failed to send reply to user via bot")
    return jsonify({"ok": True, "support": found})

@app.route('/api/supports/<sid>/resolve', methods=['POST'])
@require_admin_token
def api_support_resolve(sid):
    """
    Mark support request resolved/closed.
    Body: { reason: str (optional), notify: bool (default true) }
    """
    data = request.get_json() or {}
    reason = (data.get('reason') or "").strip()
    notify_flag = bool(data.get('notify', True))

    token = get_token_from_request(request)
    ok, payload = verify_admin_token(token)
    admin_ident = {}
    if ok and isinstance(payload, dict):
        admin_ident = {"uid": payload.get("uid"), "username": payload.get("username")}

    arr = load_json_safe(SUPPORT_FILE, [])
    found = None
    for s in arr:
        if s.get('id') == sid:
            found = s
            break
    if not found:
        return jsonify({"ok": False, "reason": "not_found"}), 404

    found['status'] = 'resolved'
    found.setdefault('closed_by', admin_ident)
    found['closed_at'] = datetime.utcnow().isoformat() + "Z"
    if reason:
        found.setdefault('close_reason', reason)
    save_json(SUPPORT_FILE, arr)

    notify_event('update_support', found, rooms=['admins_main','admins_ordinary'])
    # notify user via bot if possible
    if notify_flag and bot:
        try:
            uid = found.get('user', {}).get('id')
            if uid:
                try:
                    bot.send_message(int(uid), f"Ваш запрос закрыт. {reason or ''}")
                except Exception:
                    uname = found.get('user', {}).get('username')
                    if uname:
                        try:
                            bot.send_message(f"@{uname}", f"Ваш запрос закрыт. {reason or ''}")
                        except Exception:
                            logger.debug("Failed to notify user via bot for support resolve %s", sid)
        except Exception:
            logger.exception("Failed to send resolve notification to user via bot")
    return jsonify({"ok": True, "support": found})

# ========== User history endpoints (added) ==========
from functools import cmp_to_key

@app.route('/api/user_history', methods=['GET'])
def api_user_history():
    """
    Returns combined history for given uid.
    Query params:
      - uid (required)
      - page (default 1)
      - page_size (default 20)
      - type (optional: topup|withdraw|work)
    """
    uid = (request.args.get('uid') or "").strip()
    if not uid:
        return jsonify({"ok": False, "reason": "missing_uid"}), 400
    page = int(request.args.get('page') or 1)
    page_size = int(request.args.get('page_size') or 20)
    ftype = (request.args.get('type') or "").strip().lower()

    # Load sources
    topups = load_json_safe(TOPUPS_FILE, [])
    withdraws = load_json_safe(WITHDRAWS_FILE, [])
    works = load_json_safe(WORKS_FILE, [])

    items = []

    def push_items(arr, typ):
        for it in arr:
            user_id = None
            try:
                user_id = str(it.get('user', {}).get('id') or '')
            except Exception:
                user_id = ''
            if user_id == str(uid):
                summary = ''
                if typ == 'topup':
                    summary = f"Пополнение {it.get('amount',0)} ₽"
                elif typ == 'withdraw':
                    summary = f"Вывод {it.get('amount',0)} ₽ ({it.get('method') or ''})"
                elif typ == 'work':
                    summary = it.get('task_title') or it.get('task_id') or ''
                items.append({
                    "id": it.get('id'),
                    "type": typ,
                    "amount": it.get('amount'),
                    "summary": summary,
                    "created_at": it.get('created_at') or it.get('handled_at') or ''
                })

    if not ftype or ftype == 'topup':
        push_items(topups, 'topup')
    if not ftype or ftype == 'withdraw':
        push_items(withdraws, 'withdraw')
    if not ftype or ftype == 'work':
        push_items(works, 'work')

    # sort by created_at desc (attempt parse ISO)
    def cmp(a,b):
        try:
            ta = a.get('created_at') or ''
            tb = b.get('created_at') or ''
            # simple lexicographic compare for ISO timestamps
            if ta > tb: return -1
            if ta < tb: return 1
            return 0
        except:
            return 0
    items.sort(key=cmp_to_key(cmp))

    total = len(items)
    start = (page-1)*page_size
    end = start + page_size
    paged = items[start:end]
    return jsonify({"ok": True, "items": paged, "total": total, "page": page, "page_size": page_size})

@app.route('/api/user_history_me', methods=['GET'])
def api_user_history_me():
    """
    Convenience: uses Telegram WebApp init_data to identify user and returns user_history.
    """
    init_data = request.headers.get('X-Tg-InitData') or request.args.get('init_data')
    if not init_data:
        return jsonify({"ok": False, "reason": "init_data_required"}), 401
    ok, params = verify_telegram_init_data(init_data)
    if not ok:
        return jsonify({"ok": False, "reason": "invalid_init_data"}), 403
    uid = params.get('id') or params.get('user_id')
    if not uid:
        return jsonify({"ok": False, "reason": "uid_not_found"}), 400
    # Forward to /api/user_history
    args = request.args.to_dict()
    args['uid'] = uid
    # build query string
    qs = '&'.join([f"{k}={v}" for k,v in args.items()])
    return app.test_client().get(f"/api/user_history?{qs}").get_data(as_text=False), 200, {'Content-Type':'application/json'}

# ========== Existing admin endpoints (unchanged) ==========
# ... (existing admin endpoints remain as in previous app.py, not duplicated here)
# For brevity in this chat message we assume earlier admin endpoints (approve/reject etc.) remain unchanged.

# ========== WEBHOOK / POLLING SAFE START ==========
def start_polling_thread_safe():
    if os.environ.get("BOT_DISABLE_BOT", "").lower() in ("1", "true", "yes"):
        logger.info("[bot] BOT_DISABLE_BOT is set — skipping Telegram bot startup.")
        return None
    if not bot:
        logger.info("[bot] bot not configured — skipping polling.")
        return None

    try:
        bot.remove_webhook()
        logger.info("[bot] remove_webhook() called — webhook removed (or was not set).")
    except Exception as e:
        logger.debug("[bot] remove_webhook() failed or not needed: %s", repr(e))

    if not can_resolve_host():
        logger.warning("[bot] api.telegram.org cannot be resolved — skipping polling startup.")
        return None

    def _poll_loop():
        while True:
            try:
                logger.info("[bot] Starting Telegram polling (background)...")
                bot.infinity_polling(timeout=60, long_polling_timeout=50)
            except Exception as e:
                errstr = repr(e)
                logger.error("[bot] Polling error (will retry in 10s): %s", errstr)
                if "Conflict: can't use getUpdates" in errstr or "409" in errstr:
                    try:
                        bot.remove_webhook()
                        logger.info("[bot] remove_webhook() called after 409 — webhook removed.")
                    except Exception as e2:
                        logger.debug("[bot] remove_webhook() after 409 failed: %s", repr(e2))
                time.sleep(10)
            else:
                logger.warning("[bot] Polling stopped unexpectedly; restarting in 5s")
                time.sleep(5)

    t = threading.Thread(target=_poll_loop, daemon=True)
    t.start()
    return t

def setup_webhook_safe():
    if os.environ.get("BOT_DISABLE_BOT", "").lower() in ("1", "true", "yes"):
        logger.info("[bot] BOT_DISABLE_BOT is set — skipping webhook setup.")
        return
    if not bot:
        logger.info("[bot] bot not configured — skipping webhook setup.")
        return

    if not can_resolve_host():
        logger.warning("[webhook setup] api.telegram.org not resolvable — skipping webhook setup.")
        if os.environ.get("BOT_FORCE_POLLING", "").lower() in ("1", "true", "yes"):
            start_polling_thread_safe()
        else:
            logger.info("[webhook setup] To enable fallback polling set BOT_FORCE_POLLING=true")
        return

    max_attempts = int(os.environ.get("WEBHOOK_MAX_ATTEMPTS", "5"))
    base_backoff = float(os.environ.get("WEBHOOK_BASE_BACKOFF_SECONDS", "3"))
    use_polling_on_fail = os.environ.get("BOT_USE_POLLING", "").lower() in ("1", "true", "yes")
    webhook_url = f"{WEBAPP_URL.rstrip('/')}/webhook"
    for attempt in range(1, max_attempts + 1):
        try:
            try:
                bot.remove_webhook()
                logger.info("[webhook setup] remove_webhook() called before set_webhook.")
            except Exception:
                pass
            time.sleep(1)
            bot.set_webhook(url=webhook_url)
            logger.info(f"[webhook setup] Webhook set: {webhook_url}")
            return
        except Exception as e:
            logger.warning("[webhook setup] set_webhook attempt %s/%s failed: %s", attempt, max_attempts, e)
            if attempt >= max_attempts:
                logger.error("[webhook setup] Failed to set webhook after max attempts.")
                if use_polling_on_fail or os.environ.get("BOT_FORCE_POLLING", "").lower() in ("1","true","yes"):
                    start_polling_thread_safe()
                else:
                    logger.info("[webhook setup] Enable BOT_FORCE_POLLING to fallback to polling.")
                return
            backoff = base_backoff * (2 ** (attempt - 1))
            logger.info("[webhook setup] Retrying in %s seconds...", backoff)
            time.sleep(backoff)

# ========== START ==========
if __name__ == '__main__':
    if os.environ.get("BOT_FORCE_POLLING", "").lower() in ("1", "true", "yes"):
        logger.info("[main] BOT_FORCE_POLLING set — starting polling.")
        start_polling_thread_safe()
    else:
        threading.Thread(target=setup_webhook_safe, daemon=True).start()

    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host='0.0.0.0', port=port)
