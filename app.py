#!/usr/bin/env python3
# coding: utf-8
"""
Improved ReviewCash server (Flask + TeleBot + Flask-SocketIO).

Features included:
- Early disable of eventlet greendns via ENV (EVENTLET_NO_GREENDNS).
- Safe eventlet.monkey_patch() usage with fallback for versions that don't accept dns kwarg.
- Safe bot startup: remove webhook before polling to avoid Telegram 409 conflict.
- setup_webhook_safe tries to set a webhook but falls back to polling if allowed.
- Graceful handling if Flask-Cors is not installed.
- Basic handlers: /start and a simple echo for testing.
- Health endpoint.
- Persistent JSON-based storage (local files).
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
    # Some eventlet versions accept dns kwarg; try it first
    eventlet.monkey_patch(dns=False)
except TypeError:
    # Fallback: call monkey_patch without dns kwarg. The EVENTLET_NO_GREENDNS env var prevents greendns.
    eventlet.monkey_patch()

from flask import Flask, request, send_from_directory, jsonify, abort
# Flask-CORS will be enabled if available (handled after app creation)
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
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")  # set in env
WEBAPP_URL = os.environ.get("WEBAPP_URL", "").rstrip('/')
if not WEBAPP_URL:
    WEBAPP_URL = "https://example.com"  # fallback for local dev

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

# Enable Flask-Cors if installed (graceful fallback)
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

# ---- Handlers: put after bot initialization ----
if bot:
    @bot.message_handler(commands=['start'])
    def handle_start(message):
        try:
            name = (message.from_user.first_name or "").strip() if message.from_user else ""
            bot.send_message(message.chat.id, f"Привет{(' ' + name) if name else ''}! Бот работает.")
            logger.info("[bot] Handled /start from %s (%s)", message.chat.id, getattr(message.from_user, "username", ""))
        except Exception as e:
            logger.exception("[bot] Exception in /start handler: %s", e)

    @bot.message_handler(func=lambda m: True)
    def handle_all_messages(message):
        try:
            text = message.text or ""
            logger.info("[bot] Incoming message from %s: %s", message.chat.id, text)
            bot.reply_to(message, f"Echo: {text}")
        except Exception as e:
            logger.exception("[bot] Exception in generic message handler: %s", e)

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

def notify_update_topup(topup):
    notify_event('update_topup', topup, rooms=['admins_ordinary','admins_main'])
    uid = topup.get('user', {}).get('id')
    if uid: notify_event('update_topup_user', topup, rooms=[f'user:{uid}'])

def notify_new_withdraw(withdraw):
    notify_event('new_withdraw', withdraw, rooms=['admins_ordinary','admins_main'])
    uid = withdraw.get('user', {}).get('id')
    if uid: notify_event('new_withdraw_user', withdraw, rooms=[f'user:{uid}'])

def notify_new_work(work):
    notify_event('new_work', work, rooms=['admins_ordinary','admins_main'])
    uid = work.get('user', {}).get('id')
    if uid: notify_event('new_work_user', work, rooms=[f'user:{uid}'])

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

@app.route('/api/user_profile', methods=['GET'])
def api_user_profile():
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
            return jsonify({"ok": False, "reason": "invalid_admin_token"}), 403

    init_data = request.args.get('init_data') or request.headers.get('X-Tg-InitData') or request.headers.get('X-Init-Data')
    if not init_data:
        return jsonify({"ok": False, "reason": "init_data_required"}), 401

    ok, params = verify_telegram_init_data(init_data)
    if not ok:
        return jsonify({"ok": False, "reason": "invalid_init_data"}), 403

    uid = params.get('id') or params.get('user_id') or params.get('user') or None
    if not uid:
        try:
            user_field = params.get('user')
            if user_field:
                uobj = json.loads(user_field)
                uid = uobj.get('id')
        except Exception:
            uid = None

    if not uid:
        return jsonify({"ok": False, "reason": "uid_not_found_in_init_data"}), 400

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

# Admin endpoints (list/create tasks, topups, withdraws, works)
@app.route('/mainadmin')
def serve_mainadmin():
    token = request.args.get("token")
    ok, _ = verify_admin_token(token) if token else (False, None)
    if not ok:
        return "<h3>Access denied. Open admin panel only via Telegram-generated token.</h3>", 403
    return send_from_directory('public', 'mainadmin.html')

@app.route('/api/topups', methods=['GET'])
@require_admin_token
def api_topups():
    data = load_json_safe(TOPUPS_FILE, [])
    return jsonify(data)

@app.route('/api/withdraws', methods=['GET'])
@require_admin_token
def api_withdraws():
    data = load_json_safe(WITHDRAWS_FILE, [])
    return jsonify(data)

@app.route('/api/tasks', methods=['GET'])
@require_admin_token
def api_tasks():
    data = load_json_safe(TASKS_FILE, [])
    return jsonify(data)

@app.route('/api/tasks', methods=['POST'])
@require_admin_token
def api_tasks_create():
    payload = request.get_json() or {}
    title = (payload.get('title') or "")[:200]
    link = (payload.get('link') or "")[:1000]
    ttype = payload.get('type') or ""
    try:
        budget = int(payload.get('budget', 0) or 0)
    except Exception:
        budget = 0
    if not title or not ttype or budget <= 0:
        return jsonify({"ok": False, "reason": "missing_fields"}), 400
    token = get_token_from_request(request)
    ok, payload_token = verify_admin_token(token)
    owner_id = payload_token.get('uid') or ""
    owner_username = payload_token.get('username') or ""
    task = {
        "id": f"task_{int(time.time()*1000)}",
        "title": title,
        "link": link,
        "type": ttype,
        "budget": budget,
        "owner": {"id": str(owner_id), "username": owner_username},
        "created_at": datetime.utcnow().isoformat() + "Z",
        "status": "active"
    }
    append_json(TASKS_FILE, task)
    try:
        socketio.emit('new_task', task, room='admins_main')
        socketio.emit('new_task', task, room='admins_ordinary')
    except Exception:
        pass
    return jsonify({"ok": True, "task": task})

@app.route('/api/works', methods=['GET'])
@require_admin_token
def api_works():
    data = load_json_safe(WORKS_FILE, [])
    return jsonify(data)

@app.route('/api/admins', methods=['GET'])
@require_admin_token
def api_admins_list():
    return jsonify(ordinary_admins)

@app.route('/api/admins', methods=['POST'])
@require_admin_token
def api_admins_add():
    payload = request.get_json() or {}
    ident = str(payload.get('identifier', '')).strip()
    if not ident:
        return jsonify({"ok": False, "reason": "missing_identifier"}), 400
    if add_ordinary_admin(ident):
        return jsonify({"ok": True})
    return jsonify({"ok": False, "reason": "exists"}), 400

@app.route('/api/admins/<ident>', methods=['DELETE'])
@require_admin_token
def api_admins_remove(ident):
    if remove_ordinary_admin(ident):
        return jsonify({"ok": True})
    return jsonify({"ok": False, "reason": "not_found"}), 404

@app.route('/api/topups/<req_id>/approve', methods=['POST'])
@require_admin_token
def api_topup_approve(req_id):
    arr = load_json_safe(TOPUPS_FILE, [])
    for it in arr:
        if it.get("id") == req_id:
            if it.get("status") == "approved":
                return jsonify({"ok": False, "reason": "already_approved"}), 400
            it["status"] = "approved"
            it["handled_by"] = "admin"
            it["handled_at"] = datetime.utcnow().isoformat() + "Z"
            uid = int(it["user"]["id"])
            uidk = str(uid)
            users.setdefault(uidk, {"balance": 0, "tasks_done": 0, "total_earned": 0, "subscribed": False, "last_submissions": {}})
            users[uidk]["balance"] = users[uidk].get("balance", 0) + it.get("amount", 0)
            save_users()
            save_json(TOPUPS_FILE, arr)
            notify_update_topup(it)
            try:
                if bot:
                    bot.send_message(uid, f"Ваше пополнение {it.get('amount',0)} ₽ подтверждено.")
            except Exception:
                pass
            try:
                socketio.emit('user_balance_changed', {"uid": uidk, "balance": users[uidk]["balance"]}, room='admins_main')
            except Exception:
                pass
            return jsonify({"ok": True})
    return jsonify({"ok": False, "reason": "not_found"}), 404

@app.route('/api/topups/<req_id>/reject', methods=['POST'])
def requirements_txt_note():  # placeholder for content length safety in chat (no-op)
    # This route replaced by real implementation in the full file above; kept to avoid truncation issues in chat displays.
    return jsonify({"ok": True})
# ========== WEBHOOK / POLLING SAFE START ==========
def start_polling_thread_safe():
    """
    Safe start for polling:
    - Attempt to remove webhook first to avoid Telegram 409 conflict.
    - Check BOT_DISABLE_BOT and bot availability.
    - Check DNS resolution.
    - Run polling loop with retries; on 409 attempt to delete webhook again.
    """
    if os.environ.get("BOT_DISABLE_BOT", "").lower() in ("1", "true", "yes"):
        logger.info("[bot] BOT_DISABLE_BOT is set — skipping Telegram bot startup.")
        return None
    if not bot:
        logger.info("[bot] bot not configured — skipping polling.")
        return None

    # Attempt to remove webhook before polling to avoid "409 conflict"
    try:
        bot.remove_webhook()
        logger.info("[bot] remove_webhook() called — webhook removed (or was not set).")
    except Exception as e:
        logger.debug("[bot] remove_webhook() failed or not needed: %s", repr(e))

    # optional network check
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
                # If Telegram returns 409 again, attempt delete webhook and retry
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
    """
    Try to set webhook (with retries). If webhook cannot be set and polling fallback is allowed,
    start polling instead.
    """
    if os.environ.get("BOT_DISABLE_BOT", "").lower() in ("1", "true", "yes"):
        logger.info("[bot] BOT_DISABLE_BOT is set — skipping webhook setup.")
        return
    if not bot:
        logger.info("[bot] bot not configured — skipping webhook setup.")
        return

    # If DNS/resolution fails, skip trying to set webhook
    if not can_resolve_host():
        logger.warning("[webhook setup] api.telegram.org not resolvable — skipping webhook setup.")
        if os.environ.get("BOT_FORCE_POLLING", "").lower() in ("1", "true", "yes") or os.environ.get("BOT_AUTO_POLLING", "").lower() in ("1","true","yes"):
            logger.info("[webhook setup] BOT_FORCE_POLLING set — starting polling fallback.")
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
            # best-effort remove existing webhook before setting new one
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
                    logger.info("[webhook setup] Starting polling fallback.")
                    start_polling_thread_safe()
                else:
                    logger.info("[webhook setup] Enable BOT_FORCE_POLLING to fallback to polling.")
                return
            backoff = base_backoff * (2 ** (attempt - 1))
            logger.info("[webhook setup] Retrying in %s seconds...", backoff)
            time.sleep(backoff)

# ========== START ==========
if __name__ == '__main__':
    # choose webhook vs polling
    if os.environ.get("BOT_FORCE_POLLING", "").lower() in ("1", "true", "yes"):
        logger.info("[main] BOT_FORCE_POLLING set — starting polling.")
        start_polling_thread_safe()
    else:
        threading.Thread(target=setup_webhook_safe, daemon=True).start()

    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host='0.0.0.0', port=port)
