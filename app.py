# app.py
# Полностью рабочий пример backend для ReviewCash (dev/demo)
# - Flask + Flask-SocketIO
# - Простая файловая БД (JSON)
# - Endpoints: profile, tasks, task_types, topup, topup-confirm, withdraw, admin lists
# - Emits WebSocket-сообщений без broadcast keyword (socketio.emit(...))
# NOTE: Для продакшена нужно заменить dev-сервер и хранение на реальную БД.

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from flask_socketio import SocketIO
import os
import json
import uuid
import time
import threading

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(APP_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

TASKS_FILE = os.path.join(DATA_DIR, "tasks.json")
USERS_FILE = os.path.join(DATA_DIR, "users.json")
TOPUPS_FILE = os.path.join(DATA_DIR, "topups.json")
WITHDRAWS_FILE = os.path.join(DATA_DIR, "withdraws.json")
TASK_TYPES_FILE = os.path.join(DATA_DIR, "task_types.json")

def load_json(path, default):
    try:
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                json.dump(default, f, ensure_ascii=False, indent=2)
            return default
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def save_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)

# Initialize simple storage
tasks = load_json(TASKS_FILE, [])
users = load_json(USERS_FILE, {})
topups = load_json(TOPUPS_FILE, [])
withdraws = load_json(WITHDRAWS_FILE, [])
default_task_types = [
    {"id":"ya_review","name":"Отзыв — Яндекс Карты","unit_price":120,"max_qty":150},
    {"id":"gmaps_review","name":"Отзыв — Google Maps","unit_price":65,"max_qty":200},
    {"id":"tg_sub","name":"Подписка — Telegram канал","unit_price":10,"max_qty":100000}
]
task_types = load_json(TASK_TYPES_FILE, default_task_types)

app = Flask(__name__, static_folder=".")
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*")

### Utilities
def gen_id(prefix="it"):
    return prefix + "_" + uuid.uuid4().hex[:12]

def get_user(uid):
    u = users.get(uid)
    if not u:
        # create basic user
        u = {
            "id": uid,
            "name": f"User {uid[:6]}",
            "username": uid,
            "balance": 0,
            "history": []
        }
        users[uid] = u
        save_json(USERS_FILE, users)
    return u

def persist_all():
    save_json(TASKS_FILE, tasks)
    save_json(USERS_FILE, users)
    save_json(TOPUPS_FILE, topups)
    save_json(WITHDRAWS_FILE, withdraws)
    save_json(TASK_TYPES_FILE, task_types)

### Static file helpers
@app.route("/task_types.json")
def serve_task_types_json():
    return jsonify(task_types)

@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory(APP_DIR, filename)

### API
@app.route("/api/tasks/list")
def api_tasks_list():
    # return tasks array
    return jsonify({"ok": True, "tasks": tasks})

@app.route("/api/task_types")
def api_task_types():
    return jsonify({"ok": True, "types": task_types})

@app.route("/api/profile_me")
def api_profile_me():
    uid = request.args.get("uid") or request.args.get("user") or "guest"
    user = get_user(uid)
    return jsonify({"ok": True, "user": user})

@app.route("/api/tasks/create", methods=["POST"])
def api_tasks_create():
    data = request.get_json() or {}
    # minimal validation
    title = data.get("title") or data.get("name") or "Задание"
    unit_price = float(data.get("unit_price") or data.get("reward") or 0)
    qty = int(data.get("qty") or 1)
    owner = data.get("owner_uid") or data.get("uid") or "guest"
    url = data.get("url") or ""
    ttype = data.get("type_id") or ""
    if qty < 1 or unit_price < 0:
        return jsonify({"ok": False, "errmsg": "Неверные параметры"}), 400
    tid = gen_id("task")
    task = {
        "id": tid,
        "title": title,
        "description": data.get("description") or "",
        "unit_price": unit_price,
        "qty": qty,
        "owner_uid": owner,
        "type_id": ttype,
        "url": url,
        "completed_qty": 0,
        "created_at": int(time.time())
    }
    tasks.append(task)
    persist_all()
    socketio.emit("task_update", {"task": task})
    return jsonify({"ok": True, "task": task})

@app.route("/api/user/topup-link", methods=["POST"])
def api_topup_link():
    body = request.get_json() or {}
    uid = body.get("uid") or "guest"
    amount = int(body.get("amount") or 0)
    if amount < 100:
        return jsonify({"ok": False, "errmsg": "Минимальная сумма 100 ₽"}), 400
    manual_code = str(int(time.time()))[-6:]  # simple comment code
    topup = {
        "id": gen_id("topup"),
        "uid": uid,
        "amount": amount,
        "status": "pending",
        "manual_code": manual_code,
        "created_at": int(time.time())
    }
    # sample pay_link — in real app generate SBP/QR/pay link
    topup["pay_link"] = f"https://example-pay.test/pay?amount={amount}&comment={manual_code}"
    topup["qr_url"] = ""  # could generate a QR image
    topups.append(topup)
    persist_all()
    # Emit new_topup to connected admins
    try:
        # simply emit; frontend listens to "new_topup"
        socketio.emit("new_topup", topup)
    except Exception as e:
        app.logger.exception("socketio emit failed: %s", e)
    return jsonify({"ok": True, "topup": topup, "manual_code": manual_code, "pay_link": topup["pay_link"], "qr_url": topup["qr_url"]})

@app.route("/api/user/topup-confirm", methods=["POST"])
def api_topup_confirm():
    body = request.get_json() or {}
    uid = body.get("uid") or "guest"
    topup_id = body.get("topup_id")
    t = next((x for x in topups if x["id"] == topup_id), None)
    if not t:
        return jsonify({"ok": False, "errmsg": "Заявка не найдена"}), 404
    # In demo: mark approved and add balance
    t["status"] = "processing"
    persist_all()
    # Simulate admin verification asynchronously (demo)
    def approve_sim():
        time.sleep(2)  # simulate checking
        t["status"] = "approved"
        u = get_user(t["uid"])
        u["balance"] = u.get("balance", 0) + t["amount"]
        u.setdefault("history", []).insert(0, {"type": "topup", "amount": t["amount"], "note": f"Topup {t['id']}", "at": int(time.time())})
        persist_all()
        socketio.emit("topup_updated", t)
    threading.Thread(target=approve_sim, daemon=True).start()
    return jsonify({"ok": True, "msg": "Заявка на проверку"})

@app.route("/api/user/withdraw", methods=["POST"])
def api_withdraw():
    body = request.get_json() or {}
    uid = body.get("uid") or "guest"
    amount = int(body.get("amount") or 0)
    name = body.get("name") or ""
    details = body.get("details") or ""
    if amount < 300:
        return jsonify({"ok": False, "errmsg": "Мин. 300 ₽"}), 400
    u = get_user(uid)
    if amount > u.get("balance", 0):
        return jsonify({"ok": False, "errmsg": "Недостаточно средств"}), 400
    wid = gen_id("wd")
    w = {
        "id": wid,
        "uid": uid,
        "amount": amount,
        "name": name,
        "details": details,
        "status": "pending",
        "created_at": int(time.time())
    }
    withdraws.append(w)
    persist_all()
    socketio.emit("withdraw_new", w)
    return jsonify({"ok": True, "withdraw": w})

# Admin endpoints (simple)
@app.route("/api/admin/topups")
def api_admin_topups():
    # token simple auth (token query param)
    token = request.args.get("token") or request.headers.get("Authorization", "").replace("Bearer ", "")
    # NOTE: demo allows any token
    return jsonify({"ok": True, "items": topups})

@app.route("/api/admin/topups/<tid>/approve", methods=["POST"])
def api_admin_topup_approve(tid):
    t = next((x for x in topups if x["id"] == tid), None)
    if not t:
        return jsonify({"ok": False}), 404
    if t["status"] in ("approved",):
        return jsonify({"ok": False, "errmsg": "Уже подтверждено"})
    t["status"] = "approved"
    u = get_user(t["uid"])
    u["balance"] = u.get("balance", 0) + t["amount"]
    u.setdefault("history", []).insert(0, {"type": "topup", "amount": t["amount"], "note": f"Topup {t['id']}", "at": int(time.time())})
    persist_all()
    socketio.emit("topup_updated", t)
    return jsonify({"ok": True})

@app.route("/api/admin/topups/<tid>/reject", methods=["POST"])
def api_admin_topup_reject(tid):
    t = next((x for x in topups if x["id"] == tid), None)
    if not t:
        return jsonify({"ok": False}), 404
    t["status"] = "rejected"
    persist_all()
    socketio.emit("topup_updated", t)
    return jsonify({"ok": True})

@app.route("/api/admin/withdraws")
def api_admin_withdraws():
    return jsonify({"ok": True, "items": withdraws})

@app.route("/api/admin/withdraws/<wid>/approve", methods=["POST"])
def api_admin_withdraws_approve(wid):
    w = next((x for x in withdraws if x["id"] == wid), None)
    if not w:
        return jsonify({"ok": False}), 404
    if w["status"] != "pending":
        return jsonify({"ok": False, "errmsg": "Неверный статус"}), 400
    # subtract balance and mark done
    u = get_user(w["uid"])
    if u.get("balance", 0) < w["amount"]:
        return jsonify({"ok": False, "errmsg": "Недостаточно средств"}), 400
    u["balance"] -= w["amount"]
    w["status"] = "paid"
    u.setdefault("history", []).insert(0, {"type": "withdraw", "amount": -w["amount"], "note": f"Withdraw {w['id']}", "at": int(time.time())})
    persist_all()
    socketio.emit("withdraw_updated", w)
    return jsonify({"ok": True})

@app.route("/api/admin/withdraws/<wid>/reject", methods=["POST"])
def api_admin_withdraws_reject(wid):
    w = next((x for x in withdraws if x["id"] == wid), None)
    if not w:
        return jsonify({"ok": False}), 404
    w["status"] = "rejected"
    persist_all()
    socketio.emit("withdraw_updated", w)
    return jsonify({"ok": True})

### Basic index and admin static pages (serve local files)
@app.route("/")
def index_html():
    return send_from_directory(APP_DIR, "index.html")

@app.route("/mainadmin.html")
def mainadmin_html():
    return send_from_directory(APP_DIR, "mainadmin.html")

@app.route("/moderator.html")
def moderator_html():
    return send_from_directory(APP_DIR, "moderator.html")

### SocketIO events
@socketio.on("connect")
def on_connect():
    app.logger.info("Socket connected: %s", request.sid)

@socketio.on("disconnect")
def on_disconnect():
    app.logger.info("Socket disconnected: %s", request.sid)

if __name__ == "__main__":
    # ensure persistence files exist
    persist_all()
    # Run dev server
    socketio.run(app, host="0.0.0.0", port=8080, debug=False)
