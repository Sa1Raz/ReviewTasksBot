# bot.py — ОБНОВЛЁННЫЙ КОД (ТОЛЬКО HTTPS!)
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes

BOT_TOKEN = "8033069276:AAF-3WIgsW9iL2dnG3cs7_Gh16z5SuajvkA"
ADMIN_ID = 6482440657

# HTTPS ССЫЛКА — ИЗМЕНИ НА СВОЮ С Railway!
WEBAPP_URL = "http://127.0.0.1:5000"  # ← ТВОЯ ССЫЛКА!

logging.basicConfig(level=logging.INFO)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("ОТКРЫТЬ ПРИЛОЖЕНИЕ", web_app={"url": WEBAPP_URL})]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "Привет! Добро пожаловать в ReviewCash!\nНажми кнопку ниже:",
        reply_markup=reply_markup
    )

async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Доступ запрещён.")
        return
    keyboard = [[InlineKeyboardButton("АДМИНКА", web_app={"url": f"{WEBAPP_URL}/admin.html"})]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("АДМИНКА @RapiHappy", reply_markup=reply_markup)

app = Application.builder().token(BOT_TOKEN).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("admin", admin))

print("БОТ ЗАПУЩЕН! /start — ПРИЛОЖЕНИЕ | /admin — АДМИНКА")
app.run_polling()


