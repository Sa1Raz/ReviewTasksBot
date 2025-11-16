# app.py — 100% РАБОЧИЙ: БОТ + WEBAPP + СТАТИКА
from flask import Flask, request, send_from_directory
import telebot
import threading
import time
import os

BOT_TOKEN = "8033069276:AAFv1-kdQ68LjvLEgLHj3ZXd5ehMqyUXOYU"
WEBAPP_URL = "https://web-production-398fb.up.railway.app"

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__, static_folder='public')

# === СТАТИКА ===
@app.route('/')
def index():
    return send_from_directory('public', 'index.html')

@app.route('/<path:path>')
def static_files(path):
    return send_from_directory('public', path)

# === WEBHOOK ===
@app.route('/webhook', methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        update = telebot.types.Update.de_json(request.get_data().decode('utf-8'))
        bot.process_new_updates([update])
        return '', 200
    return 'Invalid', 403

# === КЛАВИАТУРА ===
def get_keyboard():
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    webapp = telebot.types.WebAppInfo(WEBAPP_URL)
    btn = telebot.types.KeyboardButton("ReviewCash", web_app=webapp)
    markup.add(btn)
    return markup

# === /start ===
@bot.message_handler(commands=['start'])
def start(message):
    bot.send_message(
        message.chat.id,
        "🚀 *ReviewCash ULTRA 4.0*\n\n"
        "💰 Зарабатывай на отзывах!\n"
        "👇 Нажми кнопку ниже",
        parse_mode='Markdown',
        reply_markup=get_keyboard()
    )

# === ЛЮБОЕ СООБЩЕНИЕ ===
@bot.message_handler(func=lambda m: True)
def echo(message):
    bot.reply_to(message, "Напиши /start", reply_markup=get_keyboard())

# === УСТАНОВКА WEBHOOK ===
def setup_webhook():
    time.sleep(3)
    bot.remove_webhook()
    time.sleep(1)
    success = bot.set_webhook(url=f"{WEBAPP_URL}/webhook")
    if success:
        print(f"WEBHOOK УСПЕШНО: {WEBAPP_URL}/webhook")
    else:
        print("ОШИБКА WEBHOOK!")

# === ЗАПУСК ===
if __name__ == '__main__':
    threading.Thread(target=setup_webhook, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
