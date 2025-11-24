#!/usr/bin/env python3
# coding: utf-8
"""
ReviewCash — app.py for the provided WebApp frontend
- Serves static UI from ./public
- Implements APIs used by the WebApp:
  /api/profile_me, /api/tasks_public, /api/tasks_create,
  /api/task_types, /api/task_types_add,
  /api/topups_public, /api/topups/<id>, /api/payment_webhook,
  /api/admin/incoming_topups (list), /api/admin/incoming_topups/<id>/mark_paid
  /api/admin/incoming_topups/<id>/force_match, /api/admin/incoming_topups/<id>/refund
  /api/withdraw_public, /api/user_history
- Simple persistence using JSON files in .rc_data
- Websocket events via Flask-SocketIO: new_task, topup_updated
- Telegram bot commands: /start and /mainadmin (uses BOT_TOKEN if provided)
- Admin JWT tokens: generated via /api/admin/token (admin ids configured)
Notes:
- Configure environment variables to override defaults:
  BOT_TOKEN, WEBAPP_URL, CHANNEL_ID, ADMIN_USER_IDS (comma-separated), ADMIN_JWT_SECRET
"""
import os
import time
import json
import hmac
import hashlib
import random
import string
import logging
from datetime import datetime, timedelta
from urllib.parse import quote_plus, urlparse, parse_qsl

from flask import Flask, request, jsonify, send_from_directory, abort
from flask_socketio import SocketIO
try:
    import telebot
    from telebot import types as tb_types
except Exception:
    telebot = None
try:
    import jwt
except Exception:
    jwt = None
try:
    import requests
except Exception:
    requests = None

# ---------- Config ----------
DEFAULT_BOT_TOKEN = "8033069276:AAFv1-kdQ68LjvLEgLHj3ZXd5ehMqyUXOYU"
BOT_TOKEN = os.environ.get("BOT_TOKEN", DEFAULT_BOT_TOKEN).strip()
WEBAPP_URL = os.environ.get("WEBAPP_URL", "https://web-production-398fb.up.railway.app").rstrip('/')
CHANNEL_ID = os.environ.get("CHANNEL_ID", "@ReviewCashNews").strip()
ADMIN_USER_IDS = [s.strip() for s in os.environ.get("ADMIN_USER_IDS", "6482440657").split(",") if s.strip()]
ADMIN_JWT_SECRET = os.environ.get("ADMIN_JWT_SECRET", "replace_with_strong_secret")
DATA_DIR = os.environ.get("DATA_DIR", ".rc_data")
os.makedirs(DATA_DIR, exist_ok=True)

TOPUPS_FILE = os.path.join(DATA_DIR, "topups.json")
USERS_FILE = os.path.join(DATA_DIR, "users.json")
TASKS_FILE = os.path.join(DATA_DIR, "tasks.json")
TASK_TYPES_FILE = os.path.join(DATA_DIR, "task_types.json")
ADMINS_FILE = os.path.join(DATA_DIR, "admins.json")

MIN_TOPUP = int(os.environ.get("MIN_TOPUP", "100"))

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("reviewcash")

# ---------- Helpers ----------
def load_json(path, default):
    try:
        if not os.path.exists(path):
            return default
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning("load_json(%s) failed: %s", path, e)
        return default

def save_json(path, obj):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception("save_json(%s) failed: %s", path, e)

def append_json(path, obj):
    arr = load_json(path, [])
    arr.append(obj)
    save_json(path, arr)

def gen_id(prefix="id"):
    return f"{prefix}_{int(time.time()*1000)}_{random.randint(1000,9999)}"

def gen_manual_code():
    return "RC" + ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))

# Ensure default task types
if not os.path.exists(TASK_TYPES_FILE) or not load_json(TASK_TYPES_FILE, []):
    save_json(TASK_TYPES_FILE, [
        {"id":"ya_review","name":"Отзыв — Я.К.","unit_price":100},
        {"id":"gmaps_review","name":"Отзыв — Google Maps","unit_price":65},
        {"id":"tg_sub","name":"Подписка — Telegram канал","unit_price":10},
    ])

# ---------- App ----------
app = Flask(__name__, static_folder='public', static_url_path='/')
socketio = SocketIO(app, cors_allowed_origins="*")

# Load persisted maps
users = load_json(USERS_FILE, {})
tasks = load_json(TASKS_FILE, [])
topups = load_json(TOPUPS_FILE, [])
admins = load_json(ADMINS_FILE, ADMIN_USER_IDS)

# ---------- Utility for users ----------
def get_user(uid):
    key = str(uid)
    if key not in users:
        users[key] = {"balance": 0.0, "history": [], "tasks_done": 0, "total_earned": 0.0}
        save_json(USERS_FILE, users)
    return users[key]

def credit_user(uid, amount, note="topup"):
    rec = get_user(uid)
    rec['balance'] = round(rec.get('balance',0.0) + float(amount), 2)
    rec.setdefault('history', []).insert(0, {"type": "topup", "amount": amount, "note": note, "created_at": datetime.utcnow().isoformat()+"Z"})
    save_json(USERS_FILE, users)

# ---------- Telegram bot (optional) ----------
if BOT_TOKEN and telebot:
    bot = telebot.TeleBot(BOT_TOKEN)
    logger.info("Telebot configured")
    @bot.message_handler(commands=['start'])
    def _start(m):
        uid = m.from_user.id
        first = (m.from_user.first_name or "").strip()
        text = (
            f"Привет{(' ' + first) if first else ''}! Добро пожаловать в ReviewCash —\n"
            "Зарабатывай на отзывах: выполняй задания, пополняй баланс и выводи заработок.\n\n"
            "Как начать:\n"
            "• Открой WebApp кнопкой ниже\n"
            "• Пополни баланс через QR (вводи код RC в комментарии)\n"
            "• Выполняй задания и получай оплату\n\n"
            "Если вы админ, используйте /mainadmin для доступа к панели."
        )
        kb = tb_types.InlineKeyboardMarkup()
        if WEBAPP_URL:
            kb.add(tb_types.InlineKeyboardButton("Открыть WebApp", url=WEBAPP_URL))
        try:
            bot.send_message(uid, text, reply_markup=kb)
        except Exception:
            logger.exception("bot.send_message failed")
    @bot.message_handler(commands=['mainadmin'])
    def _mainadmin(m):
        uid = str(m.from_user.id)
        is_admin = uid in ADMIN_USER_IDS
        if not is_admin:
            bot.send_message(m.chat.id, "У вас нет прав администратора.")
            return
        # create admin token
        if jwt:
            payload = {"uid": uid, "exp": datetime.utcnow() + timedelta(days=1)}
            token = jwt.encode(payload, ADMIN_JWT_SECRET, algorithm="HS256")
            if isinstance(token, bytes): token = token.decode('utf-8')
            admin_url = f"{WEBAPP_URL.rstrip('/')}/mainadmin?token={quote_plus(token)}"
            kb = tb_types.InlineKeyboardMarkup()
            kb.add(tb_types.InlineKeyboardButton("Открыть админку", url=admin_url))
            bot.send_message(m.chat.id, "Откройте админку:", reply_markup=kb)
        else:
            bot.send_message(m.chat.id, "Admin tokens disabled (PyJWT missing)")
else:
    bot = None
    if BOT_TOKEN:
        logger.warning("pytelegrambotapi not installed; Telegram features disabled")

# ---------- Static routes ----------
@app.route('/')
def index():
    return send_from_directory('public', 'index.html')

@app.route('/mainadmin')
def mainadmin_page():
    return send_from_directory('public', 'mainadmin.html')

@app.route('/<path:path>')
def static_proxy(path):
    return send_from_directory('public', path)

# ---------- API Endpoints ----------

# profile_me expects X-Tg-InitData header or init_data param (Telegram WebApp)
@app.route('/api/profile_me', methods=['GET'])
def api_profile_me():
    init_data = request.headers.get('X-Tg-InitData') or request.args.get('init_data')
    if not init_data:
        return jsonify({"ok": False, "reason": "init_data_required"}), 401
    try:
        # verify telegram init_data
        pairs = parse_qsl(init_data, keep_blank_values=True)
        params = dict(pairs)
        provided_hash = params.pop('hash', None)
        if not provided_hash:
            return jsonify({"ok": False, "reason": "invalid_init_data"}), 403
        data_check_arr = [f"{k}={params[k]}" for k in sorted(params.keys())]
        data_check_string = "\n".join(data_check_arr)
        secret_key = hashlib.sha256(BOT_TOKEN.encode()).digest()
        hmac_hash = hmac.new(secret_key, data_check_string.encode('utf-8'), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(hmac_hash, provided_hash):
            return jsonify({"ok": False, "reason": "hash_mismatch"}), 403
        uid = params.get('id') or params.get('user_id')
        username = params.get('username') or None
        first_name = params.get('first_name') or ""
        rec = get_user(uid)
        out = {"ok": True, "user": {"id": uid, "username": username, "first_name": first_name}, "balance": rec.get('balance', 0.0), "history": rec.get('history', [])}
        # try to get photo URL using bot if available
        if bot:
            try:
                photos = bot.get_user_profile_photos(int(uid))
                if photos and getattr(photos, "total_count", 0) > 0:
                    file_id = photos.photos[0][0].file_id
                    f = bot.get_file(file_id)
                    file_path = getattr(f, "file_path", None)
                    if file_path:
                        out['photo_url'] = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
            except Exception:
                pass
        return jsonify(out)
    except Exception as e:
        logger.exception("profile_me error: %s", e)
        return jsonify({"ok": False, "reason": "server_error"}), 500

# list public tasks
@app.route('/api/tasks_public', methods=['GET'])
def api_tasks_public():
    tasks_local = load_json(TASKS_FILE, [])
    active = [t for t in tasks_local if t.get('status', 'active') == 'active']
    return jsonify(active)

# create task (admin required)
def get_token_from_request(req):
    t = req.args.get("token")
    if t:
        return t
    auth = req.headers.get("Authorization") or req.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth.split(" ",1)[1].strip()
    return None

def require_admin(func):
    def wrapper(*args, **kwargs):
        token = get_token_from_request(request)
        if not token:
            return abort(401)
        if jwt is None:
            return abort(403)
        try:
            payload = jwt.decode(token, ADMIN_JWT_SECRET, algorithms=["HS256"])
            uid = str(payload.get("uid",""))
            if uid not in ADMIN_USER_IDS:
                return abort(403)
        except Exception:
            return abort(403)
        return func(*args, **kwargs)
    wrapper.__name__ = func.__name__
    return wrapper

@app.route('/api/tasks_create', methods=['POST'])
@require_admin
def api_tasks_create():
    data = request.get_json() or {}
    title = (data.get('title') or "").strip()
    description = (data.get('description') or "").strip()
    type_id = (data.get('type_id') or "").strip()
    try:
        count = int(data.get('count') or 1)
    except Exception:
        count = 1
    # find unit price from types
    types = load_json(TASK_TYPES_FILE, [])
    unit_price = None
    for t in types:
        if t.get('id') == type_id:
            unit_price = int(t.get('unit_price') or 0)
            break
    if unit_price is None:
        unit_price = int(data.get('unit_price') or 0)
    if not title or unit_price <= 0 or count <= 0:
        return jsonify({"ok": False, "reason": "bad_params"}), 400
    task = {
        "id": gen_id("task"),
        "title": title,
        "description": description,
        "type_id": type_id,
        "unit_price": unit_price,
        "count": count,
        "done": 0,
        "budget": unit_price * count,
        "status": "active",
        "created_at": datetime.utcnow().isoformat()+"Z",
        "workers": []
    }
    tasks_local = load_json(TASKS_FILE, [])
    tasks_local.insert(0, task)
    save_json(TASKS_FILE, tasks_local)
    socketio.emit('new_task', task)
    return jsonify({"ok": True, "task": task})

# Task types endpoints
@app.route('/api/task_types', methods=['GET'])
def api_task_types():
    return jsonify(load_json(TASK_TYPES_FILE, []))

@app.route('/api/task_types_add', methods=['POST'])
@require_admin
def api_task_types_add():
    data = request.get_json() or {}
    tid = (data.get('id') or "").strip()
    name = (data.get('name') or "").strip()
    try:
        price = int(data.get('unit_price') or 0)
    except Exception:
        price = 0
    if not tid or not name or price <= 0:
        return jsonify({"ok": False, "reason": "bad_params"}), 400
    types = load_json(TASK_TYPES_FILE, [])
    for t in types:
        if t.get('id') == tid:
            return jsonify({"ok": False, "reason": "id_exists"}), 400
    types.append({"id": tid, "name": name, "unit_price": price})
    save_json(TASK_TYPES_FILE, types)
    socketio.emit('task_types_updated', {"id": tid, "name": name, "unit_price": price})
    return jsonify({"ok": True, "type": {"id": tid, "name": name, "unit_price": price}})

# Topup creation (public)
@app.route('/api/topups_public', methods=['POST'])
def api_topups_public():
    data = request.get_json() or {}
    try:
        amount = float(data.get('amount') or 0)
    except Exception:
        amount = 0
    if amount < MIN_TOPUP:
        return jsonify({"ok": False, "reason": "min_topup", "min": MIN_TOPUP}), 400
    init_data = request.headers.get('X-Tg-InitData') or request.args.get('init_data')
    user = {}
    if init_data:
        try:
            pairs = parse_qsl(init_data, keep_blank_values=True)
            params = dict(pairs)
            uid = params.get('id') or params.get('user_id')
            if uid:
                user = {"id": uid}
        except Exception:
            pass
    topup_id = gen_id("top")
    code = gen_manual_code()
    pay = {"qr": f"https://api.qrserver.com/v1/create-qr-code/?size=260x260&data={quote_plus(f'SBP_PAY://pay?amount={int(amount)}&comment={code}')}", "url": ""}
    rec = {"id": topup_id, "user": user, "amount": amount, "status": "pending", "created_at": datetime.utcnow().isoformat()+"Z", "manual_code": code, "payment": pay}
    append_json(TOPUPS_FILE, rec)
    socketio.emit('new_topup', rec)
    return jsonify({"ok": True, "topup": rec, "manual_code": code})

@app.route('/api/topups/<tid>', methods=['GET'])
def api_get_topup(tid):
    arr = load_json(TOPUPS_FILE, [])
    found = next((t for t in arr if t.get('id') == tid), None)
    if not found:
        return jsonify({"ok": False, "reason": "not_found"}), 404
    return jsonify({"ok": True, "topup": found})

# Webhook for payments (provider will POST)
@app.route('/api/payment_webhook', methods=['POST'])
def api_payment_webhook():
    # If PAYMENT_WEBHOOK_SECRET configured, verify HMAC-SHA256 hex in header X-Signature
    secret = os.environ.get("PAYMENT_WEBHOOK_SECRET", "")
    if secret and request.data:
        sig = request.headers.get("X-Signature") or request.headers.get("X-Hub-Signature") or ""
        mac = hmac.new(secret.encode(), request.data, digestmod=hashlib.sha256).hexdigest()
        if not sig or not hmac.compare_digest(mac, sig):
            logger.warning("Invalid webhook signature")
            return "invalid signature", 403
    payload = request.get_json() or {}
    order_id = payload.get('orderId') or payload.get('order_id') or payload.get('merchantOrderId')
    status = payload.get('status') or payload.get('payment_status')
    comment = payload.get('comment') or payload.get('payment_comment') or ""
    amount = None
    if payload.get('amount') is not None:
        try:
            amount = float(payload.get('amount'))
        except Exception:
            amount = None
    paid = False
    if isinstance(status, str) and status.lower() in ("paid","success","confirmed","complete","ok"):
        paid = True
    if isinstance(status, bool) and status is True:
        paid = True
    tups = load_json(TOPUPS_FILE, [])
    updated = False
    # try match by order_id
    if order_id:
        for t in tups:
            if t.get('id') == str(order_id):
                if paid and t.get('status') != 'paid':
                    t['status'] = 'paid'
                    t['paid_at'] = datetime.utcnow().isoformat()+"Z"
                    if comment:
                        t.setdefault('payment', {})['comment'] = comment
                        t.setdefault('payment', {})['manual_code_verified'] = (t.get('manual_code') in comment)
                    uid = t.get('user', {}).get('id')
                    if uid:
                        credit_user(uid, t.get('amount',0.0), note='topup_webhook')
                    updated = True
                break
    # else match by manual_code in comment + amount
    if not updated and comment:
        for t in tups:
            if t.get('status') != 'pending':
                continue
            code = t.get('manual_code')
            if code and code in comment:
                if amount is not None and abs(float(t.get('amount',0)) - float(amount)) > 0.01:
                    continue
                t['status'] = 'paid'
                t['paid_at'] = datetime.utcnow().isoformat()+"Z"
                t.setdefault('payment', {})['comment'] = comment
                t.setdefault('payment', {})['manual_code_verified'] = True
                uid = t.get('user',{}).get('id')
                if uid:
                    credit_user(uid, t.get('amount',0.0), note='topup_webhook_code')
                updated = True
                break
    if updated:
        save_json(TOPUPS_FILE, tups)
        socketio.emit('topup_updated', {"matched": True})
        return jsonify({"ok": True})
    # fallback: if amount unique among pending, attach webhook payload for manual review
    if amount is not None:
        candidates = [t for t in tups if t.get('status')=='pending' and abs(float(t.get('amount',0)) - float(amount)) < 0.01]
        if len(candidates) == 1:
            t = candidates[0]
            t.setdefault('payment', {})['last_webhook'] = payload
            t.setdefault('payment', {})['manual_code_verified'] = False
            save_json(TOPUPS_FILE, tups)
            socketio.emit('topup_updated', {"matched": False})
            return jsonify({"ok": True, "note": "recorded"})
    logger.info("webhook no match order=%s comment=%s amount=%s", order_id, comment, amount)
    return jsonify({"ok": True, "matched": False})

# Admin endpoints: list incoming topups
@app.route('/api/admin/incoming_topups', methods=['GET'])
@require_admin
def api_admin_incoming_topups():
    status_q = (request.args.get('status') or "").strip().lower()
    comment_q = (request.args.get('comment') or "").strip().lower()
    tups = load_json(TOPUPS_FILE, [])
    out = []
    for t in tups:
        if status_q and t.get('status','').lower() != status_q:
            continue
        if comment_q:
            comment = (t.get('payment',{}).get('comment') or "") + " " + (t.get('manual_code') or "")
            if comment_q not in comment.lower():
                continue
        out.append(t)
    out.sort(key=lambda x: x.get('created_at',''), reverse=True)
    return jsonify({"ok": True, "items": out})

@app.route('/api/admin/incoming_topups/<tid>/mark_paid', methods=['POST'])
@require_admin
def api_admin_mark_paid(tid):
    tups = load_json(TOPUPS_FILE, [])
    found = next((t for t in tups if t.get('id')==tid), None)
    if not found:
        return jsonify({"ok": False, "reason": "not_found"}), 404
    if found.get('status') == 'paid':
        return jsonify({"ok": False, "reason": "already_paid"}), 400
    found['status'] = 'paid'
    found['paid_at'] = datetime.utcnow().isoformat()+"Z"
    found.setdefault('payment', {})['manual_code_verified'] = True
    uid = found.get('user',{}).get('id')
    if uid:
        credit_user(uid, found.get('amount',0.0), note='admin_marked')
    save_json(TOPUPS_FILE, tups)
    socketio.emit('topup_updated', {"admin_marked": tid})
    return jsonify({"ok": True, "topup": found})

@app.route('/api/admin/incoming_topups/<tid>/force_match', methods=['POST'])
@require_admin
def api_admin_force_match(tid):
    data = request.get_json() or {}
    verify_code = bool(data.get('verify_code', True))
    tups = load_json(TOPUPS_FILE, [])
    found = next((t for t in tups if t.get('id')==tid), None)
    if not found:
        return jsonify({"ok": False, "reason": "not_found"}), 404
    if found.get('status') == 'paid':
        return jsonify({"ok": False, "reason": "already_paid"}), 400
    found['status'] = 'paid'
    found['paid_at'] = datetime.utcnow().isoformat()+"Z"
    found.setdefault('payment', {})['manual_code_verified'] = verify_code
    uid = found.get('user',{}).get('id')
    if uid:
        credit_user(uid, found.get('amount',0.0), note='admin_force')
    save_json(TOPUPS_FILE, tups)
    socketio.emit('topup_updated', {"force_match": tid})
    return jsonify({"ok": True, "topup": found})

@app.route('/api/admin/incoming_topups/<tid>/refund', methods=['POST'])
@require_admin
def api_admin_refund(tid):
    data = request.get_json() or {}
    reason = (data.get('reason') or "").strip()
    tups = load_json(TOPUPS_FILE, [])
    found = next((t for t in tups if t.get('id')==tid), None)
    if not found:
        return jsonify({"ok": False, "reason": "not_found"}), 404
    if found.get('status') == 'refunded':
        return jsonify({"ok": False, "reason": "already_refunded"}), 400
    found['status'] = 'refunded'
    found['refunded_at'] = datetime.utcnow().isoformat()+"Z"
    found.setdefault('refund_info', {})['reason'] = reason
    save_json(TOPUPS_FILE, tups)
    socketio.emit('topup_updated', {"refund": tid})
    return jsonify({"ok": True, "topup": found})

# Withdraw endpoint (public) - requires init_data in header
@app.route('/api/withdraw_public', methods=['POST'])
def api_withdraw_public():
    data = request.get_json() or {}
    try:
        amount = float(data.get('amount') or 0)
    except Exception:
        amount = 0
    name = (data.get('name') or "").strip()
    bank = (data.get('bank') or "").strip()
    if amount < 250:
        return jsonify({"ok": False, "reason": "min_withdraw_250"}), 400
    init_data = request.headers.get('X-Tg-InitData') or request.args.get('init_data')
    if not init_data:
        return jsonify({"ok": False, "reason": "init_data_required"}), 401
    # verify and get uid
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True)
        params = dict(pairs)
        provided_hash = params.pop('hash', None)
        if not provided_hash:
            return jsonify({"ok": False, "reason": "invalid_init_data"}), 403
        data_check_arr = [f"{k}={params[k]}" for k in sorted(params.keys())]
        data_check_string = "\n".join(data_check_arr)
        secret_key = hashlib.sha256(BOT_TOKEN.encode()).digest()
        hmac_hash = hmac.new(secret_key, data_check_string.encode('utf-8'), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(hmac_hash, provided_hash):
            return jsonify({"ok": False, "reason": "hash_mismatch"}), 403
        uid = params.get('id') or params.get('user_id')
    except Exception:
        return jsonify({"ok": False, "reason": "invalid_init_data"}), 403
    if not uid:
        return jsonify({"ok": False, "reason": "uid_missing"}), 401
    rec = get_user(uid)
    if rec.get('balance',0.0) < amount:
        return jsonify({"ok": False, "reason": "insufficient_balance", "balance": rec.get('balance',0.0)}), 400
    rec['balance'] = round(rec.get('balance',0.0) - amount, 2)
    rec.setdefault('history', []).insert(0, {"type":"withdraw", "amount": amount, "name": name, "bank": bank, "created_at": datetime.utcnow().isoformat()+"Z"})
    save_json(USERS_FILE, users)
    # record in withdraws file
    append_json(os.path.join(DATA_DIR, "withdraws.json"), {"id": gen_id("wd"), "user": {"id": uid}, "amount": amount, "name": name, "bank": bank, "status": "pending", "created_at": datetime.utcnow().isoformat()+"Z"})
    socketio.emit('withdraw_created', {"user": uid, "amount": amount})
    return jsonify({"ok": True, "balance_after": rec['balance']})

# user_history
@app.route('/api/user_history', methods=['GET'])
def api_user_history():
    uid = (request.args.get('uid') or "").strip()
    if not uid:
        return jsonify({"ok": False, "reason": "missing_uid"}), 400
    rec = get_user(uid)
    # combine history and works
    items = rec.get('history', [])
    # include tasks where user is worker
    tasks_local = load_json(TASKS_FILE, [])
    for t in tasks_local:
        for w in t.get('workers', []):
            if str(w.get('uid')) == str(uid):
                items.insert(0, {"type": "work", "task_id": t.get('id'), "task_title": t.get('title'), "created_at": t.get('created_at')})
    return jsonify({"ok": True, "items": items})

# ---------- Run ----------
if __name__ == '__main__':
    # Start polling bot if telebot available; we avoid double polling if webhook used.
    if bot:
        try:
            bot.remove_webhook()
        except Exception:
            pass
        def _poll():
            while True:
                try:
                    logger.info("Starting bot polling...")
                    bot.infinity_polling(timeout=60, long_polling_timeout=50)
                except Exception as ex:
                    logger.exception("Bot polling error: %s", ex)
                    time.sleep(5)
        import threading
        threading.Thread(target=_poll, daemon=True).start()
    port = int(os.environ.get("PORT", "8080"))
    logger.info("Starting server on port %s", port)
    socketio.run(app, host='0.0.0.0', port=port)
