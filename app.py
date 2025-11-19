from flask import Flask, request, send_from_directory, jsonify, abort
import telebot
import threading
import time
import os
import json
import jwt
from datetime import datetime, timedelta
from urllib.parse import quote_plus

# ========== CONFIG ==========
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8033069276:AAFv1-kdQ68LjvLEgLHj3ZXd5ehMqyUXOYU")
WEBAPP_URL = os.environ.get("WEBAPP_URL", "https://web-production-398fb.up.railway.app")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "@ReviewCashNews")

# If ADMIN_USER_IDS env var is provided it will be used, otherwise default to your id (you provided 6482440657).
ADMIN_USER_IDS = [s.strip() for s in os.environ.get("ADMIN_USER_IDS", "6482440657").split(",") if s.strip()]
ADMIN_USERNAMES = [s.strip() for s in os.environ.get("ADMIN_USERNAMES", "Sa1Raz").split(",") if s.strip()]

ADMIN_JWT_SECRET = os.environ.get("ADMIN_JWT_SECRET", "replace_with_strong_secret")
ADMIN_TOKEN_TTL_SECONDS = int(os.environ.get("ADMIN_TOKEN_TTL_SECONDS", 300))  # default 5 minutes

DATA_DIR = os.environ.get("DATA_DIR", ".rc_data")
TOPUPS_FILE = os.path.join(DATA_DIR, "topups.json")
WITHDRAWS_FILE = os.path.join(DATA_DIR, "withdraws.json")

# ensure data dir exists
os.makedirs(DATA_DIR, exist_ok=True)

# ========== HELPERS FOR STORAGE ==========
def load_json_safe(path, default):
    try:
        if not os.path.exists(path):
            return default
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def append_json(path, obj):
    arr = load_json_safe(path, [])
    arr.append(obj)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(arr, f, ensure_ascii=False, indent=2)

# ========== BOT & FLASK ==========
bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__, static_folder='public')

# in-memory users (keeps existing behavior)
users = {}  # user_id: {balance, tasks_done, total_earned, subscribed}

# ========== SUBSCRIPTION CHECK ==========
def check_subscription(user_id):
    try:
        member = bot.get_chat_member(CHANNEL_ID, user_id)
        return member.status in ['member', 'administrator', 'creator']
    except Exception as e:
        print(f"Ошибка проверки подписки: {e}")
        return False

# ========== STATIC FILES ==========
@app.route('/')
def index():
    return send_from_directory('public', 'index.html')

@app.route('/<path:path>')
def static_files(path):
    return send_from_directory('public', path)

# ========== WEBHOOK ==========
@app.route('/webhook', methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        update = telebot.types.Update.de_json(request.get_data().decode('utf-8'))
        bot.process_new_updates([update])
        return '', 200
    return 'Invalid', 403

# ========== TELEGRAM KEYBOARD ==========
def main_keyboard():
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    webapp = telebot.types.WebAppInfo(WEBAPP_URL)
    btn = telebot.types.KeyboardButton("ReviewCash", web_app=webapp)
    markup.add(btn)
    return markup

# ========== START HANDLER WITH SUBSCRIPTION CHECK ==========
@bot.message_handler(commands=['start'])
def start(message):
    user_id = message.from_user.id
    if user_id not in users:
        users[user_id] = {"balance": 0, "tasks_done": 0, "total_earned": 0, "subscribed": False}

    # Автопроверка подписки
    if not check_subscription(user_id):
        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(telebot.types.InlineKeyboardButton("Подписаться на @ReviewCashNews", url=f"https://t.me/{CHANNEL_ID.lstrip('@')}"))
        markup.add(telebot.types.InlineKeyboardButton("Проверить подписку", callback_data="check_sub"))
        bot.send_message(
            message.chat.id,
            "ReviewCash — зарабатывай на отзывах!\n\n"
            "Обязательно подпишись на канал:\n"
            f"{CHANNEL_ID}\n\n"
            "Нажми кнопку и проверь!",
            parse_mode="Markdown",
            reply_markup=markup
        )
    else:
        users[user_id]["subscribed"] = True
        bot.send_message(
            message.chat.id,
            "ReviewCash\n\n"
            "Зарабатывай от 100 до 10 000 ₽ за отзыв!\n"
            "Мгновенные выплаты • 100% честно\n\n"
            "Нажми кнопку ниже и начинай!",
            parse_mode="Markdown",
            reply_markup=main_keyboard()
        )

# ========== CALLBACK CHECK SUB ==========
@bot.callback_query_handler(func=lambda call: call.data == "check_sub")
def check_sub(call):
    user_id = call.from_user.id
    if check_subscription(user_id):
        users[user_id]["subscribed"] = True
        bot.edit_message_text(
            "✅ Подписка подтверждена!\n\nТеперь ты можешь зарабатывать!",
            call.message.chat.id,
            call.message.message_id
        )
        bot.send_message(call.message.chat.id, "Го зарабатывать! 👇", reply_markup=main_keyboard())
    else:
        bot.answer_callback_query(call.id, "Ты ещё не подписался! Нажми 'Подписаться'")

# ========== ADMIN TOKEN GENERATION & VERIFICATION ==========
def generate_admin_token(uid, username):
    payload = {
        "uid": str(uid) if uid is not None else "",
        "username": username or "",
        "exp": datetime.utcnow() + timedelta(seconds=ADMIN_TOKEN_TTL_SECONDS),
        "iat": datetime.utcnow()
    }
    token = jwt.encode(payload, ADMIN_JWT_SECRET, algorithm="HS256")
    if isinstance(token, bytes):
        token = token.decode('utf-8')
    return token

def verify_admin_token(token):
    try:
        payload = jwt.decode(token, ADMIN_JWT_SECRET, algorithms=["HS256"])
        uid = str(payload.get("uid", ""))
        username = (payload.get("username") or "").strip()
        if uid and uid in ADMIN_USER_IDS:
            return True, payload
        if username and username in ADMIN_USERNAMES:
            return True, payload
        return False, None
    except jwt.ExpiredSignatureError:
        return False, "expired"
    except Exception as e:
        return False, None

# ========== ADMIN NOTIFICATION HELPERS ==========
def notify_admins_topup(topup):
    text = (f"Новая заявка на пополнение\n"
            f"Пользователь: {topup['user'].get('username','-')} ({topup['user'].get('id','-')})\n"
            f"Сумма: {topup.get('amount',0)} ₽\n"
            f"Код: {topup.get('code','-')}\n"
            f"Создано: {topup.get('created_at','-')}")
    for admin_id in ADMIN_USER_IDS:
        try:
            token = generate_admin_token(admin_id, "")  # token tied to admin id
            url = f"{WEBAPP_URL}/mainadmin?token={quote_plus(token)}"
            kb = telebot.types.InlineKeyboardMarkup()
            kb.add(telebot.types.InlineKeyboardButton("Открыть панель — проверить пополнение", url=url))
            bot.send_message(admin_id, text, reply_markup=kb)
        except Exception as e:
            print("notify_admins_topup -> send to id error:", admin_id, e)
    for admin_username in ADMIN_USERNAMES:
        if admin_username and admin_username in ADMIN_USER_IDS:
            continue
        try:
            token = generate_admin_token("", admin_username)
            url = f"{WEBAPP_URL}/mainadmin?token={quote_plus(token)}"
            kb = telebot.types.InlineKeyboardMarkup()
            kb.add(telebot.types.InlineKeyboardButton("Открыть панель — проверить пополнение", url=url))
            bot.send_message(f"@{admin_username}", text, reply_markup=kb)
        except Exception as e:
            print("notify_admins_topup -> send to username error:", admin_username, e)

def notify_admins_withdraw(withdraw):
    text = (f"Новая заявка на вывод\n"
            f"Пользователь: {withdraw['user'].get('username','-')} ({withdraw['user'].get('id','-')})\n"
            f"Сумма: {withdraw.get('amount',0)} ₽\n"
            f"Реквизиты: {withdraw.get('card','-')} / {withdraw.get('bank','-')}\n"
            f"ФИО: {withdraw.get('name','-')}\n"
            f"Создано: {withdraw.get('created_at','-')}")
    for admin_id in ADMIN_USER_IDS:
        try:
            token = generate_admin_token(admin_id, "")
            url = f"{WEBAPP_URL}/mainadmin?token={quote_plus(token)}"
            kb = telebot.types.InlineKeyboardMarkup()
            kb.add(telebot.types.InlineKeyboardButton("Открыть панель — обработать вывод", url=url))
            bot.send_message(admin_id, text, reply_markup=kb)
        except Exception as e:
            print("notify_admins_withdraw -> send to id error:", admin_id, e)
    for admin_username in ADMIN_USERNAMES:
        if admin_username and admin_username in ADMIN_USER_IDS:
            continue
        try:
            token = generate_admin_token("", admin_username)
            url = f"{WEBAPP_URL}/mainadmin?token={quote_plus(token)}"
            kb = telebot.types.InlineKeyboardMarkup()
            kb.add(telebot.types.InlineKeyboardButton("Открыть панель — обработать вывод", url=url))
            bot.send_message(f"@{admin_username}", text, reply_markup=kb)
        except Exception as e:
            print("notify_admins_withdraw -> send to username error:", admin_username, e)

# ========== /mainadmin BOT COMMAND ==========
@bot.message_handler(commands=['mainadmin'])
def mainadmin_command(message):
    sender = message.from_user
    uid = str(sender.id)
    username = (sender.username or "").strip()
    is_admin = (uid in ADMIN_USER_IDS) or (username in ADMIN_USERNAMES)
    if not is_admin:
        bot.send_message(message.chat.id, "Доступ запрещён: эта команда только для администратора.")
        return
    token = generate_admin_token(uid, username)
    url = f"{WEBAPP_URL}/mainadmin?token={quote_plus(token)}"
    kb = telebot.types.InlineKeyboardMarkup()
    kb.add(telebot.types.InlineKeyboardButton("Открыть админ-панель", url=url))
    bot.send_message(message.chat.id, "Откройте админ-панель (токен действует короткое время):", reply_markup=kb)

# ========== WEBAPP DATA HANDLER ==========
@bot.message_handler(content_types=['web_app_data'])
def webapp_handler(message):
    try:
        data = json.loads(message.web_app_data.data)
    except Exception:
        bot.send_message(message.chat.id, "Неверные данные из WebApp")
        return

    user_id = message.from_user.id
    action = data.get("action")

    if user_id not in users:
        users[user_id] = {"balance": 0, "tasks_done": 0, "total_earned": 0, "subscribed": True}

    # minimal amounts: topup >=100, withdraw >=250
    if action == "get_tasks":
        response = {
            "tasks": [],
            "user": {
                "balance": users[user_id]["balance"],
                "tasks_done": users[user_id]["tasks_done"],
                "total_earned": users[user_id]["total_earned"]
            },
            "completed": []
        }
        try:
            bot.send_data(message.chat.id, json.dumps(response))
        except Exception:
            bot.send_message(message.chat.id, "Данные пользователя отправлены (fallback).")

    elif action == "request_topup":
        amount = int(data.get("amount", 0) or 0)
        code = data.get("code", "000000")
        if amount < 100:
            bot.send_message(user_id, "Минимальная сумма пополнения — 100 ₽!")
            return
        topup = {
            "id": f"T_{int(time.time()*1000)}",
            "user": {"id": str(user_id), "username": sender_username_safe(message.from_user)},
            "amount": amount,
            "code": code,
            "phone": "+79600738559",
            "status": "pending",
            "created_at": datetime.utcnow().isoformat() + "Z"
        }
        append_json(TOPUPS_FILE, topup)
        bot.send_message(user_id, f"Заявка на пополнение {amount} ₽ принята!\nКод: `{code}`\nОжидайте зачисления после проверки.", parse_mode="Markdown")
        try:
            notify_admins_topup(topup)
        except Exception as e:
            print("notify_admins_topup error:", e)

    elif action == "request_withdraw":
        amount = int(data.get("amount", 0) or 0)
        bank = (data.get("bank", "") or "").lower()
        if amount < 250:
            bot.send_message(user_id, "Минимальная сумма вывода — 250 ₽!")
            return
        valid_banks = ["т-банк", "тинькофф", "сбер", "сбербанк", "втб", "альфа", "альфа-банк", "райффайзен", "райф", "tinkoff", "t-bank", "sber"]
        if not any(b in bank for b in valid_banks):
            bot.send_message(user_id, "Укажи настоящий банк: Тинькофф, Сбер, ВТБ, Альфа и т.д.")
            return
        withdraw = {
            "id": f"W_{int(time.time()*1000)}",
            "user": {"id": str(user_id), "username": sender_username_safe(message.from_user)},
            "amount": amount,
            "bank": bank,
            "name": data.get("name", ""),
            "card": data.get("card", ""),
            "status": "pending",
            "created_at": datetime.utcnow().isoformat() + "Z"
        }
        append_json(WITHDRAWS_FILE, withdraw)
        try:
            users[user_id]["balance"] = max(0, users[user_id].get("balance", 0) - amount)
        except Exception:
            pass
        bot.send_message(user_id, f"Заявка на вывод {amount} ₽ принята! Ожидает обработки админом.")
        try:
            notify_admins_withdraw(withdraw)
        except Exception as e:
            print("notify_admins_withdraw error:", e)

    else:
        bot.send_message(user_id, "Неизвестное действие из WebApp")

def sender_username_safe(from_user):
    return getattr(from_user, "username", "") or getattr(from_user, "first_name", "") or ""

# ========== ADMIN-PROTECTED ROUTES ==========
def get_token_from_request(req):
    t = req.args.get("token")
    if t:
        return t
    auth = req.headers.get("Authorization", "") or req.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip()
    return None

def require_admin_token(func):
    def wrapper(*args, **kwargs):
        token = get_token_from_request(request)
        if not token:
            return abort(401, "token required")
        ok, payload_or_reason = verify_admin_token(token)
        if not ok:
            if payload_or_reason == "expired":
                return abort(401, "token expired")
            return abort(403, "forbidden")
        return func(*args, **kwargs)
    wrapper.__name__ = func.__name__
    return wrapper

@app.route('/mainadmin')
def serve_mainadmin():
    token = request.args.get("token")
    ok, _ = verify_admin_token(token) if token else (False, None)
    if not ok:
        return "<h3>Доступ запрещён. Откройте панель только через телеграм-команду администратора.</h3>", 403
    return send_from_directory('public', 'mainadmin.html')

# API endpoints for admin panel (protected)
@app.route('/api/topups', methods=['GET'])
@require_admin_token
def api_topups():
    data = load_json_safe(TOPUPS_FILE, [])
    return jsonify(data)

@app.route('/api/withdraws', methods=['GET'])
@require_admin_token
def api_withdraws():
    data = load_json_safe(WITHDRAWS_FILE, [])
    return jsonify(data)

@app.route('/api/tasks', methods=['GET'])
@require_admin_token
def api_tasks():
    data = load_json_safe(os.path.join(DATA_DIR, "tasks.json"), [])
    return jsonify(data)

@app.route('/api/topups/<req_id>/approve', methods=['POST'])
@require_admin_token
def api_topup_approve(req_id):
    arr = load_json_safe(TOPUPS_FILE, [])
    for it in arr:
        if it.get("id") == req_id:
            if it.get("status") == "approved":
                return jsonify({"ok": False, "reason": "already_approved"}), 400
            it["status"] = "approved"
            it["handled_by"] = "admin"
            it["handled_at"] = datetime.utcnow().isoformat() + "Z"
            uid = int(it["user"]["id"])
            if uid not in users:
                users[uid] = {"balance": 0, "tasks_done": 0, "total_earned": 0, "subscribed": False}
            users[uid]["balance"] = users[uid].get("balance", 0) + it.get("amount", 0)
            with open(TOPUPS_FILE, "w", encoding="utf-8") as f:
                json.dump(arr, f, ensure_ascii=False, indent=2)
            return jsonify({"ok": True})
    return jsonify({"ok": False, "reason": "not_found"}), 404

@app.route('/api/withdraws/<req_id>/reject', methods=['POST'])
@require_admin_token
def api_withdraw_reject(req_id):
    payload = request.get_json() or {}
    reason = payload.get("reason", "Отклонено администратором")
    arr = load_json_safe(WITHDRAWS_FILE, [])
    for it in arr:
        if it.get("id") == req_id:
            if it.get("status") in ("rejected", "paid"):
                return jsonify({"ok": False, "reason": "already_handled"}), 400
            it["status"] = "rejected"
            it["handled_by"] = "admin"
            it["handled_at"] = datetime.utcnow().isoformat() + "Z"
            it["reject_reason"] = reason
            try:
                uid = int(it["user"]["id"])
                users[uid]["balance"] = users[uid].get("balance", 0) + it.get("amount", 0)
            except Exception:
                pass
            with open(WITHDRAWS_FILE, "w", encoding="utf-8") as f:
                json.dump(arr, f, ensure_ascii=False, indent=2)
            return jsonify({"ok": True})
    return jsonify({"ok": False, "reason": "not_found"}), 404

# ========== RUN & WEBHOOK SETUP ==========
def setup_webhook():
    time.sleep(3)
    try:
        bot.remove_webhook()
    except Exception:
        pass
    time.sleep(1)
    try:
        bot.set_webhook(url=f"{WEBAPP_URL}/webhook")
    except Exception as e:
        print("set_webhook error:", e)

if __name__ == '__main__':
    threading.Thread(target=setup_webhook, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
