#!/usr/bin/env python3
# coding: utf-8
"""
Full ReviewCash application (SBP + manual-code flow) with:
- User UI (profile, tasks, balance, history)
- SBP/QR topups with manual RC code and webhook auto-matching
- Withdraw flow
- Admin endpoints + admin UI (mainadmin.html)
- Socket.IO realtime updates
Configuration via environment variables (see README section in assistant message).
"""
import os
import time
import json
import logging
import hmac
import hashlib
import random
import string
from datetime import datetime, timedelta
from urllib.parse import quote_plus, urlparse, parse_qsl

import eventlet
try:
    eventlet.monkey_patch(dns=False)
except TypeError:
    eventlet.monkey_patch()

from flask import Flask, request, jsonify, send_from_directory, abort
from flask_socketio import SocketIO
# Optional: pytelegrambotapi (telebot)
try:
    import telebot
    from telebot import types as tb_types
except Exception:
    telebot = None
    tb_types = None

# Optional: PyJWT
try:
    import jwt
except Exception:
    jwt = None

# Optional requests
try:
    import requests
except Exception:
    requests = None

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("reviewcash")

# Config (override with env)
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
WEBAPP_URL = os.environ.get("WEBAPP_URL", "").rstrip("/")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "")
ADMIN_USER_IDS = [s.strip() for s in os.environ.get("ADMIN_USER_IDS", "").split(",") if s.strip()]
ADMIN_USERNAMES = [s.strip() for s in os.environ.get("ADMIN_USERNAMES", "").split(",") if s.strip()]
ADMIN_JWT_SECRET = os.environ.get("ADMIN_JWT_SECRET", "replace_with_strong_secret")
ADMIN_TOKEN_TTL_SECONDS = int(os.environ.get("ADMIN_TOKEN_TTL_SECONDS", "86400"))
DATA_DIR = os.environ.get("DATA_DIR", ".rc_data")
os.makedirs(DATA_DIR, exist_ok=True)

TOPUPS_FILE = os.path.join(DATA_DIR, "topups.json")
USERS_FILE = os.path.join(DATA_DIR, "users.json")
TASKS_FILE = os.path.join(DATA_DIR, "tasks.json")
TASK_TYPES_FILE = os.path.join(DATA_DIR, "task_types.json")
ADMINS_FILE = os.path.join(DATA_DIR, "admins.json")
ADMIN_TOKENS_FILE = os.path.join(DATA_DIR, "admin_tokens.json")

PAYMENT_PROVIDER = os.environ.get("PAYMENT_PROVIDER", "link_template")
PAYMENT_LINK_TEMPLATE = os.environ.get("PAYMENT_LINK_TEMPLATE", "")
PAYMENT_WEBHOOK_SECRET = os.environ.get("PAYMENT_WEBHOOK_SECRET", "").encode()
MIN_TOPUP = int(os.environ.get("MIN_TOPUP", "100"))

# Utility storage helpers
def load_json_safe(path, default):
    try:
        if not os.path.exists(path):
            return default
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning("failed load %s: %s", path, e)
        return default

def save_json(path, obj):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception("failed save %s: %s", path, e)

def append_json(path, obj):
    arr = load_json_safe(path, [])
    arr.append(obj)
    save_json(path, arr)

# Ensure base files
load_json_safe(TOPUPS_FILE, [])
load_json_safe(USERS_FILE, {})
load_json_safe(TASK_TYPES_FILE, [
    {"id":"ya_review","name":"Отзыв — Я.К.","unit_price":100},
    {"id":"gmaps_review","name":"Отзыв — Google Maps","unit_price":65},
    {"id":"tg_sub","name":"Подписка — Telegram канал","unit_price":10},
])
load_json_safe(ADMINS_FILE, [])
load_json_safe(ADMIN_TOKENS_FILE, [])

# Utilities
def gen_id(prefix="id"):
    return f"{prefix}_{int(time.time()*1000)}_{random.randint(1000,9999)}"

def gen_manual_code():
    return "RC" + ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))

def get_user_record(uid):
    users = load_json_safe(USERS_FILE, {})
    key = str(uid)
    if key not in users:
        users[key] = {"balance": 0.0, "tasks_done": 0, "total_earned": 0.0, "history": []}
        save_json(USERS_FILE, users)
    return users[key]

def save_user_record(uid, rec):
    users = load_json_safe(USERS_FILE, {})
    users[str(uid)] = rec
    save_json(USERS_FILE, users)

# Admin JWT helpers
def create_admin_token(uid, username, ttl_seconds=None):
    if jwt is None:
        raise RuntimeError("PyJWT not installed")
    if ttl_seconds is None:
        ttl_seconds = ADMIN_TOKEN_TTL_SECONDS
    payload = {"uid": str(uid) if uid is not None else "", "username": username or "", "exp": datetime.utcnow() + timedelta(seconds=ttl_seconds), "iat": datetime.utcnow()}
    token = jwt.encode(payload, ADMIN_JWT_SECRET, algorithm="HS256")
    if isinstance(token, bytes):
        token = token.decode("utf-8")
    return token

def verify_admin_token(token):
    if jwt is None:
        return False, None
    try:
        payload = jwt.decode(token, ADMIN_JWT_SECRET, algorithms=["HS256"])
        uid = str(payload.get("uid","")) or ""
        username = (payload.get("username") or "").strip()
        if uid and (uid in ADMIN_USER_IDS):
            return True, payload
        if username and (username in ADMIN_USERNAMES):
            return True, payload
        ordinary = load_json_safe(ADMINS_FILE, [])
        if uid and uid in ordinary:
            return True, payload
        if username and username in ordinary:
            return True, payload
        return False, None
    except jwt.ExpiredSignatureError:
        return False, "expired"
    except Exception as e:
        logger.debug("verify_admin_token error: %s", e)
        return False, None

def get_token_from_request(req):
    t = req.args.get("token")
    if t:
        return t
    auth = req.headers.get("Authorization") or req.headers.get("authorization") or ""
    if auth and auth.lower().startswith("bearer "):
        return auth.split(" ",1)[1].strip()
    return None

def require_admin_token(func):
    def wrapper(*args, **kwargs):
        token = get_token_from_request(request)
        if not token:
            return abort(401, "token required")
        ok, _ = verify_admin_token(token)
        if not ok:
            return abort(403, "forbidden")
        return func(*args, **kwargs)
    wrapper.__name__ = func.__name__
    return wrapper

# Payment invoice creation (simple SBP link + QR)
def create_payment_invoice(amount, order_id, description="Topup"):
    amount = float(amount)
    if PAYMENT_PROVIDER == "link_template" and PAYMENT_LINK_TEMPLATE:
        url = PAYMENT_LINK_TEMPLATE.format(amount=int(amount), order_id=quote_plus(order_id))
    else:
        url = f"https://example.com/pay?amount={int(amount)}&order_id={quote_plus(order_id)}"
    qr = f"https://api.qrserver.com/v1/create-qr-code/?size=400x400&data={quote_plus(url)}"
    return {"url": url, "qr": qr}

# Telegram bot optional setup (for /start and /mainadmin link)
if BOT_TOKEN and telebot:
    bot = telebot.TeleBot(BOT_TOKEN)
    logger.info("Telebot configured")
    @bot.message_handler(commands=['start'])
    def handle_start(message):
        try:
            uid = message.from_user.id
            first = (message.from_user.first_name or "").strip()
            text = f"Привет{(' ' + first) if first else ''}! Это ReviewCash — платформа для выполнения отзывов и подписок.\n\n" \
                   "Что вы можете делать:\n" \
                   "• Смотреть задания и выполнять их\n" \
                   "• Пополнять баланс через SBP/QR (вводите RC-код в комментарии)\n" \
                   "• Открыть админку (если у вас права) через /mainadmin\n\n" \
                   "Откройте WebApp: " + (WEBAPP_URL or "— (WEBAPP_URL не настроен)") 
            kb = tb_types.InlineKeyboardMarkup()
            if WEBAPP_URL:
                kb.add(tb_types.InlineKeyboardButton(text="Открыть WebApp", url=WEBAPP_URL))
            if CHANNEL_ID:
                kb.add(tb_types.InlineKeyboardButton(text="Канал", url=f"https://t.me/{CHANNEL_ID.lstrip('@')}"))
            bot.send_message(uid, text, reply_markup=kb)
        except Exception as e:
            logger.exception("start handler error: %s", e)
else:
    bot = None
    if BOT_TOKEN:
        logger.warning("telebot not installed; bot features disabled")

# Flask app & socketio
app = Flask(__name__, static_folder="public", static_url_path="/")
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

# Routes: pages
@app.route("/")
def index():
    return send_from_directory("public", "index.html")

@app.route("/mainadmin")
def mainadmin():
    return send_from_directory("public", "mainadmin.html")

@app.route("/<path:path>")
def static_files(path):
    return send_from_directory("public", path)

@app.route("/health")
def health():
    return jsonify({"ok": True, "ts": datetime.utcnow().isoformat()+"Z"})

# Public API: tasks list (active)
@app.route("/api/tasks_public", methods=["GET"])
def api_tasks_public():
    tasks = load_json_safe(TASKS_FILE, [])
    active = [t for t in tasks if t.get("status","active") == "active"]
    return jsonify(active)

# Admin API: create tasks (admin token required)
@app.route("/api/tasks_create", methods=["POST"])
@require_admin_token
def api_tasks_create():
    data = request.get_json() or {}
    title = (data.get("title") or "").strip()
    description = (data.get("description") or "").strip()
    type_id = (data.get("type_id") or "").strip()
    try:
        unit_price = int(data.get("unit_price") or 0)
    except Exception:
        unit_price = 0
    try:
        count = int(data.get("count") or 1)
    except Exception:
        count = 1
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
    append_json(TASKS_FILE, task)
    socketio.emit("new_task", task)
    return jsonify({"ok": True, "task": task})

# Profile: require Telegram init_data (WebApp) to get real user and optionally avatar
@app.route("/api/profile_me", methods=["GET"])
def api_profile_me():
    init_data = request.headers.get("X-Tg-InitData") or request.args.get("init_data")
    if not init_data:
        return jsonify({"ok": False, "reason": "init_data_required"}), 401
    # verify init_data (Telegram webapp)
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True)
        params = dict(pairs)
        provided_hash = params.pop("hash", None)
        if not provided_hash or not BOT_TOKEN:
            return jsonify({"ok": False, "reason": "invalid_init_data"}), 403
        data_check_items = []
        for k in sorted(params.keys()):
            data_check_items.append(f"{k}={params[k]}")
        data_check_string = "\n".join(data_check_items)
        secret_key = hashlib.sha256(BOT_TOKEN.encode()).digest()
        hmac_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(hmac_hash, provided_hash):
            return jsonify({"ok": False, "reason": "invalid_hash"}), 403
        uid = params.get("id") or params.get("user_id")
        username = params.get("username") or None
        first_name = params.get("first_name") or ""
        rec = get_user_record(uid)
        resp = {"ok": True, "user": {"id": uid, "username": username, "first_name": first_name}, "balance": rec.get("balance",0.0), "history": rec.get("history",[])}
        # try to fetch photo via bot if available
        if bot:
            try:
                photos = bot.get_user_profile_photos(int(uid))
                if photos and getattr(photos, "total_count", 0) > 0:
                    file_id = photos.photos[0][0].file_id
                    f = bot.get_file(file_id)
                    file_path = getattr(f, "file_path", None)
                    if file_path:
                        resp["photo_url"] = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
            except Exception:
                pass
        return jsonify(resp)
    except Exception as e:
        logger.exception("profile_me error: %s", e)
        return jsonify({"ok": False, "reason": "invalid_init_data"}), 403

# User history (works/topups/withdraws)
@app.route("/api/user_history", methods=["GET"])
def api_user_history():
    uid = (request.args.get("uid") or "").strip()
    if not uid:
        return jsonify({"ok": False, "reason": "missing_uid"}), 400
    # For simplicity, collate topups and tasks done
    topups = load_json_safe(TOPUPS_FILE, [])
    tasks = load_json_safe(TASKS_FILE, [])
    items = []
    for t in topups:
        try:
            user_id = str(t.get("user",{}).get("id") or "")
            if user_id == str(uid):
                items.append({"id": t.get("id"), "type": "topup", "amount": t.get("amount"), "created_at": t.get("created_at")})
        except Exception:
            pass
    # Add user's completed work from tasks.workers entries (optional)
    # For demo: show tasks where worker list contains uid
    for task in tasks:
        for w in task.get("workers", []):
            if str(w.get("uid")) == str(uid):
                items.append({"id": task.get("id"), "type": "work", "task_title": task.get("title"), "created_at": task.get("created_at")})
    items.sort(key=lambda x: x.get("created_at",""), reverse=True)
    return jsonify({"ok": True, "items": items})

# Topup create (public)
@app.route("/api/topups_public", methods=["POST"])
def api_topups_public():
    data = request.get_json() or {}
    try:
        amount = float(data.get("amount") or 0)
    except Exception:
        amount = 0
    if amount < MIN_TOPUP:
        return jsonify({"ok": False, "reason": "min_topup", "min": MIN_TOPUP}), 400
    init_data = request.headers.get("X-Tg-InitData") or request.args.get("init_data")
    user = {}
    if init_data:
        try:
            pairs = parse_qsl(init_data, keep_blank_values=True)
            params = dict(pairs)
            uid = params.get("id") or params.get("user_id")
            if uid:
                user = {"id": uid}
        except Exception:
            pass
    topup_id = gen_id("top")
    manual_code = gen_manual_code()
    rec = {"id": topup_id, "user": user, "amount": round(amount,2), "status": "pending", "created_at": datetime.utcnow().isoformat()+"Z", "manual_code": manual_code, "payment": {}, "note_nonrefundable": True, "instruction_text": f"Введите код {manual_code} в комментарии перевода."}
    try:
        rec["payment"] = create_payment_invoice(amount, topup_id)
    except Exception as e:
        rec["payment_error"] = str(e)
    append_json(TOPUPS_FILE, rec)
    socketio.emit("new_topup", rec)
    return jsonify({"ok": True, "topup": rec, "manual_code": manual_code, "instruction": rec["instruction_text"]})

# Get topup
@app.route("/api/topups/<tid>", methods=["GET"])
def api_get_topup(tid):
    arr = load_json_safe(TOPUPS_FILE, [])
    found = next((t for t in arr if t.get("id") == tid), None)
    if not found:
        return jsonify({"ok": False, "reason": "not_found"}), 404
    return jsonify({"ok": True, "topup": found})

# Payment webhook
@app.route("/api/payment_webhook", methods=["POST"])
def api_payment_webhook():
    if PAYMENT_WEBHOOK_SECRET and request.data:
        sig = request.headers.get("X-Signature") or request.headers.get("X-Hub-Signature") or ""
        if not sig:
            return "signature required", 400
        mac = hmac.new(PAYMENT_WEBHOOK_SECRET, request.data, digestmod=hashlib.sha256).hexdigest()
        if not hmac.compare_digest(mac, sig):
            return "invalid signature", 403
    payload = request.get_json() or {}
    order_id = payload.get("orderId") or payload.get("order_id") or payload.get("merchantOrderId") or payload.get("invoice_id")
    status = payload.get("status") or payload.get("payment_status")
    comment = payload.get("comment") or payload.get("payment_comment") or ""
    amount = None
    if payload.get("amount") is not None:
        try:
            amount = float(payload.get("amount"))
        except Exception:
            amount = None
    paid = False
    if isinstance(status, str) and status.lower() in ("paid","success","complete","confirmed","ok"):
        paid = True
    if isinstance(status, bool) and status is True:
        paid = True
    topups = load_json_safe(TOPUPS_FILE, [])
    updated = False
    if order_id:
        for t in topups:
            if t.get("id") == str(order_id):
                if paid and t.get("status") != "paid":
                    t["status"] = "paid"
                    t["paid_at"] = datetime.utcnow().isoformat()+"Z"
                    if comment:
                        t.setdefault("payment", {})["comment"] = comment
                        t.setdefault("payment", {})["manual_code_verified"] = (t.get("manual_code") in comment)
                    uid = t.get("user",{}).get("id")
                    if uid:
                        rec = get_user_record(uid)
                        rec["balance"] = round(float(rec.get("balance",0.0)) + float(t.get("amount",0.0)), 2)
                        save_user_record(uid, rec)
                    updated = True
                break
    if not updated and comment:
        for t in topups:
            if t.get("status") != "pending":
                continue
            code = t.get("manual_code")
            if not code:
                continue
            if code in comment:
                if amount is not None:
                    if abs(float(t.get("amount",0)) - float(amount)) > 0.001:
                        continue
                t["status"] = "paid"
                t["paid_at"] = datetime.utcnow().isoformat()+"Z"
                t.setdefault("payment", {})["comment"] = comment
                t.setdefault("payment", {})["manual_code_verified"] = True
                uid = t.get("user",{}).get("id")
                if uid:
                    rec = get_user_record(uid)
                    rec["balance"] = round(float(rec.get("balance",0.0)) + float(t.get("amount",0.0)), 2)
                    save_user_record(uid, rec)
                updated = True
                break
    if not updated and amount is not None:
        candidates = [t for t in topups if t.get("status") == "pending" and abs(float(t.get("amount",0)) - float(amount)) < 0.001]
        if len(candidates) == 1:
            t = candidates[0]
            t.setdefault("payment", {})["last_webhook"] = payload
            t.setdefault("payment", {})["manual_code_verified"] = False
            save_json(TOPUPS_FILE, topups)
            socketio.emit("topup_updated", t)
            return jsonify({"ok": True, "matched": False, "note": "recorded into candidate"})
    if updated:
        save_json(TOPUPS_FILE, topups)
        socketio.emit("topup_updated", {"source":"webhook"})
        return jsonify({"ok": True, "matched": True})
    return jsonify({"ok": True, "matched": False})

# Admin endpoints (incoming topups)
@app.route("/api/admin/incoming_topups", methods=["GET"])
@require_admin_token
def api_admin_incoming_topups():
    status_q = (request.args.get("status") or "").strip().lower()
    comment_q = (request.args.get("comment") or "").strip()
    manual_only = (request.args.get("manual_only") or "").lower() in ("1","true","yes")
    topups = load_json_safe(TOPUPS_FILE, [])
    out = []
    for t in topups:
        if status_q and (t.get("status","").lower() != status_q):
            continue
        if comment_q:
            comment_field = (t.get("payment",{}).get("comment") or "") + " " + (t.get("manual_code") or "")
            if comment_q.lower() not in comment_field.lower():
                continue
        if manual_only:
            if not t.get("manual_code"):
                continue
            if t.get("payment",{}).get("manual_code_verified") is True:
                continue
        out.append(t)
    out.sort(key=lambda x: x.get("created_at",""), reverse=True)
    return jsonify({"ok": True, "items": out})

@app.route("/api/admin/incoming_topups/<tid>/mark_paid", methods=["POST"])
@require_admin_token
def api_admin_mark_paid(tid):
    token = get_token_from_request(request)
    ok, payload = verify_admin_token(token)
    admin_ident = {}
    if ok and isinstance(payload, dict):
        admin_ident = {"uid": payload.get("uid"), "username": payload.get("username")}
    topups = load_json_safe(TOPUPS_FILE, [])
    found = next((t for t in topups if t.get("id") == tid), None)
    if not found:
        return jsonify({"ok": False, "reason": "not_found"}), 404
    if found.get("status") == "paid":
        return jsonify({"ok": False, "reason": "already_paid"}), 400
    found["status"] = "paid"
    found["paid_at"] = datetime.utcnow().isoformat() + "Z"
    found.setdefault("payment", {})["manual_code_verified"] = True
    found.setdefault("handled_by_admin", admin_ident)
    uid = found.get("user",{}).get("id")
    if uid:
        rec = get_user_record(uid)
        rec["balance"] = round(float(rec.get("balance",0.0)) + float(found.get("amount",0.0)), 2)
        save_user_record(uid, rec)
    save_json(TOPUPS_FILE, topups)
    socketio.emit("admin_topup_marked_paid", found)
    return jsonify({"ok": True, "topup": found})

@app.route("/api/admin/incoming_topups/<tid>/force_match", methods=["POST"])
@require_admin_token
def api_admin_force_match(tid):
    data = request.get_json() or {}
    verify_code = data.get("verify_code", True)
    token = get_token_from_request(request)
    ok, payload = verify_admin_token(token)
    admin_ident = {}
    if ok and isinstance(payload, dict):
        admin_ident = {"uid": payload.get("uid"), "username": payload.get("username")}
    topups = load_json_safe(TOPUPS_FILE, [])
    found = next((t for t in topups if t.get("id") == tid), None)
    if not found:
        return jsonify({"ok": False, "reason": "not_found"}), 404
    if found.get("status") == "paid":
        return jsonify({"ok": False, "reason": "already_paid"}), 400
    found["status"] = "paid"
    found["paid_at"] = datetime.utcnow().isoformat() + "Z"
    found.setdefault("payment", {})["manual_code_verified"] = bool(verify_code)
    found.setdefault("handled_by_admin", admin_ident)
    uid = found.get("user",{}).get("id")
    if uid:
        rec = get_user_record(uid)
        rec["balance"] = round(float(rec.get("balance",0.0)) + float(found.get("amount",0.0)), 2)
        save_user_record(uid, rec)
    save_json(TOPUPS_FILE, topups)
    socketio.emit("admin_topup_forced", found)
    return jsonify({"ok": True, "topup": found})

@app.route("/api/admin/incoming_topups/<tid>/refund", methods=["POST"])
@require_admin_token
def api_admin_refund(tid):
    data = request.get_json() or {}
    reason = (data.get("reason") or "").strip()
    token = get_token_from_request(request)
    ok, payload = verify_admin_token(token)
    admin_ident = {}
    if ok and isinstance(payload, dict):
        admin_ident = {"uid": payload.get("uid"), "username": payload.get("username")}
    topups = load_json_safe(TOPUPS_FILE, [])
    found = next((t for t in topups if t.get("id") == tid), None)
    if not found:
        return jsonify({"ok": False, "reason": "not_found"}), 404
    if found.get("status") == "refunded":
        return jsonify({"ok": False, "reason": "already_refunded"}), 400
    found["status"] = "refunded"
    found["refunded_at"] = datetime.utcnow().isoformat() + "Z"
    found.setdefault("refund_info", {})["admin"] = admin_ident
    if reason: found.setdefault("refund_info", {})["reason"] = reason
    save_json(TOPUPS_FILE, topups)
    socketio.emit("admin_topup_refunded", found)
    return jsonify({"ok": True, "topup": found})

# Run
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    logger.info("Starting ReviewCash on port %s", port)
    socketio.run(app, host="0.0.0.0", port=port)
