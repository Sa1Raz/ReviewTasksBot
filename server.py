# server.py — ВСЁ В ОДНОМ ФАЙЛЕ: FLASK + TELEGRAM БОТ + ВЕБХУК СБРОС

from flask import Flask, request, jsonify, send_from_directory
import sqlite3
from datetime import datetime
import os
import asyncio

# === TELEGRAM БОТ ===
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes

BOT_TOKEN = "8033069276:AAEvtPost3oicpvswzAPWesH_FMM0vIpiUo"
ADMIN_ID = 6482440657
WEBAPP_URL = "https://твой-проект.up.railway.app"  # ← ЗАМЕНИ НА СВОЮ ССЫЛКУ ПОСЛЕ ДЕПЛОЯ!

# Создаём приложение бота
bot_app = Application.builder().token(BOT_TOKEN).build()

# СБРАСЫВАЕМ ВЕБХУК ПРИ СТАРТЕ (убираем 409 Conflict)
async def reset_webhook():
    await bot_app.bot.delete_webhook(drop_pending_updates=True)
    print("Вебхук сброшен!")

# Команды
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("Открыть ReviewCash", web_app={"url": WEBAPP_URL})]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "Привет! Добро пожаловать в ReviewCash!\nНажми кнопку ниже:",
        reply_markup=reply_markup
    )

async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Доступ запрещён!")
        return
    keyboard = [[InlineKeyboardButton("Админка", web_app={"url": f"{WEBAPP_URL}/admin.html"})]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Админ-панель @RapiHappy", reply_markup=reply_markup)

# Добавляем команды
bot_app.add_handler(CommandHandler("start", start))
bot_app.add_handler(CommandHandler("admin", admin))

# === FLASK БЭКЕНД ===
app = Flask(__name__, static_folder='public')
os.makedirs('public', exist_ok=True)

def init_db():
    conn = sqlite3.connect('reviewcash.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER UNIQUE,
        username TEXT,
        balance INTEGER DEFAULT 0,
        created_at TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        title TEXT,
        link TEXT,
        type TEXT,
        reward INTEGER,
        created_at TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS topups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        username TEXT,
        amount INTEGER,
        code TEXT,
        status TEXT DEFAULT 'pending',
        created_at TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS withdraws (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        username TEXT,
        amount INTEGER,
        card TEXT,
        name TEXT,
        bank TEXT,
        status TEXT DEFAULT 'pending',
        created_at TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS proofs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        task_id INTEGER,
        proof_url TEXT,
        reward INTEGER,
        status TEXT DEFAULT 'pending',
        created_at TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS admins (
        user_id INTEGER PRIMARY KEY,
        username TEXT
    )''')
    c.execute("INSERT OR IGNORE INTO admins (user_id, username) VALUES (?, ?)", (777777777, 'RapiHappy'))
    conn.commit()
    conn.close()

init_db()

@app.route('/')
def index():
    return send_from_directory('public', 'index.html')

@app.route('/<path:path>')
def static_files(path):
    return send_from_directory('public', path)

@app.route('/webapp', methods=['POST'])
def webapp():
    data = request.json
    user = data.get('user', {})
    user_id = user.get('id')
    username = user.get('username', f'user_{user_id}')
    
    conn = sqlite3.connect('reviewcash.db')
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (user_id, username, created_at) VALUES (?, ?, ?)",
              (user_id, username, datetime.now().isoformat()))
    conn.commit()

    action = data.get('action')

    if action == 'get_tasks':
        c.execute("SELECT id, title, link, type, reward FROM tasks ORDER BY id DESC")
        tasks = [dict(zip(['id','title','link','type','reward'], row)) for row in c.fetchall()]
        c.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        balance = row[0] if row else 0
        conn.close()
        return jsonify({'tasks': tasks, 'user': {'balance': balance}})

    elif action == 'create_task':
        title, link, type_, reward = data['title'], data['link'], data['type'], data['reward']
        c.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        balance = row[0] if row else 0
        if balance < reward:
            conn.close()
            return jsonify({'error': 'Недостаточно средств'})
        c.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (reward, user_id))
        c.execute("INSERT INTO tasks (user_id, title, link, type, reward, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                  (user_id, title, link, type_, reward, datetime.now().isoformat()))
        conn.commit()
        conn.close()
        return jsonify({'success': True})

    elif action == 'request_topup':
        amount, code = data['amount'], data['code']
        c.execute("INSERT INTO topups (user_id, username, amount, code, created_at) VALUES (?, ?, ?, ?, ?)",
                  (user_id, username, amount, code, datetime.now().isoformat()))
        conn.commit()
        conn.close()
        return jsonify({'success': True})

    elif action == 'request_withdraw':
        amount = data['amount']
        c.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        balance = row[0] if row else 0
        if balance < amount:
            conn.close()
            return jsonify({'error': 'Недостаточно'})
        c.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (amount, user_id))
        c.execute("INSERT INTO withdraws (user_id, username, amount, card, name, bank, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                  (user_id, username, amount, data['card'], data['name'], data['bank'], datetime.now().isoformat()))
        conn.commit()
        conn.close()
        return jsonify({'success': True})

    elif action == 'admin_get_all':
        c.execute("SELECT * FROM admins WHERE user_id = ?", (user_id,))
        if not c.fetchone():
            conn.close()
            return jsonify({'error': 'Доступ запрещён'})
        result = {}
        c.execute("SELECT id, user_id, username, amount, code, status FROM topups ORDER BY id DESC")
        result['payments'] = [dict(zip(['id','user_id','username','amount','code','status'], r)) for r in c.fetchall()]
        c.execute("SELECT id, user_id, username, amount, card, name, bank, status FROM withdraws ORDER BY id DESC")
        result['withdraws'] = [dict(zip(['id','user_id','username','amount','card','name','bank','status'], r)) for r in c.fetchall()]
        c.execute("SELECT t.id, t.title, t.link, t.type, t.reward, u.username FROM tasks t LEFT JOIN users u ON t.user_id = u.user_id ORDER BY t.id DESC")
        result['tasks_admin'] = [dict(zip(['id','title','link','type','reward','username'], r)) for r in c.fetchall()]
        conn.close()
        return jsonify(result)

    elif action in ['admin_approve_payment', 'admin_reject_payment', 'admin_approve_withdraw', 'admin_reject_withdraw', 'admin_delete_task']:
        c.execute("SELECT * FROM admins WHERE user_id = ?", (user_id,))
        if not c.fetchone():
            conn.close()
            return jsonify({'error': 'Нет доступа'})
        if action == 'admin_approve_payment':
            c.execute("UPDATE topups SET status='approved' WHERE id=?", (data['id'],))
            c.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (data['amount'], data['user_id']))
        elif action == 'admin_reject_payment':
            c.execute("UPDATE topups SET status='rejected' WHERE id=?", (data['id'],))
        elif action == 'admin_approve_withdraw':
            c.execute("UPDATE withdraws SET status='paid' WHERE id=?", (data['id'],))
        elif action == 'admin_reject_withdraw':
            c.execute("UPDATE withdraws SET status='rejected' WHERE id=?", (data['id'],))
            c.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (data['amount'], data['user_id']))
        elif action == 'admin_delete_task':
            c.execute("DELETE FROM tasks WHERE id=?", (data['id'],))
        conn.commit()
        conn.close()
        return jsonify({'success': True})

    conn.close()
    return jsonify({'error': 'Unknown action'})

# === ЗАПУСК БОТА И FLASK В ОДНОМ ПОТОКЕ ===
if __name__ == '__main__':
    print("ReviewCash БЭКЕНД + БОТ ЗАПУЩЕН!")
    print(f"ОТКРЫВАЙ: {WEBAPP_URL}")

    # Сбрасываем вебхук и запускаем бота
    loop = asyncio.get_event_loop()
    loop.run_until_complete(reset_webhook())
    loop.create_task(bot_app.run_polling())

    # Запускаем Flask
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)), debug=False)

