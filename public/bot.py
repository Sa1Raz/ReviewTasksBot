# bot.py — Telegram бот с Webhook (БЕЗ ОШИБОК!)
from flask import Flask, request
import telebot
import requests
import os

# ← ВСТАВЬ СВОЙ ТОКЕН БОТА ЗДЕСЬ
BOT_TOKEN = "7706954432:AAH7J8rQ1Y8s2z0d9kL5vX3cP9mN2bF6hR1"
WEBAPP_URL = "https://web-production-398fb.up.railway.app"

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

@app.route('/')
def index():
    return "ReviewCash Бот работает!"

@app.route('/webhook', methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        update = telebot.types.Update.de_json(request.get_data().decode('utf-8'))
        bot.process_new_updates([update])
        return '', 200
    return 'Invalid', 403

@bot.message_handler(commands=['start'])
def start(message):
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    webapp = telebot.types.WebAppInfo(WEBAPP_URL)
    btn = telebot.types.KeyboardButton("ReviewCash", web_app=webapp)
    markup.add(btn)
    bot.send_message(
        message.chat.id,
        "🚀 *ReviewCash ULTRA 4.0*\n\n"
        "Зарабатывай на отзывах! 💰\n"
        "Создавай задания или выполняй!",
        parse_mode='Markdown',
        reply_markup=markup
    )

# === ЗАПУСК ===
if __name__ == '__main__':
    import threading
    import time

    def set_webhook():
        bot.remove_webhook()
        time.sleep(1)
        bot.set_webhook(url=f"{WEBAPP_URL}/webhook")
        print(f"Webhook установлен: {WEBAPP_URL}/webhook")

    threading.Thread(target=set_webhook).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
