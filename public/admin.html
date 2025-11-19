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

# MAIN admins (you). Prefer numeric IDs. By default use the id you provided.
ADMIN_USER_IDS = [s.strip() for s in os.environ.get("ADMIN_USER_IDS", "6482440657").split(",") if s.strip()]
ADMIN_USERNAMES = [s.strip() for s in os.environ.get("ADMIN_USERNAMES", "Sa1Raz").split(",") if s.strip()]

ADMIN_JWT_SECRET = os.environ.get("ADMIN_JWT_SECRET", "replace_with_strong_secret")
ADMIN_TOKEN_TTL_SECONDS = int(os.environ.get("ADMIN_TOKEN_TTL_SECONDS", 300))  # default 5 minutes

DATA_DIR = os.environ.get("DATA_DIR", ".rc_data")
TOPUPS_FILE = os.path.join(DATA_DIR, "topups.json")
WITHDRAWS_FILE = os.path.join(DATA_DIR, "withdraws.json")
TASKS_FILE = os.path.join(DATA_DIR, "tasks.json")
WORKS_FILE = os.path.join(DATA_DIR, "works.json")
USERS_FILE = os.path.join(DATA_DIR, "users.json")
ADMINS_FILE = os.path.join(DATA_DIR, "admins.json")  # ordinary admins (receive notifications)

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

def save_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)

def append_json(path, obj):
    arr = load_json_safe(path, [])
    arr.append(obj)
    save_json(path, arr)

# ========== BOT & FLASK ==========
bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__, static_folder='public')

# load persisted users and admins
users = load_json_safe(USERS_FILE, {})  # keyed by numeric id as string
ordinary_admins = load_json_safe(ADMINS_FILE, [])  # array of strings: numeric ids or usernames

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

# ========== USERS PERSISTENCE HELPERS ==========
def save_users():
    save_json(USERS_FILE, users)

def get_user_record(uid):
    key = str(uid)
    if key not in users:
        users[key] = {"balance": 0, "tasks_done": 0, "total_earned": 0, "subscribed": False, "last_submissions": {}}
        save_users()
    return users[key]

# ========== ADMINS MANAGEMENT HELPERS ==========
def save_ordinary_admins():
    save_json(ADMINS_FILE, ordinary_admins)

def is_main_admin(uid_or_username):
    s = str(uid_or_username)
    if s in ADMIN_USER_IDS: return True
    if s in ADMIN_USERNAMES: return True
    return False

def is_ordinary_admin(uid_or_username):
    s = str(uid_or_username)
    return s in ordinary_admins

def add_ordinary_admin(identifier):
    s = str(identifier)
    if s in ordinary_admins: return False
    ordinary_admins.append(s)
    save_ordinary_admins()
    return True

def remove_ordinary_admin(identifier):
    s = str(identifier)
    if s in ordinary_admins:
        ordinary_admins.remove(s)
        save_ordinary_admins()
        return True
    return False

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
        # allow ordinary admins if their id/username matches payload? for mainadmin we restrict to main admins only
        return False, None
    except jwt.ExpiredSignatureError:
        return False, "expired"
    except Exception as e:
        return False, None

# ========== NOTIFY ORDINARY ADMINS (only ordinary admins are notified) ==========
def notify_ordinary_admins_text(text, button_text="Открыть панель"):
    # send a short-lived mainadmin-token for convenience to each ordinary admin
    for admin in ordinary_admins:
        try:
            # if looks like numeric id
            if admin.isdigit():
                token = generate_admin_token(admin, "")
                url = f"{WEBAPP_URL}/mainadmin?token={quote_plus(token)}"
                kb = telebot.types.InlineKeyboardMarkup()
                kb.add(telebot.types.InlineKeyboardButton(button_text, url=url))
                bot.send_message(int(admin), text, reply_markup=kb)
            else:
                token = generate_admin_token("", admin)
                url = f"{WEBAPP_URL}/mainadmin?token={quote_plus(token)}"
                kb = telebot.types.InlineKeyboardMarkup()
                kb.add(telebot.types.InlineKeyboardButton(button_text, url=url))
                try:
                    bot.send_message(f"@{admin}", text, reply_markup=kb)
                except Exception as e:
                    print("notify -> send to @username failed:", admin, e)
        except Exception as e:
            print("notify -> error for admin", admin, e)

def notify_admins_topup(topup):
    text = (f"Новая заявка на пополнение\n"
            f"Пользователь: {topup['user'].get('username','-')} ({topup['user'].get('id','-')})\n"
            f"Сумма: {topup.get('amount',0)} ₽\n"
            f"Код: {topup.get('code','-')}\n"
            f"Создано: {topup.get('created_at','-')}")
    notify_ordinary_admins_text(text, button_text="Проверить пополнение")

def notify_admins_withdraw(withdraw):
    text = (f"Новая заявка на вывод\n"
            f"Пользователь: {withdraw['user'].get('username','-')} ({withdraw['user'].get('id','-')})\n"
            f"Сумма: {withdraw.get('amount',0)} ₽\n"
            f"Реквизиты: {withdraw.get('card','-')} / {withdraw.get('bank','-')}\n"
            f"ФИО: {withdraw.get('name','-')}\n"
            f"Создано: {withdraw.get('created_at','-')}")
    notify_ordinary_admins_text(text, button_text="Проверить вывод")

def notify_admins_work(work):
    text = (f"Новая заявка на выполнение задания\n"
            f"Пользователь: {work['user'].get('username','-')} ({work['user'].get('id','-')})\n"
            f"Задание: {work.get('task_title','-')}\n"
            f"Тип: {work.get('platform','-')} · Сумма: {work.get('amount',0)} ₽\n"
            f"Создано: {work.get('created_at','-')}")
    notify_ordinary_admins_text(text, button_text="Проверить выполнение")

# ========== BOT COMMANDS: admin management ==========
@bot.message_handler(commands=['addadmin'])
def cmd_addadmin(message):
    sender = message.from_user
    uid = str(sender.id)
    username = (sender.username or "").strip()
    if not is_main_admin(uid) and not is_main_admin(username):
        bot.send_message(message.chat.id, "Только главный админ может добавлять обычных админов.")
        return
    args = message.text.split()
    if len(args) < 2:
        bot.send_message(message.chat.id, "Использование: /addadmin <telegram_id_or_username_without_@>")
        return
    ident = args[1].strip().lstrip('@')
    if add_ordinary_admin(ident):
        bot.send_message(message.chat.id, f"Обычный админ {ident} добавлен.")
    else:
        bot.send_message(message.chat.id, f"{ident} уже в списке админов.")

@bot.message_handler(commands=['removeadmin'])
def cmd_removeadmin(message):
    sender = message.from_user
    uid = str(sender.id)
    username = (sender.username or "").strip()
    if not is_main_admin(uid) and not is_main_admin(username):
        bot.send_message(message.chat.id, "Только главный админ может удалять обычных админов.")
        return
    args = message.text.split()
    if len(args) < 2:
        bot.send_message(message.chat.id, "Использование: /removeadmin <telegram_id_or_username_without_@>")
        return
    ident = args[1].strip().lstrip('@')
    if remove_ordinary_admin(ident):
        bot.send_message(message.chat.id, f"Обычный админ {ident} удалён.")
    else:
        bot.send_message(message.chat.id, f"{ident} не найден в списке админов.")

@bot.message_handler(commands=['listadmins'])
def cmd_listadmins(message):
    sender = message.from_user
    uid = str(sender.id)
    if not is_main_admin(uid) and not is_main_admin((sender.username or "").strip()):
        bot.send_message(message.chat.id, "Только главный админ может просматривать список админов.")
        return
    if not ordinary_admins:
        bot.send_message(message.chat.id, "Список обычных админов пуст.")
    else:
        bot.send_message(message.chat.id, "Обычные админы:\n" + "\n".join(ordinary_admins))

# ========== /mainadmin BOT COMMAND ==========
@bot.message_handler(commands=['mainadmin'])
def mainadmin_command(message):
    sender = message.from_user
    uid = str(sender.id)
    username = (sender.username or "").strip()
    is_admin = (uid in ADMIN_USER_IDS) or (username in ADMIN_USERNAMES)
    if not is_admin:
        bot.send_message(message.chat.id, "Доступ запрещён: эта команда только для главного админа.")
        return
    token = generate_admin_token(uid, username)
    url = f"{WEBAPP_URL}/mainadmin?token={quote_plus(token)}"
    kb = telebot.types.InlineKeyboardMarkup()
    kb.add(telebot.types.InlineKeyboardButton("Открыть админ-панель", url=url))
    bot.send_message(message.chat.id, "Откройте админ-панель (токен действует короткое время):", reply_markup=kb)

# ========== WEBAPP DATA HANDLER (requests from WebApp) ==========
@bot.message_handler(content_types=['web_app_data'])
def webapp_handler(message):
    try:
        data = json.loads(message.web_app_data.data)
    except Exception:
        bot.send_message(message.chat.id, "Неверные данные из WebApp")
        return

    user_id = message.from_user.id
    uid_str = str(user_id)
    action = data.get("action")

    # ensure user record exists
    get_user_record(user_id)

    # publish_task (создание задания работодателем)
    if action == "publish_task":
        # expected fields: title, link, type, budget
        title = data.get("title", "")[:200]
        link = data.get("link", "")[:1000]
        ttype = data.get("type", "")
        budget = int(data.get("budget", 0) or 0)
        task = {
            "id": f"task_{int(time.time()*1000)}",
            "title": title,
            "link": link,
            "type": ttype,
            "budget": budget,
            "owner": {"id": uid_str, "username": (message.from_user.username or ""), "first_name": (message.from_user.first_name or "")},
            "created_at": datetime.utcnow().isoformat() + "Z",
            "status": "active"
        }
        append_json(TASKS_FILE, task)
        bot.send_message(user_id, "Задание опубликовано!")
        return

    # submit_work — исполнитель отправляет текст/ссылку о выполнении задания
    if action == "submit_work":
        task_id = data.get("task_id")
        platform = data.get("platform")  # 'yandex' | 'google' | 'telegram'
        review_text = data.get("text", "")
        review_link = data.get("link", "")
        task = None
        tasks = load_json_safe(TASKS_FILE, [])
        for t in tasks:
            if t.get("id") == task_id and t.get("status") == "active":
                task = t
                break
        if not task:
            bot.send_message(user_id, "Задача не найдена или уже не активна.")
            return

        # rate limits
        last = users[uid_str].get("last_submissions", {})
        now_ts = int(time.time())
        if platform == "yandex":
            # 3 days = 3*24*3600
            prev = int(last.get("yandex", 0) or 0)
            if now_ts - prev < 3*24*3600:
                bot.send_message(user_id, "Можно оставлять отзывы на Яндекс не чаще, чем раз в 3 дня.")
                return
        elif platform == "google":
            prev = int(last.get("google", 0) or 0)
            if now_ts - prev < 24*3600:
                bot.send_message(user_id, "Можно оставлять Google отзывы не чаще, чем раз в 1 день.")
                return
        # telegram has no limit

        # create work record
        work = {
            "id": f"WKR_{int(time.time()*1000)}",
            "task_id": task_id,
            "task_title": task.get("title"),
            "platform": platform,
            "user": {"id": uid_str, "username": (message.from_user.username or ""), "first_name": (message.from_user.first_name or "")},
            "text": review_text,
            "link": review_link,
            "amount": task.get("budget", 0),
            "status": "pending",
            "created_at": datetime.utcnow().isoformat() + "Z"
        }
        append_json(WORKS_FILE, work)

        # update last submission timestamp (freeze allowance)
        if "last_submissions" not in users[uid_str]:
            users[uid_str]["last_submissions"] = {}
        users[uid_str]["last_submissions"][platform] = now_ts
        save_users()

        bot.send_message(user_id, "Заявка на проверку отправлена. Обычные админы проверят и примут/отклонят её.")
        # notify only ordinary admins
        try:
            notify_admins_work(work)
        except Exception as e:
            print("notify_admins_work error:", e)
        return

    # topup/withdraw handled previously; keep those flows
    if action == "request_topup":
        amount = int(data.get("amount", 0) or 0)
        code = data.get("code", "000000")
        if amount < 100:
            bot.send_message(user_id, "Минимальная сумма пополнения — 100 ₽!")
            return
        topup = {
            "id": f"T_{int(time.time()*1000)}",
            "user": {"id": uid_str, "username": (message.from_user.username or "")},
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
        return

    if action == "request_withdraw":
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
            "user": {"id": uid_str, "username": (message.from_user.username or "")},
            "amount": amount,
            "bank": bank,
            "name": data.get("name", ""),
            "card": data.get("card", ""),
            "status": "pending",
            "created_at": datetime.utcnow().isoformat() + "Z"
        }
        append_json(WITHDRAWS_FILE, withdraw)
        try:
            users[uid_str]["balance"] = max(0, users[uid_str].get("balance", 0) - amount)
            save_users()
        except Exception:
            pass
        bot.send_message(user_id, f"Заявка на вывод {amount} ₽ принята! Ожидает обработки админом.")
        try:
            notify_admins_withdraw(withdraw)
        except Exception as e:
            print("notify_admins_withdraw error:", e)
        return

    bot.send_message(user_id, "Неизвестное действие из WebApp")

def sender_username_safe(from_user):
    return getattr(from_user, "username", "") or getattr(from_user, "first_name", "") or ""

# ========== PUBLIC APIs ==========
@app.route('/api/tasks_public', methods=['GET'])
def api_tasks_public():
    tasks = load_json_safe(TASKS_FILE, [])
    # return only active tasks
    active = [t for t in tasks if t.get("status") == "active"]
    return jsonify(active)

@app.route('/api/works_pending', methods=['GET'])
def api_works_pending():
    # optionally used by admin panel; restricted to admins in real world — here we return all for mainadmin flow
    arr = load_json_safe(WORKS_FILE, [])
    return jsonify(arr)

# ========== ADMIN-PROTECTED ROUTES ==========
def get_token_from_request(req):
    t = req.args.get("token")
    if t:
        return t
    auth = req.headers.get("Authorization", "") or req.headers.get("authorization", "")
    if auth and auth.lower().startswith("bearer "):
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
        return "<h3>Доступ запрещён. Откройте панель только через телеграм-команду главного администратора.</h3>", 403
    return send_from_directory('public', 'mainadmin.html')

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
    data = load_json_safe(TASKS_FILE, [])
    return jsonify(data)

@app.route('/api/works', methods=['GET'])
@require_admin_token
def api_works():
    data = load_json_safe(WORKS_FILE, [])
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
            uidk = str(uid)
            if uidk not in users:
                users[uidk] = {"balance": 0, "tasks_done": 0, "total_earned": 0, "subscribed": False, "last_submissions": {}}
            users[uidk]["balance"] = users[uidk].get("balance", 0) + it.get("amount", 0)
            save_users()
            save_json(TOPUPS_FILE, arr)
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
                users[str(uid)]["balance"] = users[str(uid)].get("balance", 0) + it.get("amount", 0)
                save_users()
            except Exception:
                pass
            save_json(WITHDRAWS_FILE, arr)
            return jsonify({"ok": True})
    return jsonify({"ok": False, "reason": "not_found"}), 404

# approve work (admin approves a performed job)
@app.route('/api/works/<work_id>/approve', methods=['POST'])
@require_admin_token
def api_work_approve(work_id):
    arr = load_json_safe(WORKS_FILE, [])
    for it in arr:
        if it.get("id") == work_id:
            if it.get("status") == "paid":
                return jsonify({"ok": False, "reason": "already_paid"}), 400
            # mark paid
            it["status"] = "paid"
            it["handled_by"] = "admin"
            it["handled_at"] = datetime.utcnow().isoformat() + "Z"
            # credit user
            uid = int(it["user"]["id"])
            users.setdefault(str(uid), {"balance": 0, "tasks_done": 0, "total_earned": 0, "subscribed": False, "last_submissions": {}})
            users[str(uid)]["balance"] = users[str(uid)].get("balance", 0) + it.get("amount", 0)
            users[str(uid)]["tasks_done"] = users[str(uid)].get("tasks_done", 0) + 1
            users[str(uid)]["total_earned"] = users[str(uid)].get("total_earned", 0) + it.get("amount", 0)
            save_users()
            save_json(WORKS_FILE, arr)
            return jsonify({"ok": True})
    return jsonify({"ok": False, "reason": "not_found"}), 404

@app.route('/api/works/<work_id>/reject', methods=['POST'])
@require_admin_token
def api_work_reject(work_id):
    payload = request.get_json() or {}
    reason = payload.get("reason", "Отклонено администратором")
    arr = load_json_safe(WORKS_FILE, [])
    for it in arr:
        if it.get("id") == work_id:
            if it.get("status") in ("rejected", "paid"):
                return jsonify({"ok": False, "reason": "already_handled"}), 400
            it["status"] = "rejected"
            it["handled_by"] = "admin"
            it["handled_at"] = datetime.utcnow().isoformat() + "Z"
            it["reject_reason"] = reason
            # optionally: revert last_submissions allowance so user can retry sooner
            try:
                uid = int(it["user"]["id"])
                # don't refund money because it wasn't paid; allow next attempt by clearing last_submissions for that platform
                if "last_submissions" in users[str(uid)] and it.get("platform"):
                    users[str(uid)]["last_submissions"].pop(it.get("platform"), None)
                    save_users()
            except Exception:
                pass
            save_json(WORKS_FILE, arr)
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
