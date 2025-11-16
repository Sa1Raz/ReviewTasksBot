from flask import Flask, request, jsonify, send_from_directory
import sqlite3
from datetime import datetime
import os

# === TELEGRAM БОТ ===
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes

BOT_TOKEN = "8033069276:AAF-3WIgsW9iL2dnG3cs7_Gh16z5SuajvkA"
ADMIN_ID = 6482440657
WEBAPP_URL = "https://твой-проект.up.railway.app"  # ← ЗАМЕНИ НА СВОЮ ССЫЛКУ!

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("Открыть ReviewCash", web_app={"url": WEBAPP_URL})]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Привет! Добро пожаловать в ReviewCash!\nНажми кнопку ниже:", reply_markup=reply_markup)

async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Доступ запрещён!")
        return
    keyboard = [[InlineKeyboardButton("Админка", web_app={"url": f"{WEBAPP_URL}/admin.html"})]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Админ-панель @RapiHappy", reply_markup=reply_markup)

# === FLASK БЭКЕНД ===
app = Flask(__name__, static_folder='public')
os.makedirs('public', exist_ok=True)

def init_db():
    conn = sqlite3.connect('reviewcash.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER UNIQUE, username TEXT, balance INTEGER DEFAULT 0, created_at TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, title TEXT, link TEXT, type TEXT, reward INTEGER, created_at TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS topups (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, username TEXT, amount INTEGER, code TEXT, status TEXT DEFAULT 'pending', created_at TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS withdraws (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, username TEXT, amount INTEGER, card TEXT, name TEXT, bank TEXT, status TEXT DEFAULT 'pending', created_at TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS proofs (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, task_id INTEGER, proof_url TEXT, reward INTEGER, status TEXT DEFAULT 'pending', created_at TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY, username TEXT)''')
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
    c.execute("INSERT OR IGNORE INTO users (user_id, username, created_at) VALUES (?, ?, ?)", (user_id, username, datetime.now().isoformat()))
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
    # ... (остальные действия без изменений)
    # ВСТАВЬ СЮДА ВЕСЬ ТВОЙ КОД ИЗ ПРЕДЫДУЩЕГО server.py
    conn.close()
    return jsonify({'error': 'Unknown action'})

# === ЗАПУСК БОТА И FLASK В ОДНОМ ПОТОКЕ ===
if __name__ == '__main__':
    print("ReviewCash БЭКЕНД + БОТ ЗАПУЩЕН!")
    print(f"ОТКРЫВАЙ: {WEBAPP_URL}")

    # Создаём и запускаем бота
    bot_app = Application.builder().token(BOT_TOKEN).build()
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CommandHandler("admin", admin))

    # Запускаем бота в фоне
    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.create_task(bot_app.run_polling())
    
    # Запускаем Flask
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)), debug=False)
