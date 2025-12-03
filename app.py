# app.py
# -*- coding: utf-8 -*-
from flask import Flask, request, send_from_directory, jsonify, abort
from flask_socketio import SocketIO
import telebot
import os
import json
from time import time
from datetime import datetime

# ====== CONFIG (замени если нужно) ======
TOKEN = "8033069276:AAFv1-kdQ68LjvLEgLHj3ZXd5ehMqyUXOYU"
ADMIN_ID = 6482440657
REQUIRED_CHANNEL = "@ReviewCashNews"
WEBAPP_URL = "https://web-production-398fb.up.railway.app"  # твой railway URL
# ========================================

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.config["SECRET_KEY"] = "reviewcash_secret"

# SocketIO: eventlet/async_mode — совместимость с Railway/deployment
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

# Telegram bot (pyTelegramBotAPI)
bot = telebot.TeleBot(TOKEN, threaded=False)

# === In-memory demo storage (замени на БД в production) ===
USERS = {}     # uid -> user dict { uid, balance, history }
TASKS = []     # list of tasks
TOPUPS = []    # list of topup requests
WITHDRAWS = [] # list of withdraw requests

def now_ts():
    return int(time())

# ========== Static pages ==========
@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/moderator")
def moderator_page():
    return send_from_directory("static", "moderator.html")

@app.route("/admin")
def admin_page():
    return send_from_directory("static", "mainadmin.html")

# ========== Profile API ==========
@app.get("/api/profile_me")
def profile_me():
    uid = request.args.get("uid")
    if not uid:
        return jsonify({"ok": False, "errmsg": "no uid"}), 400
    user = USERS.setdefault(uid, {"uid": uid, "balance": 0, "history": []})
    return jsonify({"ok": True, "user": user})

# ========== Tasks API ==========
@app.get("/api/tasks/list")
def tasks_list():
    return jsonify({"ok": True, "tasks": TASKS})

@app.post("/api/tasks/create")
def task_create():
    data = request.json or {}
    required = ("owner_uid", "title", "description", "qty", "unit_price", "url", "type_id")
    if not all(k in data for k in required):
        return jsonify({"ok": False, "errmsg": "missing fields"}), 400
    task = {
        "id": now_ts(),
        "owner_uid": str(data["owner_uid"]),
        "title": data["title"],
        "description": data["description"],
        "qty": int(data["qty"]),
        "unit_price": float(data["unit_price"]),
        "url": data["url"],
        "type_id": data.get("type_id"),
        "created_at": datetime.utcnow().isoformat()+"Z",
        "completed_qty": 0
    }
    TASKS.append(task)
    # broadcast update (compatibility guard)
    try:
        socketio.emit("task_update", {"task": task}, broadcast=True, namespace="/")
    except TypeError:
        # older/newer python-socketio signature differences -> fallback
        try:
            socketio.emit("task_update", {"task": task}, namespace="/")
        except Exception:
            pass
    return jsonify({"ok": True, "task": task})

# ========== Topup API ==========
@app.post("/api/user/topup-link")
def topup_link():
    data = request.json or {}
    uid = str(data.get("uid", ""))
    amount = int(data.get("amount", 0))
    if not uid or amount <= 0:
        return jsonify({"ok": False, "errmsg": "invalid"}), 400

    # create topup record
    topup_id = now_ts()
    manual_code = f"RC-{topup_id}"
    topup = {
        "id": topup_id,
        "uid": uid,
        "amount": amount,
        "manual_code": manual_code,
        "confirmed": False,
        "created_at": datetime.utcnow().isoformat()+"Z"
    }
    TOPUPS.append(topup)

    # fake bank url (user will be redirected)
    pay_link = "https://example.com/pay?sum=" + str(amount)

    # try to notify admin via bot (non-blocking)
    try:
        bot.send_message(ADMIN_ID, f"Новая заявка на пополнение: {amount} ₽\nUID: {uid}\nID: {topup_id}")
    except Exception:
        pass

    return jsonify({
        "ok": True,
        "id": topup_id,
        "manual_code": manual_code,
        "pay_link": pay_link,
        "qr_url": "/static/qr.png"
    })

@app.post("/api/user/topup-confirm")
def topup_confirm():
    data = request.json or {}
    topup_id = data.get("topup_id")
    uid = str(data.get("uid", ""))
    if not topup_id:
        return jsonify({"ok": False, "errmsg": "missing id"}), 400
    found = None
    for t in TOPUPS:
        if t["id"] == topup_id:
            found = t
            break
    if not found:
        return jsonify({"ok": False, "errmsg": "not found"}), 404

    # notify admin to check (in demo we don't auto-confirm)
    try:
        bot.send_message(ADMIN_ID, f"Пользователь {uid} нажал 'Я оплатил' для пополнения {found['amount']} ₽ (ID {found['id']}). Проверьте платеж.")
    except Exception:
        pass

    return jsonify({"ok": True})

# ========== Withdraw API ==========
@app.post("/api/user/withdraw")
def withdraw():
    data = request.json or {}
    uid = str(data.get("uid", ""))
    amount = int(data.get("amount", 0))
    name = data.get("name", "")
    details = data.get("details", "")
    if amount <= 0 or not uid or not name or not details:
        return jsonify({"ok": False, "errmsg": "invalid"}), 400

    req = {
        "id": now_ts(),
        "uid": uid,
        "amount": amount,
        "name": name,
        "details": details,
        "created_at": datetime.utcnow().isoformat()+"Z"
    }
    WITHDRAWS.append(req)

    # notify admin
    try:
        bot.send_message(ADMIN_ID, f"Новая заявка на вывод\nUID: {uid}\nСумма: {amount} ₽\nИмя: {name}\nРеквизиты: {details}")
    except Exception:
        pass

    return jsonify({"ok": True})

# ========== Telegram webhook handler ==========
@app.post("/bot")
def bot_webhook():
    try:
        data = request.get_data().decode("utf-8")
        update = telebot.types.Update.de_json(data)
        bot.process_new_updates([update])
    except Exception as e:
        print("bot webhook error:", e)
    return "ok"

# ========== BOT COMMANDS & HELPERS ==========
def user_is_subscribed(chat_id, channel_username):
    # returns True if user is member/admin of channel (not LEFT/LEFT/UNKNOWN)
    try:
        member = bot.get_chat_member(channel_username, chat_id)
        status = member.status
        return status in ("member", "administrator", "creator")
    except Exception as e:
        # If bot is not admin or channel private -> assume False
        return False

@bot.message_handler(commands=["start"])
def handle_start(message):
    uid = message.from_user.id
    # Check subscription
    subscribed = user_is_subscribed(uid, REQUIRED_CHANNEL)
    if not subscribed:
        # send request to subscribe with link
        kb = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
        kb.add(telebot.types.KeyboardButton(text=f"Подписаться на {REQUIRED_CHANNEL}"))
        bot.send_message(message.chat.id,
                         f"Для доступа к приложению необходимо подписаться на канал {REQUIRED_CHANNEL}. Нажми кнопку и подпишись, затем отправь /start снова.",
                         reply_markup=kb)
        return

    # user allowed -> send WebApp button
    markup = telebot.types.InlineKeyboardMarkup()
    wa = telebot.types.WebAppInfo(url=WEBAPP_URL)
    markup.add(telebot.types.InlineKeyboardButton("Открыть ReviewCash", web_app=wa))
    bot.send_message(message.chat.id, "Добро пожаловать! Открой приложение:", reply_markup=markup)

@bot.message_handler(commands=["mod"])
def handle_mod(message):
    uid = message.from_user.id
    if not user_is_subscribed(uid, REQUIRED_CHANNEL):
        bot.send_message(message.chat.id, f"Подпишитесь на {REQUIRED_CHANNEL} чтобы использовать команду.")
        return
    # WebApp link to moderator path
    markup = telebot.types.InlineKeyboardMarkup()
    wa = telebot.types.WebAppInfo(url=WEBAPP_URL + "/moderator")
    markup.add(telebot.types.InlineKeyboardButton("Открыть модератор", web_app=wa))
    bot.send_message(message.chat.id, "Открыть модераторскую панель:", reply_markup=markup)

@bot.message_handler(commands=["admin"])
def handle_admin(message):
    # Only admin id allowed
    if message.from_user and message.from_user.id != ADMIN_ID:
        bot.send_message(message.chat.id, "Только администратор может использовать эту команду.")
        return
    markup = telebot.types.InlineKeyboardMarkup()
    wa = telebot.types.WebAppInfo(url=WEBAPP_URL + "/admin")
    markup.add(telebot.types.InlineKeyboardButton("Открыть панель администратора", web_app=wa))
    bot.send_message(message.chat.id, "Открыть админ-панель:", reply_markup=markup)

# ========== Socket events ==========
@socketio.on("connect")
def on_connect():
    print("socket connected")

@socketio.on("disconnect")
def on_disconnect():
    print("socket disconnected")

# ========== Run server ==========
if __name__ == "__main__":
    # Optionally set webhook automatically (commented: set manually if you prefer)
    # try:
    #     bot.remove_webhook()
    #     bot.set_webhook(url=WEBAPP_URL + "/bot")
    #     print("Webhook set to", WEBAPP_URL + "/bot")
    # except Exception as e:
    #     print("Webhook set error:", e)

    socketio.run(app, host="0.0.0.0", port=8080)
