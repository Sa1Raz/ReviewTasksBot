# bot.py — ПОЛНАЯ ПОДДЕРЖКА: /start, /help, WebApp, Webhook
from flask import Flask, request
import telebot
import os

# ← ВСТАВЬ СВОЙ ТОКЕН ЗДЕСЬ
BOT_TOKEN = "7706954432:AAH7J8rQ1Y8s2z0d9kL5vX3cP9mN2bF6hR1"
WEBAPP_URL = "https://web-production-398fb.up.railway.app"

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

# === ГЛАВНАЯ СТРАНИЦА ===
@app.route('/')
def index():
    return "ReviewCash Бот работает 24/7!"

# === WEBHOOK ===
@app.route('/webhook', methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        update = telebot.types.Update.de_json(request.get_data().decode('utf-8'))
        bot.process_new_updates([update])
        return '', 200
    return 'Invalid', 403

# === КЛАВИАТУРА С WEBAPP ===
def main_keyboard():
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    webapp = telebot.types.WebAppInfo(WEBAPP_URL)
    btn = telebot.types.KeyboardButton("ReviewCash", web_app=webapp)
    markup.add(btn)
    return markup

# === КОМАНДА /start ===
@bot.message_handler(commands=['start'])
def start(message):
    bot.send_message(
        message.chat.id,
        "🚀 *ReviewCash ULTRA 4.0*\n\n"
        "💰 Зарабатывай на отзывах!\n"
        "👉 Создавай задания или выполняй\n\n"
        "Нажми кнопку ниже ↓",
        parse_mode='Markdown',
        reply_markup=main_keyboard()
    )

# === КОМАНДА /help ===
@bot.message_handler(commands=['help'])
def help_cmd(message):
    bot.send_message(
        message.chat.id,
        "📖 *Помощь*\n\n"
        "/start — Главное меню\n"
        "/profile — Твой профиль\n"
        "/admin — Админка (только для @RapiHappy)\n\n"
        "👇 Нажми *ReviewCash* ниже!",
        parse_mode='Markdown',
        reply_markup=main_keyboard()
    )

# === КОМАНДА /profile ===
@bot.message_handler(commands=['profile'])
def profile(message):
    bot.send_message(
        message.chat.id,
        f"👤 *Твой профиль*\n\n"
        f"ID: `{message.from_user.id}`\n"
        f"Имя: {message.from_user.first_name}\n"
        f"Баланс: 0 ₽\n\n"
        "👇 Открой приложение →",
        parse_mode='Markdown',
        reply_markup=main_keyboard()
    )

# === КОМАНДА /admin (ТОЛЬКО ДЛЯ ТЕБЯ) ===
@bot.message_handler(commands=['admin'])
def admin_cmd(message):
    if message.from_user.id != 6482440657:
        bot.reply_to(message, "🚫 Доступ запрещён!")
        return
    markup = telebot.types.InlineKeyboardMarkup()
    btn = telebot.types.InlineKeyboardButton("Открыть Админку", url=f"{WEBAPP_URL}/admin.html")
    markup.add(btn)
    bot.send_message(
        message.chat.id,
        "🔐 *Админка ULTRA 4.0*\n\n"
        "Управляй всем миром ReviewCash!",
        parse_mode='Markdown',
        reply_markup=markup
    )

# === ЛЮБОЕ СООБЩЕНИЕ ===
@bot.message_handler(func=lambda message: True)
def echo(message):
    bot.reply_to(message, "Напиши /start", reply_markup=main_keyboard())

# === ЗАПУСК ===
if __name__ == '__main__':
    import threading
    import time

    def set_webhook():
        bot.remove_webhook()
        time.sleep(1)
        result = bot.set_webhook(url=f"{WEBAPP_URL}/webhook")
        if result:
            print(f"Webhook УСПЕШНО установлен: {WEBAPP_URL}/webhook")
        else:
            print("ОШИБКА установки webhook")

    threading.Thread(target=set_webhook).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
