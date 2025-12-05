# app.py
import os
import sqlite3
import json
import logging
from time import time
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory, url_for
from flask_cors import CORS
from flask_socketio import SocketIO
import telebot

# --------- Configuration (environment overrides) ----------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8033069276:AAFv1-kdQ68LjvLEgLHj3ZXd5ehMqyUXOYU")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "6482440657"))
REQUIRED_CHANNEL = os.environ.get("REQUIRED_CHANNEL", "@ReviewCashNews")
WEBAPP_URL = os.environ.get("WEBAPP_URL", "https://web-production-398fb.up.railway.app")
DB_PATH = os.environ.get("DB_PATH", "data.db")
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", 8080))
USE_WEBHOOK_ON_START = True

# --------- App init ----------
app = Flask(__name__, static_folder="static", static_url_path="")
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "reviewcash_secret")
CORS(app, resources={r"/api/*": {"origins": "*"}})
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

bot = telebot.TeleBot(BOT_TOKEN, threaded=False)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("reviewcash")

# --------- DB helpers ----------
def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cur = conn.cursor()
    # users
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        first_name TEXT,
        last_name TEXT,
        username TEXT,
        balance REAL DEFAULT 0,
        role TEXT DEFAULT 'user'
    )""")
    # tasks
    cur.execute("""
    CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY,
        owner_uid TEXT,
        title TEXT,
        description TEXT,
        qty INTEGER,
        unit_price REAL,
        type_id TEXT,
        url TEXT,
        created_at TEXT,
        completed_qty INTEGER DEFAULT 0,
        status TEXT DEFAULT 'active'
    )""")
    # submissions (user performed a unit -> awaits moderation)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS submissions (
        id INTEGER PRIMARY KEY,
        task_id INTEGER,
        worker_uid TEXT,
        url TEXT,
        created_at TEXT,
        status TEXT DEFAULT 'pending',
        moderator_id TEXT,
        note TEXT
    )""")
    # topups
    cur.execute("""
    CREATE TABLE IF NOT EXISTS topups (
        id INTEGER PRIMARY KEY,
        uid TEXT,
        amount REAL,
        manual_code TEXT,
        status TEXT DEFAULT 'pending',
        created_at TEXT
    )""")
    # withdraws
    cur.execute("""
    CREATE TABLE IF NOT EXISTS withdraws (
        id INTEGER PRIMARY KEY,
        uid TEXT,
        amount REAL,
        name TEXT,
        details TEXT,
        status TEXT DEFAULT 'pending',
        created_at TEXT
    )""")
    conn.commit()
    conn.close()

init_db()

# --------- utilities ----------
def ensure_user(uid, first_name=None, last_name=None, username=None):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE id = ?", (uid,))
    row = cur.fetchone()
    if row:
        # maybe update names
        if first_name or last_name or username:
            cur.execute("UPDATE users SET first_name=?, last_name=?, username=? WHERE id=?",
                        (first_name or row["first_name"], last_name or row["last_name"], username or row["username"], uid))
            conn.commit()
        conn.close()
        return
    cur.execute("INSERT INTO users (id, first_name, last_name, username, balance) VALUES (?,?,?,?,?)",
                (uid, first_name, last_name, username, 0))
    conn.commit()
    conn.close()

def get_user(uid):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE id = ?", (uid,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None

def change_balance(uid, delta):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET balance = balance + ? WHERE id = ?", (delta, uid))
    conn.commit()
    cur.execute("SELECT balance FROM users WHERE id = ?", (uid,))
    r = cur.fetchone()
    conn.close()
    return r["balance"] if r else None

# --------- static routes ----------
@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/admin")
def admin_panel():
    return send_from_directory("static", "admin.html")

@app.route("/moderator")
def moderator_panel():
    return send_from_directory("static", "moderator.html")

@app.route("/<path:filename>")
def static_files(filename):
    return send_from_directory("static", filename)

# --------- Task types (prices: creator price and worker price) ----------
TASK_TYPES = [
    {"id":"ya_review", "name":"Отзыв — Яндекс Карты", "creator_price":120, "worker_price":100, "max_qty":500},
    {"id":"gmaps_review", "name":"Отзыв — Google Maps", "creator_price":65, "worker_price":50, "max_qty":500},
    {"id":"tg_sub", "name":"Подписка — Telegram канал", "creator_price":10, "worker_price":5, "max_qty":100000}
]

@app.get("/api/task_types")
def api_task_types():
    return jsonify(TASK_TYPES)

# --------- Profile ----------
@app.get("/api/profile_me")
def api_profile_me():
    uid = request.args.get("uid")
    if not uid:
        return jsonify({"ok": False, "errmsg":"no uid"}), 400
    # ensure exists
    ensure_user(uid)
    user = get_user(uid)
    # include recent history: last 8 submissions/topups/withdraws
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM topups WHERE uid=? ORDER BY id DESC LIMIT 8", (uid,))
    topups = [dict(x) for x in cur.fetchall()]
    cur.execute("SELECT * FROM withdraws WHERE uid=? ORDER BY id DESC LIMIT 8", (uid,))
    withdraws = [dict(x) for x in cur.fetchall()]
    cur.execute("SELECT * FROM submissions WHERE worker_uid=? ORDER BY id DESC LIMIT 8", (uid,))
    subs = [dict(x) for x in cur.fetchall()]
    conn.close()
    user_data = user.copy()
    user_data["history"] = topups + withdraws + subs
    return jsonify({"ok": True, "user": user_data})

# --------- Tasks list ----------
@app.get("/api/tasks/list")
def api_tasks_list():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM tasks WHERE status='active' OR status='inactive' ORDER BY id DESC")
    rows = cur.fetchall()
    tasks = [dict(r) for r in rows]
    conn.close()
    return jsonify({"ok": True, "tasks": tasks})

# Create task: will deduct (reserve) total from owner's balance
@app.post("/api/tasks/create")
def api_tasks_create():
    data = request.json or {}
    required = ("owner_uid","title","description","qty","type_id")
    if not all(k in data for k in required):
        return jsonify({"ok": False, "errmsg":"missing fields"}), 400
    owner_uid = str(data["owner_uid"])
    qty = int(data["qty"])
    ttype = next((x for x in TASK_TYPES if x["id"] == data["type_id"]), None)
    if not ttype:
        return jsonify({"ok": False, "errmsg":"invalid type"}), 400
    creator_unit_price = float(ttype["creator_price"])
    total = creator_unit_price * qty
    ensure_user(owner_uid)
    user = get_user(owner_uid)
    if user["balance"] < total:
        return jsonify({"ok": False, "errmsg":"insufficient_balance"}), 400
    # debit owner's balance (reserve)
    change_balance(owner_uid, -total)
    # create task
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
      INSERT INTO tasks (owner_uid,title,description,qty,unit_price,type_id,url,created_at)
      VALUES (?,?,?,?,?,?,?,?)
    """, (owner_uid, data["title"], data["description"], qty, creator_unit_price, data["type_id"], data.get("url",""), datetime.utcnow().isoformat()))
    conn.commit()
    task_id = cur.lastrowid
    cur.execute("SELECT * FROM tasks WHERE id=?", (task_id,))
    task = dict(cur.fetchone())
    conn.close()
    # notify
    socketio.emit("task_update", {"task": task}, broadcast=True)
    return jsonify({"ok": True, "task": task})

# Claim / submit a work (worker sends evidence / url). We create submission awaiting moderation.
@app.post("/api/tasks/claim")
def api_tasks_claim():
    data = request.json or {}
    task_id = data.get("task_id")
    worker_uid = str(data.get("worker_uid") or data.get("uid"))
    evidence_url = data.get("url", "")
    if not task_id or not worker_uid:
        return jsonify({"ok": False, "errmsg":"missing"}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM tasks WHERE id=?", (int(task_id),))
    task = cur.fetchone()
    if not task:
        conn.close()
        return jsonify({"ok": False, "errmsg":"task not found"}), 404
    if task["status"] != "active":
        conn.close()
        return jsonify({"ok": False, "errmsg":"task not active"}), 400
    # create submission
    cur.execute("""
      INSERT INTO submissions (task_id, worker_uid, url, created_at, status)
      VALUES (?,?,?,?,?)
    """, (task["id"], worker_uid, evidence_url, datetime.utcnow().isoformat(), "pending"))
    conn.commit()
    sub_id = cur.lastrowid
    cur.execute("SELECT * FROM submissions WHERE id=?", (sub_id,))
    sub = dict(cur.fetchone())
    conn.close()
    # notify moderator via bot
    try:
        bot.send_message(ADMIN_ID, f"Новая заявка на проверку (task:{task['id']}) от {worker_uid}. Посмотреть: {WEBAPP_URL}/moderator")
    except Exception:
        pass
    socketio.emit("new_submission", {"submission": sub}, broadcast=True)
    return jsonify({"ok": True, "submission": sub})

# Moderator: list pending submissions
@app.get("/api/moderator/submissions")
def api_moderator_submissions():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT s.*, t.title, t.type_id, t.owner_uid FROM submissions s LEFT JOIN tasks t ON s.task_id = t.id WHERE s.status='pending' ORDER BY s.id DESC")
    items = [dict(x) for x in cur.fetchall()]
    conn.close()
    return jsonify({"ok": True, "items": items})

# Moderator approves submission -> credit worker by worker_price; increment task completed_qty; mark submission approved
@app.post("/api/moderator/submissions/<int:sub_id>/approve")
def api_moderator_approve(sub_id):
    moderator_id = request.json.get("moderator_id") if request.json else None
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM submissions WHERE id=?", (sub_id,))
    sub = cur.fetchone()
    if not sub:
        conn.close(); return jsonify({"ok": False, "errmsg":"not found"}), 404
    if sub["status"] != "pending":
        conn.close(); return jsonify({"ok": False, "errmsg":"already processed"}), 400
    cur.execute("SELECT * FROM tasks WHERE id=?", (sub["task_id"],))
    task = cur.fetchone()
    if not task:
        conn.close(); return jsonify({"ok": False, "errmsg":"task not found"}), 404
    # find worker_price for task.type_id
    ttype = next((x for x in TASK_TYPES if x["id"] == task["type_id"]), None)
    worker_reward = float(ttype["worker_price"]) if ttype else float(task["unit_price"])
    # credit worker
    ensure_user(sub["worker_uid"])
    change_balance(sub["worker_uid"], worker_reward)
    # increment completed_qty
    cur.execute("UPDATE tasks SET completed_qty = completed_qty + 1 WHERE id=?", (task["id"],))
    # mark submission approved
    cur.execute("UPDATE submissions SET status=?, moderator_id=?, note=? WHERE id=?", ("approved", moderator_id or "", request.json.get("note",""), sub_id))
    # if completed >= qty -> set task inactive
    cur.execute("SELECT completed_qty, qty FROM tasks WHERE id=?", (task["id"],))
    tnow = cur.fetchone()
    if tnow and tnow["completed_qty"] >= tnow["qty"]:
        cur.execute("UPDATE tasks SET status='inactive' WHERE id=?", (task["id"],))
    conn.commit()
    conn.close()
    # notify sockets and involved users
    socketio.emit("submission_update", {"submission_id": sub_id, "status":"approved"}, broadcast=True)
    socketio.emit("task_update", {"task_id": task["id"]}, broadcast=True)
    # notify worker via bot (if chat id numeric)
    try:
        bot.send_message(int(sub["worker_uid"]), f"Ваша работа по заданию {task['title']} подтверждена. +{worker_reward} ₽ на баланс.")
    except Exception:
        pass
    return jsonify({"ok": True})

# Moderator rejects
@app.post("/api/moderator/submissions/<int:sub_id>/reject")
def api_moderator_reject(sub_id):
    moderator_id = request.json.get("moderator_id") if request.json else None
    note = request.json.get("note","")
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM submissions WHERE id=?", (sub_id,))
    sub = cur.fetchone()
    if not sub:
        conn.close(); return jsonify({"ok": False, "errmsg":"not found"}), 404
    if sub["status"] != "pending":
        conn.close(); return jsonify({"ok": False, "errmsg":"already processed"}), 400
    cur.execute("UPDATE submissions SET status=?, moderator_id=?, note=? WHERE id=?", ("rejected", moderator_id or "", note, sub_id))
    conn.commit(); conn.close()
    socketio.emit("submission_update", {"submission_id": sub_id, "status":"rejected"}, broadcast=True)
    return jsonify({"ok": True})

# --------- Topup APIs ----------
@app.post("/api/user/topup-link")
def api_topup_link():
    data = request.json or {}
    uid = data.get("uid"); amount = data.get("amount")
    if not uid or not amount:
        return jsonify({"ok": False, "errmsg":"missing"}), 400
    topup_id = int(time()*1000)
    manual_code = f"RC-{topup_id}"
    conn = get_db(); cur = conn.cursor()
    cur.execute("INSERT INTO topups (id, uid, amount, manual_code, status, created_at) VALUES (?,?,?,?,?,?)",
                (topup_id, uid, float(amount), manual_code, "pending", datetime.utcnow().isoformat()))
    conn.commit(); conn.close()
    pay_link = "https://www.tbank.ru/cf/AjpqOu4cEzU"
    qr_url = url_for("static_files", filename="qr.png") if os.path.exists(os.path.join(app.static_folder,"qr.png")) else ""
    # notify admin via socket
    socketio.emit("new_topup", {"id": topup_id, "uid": uid, "amount": amount}, broadcast=True)
    return jsonify({"ok": True, "id": topup_id, "manual_code": manual_code, "pay_link": pay_link, "qr_url": qr_url})

@app.post("/api/user/topup-confirm")
def api_topup_confirm():
    data = request.json or {}
    topup_id = data.get("topup_id"); uid = data.get("uid")
    if not topup_id or not uid:
        return jsonify({"ok": False, "errmsg":"missing"}), 400
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM topups WHERE id=?", (int(topup_id),))
    t = cur.fetchone()
    if not t:
        conn.close(); return jsonify({"ok": False, "errmsg":"not found"}), 404
    # notify admin via bot (manual verification)
    try:
        bot.send_message(ADMIN_ID, f"Пополнение ожидает проверки\nID:{t['id']}\nUID:{uid}\nСумма:{t['amount']} ₽")
    except Exception:
        pass
    conn.close()
    return jsonify({"ok": True})

# Admin confirms topup -> mark paid and credit user
@app.post("/api/admin/topups/<int:topup_id>/approve")
def api_admin_topup_approve(topup_id):
    # In production: check admin auth; here demo
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM topups WHERE id=?", (topup_id,))
    t = cur.fetchone()
    if not t:
        conn.close(); return jsonify({"ok": False, "errmsg":"not found"}), 404
    cur.execute("UPDATE topups SET status='paid' WHERE id=?", (topup_id,))
    change_balance(t["uid"], float(t["amount"]))
    conn.commit(); conn.close()
    socketio.emit("user_update", {"user_id": t["uid"], "balance": get_user(t["uid"])["balance"]}, broadcast=True)
    return jsonify({"ok": True})

@app.post("/api/admin/topups/<int:topup_id>/reject")
def api_admin_topup_reject(topup_id):
    conn = get_db(); cur = conn.cursor()
    cur.execute("UPDATE topups SET status='refunded' WHERE id=?", (topup_id,))
    conn.commit(); conn.close()
    return jsonify({"ok": True})

# --------- Withdraw flow ----------
@app.post("/api/user/withdraw")
def api_user_withdraw():
    data = request.json or {}
    uid = data.get("uid")
    amount = float(data.get("amount") or 0)
    name = data.get("name"); details = data.get("details")
    if not uid or amount <= 0 or not name or not details:
        return jsonify({"ok": False, "errmsg":"missing"}), 400
    ensure_user(uid)
    user = get_user(uid)
    if user["balance"] < amount:
        return jsonify({"ok": False, "errmsg":"insufficient_balance"}), 400
    wid = int(time()*1000)
    conn = get_db(); cur = conn.cursor()
    cur.execute("INSERT INTO withdraws (id, uid, amount, name, details, status, created_at) VALUES (?,?,?,?,?,?,?)",
                (wid, uid, amount, name, details, "pending", datetime.utcnow().isoformat()))
    # debit immediately (reserve)
    change_balance(uid, -amount)
    conn.commit(); conn.close()
    # notify admin via bot
    try:
        bot.send_message(ADMIN_ID, f"Новая заявка на вывод\nID:{wid}\nUID:{uid}\nСумма:{amount} ₽\nПолучатель:{name}\nРеквизиты:{details}")
    except Exception:
        pass
    socketio.emit("new_withdraw", {"id": wid, "uid": uid, "amount": amount}, broadcast=True)
    return jsonify({"ok": True, "withdraw": {"id": wid}})

# Admin approve/reject withdraw
@app.post("/api/admin/withdraws/<int:w_id>/approve")
def api_admin_withdraws_approve(w_id):
    # in demo, admin approves and status set approved
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM withdraws WHERE id=?", (w_id,))
    w = cur.fetchone()
    if not w:
        conn.close(); return jsonify({"ok": False, "errmsg":"not found"}), 404
    cur.execute("UPDATE withdraws SET status='approved' WHERE id=?", (w_id,))
    conn.commit(); conn.close()
    socketio.emit("withdraw_update", {"id": w_id, "status": "approved"}, broadcast=True)
    return jsonify({"ok": True})

@app.post("/api/admin/withdraws/<int:w_id>/reject")
def api_admin_withdraws_reject(w_id):
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM withdraws WHERE id=?", (w_id,))
    w = cur.fetchone()
    if not w:
        conn.close(); return jsonify({"ok": False, "errmsg":"not found"}), 404
    # if rejecting — refund to user
    cur.execute("UPDATE withdraws SET status='rejected' WHERE id=?", (w_id,))
    change_balance(w["uid"], float(w["amount"]))
    conn.commit(); conn.close()
    socketio.emit("withdraw_update", {"id": w_id, "status":"rejected"}, broadcast=True)
    return jsonify({"ok": True})

# --------- Admin minimal APIs ----------
@app.get("/api/admin/dashboard")
def api_admin_dashboard():
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT COUNT(*) as cnt FROM users"); users_count = cur.fetchone()["cnt"]
    cur.execute("SELECT SUM(amount) as s FROM topups WHERE status='paid'"); row = cur.fetchone(); total_revenue = row["s"] or 0
    cur.execute("SELECT COUNT(*) as cnt FROM tasks"); tasks_count = cur.fetchone()["cnt"]
    cur.execute("SELECT COUNT(*) as cnt FROM (SELECT id FROM topups WHERE status='pending' UNION SELECT id FROM withdraws WHERE status='pending')"); pending_count = cur.fetchone()["cnt"]
    # recent
    recent = []
    cur.execute("SELECT id, uid, amount, status, created_at FROM topups ORDER BY id DESC LIMIT 5")
    for r in cur.fetchall():
        recent.append({"id": r["id"], "type": "topup", "amount": r["amount"], "status": r["status"], "user": r["uid"], "created_at": r["created_at"]})
    cur.execute("SELECT id, uid, amount, status, created_at FROM withdraws ORDER BY id DESC LIMIT 5")
    for r in cur.fetchall():
        recent.append({"id": r["id"], "type":"withdraw", "amount": r["amount"], "status": r["status"], "user": r["uid"], "created_at": r["created_at"]})
    conn.close()
    return jsonify({"ok": True, "data":{"usersCount": users_count,"totalRevenue": total_revenue,"tasksCount": tasks_count,"pendingCount": pending_count,"recentActivity": recent}})

@app.get("/api/admin/users")
def api_admin_users():
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM users")
    users = [dict(x) for x in cur.fetchall()]
    conn.close()
    return jsonify({"ok": True, "users": users})

@app.get("/api/admin/tasks")
def api_admin_tasks():
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM tasks ORDER BY id DESC")
    tasks = [dict(x) for x in cur.fetchall()]
    conn.close()
    return jsonify({"ok": True, "tasks": tasks})

@app.get("/api/admin/topups")
def api_admin_topups():
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM topups ORDER BY id DESC")
    items = [dict(x) for x in cur.fetchall()]
    conn.close()
    return jsonify({"ok": True, "items": items})

@app.get("/api/admin/withdraws")
def api_admin_withdraws():
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM withdraws ORDER BY id DESC")
    items = [dict(x) for x in cur.fetchall()]
    conn.close()
    return jsonify({"ok": True, "items": items})

# --------- BOT webhook and commands ----------
@app.post("/bot")
def bot_webhook_handler():
    try:
        raw = request.stream.read().decode("utf-8")
        update = telebot.types.Update.de_json(raw)
        bot.process_new_updates([update])
        return "ok"
    except Exception as e:
        logger.exception("Failed to process webhook: %s", e)
        return "err", 500

def is_subscribed_to_channel(user_id: int, channel: str) -> bool:
    try:
        res = bot.get_chat_member(channel, user_id)
        status = getattr(res, "status", None)
        return status in ("creator","administrator","member")
    except Exception as e:
        logger.warning("subscribe check failed: %s", e)
        return False

def check_and_notify_sub(chat_id: int) -> bool:
    try:
        ok = is_subscribed_to_channel(chat_id, REQUIRED_CHANNEL)
        if ok:
            return True
        else:
            markup = telebot.types.InlineKeyboardMarkup()
            markup.add(telebot.types.InlineKeyboardButton("Подписаться", url=f"https://t.me/{REQUIRED_CHANNEL.lstrip('@')}"))
            bot.send_message(chat_id, f"Для доступа подпишитесь на канал {REQUIRED_CHANNEL}", reply_markup=markup)
            return False
    except Exception as e:
        logger.exception("subscribe check failed: %s", e)
        bot.send_message(chat_id, f"Не удалось проверить подписку. Пожалуйста, подпишитесь на {REQUIRED_CHANNEL} и попробуйте снова.")
        return False

@bot.message_handler(commands=["start"])
def cmd_start(message):
    chat_id = message.chat.id
    if not check_and_notify_sub(chat_id):
        return
    # store user
    ensure_user(str(chat_id), message.from_user.first_name, message.from_user.last_name, message.from_user.username)
    # send webapp button
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("Открыть WebApp", web_app=telebot.types.WebAppInfo(url=WEBAPP_URL)))
    bot.send_message(chat_id, "👋 Привет! Открой личный кабинет ReviewCash:", reply_markup=markup)

@bot.message_handler(commands=["admin"])
def cmd_admin(message):
    chat_id = message.chat.id
    if chat_id != ADMIN_ID:
        bot.send_message(chat_id, "Доступно только супер-админу.")
        return
    if not check_and_notify_sub(chat_id):
        return
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("Открыть Admin", web_app=telebot.types.WebAppInfo(url=WEBAPP_URL + "/admin")))
    bot.send_message(chat_id, "Открой админ-панель:", reply_markup=markup)

@bot.message_handler(commands=["mod"])
def cmd_mod(message):
    chat_id = message.chat.id
    if not check_and_notify_sub(chat_id):
        return
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("Открыть Moderator", web_app=telebot.types.WebAppInfo(url=WEBAPP_URL + "/moderator")))
    bot.send_message(chat_id, "Открой модераторскую панель:", reply_markup=markup)

@bot.message_handler(commands=["addadmin"])
def cmd_addadmin(message):
    if message.chat.id != ADMIN_ID:
        bot.send_message(message.chat.id, "Только супер-админ.")
        return
    parts = message.text.split()
    if len(parts) < 2:
        bot.send_message(message.chat.id, "Использование: /addadmin <user_id>")
        return
    try:
        new_id = str(int(parts[1]))
        ensure_user(new_id)
        conn = get_db(); cur = conn.cursor()
        cur.execute("UPDATE users SET role='admin' WHERE id=?", (new_id,))
        conn.commit(); conn.close()
        bot.send_message(message.chat.id, f"{new_id} теперь админ (демо).")
    except Exception as e:
        bot.send_message(message.chat.id, "Ошибка: " + str(e))

@bot.message_handler(commands=["addmod"])
def cmd_addmod(message):
    if message.chat.id != ADMIN_ID:
        bot.send_message(message.chat.id, "Только супер-админ.")
        return
    parts = message.text.split()
    if len(parts) < 2:
        bot.send_message(message.chat.id, "Использование: /addmod <user_id>")
        return
    try:
        new_id = str(int(parts[1]))
        ensure_user(new_id)
        conn = get_db(); cur = conn.cursor()
        cur.execute("UPDATE users SET role='mod' WHERE id=?", (new_id,))
        conn.commit(); conn.close()
        bot.send_message(message.chat.id, f"{new_id} теперь модератор (демо).")
    except Exception as e:
        bot.send_message(message.chat.id, "Ошибка: " + str(e))

# set_webhook endpoint
@app.get("/set_webhook")
def set_webhook():
    if not WEBAPP_URL or not WEBAPP_URL.startswith("http"):
        return jsonify({"ok": False, "errmsg":"WEBAPP_URL not configured"}), 400
    webhook_url = WEBAPP_URL.rstrip("/") + "/bot"
    try:
        bot.remove_webhook()
    except Exception:
        pass
    try:
        ok = bot.set_webhook(url=webhook_url)
        logger.info("set_webhook -> %s", ok)
        return jsonify({"ok": True, "webhook": webhook_url, "result": ok})
    except Exception as e:
        logger.exception("set_webhook failed: %s", e)
        return jsonify({"ok": False, "errmsg": str(e)}), 500

# socket handlers
@socketio.on("connect")
def on_connect():
    logger.info("socket connected: %s", request.sid)

@socketio.on("disconnect")
def on_disconnect():
    logger.info("socket disconnected: %s", request.sid)

if __name__ == "__main__":
    if USE_WEBHOOK_ON_START:
        try:
            webhook_url = WEBAPP_URL.rstrip("/") + "/bot"
            bot.remove_webhook()
            bot.set_webhook(url=webhook_url)
            logger.info("Webhook set to %s", webhook_url)
        except Exception as e:
            logger.exception("Failed to set webhook on start: %s", e)
    logger.info("Starting server on %s:%s", HOST, PORT)
    socketio.run(app, host=HOST, port=PORT)
