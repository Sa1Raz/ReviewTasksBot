#!/usr/bin/env python3
# coding: utf-8
"""
ReviewCash — full server with:
- topup creation (manual RC code, QR/link generation)
- webhook with auto-matching by order_id or manual_code+amount
- admin endpoints for incoming transfers (filter, mark_paid, refund, force_match)
- safe polling/webhook logic for Telegram (if needed)
- socket.io notifications
Note: This file includes BOT_TOKEN, WEBAPP_URL, CHANNEL_ID and ADMIN_USER_IDS as provided.
Set env vars to override defaults if needed.
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
from datetime import datetime, timedelta
from urllib.parse import quote_plus, urlparse, parse_qsl

import eventlet
try:
    eventlet.monkey_patch(dns=False)
except TypeError:
    eventlet.monkey_patch()

from flask import Flask, request, jsonify, send_from_directory, abort
from flask_socketio import SocketIO, emit, join_room

# Optional libs
try:
    import requests
except Exception:
    requests = None

# jwt optional for admin tokens
try:
    import jwt
except Exception:
    jwt = None

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("reviewcash")

# ---------- Config (prefilled per your request; you may override via env) ----------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8033069276:AAFv1-kdQ68LjvLEgLHj3ZXd5ehMqyUXOYU").strip()
WEBAPP_URL = os.environ.get("WEBAPP_URL", "https://web-production-398fb.up.railway.app").rstrip('/')
CHANNEL_ID = os.environ.get("CHANNEL_ID", "@ReviewCashNews").strip()
ADMIN_USER_IDS = [s.strip() for s in os.environ.get("ADMIN_USER_IDS", "6482440657").split(",") if s.strip()]
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
SUPPORT_FILE = os.path.join(DATA_DIR, "support.json")

# Payment config
PAYMENT_PROVIDER = os.environ.get("PAYMENT_PROVIDER", "link_template")  # or 'tinkoff'
PAYMENT_LINK_TEMPLATE = os.environ.get("PAYMENT_LINK_TEMPLATE", "")  # e.g. https://pay.example.com/pay?amount={amount}&order_id={order_id}
PAYMENT_WEBHOOK_SECRET = os.environ.get("PAYMENT_WEBHOOK_SECRET", "").encode()
MIN_TOPUP = int(os.environ.get("MIN_TOPUP", "100"))

# Telegram outbound config
TELEGRAM_HTTP_TIMEOUT = float(os.environ.get("TELEGRAM_HTTP_TIMEOUT", "6"))
TELEGRAM_HTTP_RETRIES = int(os.environ.get("TELEGRAM_HTTP_RETRIES", "3"))
TELEGRAM_HTTP_BACKOFF = float(os.environ.get("TELEGRAM_HTTP_BACKOFF", "1.2"))

# ---------- Storage helpers ----------
def load_json_safe(path, default):
    try:
        if not os.path.exists(path):
            return default
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning("Failed to load %s: %s", path, e)
        return default

def save_json(path, obj):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception("Failed to save %s: %s", path, e)

def append_json(path, obj):
    arr = load_json_safe(path, [])
    arr.append(obj)
    save_json(path, arr)

# Ensure default files exist
load_json_safe(TOPUPS_FILE, [])
load_json_safe(USERS_FILE, {})
load_json_safe(TASK_TYPES_FILE, [
    {"id": "ya_review", "name": "Отзыв — Я.К.", "unit_price": 100},
    {"id": "gmaps_review", "name": "Отзыв — Google Maps", "unit_price": 65},
    {"id": "tg_sub", "name": "Подписка — Telegram канал", "unit_price": 10}
])
load_json_safe(ADMINS_FILE, [])
load_json_safe(ADMIN_TOKENS_FILE, [])

# ---------- Utilities ----------
def gen_id(prefix="id"):
    return f"{prefix}_{int(time.time()*1000)}_{random.randint(1000,9999)}"

def gen_manual_code():
    return "RC" + ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))

def get_user_record(uid):
    users = load_json_safe(USERS_FILE, {})
    key = str(uid)
    if key not in users:
        users[key] = {"balance": 0.0, "tasks_done": 0, "total_earned": 0.0, "subscribed": False}
        save_json(USERS_FILE, users)
    return users[key]

def save_user_record(uid, rec):
    users = load_json_safe(USERS_FILE, {})
    users[str(uid)] = rec
    save_json(USERS_FILE, users)

# ---------- JWT admin token helpers ----------
def create_jwt(payload):
    if jwt is None:
        raise RuntimeError("PyJWT not installed")
    token = jwt.encode(payload, ADMIN_JWT_SECRET, algorithm="HS256")
    if isinstance(token, bytes):
        token = token.decode('utf-8')
    return token

def verify_admin_token(token):
    if jwt is None:
        return False, None
    try:
        payload = jwt.decode(token, ADMIN_JWT_SECRET, algorithms=["HS256"])
        uid = str(payload.get("uid","")) or ""
        username = (payload.get("username") or "").strip()
        # allow main admin IDs or usernames, or admins added to ADMINS_FILE
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
        ok, payload = verify_admin_token(token)
        if not ok:
            return abort(403, "forbidden")
        return func(*args, **kwargs)
    wrapper.__name__ = func.__name__
    return wrapper

# ---------- Payment invoice creation ----------
def create_payment_invoice(amount, order_id, description="Topup"):
    amount = float(amount)
    if PAYMENT_PROVIDER == "link_template":
        template = PAYMENT_LINK_TEMPLATE or ""
        if not template:
            # fallback: build simple payment URL to show in QR; replace with real provider in production
            url = f"https://example.com/pay?amount={int(amount)}&order_id={quote_plus(order_id)}"
        else:
            url = template.format(amount=int(amount), order_id=quote_plus(order_id))
        qr = f"https://api.qrserver.com/v1/create-qr-code/?size=400x400&data={quote_plus(url)}"
        return {"url": url, "qr": qr}
    else:
        # For production: implement provider-specific Init API here (Tinkoff/Sber, etc.)
        template = PAYMENT_LINK_TEMPLATE or f"https://example.com/pay?amount={int(amount)}&order_id={quote_plus(order_id)}"
        qr = f"https://api.qrserver.com/v1/create-qr-code/?size=400x400&data={quote_plus(template)}"
        return {"url": template, "qr": qr}

# ---------- App & SocketIO ----------
app = Flask(__name__, static_folder='public', static_url_path='/')
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

# ---------- Routes ----------
@app.route('/')
def index():
    return send_from_directory('public', 'index.html')

@app.route('/mainadmin')
def mainadmin_route():
    return send_from_directory('public', 'mainadmin.html')

@app.route('/<path:path>')
def static_files(path):
    return send_from_directory('public', path)

@app.route('/health')
def health():
    return jsonify({"ok": True, "ts": datetime.utcnow().isoformat()+"Z"})

# Create topup (public)
@app.route('/api/topups_public', methods=['POST'])
def api_topups_public():
    payload = request.get_json() or {}
    try:
        amount = float(payload.get('amount') or 0)
    except Exception:
        amount = 0
    if amount < MIN_TOPUP:
        return jsonify({"ok": False, "reason": "min_topup_100", "min": MIN_TOPUP}), 400

    init_data = request.headers.get('X-Tg-InitData') or request.args.get('init_data')
    user = {}
    if init_data:
        # parse minimal user id if present (best-effort)
        try:
            pairs = parse_qsl(init_data, keep_blank_values=True)
            params = dict(pairs)
            uid = params.get('id') or params.get('user_id')
            if uid:
                user = {"id": uid}
        except Exception:
            pass

    topup_id = gen_id("top")
    manual_code = gen_manual_code()
    rec = {
        "id": topup_id,
        "user": user,
        "amount": round(amount,2),
        "status": "pending",
        "created_at": datetime.utcnow().isoformat()+"Z",
        "manual_code": manual_code,
        "payment": {},
        "note_nonrefundable": True,
        "instruction_text": f"Обязательно введите код {manual_code} в поле «Комментарий к переводу» при оплате."
    }
    # create invoice/link
    try:
        rec['payment'] = create_payment_invoice(amount, topup_id, description=f"Topup {topup_id}")
    except Exception as e:
        logger.exception("create payment invoice failed: %s", e)
        rec['payment_error'] = str(e)

    append_json(TOPUPS_FILE, rec)
    socketio.emit('new_topup', rec)
    return jsonify({"ok": True, "topup": rec, "manual_code": manual_code, "instruction": rec['instruction_text']})

# Get single topup
@app.route('/api/topups/<tid>', methods=['GET'])
def api_get_topup(tid):
    arr = load_json_safe(TOPUPS_FILE, [])
    found = next((t for t in arr if t.get('id') == tid), None)
    if not found:
        return jsonify({"ok": False, "reason": "not_found"}), 404
    return jsonify({"ok": True, "topup": found})

# Payment webhook: try matching by order_id, then by manual_code+amount
@app.route('/api/payment_webhook', methods=['POST'])
def api_payment_webhook():
    # verify signature if configured
    if PAYMENT_WEBHOOK_SECRET and request.data:
        sig = request.headers.get('X-Signature') or request.headers.get('X-Hub-Signature') or ''
        if not sig:
            logger.warning("Webhook missing signature header")
            return "signature required", 400
        mac = hmac.new(PAYMENT_WEBHOOK_SECRET, request.data, digestmod=hashlib.sha256).hexdigest()
        if not hmac.compare_digest(mac, sig):
            logger.warning("Webhook invalid signature")
            return "invalid signature", 403

    payload = request.get_json() or {}
    order_id = payload.get('orderId') or payload.get('order_id') or payload.get('merchantOrderId') or payload.get('invoice_id')
    status = payload.get('status') or payload.get('payment_status')
    comment = payload.get('comment') or payload.get('payment_comment') or ""
    # amount fields vary
    amount = None
    if payload.get('amount') is not None:
        try:
            amount = float(payload.get('amount'))
        except Exception:
            try:
                amount = float(payload.get('Amount'))
            except Exception:
                amount = None

    paid = False
    if isinstance(status, str) and status.lower() in ("paid","success","complete","confirmed","ok"):
        paid = True
    if isinstance(status, bool) and status is True:
        paid = True

    topups = load_json_safe(TOPUPS_FILE, [])
    updated = False

    # match by order_id first
    if order_id:
        for t in topups:
            if t.get('id') == str(order_id):
                if paid and t.get('status') != 'paid':
                    t['status'] = 'paid'
                    t['paid_at'] = datetime.utcnow().isoformat() + "Z"
                    if comment:
                        t.setdefault('payment', {})['comment'] = comment
                        t.setdefault('payment', {})['manual_code_verified'] = (t.get('manual_code') in comment)
                    uid = t.get('user', {}).get('id')
                    if uid:
                        rec = get_user_record(uid)
                        rec['balance'] = round(float(rec.get('balance',0.0)) + float(t.get('amount',0.0)), 2)
                        save_user_record(uid, rec)
                    updated = True
                break

    # if not updated, attempt match by manual_code in comment + amount equality
    if not updated and comment:
        for t in topups:
            if t.get('status') != 'pending':
                continue
            code = t.get('manual_code')
            if not code:
                continue
            if code in comment:
                # if webhook provides amount, require amounts match
                if amount is not None:
                    if abs(float(t.get('amount',0)) - float(amount)) > 0.001:
                        logger.info("Manual code matched but amount mismatch for topup %s: topup=%s webhook=%s", t.get('id'), t.get('amount'), amount)
                        continue
                # mark paid
                t['status'] = 'paid'
                t['paid_at'] = datetime.utcnow().isoformat() + "Z"
                t.setdefault('payment', {})['comment'] = comment
                t.setdefault('payment', {})['manual_code_verified'] = True
                uid = t.get('user', {}).get('id')
                if uid:
                    rec = get_user_record(uid)
                    rec['balance'] = round(float(rec.get('balance',0.0)) + float(t.get('amount',0.0)), 2)
                    save_user_record(uid, rec)
                updated = True
                logger.info("Auto-matched webhook by manual code %s -> topup %s", code, t.get('id'))
                break

    # if still not matched: if amount unique among pending topups, record webhook payload to that candidate
    if not updated and amount is not None:
        candidates = [t for t in topups if t.get('status') == 'pending' and abs(float(t.get('amount',0)) - float(amount)) < 0.001]
        if len(candidates) == 1:
            t = candidates[0]
            t.setdefault('payment', {})['last_webhook'] = payload
            t.setdefault('payment', {})['manual_code_verified'] = False
            save_json(TOPUPS_FILE, topups)
            socketio.emit('topup_updated', t)
            logger.info("Recorded webhook for single-amount candidate topup %s", t.get('id'))
            return jsonify({"ok": True, "matched": False, "note":"recorded into candidate"})
    if updated:
        save_json(TOPUPS_FILE, topups)
        socketio.emit('topup_updated', {"source":"webhook"})
        return jsonify({"ok": True, "matched": True})
    logger.info("Webhook processed but no match found (order_id=%s, comment=%s, amount=%s)", order_id, comment, amount)
    return jsonify({"ok": True, "matched": False})

# ---------- Admin incoming transfers endpoints ----------
@app.route('/api/admin/incoming_topups', methods=['GET'])
@require_admin_token
def api_admin_incoming_topups():
    status_q = (request.args.get('status') or "").strip().lower()
    comment_q = (request.args.get('comment') or "").strip()
    manual_only = (request.args.get('manual_only') or "").lower() in ("1","true","yes")
    topups = load_json_safe(TOPUPS_FILE, [])
    out = []
    for t in topups:
        if status_q and (t.get('status','').lower() != status_q):
            continue
        if comment_q:
            comment = (t.get('payment',{}).get('comment') or "") + " " + (t.get('manual_code') or "")
            if comment_q.lower() not in comment.lower():
                continue
        if manual_only:
            if not t.get('manual_code'):
                continue
            if t.get('payment',{}).get('manual_code_verified') is True:
                continue
        out.append(t)
    out.sort(key=lambda x: x.get('created_at',''), reverse=True)
    return jsonify({"ok": True, "items": out})

@app.route('/api/admin/incoming_topups/<tid>/mark_paid', methods=['POST'])
@require_admin_token
def api_admin_incoming_mark_paid(tid):
    token = get_token_from_request(request)
    ok, payload = verify_admin_token(token)
    admin_ident = {}
    if ok and isinstance(payload, dict):
        admin_ident = {"uid": payload.get("uid"), "username": payload.get("username")}
    topups = load_json_safe(TOPUPS_FILE, [])
    found = next((t for t in topups if t.get('id') == tid), None)
    if not found:
        return jsonify({"ok": False, "reason": "not_found"}), 404
    if found.get('status') == 'paid':
        return jsonify({"ok": False, "reason": "already_paid"}), 400
    found['status'] = 'paid'
    found['paid_at'] = datetime.utcnow().isoformat() + "Z"
    found.setdefault('payment', {})['manual_code_verified'] = True
    found.setdefault('handled_by_admin', admin_ident)
    uid = found.get('user',{}).get('id')
    if uid:
        rec = get_user_record(uid)
        rec['balance'] = round(float(rec.get('balance',0.0)) + float(found.get('amount',0.0)), 2)
        save_user_record(uid, rec)
    save_json(TOPUPS_FILE, topups)
    socketio.emit('admin_topup_marked_paid', found)
    return jsonify({"ok": True, "topup": found})

@app.route('/api/admin/incoming_topups/<tid>/refund', methods=['POST'])
@require_admin_token
def api_admin_incoming_refund(tid):
    data = request.get_json() or {}
    reason = (data.get('reason') or "").strip()
    token = get_token_from_request(request)
    ok, payload = verify_admin_token(token)
    admin_ident = {}
    if ok and isinstance(payload, dict):
        admin_ident = {"uid": payload.get("uid"), "username": payload.get("username")}
    topups = load_json_safe(TOPUPS_FILE, [])
    found = next((t for t in topups if t.get('id') == tid), None)
    if not found:
        return jsonify({"ok": False, "reason": "not_found"}), 404
    if found.get('status') == 'refunded':
        return jsonify({"ok": False, "reason": "already_refunded"}), 400
    found['status'] = 'refunded'
    found['refunded_at'] = datetime.utcnow().isoformat() + "Z"
    found.setdefault('refund_info', {})['admin'] = admin_ident
    if reason: found.setdefault('refund_info', {})['reason'] = reason
    save_json(TOPUPS_FILE, topups)
    socketio.emit('admin_topup_refunded', found)
    return jsonify({"ok": True, "topup": found})

@app.route('/api/admin/incoming_topups/<tid>/force_match', methods=['POST'])
@require_admin_token
def api_admin_incoming_force_match(tid):
    data = request.get_json() or {}
    verify_code = data.get('verify_code', True)
    token = get_token_from_request(request)
    ok, payload = verify_admin_token(token)
    admin_ident = {}
    if ok and isinstance(payload, dict):
        admin_ident = {"uid": payload.get("uid"), "username": payload.get("username")}
    topups = load_json_safe(TOPUPS_FILE, [])
    found = next((t for t in topups if t.get('id') == tid), None)
    if not found:
        return jsonify({"ok": False, "reason": "not_found"}), 404
    if found.get('status') == 'paid':
        return jsonify({"ok": False, "reason": "already_paid"}), 400
    found['status'] = 'paid'
    found['paid_at'] = datetime.utcnow().isoformat() + "Z"
    found.setdefault('payment', {})['manual_code_verified'] = bool(verify_code)
    found.setdefault('handled_by_admin', admin_ident)
    uid = found.get('user',{}).get('id')
    if uid:
        rec = get_user_record(uid)
        rec['balance'] = round(float(rec.get('balance',0.0)) + float(found.get('amount',0.0)), 2)
        save_user_record(uid, rec)
    save_json(TOPUPS_FILE, topups)
    socketio.emit('admin_topup_forced', found)
    return jsonify({"ok": True, "topup": found})

# ---------- Start server ----------
if __name__ == '__main__':
    port = int(os.environ.get("PORT", "8080"))
    logger.info("Starting server on port %s", port)
    socketio.run(app, host='0.0.0.0', port=port)
