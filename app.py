from flask import Flask, request, send_from_directory, jsonify
from flask_socketio import SocketIO, emit
import telebot
import os
import json
from time import time

TOKEN = "8033069276:AAFv1-kdQ68LjvLEgLHj3ZXd5ehMqyUXOYU"
ADMIN_ID = 6482440657  # id админа

# ====== Flask Setup ======
app = Flask(__name__, static_folder="static", static_url_path="")
app.config['SECRET_KEY'] = "reviewcash_secret"

# ====== SocketIO ======
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

# ====== Telegram Bot ======
bot = telebot.TeleBot(TOKEN, threaded=False)

# ====== Simple In-Memory DB (demo) ======
USERS = {}
TASKS = []
TOPUPS = []
WITHDRAWS = []

# ========= STATIC FILES ==========
@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/admin")
def admin():
    return send_from_directory("static", "admin.html")


# ========= PROFILE ==========
@app.get("/api/profile_me")
def profile_me():
    uid = request.args.get("uid")
    if not uid:
        return jsonify({"ok": False})

    user = USERS.setdefault(uid, {
        "uid": uid,
        "balance": 0,
        "history": [],
        "tasks": []
    })

    return jsonify({"ok": True, "user": user})


# ========= TASKS ==========
@app.get("/api/tasks/list")
def tasks_list():
    return jsonify({"ok": True, "tasks": TASKS})


@app.post("/api/tasks/create")
def task_create():
    data = request.json
    task = {
        "id": int(time()),
        "owner_uid": data["owner_uid"],
        "title": data["title"],
        "description": data["description"],
        "qty": data["qty"],
        "unit_price": data["unit_price"],
        "url": data["url"],
        "completed_qty": 0
    }
    TASKS.append(task)

    socketio.emit("task_update", {})

    return jsonify({"ok": True})


# ========= TOPUP ==========
@app.post("/api/user/topup-link")
def topup_link():
    data = request.json
    uid = data["uid"]
    amount = data["amount"]

    topup_id = int(time())
    manual_code = f"RC-{topup_id}"

    TOPUPS.append({
        "id": topup_id,
        "uid": uid,
        "amount": amount,
        "manual": manual_code,
        "confirmed": False
    })

    pay_link = "https://www.tbank.ru/cf/AjpqOu4cEzU"

    return jsonify({
        "ok": True,
        "id": topup_id,
        "manual_code": manual_code,
        "pay_link": pay_link,
        "qr_url": "/static/qr.png"
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
                f"💳 Пополнение\nID: {topup_id}\nUID: {uid}\nСумма: {t['amount']} ₽"
            )
            return jsonify({"ok": True})

    return jsonify({"ok": False, "errmsg": "Not found"})


# ========= WITHDRAW ==========
@app.post("/api/user/withdraw")
def withdraw():
    data = request.json
    uid = data["uid"]

    WITHDRAWS.append(data)

    bot.send_message(
        ADMIN_ID,
        f"🤑 Вывод\nUID: {uid}\nСумма: {data['amount']} ₽\nИмя: {data['name']}\nРеквизиты: {data['details']}"
    )

    return jsonify({"ok": True})


# ========= BOT WEBHOOK ==========
@app.post("/bot")
def bot_webhook():
    json_data = request.stream.read().decode("utf-8")
    update = telebot.types.Update.de_json(json_data)
    bot.process_new_updates([update])
    return "ok"


# ========= BOT COMMANDS ==========
@bot.message_handler(commands=["start"])
def start(m):
    bot.send_message(
        m.chat.id,
        "Добро пожаловать!\nОткрой WebApp:",
        reply_markup=telebot.types.InlineKeyboardMarkup().add(
            telebot.types.InlineKeyboardButton("Открыть", web_app=telebot.types.WebAppInfo(url="https://YOUR-RAILWAY-URL"))
        )
    )


# ========= SOCKET EVENTS ==========
@socketio.on("connect")
def socket_connect():
    print("socket connected")


@socketio.on("disconnect")
def socket_disconnect():
    print("socket disconnected")


# ========= RUN SERVER ==========
if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=8080)
