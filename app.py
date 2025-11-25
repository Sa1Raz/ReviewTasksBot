#!/usr/bin/env python3
# coding: utf-8

import os
import time
import json
import random
import string
import logging
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, send_from_directory, abort
from flask_socketio import SocketIO

try:
    import telebot
    from telebot import types as tb_types
    import requests
except Exception:
    telebot = None
try:
    import jwt
except Exception:
    jwt = None

DEFAULT_BOT_TOKEN = "8033069276:AAFv1-kdQ68LjvLEgLHj3ZXd5ehMqyUXOYU"
BOT_TOKEN = os.environ.get("BOT_TOKEN", DEFAULT_BOT_TOKEN).strip()
WEBAPP_URL = os.environ.get("WEBAPP_URL", f"https://{os.environ.get('RAILWAY_PUBLIC_DOMAIN', 'your-app.up.railway.app')}").rstrip('/')
CHANNEL_ID = os.environ.get("CHANNEL_ID", "@ReviewCashNews").strip()
ADMIN_USER_IDS = [s.strip() for s in os.environ.get("ADMIN_USER_IDS", "6482440657").split(",") if s.strip()]
ADMIN_JWT_SECRET = os.environ.get("ADMIN_JWT_SECRET", "replace_with_strong_secret")
DATA_DIR = os.environ.get("DATA_DIR", ".rc_data")
os.makedirs(DATA_DIR, exist_ok=True)
TOPUPS_FILE = os.path.join(DATA_DIR, "topups.json")
USERS_FILE = os.path.join(DATA_DIR, "users.json")
TASKS_FILE = os.path.join(DATA_DIR, "tasks.json")
TASK_TYPES_FILE = os.path.join(DATA_DIR, "task_types.json")
ADMINS_FILE = os.path.join(DATA_DIR, "admins.json")
WITHDRAWS_FILE = os.path.join(DATA_DIR, "withdraws.json")
REVIEWS_FILE = os.path.join(DATA_DIR, "reviews.json")
MIN_TOPUP = int(os.environ.get("MIN_TOPUP", "150"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("reviewcash")

# --- Helpers ---
def load_json(path, default):
    try:
        if not os.path.exists(path):
            return default
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning("load_json(%s) failed: %s", path, e)
        return default
def save_json(path, obj):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception("save_json(%s) failed: %s", path, e)
def append_json(path, obj):
    arr = load_json(path, [])
    arr.insert(0, obj)
    save_json(path, arr)
def gen_id(prefix="id"):
    return f"{prefix}_{int(time.time()*1000)}_{random.randint(1000,9999)}"
def gen_manual_code():
    return "RC" + ''.join(random.choices(string.ascii_uppercase + string.digits, k=5))

# Ensure default files and types
if not os.path.exists(TASK_TYPES_FILE) or not load_json(TASK_TYPES_FILE, []):
    save_json(TASK_TYPES_FILE, [
        {"id":"ya_review","name":"Отзыв — Яндекс Карты","unit_price":85},
        {"id":"gmaps_review","name":"Отзыв — Google Maps","unit_price":50},
        {"id":"tg_sub","name":"Подписка — Telegram канал","unit_price":5},
    ])
for f, v in [
    (USERS_FILE, {}), (TOPUPS_FILE, []), (TASKS_FILE, []), (WITHDRAWS_FILE, []), (REVIEWS_FILE, [])
]:
    if not os.path.exists(f): save_json(f, v)
if not os.path.exists(ADMINS_FILE):
    save_json(ADMINS_FILE, {uid: {"name": "Admin", "tasks_reviewed": 0, "role": "mod"} for uid in ADMIN_USER_IDS})

def get_user(uid):
    users = load_json(USERS_FILE, {})
    key = str(uid)
    if key not in users:
        users[key] = {"balance": 0.0, "history": [], "tasks_done": 0, "total_earned": 0.0}
        save_json(USERS_FILE, users)
    return users[key]
def update_user_balance(uid, amount, history_item):
    users = load_json(USERS_FILE, {})
    key = str(uid)
    if key not in users:
        users[key] = {"balance": 0.0, "history": [], "tasks_done": 0}
    users[key]['balance'] = round(users[key].get('balance', 0.0) + float(amount), 2)
    if history_item:
        history_item['created_at'] = datetime.utcnow().isoformat()+"Z"
        users[key].setdefault('history', []).insert(0, history_item)
        users[key]['history'] = users[key]['history'][:50]
    save_json(USERS_FILE, users)
    socketio.emit("user_update", {"user_id": key, "balance": users[key]['balance']}, broadcast=True)
    return users[key]
def is_user_subscribed(telegram_id):
    try:
        api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/getChatMember"
        channel = CHANNEL_ID
        params = {"chat_id": channel, "user_id": telegram_id}
        resp = requests.get(api_url, params=params, timeout=3).json()
        if resp.get("ok") and resp["result"]["status"] in ("member","administrator","creator"):
            return True
        return False
    except Exception as e:
        return False
def get_unit_price_for_type(task_type):
    all_types = load_json(TASK_TYPES_FILE, [])
    for t in all_types:
        if t["id"] == task_type:
            return float(t.get("unit_price", 0))
    return 0.0

# --- Push-уведомление модераторам ---
def notify_moderators_new_review(review):
    try:
        if not telebot:
            return
        admins_db = load_json(ADMINS_FILE, {})
        notify_ids = set(str(uid) for uid, v in admins_db.items() if v.get("role") == "mod")
        notify_ids.update(ADMIN_USER_IDS)
        msg = (
            f"🆕 Новая заявка на модерацию!\n"
            f"Тип: {review.get('task_type','-')}\n"
            f"User ID: {review.get('user_id','-')}\n"
            f"Заголовок: {review.get('title','')}\n"
        )
        if review.get("review_url"):
            msg += f"Ссылка: {review['review_url']}\n"
        if review.get("site_name"):
            msg += f"Площадка: {review['site_name']}\n"
        kb = tb_types.InlineKeyboardMarkup()
        kb.add(tb_types.InlineKeyboardButton("Открыть модерацию", web_app=tb_types.WebAppInfo(url=WEBAPP_URL + "/moderator.html")))
        for uid in notify_ids:
            try:
                bot.send_message(uid, msg, reply_markup=kb, disable_web_page_preview=True)
            except Exception as e:
                logger.warning(f"telegram notify fail {uid}: {e}")
    except Exception as e:
        logger.warning(f"notify_moderators_new_review error: {e}")

# ---------- App ----------
app = Flask(__name__, static_folder='public', static_url_path='/')
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# ---------- Telegram bot ----------
if telebot:
    bot = telebot.TeleBot(BOT_TOKEN)
    logger.info("Telebot configured")

    @bot.message_handler(commands=['start'])
    def _start(m):
        uid = m.from_user.id
        username = m.from_user.username or ""
        txt = (
            "<b>⚡️ Добро пожаловать в <a href='https://t.me/ReviewCashNews'>ReviewCash</a>!</b>\n\n"
            "<b>💸 Здесь ты можешь зарабатывать на отзывах и заданиях!</b>\n\n"
            "▶️ <b>Подпишись на наш канал</b> чтобы пользоваться платформой:\n"
            f"<a href='https://t.me/{CHANNEL_ID.lstrip('@')}'>Перейти в канал</a>\n\n"
            "❗️ После подписки нажми кнопку ниже 👇"
        )
        kb = tb_types.InlineKeyboardMarkup()
        kb.add(tb_types.InlineKeyboardButton("✅ Я подписался!", callback_data="checksub"))
        bot.send_message(uid, txt, reply_markup=kb, parse_mode='HTML', disable_web_page_preview=True)
        users = load_json(USERS_FILE, {})
        users[str(uid)] = users.get(str(uid), {"balance":0,"first_name":m.from_user.first_name,"username":username})
        save_json(USERS_FILE, users)

    @bot.callback_query_handler(func=lambda cq: cq.data == "checksub")
    def check_sub(cq):
        uid = cq.from_user.id
        if is_user_subscribed(uid):
            bot.answer_callback_query(cq.id, "Отлично! Переходим в приложение", show_alert=True)
            kb = tb_types.InlineKeyboardMarkup()
            kb.add(tb_types.InlineKeyboardButton("🚀 Открыть ReviewCash", web_app=tb_types.WebAppInfo(url=WEBAPP_URL+"/index.html")))
            bot.send_message(uid, "✅ Всё, добро пожаловать!\nЖми «Открыть ReviewCash» 👇", reply_markup=kb)
        else:
            bot.answer_callback_query(cq.id, "Вы не подписаны на канал...", show_alert=True)
            kb = tb_types.InlineKeyboardMarkup()
            kb.add(tb_types.InlineKeyboardButton("Перейти в канал", url=f"https://t.me/{CHANNEL_ID.lstrip('@')}"))
            bot.send_message(uid, "❗️ Сначала подпишись на канал!", reply_markup=kb)

    @bot.message_handler(commands=['mainadmin'])
    def _mainadmin(m):
        uid = str(m.from_user.id)
        if uid not in ADMIN_USER_IDS:
            bot.send_message(m.chat.id, "⛔ Нет прав супер-админа.")
            return
        if jwt:
            payload = {"uid": uid, "role": "super", "exp": datetime.utcnow() + timedelta(days=7)}
            token = jwt.encode(payload, ADMIN_JWT_SECRET, algorithm="HS256")
            if isinstance(token, bytes): token = token.decode('utf-8')
            kb = tb_types.InlineKeyboardMarkup()
            admin_url = f"{WEBAPP_URL}/mainadmin.html?token={token}"
            kb.add(tb_types.InlineKeyboardButton("👑 Открыть Админ-панель", web_app=tb_types.WebAppInfo(url=admin_url)))
            bot.send_message(m.chat.id, "Панель супер-админа:", reply_markup=kb)

    @bot.message_handler(commands=['admin', 'mod', 'moderator'])
    def _moderator(m):
        uid = str(m.from_user.id)
        admins_db = load_json(ADMINS_FILE, {})
        if uid not in ADMIN_USER_IDS and uid not in admins_db:
            bot.send_message(m.chat.id, "⛔ Нет доступа.")
            return
        if jwt:
            payload = {"uid": uid, "role": "mod", "exp": datetime.utcnow() + timedelta(days=7)}
            token = jwt.encode(payload, ADMIN_JWT_SECRET, algorithm="HS256")
            if isinstance(token, bytes): token = token.decode('utf-8')
            kb = tb_types.InlineKeyboardMarkup()
            mod_url = f"{WEBAPP_URL}/moderator.html?token={token}"
            kb.add(tb_types.InlineKeyboardButton("🛡️ Открыть Модераторку", web_app=tb_types.WebAppInfo(url=mod_url)))
            bot.send_message(m.chat.id, "Запускаю приложение модератора:", reply_markup=kb)

# ---------- API для задач (автоматическая проверка tg_sub) ----------
@app.route('/api/reviews/submit', methods=["POST"])
def submit_review():
    data = request.json
    user_id = str(data.get('user_id'))
    task_type = data.get('task_type')
    title = data.get('title', 'Задание')
    site_name = data.get('site_name')
    review_url = data.get('review_url')
    proof_type = data.get('proof_type')
    proof_data = data.get('proof_data')
    review = {
        "id": gen_id("rev"),
        "user_id": user_id,
        "title": title,
        "task_type": task_type,
        "site_name": site_name,
        "review_url": review_url,
        "proof_type": proof_type,
        "proof_data": proof_data,
        "created_at": datetime.utcnow().isoformat()+"Z",
        "status": "pending"
    }
    if task_type == "tg_sub":
        if is_user_subscribed(user_id):
            reward = get_unit_price_for_type("tg_sub") or 5
            review['status'] = "approved"
            review['reward'] = reward
            update_user_balance(user_id, reward, {"type": "tg_sub", "note": "autopaid", "amount": reward})
        else:
            review['status'] = "rejected"
            review['reject_reason'] = "Вы не подписаны на канал"
        append_json(REVIEWS_FILE, review)
        socketio.emit("user_update", {"user_id": user_id}, broadcast=True)
        return jsonify({"ok": review['status'] == "approved", "status": review['status'], "review": review})
    else:
        append_json(REVIEWS_FILE, review)
        try:
            notify_moderators_new_review(review)
        except Exception as ex:
            logger.warning(f"Can't notify moderators: {ex}")
        return jsonify({"ok": True, "status": "pending", "review": review})

@app.route('/api/moderator/queue')
def mod_queue():
    items = load_json(REVIEWS_FILE, [])
    for item in items:
        if item.get("status") == "pending" and item.get("task_type") != "tg_sub":
            return jsonify({"ok": True, "assignment": {
                "id": item["id"],
                "user_id": item["user_id"],
                "task_title": item.get("title", "Отзыв"),
                "task_type": item.get("task_type", ""),
                "review_target": item.get("review_url"),
                "site_name": item.get("site_name", ""),
                "proof_type": item.get("proof_type"),
                "proof_data": item.get("proof_data"),
            }, "queue_length": len([x for x in items if x.get("status")=="pending" and x.get("task_type")!="tg_sub"])})
    return jsonify({"ok":True, "assignment":None, "queue_length":0})

@app.route('/api/moderator/me')
def mod_me():
    uid = request.args.get("uid","mod")
    data = load_json(ADMINS_FILE,{})
    name = data.get(uid,{}).get("name","Модератор")
    tasks_reviewed = data.get(uid,{}).get("tasks_reviewed",0)
    return jsonify({"ok":True, "name":name, "tasks_reviewed":tasks_reviewed})

@app.route('/api/moderator/approve', methods=["POST"])
def mod_approve():
    data = request.json
    rid = data.get("id")
    reviews = load_json(REVIEWS_FILE, [])
    for r in reviews:
        if r["id"] == rid and r["status"]=="pending":
            r["status"] = "approved"
            reward = None
            if "reward" in r:
                try: reward = float(r["reward"])
                except: reward = None
            if reward is None: reward = get_unit_price_for_type(r.get("task_type","").strip())
            if not reward: reward = 10
            save_json(REVIEWS_FILE, reviews)
            update_user_balance(r["user_id"], reward, {"type":"review","note":"approve","amount":reward,"task_type":r.get("task_type","")})
            socketio.emit("user_update", {"user_id": r["user_id"]}, broadcast=True)
            return jsonify({"ok":True})
    return jsonify({"ok":False})

@app.route('/api/moderator/reject', methods=["POST"])
def mod_reject():
    data = request.json
    rid = data.get("id")
    reason = data.get("reason","Не выполнено условие")
    reviews = load_json(REVIEWS_FILE, [])
    for r in reviews:
        if r["id"] == rid and r["status"]=="pending":
            r["status"] = "rejected"
            r["reject_reason"] = reason
            save_json(REVIEWS_FILE, reviews)
            socketio.emit("user_update", {"user_id": r["user_id"]}, broadcast=True)
            return jsonify({"ok":True})
    return jsonify({"ok":False})

@app.route('/api/profile_me', methods=['GET'])
def profile_me():
    uid = request.args.get('uid')
    if not uid: return jsonify({"ok": False})
    u = get_user(uid)
    return jsonify({"ok": True, "user": u})

@app.route('/api/user/topup', methods=["POST"])
def api_user_topup():
    data = request.json
    uid, amount = data.get("uid"), float(data.get("amount"))
    top = {
        "id": gen_id("topup"),
        "user": {"id": uid},
        "amount": amount,
        "status": "pending",
        "manual_code": data.get("manual_code"),
        "created_at": datetime.utcnow().isoformat()+"Z"
    }
    append_json(TOPUPS_FILE, top)
    socketio.emit("new_topup", top, broadcast=True)
    return jsonify({"ok": True, "topup": top})

@app.route('/api/user/withdraw', methods=["POST"])
def api_user_withdraw():
    data = request.json
    uid = str(data.get("uid"))
    amount = float(data.get("amount", 0))
    name = data.get("name")
    details = data.get("details")
    if amount < 300: return jsonify({"ok": False, "errmsg": "Мин. 300"})
    u = get_user(uid)
    if u["balance"] < amount: return jsonify({"ok": False, "errmsg": "Нет средств"})
    update_user_balance(uid, -amount, {"type": "withdraw_req", "amount": amount})
    wd = {
        "id": gen_id("wd"),
        "user": {"id": uid},
        "amount": amount,
        "name": name,
        "details": details,
        "status": "pending",
        "created_at": datetime.utcnow().isoformat()+"Z"
    }
    append_json(WITHDRAWS_FILE, wd)
    socketio.emit("new_withdraw", wd, broadcast=True)
    return jsonify({"ok": True, "withdraw": wd})

@app.route('/')
def index(): return send_from_directory('public', 'index.html')
@app.route('/<path:path>')
def static_proxy(path): return send_from_directory('public', path)

if __name__ == '__main__':
    if telebot:
        import threading
        threading.Thread(target=bot.infinity_polling, daemon=True).start()
    port = int(os.environ.get("PORT", "8080"))
    logger.info(f"Starting server on port {port}")
    socketio.run(app, host='0.0.0.0', port=port, allow_unsafe_werkzeug=True, use_reloader=False)
