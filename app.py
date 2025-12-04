# app.py
import os
import json
import logging
from time import time
from datetime import datetime
from typing import Optional

from flask import Flask, request, jsonify, send_from_directory, url_for
from flask_cors import CORS
from flask_socketio import SocketIO
import telebot
import requests

# -----------------------
# Configuration
# -----------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8033069276:AAFv1-kdQ68LjvLEgLHj3ZXd5ehMqyUXOYU")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "6482440657"))
REQUIRED_CHANNEL = os.environ.get("REQUIRED_CHANNEL", "@ReviewCashNews")  # channel username or id like @channel
WEBAPP_URL = os.environ.get("WEBAPP_URL", "https://web-production-398fb.up.railway.app")  # must be https
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", 8080))
USE_WEBHOOK_ON_START = True  # set False to disable automatic set_webhook at startup

# -----------------------
# App init
# -----------------------
app = Flask(__name__, static_folder="static", static_url_path="")
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "reviewcash_secret")
CORS(app, resources={r"/api/*": {"origins": "*"}})

# Use eventlet for socketio websockets (Railway compatible)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

# Telegram bot
bot = telebot.TeleBot(BOT_TOKEN, threaded=False)

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("reviewcash")

# -----------------------
# In-memory demo storage
# -----------------------
USERS = {}      # uid -> { uid, first_name, last_name, username, balance, history, tasks }
TASKS = []      # list of tasks
TOPUPS = []     # list of topup dicts
WITHDRAWS = []  # list of withdraw dicts

# Helper to ensure user exists
def ensure_user(uid: str, add_defaults=True):
    u = USERS.get(uid)
    if not u and add_defaults:
        USERS[uid] = {
            "id": uid,
            "first_name": None,
            "last_name": None,
            "username": None,
            "balance": 0,
            "history": [],
            "tasks": []
        }
        u = USERS[uid]
    return u

# -----------------------
# Static files (frontend)
# -----------------------
@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/admin")
def admin_panel():
    return send_from_directory("static", "admin.html")

@app.route("/moderator")
def moderator_panel():
    return send_from_directory("static", "moderator.html")

# serve other static assets
@app.route("/<path:filename>")
def static_files(filename):
    # allow serving images, js, css etc from static folder
    return send_from_directory("static", filename)

# -----------------------
# Utility: check subscription
# -----------------------
def is_subscribed_to_channel(user_id: int, channel: str) -> bool:
    """
    Check whether `user_id` is a member of `channel` (username like '@channel' or chat id).
    Returns True if status is 'member', 'creator' or 'administrator'.
    Note: bot must be admin or at least able to call getChatMember on channel.
    """
    try:
        # telebot provides get_chat_member(chat_id, user_id)
        res = bot.get_chat_member(channel, user_id)
        # res has .status
        status = getattr(res, "status", None)
        logger.debug("get_chat_member result: %s", status)
        if status in ("creator", "administrator", "member"):
            return True
        return False
    except Exception as e:
        logger.warning("Failed to check subscription: %s", e)
        # Fail-safe: return False so we won't grant access if check failed
        return False

# -----------------------
# API: Profile
# -----------------------
@app.get("/api/profile_me")
def api_profile_me():
    uid = request.args.get("uid")
    if not uid:
        return jsonify({"ok": False, "errmsg": "no uid"}), 400
    user = ensure_user(uid)
    return jsonify({"ok": True, "user": user})

# -----------------------
# API: Tasks
# -----------------------
@app.get("/api/tasks/list")
def api_tasks_list():
    return jsonify({"ok": True, "tasks": TASKS})

@app.post("/api/tasks/create")
def api_tasks_create():
    data = request.json or {}
    required = ("owner_uid", "title", "description", "qty", "unit_price", "url", "type_id")
    if not all(k in data for k in required):
        return jsonify({"ok": False, "errmsg": "missing fields"}), 400
    task = {
        "id": int(time()*1000),
        "owner_uid": data["owner_uid"],
        "title": data["title"],
        "description": data["description"],
        "qty": int(data["qty"]),
        "unit_price": float(data["unit_price"]),
        "url": data["url"],
        "type_id": data.get("type_id"),
        "created_at": datetime.utcnow().isoformat(),
        "completed_qty": 0,
        "status": "active"
    }
    TASKS.append(task)
    # add to user's tasks list
    ensure_user(data["owner_uid"])
    USERS[data["owner_uid"]]["tasks"].append(task["id"])
    # notify via socketio
    try:
        socketio.emit("task_update", {"task": task}, broadcast=True)
    except Exception as e:
        logger.debug("socket emit failed: %s", e)
    return jsonify({"ok": True, "task": task})

# Endpoint to mark task as taken/completed by a worker (so another user can perform it)
@app.post("/api/tasks/claim")
def api_tasks_claim():
    data = request.json or {}
    task_id = data.get("task_id")
    worker_uid = data.get("worker_uid")
    if not task_id or not worker_uid:
        return jsonify({"ok": False, "errmsg": "missing"}), 400
    # find task
    task = next((t for t in TASKS if t["id"] == int(task_id)), None)
    if not task:
        return jsonify({"ok": False, "errmsg": "task not found"}), 404
    if task["status"] != "active":
        return jsonify({"ok": False, "errmsg": "task not active"}), 400
    # record a "claim" record in user's history (here simple)
    ensure_user(worker_uid)
    USERS[worker_uid]["history"].append({
        "type": "claim",
        "task_id": task["id"],
        "at": datetime.utcnow().isoformat()
    })
    # increment completed (for demo, we allow immediate completion)
    task["completed_qty"] += 1
    # if completed >= qty, mark inactive
    if task["completed_qty"] >= task["qty"]:
        task["status"] = "inactive"
    # Notify sockets
    socketio.emit("task_update", {"task_id": task["id"], "completed_qty": task["completed_qty"]}, broadcast=True)
    return jsonify({"ok": True, "task": task})

# -----------------------
# API: Topup (create + confirm)
# -----------------------
@app.post("/api/user/topup-link")
def api_topup_link():
    data = request.json or {}
    uid = data.get("uid")
    amount = data.get("amount")
    if not uid or not amount:
        return jsonify({"ok": False, "errmsg": "missing"}), 400
    topup_id = int(time()*1000)
    manual_code = f"RC-{topup_id}"
    topup = {
        "id": topup_id,
        "uid": uid,
        "amount": float(amount),
        "manual_code": manual_code,
        "status": "pending",
        "created_at": datetime.utcnow().isoformat()
    }
    TOPUPS.append(topup)
    # build a pay link (demo static link)
    pay_link = "https://www.tbank.ru/cf/AjpqOu4cEzU"
    # return qr_url relative to static (place qr.png in static or widget will show link)
    qr_url = url_for("static_files", filename="qr.png") if os.path.exists(os.path.join(app.static_folder, "qr.png")) else ""
    # emit new topup to admin via socket
    try:
        socketio.emit("new_topup", {"topup": topup}, broadcast=True)
    except Exception:
        pass
    return jsonify({"ok": True, "id": topup_id, "manual_code": manual_code, "pay_link": pay_link, "qr_url": qr_url, "topup": topup})

@app.post("/api/user/topup-confirm")
def api_topup_confirm():
    data = request.json or {}
    topup_id = data.get("topup_id")
    uid = data.get("uid")
    if not topup_id or not uid:
        return jsonify({"ok": False, "errmsg": "missing"}), 400
    topup = next((t for t in TOPUPS if t["id"] == int(topup_id)), None)
    if not topup:
        return jsonify({"ok": False, "errmsg": "not found"}), 404
    # For demo, we just notify admin for manual verification via bot
    try:
        bot.send_message(ADMIN_ID, f"Новая заявка на пополнение\nID: {topup['id']}\nUID: {uid}\nСумма: {topup['amount']} ₽\nКод: {topup['manual_code']}")
    except Exception as e:
        logger.exception("Failed to notify admin: %s", e)
    return jsonify({"ok": True})

# -----------------------
# API: Withdraw
# -----------------------
@app.post("/api/user/withdraw")
def api_user_withdraw():
    data = request.json or {}
    uid = data.get("uid")
    amount = float(data.get("amount", 0) or 0)
    name = data.get("name")
    details = data.get("details")
    if not uid or amount <= 0 or not name or not details:
        return jsonify({"ok": False, "errmsg": "missing"}), 400
    wid = int(time()*1000)
    req = {
        "id": wid,
        "uid": uid,
        "amount": amount,
        "name": name,
        "details": details,
        "status": "pending",
        "created_at": datetime.utcnow().isoformat()
    }
    WITHDRAWS.append(req)
    # notify admin via bot
    try:
        bot.send_message(ADMIN_ID, f"Заявка на вывод\nID: {wid}\nUID: {uid}\nСумма: {amount} ₽\nИмя: {name}\nРеквизиты: {details}")
    except Exception as e:
        logger.exception("Failed to notify admin of withdraw: %s", e)
    # emit socket
    socketio.emit("new_withdraw", {"withdraw": req}, broadcast=True)
    return jsonify({"ok": True, "withdraw": req})

# -----------------------
# Admin minimal API (demo)
# -----------------------
@app.get("/api/admin/dashboard")
def api_admin_dashboard():
    users_count = len(USERS)
    total_revenue = sum(t["amount"] for t in TOPUPS if t.get("status") == "paid") if TOPUPS else 0
    tasks_count = len(TASKS)
    pending_count = sum(1 for t in TOPUPS + WITHDRAWS if t.get("status") == "pending")
    recent = []
    # recent topups and withdraws (last 10)
    recent_items = (TOPUPS + WITHDRAWS)[-10:]
    for it in recent_items:
        recent.append({
            "id": it["id"],
            "type": "topup" if "amount" in it and it in TOPUPS else "withdraw",
            "amount": it.get("amount"),
            "status": it.get("status"),
            "user": it.get("uid"),
            "created_at": it.get("created_at")
        })
    return jsonify({"ok": True, "data": {"usersCount": users_count, "totalRevenue": total_revenue, "tasksCount": tasks_count, "pendingCount": pending_count, "recentActivity": recent}})

@app.get("/api/admin/users")
def api_admin_users():
    # return small user list
    users = []
    for k, v in USERS.items():
        users.append({
            "id": v["id"],
            "first_name": v.get("first_name"),
            "username": v.get("username"),
            "balance": v.get("balance", 0),
            "tasks_done": sum(1 for t in TASKS if str(t.get("owner_uid")) == str(k) and t.get("status") == "inactive")
        })
    return jsonify({"ok": True, "users": users})

@app.get("/api/admin/tasks")
def api_admin_tasks():
    return jsonify({"ok": True, "tasks": TASKS})

@app.get("/api/admin/topups")
def api_admin_topups():
    return jsonify({"ok": True, "items": TOPUPS})

@app.get("/api/admin/withdraws")
def api_admin_withdraws():
    return jsonify({"ok": True, "items": WITHDRAWS})

# admin endpoints to approve/reject topups/withdraws (demo)
@app.post("/api/admin/topups/<int:topup_id>/approve")
def api_admin_topup_approve(topup_id):
    # only admin (in production add auth)
    t = next((x for x in TOPUPS if x["id"] == topup_id), None)
    if not t:
        return jsonify({"ok": False, "errmsg": "Not found"}), 404
    t["status"] = "paid"
    # credit user balance
    u = ensure_user(t["uid"])
    u["balance"] = u.get("balance", 0) + float(t["amount"])
    # notify sockets
    socketio.emit("user_update", {"user_id": t["uid"], "balance": u["balance"]}, broadcast=True)
    # notify user via bot if we have chat id (try)
    try:
        bot.send_message(int(t["uid"]), f"Ваше пополнение на {t['amount']} ₽ подтверждено. Баланс: {u['balance']} ₽")
    except Exception:
        pass
    return jsonify({"ok": True})

@app.post("/api/admin/topups/<int:topup_id>/reject")
def api_admin_topup_reject(topup_id):
    t = next((x for x in TOPUPS if x["id"] == topup_id), None)
    if not t:
        return jsonify({"ok": False, "errmsg": "Not found"}), 404
    t["status"] = "refunded"
    return jsonify({"ok": True})

@app.post("/api/admin/withdraws/<int:w_id>/approve")
def api_admin_withdraws_approve(w_id):
    w = next((x for x in WITHDRAWS if x["id"] == w_id), None)
    if not w:
        return jsonify({"ok": False, "errmsg": "Not found"}), 404
    w["status"] = "approved"
    # debit user's balance
    u = ensure_user(w["uid"])
    u["balance"] = max(0, u.get("balance", 0) - float(w["amount"]))
    socketio.emit("user_update", {"user_id": w["uid"], "balance": u["balance"]}, broadcast=True)
    try:
        bot.send_message(int(w["uid"]), f"Ваша заявка на вывод {w['amount']} ₽ одобрена.")
    except Exception:
        pass
    return jsonify({"ok": True})

@app.post("/api/admin/withdraws/<int:w_id>/reject")
def api_admin_withdraws_reject(w_id):
    w = next((x for x in WITHDRAWS if x["id"] == w_id), None)
    if not w:
        return jsonify({"ok": False, "errmsg": "Not found"}), 404
    w["status"] = "rejected"
    return jsonify({"ok": True})

# -----------------------
# BOT: webhook endpoint
# -----------------------
@app.post("/bot")
def bot_webhook_handler():
    try:
        raw = request.stream.read().decode("utf-8")
        update = telebot.types.Update.de_json(raw)
        bot.process_new_updates([update])
        return "ok"
    except Exception as e:
        logger.exception("Failed to process webhook update: %s", e)
        return "error", 500

# -----------------------
# BOT: commands
# -----------------------
def tg_send_webapp_button(chat_id: int, path: str = "/"):
    """
    Send an InlineKeyboardButton with web_app to open frontend page.
    path is appended to WEBAPP_URL, e.g. / (index), /admin
    """
    url = WEBAPP_URL.rstrip("/") + path
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(
        telebot.types.InlineKeyboardButton("Открыть приложение", web_app=telebot.types.WebAppInfo(url=url))
    )
    bot.send_message(chat_id, "Откройте приложение:", reply_markup=markup)

def check_and_notify_sub(chat_id: int) -> bool:
    """
    Check subscription to REQUIRED_CHANNEL. If not subscribed, send message with link to channel and return False.
    If subscribed → True.
    """
    try:
        ok = is_subscribed_to_channel(chat_id, REQUIRED_CHANNEL)
        if ok:
            return True
        else:
            # send message with channel invite button
            markup = telebot.types.InlineKeyboardMarkup()
            markup.add(telebot.types.InlineKeyboardButton("Подписаться", url=f"https://t.me/{REQUIRED_CHANNEL.lstrip('@')}"))
            bot.send_message(chat_id, f"Для доступа нужно подписаться на канал {REQUIRED_CHANNEL}", reply_markup=markup)
            return False
    except Exception as e:
        logger.exception("subscribe check failed: %s", e)
        # inform user to subscribe manually
        bot.send_message(chat_id, f"Не удалось проверить подписку. Пожалуйста, подпишитесь на {REQUIRED_CHANNEL} и попробуйте снова.")
        return False

@bot.message_handler(commands=["start"])
def cmd_start(message):
    chat_id = message.chat.id
    # Send nice welcome with webapp button, only if subscribed
    if not check_and_notify_sub(chat_id):
        return
    # nice rich message
    text = ("👋 Привет! Это ReviewCash — платформа для вознаграждений.\n\n"
            "🔹 Нажмите кнопку ниже, чтобы открыть личный кабинет.\n"
            "🔸 В личном кабинете вы можете создавать задания, пополнять баланс и выводить средства.")
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("Открыть кабинет", web_app=telebot.types.WebAppInfo(url=WEBAPP_URL)))
    bot.send_message(chat_id, text, reply_markup=markup)

@bot.message_handler(commands=["admin"])
def cmd_admin(message):
    chat_id = message.chat.id
    # check admin id
    if chat_id != ADMIN_ID:
        bot.send_message(chat_id, "Доступно только администратору.")
        return
    if not check_and_notify_sub(chat_id):
        return
    # open admin webapp
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("Открыть админ-панель", web_app=telebot.types.WebAppInfo(url=WEBAPP_URL + "/admin")))
    bot.send_message(chat_id, "Откройте админ-панель:", reply_markup=markup)

@bot.message_handler(commands=["mod"])
def cmd_mod(message):
    chat_id = message.chat.id
    if not check_and_notify_sub(chat_id):
        return
    # moderator panel
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(telebot.types.InlineKeyboardButton("Открыть модераторскую панель", web_app=telebot.types.WebAppInfo(url=WEBAPP_URL + "/moderator")))
    bot.send_message(chat_id, "Откройте модераторскую панель:", reply_markup=markup)

# Add commands to manage admins/moderators (simple demo, restricted to ADMIN_ID)
@bot.message_handler(commands=["addadmin"])
def cmd_addadmin(message):
    if message.chat.id != ADMIN_ID:
        bot.send_message(message.chat.id, "Только супер-админ может это делать.")
        return
    parts = message.text.split()
    if len(parts) < 2:
        bot.send_message(message.chat.id, "Использование: /addadmin <user_id>")
        return
    try:
        new_id = int(parts[1])
        # store in USERS for demo as elevated role (in real — persistent DB)
        ensure_user(str(new_id))
        USERS[str(new_id)]["role"] = "admin"
        bot.send_message(message.chat.id, f"Пользователь {new_id} добавлен в админы (демо).")
    except Exception as e:
        bot.send_message(message.chat.id, "Ошибка: " + str(e))

@bot.message_handler(commands=["addmod"])
def cmd_addmod(message):
    if message.chat.id != ADMIN_ID:
        bot.send_message(message.chat.id, "Только супер-админ может это делать.")
        return
    parts = message.text.split()
    if len(parts) < 2:
        bot.send_message(message.chat.id, "Использование: /addmod <user_id>")
        return
    try:
        new_id = int(parts[1])
        ensure_user(str(new_id))
        USERS[str(new_id)]["role"] = "mod"
        bot.send_message(message.chat.id, f"Пользователь {new_id} добавлен в модераторы (демо).")
    except Exception as e:
        bot.send_message(message.chat.id, "Ошибка: " + str(e))

# -----------------------
# WEBHOOK helper: set webhook
# -----------------------
@app.get("/set_webhook")
def set_webhook():
    # only allow if WEBAPP_URL is provided
    if not WEBAPP_URL or not WEBAPP_URL.startswith("http"):
        return jsonify({"ok": False, "errmsg": "WEBAPP_URL not configured"}), 400
    webhook_url = WEBAPP_URL.rstrip("/") + "/bot"
    try:
        # remove previous
        try:
            bot.remove_webhook()
        except Exception:
            pass
        result = bot.set_webhook(url=webhook_url)
        logger.info("set_webhook result: %s -> %s", webhook_url, result)
        return jsonify({"ok": True, "webhook": webhook_url, "result": result})
    except Exception as e:
        logger.exception("set_webhook failed: %s", e)
        return jsonify({"ok": False, "errmsg": str(e)}), 500

# -----------------------
# SocketIO events
# -----------------------
@socketio.on("connect")
def on_connect():
    logger.info("Socket connected: %s", request.sid)

@socketio.on("disconnect")
def on_disconnect():
    logger.info("Socket disconnected: %s", request.sid)

# -----------------------
# Startup behavior
# -----------------------
def try_set_webhook_on_start():
    if not USE_WEBHOOK_ON_START:
        logger.info("Skipping automatic webhook set on start (USE_WEBHOOK_ON_START=False)")
        return
    if not WEBAPP_URL:
        logger.info("WEBAPP_URL not set; skipping webhook set")
        return
    try:
        webhook_url = WEBAPP_URL.rstrip("/") + "/bot"
        bot.remove_webhook()
        ok = bot.set_webhook(url=webhook_url)
        logger.info("Attempted to set webhook -> %s (url=%s)", ok, webhook_url)
    except Exception as e:
        logger.exception("Failed to set webhook on start: %s", e)

# -----------------------
# Run server
# -----------------------
if __name__ == "__main__":
    # ensure static folder exists (for demo)
    if not os.path.isdir(app.static_folder):
        logger.warning("Static folder %s missing; create 'static' with index.html and admin.html", app.static_folder)
    # optionally set webhook
    try_set_webhook_on_start()
    logger.info("Starting server on %s:%s", HOST, PORT)
    # Use eventlet (requirements.txt contains eventlet)
    socketio.run(app, host=HOST, port=PORT)
