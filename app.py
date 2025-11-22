#!/usr/bin/env python3
# coding: utf-8
"""
ReviewCash — complete server for SBP-only flow (manual-code + QR), admin tools, and webhook matching.
- No external payment provider integration required: uses payment link template + public QR generator.
- Generates manual RC codes for each topup that user must enter in "Комментарий к переводу".
- Minimal topup amount enforced (100 ₽).
- Webhook endpoint supports matching:
    * by order_id (topup id) if provider sends it
    * by manual_code contained in webhook comment + amount equality
    * if a single pending topup matches amount, webhook is recorded to that candidate
- Admin endpoints:
    GET  /api/admin/incoming_topups?status=&comment=&manual_only=
    POST /api/admin/incoming_topups/<tid>/mark_paid
    POST /api/admin/incoming_topups/<tid>/force_match
    POST /api/admin/incoming_topups/<tid>/refund
- Serves public/index.html (user UI) and public/mainadmin.html (admin UI).
- Pre-filled config values from your request; you can override them via environment variables.
"""
import os
import time
import json
import logging
import hmac
import hashlib
import random
import string
from datetime import datetime
from urllib.parse import quote_plus, urlparse, parse_qsl

import eventlet
try:
    eventlet.monkey_patch(dns=False)
except TypeError:
    eventlet.monkey_patch()

from flask import Flask, request, jsonify, send_from_directory, abort
from flask_socketio import SocketIO

# Optional libraries
try:
    import requests
except Exception:
    requests = None

try:
    import jwt
except Exception:
    jwt = None

# ---------------------
# Logging
# ---------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("reviewcash")

# ---------------------
# Configuration (prefilled; override with env variables)
# ---------------------
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
TASK_TYPES_FILE = os.path.join(DATA_DIR, "task_types.json")
ADMINS_FILE = os.path.join(DATA_DIR, "admins.json")
ADMIN_TOKENS_FILE = os.path.join(DATA_DIR, "admin_tokens.json")

# Payment config: we use a link template to create a payment page (users pay via bank app SBP/QR)
PAYMENT_PROVIDER = os.environ.get("PAYMENT_PROVIDER", "link_template")
PAYMENT_LINK_TEMPLATE = os.environ.get("PAYMENT_LINK_TEMPLATE", "")  # optional, fallback used if empty
PAYMENT_WEBHOOK_SECRET = os.environ.get("PAYMENT_WEBHOOK_SECRET", "").encode()  # optional HMAC secret for webhook verification

MIN_TOPUP = int(os.environ.get("MIN_TOPUP", "100"))

# ---------------------
# Storage helpers
# ---------------------
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

# Ensure files exist with sensible defaults
load_json_safe(TOPUPS_FILE, [])
load_json_safe(USERS_FILE, {})
load_json_safe(TASK_TYPES_FILE, [
    {"id": "ya_review", "name": "Отзыв — Я.К.", "unit_price": 100},
    {"id": "gmaps_review", "name": "Отзыв — Google Maps", "unit_price": 65},
    {"id": "tg_sub", "name": "Подписка — Telegram канал", "unit_price": 10}
])
load_json_safe(ADMINS_FILE, [])
load_json_safe(ADMIN_TOKENS_FILE, [])

# ---------------------
# Utilities
# ---------------------
def gen_id(prefix="id"):
    return f"{prefix}_{int(time.time()*1000)}_{random.randint(1000,9999)}"

def gen_manual_code():
    # RC + 6 alnum characters
    return "RC" + ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))

def get_user_record(uid):
    users = load_json_safe(USERS_FILE, {})
    key = str(uid)
    if key not in users:
        users[key] = {"balance": 0.0, "tasks_done": 0, "total_earned": 0.0}
        save_json(USERS_FILE, users)
    return users[key]

def save_user_record(uid, rec):
    users = load_json_safe(USERS_FILE, {})
    users[str(uid)] = rec
    save_json(USERS_FILE, users)

# ---------------------
# Admin token helpers (JWT)
# ---------------------
def create_admin_jwt(payload):
    if jwt is None:
        raise RuntimeError("PyJWT not installed")
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
        if uid and uid in ADMIN_USER_IDS:
            return True, payload
        if username and username in ADMIN_USERNAMES:
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

# ---------------------
# Payment invoice creation (SBP flow)
# ---------------------
def create_payment_invoice(amount, order_id, description="Topup"):
    """
    Creates a payment URL and QR code. For SBP-only flow we rely on a simple payment link
    or a placeholder. In production you can replace PAYMENT_LINK_TEMPLATE with a real page.
    Returns dict with keys 'url' and 'qr'
    """
    amount = float(amount)
    if PAYMENT_PROVIDER == "link_template" and PAYMENT_LINK_TEMPLATE:
        url = PAYMENT_LINK_TEMPLATE.format(amount=int(amount), order_id=quote_plus(order_id))
    else:
        # Fallback simple link (not a payment processor) — user will use bank app and manual code
        url = f"https://example.com/pay?amount={int(amount)}&order_id={quote_plus(order_id)}"
    qr = f"https://api.qrserver.com/v1/create-qr-code/?size=400x400&data={quote_plus(url)}"
    return {"url": url, "qr": qr}

# ---------------------
# Flask app + socket
# ---------------------
app = Flask(__name__, static_folder="public", static_url_path="/")
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

# ---------------------
# Routes: UI files
# ---------------------
@app.route("/")
def index_route():
    return send_from_directory("public", "index.html")

@app.route("/mainadmin")
def mainadmin_route():
    return send_from_directory("public", "mainadmin.html")

@app.route("/<path:path>")
def static_files(path):
    return send_from_directory("public", path)

@app.route("/health")
def health():
    return jsonify({"ok": True, "ts": datetime.utcnow().isoformat()+"Z"})

# ---------------------
# Topup creation (public)
# ---------------------
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
        # try to parse minimal id
        try:
            pairs = [p.split("=",1) for p in init_data.split("&") if "=" in p]
            params = {k:v for k,v in pairs}
            uid = params.get("id") or params.get("user_id")
            if uid:
                user = {"id": uid}
        except Exception:
            pass

    topup_id = gen_id("top")
    manual_code = gen_manual_code()
    topup = {
        "id": topup_id,
        "user": user,
        "amount": round(amount, 2),
        "status": "pending",
        "created_at": datetime.utcnow().isoformat()+"Z",
        "manual_code": manual_code,
        "payment": {},
        "note_nonrefundable": True,
        "instruction_text": f"В поле «Комментарий к переводу» у банка введите код {manual_code} (обязательно)."
    }

    try:
        topup["payment"] = create_payment_invoice(amount, topup_id, description=f"Topup {topup_id}")
    except Exception as e:
        logger.exception("create_payment_invoice failed: %s", e)
        topup["payment_error"] = str(e)

    append_json(TOPUPS_FILE, topup)
    socketio.emit("new_topup", topup)
    return jsonify({"ok": True, "topup": topup, "manual_code": manual_code, "instruction": topup["instruction_text"]})

# ---------------------
# Get topup
# ---------------------
@app.route("/api/topups/<tid>", methods=["GET"])
def api_get_topup(tid):
    arr = load_json_safe(TOPUPS_FILE, [])
    found = next((t for t in arr if t.get("id") == tid), None)
    if not found:
        return jsonify({"ok": False, "reason": "not_found"}), 404
    return jsonify({"ok": True, "topup": found})

# ---------------------
# Webhook endpoint: match by order_id or manual_code+amount
# ---------------------
@app.route("/api/payment_webhook", methods=["POST"])
def api_payment_webhook():
    # verify signature if PAYMENT_WEBHOOK_SECRET configured
    if PAYMENT_WEBHOOK_SECRET and request.data:
        sig = request.headers.get("X-Signature") or request.headers.get("X-Hub-Signature") or ""
        if not sig:
            logger.warning("Webhook missing signature header")
            return "signature required", 400
        mac = hmac.new(PAYMENT_WEBHOOK_SECRET, request.data, digestmod=hashlib.sha256).hexdigest()
        if not hmac.compare_digest(mac, sig):
            logger.warning("Webhook invalid signature")
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
            try:
                amount = float(payload.get("Amount"))
            except Exception:
                amount = None

    paid = False
    if isinstance(status, str) and status.lower() in ("paid","success","complete","confirmed","ok"):
        paid = True
    if isinstance(status, bool) and status is True:
        paid = True

    topups = load_json_safe(TOPUPS_FILE, [])
    matched = False

    # 1) match by order_id
    if order_id:
        for t in topups:
            if t.get("id") == str(order_id):
                if paid and t.get("status") != "paid":
                    t["status"] = "paid"
                    t["paid_at"] = datetime.utcnow().isoformat()+"Z"
                    if comment:
                        t.setdefault("payment", {})["comment"] = comment
                        t.setdefault("payment", {})["manual_code_verified"] = (t.get("manual_code") in comment)
                    uid = t.get("user", {}).get("id")
                    if uid:
                        rec = get_user_record(uid)
                        rec["balance"] = round(float(rec.get("balance",0.0)) + float(t.get("amount",0.0)), 2)
                        save_user_record(uid, rec)
                    matched = True
                break

    # 2) match by manual_code in comment + amount equality
    if not matched and comment:
        for t in topups:
            if t.get("status") != "pending":
                continue
            code = t.get("manual_code")
            if not code:
                continue
            if code in comment:
                if amount is not None:
                    if abs(float(t.get("amount",0)) - float(amount)) > 0.001:
                        logger.info("Code matched but amount mismatch (topup %s: %s vs webhook %s)", t.get("id"), t.get("amount"), amount)
                        continue
                t["status"] = "paid"
                t["paid_at"] = datetime.utcnow().isoformat()+"Z"
                t.setdefault("payment", {})["comment"] = comment
                t.setdefault("payment", {})["manual_code_verified"] = True
                uid = t.get("user", {}).get("id")
                if uid:
                    rec = get_user_record(uid)
                    rec["balance"] = round(float(rec.get("balance",0.0)) + float(t.get("amount",0.0)), 2)
                    save_user_record(uid, rec)
                matched = True
                logger.info("Auto-matched webhook by manual code %s -> topup %s", code, t.get("id"))
                break

    # 3) if still not matched but amount unique among pending, save webhook payload into that candidate for manual inspection
    if not matched and amount is not None:
        candidates = [t for t in topups if t.get("status") == "pending" and abs(float(t.get("amount",0)) - float(amount)) < 0.001]
        if len(candidates) == 1:
            t = candidates[0]
            t.setdefault("payment", {})["last_webhook"] = payload
            t.setdefault("payment", {})["manual_code_verified"] = False
            save_json(TOPUPS_FILE, topups)
            socketio.emit("topup_updated", t)
            logger.info("Recorded webhook payload to single-amount candidate topup %s", t.get("id"))
            return jsonify({"ok": True, "matched": False, "note": "recorded into candidate"})

    if matched:
        save_json(TOPUPS_FILE, topups)
        socketio.emit("topup_updated", {"source": "webhook"})
        return jsonify({"ok": True, "matched": True})

    logger.info("Webhook processed but no match (order_id=%s, comment=%s, amount=%s)", order_id, comment, amount)
    return jsonify({"ok": True, "matched": False})

# ---------------------
# Admin endpoints for incoming transfers
# ---------------------
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
            if not t.get("manual_code"): continue
            if t.get("payment",{}).get("manual_code_verified") is True: continue
        out.append(t)
    out.sort(key=lambda x: x.get("created_at",""), reverse=True)
    return jsonify({"ok": True, "items": out})

@app.route("/api/admin/incoming_topups/<tid>/mark_paid", methods=["POST"])
@require_admin_token
def api_admin_incoming_mark_paid(tid):
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
        rec["balance"] = round(float(rec.get("balance", 0.0)) + float(found.get("amount", 0.0)), 2)
        save_user_record(uid, rec)
    save_json(TOPUPS_FILE, topups)
    socketio.emit("admin_topup_marked_paid", found)
    return jsonify({"ok": True, "topup": found})

@app.route("/api/admin/incoming_topups/<tid>/force_match", methods=["POST"])
@require_admin_token
def api_admin_incoming_force_match(tid):
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
        rec["balance"] = round(float(rec.get("balance", 0.0)) + float(found.get("amount", 0.0)), 2)
        save_user_record(uid, rec)
    save_json(TOPUPS_FILE, topups)
    socketio.emit("admin_topup_forced", found)
    return jsonify({"ok": True, "topup": found})

@app.route("/api/admin/incoming_topups/<tid>/refund", methods=["POST"])
@require_admin_token
def api_admin_incoming_refund(tid):
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

# ---------------------
# Run server
# ---------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    logger.info("Starting server on port %s", port)
    socketio.run(app, host="0.0.0.0", port=port)
