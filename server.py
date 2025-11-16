# server.py — ПОЛНЫЙ БЭКЕНД ДЛЯ ReviewCash ULTRA 4.0
# Поддержка: index.html, admin.html, API, база данных

from flask import Flask, request, jsonify, send_from_directory
import sqlite3
from datetime import datetime
import os

app = Flask(__name__, static_folder='public')
os.makedirs('public', exist_ok=True)

# === БАЗА ДАННЫХ ===
def init_db():
    conn = sqlite3.connect('reviewcash.db')
    c = conn.cursor()
    
    # Пользователи
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER UNIQUE,
        username TEXT,
        balance INTEGER DEFAULT 0,
        created_at TEXT
    )''')
    
    # Задания
    c.execute('''CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        title TEXT,
        link TEXT,
        type TEXT,
        reward INTEGER,
        created_at TEXT
    )''')
    
    # Пополнения
    c.execute('''CREATE TABLE IF NOT EXISTS topups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        username TEXT,
        amount INTEGER,
        code TEXT,
        status TEXT DEFAULT 'pending',
        created_at TEXT
    )''')
    
    # Выводы
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
    
    # Доказательства (скрины)
    c.execute('''CREATE TABLE IF NOT EXISTS proofs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        task_id INTEGER,
        proof_url TEXT,
        reward INTEGER,
        status TEXT DEFAULT 'pending',
        created_at TEXT
    )''')
    
    # Админы
    c.execute('''CREATE TABLE IF NOT EXISTS admins (
        user_id INTEGER PRIMARY KEY,
        username TEXT
    )''')
    c.execute("INSERT OR IGNORE INTO admins (user_id, username) VALUES (?, ?)", (6482440657, 'RapiHappy'))
    
    conn.commit()
    conn.close()

init_db()

# === СТАТИКА ===
@app.route('/')
def index():
    return send_from_directory('public', 'index.html')

@app.route('/<path:path>')
def static_files(path):
    return send_from_directory('public', path)

# === API ===
@app.route('/webapp', methods=['POST'])
def webapp():
    try:
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

        # === ГЛАВНАЯ СТРАНИЦА: Получить задания + баланс ===
        if action == 'get_tasks':
            c.execute("SELECT id, title, link, type, reward FROM tasks ORDER BY id DESC")
            tasks = [dict(zip(['id','title','link','type','reward'], row)) for row in c.fetchall()]
            c.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
            row = c.fetchone()
            balance = row[0] if row else 0
            conn.close()
            return jsonify({'tasks': tasks, 'user': {'balance': balance}})

        # === АДМИНКА: Полная статистика + все данные ===
        elif action == 'admin_get_all':
            if user_id != 6482440657:
                conn.close()
                return jsonify({'error': 'Доступ запрещён'}), 403

            # Статистика
            c.execute("SELECT COUNT(*) FROM users")
            users = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM tasks")
            tasks = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM proofs WHERE status = 'pending'")
            pending = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM withdraws WHERE status = 'pending'")
            withdraw_pending = c.fetchone()[0]

            # Пополнения
            c.execute("SELECT id, username, amount, code, status, created_at FROM topups ORDER BY id DESC")
            topups = [dict(zip(['id','username','amount','code','status','created_at'], row)) for row in c.fetchall()]

            # Выводы
            c.execute("SELECT id, username, amount, card, name, bank, status, created_at FROM withdraws ORDER BY id DESC")
            withdraws = [dict(zip(['id','username','amount','card','name','bank','status','created_at'], row)) for row in c.fetchall()]

            # Задания
            c.execute("SELECT id, title, link, type, reward, created_at FROM tasks ORDER BY id DESC")
            tasks_admin = [dict(zip(['id','title','link','type','reward','created_at'], row)) for row in c.fetchall()]

            conn.close()
            return jsonify({
                'stats': {
                    'users': users,
                    'tasks': tasks,
                    'pending': pending,
                    'withdraw': withdraw_pending
                },
                'payments': topups,
                'withdraws': withdraws,
                'tasks_admin': tasks_admin
            })

        # === СОЗДАНИЕ ЗАДАНИЯ ===
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

        # === ПОПОЛНЕНИЕ ===
        elif action == 'request_topup':
            amount, code = data['amount'], data['code']
            c.execute("INSERT INTO topups (user_id, username, amount, code, created_at) VALUES (?, ?, ?, ?, ?)",
                      (user_id, username, amount, code, datetime.now().isoformat()))
            conn.commit()
            conn.close()
            return jsonify({'success': True})

        # === ВЫВОД ===
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

        # === НЕИЗВЕСТНОЕ ДЕЙСТВИЕ ===
        conn.close()
        return jsonify({'error': 'Unknown action'})

    except Exception as e:
        return jsonify({'error': str(e)})

# === ЗАПУСК ===
if __name__ == '__main__':
    print("ReviewCash БЭКЕНД ЗАПУЩЕН! (БОТ ОТКЛЮЧЁН)")
    print("ОТКРЫВАЙ: https://web-production-398fb.up.railway.app")
    print("АДМИНКА: https://web-production-398fb.up.railway.app/admin.html")
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
