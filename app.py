#!/usr/bin/env python3
# coding: utf-8
"""
Improved app.py:
- keeps your previous logic
- adds /api/check-url (requested)
- adds some Telegram handlers (commands /start,/balance,/withdraw,/tasks, inline query)
- generates QR base64 for SBP-pay link
- emits socket events on new topup/withdraw/review
"""
import os
import time
import json
import random
import string
import logging
import base64
import io
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from flask_socketio import SocketIO, emit
import qrcode

# Optional telegram libs
try:
    import telebot
    from telebot import types as tb_types
    import requests
except Exception:
    telebot = None
    requests = None

try:
    import jwt
except Exception:
    jwt = None

# ---- Config ----
PORT = int(os.environ.get("PORT", 8080))
DATA_DIR = os.environ.get("DATA_DIR", ".rc_data")
os.makedirs(DATA_DIR, exist_ok=True)

USERS_FILE = os.path.join(DATA_DIR, "users.json")
TOPUPS_FILE = os.path.join(DATA_DIR, "topups.json")
WITHDRAWS_FILE = os.path.join(DATA_DIR, "withdraws.json")
TASKS_FILE = os.path.join(DATA_DIR, "tasks.json")
REVIEWS_FILE = os.path.join(DATA_DIR, "reviews.json")
TASK_TYPES_FILE = os.path.join(DATA_DIR, "task_types.json")
ADMINS_FILE = os.path.join(DATA_DIR, "admins.json")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "@ReviewCashNews")
ADMIN_USER_IDS = [s.strip() for s in os.environ.get("ADMIN_USER_IDS","").split(",") if s.strip()]
ADMIN_JWT_SECRET = os.environ.get("ADMIN_JWT_SECRET", "replace_with_strong_secret")
MIN_TOPUP = int(os.environ.get("MIN_TOPUP", "150"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("reviewcash")

# ---- helpers ----
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
    arr.insert(0, obj)
    save_json(path, arr)

def gen_id(prefix="id"):
    return f"{prefix}_{int(time.time()*1000)}_{random.randint(1000,9999)}"

def gen_manual_code():
    return "RC" + ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))

def generate_qr_base64(payload_url: str):
    """Generates a QR PNG and returns data:image/png;base64,... (no personal data)"""
    img = qrcode.make(payload_url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    return "data:image/png;base64," + b64

# Ensure default files exist
if not os.path.exists(TASK_TYPES_FILE):
    save_json(TASK_TYPES_FILE, [
        {"id":"ya_review","name":"Отзыв — Яндекс Карты","unit_price":120},
        {"id":"gmaps_review","name":"Отзыв — Google Maps","unit_price":65},
        {"id":"tg_sub","name":"Подписка — Telegram канал","unit_price":10},
    ])
for f, default in [
    (USERS_FILE, {}), (TOPUPS_FILE, []), (WITHDRAWS_FILE, []),
    (TASKS_FILE, []), (REVIEWS_FILE, []), (ADMINS_FILE, {})
]:
    if not os.path.exists(f):
        save_json(f, default)

# ---- Flask + SocketIO ----
app = Flask(__name__, static_folder='public', static_url_path='/')
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# ---- domain logic ----
def get_user(uid):
    users = load_json(USERS_FILE, {})
    key = str(uid)
    if key not in users:
        users[key] = {"balance": 0.0, "history": [], "tasks_done": 0, "total_earned": 0.0, "first_name": None, "username": None, "photo_url": None}
        save_json(USERS_FILE, users)
    return users[key]

def update_user_balance(uid, amount, history_item=None):
    users = load_json(USERS_FILE, {})
    key = str(uid)
    if key not in users:
        users[key] = {"balance":0.0, "history": [], "tasks_done":0}
    users[key]["balance"] = round(users[key].get("balance",0.0) + float(amount), 2)
    if history_item:
        history_item["ts"] = datetime.utcnow().isoformat()+"Z"
        users[key].setdefault("history", []).insert(0, history_item)
        users[key]["history"] = users[key]["history"][:100]
    save_json(USERS_FILE, users)
    socketio.emit("user_update", {"user_id": key, "balance": users[key]["balance"]}, broadcast=True)
    return users[key]

def get_unit_price_for_type(tid):
    types = load_json(TASK_TYPES_FILE, [])
    for t in types:
        if t["id"] == tid:
            return float(t.get("unit_price", 0))
    return 0.0

# ---- Telegram features (optional) ----
if telebot and BOT_TOKEN:
    bot = telebot.TeleBot(BOT_TOKEN)
    logger.info("Telebot configured")
    # /start: welcome + checksub button + "open webapp"
    @bot.message_handler(commands=['start'])
    def _start(m):
        uid = m.from_user.id
        kb = tb_types.InlineKeyboardMarkup()
        kb.add(tb_types.InlineKeyboardButton("Перейти в канал", url=f"https://t.me/{CHANNEL_ID.lstrip('@')}"))
        kb.add(tb_types.InlineKeyboardButton("Открыть приложение", web_app=tb_types.WebAppInfo(url=f"{os.environ.get('WEBAPP_URL','')}/index.html" if os.environ.get('WEBAPP_URL') else "/index.html")))
        bot.send_message(uid, "Добро пожаловать в ReviewCash — нажмите кнопку чтобы открыть WebApp или подписаться на канал.", reply_markup=kb)

    # /balance: quick balance + open app
    @bot.message_handler(commands=['balance'])
    def _balance(m):
        uid = str(m.from_user.id)
        u = get_user(uid)
        txt = f"Баланс: {u.get('balance',0):.2f} ₽"
        kb = tb_types.InlineKeyboardMarkup()
        kb.add(tb_types.InlineKeyboardButton("Открыть WebApp", web_app=tb_types.WebAppInfo(url=f"{os.environ.get('WEBAPP_URL','')}/index.html" if os.environ.get('WEBAPP_URL') else "/index.html")))
        bot.send_message(m.chat.id, txt, reply_markup=kb)

    @bot.message_handler(commands=['tasks'])
    def _tasks(m):
        kb = tb_types.InlineKeyboardMarkup()
        kb.add(tb_types.InlineKeyboardButton("Открыть задания", web_app=tb_types.WebAppInfo(url=f"{os.environ.get('WEBAPP_URL','')}/index.html" if os.environ.get('WEBAPP_URL') else "/index.html")))
        bot.send_message(m.chat.id, "Открой задания:", reply_markup=kb)

    @bot.message_handler(commands=['withdraw'])
    def _withdraw(m):
        kb = tb_types.InlineKeyboardMarkup()
        kb.add(tb_types.InlineKeyboardButton("Открыть форму вывода", web_app=tb_types.WebAppInfo(url=f"{os.environ.get('WEBAPP_URL','')}/index.html" if os.environ.get('WEBAPP_URL') else "/index.html")))
        bot.send_message(m.chat.id, "Открыть форму вывода средств:", reply_markup=kb)

    # inline query — quick results (simple)
    @bot.inline_handler(lambda query: True)
    def inline_query(inline_query):
        q = inline_query.query or "ReviewCash"
        results = []
        # Article: open webapp
        content = tb_types.InputTextMessageContent(f"Откройте ReviewCash — {q}")
        r = tb_types.InlineQueryResultArticle(id="open_app", title="Открыть ReviewCash", input_message_content=content)
        # attach web_app button via reply_markup when user taps article (users will see a message with button)
        kb = tb_types.InlineKeyboardMarkup()
        kb.add(tb_types.InlineKeyboardButton("Открыть приложение", web_app=tb_types.WebAppInfo(url=f"{os.environ.get('WEBAPP_URL','')}/index.html" if os.environ.get('WEBAPP_URL') else "/index.html")))
        r.reply_markup = kb
        results.append(r)
        try:
            bot.answer_inline_query(inline_query.id, results, cache_time=1)
        except Exception as e:
            logger.warning("inline answer failed: %s", e)

    # small helper: notify admins/channel
    def notify_channel(text):
        try:
            if CHANNEL_ID:
                bot.send_message(CHANNEL_ID, text, disable_web_page_preview=True)
        except Exception as e:
            logger.warning("notify_channel failed: %s", e)
else:
    bot = None

# ---- API: user-facing ----
@app.route('/api/tasks/list')
def tasks_list():
    tasks = load_json(TASKS_FILE, [])
    active = [t for t in tasks if t.get("status","active") == "active"]
    return jsonify({"ok": True, "tasks": active})

@app.route('/api/tasks/create', methods=['POST'])
def tasks_create():
    data = request.json or {}
    title = data.get("title","Untitled")
    desc = data.get("description","")
    unit_price = float(data.get("unit_price") or data.get("reward") or 0)
    qty = int(data.get("qty",1))
    t = {
        "id": gen_id("tsk"),
        "title": title,
        "description": desc,
        "unit_price": unit_price,
        "qty": qty,
        "count": 0,
        "budget": round(unit_price * qty,2),
        "status": "active",
        "created_at": datetime.utcnow().isoformat()+"Z",
        "type_id": data.get("type_id","custom")
    }
    append_json(TASKS_FILE, t)
    socketio.emit("task_update", {"type":"new_task", "task": t}, broadcast=True)
    # notify channel/admins
    if bot:
        try:
            notify_text = f"Новый таск: {t['title']} — {t['unit_price']} ₽ × {t['qty']}"
            notify_channel(notify_text)
        except:
            pass
    return jsonify({"ok": True, "task": t})

@app.route('/api/profile_me')
def api_profile_me():
    uid = request.args.get("uid")
    if not uid:
        return jsonify({"ok": False, "errmsg":"uid required"}), 400
    user = get_user(uid)
    return jsonify({"ok": True, "user": user})

@app.route('/api/check-url')
def api_check_url():
    """
    Check url reachability (simple GET). This route is used by the WebApp to verify provided links.
    """
    url = request.args.get("url","").strip()
    if not url:
        return jsonify({"ok": False, "errmsg": "empty"}), 400
    if not (url.startswith("http://") or url.startswith("https://")):
        return jsonify({"ok": False, "errmsg": "invalid"})
    if requests is None:
        return jsonify({"ok": False, "errmsg": "requests not available"}), 500
    try:
        r = requests.get(url, timeout=4, allow_redirects=True)
        return jsonify({"ok": r.status_code < 400, "status_code": r.status_code})
    except Exception:
        return jsonify({"ok": False}), 200

# topup link -> returns qr_base64 + pay_link + manual_code
@app.route('/api/user/topup-link', methods=['POST'])
def api_topup_link():
    data = request.json or {}
    uid = data.get("uid")
    amount = float(data.get("amount", 0))
    if not uid or amount < MIN_TOPUP:
        return jsonify({"ok": False, "errmsg": f"Min topup {MIN_TOPUP}"}), 400
    manual_code = gen_manual_code()
    topup = {
        "id": gen_id("tup"),
        "uid": str(uid),
        "amount": amount,
        "manual_code": manual_code,
        "status": "waiting_for_payment",
        "created_at": datetime.utcnow().isoformat()+"Z"
    }
    append_json(TOPUPS_FILE, topup)
    pay_link = f"https://pay.mock/sbp?code={manual_code}&amount={int(amount)}"
    qr_b64 = generate_qr_base64(pay_link)
    socketio.emit("new_topup", topup, broadcast=True)
    if bot:
        try:
            notify_channel(f"Новая заявка на пополнение: {amount} ₽ (код {manual_code})")
        except:
            pass
    return jsonify({"ok": True, "topup": topup, "manual_code": manual_code, "pay_link": pay_link, "qr_base64": qr_b64})

@app.route('/api/user/topup-confirm', methods=['POST'])
def api_topup_confirm():
    data = request.json or {}
    topup_id = data.get("topup_id") or data.get("topupId") or data.get("id")
    uid = data.get("uid")
    topups = load_json(TOPUPS_FILE, [])
    item = next((x for x in topups if x["id"] == topup_id), None)
    if not item:
        return jsonify({"ok": False, "errmsg":"topup not found"}), 404
    item["status"] = "pending"
    item["confirmed_by_user_at"] = datetime.utcnow().isoformat()+"Z"
    save_json(TOPUPS_FILE, topups)
    socketio.emit("new_topup_waiting", item, broadcast=True)
    return jsonify({"ok": True, "topup": item})

@app.route('/api/user/topup', methods=['POST'])
def api_user_topup_simple():
    data = request.json or {}
    uid = data.get("uid")
    amount = float(data.get("amount", 0))
    if not uid or amount < MIN_TOPUP:
        return jsonify({"ok": False, "errmsg":"invalid"}), 400
    top = {
        "id": gen_id("topup"),
        "user": {"id": str(uid)},
        "amount": amount,
        "status": "pending",
        "manual_code": data.get("manual_code"),
        "created_at": datetime.utcnow().isoformat()+"Z"
    }
    append_json(TOPUPS_FILE, top)
    socketio.emit("new_topup", top, broadcast=True)
    if bot:
        try: notify_channel(f"Попытка пополнения: {amount} ₽ (пользователь {uid})") 
        except: pass
    return jsonify({"ok": True, "topup": top})

@app.route('/api/user/withdraw', methods=['POST'])
def api_user_withdraw():
    data = request.json or {}
    uid = str(data.get("uid"))
    amount = float(data.get("amount", 0))
    name = data.get("name", "")
    details = data.get("details", "")
    if amount < 300:
        return jsonify({"ok": False, "errmsg":"min 300"}), 400
    u = get_user(uid)
    if u["balance"] < amount:
        return jsonify({"ok": False, "errmsg":"no funds"}), 400
    update_user_balance(uid, -amount, {"type":"withdraw_reserve", "amount": amount})
    wd = {
        "id": gen_id("wd"),
        "user": {"id": uid},
        "amount": amount,
        "name": name,
        "details": details,
        "status": "pending",
        "created_at": datetime.utcnow().isoformat()+"Z"
    }
    append_json(WITHDRAWS_FILE, wd)
    socketio.emit("new_withdraw", wd, broadcast=True)
    if bot:
        try: notify_channel(f"Новая заявка на вывод: {amount} ₽ от {uid}")
        except: pass
    return jsonify({"ok": True, "withdraw": wd})

# ---- Admin endpoints used by mainadmin.html (unchanged behaviour) ----
@app.route('/api/admin/dashboard')
def api_admin_dashboard():
    users = load_json(USERS_FILE, {})
    topups = load_json(TOPUPS_FILE, [])
    withdraws = load_json(WITHDRAWS_FILE, [])
    tasks = load_json(TASKS_FILE, [])
    total_revenue = sum([t.get("amount",0) for t in topups if t.get("status") in ("approved","completed")])
    pending_count = len([x for x in topups + withdraws if x.get("status") in ("pending","waiting_for_payment")])
    recent = (topups[:5] + withdraws[:5])[:10]
    return jsonify({
        "ok": True,
        "data": {
            "usersCount": len(users),
            "totalRevenue": round(total_revenue,2),
            "tasksCount": len(tasks),
            "pendingCount": pending_count,
            "recentActivity": recent
        }
    })

@app.route('/api/admin/users')
def api_admin_users():
    users = load_json(USERS_FILE, {})
    arr = []
    for uid, u in users.items():
        arr.append({
            "id": uid,
            "first_name": u.get("first_name"),
            "username": u.get("username"),
            "balance": u.get("balance",0),
            "tasks_done": u.get("tasks_done",0)
        })
    return jsonify({"ok": True, "users": arr})

@app.route('/api/admin/tasks')
def api_admin_tasks():
    tasks = load_json(TASKS_FILE, [])
    return jsonify({"ok": True, "tasks": tasks})

@app.route('/api/admin/topups')
def api_admin_topups():
    status = request.args.get("status")
    comment = request.args.get("comment")
    items = load_json(TOPUPS_FILE, [])
    if status:
        items = [x for x in items if x.get("status")==status]
    if comment:
        items = [x for x in items if comment in (x.get("manual_code") or "")]
    return jsonify({"ok": True, "items": items})

@app.route('/api/admin/withdraws')
def api_admin_withdraws():
    status = request.args.get("status")
    userq = request.args.get("user")
    items = load_json(WITHDRAWS_FILE, [])
    if status:
        items = [x for x in items if x.get("status")==status]
    if userq:
        items = [x for x in items if str(x.get("user",{}).get("id",""))==userq or userq in str(x.get("name",""))]
    return jsonify({"ok": True, "items": items})

@app.route('/api/admin/topups/<tup_id>/approve', methods=['POST'])
def api_admin_topup_approve(tup_id):
    topups = load_json(TOPUPS_FILE, [])
    item = next((x for x in topups if x["id"]==tup_id), None)
    if not item:
        return jsonify({"ok": False, "errmsg":"not found"}), 404
    item["status"] = "approved"
    save_json(TOPUPS_FILE, topups)
    uid = item.get("uid") or (item.get("user") or {}).get("id")
    if uid:
        update_user_balance(str(uid), float(item.get("amount",0)), {"type":"topup", "note":"admin_approve", "amount": item.get("amount",0)})
    socketio.emit("topup_approved", {"id": tup_id, "item": item}, broadcast=True)
    return jsonify({"ok": True})

@app.route('/api/admin/topups/<tup_id>/reject', methods=['POST'])
def api_admin_topup_reject(tup_id):
    topups = load_json(TOPUPS_FILE, [])
    item = next((x for x in topups if x["id"]==tup_id), None)
    if not item:
        return jsonify({"ok": False, "errmsg":"not found"}), 404
    item["status"] = "rejected"
    save_json(TOPUPS_FILE, topups)
    socketio.emit("topup_rejected", {"id": tup_id, "item": item}, broadcast=True)
    return jsonify({"ok": True})

@app.route('/api/admin/withdraws/<wd_id>/approve', methods=['POST'])
def api_admin_withdraw_approve(wd_id):
    withdraws = load_json(WITHDRAWS_FILE, [])
    item = next((x for x in withdraws if x["id"]==wd_id), None)
    if not item:
        return jsonify({"ok": False, "errmsg":"not found"}), 404
    if item.get("status") != "pending":
        return jsonify({"ok": False, "errmsg":"already processed"}), 400
    item["status"] = "approved"
    save_json(WITHDRAWS_FILE, withdraws)
    socketio.emit("withdraw_approved", {"id": wd_id, "item": item}, broadcast=True)
    return jsonify({"ok": True})

@app.route('/api/admin/withdraws/<wd_id>/reject', methods=['POST'])
def api_admin_withdraw_reject(wd_id):
    withdraws = load_json(WITHDRAWS_FILE, [])
    item = next((x for x in withdraws if x["id"]==wd_id), None)
    if not item:
        return jsonify({"ok": False, "errmsg":"not found"}), 404
    uid = (item.get("user") or {}).get("id")
    if uid:
        update_user_balance(str(uid), float(item.get("amount",0)), {"type":"withdraw_rejected", "amount": item.get("amount",0)})
    item["status"] = "rejected"
    save_json(WITHDRAWS_FILE, withdraws)
    socketio.emit("withdraw_rejected", {"id": wd_id, "item": item}, broadcast=True)
    return jsonify({"ok": True})

@app.route('/api/admin/task-create', methods=['POST'])
def api_admin_task_create():
    data = request.json or {}
    t = {
        "id": gen_id("tsk"),
        "title": data.get("title","Task"),
        "desc": data.get("desc",""),
        "reward": float(data.get("reward") or 0),
        "url": data.get("url") or "",
        "status": "active",
        "created_at": datetime.utcnow().isoformat()+"Z"
    }
    append_json(TASKS_FILE, t)
    socketio.emit("task_update", {"type":"new_task","task": t}, broadcast=True)
    return jsonify({"ok": True, "task": t})

# ---- Reviews / moderator endpoints ----
@app.route('/api/reviews/submit', methods=['POST'])
def submit_review():
    data = request.json or {}
    user_id = str(data.get('user_id',''))
    task_type = data.get('task_type','')
    title = data.get('title','Задание')
    site_name = data.get('site_name')
    review_url = data.get('review_url')
    proof_type = data.get('proof_type')
    proof_data = data.get('proof_data')
    review = {
        "id": gen_id("rev"),
        "user_id": user_id,
        "title": title,
        "task_type": task_type,
        "site_name": site_name,
        "review_url": review_url,
        "proof_type": proof_type,
        "proof_data": proof_data,
        "created_at": datetime.utcnow().isoformat()+"Z",
        "status": "pending"
    }
    # Auto-check tg_sub:
    if task_type == "tg_sub":
        subscribed = False
        if requests and BOT_TOKEN:
            try:
                resp = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getChatMember", params={"chat_id": CHANNEL_ID, "user_id": user_id}, timeout=3).json()
                subscribed = resp.get("ok") and resp["result"]["status"] in ("member","administrator","creator")
            except Exception:
                subscribed = False
        if subscribed:
            reward = get_unit_price_for_type("tg_sub") or 10
            review["status"] = "approved"
            review["reward"] = reward
            update_user_balance(user_id, reward, {"type":"tg_sub","amount":reward})
        else:
            review["status"] = "rejected"
            review["reject_reason"] = "not_subscribed"
    append_json(REVIEWS_FILE, review)
    socketio.emit("new_review", review, broadcast=True)
    if bot:
        try:
            notify_channel(f"Новая заявка (review): {title} — {task_type}")
        except:
            pass
    return jsonify({"ok": True, "review": review, "status": review["status"]})

@app.route('/api/moderator/queue')
def mod_queue():
    items = load_json(REVIEWS_FILE, [])
    pending = [it for it in items if it.get("status") == "pending" and it.get("task_type") != "tg_sub"]
    if not pending:
        return jsonify({"ok": True, "assignment": None, "queue_length": 0})
    a = pending[0]
    return jsonify({"ok": True, "assignment": {
        "id": a["id"],
        "user_id": a["user_id"],
        "task_title": a.get("title"),
        "task_type": a.get("task_type"),
        "review_target": a.get("review_url"),
        "site_name": a.get("site_name"),
        "proof_type": a.get("proof_type"),
        "proof_data": a.get("proof_data")
    }, "queue_length": len(pending)})

@app.route('/api/moderator/me')
def mod_me():
    uid = request.args.get("uid", "mod")
    data = load_json(ADMINS_FILE, {})
    u = data.get(str(uid), {"name": "Модератор", "tasks_reviewed": 0})
    return jsonify({"ok": True, "name": u.get("name"), "tasks_reviewed": u.get("tasks_reviewed", 0)})

@app.route('/api/moderator/approve', methods=['POST'])
def mod_approve():
    data = request.json or {}
    rid = data.get("id")
    items = load_json(REVIEWS_FILE, [])
    for it in items:
        if it["id"] == rid and it.get("status") == "pending":
            it["status"] = "approved"
            reward = it.get("reward") or get_unit_price_for_type(it.get("task_type","")) or 10
            save_json(REVIEWS_FILE, items)
            update_user_balance(it["user_id"], reward, {"type":"review","amount":reward})
            socketio.emit("review_approved", {"id": rid, "review": it}, broadcast=True)
            return jsonify({"ok": True})
    return jsonify({"ok": False}), 404

@app.route('/api/moderator/reject', methods=['POST'])
def mod_reject():
    data = request.json or {}
    rid = data.get("id")
    reason = data.get("reason", "Не выполнено")
    items = load_json(REVIEWS_FILE, [])
    for it in items:
        if it["id"] == rid and it.get("status") == "pending":
            it["status"] = "rejected"
            it["reject_reason"] = reason
            save_json(REVIEWS_FILE, items)
            socketio.emit("review_rejected", {"id": rid, "review": it}, broadcast=True)
            return jsonify({"ok": True})
    return jsonify({"ok": False}), 404

# ---- static route passthrough ----
@app.route('/')
def index_page():
    return send_from_directory('public', 'index.html')

@app.route('/<path:path>')
def static_proxy(path):
    return send_from_directory('public', path)

# ---- sockets ----
@socketio.on('connect')
def on_connect():
    logger.info("Socket connected: %s", request.sid)
    emit("hello", {"msg":"connected"})

if __name__ == '__main__':
    if telebot and bot:
        import threading
        threading.Thread(target=bot.infinity_polling, daemon=True).start()
    logger.info("Starting server on port %s", PORT)
    socketio.run(app, host='0.0.0.0', port=PORT, allow_unsafe_werkzeug=True)
