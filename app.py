# app.py
from flask import Flask, request, send_from_directory, jsonify, abort
from flask_socketio import SocketIO
import telebot
import os, json, requests
from time import time

# --------- CONFIG (из твоего сообщения) ----------
TOKEN = "8033069276:AAFv1-kdQ68LjvLEgLHj3ZXd5ehMqyUXOYU"
WEBAPP_URL = "https://web-production-398fb.up.railway.app"  # твой webapp root
ADMIN_ID = 6482440657
REQUIRED_CHANNEL = "@ReviewCashNews"
DATA_FILE = "data.json"
# -------------------------------------------------

# Flask + SocketIO
app = Flask(__name__, static_folder="static", static_url_path="")
app.config["SECRET_KEY"] = "reviewcash_secret"
# async_mode eventlet/gevent preferred in production. If not available, it will fallback.
socketio = SocketIO(app, cors_allowed_origins="*")

# Telebot (we will process incoming webhook updates via /bot)
bot = telebot.TeleBot(TOKEN, threaded=False)

# ================= simple file DB =================
DEFAULT_DATA = {
    "users": {},     # uid -> { uid, balance, history:[], tasks:[] }
    "tasks": [],     # list of task dicts
    "topups": [],    # list of topup dicts
    "withdraws": []  # list of withdraw dicts
}

def load_data():
    if not os.path.exists(DATA_FILE):
        save_data(DEFAULT_DATA)
        return DEFAULT_DATA.copy()
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except Exception:
            return DEFAULT_DATA.copy()

def save_data(d):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)

DATA = load_data()

def get_user(uid):
    users = DATA.setdefault("users", {})
    u = users.get(str(uid))
    if not u:
        u = {"uid": str(uid), "balance": 0, "history": [], "tasks": []}
        users[str(uid)] = u
        save_data(DATA)
    return u

# ================ Static routes ===================
@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/admin")
def admin_page():
    return send_from_directory("static", "admin.html")

@app.route("/task_types.json")
def task_types_json():
    # Provide reasonable default types for frontend
    types = [
        {"id": "ya_review", "name": "Отзыв — Яндекс Карты", "unit_price": 120, "max_qty": 500},
        {"id": "gmaps_review", "name": "Отзыв — Google Maps", "unit_price": 65, "max_qty": 500},
        {"id": "tg_sub", "name": "Подписка — Telegram канал", "unit_price": 10, "max_qty": 100000}
    ]
    return jsonify(types)

# ================ Profile =========================
@app.get("/api/profile_me")
def profile_me():
    uid = request.args.get("uid")
    if not uid:
        return jsonify({"ok": False, "errmsg": "no uid"})
    user = get_user(uid)
    return jsonify({"ok": True, "user": user})

# ================ Tasks ===========================
@app.get("/api/tasks/list")
def tasks_list():
    return jsonify({"ok": True, "tasks": DATA.get("tasks", [])})

@app.post("/api/tasks/create")
def tasks_create():
    data = request.get_json(force=True)
    # basic validation
    required = ["title", "description", "qty", "unit_price", "url", "owner_uid", "type_id"]
    for k in required:
        if k not in data:
            return jsonify({"ok": False, "errmsg": f"missing {k}"}), 400
    t = {
        "id": int(time()*1000),
        "owner_uid": str(data["owner_uid"]),
        "title": data["title"],
        "description": data["description"],
        "qty": int(data.get("qty", 1)),
        "unit_price": float(data.get("unit_price", 0)),
        "url": data["url"],
        "type_id": data.get("type_id"),
        "completed_qty": 0,
        "created_at": int(time())
    }
    DATA.setdefault("tasks", []).append(t)
    save_data(DATA)
    # notify clients
    try:
        socketio.emit("task_update", {"task": t})
    except Exception:
        app.logger.exception("emit task_update failed")
    return jsonify({"ok": True, "task": t})

# ================ Topup ===========================
@app.post("/api/user/topup-link")
def topup_link():
    data = request.get_json(force=True)
    uid = str(data.get("uid", ""))
    amount = data.get("amount")
    if not uid or not amount:
        return jsonify({"ok": False, "errmsg": "uid/amount required"}), 400
    try:
        amount = float(amount)
    except:
        return jsonify({"ok": False, "errmsg": "invalid amount"}), 400

    topup_id = int(time()*1000)
    manual_code = f"RC-{topup_id}"
    topup = {
        "id": topup_id,
        "uid": uid,
        "amount": amount,
        "manual_code": manual_code,
        "confirmed": False,
        "created_at": int(time())
    }
    DATA.setdefault("topups", []).append(topup)
    save_data(DATA)

    # In demo we provide a placeholder pay link and a static qr (you can put real gateway)
    pay_link = "https://tinkoff.ru"  # replace with real payment link if needed

    # notify admin via bot about new topup (optional)
    try:
        bot.send_message(
            ADMIN_ID,
            f"Новая заявка на пополнение\nID: {topup_id}\nUID: {uid}\nСумма: {amount} ₽\nКомментарий: {manual_code}"
        )
    except Exception:
        app.logger.exception("bot.send_message failed for topup notification")

    # notify clients
    try:
        socketio.emit("new_topup", {"topup": topup})
    except Exception:
        app.logger.exception("emit new_topup failed")

    return jsonify({
        "ok": True,
        "id": topup_id,
        "manual_code": manual_code,
        "pay_link": pay_link,
        "qr_url": "/static/qr.png"  # if you have a static qr image
    })

@app.post("/api/user/topup-confirm")
def topup_confirm():
    data = request.get_json(force=True)
    topup_id = data.get("topup_id")
    uid = str(data.get("uid", ""))

    if not topup_id:
        return jsonify({"ok": False, "errmsg": "no id"}), 400

    for t in DATA.get("topups", []):
        if t["id"] == topup_id:
            # Mark confirmed and add to user's balance
            t["confirmed"] = True
            user = get_user(uid)
            user["balance"] = float(user.get("balance", 0)) + float(t.get("amount", 0))
            # add history
            user.setdefault("history", []).append({
                "type": "topup",
                "amount": t.get("amount"),
                "ts": int(time()),
                "note": f"Пополнение {t.get('amount')} ₽"
            })
            save_data(DATA)

            # notify admin
            try:
                bot.send_message(ADMIN_ID, f"Топап подтверждён\nID: {topup_id}\nUID: {uid}\nСумма: {t.get('amount')} ₽")
            except Exception:
                app.logger.exception("bot notify fail")

            # notify clients
            try:
                socketio.emit("user_update", {"user_id": uid, "balance": user["balance"]})
            except Exception:
                app.logger.exception("emit user_update failed")

            return jsonify({"ok": True})
    return jsonify({"ok": False, "errmsg": "not found"}), 404

# ================ Withdraw =======================
@app.post("/api/user/withdraw")
def api_withdraw():
    data = request.get_json(force=True)
    uid = str(data.get("uid", ""))
    amount = data.get("amount")
    name = data.get("name", "")
    details = data.get("details", "")

    if not uid or not amount:
        return jsonify({"ok": False, "errmsg": "uid/amount required"}), 400
    try:
        amount = float(amount)
    except:
        return jsonify({"ok": False, "errmsg": "invalid amount"}), 400

    user = get_user(uid)
    if user.get("balance", 0) < amount:
        return jsonify({"ok": False, "errmsg": "insufficient funds"}), 400

    w = {
        "id": int(time()*1000),
        "uid": uid,
        "amount": amount,
        "name": name,
        "details": details,
        "created_at": int(time())
    }
    DATA.setdefault("withdraws", []).append(w)
    # deduct balance (reserve)
    user["balance"] = float(user.get("balance", 0)) - amount
    user.setdefault("history", []).append({"type": "withdraw", "amount": -amount, "ts": int(time()), "note": f"Вывод {amount} ₽"})
    save_data(DATA)

    # notify admin with withdraw details (telegram)
    try:
        bot.send_message(
            ADMIN_ID,
            f"Запрос на вывод\nID: {w['id']}\nUID: {uid}\nСумма: {amount} ₽\nИмя: {name}\nРеквизиты: {details}"
        )
    except Exception:
        app.logger.exception("bot send withdraw failed")

    # notify clients
    try:
        socketio.emit("user_update", {"user_id": uid, "balance": user["balance"]})
    except Exception:
        app.logger.exception("emit user_update failed")

    return jsonify({"ok": True, "withdraw": w})

# ================ Telegram webhook endpoint ============
@app.post("/bot")
def bot_webhook():
    # Telegram will POST update JSON here
    try:
        json_str = request.get_data().decode("utf-8")
        update = telebot.types.Update.de_json(json_str)
        bot.process_new_updates([update])
    except Exception:
        app.logger.exception("failed to process incoming update")
    return "ok"

# Example command /start
@bot.message_handler(commands=["start"])
def handle_start(message):
    try:
        kb = telebot.types.InlineKeyboardMarkup()
        kb.add(telebot.types.InlineKeyboardButton("Открыть кабинет", web_app=telebot.types.WebAppInfo(url=WEBAPP_URL)))
        # Optional: check subscription - here we just send instructions
        bot.send_message(message.chat.id, f"Привет! Для использования необходимо подписаться на канал {REQUIRED_CHANNEL}.\nОткрой WebApp:", reply_markup=kb)
    except Exception:
        app.logger.exception("send start message failed")

# ================ SocketIO events ====================
@socketio.on("connect")
def handle_connect():
    app.logger.debug("socket connected")

@socketio.on("disconnect")
def handle_disconnect():
    app.logger.debug("socket disconnected")

# ================ Admin helper: set webhook ================
@app.get("/set_webhook")
def set_webhook():
    """
    Convenience endpoint: call /set_webhook to register Telegram webhook to this app.
    You can also run the curl command shown below locally/CI.
    """
    webhook_url = WEBAPP_URL.rstrip("/") + "/bot"
    set_url = f"https://api.telegram.org/bot{TOKEN}/setWebhook"
    try:
        r = requests.post(set_url, json={"url": webhook_url})
        return jsonify({"ok": True, "result": r.json()})
    except Exception as e:
        return jsonify({"ok": False, "errmsg": str(e)}), 500

# ================ Run ================================
if __name__ == "__main__":
    # ensure data saved
    save_data(DATA)
    # NOTE: in production use eventlet/gevent worker runner, e.g. with eventlet:
    #   pip install eventlet
    #   python app.py
    # socketio will auto-detect installed async mode if eventlet/gevent present.
    socketio.run(app, host="0.0.0.0", port=8080)
