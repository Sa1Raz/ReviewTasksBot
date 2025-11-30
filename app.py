# app.py — Full working demo server for ReviewCash (dev/demo)
# - Flask + Flask-SocketIO
# - Simple JSON file persistence (data/*.json)
# - Endpoints used by the frontend in your project

import os
import json
import uuid
import time
from datetime import datetime
from pathlib import Path
from flask import Flask, jsonify, request, send_from_directory, abort
from flask_cors import CORS
from flask_socketio import SocketIO

# ---- Config ----
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
TASKS_FILE = DATA_DIR / "tasks.json"
USERS_FILE = DATA_DIR / "users.json"
TOPUPS_FILE = DATA_DIR / "topups.json"
WITHDRAWS_FILE = DATA_DIR / "withdraws.json"
TYPES_FILE = DATA_DIR / "task_types.json"

ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "admintoken_demo_123")  # change in prod

# ---- Helpers for simple JSON storage ----
def load_json(path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        app.logger.exception("load_json failed for %s", path)
    return default

def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

# ---- Create app ----
app = Flask(__name__, static_folder="static", static_url_path="/static")
CORS(app, resources={r"/api/*": {"origins": "*"}})
socketio = SocketIO(app, cors_allowed_origins="*", logger=False, engineio_logger=False)

# ---- Initialize default data if empty ----
if not TYPES_FILE.exists():
    default_types = [
        {"id": "ya_review", "name": "Отзыв — Яндекс Карты", "unit_price": 120, "max_qty": 150},
        {"id": "gmaps_review", "name": "Отзыв — Google Maps", "unit_price": 65, "max_qty": 200},
        {"id": "tg_sub", "name": "Подписка — Telegram канал", "unit_price": 10, "max_qty": 100000}
    ]
    save_json(TYPES_FILE, default_types)

if not TASKS_FILE.exists():
    save_json(TASKS_FILE, [])

if not USERS_FILE.exists():
    # example user(s)
    demo_users = {
        "guest_1": {"id": "guest_1", "name": "Пользователь", "username": "guest", "balance": 500, "history": []}
    }
    save_json(USERS_FILE, demo_users)

if not TOPUPS_FILE.exists():
    save_json(TOPUPS_FILE, [])

if not WITHDRAWS_FILE.exists():
    save_json(WITHDRAWS_FILE, [])

# ---- Utility functions ----
def now_ts():
    return int(time.time())

def gen_id(prefix="id"):
    return f"{prefix}_{uuid.uuid4().hex[:10]}"

def get_user(uid):
    users = load_json(USERS_FILE, {})
    return users.get(str(uid))

def upsert_user(user):
    users = load_json(USERS_FILE, {})
    users[str(user["id"])] = user
    save_json(USERS_FILE, users)

# ---- Static / root routes (if you host front-end with server) ----
@app.route("/")
def index():
    # If you have a static index in /static, serve that; else simple JSON
    if (BASE_DIR / "static" / "index.html").exists():
        return send_from_directory(str(BASE_DIR / "static"), "index.html")
    return jsonify({"ok": True, "msg": "ReviewCash API running"})

# serve task_types.json for legacy frontends that try to fetch it
@app.route("/task_types.json")
def task_types_file():
    types = load_json(TYPES_FILE, [])
    return jsonify(types)

# ---- Public API ----

@app.route("/api/task_types")
def api_task_types():
    types = load_json(TYPES_FILE, [])
    return jsonify({"ok": True, "types": types})

@app.route("/api/tasks/list")
def api_tasks_list():
    tasks = load_json(TASKS_FILE, [])
    # Allow optional filters
    owner = request.args.get("owner")
    if owner:
        tasks = [t for t in tasks if str(t.get("owner_uid")) == str(owner)]
    return jsonify({"ok": True, "tasks": tasks})

@app.route("/api/tasks/create", methods=["POST"])
def api_tasks_create():
    try:
        data = request.get_json() or {}
        title = data.get("title") or data.get("name") or "Задание"
        owner_uid = data.get("owner_uid") or data.get("uid") or "guest_1"
        unit_price = float(data.get("unit_price") or data.get("reward") or 0)
        qty = int(data.get("qty") or 1)
        url = data.get("url") or ""
        description = data.get("description") or data.get("desc") or ""
        t = {
            "id": gen_id("task"),
            "title": title,
            "description": description,
            "owner_uid": str(owner_uid),
            "unit_price": unit_price,
            "qty": qty,
            "url": url,
            "created_at": now_ts(),
            "completed_qty": 0,
            "status": "open"
        }
        tasks = load_json(TASKS_FILE, [])
        tasks.insert(0, t)
        save_json(TASKS_FILE, tasks)

        # notify clients
        try:
            socketio.emit("task_update", {"task": t})
        except Exception:
            app.logger.exception("socket emit failed")

        return jsonify({"ok": True, "task": t})
    except Exception as e:
        app.logger.exception("create task error")
        return jsonify({"ok": False, "errmsg": "Ошибка создания задания"}), 500

@app.route("/api/profile_me")
def api_profile_me():
    uid = request.args.get("uid") or "guest_1"
    user = get_user(uid)
    if not user:
        # create minimal user
        user = {"id": str(uid), "name": "Пользователь", "username": "", "balance": 0, "history": []}
        upsert_user(user)
    return jsonify({"ok": True, "user": user})

# ---- Topup flow (demo) ----
@app.route("/api/user/topup-link", methods=["POST"])
def api_topup_link():
    try:
        body = request.get_json() or {}
        uid = body.get("uid") or "guest_1"
        amount = float(body.get("amount") or 0)
        if amount <= 0:
            return jsonify({"ok": False, "errmsg": "Неверная сумма"}), 400

        topups = load_json(TOPUPS_FILE, [])
        # create topup record
        topup_id = gen_id("tp")
        manual_code = f"RC{int(time.time())%100000}"
        pay_link = f"https://pay.example.local/pay/{topup_id}"  # demo link
        topup = {
            "id": topup_id,
            "uid": str(uid),
            "amount": amount,
            "manual_code": manual_code,
            "status": "pending",
            "created_at": now_ts(),
            "pay_link": pay_link,
            # qr_base64 left empty (frontend may open pay_link)
            "qr_base64": "",
        }
        topups.insert(0, topup)
        save_json(TOPUPS_FILE, topups)

        # notify admins/clients about new topup (emit without broadcast arg)
        try:
            socketio.emit("new_topup", {"topup": topup})
        except Exception:
            app.logger.exception("socket emit new_topup failed")

        return jsonify({"ok": True, "topup": topup, "manual_code": manual_code, "pay_link": pay_link})
    except Exception as e:
        app.logger.exception("topup-link error")
        return jsonify({"ok": False, "errmsg": "Ошибка создания ссылки"}), 500

@app.route("/api/user/topup-confirm", methods=["POST"])
def api_topup_confirm():
    try:
        body = request.get_json() or {}
        topup_id = body.get("topup_id")
        uid = body.get("uid")
        if not topup_id:
            return jsonify({"ok": False, "errmsg": "topup_id required"}), 400
        topups = load_json(TOPUPS_FILE, [])
        for t in topups:
            if t["id"] == topup_id:
                # Demo: mark as confirmed and credit user
                t["status"] = "processing"
                t["confirmed_at"] = now_ts()
                save_json(TOPUPS_FILE, topups)

                # credit (demo immediate)
                user = get_user(uid) or {"id": str(uid), "name": "Пользователь", "balance": 0, "history": []}
                user["balance"] = float(user.get("balance", 0)) + float(t["amount"])
                hist_entry = {"type": "topup", "amount": t["amount"], "note": f"Пополнение {t['id']}", "ts": now_ts()}
                user.setdefault("history", []).insert(0, hist_entry)
                upsert_user(user)

                # notify
                socketio.emit("user_update", {"user_id": user["id"], "balance": user["balance"]})
                socketio.emit("topup_update", {"topup": t})

                return jsonify({"ok": True, "msg": "Подтверждение принято"})
        return jsonify({"ok": False, "errmsg": "Заявка не найдена"}), 404
    except Exception:
        app.logger.exception("topup-confirm error")
        return jsonify({"ok": False, "errmsg": "Ошибка проверки"}), 500

# ---- Withdraw flow ----
@app.route("/api/user/withdraw", methods=["POST"])
def api_user_withdraw():
    try:
        body = request.get_json() or {}
        uid = body.get("uid")
        amount = float(body.get("amount") or 0)
        name = body.get("name") or ""
        details = body.get("details") or ""
        if not uid or amount <= 0 or not name or not details:
            return jsonify({"ok": False, "errmsg": "Заполните все поля"}), 400
        user = get_user(uid)
        if not user:
            return jsonify({"ok": False, "errmsg": "Пользователь не найден"}), 404
        if amount > float(user.get("balance", 0)):
            return jsonify({"ok": False, "errmsg": "Недостаточно средств"}), 400

        withdraws = load_json(WITHDRAWS_FILE, [])
        wid = gen_id("wd")
        rec = {"id": wid, "uid": str(uid), "amount": amount, "name": name, "details": details, "status": "pending", "created_at": now_ts()}
        withdraws.insert(0, rec)
        save_json(WITHDRAWS_FILE, withdraws)

        # optionally reserve amount
        user["balance"] = float(user.get("balance", 0)) - amount
        user.setdefault("history", []).insert(0, {"type": "withdraw_request", "amount": -amount, "note": f"Запрос вывода {wid}", "ts": now_ts()})
        upsert_user(user)
        socketio.emit("user_update", {"user_id": user["id"], "balance": user["balance"]})
        socketio.emit("withdraw_new", {"withdraw": rec})

        return jsonify({"ok": True, "withdraw": rec})
    except Exception:
        app.logger.exception("withdraw error")
        return jsonify({"ok": False, "errmsg": "Ошибка отправки заявки"}), 500

# ---- Admin endpoints (simple token-check) ----
def require_admin_token():
    token = request.args.get("token") or request.headers.get("Authorization", "").replace("Bearer ", "")
    if token != ADMIN_TOKEN:
        abort(401, description="Unauthorized")

@app.route("/api/admin/dashboard")
def api_admin_dashboard():
    try:
        require_admin_token()
        users = load_json(USERS_FILE, {})
        topups = load_json(TOPUPS_FILE, [])
        withdraws = load_json(WITHDRAWS_FILE, [])
        tasks = load_json(TASKS_FILE, [])
        total_revenue = sum(t.get("amount", 0) for t in topups if t.get("status") in ("confirmed", "processing"))
        pending_count = len([w for w in withdraws if w.get("status") == "pending"])
        return jsonify({"ok": True, "data": {
            "usersCount": len(users),
            "totalRevenue": total_revenue,
            "pendingCount": pending_count,
            "tasksCount": len(tasks)
        }})
    except Exception:
        app.logger.exception("dashboard error")
        return jsonify({"ok": False}), 500

@app.route("/api/admin/topups")
def api_admin_topups():
    require_admin_token()
    items = load_json(TOPUPS_FILE, [])
    return jsonify({"ok": True, "items": items})

@app.route("/api/admin/topups/<tid>/approve", methods=["POST"])
def api_admin_topup_approve(tid):
    require_admin_token()
    topups = load_json(TOPUPS_FILE, [])
    for t in topups:
        if t["id"] == tid:
            t["status"] = "confirmed"
            t["approved_at"] = now_ts()
            save_json(TOPUPS_FILE, topups)
            # credit user
            user = get_user(t["uid"]) or {"id": t["uid"], "balance": 0, "history": []}
            user["balance"] = float(user.get("balance", 0)) + float(t.get("amount", 0))
            user.setdefault("history", []).insert(0, {"type": "topup", "amount": t.get("amount"), "note": f"Пополнение {t['id']}", "ts": now_ts()})
            upsert_user(user)
            socketio.emit("topup_update", {"topup": t})
            socketio.emit("user_update", {"user_id": user["id"], "balance": user["balance"]})
            return jsonify({"ok": True})
    return jsonify({"ok": False, "errmsg": "Not found"}), 404

@app.route("/api/admin/topups/<tid>/reject", methods=["POST"])
def api_admin_topup_reject(tid):
    require_admin_token()
    topups = load_json(TOPUPS_FILE, [])
    for t in topups:
        if t["id"] == tid:
            t["status"] = "rejected"
            t["rejected_at"] = now_ts()
            save_json(TOPUPS_FILE, topups)
            socketio.emit("topup_update", {"topup": t})
            return jsonify({"ok": True})
    return jsonify({"ok": False, "errmsg": "Not found"}), 404

@app.route("/api/admin/withdraws")
def api_admin_withdraws():
    require_admin_token()
    items = load_json(WITHDRAWS_FILE, [])
    return jsonify({"ok": True, "items": items})

@app.route("/api/admin/withdraws/<wid>/approve", methods=["POST"])
def api_admin_withdraws_approve(wid):
    require_admin_token()
    withdraws = load_json(WITHDRAWS_FILE, [])
    for w in withdraws:
        if w["id"] == wid:
            w["status"] = "paid"
            w["paid_at"] = now_ts()
            save_json(WITHDRAWS_FILE, withdraws)
            socketio.emit("withdraw_update", {"withdraw": w})
            return jsonify({"ok": True})
    return jsonify({"ok": False, "errmsg": "not found"}), 404

@app.route("/api/admin/withdraws/<wid>/reject", methods=["POST"])
def api_admin_withdraws_reject(wid):
    require_admin_token()
    withdraws = load_json(WITHDRAWS_FILE, [])
    for w in withdraws:
        if w["id"] == wid:
            w["status"] = "rejected"
            w["rejected_at"] = now_ts()
            save_json(WITHDRAWS_FILE, withdraws)
            socketio.emit("withdraw_update", {"withdraw": w})
            return jsonify({"ok": True})
    return jsonify({"ok": False, "errmsg": "not found"}), 404

# ---- SocketIO handlers (simple) ----
@socketio.on("connect")
def on_connect():
    app.logger.debug("socket connected")

@socketio.on("disconnect")
def on_disconnect():
    app.logger.debug("socket disconnected")

# ---- Run ----
if __name__ == "__main__":
    # For dev use the built-in server
    print("Starting ReviewCash demo server on http://127.0.0.1:8080")
    socketio.run(app, host="0.0.0.0", port=8080)
