from flask import Flask, request, send_from_directory, jsonify
from flask_socketio import SocketIO, emit
import telebot
import os
import json
from time import time
import requests

# -----------------------------
# CONFIG
# -----------------------------
TOKEN = "8033069276:AAFv1-kdQ68LjvLEgLHj3ZXd5ehMqyUXOYU"
ADMIN_ID = 6482440657
REQUIRED_CHANNEL = "@ReviewCashNews"

WEBAPP_URL = "https://web-production-398fb.up.railway.app/"
STATIC_DIR = "static"
DB_DIR = "db"

os.makedirs(DB_DIR, exist_ok=True)

app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="")
app.config['SECRET_KEY'] = "reviewcash_secret"

socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

bot = telebot.TeleBot(TOKEN, threaded=False)

# -----------------------------
# DB LOAD/SAVE
# -----------------------------
DB_FILES = {
    "users": "users.json",
    "tasks": "tasks.json",
    "task_types": "task_types.json",
    "topups": "topups.json",
    "withdraws": "withdraws.json",
    "admin_stats": "admin_stats.json"
}

def load(name):
    path = f"{DB_DIR}/{DB_FILES[name]}"
    if not os.path.exists(path):
        with open(path, "w", encoding="utf8") as f:
            f.write("[]")
    with open(path, "r", encoding="utf8") as f:
        return json.load(f)

def save(name, data):
    with open(f"{DB_DIR}/{DB_FILES[name]}", "w", encoding="utf8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# -----------------------------
# UTILS
# -----------------------------
def get_user(uid):
    users = load("users")
    for u in users:
        if str(u["uid"]) == str(uid):
            return u
    # create if not exist
    new_u = {
        "uid": str(uid),
        "first_name": "",
        "username": "",
        "avatar": "",
        "role": "user",
        "balance": 0,
        "tasks_done": 0
    }
    users.append(new_u)
    save("users", users)
    return new_u

def update_user(user):
    users = load("users")
    for i, u in enumerate(users):
        if str(u["uid"]) == str(user["uid"]):
            users[i] = user
            save("users", users)
            return

def broadcast_user(uid):
    socketio.emit("user_update", {"user_id": uid})

def broadcast_tasks():
    socketio.emit("tasks_update", {})

# -----------------------------
# CHECK TG SUBSCRIPTION
# -----------------------------
def is_subscribed(user_id):
    try:
        r = bot.get_chat_member(REQUIRED_CHANNEL, user_id)
        return r.status in ["member", "administrator", "creator"]
    except:
        return False

# -----------------------------
# ROUTES: STATIC
# -----------------------------
@app.route("/")
def index_page():
    return send_from_directory(STATIC_DIR, "index.html")

@app.route("/admin")
def admin_page():
    return send_from_directory(STATIC_DIR, "admin.html")

@app.route("/moder")
def moder_page():
    return send_from_directory(STATIC_DIR, "moderator.html")

# -----------------------------
# API: PROFILE
# -----------------------------
@app.get("/api/profile")
def api_profile():
    uid = request.args.get("uid")
    u = get_user(uid)
    return jsonify({"ok": True, "user": u})

# -----------------------------
# API: TASK TYPES (ADMIN)
# -----------------------------
@app.get("/api/task_types")
def api_task_types():
    return jsonify({"ok": True, "types": load("task_types")})

@app.post("/api/task_type/add")
def api_task_add():
    d = request.json
    types = load("task_types")
    new_t = {
        "id": int(time()),
        "title": d["title"],
        "price": d["price"]
    }
    types.append(new_t)
    save("task_types", types)
    return jsonify({"ok": True})

# -----------------------------
# API: TASKS
# -----------------------------
@app.get("/api/tasks")
def api_tasks():
    return jsonify({"ok": True, "tasks": load("tasks")})

@app.post("/api/task/create")
def api_task_create():
    d = request.json
    uid = d["owner_uid"]
    amount = d["qty"] * d["unit_price"]

    user = get_user(uid)
    if user["balance"] < amount:
        return jsonify({"ok": False, "msg": "Недостаточно средств"})

    user["balance"] -= amount
    update_user(user)
    broadcast_user(uid)

    tasks = load("tasks")
    new_task = {
        "id": int(time()),
        "owner_uid": uid,
        "type_id": d["type_id"],
        "title": d["title"],
        "url": d["url"],
        "qty": d["qty"],
        "unit_price": d["unit_price"],
        "done": 0,
        "performers": [],
        "status": "active"
    }
    tasks.append(new_task)
    save("tasks", tasks)
    broadcast_tasks()

    return jsonify({"ok": True})

@app.post("/api/task/complete")
def api_task_complete():
    d = request.json
    task_id = d["task_id"]
    performer = d["uid"]

    tasks = load("tasks")
    for t in tasks:
        if t["id"] == task_id and t["status"] == "active":
            if performer in t["performers"]:
                return jsonify({"ok": False, "msg": "Вы уже выполняли это задание"})

            t["performers"].append(performer)
            save("tasks", tasks)

            # notify moderator
            bot.send_message(
                ADMIN_ID,
                f"🔎 Новое выполнение\nTask {task_id}\nUser {performer}"
            )
            return jsonify({"ok": True})

    return jsonify({"ok": False})

# -----------------------------
# TOPUP / WITHDRAW
# -----------------------------
@app.post("/api/topup")
def api_topup():
    d = request.json
    uid = d["uid"]
    amount = int(d["amount"])

    topups = load("topups")
    topup = {
        "id": int(time()),
        "uid": uid,
        "amount": amount,
        "timestamp": int(time()),
        "status": "pending",
        "manual_code": f"RC-{int(time())}"
    }
    topups.append(topup)
    save("topups", topups)

    bot.send_message(ADMIN_ID, f"💳 Новое пополнение\nUID: {uid}\nСумма: {amount}")

    return jsonify({"ok": True, "pay_link": "https://www.tbank.ru/cf/AjpqOu4cEzU"})

@app.post("/api/withdraw")
def api_withdraw():
    d = request.json
    uid = d["uid"]
    amount = int(d["amount"])

    user = get_user(uid)
    if user["balance"] < amount:
        return jsonify({"ok": False, "msg": "Недостаточно средств"})

    withdraws = load("withdraws")
    w = {
        "id": int(time()),
        "uid": uid,
        "amount": amount,
        "name": d["name"],
        "details": d["details"],
        "status": "pending",
        "timestamp": int(time())
    }
    withdraws.append(w)
    save("withdraws", withdraws)

    user["balance"] -= amount
    update_user(user)
    broadcast_user(uid)

    bot.send_message(
        ADMIN_ID,
        f"💸 Новый вывод\nUID: {uid}\nСумма: {amount}₽\nИмя: {d['name']}\nРеквизиты: {d['details']}"
    )
    return jsonify({"ok": True})

# -----------------------------
# WEBHOOK
# -----------------------------
@app.post("/bot")
def bot_webhook():
    data = request.stream.read().decode("utf-8")
    update = telebot.types.Update.de_json(data)
    bot.process_new_updates([update])
    return "ok"

# -----------------------------
# BOT COMMANDS
# -----------------------------
@bot.message_handler(commands=["start"])
def start_cmd(m):
    if not is_subscribed(m.from_user.id):
        bot.send_message(m.chat.id, "⚠️ Чтобы получить доступ, подпишитесь на канал:\n" + REQUIRED_CHANNEL)
        return

    u = get_user(m.from_user.id)
    u["first_name"] = m.from_user.first_name or ""
    u["username"] = m.from_user.username or ""
    u["avatar"] = m.from_user.photo_url if hasattr(m.from_user, "photo_url") else ""
    update_user(u)

    kb = telebot.types.InlineKeyboardMarkup()
    kb.add(
        telebot.types.InlineKeyboardButton(
            "Открыть ReviewCash",
            web_app=telebot.types.WebAppInfo(url=WEBAPP_URL)
        )
    )

    bot.send_message(
        m.chat.id,
        "✨ Добро пожаловать в ReviewCash!\nЗарабатывай на отзывах и заданиях!",
        reply_markup=kb
    )

@bot.message_handler(commands=["admin", "mainadmin"])
def admin_open(m):
    u = get_user(m.from_user.id)
    if u["role"] not in ["admin", "superadmin"]:
        bot.reply_to(m, "⛔ Доступ запрещён")
        return

    kb = telebot.types.InlineKeyboardMarkup()
    kb.add(
        telebot.types.InlineKeyboardButton(
            "🛠 Открыть Admin Panel",
            web_app=telebot.types.WebAppInfo(url=WEBAPP_URL + "admin")
        )
    )
    bot.send_message(m.chat.id, "Открываю панель администратора:", reply_markup=kb)

@bot.message_handler(commands=["mod", "moder"])
def moder_open(m):
    u = get_user(m.from_user.id)
    if u["role"] not in ["moderator", "admin", "superadmin"]:
        bot.reply_to(m, "⛔ Доступ запрещён")
        return

    kb = telebot.types.InlineKeyboardMarkup()
    kb.add(
        telebot.types.InlineKeyboardButton(
            "🛠 Открыть панель модератора",
            web_app=telebot.types.WebAppInfo(url=WEBAPP_URL + "moder")
        )
    )
    bot.send_message(m.chat.id, "Открываю панель модератора:", reply_markup=kb)

# -----------------------------
# ROLE COMMANDS
# -----------------------------
@bot.message_handler(commands=["add_admin"])
def add_admin(m):
    if m.from_user.id != ADMIN_ID:
        return

    try:
        uid = m.text.split()[1]
        u = get_user(uid)
        u["role"] = "admin"
        update_user(u)
        bot.reply_to(m, f"🎉 Пользователь {uid} теперь админ")
    except:
        bot.reply_to(m, "Ошибка")

@bot.message_handler(commands=["add_moder"])
def add_moder(m):
    if m.from_user.id != ADMIN_ID:
        return

    try:
        uid = m.text.split()[1]
        u = get_user(uid)
        u["role"] = "moderator"
        update_user(u)
        bot.reply_to(m, f"🛡 Пользователь {uid} теперь модератор")
    except:
        bot.reply_to(m, "Ошибка")

@bot.message_handler(commands=["remove_admin"])
def remove_admin(m):
    if m.from_user.id != ADMIN_ID:
        return

    try:
        uid = m.text.split()[1]
        u = get_user(uid)
        u["role"] = "user"
        update_user(u)
        bot.reply_to(m, f"❌ Админка снята с {uid}")
    except:
        bot.reply_to(m, "Ошибка")

@bot.message_handler(commands=["remove_moder"])
def remove_moder(m):
    if m.from_user.id != ADMIN_ID:
        return

    try:
        uid = m.text.split()[1]
        u = get_user(uid)
        u["role"] = "user"
        update_user(u)
        bot.reply_to(m, f"❌ Модера сняли с {uid}")
    except:
        bot.reply_to(m, "Ошибка")


# -----------------------------
# MAIN
# -----------------------------
if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=8080)
