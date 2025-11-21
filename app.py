#!/usr/bin/env python3
# coding: utf-8
"""
ReviewCash main server (Flask + TeleBot + Flask-SocketIO).
Important:
- Do NOT hardcode BOT_TOKEN in this file; set environment variables instead.
- Recommended env vars:
  BOT_TOKEN, WEBAPP_URL, ADMIN_USER_IDS, ADMIN_USERNAMES, ADMIN_JWT_SECRET,
  BOT_FORCE_POLLING, BOT_USE_POLLING, BOT_DISABLE_BOT, EVENTLET_NO_GREENDNS
"""
import os
# Disable eventlet greendns as early as possible (before importing eventlet)
os.environ.setdefault('EVENTLET_NO_GREENDNS', 'true')

import time
import json
import threading
import random
import socket as _socket
import hashlib
import hmac
from datetime import datetime, timedelta
from urllib.parse import quote_plus, parse_qsl

# third-party imports
# Import eventlet after setting EVENTLET_NO_GREENDNS
import eventlet
# Some eventlet versions accept dns kwarg, others don't - try both safely
try:
    eventlet.monkey_patch(dns=False)
except TypeError:
    eventlet.monkey_patch()

# Flask & SocketIO
from flask import Flask, request, send_from_directory, jsonify, abort
from flask_socketio import SocketIO, join_room, leave_room

# Optional telebot import - allow running server without bot token
try:
    import telebot
except Exception:
    telebot = None

# JWT for admin tokens
try:
    import jwt
except Exception:
    jwt = None  # If missing, admin token features will fail; recommend installing PyJWT

# ========== CONFIG ==========
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8033069276:AAFv1-kdQ68LjvLEgLHj3ZXd5ehMqyUXOYU")  # set in env
WEBAPP_URL = os.environ.get("WEBAPP_URL", "https://example.com").rstrip('/')
CHANNEL_ID = os.environ.get("CHANNEL_ID", "@ReviewCashNews")

# Ensure your admin id is present by default (change as needed)
ADMIN_USER_IDS = [s.strip() for s in os.environ.get("ADMIN_USER_IDS", "6482440657").split(",") if s.strip()]
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

# ========== STORAGE HELPERS ==========
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

# ========== APP / BOT / SOCKETIO ==========
app = Flask(__name__, static_folder='public')

# initialize telebot only if token present and library available
if BOT_TOKEN and telebot:
    bot = telebot.TeleBot(BOT_TOKEN)
else:
    bot = None
    if not BOT_TOKEN:
        print("[startup] BOT_TOKEN not set - Telegram bot disabled.")
    elif not telebot:
        print("[startup] pytelegrambotapi not installed - Telegram features disabled.")

REDIS_URL = os.environ.get("REDIS_URL")
if REDIS_URL:
    socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet", message_queue=REDIS_URL)
else:
    socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

# persisted data
users = load_json_safe(USERS_FILE, {})  # keyed by uid string
ordinary_admins = load_json_safe(ADMINS_FILE, [])  # list of identifiers (id strings or usernames)

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
    except Exception:
        return False, None

# ========== Telegram WebApp init_data verification ==========
def verify_telegram_init_data(init_data_str):
    """
    Verify Telegram WebApp initData per Telegram docs.
    Returns (True, params) on success, else (False, None).
    """
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

# ========== DNS / network helper ==========
def can_resolve_host(host="api.telegram.org"):
    try:
        _socket.getaddrinfo(host, 443)
        return True
    except Exception as e:
        print(f"[net-check] DNS resolution failed for {host}: {e}")
        return False

# ========== SOCKET.IO handlers ==========
@socketio.on('connect')
def _on_connect(auth):
    try:
        token = None
        if isinstance(auth, dict):
            token = auth.get('token')
        if not token:
            # allow readonly connections
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

# ========== FLASK ROUTES ==========
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
    """
    Protected user profile endpoint.
    - Admin token: ?token=... and ?uid=...
    - Otherwise: require Telegram WebApp init_data (header X-Tg-InitData or ?init_data=)
    """
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

# Admin / main admin pages and APIs
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
@require_admin_token
def api_topup_reject(req_id):
    payload = request.get_json() or {}
    reason = payload.get("reason", "Отклонено администратором")
    arr = load_json_safe(TOPUPS_FILE, [])
    for it in arr:
        if it.get("id") == req_id:
            if it.get("status") in ("rejected", "approved"):
                return jsonify({"ok": False, "reason": "already_handled"}), 400
            it["status"] = "rejected"
            it["handled_by"] = "admin"
            it["handled_at"] = datetime.utcnow().isoformat() + "Z"
            it["reject_reason"] = reason
            save_json(TOPUPS_FILE, arr)
            notify_update_topup(it)
            try:
                uid = int(it["user"]["id"])
                if bot:
                    bot.send_message(uid, f"Ваше пополнение отклонено: {reason}")
            except Exception:
                pass
            return jsonify({"ok": True})
    return jsonify({"ok": False, "reason": "not_found"}), 404

@app.route('/api/withdraws/<req_id>/approve', methods=['POST'])
@require_admin_token
def api_withdraw_approve(req_id):
    arr = load_json_safe(WITHDRAWS_FILE, [])
    for it in arr:
        if it.get("id") == req_id:
            if it.get("status") == "paid":
                return jsonify({"ok": False, "reason": "already_paid"}), 400
            it["status"] = "paid"
            it["handled_by"] = "admin"
            it["handled_at"] = datetime.utcnow().isoformat() + "Z"
            uid = int(it["user"]["id"])
            users.setdefault(str(uid), {"balance": 0, "tasks_done": 0, "total_earned": 0, "subscribed": False, "last_submissions": {}})
            save_json(WITHDRAWS_FILE, arr)
            notify_new_withdraw(it)
            try:
                if bot:
                    bot.send_message(uid, f"Ваш вывод {it.get('amount',0)} ₽ помечен как выплачен.")
            except Exception:
                pass
            return jsonify({"ok": True})
    return jsonify({"ok": False, "reason": "not_found"}), 404

@app.route('/api/withdraws/<req_id>/reject', methods=['POST'])
@require_admin_token
def api_withdraw_reject(req_id):
    payload = request.get_json() or {}
    reason = payload.get("reason", "Отклонено администратором")
    arr = load_json_safe(WITHDRAWS_FILE, [])
    for it in arr:
        if it.get("id") == req_id:
            if it.get("status") in ("rejected", "paid"):
                return jsonify({"ok": False, "reason": "already_handled"}), 400
            it["status"] = "rejected"
            it["handled_by"] = "admin"
            it["handled_at"] = datetime.utcnow().isoformat() + "Z"
            it["reject_reason"] = reason
            try:
                uid = int(it["user"]["id"])
                if str(uid) in users:
                    users[str(uid)]["balance"] = users[str(uid)].get("balance", 0) + it.get("amount", 0)
                    save_users()
                    socketio.emit('user_balance_changed', {"uid": str(uid), "balance": users[str(uid)]["balance"]}, room='admins_main')
            except Exception:
                pass
            save_json(WITHDRAWS_FILE, arr)
            notify_new_withdraw(it)
            try:
                uid = int(it["user"]["id"])
                if bot:
                    bot.send_message(uid, f"Ваш вывод отклонён: {reason}")
            except Exception:
                pass
            return jsonify({"ok": True})
    return jsonify({"ok": False, "reason": "not_found"}), 404

@app.route('/api/works/<work_id>/approve', methods=['POST'])
@require_admin_token
def api_work_approve(work_id):
    arr = load_json_safe(WORKS_FILE, [])
    for it in arr:
        if it.get("id") == work_id:
            if it.get("status") == "paid":
                return jsonify({"ok": False, "reason": "already_paid"}), 400
            it["status"] = "paid"
            it["handled_by"] = "admin"
            it["handled_at"] = datetime.utcnow().isoformat() + "Z"
            uid = int(it["user"]["id"])
            users.setdefault(str(uid), {"balance": 0, "tasks_done": 0, "total_earned": 0, "subscribed": False, "last_submissions": {}})
            users[str(uid)]["balance"] = users[str(uid)].get("balance", 0) + it.get("amount", 0)
            users[str(uid)]["tasks_done"] = users[str(uid)].get("tasks_done", 0) + 1
            users[str(uid)]["total_earned"] = users[str(uid)].get("total_earned", 0) + it.get("amount", 0)
            save_users()
            save_json(WORKS_FILE, arr)
            notify_new_work(it)
            try:
                if bot:
                    bot.send_message(uid, f"Ваша заявка на выполнение задания принята. +{it.get('amount',0)} ₽ на баланс.")
            except Exception:
                pass
            try:
                socketio.emit('user_balance_changed', {"uid": str(uid), "balance": users[str(uid)]["balance"]}, room='admins_main')
            except Exception:
                pass
            return jsonify({"ok": True})
    return jsonify({"ok": False, "reason": "not_found"}), 404

@app.route('/api/works/<work_id>/reject', methods=['POST'])
@require_admin_token
def api_work_reject(work_id):
    payload = request.get_json() or {}
    reason = payload.get("reason", "Отклонено администратором")
    arr = load_json_safe(WORKS_FILE, [])
    for it in arr:
        if it.get("id") == work_id:
            if it.get("status") in ("rejected", "paid"):
                return jsonify({"ok": False, "reason": "already_handled"}), 400
            it["status"] = "rejected"
            it["handled_by"] = "admin"
            it["handled_at"] = datetime.utcnow().isoformat() + "Z"
            it["reject_reason"] = reason
            try:
                uid = int(it["user"]["id"])
                if "last_submissions" in users.get(str(uid), {}):
                    if it.get("platform"):
                        users[str(uid)]["last_submissions"].pop(it.get("platform"), None)
                        save_users()
            except Exception:
                pass
            save_json(WORKS_FILE, arr)
            notify_new_work(it)
            try:
                if bot:
                    bot.send_message(int(it["user"]["id"]), f"Ваша заявка отклонена: {reason}")
            except Exception:
                pass
            return jsonify({"ok": True})
    return jsonify({"ok": False, "reason": "not_found"}), 404

@app.route('/api/tasks/<task_id>/delete', methods=['POST'])
@require_admin_token
def api_task_delete(task_id):
    payload = request.get_json() or {}
    reason = payload.get("reason", "")
    arr = load_json_safe(TASKS_FILE, [])
    for t in arr:
        if t.get("id") == task_id:
            t["status"] = "deleted"
            t["deleted_by"] = "admin"
            t["deleted_at"] = datetime.utcnow().isoformat() + "Z"
            t["delete_reason"] = reason
            save_json(TASKS_FILE, arr)
            try:
                socketio.emit('task_deleted', t, room='admins_main')
                socketio.emit('task_deleted', t, room='admins_ordinary')
            except Exception:
                pass
            return jsonify({"ok": True})
    return jsonify({"ok": False, "reason": "not_found"}), 404

@app.route('/whoami', methods=['GET'])
@require_admin_token
def api_whoami():
    token = get_token_from_request(request)
    ok, payload = verify_admin_token(token)
    if not ok:
        return jsonify({"ok": False}), 403
    uid = str(payload.get('uid', ''))
    username = payload.get('username', '')
    is_main = (uid in ADMIN_USER_IDS) or (username in ADMIN_USERNAMES)
    return jsonify({"ok": True, "admin": {"uid": uid, "username": username, "is_main": is_main}})

# ========== WEBHOOK / POLLING ==========
def start_polling_thread_safe():
    if os.environ.get("BOT_DISABLE_BOT", "").lower() in ("1", "true", "yes"):
        print("[bot] BOT_DISABLE_BOT is set — skipping Telegram bot startup.")
        return None
    if not bot:
        print("[bot] bot not configured — skipping polling.")
        return None
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
                print("[bot] Polling stopped unexpectedly; restarting in 5s")
                time.sleep(5)
    t = threading.Thread(target=_poll_loop, daemon=True)
    t.start()
    return t

def setup_webhook_safe():
    if os.environ.get("BOT_DISABLE_BOT", "").lower() in ("1", "true", "yes"):
        print("[bot] BOT_DISABLE_BOT is set — skipping webhook setup.")
        return
    if not bot:
        print("[bot] bot not configured — skipping webhook setup.")
        return
    if not can_resolve_host():
        print("[webhook setup] api.telegram.org not resolvable — skipping webhook setup.")
        if os.environ.get("BOT_FORCE_POLLING", "").lower() in ("1", "true", "yes") or os.environ.get("BOT_AUTO_POLLING", "").lower() in ("1","true","yes"):
            print("[webhook setup] BOT_FORCE_POLLING set — starting polling fallback.")
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

# ========== START ==========
if __name__ == '__main__':
    if os.environ.get("BOT_FORCE_POLLING", "").lower() in ("1", "true", "yes"):
        print("[main] BOT_FORCE_POLLING set — starting polling.")
        start_polling_thread_safe()
    else:
        threading.Thread(target=setup_webhook_safe, daemon=True).start()

    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host='0.0.0.0', port=port)
