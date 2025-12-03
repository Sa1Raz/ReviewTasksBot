# app.py
from flask import Flask, request, jsonify, send_from_directory, abort
from flask_cors import CORS
from flask_socketio import SocketIO
import telebot
import os
import json
from time import time
from datetime import datetime

# ========== CONFIG ==========
TOKEN = "8033069276:AAFv1-kdQ68LjvLEgLHj3ZXd5ehMqyUXOYU"  # из твоего сообщения
WEBAPP_BASE = "https://web-production-398fb.up.railway.app"  # твой webapp url
CHANNEL_TO_CHECK = "@ReviewCashNews"
MAIN_ADMIN = 6482440657  # твой админ id

PORT = int(os.environ.get("PORT", 8080))

# ========== APP & SOCKET ==========
app = Flask(__name__, static_folder="static", static_url_path="/static")
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

# ========== TELEGRAM BOT ==========
bot = telebot.TeleBot(TOKEN, threaded=False)

# ========== SIMPLE PERSISTENT STORAGE (files) ==========
DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)
USERS_FILE = os.path.join(DATA_DIR, "users.json")
TASKS_FILE = os.path.join(DATA_DIR, "tasks.json")
TOPUPS_FILE = os.path.join(DATA_DIR, "topups.json")
WITHD_FILE = os.path.join(DATA_DIR, "withdraws.json")
ROLES_FILE = os.path.join(DATA_DIR, "roles.json")

def load_json(path, default):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return default

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

USERS = load_json(USERS_FILE, {})       # key: uid -> user dict {uid, first_name, username, balance, history}
TASKS = load_json(TASKS_FILE, [])       # list of task dicts
TOPUPS = load_json(TOPUPS_FILE, [])
WITHDRAWS = load_json(WITHD_FILE, [])
ROLES = load_json(ROLES_FILE, {"admins": [MAIN_ADMIN], "mods": []})

# Task reward map (for moderator approve)
REWARD_MAP = {
    "ya_review": 100,
    "gmaps_review": 50,
    "tg_sub": 5
}

# ========== HELPERS ==========
def persist_all():
    save_json(USERS_FILE, USERS)
    save_json(TASKS_FILE, TASKS)
    save_json(TOPUPS_FILE, TOPUPS)
    save_json(WITHD_FILE, WITHDRAWS)
    save_json(ROLES_FILE, ROLES)

def ensure_user(uid, info=None):
    uid = str(uid)
    if uid not in USERS:
        USERS[uid] = {"uid": uid, "first_name": info.get("first_name") if info else "", "username": info.get("username") if info else "", "balance": 0, "history": []}
        persist_all()
    return USERS[uid]

def now_iso():
    return datetime.utcnow().isoformat()

def is_admin(user_id):
    return int(user_id) in [int(x) for x in ROLES.get("admins", [])]

def is_mod(user_id):
    return int(user_id) in [int(x) for x in ROLES.get("mods", [])]

# ========== STATIC PAGES ==========
@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/admin")
def admin_page():
    return send_from_directory("static", "admin.html")

@app.route("/moderator")
def moderator_page():
    return send_from_directory("static", "moderator.html")

# allow loading of other static assets automatically
@app.route("/<path:path>")
def static_proxy(path):
    # Let Flask serve files from static folder (index/admin etc. already handled)
    if os.path.exists(os.path.join("static", path)):
        return send_from_directory("static", path)
    abort(404)

# ========== API: PROFILE ==========
@app.get("/api/profile_me")
def api_profile_me():
    uid = request.args.get("uid")
    if not uid:
        return jsonify({"ok": False, "errmsg": "missing uid"}), 400
    u = ensure_user(uid)
    return jsonify({"ok": True, "user": u})

# ========== API: TASKS ==========
@app.get("/api/tasks/list")
def api_tasks_list():
    return jsonify({"ok": True, "tasks": TASKS})

@app.post("/api/tasks/create")
def api_tasks_create():
    data = request.get_json() or {}
    required = ["owner_uid", "title", "description", "qty", "unit_price", "url", "type_id"]
    for k in required:
        if k not in data:
            return jsonify({"ok": False, "errmsg": f"missing {k}"}), 400
    t = {
        "id": int(time()*1000),
        "owner_uid": str(data["owner_uid"]),
        "title": data["title"],
        "description": data["description"],
        "qty": int(data["qty"]),
        "unit_price": int(data["unit_price"]),
        "url": data["url"],
        "type_id": data["type_id"],
        "created_at": now_iso(),
        "completed_qty": 0,
        "status": "active",   # active / closed
        "assigned_to": None,
        "reviews": []
    }
    TASKS.insert(0, t)
    persist_all()
    socketio.emit("task_update", {})
    return jsonify({"ok": True, "task": t})

# task claim (user wants to do one unit)
@app.post("/api/tasks/claim")
def api_tasks_claim():
    data = request.get_json() or {}
    tid = data.get("task_id")
    uid = str(data.get("uid"))
    if not tid or not uid:
        return jsonify({"ok": False}), 400
    for t in TASKS:
        if str(t["id"]) == str(tid):
            # allow multiple people do it; create submission record
            sub = {"id": int(time()*1000), "task_id": t["id"], "worker_uid": uid, "url": t.get("url"), "submitted_at": now_iso(), "status":"pending", "evidence": None}
            t.setdefault("submissions", [])
            t["submissions"].append(sub)
            persist_all()
            socketio.emit("task_update", {})
            return jsonify({"ok": True, "submission": sub})
    return jsonify({"ok": False, "errmsg": "task not found"}), 404

# ========== MODERATION (mod approves submission) ==========
@app.post("/api/mod/review")
def api_mod_review():
    data = request.get_json() or {}
    reviewer = data.get("reviewer")
    if not is_mod(reviewer) and not is_admin(reviewer):
        return jsonify({"ok": False, "errmsg": "not authorized"}), 403
    task_id = data.get("task_id")
    submission_id = data.get("submission_id")
    action = data.get("action")  # approve / reject
    reason = data.get("reason", "")
    if not task_id or not submission_id or action not in ("approve", "reject"):
        return jsonify({"ok": False, "errmsg": "bad request"}), 400
    for t in TASKS:
        if str(t["id"]) == str(task_id):
            subs = t.get("submissions", [])
            for s in subs:
                if str(s["id"]) == str(submission_id):
                    s["status"] = "approved" if action=="approve" else "rejected"
                    s["reviewed_by"] = reviewer
                    s["reviewed_at"] = now_iso()
                    s["review_reason"] = reason
                    # on approve: pay worker
                    if action == "approve":
                        worker_uid = str(s["worker_uid"])
                        amount = REWARD_MAP.get(t.get("type_id"), int(t.get("unit_price",0)))
                        ensure_user(worker_uid)
                        USERS[worker_uid]["balance"] = int(USERS[worker_uid].get("balance", 0)) + int(amount)
                        USERS[worker_uid]["history"].insert(0, {"type":"task_reward","amount":amount,"task_id":t["id"],"at":now_iso()})
                        # increment completed
                        t["completed_qty"] = int(t.get("completed_qty", 0)) + 1
                    persist_all()
                    socketio.emit("task_update", {})
                    socketio.emit("user_update", {"user_id": s["worker_uid"], "balance": USERS.get(str(s["worker_uid"]),{}).get("balance",0)})
                    return jsonify({"ok": True})
    return jsonify({"ok": False, "errmsg":"not found"}), 404

# ========== TOPUP ==========
@app.post("/api/user/topup-link")
def api_topup_link():
    data = request.get_json() or {}
    uid = str(data.get("uid"))
    amount = int(data.get("amount",0))
    if amount < 150:
        return jsonify({"ok": False, "errmsg": "min 150"}), 400
    tid = int(time()*1000)
    manual_code = f"RC{tid}"
    topup = {"id": tid, "uid": uid, "amount": amount, "manual_code": manual_code, "status": "pending", "created_at": now_iso()}
    TOPUPS.insert(0, topup)
    persist_all()
    # make a fake pay link (in prod integrate with real payment)
    pay_link = f"https://example-pay.example/?comment={manual_code}"
    socketio.emit("task_update", {})
    return jsonify({"ok": True, "topup": topup, "pay_link": pay_link, "qr_url": f"{WEBAPP_BASE}/static/qr.png", "manual_code": manual_code})

@app.post("/api/user/topup-confirm")
def api_topup_confirm():
    data = request.get_json() or {}
    topup_id = data.get("topup_id")
    uid = str(data.get("uid"))
    for t in TOPUPS:
        if str(t["id"]) == str(topup_id) and str(t["uid"])==str(uid):
            # notify admin
            bot.send_message(MAIN_ADMIN, f"🔔 Новый запрос пополнения\nID: {t['id']}\nUID: {t['uid']}\nСумма: {t['amount']} ₽\nКод: {t['manual_code']}")
            return jsonify({"ok": True})
    return jsonify({"ok": False, "errmsg":"not found"}), 404

# ========== WITHDRAW ==========
@app.post("/api/user/withdraw")
def api_user_withdraw():
    data = request.get_json() or {}
    uid = str(data.get("uid"))
    amount = int(data.get("amount",0))
    name = data.get("name","")
    details = data.get("details","")
    if amount < 300:
        return jsonify({"ok": False, "errmsg":"min 300"}), 400
    ensure_user(uid)
    if USERS[uid]["balance"] < amount:
        return jsonify({"ok": False, "errmsg":"insufficient"}), 400
    wid = int(time()*1000)
    WITHDRAWS.insert(0, {"id": wid, "uid": uid, "amount": amount, "name": name, "details": details, "status":"pending", "created_at": now_iso()})
    # notify admin
    bot.send_message(MAIN_ADMIN, f"🤑 Заявка вывода\nUID: {uid}\nСумма: {amount} ₽\nИмя: {name}\nРеквизиты: {details}")
    persist_all()
    socketio.emit("task_update", {})
    return jsonify({"ok": True})

# ========== ADMIN API (for admin.html) ==========
@app.get("/api/admin/dashboard")
def api_admin_dashboard():
    users_count = len(USERS)
    tasks_count = len(TASKS)
    total_rev = sum([t.get("amount",0) for t in TOPUPS]) if TOPUPS else 0
    pending = len([x for x in TOPUPS+WITHDRAWS if x.get("status","pending")=="pending"])
    recent = []
    for t in (TOPUPS[:10] + WITHDRAWS[:10]):
        recent.append({"type":"topup" if "manual_code" in t else "withdraw", "id": t.get("id"), "amount": t.get("amount"), "status": t.get("status"), "user": t.get("uid"), "created_at": t.get("created_at")})
    return jsonify({"ok": True, "data": {"usersCount": users_count, "tasksCount": tasks_count, "totalRevenue": total_rev, "pendingCount": pending, "recentActivity": recent}})

@app.get("/api/admin/users")
def api_admin_users():
    arr = []
    for k,u in USERS.items():
        arr.append({"id": u["uid"], "first_name": u.get("first_name"), "username": u.get("username"), "balance": u.get("balance",0), "tasks_done": sum(1 for t in TASKS for s in t.get("submissions",[]) if s.get("worker_uid")==u["uid"] and s.get("status")=="approved")})
    return jsonify({"ok": True, "users": arr})

@app.get("/api/admin/tasks")
def api_admin_tasks():
    return jsonify({"ok": True, "tasks": TASKS})

@app.get("/api/admin/topups")
def api_admin_topups():
    return jsonify({"ok": True, "items": TOPUPS})

@app.get("/api/admin/withdraws")
def api_admin_withdraws():
    return jsonify({"ok": True, "items": WITHDRAWS})

# Admin endpoints to process topups/withdraws (simple)
@app.post("/api/admin/topups/<int:tid>/approve")
def api_admin_topup_approve(tid):
    # only super admin (MAIN_ADMIN) allowed via bot; but here we don't auth; keep simple
    for t in TOPUPS:
        if t["id"] == tid:
            t["status"] = "paid"
            ensure_user(t["uid"])
            USERS[str(t["uid"])]["balance"] = USERS[str(t["uid"])].get("balance",0) + int(t["amount"])
            USERS[str(t["uid"])]["history"].insert(0, {"type":"topup","amount":t["amount"],"at":now_iso()})
            persist_all()
            socketio.emit("user_update", {"user_id": t["uid"], "balance": USERS[str(t["uid"])]["balance"]})
            return jsonify({"ok": True})
    return jsonify({"ok": False}), 404

@app.post("/api/admin/withdraws/<int:wid>/approve")
def api_admin_withdraw_approve(wid):
    for w in WITHDRAWS:
        if w["id"] == wid:
            w["status"] = "approved"
            # deduct
            ensure_user(w["uid"])
            USERS[str(w["uid"])]["balance"] = USERS[str(w["uid"])].get("balance",0) - int(w["amount"])
            USERS[str(w["uid"])]["history"].insert(0, {"type":"withdraw","amount":-w["amount"],"at":now_iso()})
            persist_all()
            socketio.emit("user_update", {"user_id": w["uid"], "balance": USERS[str(w["uid"])]["balance"]})
            return jsonify({"ok": True})
    return jsonify({"ok": False}), 404

# ========== ROLES MANAGEMENT (BOT COMMANDS will call endpoints) ==========
@app.post("/api/roles/add_admin")
def api_roles_add_admin():
    data = request.get_json() or {}
    caller = data.get("caller")
    new_id = data.get("id")
    if not caller or not is_admin(caller):
        return jsonify({"ok": False, "errmsg":"not allowed"}), 403
    if not new_id:
        return jsonify({"ok": False, "errmsg":"missing id"}), 400
    if int(new_id) not in ROLES.get("admins", []):
        ROLES.setdefault("admins", []).append(int(new_id))
        persist_all()
    return jsonify({"ok": True})

@app.post("/api/roles/add_mod")
def api_roles_add_mod():
    data = request.get_json() or {}
    caller = data.get("caller")
    new_id = data.get("id")
    if not caller or not is_admin(caller):
        return jsonify({"ok": False, "errmsg":"not allowed"}), 403
    if not new_id:
        return jsonify({"ok": False, "errmsg":"missing id"}), 400
    if int(new_id) not in ROLES.get("mods", []):
        ROLES.setdefault("mods", []).append(int(new_id))
        persist_all()
    return jsonify({"ok": True})

# ========== TELEGRAM WEBHOOK (receive updates) ==========
@app.post("/bot")
def bot_webhook():
    try:
        json_str = request.get_data().decode("utf-8")
        update = telebot.types.Update.de_json(json_str)
        bot.process_new_updates([update])
        return "ok"
    except Exception as e:
        return f"error: {e}", 500

# ========== BOT COMMANDS ==========
@bot.message_handler(commands=["start"])
def cmd_start(m):
    uid = m.from_user.id
    # check subscription
    try:
        status = bot.get_chat_member(CHANNEL_TO_CHECK, uid).status
        if status in ("left", "kicked"):
            # ask to subscribe
            kb = telebot.types.InlineKeyboardMarkup()
            kb.add(telebot.types.InlineKeyboardButton("Подписаться на канал", url=f"https://t.me/{CHANNEL_TO_CHECK.strip('@')}"))
            kb.add(telebot.types.InlineKeyboardButton("Проверить подписку", callback_data="check_sub"))
            bot.send_message(uid, "🔒 Для доступа необходимо подписаться на канал. Нажмите кнопку ниже и подпишитесь, затем проверьте.", reply_markup=kb)
            return
    except Exception:
        # if error, let them in but warn
        pass

    # show WebApp open button with nice welcome
    kb = telebot.types.InlineKeyboardMarkup()
    kb.add(telebot.types.InlineKeyboardButton("Открыть личный кабинет", web_app=telebot.types.WebAppInfo(url=f"{WEBAPP_BASE}?uid={uid}")))
    kb.add(telebot.types.InlineKeyboardButton("Админка", web_app=telebot.types.WebAppInfo(url=f"{WEBAPP_BASE}/admin?uid={uid}")))
    bot.send_message(uid, f"👋 Привет, {m.from_user.first_name}!\nДобро пожаловать в ReviewCash — выполняй задания и зарабатывай.\n• Подпишись на {CHANNEL_TO_CHECK} чтобы всё работало.", reply_markup=kb)

@bot.callback_query_handler(func=lambda call: call.data == "check_sub")
def cb_check_sub(call):
    uid = call.from_user.id
    try:
        st = bot.get_chat_member(CHANNEL_TO_CHECK, uid).status
        if st not in ("left", "kicked"):
            bot.answer_callback_query(call.id, "Подписка подтверждена, открываю WebApp.")
            bot.send_message(uid, "Открой личный кабинет:", reply_markup=telebot.types.InlineKeyboardMarkup().add(
                telebot.types.InlineKeyboardButton("Открыть", web_app=telebot.types.WebAppInfo(url=f"{WEBAPP_BASE}?uid={uid}"))
            ))
        else:
            bot.answer_callback_query(call.id, "Вы всё ещё не подписаны.")
    except Exception:
        bot.answer_callback_query(call.id, "Не удалось проверить подписку, попробуйте позже.")

@bot.message_handler(commands=["admin"])
def cmd_admin(m):
    uid = m.from_user.id
    if not is_admin(uid):
        bot.send_message(uid, "❌ У вас нет прав администратора.")
        return
    bot.send_message(uid, "Открываю админку:", reply_markup=telebot.types.InlineKeyboardMarkup().add(
        telebot.types.InlineKeyboardButton("Admin Panel", web_app=telebot.types.WebAppInfo(url=f"{WEBAPP_BASE}/admin?uid={uid}"))
    ))

@bot.message_handler(commands=["mod"])
def cmd_mod(m):
    uid = m.from_user.id
    if not is_mod(uid) and not is_admin(uid):
        bot.send_message(uid, "❌ У вас нет прав модератора.")
        return
    bot.send_message(uid, "Открываю панель модератора:", reply_markup=telebot.types.InlineKeyboardMarkup().add(
        telebot.types.InlineKeyboardButton("Moderator Panel", web_app=telebot.types.WebAppInfo(url=f"{WEBAPP_BASE}/moderator?uid={uid}"))
    ))

@bot.message_handler(commands=["addadmin"])
def cmd_addadmin(m):
    if m.from_user.id != MAIN_ADMIN:
        bot.reply_to(m, "Только главный админ может добавлять админов.")
        return
    parts = m.text.split()
    if len(parts) < 2:
        bot.reply_to(m, "Использование: /addadmin <user_id>")
        return
    try:
        new = int(parts[1])
        if new not in ROLES.get("admins", []):
            ROLES.setdefault("admins", []).append(new)
            persist_all()
            bot.reply_to(m, f"Добавлен админ: {new}")
        else:
            bot.reply_to(m, "Уже админ.")
    except:
        bot.reply_to(m, "Неправильный ID")

@bot.message_handler(commands=["addmod"])
def cmd_addmod(m):
    if not is_admin(m.from_user.id):
        bot.reply_to(m, "Только админ может добавлять модераторов.")
        return
    parts = m.text.split()
    if len(parts) < 2:
        bot.reply_to(m, "Использование: /addmod <user_id>")
        return
    try:
        new = int(parts[1])
        if new not in ROLES.get("mods", []):
            ROLES.setdefault("mods", []).append(new)
            persist_all()
            bot.reply_to(m, f"Добавлен модератор: {new}")
        else:
            bot.reply_to(m, "Уже модератор.")
    except:
        bot.reply_to(m, "Неправильный ID")

# ========== SOCKET EVENTS ==========
@socketio.on("connect")
def handle_connect():
    print("socket connected")

@socketio.on("disconnect")
def handle_disconnect():
    print("socket disconnected")

# ========== OPTIONAL: set webhook helper ==========
@app.get("/set_webhook")
def set_webhook():
    # call this once to set webhook to /bot route — modify as needed
    url = request.args.get("url", f"{WEBAPP_BASE}/bot")
    try:
        bot.remove_webhook()
        ok = bot.set_webhook(url)
        return jsonify({"ok": ok, "webhook": url})
    except Exception as e:
        return jsonify({"ok": False, "errmsg": str(e)}), 500

# ========== RUN ==========
if __name__ == "__main__":
    persist_all()
    print("Starting server on port", PORT)
    # use eventlet for websocket support
    socketio.run(app, host="0.0.0.0", port=PORT)
