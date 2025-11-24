#!/usr/bin/env python3
# coding: utf-8
"""
ReviewCash — app.py for the provided WebApp frontend
"""
import os
import time
import json
import hmac
import hashlib
import random
import string
import logging
from datetime import datetime, timedelta
from urllib.parse import quote_plus, urlparse, parse_qsl
from flask import Flask, request, jsonify, send_from_directory, abort
from flask_socketio import SocketIO
try:
    import telebot
    from telebot import types as tb_types
    from telebot.apihelper import ApiTelegramException
except Exception:
    telebot = None
try:
    import jwt
except Exception:
    jwt = None
# ---------- Config ----------
DEFAULT_BOT_TOKEN = "8033069276:AAFv1-kdQ68LjvLEgLHj3ZXd5ehMqyUXOYU"
BOT_TOKEN = os.environ.get("BOT_TOKEN", DEFAULT_BOT_TOKEN).strip()
# Автоматически определяет правильный URL для Railway
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
MIN_TOPUP = int(os.environ.get("MIN_TOPUP", "150"))
# ---------- Logging ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("reviewcash")
# ---------- Helpers ----------
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
    arr.insert(0, obj) # Newest first
    save_json(path, arr)
def gen_id(prefix="id"):
    return f"{prefix}_{int(time.time()*1000)}_{random.randint(1000,9999)}"
def gen_manual_code():
    return "RC" + ''.join(random.choices(string.ascii_uppercase + string.digits, k=5))
# Ensure default task types
if not os.path.exists(TASK_TYPES_FILE) or not load_json(TASK_TYPES_FILE, []):
    save_json(TASK_TYPES_FILE, [
        {"id":"ya_review","name":"Отзыв — Я.К.","unit_price":100},
        {"id":"gmaps_review","name":"Отзыв — Google Maps","unit_price":65},
        {"id":"tg_sub","name":"Подписка — Telegram канал","unit_price":10},
    ])
# Ensure files exist
for f in [USERS_FILE, TOPUPS_FILE, TASKS_FILE, WITHDRAWS_FILE]:
    if not os.path.exists(f):
        save_json(f, [] if f != USERS_FILE else {})
if not os.path.exists(ADMINS_FILE):
    save_json(ADMINS_FILE, {uid: {"name": "Admin", "tasks_reviewed": 0} for uid in ADMIN_USER_IDS})
# ---------- App ----------
app = Flask(__name__, static_folder='public', static_url_path='/')
# Allow unsafe werkzeug is needed for Railway sometimes with socketio
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')
# ---------- Utility for users ----------
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
    
    # Update balance
    users[key]['balance'] = round(users[key].get('balance', 0.0) + float(amount), 2)
    
    # Add history
    if history_item:
        history_item['created_at'] = datetime.utcnow().isoformat()+"Z"
        users[key].setdefault('history', []).insert(0, history_item)
        # Limit history length
        users[key]['history'] = users[key]['history'][:50]
    
    save_json(USERS_FILE, users)
    return users[key]
# ---------- Telegram bot ----------
if BOT_TOKEN and telebot:
    bot = telebot.TeleBot(BOT_TOKEN)
    logger.info("Telebot configured")
    @bot.message_handler(commands=['start'])
    def _start(m):
        uid = m.from_user.id
        text = (
            "👋 Привет! Добро пожаловать в ReviewCash.\n\n"
            "Здесь можно зарабатывать на заданиях и продвигать свои проекты.\n"
            "Нажмите кнопку ниже, чтобы открыть приложение 👇"
        )
        kb = tb_types.InlineKeyboardMarkup()
        if WEBAPP_URL:
            kb.add(tb_types.InlineKeyboardButton("🚀 Открыть WebApp", web_app=tb_types.WebAppInfo(url=WEBAPP_URL + "/user.html")))
        try:
            bot.send_message(uid, text, reply_markup=kb)
        except Exception as e:
            logger.error(f"Start error: {e}")
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
            
            # Отправляем WebApp кнопку
            kb = tb_types.InlineKeyboardMarkup()
            admin_url = f"{WEBAPP_URL}/admin.html?token={token}"
            kb.add(tb_types.InlineKeyboardButton("👑 Открыть Админ-панель", web_app=tb_types.WebAppInfo(url=admin_url)))
            bot.send_message(m.chat.id, "Панель супер-админа:", reply_markup=kb)
    @bot.message_handler(commands=['mod'])
    def _mod(m):
        uid = str(m.from_user.id)
        admins_db = load_json(ADMINS_FILE, {})
        
        if uid not in ADMIN_USER_IDS and uid not in admins_db:
            bot.send_message(m.chat.id, "⛔ Вы не являетесь модератором.")
            return
            
        if jwt:
            payload = {"uid": uid, "role": "mod", "exp": datetime.utcnow() + timedelta(days=7)}
            token = jwt.encode(payload, ADMIN_JWT_SECRET, algorithm="HS256")
            if isinstance(token, bytes): token = token.decode('utf-8')
            
            kb = tb_types.InlineKeyboardMarkup()
            mod_url = f"{WEBAPP_URL}/admin.html?token={token}"
            kb.add(tb_types.InlineKeyboardButton("🛡️ Панель Модератора", web_app=tb_types.WebAppInfo(url=mod_url)))
            bot.send_message(m.chat.id, "Панель модератора:", reply_markup=kb)
    @bot.message_handler(commands=['addadmin'])
    def _addadmin(m):
        uid = str(m.from_user.id)
        if uid not in ADMIN_USER_IDS:
            return
        try:
            parts = m.text.split()
            if len(parts) < 3:
                bot.reply_to(m, "Использование: /addadmin ID ИМЯ")
                return
            new_id = parts[1]
            name = " ".join(parts[2:])
            
            admins_db = load_json(ADMINS_FILE, {})
            admins_db[new_id] = {"name": name, "tasks_reviewed": 0, "role": "mod"}
            save_json(ADMINS_FILE, admins_db)
            bot.reply_to(m, f"✅ Админ {name} ({new_id}) добавлен!")
        except Exception as e:
            bot.reply_to(m, f"Ошибка: {e}")
            
    @bot.message_handler(commands=['balance'])
    def _balance(m):
        uid = str(m.from_user.id)
        users = load_json(USERS_FILE, {})
        bal = users.get(uid, {}).get('balance', 0)
        
        kb = tb_types.InlineKeyboardMarkup()
        kb.add(tb_types.InlineKeyboardButton("💰 Пополнить", web_app=tb_types.WebAppInfo(url=WEBAPP_URL + "/user.html")))
        
        bot.send_message(m.chat.id, f"💰 Ваш баланс: {bal} ₽", reply_markup=kb)
# ---------- API Endpoints ----------
# --- Auth Middleware ---
def get_token(req):
    t = req.args.get("token")
    if not t:
        h = req.headers.get("Authorization", "")
        if h.startswith("Bearer "): t = h.split(" ")[1]
    return t
def require_admin(role="mod"):
    def decorator(f):
        def wrapper(*args, **kwargs):
            token = get_token(request)
            if not token or not jwt: return abort(401)
            try:
                data = jwt.decode(token, ADMIN_JWT_SECRET, algorithms=["HS256"])
                if role == "super" and data.get("role") != "super":
                    return abort(403)
                return f(*args, **kwargs)
            except:
                return abort(403)
        wrapper.__name__ = f.__name__
        return wrapper
    return decorator
# --- Public API ---
@app.route('/')
def index(): return send_from_directory('public', 'index.html')
@app.route('/<path:path>')
def static_proxy(path): return send_from_directory('public', path)
@app.route('/api/profile_me', methods=['GET'])
def profile_me():
    # Simplified for demo - in prod verify init_data
    uid = request.args.get('uid')
    if not uid: return jsonify({"ok": False})
    u = get_user(uid)
    return jsonify({"ok": True, "user": u})
@app.route('/api/task_types', methods=['GET'])
def get_task_types():
    return jsonify(load_json(TASK_TYPES_FILE, []))
@app.route('/api/tasks_public', methods=['GET'])
def get_tasks_public():
    tasks = load_json(TASKS_FILE, [])
    active = [t for t in tasks if t.get('status') == 'active']
    return jsonify(active)
# --- Admin API ---
@app.route('/api/admin/dashboard', methods=['GET'])
@require_admin("mod")
def admin_dashboard():
    users = load_json(USERS_FILE, {})
    tasks = load_json(TASKS_FILE, [])
    topups = load_json(TOPUPS_FILE, [])
    withdraws = load_json(WITHDRAWS_FILE, [])
    
    rev = sum(t['amount'] for t in topups if t['status'] == 'paid')
    pending = len([t for t in topups if t['status'] == 'pending']) + len([w for w in withdraws if w['status'] == 'pending'])
    
    # Recent activity
    activity = []
    for t in topups[:5]:
        activity.append({"type": "Пополнение", "id": t['id'], "amount": t['amount'], "status": t['status'], "user": t.get('user', {}).get('id'), "created_at": t['created_at']})
    for w in withdraws[:5]:
        activity.append({"type": "Вывод", "id": w['id'], "amount": w['amount'], "status": w['status'], "user": w.get('user', {}).get('id'), "created_at": w['created_at']})
    
    activity.sort(key=lambda x: x['created_at'], reverse=True)
    
    return jsonify({
        "ok": True, 
        "data": {
            "usersCount": len(users),
            "totalRevenue": rev,
            "tasksCount": len(tasks),
            "pendingCount": pending,
            "recentActivity": activity[:10]
        }
    })
@app.route('/api/admin/incoming_topups', methods=['GET'])
@require_admin("mod")
def admin_topups():
    status = request.args.get('status')
    comment = request.args.get('comment', '').lower()
    
    items = load_json(TOPUPS_FILE, [])
    res = []
    for i in items:
        if status and i['status'] != status: continue
        if comment and comment not in str(i).lower(): continue
        res.append(i)
    return jsonify({"ok": True, "items": res})
@app.route('/api/admin/topups/<tid>/approve', methods=['POST'])
@require_admin("mod") # Allow mods to approve for smoother operations, or change to super
def approve_topup(tid):
    items = load_json(TOPUPS_FILE, [])
    found = None
    for i in items:
        if i['id'] == tid:
            found = i
            break
    
    if not found or found['status'] != 'pending':
        return jsonify({"ok": False, "reason": "invalid"})
    
    found['status'] = 'paid'
    found['paid_at'] = datetime.utcnow().isoformat()+"Z"
    save_json(TOPUPS_FILE, items)
    
    # Credit user
    uid = found.get('user', {}).get('id')
    if uid:
        update_user_balance(uid, found['amount'], {
            "type": "topup",
            "amount": found['amount'],
            "note": "admin_approved"
        })
        
    return jsonify({"ok": True})
@app.route('/api/admin/topups/<tid>/reject', methods=['POST'])
@require_admin("mod")
def reject_topup(tid):
    items = load_json(TOPUPS_FILE, [])
    found = None
    for i in items:
        if i['id'] == tid:
            found = i
            break
    
    if not found: return jsonify({"ok": False})
    
    found['status'] = 'refunded'
    save_json(TOPUPS_FILE, items)
    return jsonify({"ok": True})
@app.route('/api/admin/withdraws', methods=['GET'])
@require_admin("mod")
def admin_withdraws():
    status = request.args.get('status')
    items = load_json(WITHDRAWS_FILE, [])
    if status:
        items = [i for i in items if i['status'] == status]
    return jsonify({"ok": True, "items": items})
@app.route('/api/admin/withdraws/<wid>/approve', methods=['POST'])
@require_admin("super")
def approve_withdraw(wid):
    items = load_json(WITHDRAWS_FILE, [])
    found = next((i for i in items if i['id'] == wid), None)
    if not found or found['status'] != 'pending':
        return jsonify({"ok": False})
        
    found['status'] = 'approved'
    found['processed_at'] = datetime.utcnow().isoformat()+"Z"
    save_json(WITHDRAWS_FILE, items)
    # Money already deducted on request
    return jsonify({"ok": True})
@app.route('/api/admin/withdraws/<wid>/reject', methods=['POST'])
@require_admin("super")
def reject_withdraw(wid):
    items = load_json(WITHDRAWS_FILE, [])
    found = next((i for i in items if i['id'] == wid), None)
    if not found or found['status'] != 'pending':
        return jsonify({"ok": False})
        
    found['status'] = 'rejected'
    save_json(WITHDRAWS_FILE, items)
    
    # Refund user
    uid = found.get('user', {}).get('id')
    if uid:
        update_user_balance(uid, found['amount'], {
            "type": "refund",
            "amount": found['amount'],
            "note": "withdraw_rejected"
        })
        
    return jsonify({"ok": True})
@app.route('/api/admin/stats', methods=['GET'])
@require_admin("mod")
def admin_stats_api():
    admins = load_json(ADMINS_FILE, {})
    return jsonify({"ok": True, "admins": [{"id": k, **v} for k,v in admins.items()]})
@app.route('/api/admin/users', methods=['GET'])
@require_admin("mod")
def admin_users():
    users = load_json(USERS_FILE, {})
    return jsonify({"ok": True, "users": [{"id": k, **v} for k,v in users.items()]})
@app.route('/api/admin/tasks', methods=['GET'])
@require_admin("mod")
def admin_tasks():
    tasks = load_json(TASKS_FILE, [])
    return jsonify({"ok": True, "tasks": tasks})
@app.route('/api/task_types_add', methods=['POST'])
@require_admin("super")
def add_task_type():
    data = request.json
    types = load_json(TASK_TYPES_FILE, [])
    types.append(data)
    save_json(TASK_TYPES_FILE, types)
    return jsonify({"ok": True})
if __name__ == '__main__':
    # Threaded polling with restart protection (fixes 409 error)
    if bot:
        try:
            bot.remove_webhook()
        except Exception:
            pass
        def _poll():
            time.sleep(1) # Delay to let old instance die
            while True:
                try:
                    logger.info("Starting bot polling...")
                    bot.infinity_polling(timeout=60, long_polling_timeout=50, restart_on_change=False)
                except Exception as ex:
                    if "409" in str(ex):
                        logger.warning("Conflict (409): Another instance running. Retrying in 10s...")
                        time.sleep(10)
                    else:
                        logger.error(f"Bot error: {ex}")
                        time.sleep(5)
        import threading
        threading.Thread(target=_poll, daemon=True).start()
    port = int(os.environ.get("PORT", "8080"))
    logger.info(f"Starting server on port {port}")
    # use_reloader=False is CRITICAL to prevent double execution
    socketio.run(app, host='0.0.0.0', port=port, allow_unsafe_werkzeug=True, use_reloader=False)
