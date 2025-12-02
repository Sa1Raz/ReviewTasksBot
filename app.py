from flask import Flask, request, send_from_directory, jsonify
from flask_socketio import SocketIO, emit
import telebot
import os
import json
from time import time

# =========================================
# CONFIG
# =========================================
TOKEN = "8033069276:AAFv1-kdQ68LjvLEgLHj3ZXd5ehMqyUXOYU"
ADMIN_ID = 6482440657
WEBAPP_URL = "https://web-production-398fb.up.railway.app/"
NEWS_CHANNEL = "@ReviewCashNews"

# =========================================
# FLASK
# =========================================
app = Flask(__name__, static_folder="static", static_url_path="")
app.config["SECRET_KEY"] = "reviewcash_secret"

# =========================================
# SOCKET.IO (EIO=4, SIO=4 – правильная версия)
# =========================================
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

# =========================================
# TELEGRAM BOT
# =========================================
bot = telebot.TeleBot(TOKEN, threaded=False)

# =========================================
# SIMPLE DATABASE (DEMO)
# =========================================
USERS = {}          # uid → user
TASKS = []          # задания
TOPUPS = []         # пополнения
WITHDRAWS = []      # выводы

# =========================================
# STATIC ROUTES
# =========================================
@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/admin")
def admin():
    return send_from_directory("static", "admin.html")

@app.route("/moderator")
def moderator():
    return send_from_directory("static", "moderator.html")


# ============================================================
# =====================   USER PROFILE   ======================
# ============================================================
@app.get("/api/profile_me")
def profile_me():
    uid = request.args.get("uid")
    name = request.args.get("name", "")
    avatar = request.args.get("avatar", "")

    if not uid:
        return jsonify({"ok": False, "err": "NO_UID"})

    # Auto-create user
    user = USERS.setdefault(uid, {
        "uid": uid,
        "balance": 0,
        "history": [],
        "name": name,
        "avatar": avatar,
        "tasks": []
    })

    # Update cached name/avatar
    if name:
        user["name"] = name
    if avatar:
        user["avatar"] = avatar

    return jsonify({"ok": True, "user": user})


# ============================================================
# =======================   TASKS API   =======================
# ============================================================
@app.get("/api/tasks/list")
def tasks_list():
    return jsonify({"ok": True, "tasks": TASKS})


@app.post("/api/tasks/create")
def task_create():
    data = request.json

    task = {
        "id": int(time() * 1000),
        "owner_uid": data["owner_uid"],
        "title": data["title"],
        "description": data["description"],
        "qty": data["qty"],
        "completed_qty": 0,
        "unit_price": data["unit_price"],
        "url": data["url"],
        "type_id": data.get("type_id")
    }

    TASKS.append(task)

    socketio.emit("task_update", {}, broadcast=True)

    return jsonify({"ok": True})


# ============================================================
# =======================   TOPUP API   =======================
# ============================================================
@app.post("/api/user/topup-link")
def topup_link():
    data = request.json
    uid = data["uid"]
    amount = int(data["amount"])

    topup_id = int(time() * 1000)
    manual_code = f"RC-{topup_id}"

    TOPUPS.append({
        "id": topup_id,
        "uid": uid,
        "amount": amount,
        "manual_code": manual_code,
        "confirmed": False
    })

    return jsonify({
        "ok": True,
        "id": topup_id,
        "manual_code": manual_code,
        "pay_link": "https://www.tbank.ru/cf/AjpqOu4cEzU",
        "qr_url": "/static/img/qr.png"
    })


@app.post("/api/user/topup-confirm")
def topup_confirm():
    data = request.json
    topup_id = data["topup_id"]
    uid = data["uid"]

    for t in TOPUPS:
        if t["id"] == topup_id:
            bot.send_message(
                ADMIN_ID,
                f"💳 Новое пополнение:\n"
                f"Пользователь: {uid}\n"
                f"ID пополнения: {topup_id}\n"
                f"Сумма: {t['amount']} ₽\n"
                f"Комментарий: {t['manual_code']}"
            )
            return jsonify({"ok": True})

    return jsonify({"ok": False, "errmsg": "Not found"})


# ============================================================
# =======================  WITHDRAW API  ======================
# ============================================================
@app.post("/api/user/withdraw")
def withdraw():
    data = request.json

    WITHDRAWS.append(data)

    bot.send_message(
        ADMIN_ID,
        f"🟢 Новая заявка на вывод\n"
        f"UID: {data['uid']}\n"
        f"Сумма: {data['amount']} ₽\n"
        f"Имя: {data['name']}\n"
        f"Реквизиты: {data['details']}"
    )

    return jsonify({"ok": True})


# ============================================================
# =======================    ADMIN API   ======================
# ============================================================
@app.get("/api/admin/topups")
def admin_topups():
    return jsonify({"ok": True, "list": TOPUPS})


@app.get("/api/admin/withdraws")
def admin_withdraws():
    return jsonify({"ok": True, "list": WITHDRAWS})


@app.get("/api/admin/tasks")
def admin_tasks():
    return jsonify({"ok": True, "list": TASKS})


@app.post("/api/admin/confirm_topup")
def admin_confirm_topup():
    data = request.json
    topup_id = data["id"]

    for t in TOPUPS:
        if t["id"] == topup_id:
            t["confirmed"] = True

            USERS[t["uid"]]["balance"] += t["amount"]
            USERS[t["uid"]]["history"].append({
                "type": "Пополнение",
                "amount": t["amount"],
                "time": time()
            })

            socketio.emit(
                "balance_update",
                {"uid": t["uid"], "balance": USERS[t["uid"]]["balance"]},
                broadcast=True
            )

            return jsonify({"ok": True})

    return jsonify({"ok": False})


@app.post("/api/admin/confirm_withdraw")
def admin_confirm_withdraw():
    data = request.json
    w_id = data["id"]

    for w in WITHDRAWS:
        if w["id"] == w_id:
            w["confirmed"] = True
            return jsonify({"ok": True})

    return jsonify({"ok": False})


# ============================================================
# ===================  TELEGRAM WEBHOOK  ======================
# ============================================================
@app.post("/bot")
def bot_webhook():
    data = request.data.decode("utf-8")
    update = telebot.types.Update.de_json(data)
    bot.process_new_updates([update])
    return "ok"


# ============================================================
# =====================  BOT COMMANDS  ========================
# ============================================================
@bot.message_handler(commands=["start"])
def start(msg):
    uid = msg.from_user.id

    # check subscription
    try:
        chat = bot.get_chat_member(NEWS_CHANNEL, uid)
        subscribed = chat.status not in ("left", "kicked")
    except:
        subscribed = False

    if not subscribed:
        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(
            telebot.types.InlineKeyboardButton("Подписаться", url=f"https://t.me/{NEWS_CHANNEL[1:]}")
        )
        markup.add(
            telebot.types.InlineKeyboardButton("Проверить 👌", callback_data="check_sub")
        )
        bot.send_message(uid, "❗ Для доступа к сервису, подпишись на канал", reply_markup=markup)
        return

    # show button
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(
        telebot.types.InlineKeyboardButton(
            "Открыть приложение 🚀",
            web_app=telebot.types.WebAppInfo(url=WEBAPP_URL)
        )
    )
    bot.send_message(uid, "Добро пожаловать!", reply_markup=markup)


@bot.callback_query_handler(func=lambda c: c.data == "check_sub")
def check_sub(c):
    uid = c.from_user.id

    try:
        chat = bot.get_chat_member(NEWS_CHANNEL, uid)
        subscribed = chat.status not in ("left", "kicked")
    except:
        subscribed = False

    if subscribed:
        bot.answer_callback_query(c.id, "Вы подписались!")
        start(c.message)
    else:
        bot.answer_callback_query(c.id, "Вы ещё не подписались!")


# ============================================================
# =============== SOCKET.IO — BALANCE STREAM ==================
# ============================================================
@socketio.on("request_balance")
def req_balance(uid):
    if uid in USERS:
        emit("balance_update", {"uid": uid, "balance": USERS[uid]["balance"]})


@socketio.on("connect")
def socket_connect():
    print("socket connected")


@socketio.on("disconnect")
def socket_disconnect():
    print("socket disconnected")


# ============================================================
# RUN SERVER
# ============================================================
if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=8080)
from flask import Flask, request, send_from_directory, jsonify
from flask_socketio import SocketIO, emit
import telebot
import os
import json
from time import time

# =========================================
# CONFIG
# =========================================
TOKEN = "8033069276:AAFv1-kdQ68LjvLEgLHj3ZXd5ehMqyUXOYU"
ADMIN_ID = 6482440657
WEBAPP_URL = "https://web-production-398fb.up.railway.app/"
NEWS_CHANNEL = "@ReviewCashNews"

# =========================================
# FLASK
# =========================================
app = Flask(__name__, static_folder="static", static_url_path="")
app.config["SECRET_KEY"] = "reviewcash_secret"

# =========================================
# SOCKET.IO (EIO=4, SIO=4 – правильная версия)
# =========================================
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

# =========================================
# TELEGRAM BOT
# =========================================
bot = telebot.TeleBot(TOKEN, threaded=False)

# =========================================
# SIMPLE DATABASE (DEMO)
# =========================================
USERS = {}          # uid → user
TASKS = []          # задания
TOPUPS = []         # пополнения
WITHDRAWS = []      # выводы

# =========================================
# STATIC ROUTES
# =========================================
@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/admin")
def admin():
    return send_from_directory("static", "admin.html")

@app.route("/moderator")
def moderator():
    return send_from_directory("static", "moderator.html")


# ============================================================
# =====================   USER PROFILE   ======================
# ============================================================
@app.get("/api/profile_me")
def profile_me():
    uid = request.args.get("uid")
    name = request.args.get("name", "")
    avatar = request.args.get("avatar", "")

    if not uid:
        return jsonify({"ok": False, "err": "NO_UID"})

    # Auto-create user
    user = USERS.setdefault(uid, {
        "uid": uid,
        "balance": 0,
        "history": [],
        "name": name,
        "avatar": avatar,
        "tasks": []
    })

    # Update cached name/avatar
    if name:
        user["name"] = name
    if avatar:
        user["avatar"] = avatar

    return jsonify({"ok": True, "user": user})


# ============================================================
# =======================   TASKS API   =======================
# ============================================================
@app.get("/api/tasks/list")
def tasks_list():
    return jsonify({"ok": True, "tasks": TASKS})


@app.post("/api/tasks/create")
def task_create():
    data = request.json

    task = {
        "id": int(time() * 1000),
        "owner_uid": data["owner_uid"],
        "title": data["title"],
        "description": data["description"],
        "qty": data["qty"],
        "completed_qty": 0,
        "unit_price": data["unit_price"],
        "url": data["url"],
        "type_id": data.get("type_id")
    }

    TASKS.append(task)

    socketio.emit("task_update", {}, broadcast=True)

    return jsonify({"ok": True})


# ============================================================
# =======================   TOPUP API   =======================
# ============================================================
@app.post("/api/user/topup-link")
def topup_link():
    data = request.json
    uid = data["uid"]
    amount = int(data["amount"])

    topup_id = int(time() * 1000)
    manual_code = f"RC-{topup_id}"

    TOPUPS.append({
        "id": topup_id,
        "uid": uid,
        "amount": amount,
        "manual_code": manual_code,
        "confirmed": False
    })

    return jsonify({
        "ok": True,
        "id": topup_id,
        "manual_code": manual_code,
        "pay_link": "https://www.tbank.ru/cf/AjpqOu4cEzU",
        "qr_url": "/static/img/qr.png"
    })


@app.post("/api/user/topup-confirm")
def topup_confirm():
    data = request.json
    topup_id = data["topup_id"]
    uid = data["uid"]

    for t in TOPUPS:
        if t["id"] == topup_id:
            bot.send_message(
                ADMIN_ID,
                f"💳 Новое пополнение:\n"
                f"Пользователь: {uid}\n"
                f"ID пополнения: {topup_id}\n"
                f"Сумма: {t['amount']} ₽\n"
                f"Комментарий: {t['manual_code']}"
            )
            return jsonify({"ok": True})

    return jsonify({"ok": False, "errmsg": "Not found"})


# ============================================================
# =======================  WITHDRAW API  ======================
# ============================================================
@app.post("/api/user/withdraw")
def withdraw():
    data = request.json

    WITHDRAWS.append(data)

    bot.send_message(
        ADMIN_ID,
        f"🟢 Новая заявка на вывод\n"
        f"UID: {data['uid']}\n"
        f"Сумма: {data['amount']} ₽\n"
        f"Имя: {data['name']}\n"
        f"Реквизиты: {data['details']}"
    )

    return jsonify({"ok": True})


# ============================================================
# =======================    ADMIN API   ======================
# ============================================================
@app.get("/api/admin/topups")
def admin_topups():
    return jsonify({"ok": True, "list": TOPUPS})


@app.get("/api/admin/withdraws")
def admin_withdraws():
    return jsonify({"ok": True, "list": WITHDRAWS})


@app.get("/api/admin/tasks")
def admin_tasks():
    return jsonify({"ok": True, "list": TASKS})


@app.post("/api/admin/confirm_topup")
def admin_confirm_topup():
    data = request.json
    topup_id = data["id"]

    for t in TOPUPS:
        if t["id"] == topup_id:
            t["confirmed"] = True

            USERS[t["uid"]]["balance"] += t["amount"]
            USERS[t["uid"]]["history"].append({
                "type": "Пополнение",
                "amount": t["amount"],
                "time": time()
            })

            socketio.emit(
                "balance_update",
                {"uid": t["uid"], "balance": USERS[t["uid"]]["balance"]},
                broadcast=True
            )

            return jsonify({"ok": True})

    return jsonify({"ok": False})


@app.post("/api/admin/confirm_withdraw")
def admin_confirm_withdraw():
    data = request.json
    w_id = data["id"]

    for w in WITHDRAWS:
        if w["id"] == w_id:
            w["confirmed"] = True
            return jsonify({"ok": True})

    return jsonify({"ok": False})


# ============================================================
# ===================  TELEGRAM WEBHOOK  ======================
# ============================================================
@app.post("/bot")
def bot_webhook():
    data = request.data.decode("utf-8")
    update = telebot.types.Update.de_json(data)
    bot.process_new_updates([update])
    return "ok"


# ============================================================
# =====================  BOT COMMANDS  ========================
# ============================================================
@bot.message_handler(commands=["start"])
def start(msg):
    uid = msg.from_user.id

    # check subscription
    try:
        chat = bot.get_chat_member(NEWS_CHANNEL, uid)
        subscribed = chat.status not in ("left", "kicked")
    except:
        subscribed = False

    if not subscribed:
        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(
            telebot.types.InlineKeyboardButton("Подписаться", url=f"https://t.me/{NEWS_CHANNEL[1:]}")
        )
        markup.add(
            telebot.types.InlineKeyboardButton("Проверить 👌", callback_data="check_sub")
        )
        bot.send_message(uid, "❗ Для доступа к сервису, подпишись на канал", reply_markup=markup)
        return

    # show button
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(
        telebot.types.InlineKeyboardButton(
            "Открыть приложение 🚀",
            web_app=telebot.types.WebAppInfo(url=WEBAPP_URL)
        )
    )
    bot.send_message(uid, "Добро пожаловать!", reply_markup=markup)


@bot.callback_query_handler(func=lambda c: c.data == "check_sub")
def check_sub(c):
    uid = c.from_user.id

    try:
        chat = bot.get_chat_member(NEWS_CHANNEL, uid)
        subscribed = chat.status not in ("left", "kicked")
    except:
        subscribed = False

    if subscribed:
        bot.answer_callback_query(c.id, "Вы подписались!")
        start(c.message)
    else:
        bot.answer_callback_query(c.id, "Вы ещё не подписались!")


# ============================================================
# =============== SOCKET.IO — BALANCE STREAM ==================
# ============================================================
@socketio.on("request_balance")
def req_balance(uid):
    if uid in USERS:
        emit("balance_update", {"uid": uid, "balance": USERS[uid]["balance"]})


@socketio.on("connect")
def socket_connect():
    print("socket connected")


@socketio.on("disconnect")
def socket_disconnect():
    print("socket disconnected")


# ============================================================
# RUN SERVER
# ============================================================
if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=8080)
