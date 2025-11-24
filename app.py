#!/usr/bin/env python3
# coding: utf-8
"""
ReviewCash — Полный backend с исправленными командами Telegram бота
Все команды теперь отправляют inline кнопки с WebApp
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
except Exception:
    telebot = None
try:
    import jwt
except Exception:
    jwt = None
try:
    import requests
except Exception:
    requests = None

# ---------- Config ----------
DEFAULT_BOT_TOKEN = "8033069276:AAFv1-kdQ68LjvLEgLHj3ZXd5ehMqyUXOYU"  # Замените на ваш токен
BOT_TOKEN = os.environ.get("BOT_TOKEN", DEFAULT_BOT_TOKEN).strip()

# Railway автоматически устанавливает PORT и PROVIDER статический URL
WEBAPP_URL = os.environ.get("WEBAPP_URL", f"https://{os.environ.get('RAILWAY_PUBLIC_DOMAIN', 'your-app.up.railway.app')}").rstrip('/')

CHANNEL_ID = os.environ.get("CHANNEL_ID", "@ReviewCashNews").strip()
ADMIN_USER_IDS = [s.strip() for s in os.environ.get("ADMIN_USER_IDS", "6482440657").split(",") if s.strip()]
ADMIN_JWT_SECRET = os.environ.get("ADMIN_JWT_SECRET", "replace_with_strong_secret_change_in_production")
DATA_DIR = os.environ.get("DATA_DIR", ".rc_data")
os.makedirs(DATA_DIR, exist_ok=True)

TOPUPS_FILE = os.path.join(DATA_DIR, "topups.json")
USERS_FILE = os.path.join(DATA_DIR, "users.json")
TASKS_FILE = os.path.join(DATA_DIR, "tasks.json")
TASK_TYPES_FILE = os.path.join(DATA_DIR, "task_types.json")
ADMINS_FILE = os.environ.get("ADMINS_FILE", os.path.join(DATA_DIR, "admins.json"))
WITHDRAWS_FILE = os.environ.get("WITHDRAWS_FILE", os.path.join(DATA_DIR, "withdraws.json"))

MIN_TOPUP = int(os.environ.get("MIN_TOPUP", "150"))
MIN_WITHDRAW = int(os.environ.get("MIN_WITHDRAW", "300"))

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
    arr.append(obj)
    save_json(path, arr)

def gen_id(prefix="id"):
    return f"{prefix}_{int(time.time()*1000)}_{random.randint(1000,9999)}"

def gen_manual_code():
    return "RC" + ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))

# Ensure default files
if not os.path.exists(TASK_TYPES_FILE) or not load_json(TASK_TYPES_FILE, []):
    save_json(TASK_TYPES_FILE, [
        {"id":"ya_review","name":"Отзыв — Я.К.","unit_price":100},
        {"id":"gmaps_review","name":"Отзыв — Google Maps","unit_price":65},
        {"id":"tg_sub","name":"Подписка — Telegram канал","unit_price":10},
    ])

# Create empty files if they don't exist
for path in [USERS_FILE, TASKS_FILE, TOPUPS_FILE, ADMINS_FILE, WITHDRAWS_FILE]:
    if not os.path.exists(path):
        save_json(path, [] if 'File' in path else {} if path != WITHDRAWS_FILE else [])

# ---------- App ----------
app = Flask(__name__, static_folder='public', static_url_path='/')
socketio = SocketIO(app, cors_allowed_origins="*")

# ---------- Telegram bot ----------
if BOT_TOKEN and telebot and BOT_TOKEN != "YOUR_BOT_TOKEN_HERE":
    bot = telebot.TeleBot(BOT_TOKEN)
    logger.info("Telebot configured with token: %s", BOT_TOKEN[:10] + "...")

    @bot.message_handler(commands=['start'])
    def _start(m):
        uid = m.from_user.id
        first = (m.from_user.first_name or "").strip()
        text = (
            f"👋 Привет{(' ' + first) if first else ''}! Добро пожаловать в ReviewCash!\n\n"
            "💰 Зарабатывай на отзывах:\n"
            "• Выполняй задания и получай оплату\n"
            "• Пополняй баланс через безопасные QR‑коды\n"
            "• Выводи заработок на карту\n\n"
            "🚀 Открой WebApp и начни зарабатывать!"
        )
        kb = tb_types.InlineKeyboardMarkup()
        if WEBAPP_URL:
            kb.add(tb_types.InlineKeyboardButton(
                "🚀 Открыть WebApp", 
                url=WEBAPP_URL + "/user.html"
            ))
        try:
            bot.send_message(uid, text, reply_markup=kb, parse_mode='Markdown')
        except Exception as e:
            logger.exception("bot.send_message failed for /start: %s", e)

    @bot.message_handler(commands=['mainadmin'])
    def _mainadmin(m):
        uid = str(m.from_user.id)
        is_admin = uid in ADMIN_USER_IDS
        if not is_admin:
            bot.reply_to(m, "⛔ У вас нет прав администратора.")
            return

        try:
            # Создаем JWT токен для супер-админа
            if jwt:
                payload = {
                    "uid": uid, 
                    "role": "super", 
                    "exp": datetime.utcnow() + timedelta(days=1),
                    "name": m.from_user.first_name or "Admin"
                }
                token = jwt.encode(payload, ADMIN_JWT_SECRET, algorithm="HS256")
                if isinstance(token, bytes): 
                    token = token.decode('utf-8')
                
                # Формируем URL админ-панели
                admin_url = f"{WEBAPP_URL}/admin.html?token={quote_plus(token)}"
                
                kb = tb_types.InlineKeyboardMarkup()
                kb.add(tb_types.InlineKeyboardButton(
                    "👑 Открыть Админ-панель", 
                    url=admin_url
                ))
                
                bot.reply_to(m, "🔐 Доступ к супер-админ панели:", reply_markup=kb)
            else:
                bot.reply_to(m, "⚠️ JWT не установлен. Установите PyJWT.")
        except Exception as e:
            logger.exception("Error in /mainadmin: %s", e)
            bot.reply_to(m, "❌ Ошибка генерации токена.")

    @bot.message_handler(commands=['mod', 'moderator'])
    def _moderator(m):
        uid = str(m.from_user.id)
        # Проверяем, что пользователь является админом
        admins = load_json(ADMINS_FILE, {})
        if uid not in admins:
            bot.reply_to(m, "⛔ Вы не являетесь администратором.")
            return

        try:
            if jwt:
                payload = {
                    "uid": uid, 
                    "role": "mod", 
                    "exp": datetime.utcnow() + timedelta(days=1),
                    "name": m.from_user.first_name or "Moderator"
                }
                token = jwt.encode(payload, ADMIN_JWT_SECRET, algorithm="HS256")
                if isinstance(token, bytes): 
                    token = token.decode('utf-8')
                
                # Формируем URL для модератора (используем admin.html с токеном модератора)
                mod_url = f"{WEBAPP_URL}/admin.html?token={quote_plus(token)}"
                
                kb = tb_types.InlineKeyboardMarkup()
                kb.add(tb_types.InlineKeyboardButton(
                    "🛡️ Открыть Панель Модератора", 
                    url=mod_url
                ))
                
                bot.reply_to(m, "🛡️ Доступ к панели модератора:", reply_markup=kb)
            else:
                bot.reply_to(m, "⚠️ JWT не установлен.")
        except Exception as e:
            logger.exception("Error in /mod: %s", e)
            bot.reply_to(m, "❌ Ошибка генерации токена.")

    @bot.message_handler(commands=['addadmin'])
    def _addadmin(m):
        uid = str(m.from_user.id)
        is_super = uid in ADMIN_USER_IDS
        
        if not is_super:
            bot.reply_to(m, "⛔ Только супер-админ может добавлять админов.")
            return

        try:
            # Парсим команду: /addadmin 123456789 Имя
            parts = m.text.split()
            if len(parts) < 2:
                bot.reply_to(m, "📝 Использование: /addadmin <ID_Telegram> [Имя]\n\nПример: /addadmin 123456789 Иван")
                return
            
            new_admin_id = parts[1]
            new_admin_name = parts[2] if len(parts) > 2 else "Новый Админ"
            
            # Добавляем в админы
            admins = load_json(ADMINS_FILE, {})
            admins[new_admin_id] = {
                "role": "mod", 
                "name": new_admin_name, 
                "tasks_reviewed": 0,
                "created_at": datetime.utcnow().isoformat() + "Z"
            }
            save_json(ADMINS_FILE, admins)
            
            bot.reply_to(m, f"✅ Администратор добавлен:\n👤 {new_admin_name} (ID: {new_admin_id})\n🔐 Роль: Модератор\n\nТеперь он может использовать команду /mod")
            
        except Exception as e:
            logger.exception("Error in /addadmin: %s", e)
            bot.reply_to(m, "❌ Ошибка при добавлении администратора.")

    @bot.message_handler(commands=['balance', 'баланс'])
    def _balance(m):
        uid = str(m.from_user.id)
        try:
            users = load_json(USERS_FILE, {})
            user_data = users.get(uid, {"balance": 0.0})
            balance = user_data.get("balance", 0.0)
            
            text = f"💰 Ваш баланс: **{balance:.0f} ₽**\n\n📋 Последние операции:\n"
            
            history = user_data.get("history", [])[:3]
            for item in history:
                type_emoji = {"topup": "➕", "withdraw": "➖", "work": "⭐"}
                emoji = type_emoji.get(item.get("type"), "•")
                amount = item.get("amount", 0)
                note = item.get("note", "")
                text += f"{emoji} {amount:.0f} ₽ {note}\n"
            
            kb = tb_types.InlineKeyboardMarkup()
            kb.add(tb_types.InlineKeyboardButton("💰 Пополнить", url=WEBAPP_URL + "/user.html"))
            kb.add(tb_types.InlineKeyboardButton("📋 Задания", url=WEBAPP_URL + "/user.html"))
            
            bot.reply_to(m, text, reply_markup=kb, parse_mode='Markdown')
        except Exception as e:
            logger.exception("Error in /balance: %s", e)
            bot.reply_to(m, "❌ Ошибка получения баланса.")

    def _poll():
        while True:
            try:
                logger.info("Starting bot polling...")
                bot.infinity_polling(timeout=60, long_polling_timeout=50)
            except Exception as ex:
                logger.exception("Bot polling error: %s", ex)
                time.sleep(5)
    
    import threading
    threading.Thread(target=_poll, daemon=True).start()
else:
    bot = None
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        logger.warning("Bot token not set! Set BOT_TOKEN environment variable.")
    elif not telebot:
        logger.warning("pytelegrambotapi not installed; Telegram features disabled")

# ---------- Static routes ----------
@app.route('/')
def index():
    return send_from_directory('public', 'index.html')

@app.route('/admin.html')
def admin_page():
    return send_from_directory('public', 'admin.html')

@app.route('/user.html')
def user_page():
    return send_from_directory('public', 'user.html')

@app.route('/<path:path>')
def static_proxy(path):
    return send_from_directory('public', path)

# ---------- API Endpoints ----------

# Profile endpoint for Telegram WebApp
@app.route('/api/profile_me', methods=['GET'])
def api_profile_me():
    init_data = request.headers.get('X-Tg-InitData') or request.args.get('init_data')
    if not init_data:
        return jsonify({"ok": False, "reason": "init_data_required"}), 401
    try:
        # Verify telegram init_data
        pairs = parse_qsl(init_data, keep_blank_values=True)
        params = dict(pairs)
        provided_hash = params.pop('hash', None)
        if not provided_hash:
            return jsonify({"ok": False, "reason": "invalid_init_data"}), 403
        
        data_check_arr = [f"{k}={params[k]}" for k in sorted(params.keys())]
        data_check_string = "\n".join(data_check_arr)
        secret_key = hashlib.sha256(BOT_TOKEN.encode()).digest()
        hmac_hash = hmac.new(secret_key, data_check_string.encode('utf-8'), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(hmac_hash, provided_hash):
            return jsonify({"ok": False, "reason": "hash_mismatch"}), 403
        
        uid = params.get('id') or params.get('user_id')
        username = params.get('username') or None
        first_name = params.get('first_name') or ""
        
        # Get or create user
        users = load_json(USERS_FILE, {})
        if str(uid) not in users:
            users[str(uid)] = {
                "balance": 0.0, 
                "history": [], 
                "tasks_done": 0, 
                "total_earned": 0.0,
                "first_name": first_name,
                "username": username
            }
            save_json(USERS_FILE, users)
        
        user_data = users[str(uid)]
        
        out = {
            "ok": True, 
            "user": {
                "id": uid, 
                "username": username, 
                "first_name": first_name
            }, 
            "balance": user_data.get('balance', 0.0), 
            "history": user_data.get('history', [])
        }
        
        # Try to get photo URL using bot if available
        if bot:
            try:
                photos = bot.get_user_profile_photos(int(uid))
                if photos and getattr(photos, "total_count", 0) > 0:
                    file_id = photos.photos[0][0].file_id
                    f = bot.get_file(file_id)
                    file_path = getattr(f, "file_path", None)
                    if file_path:
                        out['photo_url'] = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
            except Exception:
                pass
        
        return jsonify(out)
    except Exception as e:
        logger.exception("profile_me error: %s", e)
        return jsonify({"ok": False, "reason": "server_error"}), 500

# List public tasks
@app.route('/api/tasks_public', methods=['GET'])
def api_tasks_public():
    tasks_local = load_json(TASKS_FILE, [])
    active = [t for t in tasks_local if t.get('status', 'active') == 'active']
    return jsonify(active)

# Admin authentication
def get_token_from_request(req):
    t = req.args.get("token")
    if t:
        return t
    auth = req.headers.get("Authorization") or req.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth.split(" ",1)[1].strip()
    return None

def require_admin(role_needed="mod"):
    def decorator(func):
        def wrapper(*args, **kwargs):
            token = get_token_from_request(request)
            if not token:
                return abort(401)
            if jwt is None:
                return abort(403)
            try:
                payload = jwt.decode(token, ADMIN_JWT_SECRET, algorithms=["HS256"])
                uid = str(payload.get("uid",""))
                role = payload.get("role","")
                
                # Проверяем роль
                if role_needed == "super" and role != "super":
                    return abort(403)
                elif role_needed == "mod" and role not in ["mod", "super"]:
                    return abort(403)
                
                request.admin_user = {"id": uid, "role": role, "data": payload}
            except Exception:
                return abort(403)
            return func(*args, **kwargs)
        wrapper.__name__ = func.__name__
        return wrapper
    return decorator

# Create task (admin required)
@app.route('/api/tasks_create', methods=['POST'])
@require_admin("mod")
def api_tasks_create():
    data = request.get_json() or {}
    title = (data.get('title') or "").strip()
    description = (data.get('description') or "").strip()
    type_id = (data.get('type_id') or "").strip()
    try:
        count = int(data.get('count') or 1)
    except Exception:
        count = 1
    
    # Find unit price from types
    types = load_json(TASK_TYPES_FILE, [])
    unit_price = None
    for t in types:
        if t.get('id') == type_id:
            unit_price = int(t.get('unit_price') or 0)
            break
    if unit_price is None:
        unit_price = int(data.get('unit_price') or 0)
    
    if not title or unit_price <= 0 or count <= 0:
        return jsonify({"ok": False, "reason": "bad_params"}), 400
    
    task = {
        "id": gen_id("task"),
        "title": title,
        "description": description,
        "type_id": type_id,
        "unit_price": unit_price,
        "count": count,
        "done": 0,
        "budget": unit_price * count,
        "status": "active",
        "created_at": datetime.utcnow().isoformat()+"Z",
        "workers": []
    }
    tasks_local = load_json(TASKS_FILE, [])
    tasks_local.insert(0, task)
    save_json(TASKS_FILE, tasks_local)
    socketio.emit('new_task', task)
    return jsonify({"ok": True, "task": task})

# Task types endpoints
@app.route('/api/task_types', methods=['GET'])
def api_task_types():
    return jsonify(load_json(TASK_TYPES_FILE, []))

@app.route('/api/task_types_add', methods=['POST'])
@require_admin("super")
def api_task_types_add():
    data = request.get_json() or {}
    tid = (data.get('id') or "").strip()
    name = (data.get('name') or "").strip()
    try:
        price = int(data.get('unit_price') or 0)
    except Exception:
        price = 0
    if not tid or not name or price <= 0:
        return jsonify({"ok": False, "reason": "bad_params"}), 400
    
    types = load_json(TASK_TYPES_FILE, [])
    for t in types:
        if t.get('id') == tid:
            return jsonify({"ok": False, "reason": "id_exists"}), 400
    
    types.append({"id": tid, "name": name, "unit_price": price})
    save_json(TASK_TYPES_FILE, types)
    socketio.emit('task_types_updated', {"id": tid, "name": name, "unit_price": price})
    return jsonify({"ok": True, "type": {"id": tid, "name": name, "unit_price": price}})

# Topup creation (public)
@app.route('/api/topups_public', methods=['POST'])
def api_topups_public():
    data = request.get_json() or {}
    try:
        amount = float(data.get('amount') or 0)
    except Exception:
        amount = 0
    if amount < MIN_TOPUP:
        return jsonify({"ok": False, "reason": "min_topup", "min": MIN_TOPUP}), 400
    
    init_data = request.headers.get('X-Tg-InitData') or request.args.get('init_data')
    user = {}
    if init_data:
        try:
            pairs = parse_qsl(init_data, keep_blank_values=True)
            params = dict(pairs)
            uid = params.get('id') or params.get('user_id')
            if uid:
                user = {"id": uid}
        except Exception:
            pass
    
    topup_id = gen_id("top")
    code = gen_manual_code()
    
    rec = {
        "id": topup_id, 
        "user": user, 
        "amount": amount, 
        "status": "pending", 
        "created_at": datetime.utcnow().isoformat()+"Z", 
        "manual_code": code,
        "payment": {"comment": code}
    }
    append_json(TOPUPS_FILE, rec)
    socketio.emit('new_topup', rec)
    return jsonify({"ok": True, "topup": rec, "manual_code": code})

@app.route('/api/topups/<tid>', methods=['GET'])
def api_get_topup(tid):
    arr = load_json(TOPUPS_FILE, [])
    found = next((t for t in arr if t.get('id') == tid), None)
    if not found:
        return jsonify({"ok": False, "reason": "not_found"}), 404
    return jsonify({"ok": True, "topup": found})

# Webhook for payments
@app.route('/api/payment_webhook', methods=['POST'])
def api_payment_webhook():
    secret = os.environ.get("PAYMENT_WEBHOOK_SECRET", "")
    if secret and request.data:
        sig = request.headers.get("X-Signature") or request.headers.get("X-Hub-Signature") or ""
        mac = hmac.new(secret.encode(), request.data, digestmod=hashlib.sha256).hexdigest()
        if not sig or not hmac.compare_digest(mac, sig):
            logger.warning("Invalid webhook signature")
            return "invalid signature", 403
    
    payload = request.get_json() or {}
    order_id = payload.get('orderId') or payload.get('order_id') or payload.get('merchantOrderId')
    status = payload.get('status') or payload.get('payment_status')
    comment = payload.get('comment') or payload.get('payment_comment') or ""
    amount = None
    if payload.get('amount') is not None:
        try:
            amount = float(payload.get('amount'))
        except Exception:
            amount = None
    
    paid = False
    if isinstance(status, str) and status.lower() in ("paid","success","confirmed","complete","ok"):
        paid = True
    if isinstance(status, bool) and status is True:
        paid = True
    
    tups = load_json(TOPUPS_FILE, [])
    updated = False
    
    # Try match by order_id
    if order_id:
        for t in tups:
            if t.get('id') == str(order_id):
                if paid and t.get('status') != 'paid':
                    t['status'] = 'paid'
                    t['paid_at'] = datetime.utcnow().isoformat()+"Z"
                    if comment:
                        t.setdefault('payment', {})['comment'] = comment
                        t.setdefault('payment', {})['manual_code_verified'] = (t.get('manual_code') in comment)
                    uid = t.get('user', {}).get('id')
                    if uid:
                        # Credit user
                        users = load_json(USERS_FILE, {})
                        if str(uid) in users:
                            users[str(uid)]['balance'] = round(users[str(uid)].get('balance', 0.0) + float(t.get('amount',0.0)), 2)
                            users[str(uid)].setdefault('history', []).insert(0, {
                                "type": "topup", 
                                "amount": t.get('amount',0.0), 
                                "note": "topup_webhook", 
                                "created_at": datetime.utcnow().isoformat()+"Z"
                            })
                            save_json(USERS_FILE, users)
                    updated = True
                break
    
    # Else match by manual_code in comment + amount
    if not updated and comment:
        for t in tups:
            if t.get('status') != 'pending':
                continue
            code = t.get('manual_code')
            if code and code in comment:
                if amount is not None and abs(float(t.get('amount',0)) - float(amount)) > 0.01:
                    continue
                t['status'] = 'paid'
                t['paid_at'] = datetime.utcnow().isoformat()+"Z"
                t.setdefault('payment', {})['comment'] = comment
                t.setdefault('payment', {})['manual_code_verified'] = True
                uid = t.get('user',{}).get('id')
                if uid:
                    # Credit user
                    users = load_json(USERS_FILE, {})
                    if str(uid) in users:
                        users[str(uid)]['balance'] = round(users[str(uid)].get('balance', 0.0) + float(t.get('amount',0.0)), 2)
                        users[str(uid)].setdefault('history', []).insert(0, {
                            "type": "topup", 
                            "amount": t.get('amount',0.0), 
                            "note": "topup_webhook_code", 
                            "created_at": datetime.utcnow().isoformat()+"Z"
                        })
                        save_json(USERS_FILE, users)
                updated = True
                break
    
    if updated:
        save_json(TOPUPS_FILE, tups)
        socketio.emit('topup_updated', {"matched": True})
        return jsonify({"ok": True})
    
    logger.info("webhook no match order=%s comment=%s amount=%s", order_id, comment, amount)
    return jsonify({"ok": True, "matched": False})

# Admin endpoints: Dashboard
@app.route('/api/admin/dashboard', methods=['GET'])
@require_admin("mod")
def api_admin_dashboard():
    users = load_json(USERS_FILE, {})
    tasks = load_json(TASKS_FILE, [])
    topups = load_json(TOPUPS_FILE, [])
    withdraws = load_json(WITHDRAWS_FILE, [])
    
    # Calculate stats
    users_count = len(users)
    total_revenue = sum(t.get('amount', 0) for t in topups if t.get('status') == 'paid')
    tasks_count = len([t for t in tasks if t.get('status') == 'active'])
    pending_count = len([t for t in topups if t.get('status') == 'pending']) + len([w for w in withdraws if w.get('status') == 'pending'])
    
    # Recent activity
    recent_activity = []
    for t in topups[:5]:
        recent_activity.append({
            "type": "Пополнение",
            "id": t.get('id'),
            "amount": t.get('amount'),
            "status": t.get('status'),
            "user": f"User {t.get('user', {}).get('id', 'Unknown')}",
            "created_at": t.get('created_at')
        })
    
    return jsonify({
        "ok": True,
        "data": {
            "usersCount": users_count,
            "totalRevenue": total_revenue,
            "tasksCount": tasks_count,
            "pendingCount": pending_count,
            "recentActivity": recent_activity
        }
    })

# Admin endpoints: List incoming topups
@app.route('/api/admin/incoming_topups', methods=['GET'])
@require_admin("mod")
def api_admin_incoming_topups():
    status_q = (request.args.get('status') or "").strip().lower()
    comment_q = (request.args.get('comment') or "").strip().lower()
    tups = load_json(TOPUPS_FILE, [])
    out = []
    for t in tups:
        if status_q and t.get('status','').lower() != status_q:
            continue
        if comment_q:
            comment = (t.get('payment',{}).get('comment') or "") + " " + (t.get('manual_code') or "")
            if comment_q not in comment.lower():
                continue
        out.append(t)
    out.sort(key=lambda x: x.get('created_at',''), reverse=True)
    return jsonify({"ok": True, "items": out})

@app.route('/api/admin/incoming_topups/<tid>/mark_paid', methods=['POST'])
@require_admin("super")
def api_admin_mark_paid(tid):
    tups = load_json(TOPUPS_FILE, [])
    found = next((t for t in tups if t.get('id')==tid), None)
    if not found:
        return jsonify({"ok": False, "reason": "not_found"}), 404
    if found.get('status') == 'paid':
        return jsonify({"ok": False, "reason": "already_paid"}), 400
    
    # Mark as paid and credit user
    found['status'] = 'paid'
    found['paid_at'] = datetime.utcnow().isoformat()+"Z"
    found.setdefault('payment', {})['manual_code_verified'] = True
    save_json(TOPUPS_FILE, tups)
    
    uid = found.get('user',{}).get('id')
    if uid:
        users = load_json(USERS_FILE, {})
        if str(uid) in users:
            users[str(uid)]['balance'] = round(users[str(uid)].get('balance', 0.0) + float(found.get('amount',0.0)), 2)
            users[str(uid)].setdefault('history', []).insert(0, {
                "type": "topup", 
                "amount": found.get('amount',0.0), 
                "note": "admin_approved", 
                "created_at": datetime.utcnow().isoformat()+"Z"
            })
            save_json(USERS_FILE, users)
    
    socketio.emit('topup_updated', {"approved": tid})
    return jsonify({"ok": True})

@app.route('/api/admin/incoming_topups/<tid>/refund', methods=['POST'])
@require_admin("super")
def api_admin_refund(tid):
    data = request.get_json() or {}
    reason = (data.get('reason') or "").strip()
    tups = load_json(TOPUPS_FILE, [])
    found = next((t for t in tups if t.get('id')==tid), None)
    if not found:
        return jsonify({"ok": False, "reason": "not_found"}), 404
    if found.get('status') == 'refunded':
        return jsonify({"ok": False, "reason": "already_refunded"}), 400
    
    found['status'] = 'refunded'
    found['refunded_at'] = datetime.utcnow().isoformat()+"Z"
    found.setdefault('refund_info', {})['reason'] = reason
    save_json(TOPUPS_FILE, tups)
    socketio.emit('topup_updated', {"rejected": tid})
    return jsonify({"ok": True})

# Withdraw endpoints
@app.route('/api/withdraw_public', methods=['POST'])
def api_withdraw_public():
    data = request.get_json() or {}
    try:
        amount = float(data.get('amount') or 0)
    except Exception:
        amount = 0
    name = (data.get('name') or "").strip()
    details = (data.get('details') or "").strip()
    
    if amount < MIN_WITHDRAW:
        return jsonify({"ok": False, "reason": "min_withdraw", "min": MIN_WITHDRAW}), 400
    
    init_data = request.headers.get('X-Tg-InitData') or request.args.get('init_data')
    if not init_data:
        return jsonify({"ok": False, "reason": "init_data_required"}), 401
    
    # Verify and get uid
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True)
        params = dict(pairs)
        provided_hash = params.pop('hash', None)
        if not provided_hash:
            return jsonify({"ok": False, "reason": "invalid_init_data"}), 403
        
        data_check_arr = [f"{k}={params[k]}" for k in sorted(params.keys())]
        data_check_string = "\n".join(data_check_arr)
        secret_key = hashlib.sha256(BOT_TOKEN.encode()).digest()
        hmac_hash = hmac.new(secret_key, data_check_string.encode('utf-8'), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(hmac_hash, provided_hash):
            return jsonify({"ok": False, "reason": "hash_mismatch"}), 403
        
        uid = params.get('id') or params.get('user_id')
    except Exception:
        return jsonify({"ok": False, "reason": "invalid_init_data"}), 403
    
    if not uid:
        return jsonify({"ok": False, "reason": "uid_missing"}), 401
    
    # Check balance
    users = load_json(USERS_FILE, {})
    if str(uid) not in users:
        return jsonify({"ok": False, "reason": "user_not_found"}), 404
    
    user_data = users[str(uid)]
    if user_data.get('balance', 0.0) < amount:
        return jsonify({"ok": False, "reason": "insufficient_balance", "balance": user_data.get('balance', 0.0)}), 400
    
    # Deduct from balance
    user_data['balance'] = round(user_data.get('balance', 0.0) - amount, 2)
    user_data.setdefault('history', []).insert(0, {
        "type":"withdraw", 
        "amount": amount, 
        "name": name, 
        "details": details, 
        "created_at": datetime.utcnow().isoformat()+"Z"
    })
    save_json(USERS_FILE, users)
    
    # Create withdraw record
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
    socketio.emit('withdraw_created', {"user": uid, "amount": amount})
    return jsonify({"ok": True, "balance_after": user_data['balance']})

@app.route('/api/admin/withdraws', methods=['GET'])
@require_admin("mod")
def api_admin_withdraws():
    status_q = (request.args.get('status') or "").strip().lower()
    method_q = (request.args.get('method') or "").strip().lower()
    wds = load_json(WITHDRAWS_FILE, [])
    out = []
    for w in wds:
        if status_q and w.get('status','').lower() != status_q:
            continue
        if method_q and (w.get('details') or "").lower().find(method_q) == -1:
            continue
        out.append(w)
    out.sort(key=lambda x: x.get('created_at',''), reverse=True)
    return jsonify({"ok": True, "items": out})

@app.route('/api/admin/withdraws/<wid>/approve', methods=['POST'])
@require_admin("super")
def api_admin_withdraw_approve(wid):
    wds = load_json(WITHDRAWS_FILE, [])
    found = next((w for w in wds if w.get('id')==wid), None)
    if not found:
        return jsonify({"ok": False, "reason": "not_found"}), 404
    if found.get('status') != 'pending':
        return jsonify({"ok": False, "reason": "not_pending"}), 400
    
    found['status'] = 'approved'
    found['processed_at'] = datetime.utcnow().isoformat()+"Z"
    save_json(WITHDRAWS_FILE, wds)
    socketio.emit('withdraw_updated', {"approved": wid})
    return jsonify({"ok": True})

@app.route('/api/admin/withdraws/<wid>/reject', methods=['POST'])
@require_admin("super")
def api_admin_withdraw_reject(wid):
    data = request.get_json() or {}
    reason = (data.get('reason') or "").strip()
    wds = load_json(WITHDRAWS_FILE, [])
    found = next((w for w in wds if w.get('id')==wid), None)
    if not found:
        return jsonify({"ok": False, "reason": "not_found"}), 404
    if found.get('status') != 'pending':
        return jsonify({"ok": False, "reason": "not_pending"}), 400
    
    # Refund to user
    uid = found.get('user', {}).get('id')
    amount = found.get('amount', 0)
    if uid:
        users = load_json(USERS_FILE, {})
        if str(uid) in users:
            users[str(uid)]['balance'] = round(users[str(uid)].get('balance', 0.0) + amount, 2)
            users[str(uid)].setdefault('history', []).insert(0, {
                "type": "refund", 
                "amount": amount, 
                "note": "withdraw_rejected", 
                "reason": reason,
                "created_at": datetime.utcnow().isoformat()+"Z"
            })
            save_json(USERS_FILE, users)
    
    found['status'] = 'rejected'
    found['reason'] = reason
    found['processed_at'] = datetime.utcnow().isoformat()+"Z"
    save_json(WITHDRAWS_FILE, wds)
    socketio.emit('withdraw_updated', {"rejected": wid})
    return jsonify({"ok": True})

# Admin stats
@app.route('/api/admin/stats', methods=['GET'])
@require_admin("mod")
def api_admin_stats():
    admins = load_json(ADMINS_FILE, {})
    out = []
    for k,v in admins.items():
        v_copy = dict(v)
        v_copy['id'] = k
        out.append(v_copy)
    return jsonify({"ok": True, "admins": out})

# User history
@app.route('/api/user_history', methods=['GET'])
def api_user_history():
    uid = (request.args.get('uid') or "").strip()
    if not uid:
        return jsonify({"ok": False, "reason": "missing_uid"}), 400
    
    users = load_json(USERS_FILE, {})
    if str(uid) not in users:
        return jsonify({"ok": False, "reason": "user_not_found"}), 404
    
    user_data = users[str(uid)]
    items = user_data.get('history', [])
    return jsonify({"ok": True, "items": items})

# Admin users list
@app.route('/api/admin/users', methods=['GET'])
@require_admin("mod")
def api_admin_users():
    users = load_json(USERS_FILE, {})
    out = []
    for uid, data in users.items():
        out.append({
            "id": uid,
            "first_name": data.get("first_name"),
            "username": data.get("username"),
            "balance": data.get("balance", 0.0),
            "tasks_done": data.get("tasks_done", 0),
            "created_at": data.get("created_at", datetime.utcnow().isoformat()+"Z")
        })
    return jsonify({"ok": True, "users": out})

# Admin tasks list
@app.route('/api/admin/tasks', methods=['GET'])
@require_admin("mod")
def api_admin_tasks():
    tasks = load_json(TASKS_FILE, [])
    return jsonify({"ok": True, "tasks": tasks})

# ---------- Run ----------
if __name__ == '__main__':
    port = int(os.environ.get("PORT", "8080"))
    logger.info(f"Starting server on port {port}")
    logger.info(f"WEBAPP_URL: {WEBAPP_URL}")
    logger.info(f"ADMIN_USER_IDS: {ADMIN_USER_IDS}")
    socketio.run(app, host='0.0.0.0', port=port)
