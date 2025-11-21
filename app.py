#!/usr/bin/env python3
# coding: utf-8
"""
ReviewCash — full single app.py (improved Telegram notification resilience + UI-ready)
- Provides Flask + Flask-SocketIO + telebot handlers + persistent JSON storage
- Replaces many direct bot.send_message calls with a background HTTP sender queue that
  uses requests with timeouts/retries to avoid blocking and connect-timeout crashes
- Keeps telebot for receiving updates and WebApp interactions, but outbound admin/user
  notifications are delivered via the robust queue (so network issues won't raise in handlers)
- Maintains persistent admin JWT tokens (admin_tokens.json) to avoid regenerating tokens each /mainadmin
- All endpoints: tasks/topup/withdraw/support/profile_me/user_history/admin endpoints remain present
"""
import os
import time
import json
import threading
import logging
import hmac
import hashlib
import random
import string
import queue
from datetime import datetime, timedelta
from urllib.parse import quote_plus, parse_qsl

import eventlet
try:
    eventlet.monkey_patch(dns=False)
except TypeError:
    eventlet.monkey_patch()

from flask import Flask, request, jsonify, send_from_directory, abort
from flask_socketio import SocketIO, join_room

# Optional libs
try:
    import telebot
    from telebot import types as tb_types
except Exception:
    telebot = None
    tb_types = None

try:
    import jwt
except Exception:
    jwt = None

# HTTP requests for robust outbound Telegram calls
try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except Exception:
    requests = None

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("reviewcash")

# ---------- Config ----------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8033069276:AAFv1-kdQ68LjvLEgLHj3ZXd5ehMqyUXOYU").strip()
WEBAPP_URL = os.environ.get("WEBAPP_URL", "https://web-production-398fb.up.railway.app/").rstrip("/") or ""
CHANNEL_ID = os.environ.get("CHANNEL_ID", "@ReviewCashNews").strip()
ADMIN_USER_IDS = [s.strip() for s in os.environ.get("ADMIN_USER_IDS", "6482440657").split(",") if s.strip()]
ADMIN_USERNAMES = [s.strip() for s in os.environ.get("ADMIN_USERNAMES", "").split(",") if s.strip()]
ADMIN_JWT_SECRET = os.environ.get("ADMIN_JWT_SECRET", "replace_with_strong_secret")
ADMIN_TOKEN_TTL_SECONDS = int(os.environ.get("ADMIN_TOKEN_TTL_SECONDS", "86400"))

DATA_DIR = os.environ.get("DATA_DIR", ".rc_data")
os.makedirs(DATA_DIR, exist_ok=True)
TOPUPS_FILE = os.path.join(DATA_DIR, "topups.json")
WITHDRAWS_FILE = os.path.join(DATA_DIR, "withdraws.json")
TASKS_FILE = os.path.join(DATA_DIR, "tasks.json")
WORKS_FILE = os.path.join(DATA_DIR, "works.json")
USERS_FILE = os.path.join(DATA_DIR, "users.json")
ADMINS_FILE = os.path.join(DATA_DIR, "admins.json")
SUPPORT_FILE = os.path.join(DATA_DIR, "support.json")
ADMIN_TOKENS_FILE = os.path.join(DATA_DIR, "admin_tokens.json")

# Outbound telegram HTTP sender config
TELEGRAM_HTTP_TIMEOUT = float(os.environ.get("TELEGRAM_HTTP_TIMEOUT", "6"))  # seconds per request
TELEGRAM_HTTP_RETRIES = int(os.environ.get("TELEGRAM_HTTP_RETRIES", "3"))
TELEGRAM_HTTP_BACKOFF = float(os.environ.get("TELEGRAM_HTTP_BACKOFF", "1.4"))  # multiplier

# ---------- Storage helpers ----------
def load_json_safe(path, default):
    try:
        if not os.path.exists(path):
            return default
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning("load_json_safe(%s) failed: %s", path, e)
        return default

def save_json(path, obj):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error("save_json(%s) failed: %s", path, e)

def append_json(path, obj):
    arr = load_json_safe(path, [])
    arr.append(obj)
    save_json(path, arr)

# ---------- Flask & SocketIO ----------
app = Flask(__name__, static_folder='public', static_url_path='/')
try:
    from flask_cors import CORS
    CORS(app)
    logger.info("Flask-Cors enabled")
except Exception:
    pass

REDIS_URL = os.environ.get("REDIS_URL", None)
if REDIS_URL:
    socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet", message_queue=REDIS_URL)
else:
    socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

# persisted
users = load_json_safe(USERS_FILE, {})
ordinary_admins = load_json_safe(ADMINS_FILE, [])
admin_tokens = load_json_safe(ADMIN_TOKENS_FILE, [])

# ---------- Utilities ----------
def save_users(): save_json(USERS_FILE, users)
def save_ordinary_admins(): save_json(ADMINS_FILE, ordinary_admins)
def persist_admin_tokens(): save_json(ADMIN_TOKENS_FILE, admin_tokens)

def get_user_record(uid):
    key = str(uid)
    if key not in users:
        users[key] = {"balance": 0.0, "tasks_done": 0, "total_earned": 0.0, "subscribed": False}
        save_users()
    return users[key]

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

# ---------- JWT admin token helpers ----------
def generate_admin_token_payload(uid, username, ttl_seconds=None):
    if ttl_seconds is None: ttl_seconds = ADMIN_TOKEN_TTL_SECONDS
    payload = {
        "uid": str(uid) if uid is not None else "",
        "username": username or "",
        "exp": datetime.utcnow() + timedelta(seconds=ttl_seconds),
        "iat": datetime.utcnow()
    }
    return payload

def create_jwt(payload):
    if jwt is None:
        raise RuntimeError("PyJWT not installed")
    token = jwt.encode(payload, ADMIN_JWT_SECRET, algorithm="HS256")
    if isinstance(token, bytes): token = token.decode('utf-8')
    return token

def verify_admin_token(token):
    if jwt is None: return False, None
    try:
        payload = jwt.decode(token, ADMIN_JWT_SECRET, algorithms=["HS256"])
        uid = str(payload.get("uid","")) or ""
        username = (payload.get("username") or "").strip()
        if uid and (uid in ADMIN_USER_IDS or uid in ordinary_admins):
            return True, payload
        if username and (username in ADMIN_USERNAMES or username in ordinary_admins):
            return True, payload
        return False, None
    except jwt.ExpiredSignatureError:
        return False, "expired"
    except Exception as e:
        logger.debug("verify_admin_token error: %s", e)
        return False, None

def generate_or_get_admin_token(uid, username):
    now_ts = int(time.time())
    uid_s = str(uid) if uid is not None else ""
    username_s = (username or "").strip()
    changed = False
    new_list = []
    for rec in admin_tokens:
        if rec.get("exp_ts", 0) > now_ts:
            new_list.append(rec)
        else:
            changed = True
    if changed:
        admin_tokens[:] = new_list
        persist_admin_tokens()
    for rec in admin_tokens:
        if rec.get("uid") == uid_s or rec.get("username") == username_s:
            return rec.get("token")
    payload = generate_admin_token_payload(uid_s or username_s, username_s)
    token = create_jwt(payload)
    exp_ts = int((payload["exp"] - datetime(1970,1,1)).total_seconds())
    admin_tokens.append({"uid": uid_s, "username": username_s, "token": token, "exp_ts": exp_ts})
    persist_admin_tokens()
    return token

# ---------- Robust outbound Telegram HTTP sender (background queue) ----------
telegram_queue = queue.Queue()

# prepare requests.Session with retries for stability
_http_session = None
def _get_http_session():
    global _http_session
    if _http_session is None:
        sess = requests.Session() if requests else None
        if sess and requests:
            retries = Retry(total=3, backoff_factor=0.6, status_forcelist=(429,500,502,503,504))
            adapter = HTTPAdapter(max_retries=retries)
            sess.mount("https://", adapter)
            sess.mount("http://", adapter)
        _http_session = sess
    return _http_session

def enqueue_telegram_message(chat_identifier, text, parse_mode="HTML"):
    """
    chat_identifier: numeric id or "@username"
    text: message text
    This simply pushes into queue to be delivered asynchronously with timeouts and retries.
    """
    telegram_queue.put({"chat": chat_identifier, "text": text, "parse_mode": parse_mode})

def _telegram_worker_loop():
    session = _get_http_session()
    while True:
        try:
            item = telegram_queue.get()
            if item is None:
                break
            chat = item.get("chat")
            text = item.get("text")
            parse_mode = item.get("parse_mode", "HTML")
            # perform HTTP POST to Telegram API (sendMessage)
            if not BOT_TOKEN or requests is None:
                logger.debug("Skipping outbound TB message (no BOT_TOKEN or requests): %s", text[:120])
                continue
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
            payload = {"chat_id": chat, "text": text, "parse_mode": parse_mode}
            # try attempts with backoff
            attempt = 0
            backoff = TELEGRAM_HTTP_BACKOFF
            while attempt < TELEGRAM_HTTP_RETRIES:
                try:
                    resp = session.post(url, data=payload, timeout=TELEGRAM_HTTP_TIMEOUT)
                    if resp.status_code == 200:
                        logger.debug("Telegram message sent to %s", chat)
                        break
                    else:
                        logger.warning("Telegram sendMessage returned %s: %s", resp.status_code, resp.text[:200])
                except Exception as e:
                    logger.warning("Telegram HTTP send exception (attempt %s) to %s: %s", attempt+1, chat, e)
                attempt += 1
                time.sleep(backoff * attempt)
        except Exception as e:
            logger.exception("telegram worker loop error: %s", e)
        finally:
            try:
                telegram_queue.task_done()
            except Exception:
                pass

# start worker thread
if requests:
    _telegram_thread = threading.Thread(target=_telegram_worker_loop, daemon=True)
    _telegram_thread.start()
else:
    logger.warning("requests not available: outbound telegram HTTP sending disabled")

# ---------- Telegram init_data verification ----------
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

# ---------- Bot init & handlers ----------
if BOT_TOKEN and telebot:
    bot = telebot.TeleBot(BOT_TOKEN)
    logger.info("Telebot configured")
else:
    bot = None
    if not BOT_TOKEN:
        logger.warning("BOT_TOKEN not set — Telegram features disabled")
    else:
        logger.warning("pytelegrambotapi not installed — Telegram features disabled")

if bot:
    @bot.message_handler(commands=['start'])
    def handle_start(message):
        try:
            uid = message.from_user.id
            first = (message.from_user.first_name or "").strip()
            text = f"Привет{(' ' + first) if first else ''}! Добро пожаловать в ReviewCash.\n\n" \
                   "Пожалуйста подпишитесь на канал и нажмите «Проверить подписку»."
            kb = tb_types.InlineKeyboardMarkup()
            if CHANNEL_ID:
                kb.add(tb_types.InlineKeyboardButton(text="Перейти в канал", url=f"https://t.me/{CHANNEL_ID.lstrip('@')}"))
                kb.add(tb_types.InlineKeyboardButton(text="Проверить подписку", callback_data="check_sub"))
            if WEBAPP_URL:
                kb.add(tb_types.InlineKeyboardButton(text="Открыть WebApp", url=WEBAPP_URL))
            bot.send_message(uid, text, reply_markup=kb)
        except Exception as e:
            logger.exception("start handler error: %s", e)

    @bot.callback_query_handler(func=lambda call: call.data == 'check_sub')
    def cb_check_sub(call):
        try:
            uid = call.from_user.id
            subscribed = False
            if CHANNEL_ID:
                try:
                    mem = bot.get_chat_member(CHANNEL_ID, uid)
                    status = getattr(mem, 'status', '') or ''
                    if status not in ('left', 'kicked', 'restricted', ''):
                        subscribed = True
                except Exception:
                    subscribed = False
            if subscribed:
                rec = get_user_record(uid)
                rec['subscribed'] = True
                save_users()
                bot.answer_callback_query(call.id, "Подписка подтверждена")
                # use queue for outbound send (avoid blocking)
                enqueue_telegram_message(uid, "Спасибо! Подписка подтверждена.")
            else:
                bot.answer_callback_query(call.id, "Подписка не найдена")
                enqueue_telegram_message(uid, f"Пожалуйста подпишитесь на {CHANNEL_ID} и повторите проверку.")
        except Exception as e:
            logger.exception("callback check_sub error: %s", e)

    @bot.message_handler(commands=['mainadmin','admin','addadmin'])
    def handle_admin_cmds(message):
        try:
            cmd_text = (message.text or "").strip()
            parts = cmd_text.split()
            cmd = parts[0].lower()
            uid = message.from_user.id
            uname = getattr(message.from_user, "username", None)
            if not is_main_admin(uid) and not is_main_admin(uname):
                enqueue_telegram_message(uid, "У вас нет прав выполнять эту команду.")
                return
            if cmd == '/mainadmin':
                token_admin = generate_or_get_admin_token(uid, uname)
                admin_url = f"{WEBAPP_URL.rstrip('/')}/mainadmin?token={quote_plus(token_admin)}"
                try:
                    kb = tb_types.InlineKeyboardMarkup()
                    webinfo = tb_types.WebAppInfo(url=admin_url)
                    kb.add(tb_types.InlineKeyboardButton(text="Открыть админку (WebApp)", web_app=webinfo))
                    kb.add(tb_types.InlineKeyboardButton(text="Открыть в браузере", url=admin_url))
                    bot.send_message(uid, "Откройте админку:", reply_markup=kb)
                except Exception:
                    # fallback: queue plain link
                    enqueue_telegram_message(uid, f"Ссылка на админку: {admin_url}")
                return
            if cmd == '/admin':
                topups = load_json_safe(TOPUPS_FILE, [])
                withdraws = load_json_safe(WITHDRAWS_FILE, [])
                works = load_json_safe(WORKS_FILE, [])
                supports = load_json_safe(SUPPORT_FILE, [])
                users_map = load_json_safe(USERS_FILE, {})
                out = ("Статистика системы:\n"
                       f"Пользователей: {len(users_map)}\n"
                       f"Пополнений: {len(topups)}\n"
                       f"Выводов: {len(withdraws)}\n"
                       f"Работ: {len(works)}\n"
                       f"Support: {len(supports)}\n")
                enqueue_telegram_message(uid, out)
                return
            if cmd == '/addadmin':
                if len(parts) < 2:
                    enqueue_telegram_message(uid, "Использование: /addadmin <uid_or_username>")
                    return
                target = parts[1].strip()
                if target.startswith('@'): target = target[1:]
                added = add_ordinary_admin(target)
                if added:
                    enqueue_telegram_message(uid, f"{target} добавлен как ordinary admin.")
                    notify_event('admins_updated', {"action":"add","who":target}, rooms=['admins_main','admins_ordinary'])
                else:
                    enqueue_telegram_message(uid, f"{target} уже в списке админов.")
                return
        except Exception as e:
            logger.exception("admin cmd error: %s", e)

# ---------- SocketIO connect ----------
@socketio.on('connect')
def _on_connect(auth):
    try:
        token = None
        if isinstance(auth, dict):
            token = auth.get('token')
        if not token:
            return
        ok, payload = verify_admin_token(token)
        if not ok:
            return False
        uid = str(payload.get('uid') or '')
        username = str(payload.get('username') or '')
        if uid in ADMIN_USER_IDS or username in ADMIN_USERNAMES:
            join_room('admins_main')
        if uid and uid in ordinary_admins:
            join_room('admins_ordinary'); join_room(f'user:{uid}')
    except Exception as e:
        logger.exception("socket connect error: %s", e)
        return False

# ---------- Notifications helpers ----------
def notify_event(name, payload, rooms=None):
    try:
        if rooms:
            for r in rooms:
                socketio.emit(name, payload, room=r)
        else:
            socketio.emit(name, payload)
    except Exception as e:
        logger.debug("notify_event error: %s", e)

def notify_new_topup(t):
    notify_event('new_topup', t, rooms=['admins_main','admins_ordinary'])
    uid = t.get('user',{}).get('id')
    if uid:
        notify_event('new_topup_user', t, rooms=[f'user:{uid}'])
    # enqueue admin notifications (non-blocking)
    try:
        msg = f"Новый топап: {t.get('amount')} ₽, код {t.get('code','')}, id {t.get('id')}"
        for a in ordinary_admins + ADMIN_USER_IDS:
            enqueue_telegram_message(a, msg)
    except Exception:
        pass

def notify_new_withdraw(w):
    notify_event('new_withdraw', w, rooms=['admins_main','admins_ordinary'])
    uid = w.get('user',{}).get('id')
    if uid:
        notify_event('new_withdraw_user', w, rooms=[f'user:{uid}'])
    try:
        msg = f"Новый вывод: {w.get('amount')} ₽, user {w.get('user',{}).get('id')}"
        for a in ordinary_admins + ADMIN_USER_IDS:
            enqueue_telegram_message(a, msg)
    except Exception:
        pass

def notify_new_support(s):
    notify_event('new_support', s, rooms=['admins_main','admins_ordinary'])
    try:
        msg = f"Новый запрос в поддержку: {s.get('message','')[:300]}"
        for a in ordinary_admins + ADMIN_USER_IDS:
            enqueue_telegram_message(a, msg)
    except Exception:
        pass

# ---------- Routes & API ----------
@app.route('/')
def index(): return send_from_directory('public', 'index.html')

@app.route('/<path:path>')
def static_files(path): return send_from_directory('public', path)

@app.route('/health')
def health(): return jsonify({"ok": True, "ts": datetime.utcnow().isoformat()+"Z"})

def get_token_from_request(req):
    t = req.args.get("token")
    if t: return t
    auth = req.headers.get("Authorization") or req.headers.get("authorization") or ""
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

# Public endpoints (tasks/topup/withdraw/support/profile/history) ...
# For brevity in this file block we include the same functional endpoints as previously described.
# They are implemented below (full versions), using enqueue_telegram_message for notifications
# and the same validations as in earlier versions.

# ----- tasks_public -----
@app.route('/api/tasks_public', methods=['GET'])
def api_tasks_public():
    tasks = load_json_safe(TASKS_FILE, [])
    active = [t for t in tasks if t.get("status","active") == "active"]
    return jsonify(active)

# ----- topups_public -----
@app.route('/api/topups_public', methods=['POST'])
def api_topups_public():
    payload = request.get_json() or {}
    try: amount = float(payload.get('amount') or 0)
    except Exception: amount = 0
    if amount < 100: return jsonify({"ok": False, "reason": "min_topup_100"}), 400
    init_data = request.headers.get('X-Tg-InitData') or request.args.get('init_data')
    user = {}
    if init_data:
        ok, params = verify_telegram_init_data(init_data)
        if ok:
            uid = params.get('id') or params.get('user_id')
            user = {"id": uid, "params": params}
    code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
    rec = {"id": f"top_{int(time.time()*1000)}", "user": user, "amount": amount, "code": code, "status": "pending", "created_at": datetime.utcnow().isoformat()+"Z"}
    append_json(TOPUPS_FILE, rec)
    notify_new_topup(rec)
    bank_info = {"bank_name":"Тинькофф (Раяз Н.)","phone":"+79600738559","note":"Укажите код в комментарии"}
    return jsonify({"ok": True, "topup": rec, "bank": bank_info})

# ----- withdraw_public -----
@app.route('/api/withdraw_public', methods=['POST'])
def api_withdraw_public():
    payload = request.get_json() or {}
    try: amount = float(payload.get('amount') or 0)
    except Exception: amount = 0
    name = (payload.get('name') or "").strip()
    bank = (payload.get('bank') or "").strip()
    if amount <= 0 or not name or not bank: return jsonify({"ok": False, "reason": "bad_params"}), 400
    if amount < 250: return jsonify({"ok": False, "reason": "min_withdraw_250"}), 400
    init_data = request.headers.get('X-Tg-InitData') or request.args.get('init_data')
    user_id = None
    if init_data:
        ok, params = verify_telegram_init_data(init_data)
        if ok:
            user_id = params.get('id') or params.get('user_id')
    if not user_id: return jsonify({"ok": False, "reason": "init_data_required"}), 401
    rec_user = get_user_record(user_id)
    bal = float(rec_user.get('balance', 0.0))
    if bal < amount: return jsonify({"ok": False, "reason": "insufficient_balance", "balance": bal}), 400
    rec_user['balance'] = round(bal - amount, 2); save_users()
    rec = {"id": f"wd_{int(time.time()*1000)}", "user": {"id": user_id}, "amount": amount, "name": name, "bank": bank, "status": "pending", "created_at": datetime.utcnow().isoformat()+"Z"}
    append_json(WITHDRAWS_FILE, rec)
    notify_new_withdraw(rec)
    return jsonify({"ok": True, "withdraw": rec, "balance_after": rec_user['balance']})

# ----- support endpoints -----
@app.route('/api/support', methods=['POST'])
def api_support_create():
    payload = request.get_json() or {}
    message_text = (payload.get('message') or "").strip()
    contact = (payload.get('contact') or "").strip()
    if not message_text: return jsonify({"ok": False, "reason": "message_required"}), 400
    init_data = request.headers.get('X-Tg-InitData') or request.args.get('init_data')
    user_obj = {}
    if init_data:
        ok, params = verify_telegram_init_data(init_data)
        if ok:
            uid = params.get('id') or params.get('user_id')
            user_obj = {"id": uid, "params": params}
    rec = {"id": f"sup_{int(time.time()*1000)}", "user": user_obj, "message": message_text, "contact": contact, "created_at": datetime.utcnow().isoformat()+"Z", "status": "new", "replies": []}
    append_json(SUPPORT_FILE, rec)
    notify_new_support(rec)
    return jsonify({"ok": True, "support": rec})

@app.route('/api/supports', methods=['GET'])
@require_admin_token
def api_supports_list():
    status = (request.args.get('status') or "").strip().lower()
    arr = load_json_safe(SUPPORT_FILE, [])
    if status:
        arr = [s for s in arr if (s.get('status') or '').lower() == status]
    return jsonify(arr)

@app.route('/api/supports/<sid>/reply', methods=['POST'])
@require_admin_token
def api_support_reply(sid):
    data = request.get_json() or {}
    message = (data.get('message') or "").strip()
    if not message: return jsonify({"ok": False, "reason": "message_required"}), 400
    token = get_token_from_request(request)
    ok, payload = verify_admin_token(token)
    admin_ident = {}
    if ok and isinstance(payload, dict):
        admin_ident = {"uid": payload.get("uid"), "username": payload.get("username")}
    arr = load_json_safe(SUPPORT_FILE, [])
    found = next((s for s in arr if s.get('id')==sid), None)
    if not found: return jsonify({"ok": False, "reason": "not_found"}), 404
    reply = {"message": message, "admin": admin_ident, "created_at": datetime.utcnow().isoformat()+"Z"}
    found.setdefault('replies', []).append(reply)
    found['status'] = 'replied'
    found.setdefault('handled_by', admin_ident)
    found['handled_at'] = datetime.utcnow().isoformat()+"Z"
    save_json(SUPPORT_FILE, arr)
    notify_event('update_support', found, rooms=['admins_main','admins_ordinary'])
    uid = found.get('user',{}).get('id')
    if uid: enqueue_telegram_message(uid, f"Ответ поддержки: {message}")
    return jsonify({"ok": True, "support": found})

@app.route('/api/supports/<sid>/resolve', methods=['POST'])
@require_admin_token
def api_support_resolve(sid):
    data = request.get_json() or {}
    reason = (data.get('reason') or "").strip()
    token = get_token_from_request(request)
    ok, payload = verify_admin_token(token)
    admin_ident = {}
    if ok and isinstance(payload, dict):
        admin_ident = {"uid": payload.get("uid"), "username": payload.get("username")}
    arr = load_json_safe(SUPPORT_FILE, [])
    found = next((s for s in arr if s.get('id')==sid), None)
    if not found: return jsonify({"ok": False, "reason": "not_found"}), 404
    found['status'] = 'resolved'
    found.setdefault('closed_by', admin_ident)
    found['closed_at'] = datetime.utcnow().isoformat()+"Z"
    if reason: found.setdefault('close_reason', reason)
    save_json(SUPPORT_FILE, arr)
    notify_event('update_support', found, rooms=['admins_main','admins_ordinary'])
    uid = found.get('user',{}).get('id')
    if uid: enqueue_telegram_message(uid, f"Ваш запрос закрыт. {reason or ''}")
    return jsonify({"ok": True, "support": found})

# ----- admins list & stats -----
@app.route('/api/admins', methods=['GET'])
@require_admin_token
def api_admins_list():
    admins_out = []
    seen = set()
    for a in ADMIN_USER_IDS:
        if a in seen: continue
        seen.add(a); admins_out.append({"id_or_username": a, "is_main": True})
    for a in ADMIN_USERNAMES:
        if a in seen: continue
        seen.add(a); admins_out.append({"id_or_username": a, "is_main": True})
    for a in ordinary_admins:
        if a in seen: continue
        seen.add(a); admins_out.append({"id_or_username": a, "is_main": False})
    supports = load_json_safe(SUPPORT_FILE, [])
    count_map = {adm["id_or_username"]: 0 for adm in admins_out}
    def norm(k): return str(k) if k is not None else ""
    for s in supports:
        hb = s.get('handled_by') or {}
        if isinstance(hb, dict):
            cand = norm(hb.get('uid') or hb.get('username') or '')
            if cand in count_map: count_map[cand] = count_map.get(cand,0)+1
        for r in s.get('replies', []) or []:
            adm = r.get('admin') or {}
            cand = norm(adm.get('uid') or adm.get('username') or '')
            if cand in count_map: count_map[cand] = count_map.get(cand,0)+1
    for adm in admins_out:
        adm['supports_handled'] = count_map.get(adm['id_or_username'], 0)
    return jsonify({"ok": True, "admins": admins_out})

# ----- profile_me -----
@app.route('/api/profile_me', methods=['GET'])
def api_profile_me():
    init_data = request.headers.get('X-Tg-InitData') or request.args.get('init_data')
    if not init_data: return jsonify({"ok": False, "reason": "init_data_required"}), 401
    ok, params = verify_telegram_init_data(init_data)
    if not ok: return jsonify({"ok": False, "reason": "invalid_init_data"}), 403
    uid = params.get('id') or params.get('user_id')
    username = params.get('username') or None
    rec = get_user_record(uid)
    resp = {"ok": True, "user": {"id": uid, "username": username, "first_name": params.get('first_name')}, "balance": rec.get('balance', 0.0), "subscribed": rec.get('subscribed', False)}
    if bot:
        try:
            photos = bot.get_user_profile_photos(int(uid))
            if photos and getattr(photos, "total_count", 0) > 0:
                file_id = photos.photos[0][0].file_id
                f = bot.get_file(file_id)
                file_path = getattr(f, "file_path", None)
                if file_path:
                    resp['photo_url'] = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
        except Exception:
            pass
    return jsonify(resp)

# ----- user_history -----
from functools import cmp_to_key
@app.route('/api/user_history', methods=['GET'])
def api_user_history():
    uid = (request.args.get('uid') or "").strip()
    if not uid: return jsonify({"ok": False, "reason": "missing_uid"}), 400
    page = int(request.args.get('page') or 1); page_size = int(request.args.get('page_size') or 20)
    ftype = (request.args.get('type') or "").strip().lower()
    topups = load_json_safe(TOPUPS_FILE, []); withdraws = load_json_safe(WITHDRAWS_FILE, []); works = load_json_safe(WORKS_FILE, [])
    items = []
    def push(arr, typ):
        for it in arr:
            try: user_id = str(it.get('user',{}).get('id') or '')
            except: user_id = ''
            if user_id == str(uid):
                summary = ''
                if typ == 'topup': summary = f"Пополнение {it.get('amount',0)} ₽"
                elif typ == 'withdraw': summary = f"Вывод {it.get('amount',0)} ₽ ({it.get('bank') or ''})"
                elif typ == 'work': summary = it.get('task_title') or it.get('task_id') or ''
                items.append({"id": it.get('id'), "type": typ, "amount": it.get('amount'), "summary": summary, "created_at": it.get('created_at') or it.get('handled_at') or ''})
    if not ftype or ftype == 'topup': push(topups, 'topup')
    if not ftype or ftype == 'withdraw': push(withdraws, 'withdraw')
    if not ftype or ftype == 'work': push(works, 'work')
    def cmp(a,b):
        ta = a.get('created_at') or ''; tb = b.get('created_at') or ''
        if ta > tb: return -1
        if ta < tb: return 1
        return 0
    items.sort(key=cmp_to_key(cmp))
    total = len(items)
    start = (page-1)*page_size; end = start+page_size
    paged = items[start:end]
    return jsonify({"ok": True, "items": paged, "total": total, "page": page, "page_size": page_size})

# ---------- Webhook endpoint ----------
@app.route('/webhook', methods=['POST'])
def webhook():
    if not bot: return "bot not configured", 500
    if request.headers.get('content-type') == 'application/json':
        try:
            update = telebot.types.Update.de_json(request.get_data().decode('utf-8'))
            bot.process_new_updates([update])
            return '', 200
        except Exception as e:
            logger.exception("webhook error: %s", e)
            return 'error', 500
    return 'Invalid', 403

# ---------- Bot startup helpers (avoid 409) ----------
def can_resolve_host(host="api.telegram.org"):
    import socket
    try: socket.getaddrinfo(host, 443); return True
    except Exception as e:
        logger.debug("DNS resolution failed: %s", e)
        return False

def start_polling_thread_safe():
    if not bot:
        logger.info("Bot not configured; skipping polling")
        return None
    try:
        bot.remove_webhook()
        logger.info("remove_webhook() called before polling")
    except Exception:
        pass
    def _loop():
        while True:
            try:
                logger.info("Starting bot polling...")
                bot.infinity_polling(timeout=60, long_polling_timeout=50)
            except Exception as e:
                logger.error("Polling exception: %s", e)
                # try remove webhook and retry after sleep
                try:
                    bot.remove_webhook()
                except Exception:
                    pass
                time.sleep(8)
    t = threading.Thread(target=_loop, daemon=True); t.start(); return t

def setup_webhook_safe():
    if not bot:
        return
    if not WEBAPP_URL:
        logger.info("WEBAPP_URL not set — falling back to polling")
        start_polling_thread_safe()
        return
    if not can_resolve_host():
        logger.warning("api.telegram.org not resolvable — using polling")
        start_polling_thread_safe()
        return
    try:
        bot.remove_webhook()
    except Exception:
        pass
    try:
        bot.set_webhook(url=f"{WEBAPP_URL.rstrip('/')}/webhook")
        logger.info("Webhook set to %s", f"{WEBAPP_URL.rstrip('/')}/webhook")
    except Exception as e:
        logger.warning("set_webhook failed: %s — falling back to polling", e)
        start_polling_thread_safe()

# ---------- Start ----------
if __name__ == '__main__':
    if bot:
        if os.environ.get("BOT_FORCE_POLLING", "").lower() in ("1","true","yes"):
            start_polling_thread_safe()
        else:
            threading.Thread(target=setup_webhook_safe, daemon=True).start()
    port = int(os.environ.get("PORT", "8080"))
    logger.info("Starting server on port %s", port)
    socketio.run(app, host='0.0.0.0', port=port)
