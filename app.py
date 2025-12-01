from flask import Flask, request, send_from_directory, jsonify
from flask_socketio import SocketIO
from telebot import TeleBot, types
import json
import time

TOKEN = "8033069276:AAFv1-kdQ68LjvLEgLHj3ZXd5ehMqyUXOYU"
ADMIN_ID = 6482440657

app = Flask(__name__, static_folder="static", static_url_path="")
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

bot = TeleBot(TOKEN, threaded=False)

# ——————————————————————————
# Simple in-memory demo DB
# ——————————————————————————
USERS = {}
TASKS = []
TOPUPS = []
WITHDRAWS = []


# ——————————————————————————
# STATIC FILES
# ——————————————————————————
@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/admin")
def admin():
    return send_from_directory("static", "admin.html")


# ——————————————————————————
# PROFILE
# ——————————————————————————
@app.get("/api/profile_me")
def profile_me():
    uid = request.args.get("uid")
    if not uid:
        return jsonify({"ok": False})

    user = USERS.setdefault(uid, {
        "uid": uid,
        "balance": 0,
        "history": []
    })

    return jsonify({"ok": True, "user": user})


# ——————————————————————————
# TASKS
# ——————————————————————————
@app.get("/api/tasks/list")
def tasks_list():
    return jsonify({"ok": True, "tasks": TASKS})


@app.post("/api/tasks/create")
def task_create():
    data = request.json
    task = {
        "id": int(time.time()),
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


# ——————————————————————————
# TOPUP
# ——————————————————————————
@app.post("/api/user/topup-link")
def topup_link():
    data = request.json
    uid = data["uid"]
    amount = data["amount"]

    top_id = int(time.time())
    code = f"RC-{top_id}"

    TOPUPS.append({
        "id": top_id,
        "uid": uid,
        "amount": amount,
        "code": code,
        "confirmed": False
    })

    return jsonify({
        "ok": True,
        "id": top_id,
        "manual_code": code,
        "qr_url": "/static/qr.png",
        "pay_link": "https://www.tbank.ru/cf/AjpqOu4cEzU"
    })


@app.post("/api/user/topup-confirm")
def topup_confirm():
    data = request.json
    top_id = data["topup_id"]
    uid = data["uid"]

    for t in TOPUPS:
        if t["id"] == top_id:
            bot.send_message(
                ADMIN_ID,
                f"💳 Пополнение\nID {top_id}\nUID {uid}\nСумма {t['amount']} ₽"
            )
            return jsonify({"ok": True})

    return jsonify({"ok": False})


# ——————————————————————————
# WITHDRAW
# ——————————————————————————
@app.post("/api/user/withdraw")
def withdraw():
    data = request.json

    WITHDRAWS.append(data)

    bot.send_message(
        ADMIN_ID,
        f"🤑 Вывод\nUID {data['uid']}\nСумма: {data['amount']} ₽\nИмя: {data['name']}\nРеквизиты: {data['details']}"
    )

    return jsonify({"ok": True})


# ——————————————————————————
# BOT WEBHOOK
# ——————————————————————————
@app.post("/bot")
def bot_webhook():
    update = types.Update.de_json(request.data.decode("utf-8"))
    bot.process_new_updates([update])
    return "ok"


# ——————————————————————————
# BOT COMMANDS — FIXED
# ——————————————————————————
@bot.message_handler(commands=["start"])
def start(m):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(
        "Открыть приложение",
        web_app=types.WebAppInfo(url="https://web-production-398fb.up.railway.app/")
    ))

    bot.send_message(
        m.chat.id,
        "Добро пожаловать в ReviewCash!\n\nНажмите кнопку ниже чтобы открыть приложение:",
        reply_markup=kb
    )


@bot.message_handler(commands=["balance"])
def balance(m):
    uid = str(m.chat.id)
    bal = USERS.get(uid, {}).get("balance", 0)
    bot.send_message(m.chat.id, f"Ваш баланс: {bal} ₽")


@bot.message_handler(commands=["help"])
def help_cmd(m):
    bot.send_message(m.chat.id, "Доступные команды:\n/start\n/balance\n/help\n/info")


@bot.message_handler(commands=["info"])
def info(m):
    bot.send_message(m.chat.id, "ReviewCash — сервис для заданий и отзывов.")

# ========= MAIN ADMIN ==========
@bot.message_handler(commands=["mainadmin"])
def main_admin(m):
    if m.chat.id != ADMIN_ID:
        bot.send_message(m.chat.id, "⛔ У вас нет доступа к админ-панели.")
        return
    
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton(
            "Открыть ADMIN панель",
            web_app=types.WebAppInfo(url="https://web-production-398fb.up.railway.app/admin")
        )
    )

    bot.send_message(
        m.chat.id,
        "💼 Админ панель:",
        reply_markup=kb
    )


# ========= MODERATOR PANEL ==========
MODERATORS = {6482440657}  # можно добавлять

@bot.message_handler(commands=["mod"])
def moderator_panel(m):
    if m.chat.id not in MODERATORS and m.chat.id != ADMIN_ID:
        bot.send_message(m.chat.id, "⛔ У вас нет доступа к панели модератора.")
        return

    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton(
            "Открыть панель модератора",
            web_app=types.WebAppInfo(url="https://web-production-398fb.up.railway.app/moderator")
        )
    )

    bot.send_message(
        m.chat.id,
        "🛠 Панель модератора:",
        reply_markup=kb
    )

# ——————————————————————————
# Websocket
# ——————————————————————————
@socketio.on("connect")
def conn():
    print("socket connected")


@socketio.on("disconnect")
def disc():
    print("socket disconnected")


# ——————————————————————————
# RUN SERVER
# ——————————————————————————
if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=8080)
