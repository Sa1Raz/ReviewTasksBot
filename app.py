from flask import Flask, request, send_from_directory, jsonify
import telebot
import threading
import time
import os
import json

BOT_TOKEN = "8033069276:AAFv1-kdQ68LjvLEgLHj3ZXd5ehMqyUXOYU"
WEBAPP_URL = "https://web-production-398fb.up.railway.app"
CHANNEL_ID = "@ReviewCashNews"  # ← ТВОЙ КАНАЛ!

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__, static_folder='public')

# База данных в памяти
users = {}  # user_id: {balance, tasks_done, total_earned, subscribed}

# === АВТОПРОВЕРКА ПОДПИСКИ ===
def check_subscription(user_id):
    try:
        member = bot.get_chat_member(CHANNEL_ID, user_id)
        return member.status in ['member', 'administrator', 'creator']
    except Exception as e:
        print(f"Ошибка проверки подписки: {e}")
        return False

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

# === Клавиатура ===
def main_keyboard():
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    webapp = telebot.types.WebAppInfo(WEBAPP_URL)
    btn = telebot.types.KeyboardButton("ReviewCash", web_app=webapp)
    markup.add(btn)
    return markup

# === /start с автопроверкой ===
@bot.message_handler(commands=['start'])
def start(message):
    user_id = message.from_user.id
    if user_id not in users:
        users[user_id] = {"balance": 0, "tasks_done": 0, "total_earned": 0, "subscribed": False}

    # Автопроверка подписки
    if not check_subscription(user_id):
        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(telebot.types.InlineKeyboardButton("Подписаться на @ReviewCashNews", url="https://t.me/ReviewCashNews"))
        markup.add(telebot.types.InlineKeyboardButton("Проверить подписку", callback_data="check_sub"))
        bot.send_message(
            message.chat.id,
            "ReviewCash — зарабатывай на отзывах!\n\n"
            "Обязательно подпишись на канал:\n"
            "@ReviewCashNews\n\n"
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

# === Проверка подписки по кнопке ===
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

# === WebApp данные ===
@bot.message_handler(content_types=['web_app_data'])
def webapp_handler(message):
    data = json.loads(message.web_app_data.data)
    user_id = message.from_user.id
    action = data.get("action")

    if user_id not in users:
        users[user_id] = {"balance": 0, "tasks_done": 0, "total_earned": 0, "subscribed": True}

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
        bot.send_data(message.chat.id, json.dumps(response))

    elif action == "request_topup":
        amount = data.get("amount", 0)
        code = data.get("code", "000000")
        if amount < 50:
            bot.send_message(user_id, "Минимальная сумма пополнения — 50 ₽!")
            return
        bot.send_message(user_id, f"Заявка на {amount} ₽ принята!\nКод: `{code}`\nЗачислю в течение дня!", parse_mode="Markdown")

    elif action == "request_withdraw":
        amount = data.get("amount", 0)
        bank = data.get("bank", "").lower()
        valid_banks = ["т-банк", "тинькофф", "сбер", "сбербанк", "втб", "альфа", "альфа-банк", "райффайзен", "райф"]
        if not any(b in bank for b in valid_banks):
            bot.send_message(user_id, "Укажи настоящий банк: Т-Банк, Сбер, ВТБ, Альфа и т.д.")
            return
        bot.send_message(user_id, f"Заявка на вывод {amount} ₽ на {bank.title()} принята!\nСкоро переведу!")

# === Запуск ===
def setup_webhook():
    time.sleep(3)
    bot.remove_webhook()
    time.sleep(1)
    bot.set_webhook(url=f"{WEBAPP_URL}/webhook")

if __name__ == '__main__':
    threading.Thread(target=setup_webhook, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
