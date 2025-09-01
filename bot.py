# bot.py
import logging
import sqlite3
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram import Update
import ccxt
import os
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# === НАСТРОЙКИ ===
TOKEN = os.getenv("TELEGRAM_TOKEN")
THRESHOLD = 1.5  # Минимальная разница в %
symbols = ['BTC/USDT', 'ETH/USDT']

# Биржи
exchanges = {
    'binance': ccxt.binance(),
    'bybit': ccxt.bybit(),
    'kucoin': ccxt.kucoin()
}

# Подключение к БД
def init_db():
    conn = sqlite3.connect('/tmp/users.db')
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            is_premium INTEGER DEFAULT 0,
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

def add_user(user_id, username):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)', (user_id, username))
    conn.commit()
    conn.close()

def is_premium(user_id):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('SELECT is_premium FROM users WHERE user_id = ?', (user_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else False

def set_premium(user_id):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('UPDATE users SET is_premium = 1 WHERE user_id = ?', (user_id,))
    conn.commit()
    conn.close()

# === ПЛАНИРОВЩИК АЛЕРТОВ ===
scheduler = AsyncIOScheduler()

async def check_arbitrage(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    user_id = job.user_id
    if not is_premium(user_id):
        return  # Только премиум получают алерты (Free — не получают в этом примере)

    for symbol in symbols:
        prices = {}
        for name, exchange in exchanges.items():
            try:
                ticker = exchange.fetch_ticker(symbol)
                prices[name] = ticker['last']
            except Exception as e:
                logger.error(f"{name}: {e}")

        if len(prices) < 2:
            continue

        min_price = min(prices.values())
        max_price = max(prices.values())
        spread = (max_price - min_price) / min_price * 100

        if spread > THRESHOLD:
            min_ex = [k for k, v in prices.items() if v == min_price][0]
            max_ex = [k for k, v in prices.items() if v == max_price][0]

            message = f"""
🚨 **АРБИТРАЖ** ({symbol})
📉 {min_ex}: ${min_price:,.2f}
📈 {max_ex}: ${max_price:,.2f}
📊 Разница: **{spread:.2f}%**
🔗 [Buy on {min_ex}]({exchanges[min_ex].urls['www']})
            """
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=message,
                    parse_mode='Markdown'
                )
            except Exception as e:
                logger.error(f"Не удалось отправить пользователю {user_id}: {e}")
                scheduler.remove_job(f"job_{user_id}")

# === КОМАНДЫ ===
async def start(update: Update, context):
    user = update.effective_user
    add_user(user.id, user.username)
    await update.message.reply_text(
        f"👋 Привет, {user.first_name}!\n\n"
        "Я — **ArbHunterBot**. Слежу за ценами на криптовалюты и присылаю сигналы арбитража.\n\n"
        "🔹 /prices — текущие цены\n"
        "🔸 /subscribe — как стать Premium\n"
        "💎 /premium — активировать Premium (временно бесплатно)"
    )

async def prices(update: Update, context):
    user_id = update.effective_user.id
    reply = "📊 Текущие цены:\n\n"
    for symbol in symbols:
        reply += f"**{symbol}**\n"
        for name, exchange in exchanges.items():
            try:
                price = exchange.fetch_ticker(symbol)['last']
                reply += f"  {name}: ${price:,.2f}\n"
            except:
                reply += f"  {name}: ошибка\n"
        reply += "\n"
    await update.message.reply_text(reply, parse_mode='Markdown')

async def subscribe(update: Update, context):
    text = """
💎 **Premium подписка** — $9.99/мес

✅ Алерты каждые 15 секунд  
✅ Все монеты и биржи  
✅ Приоритетная обработка  
✅ API доступ (скоро)

👉 Пока что активация временно **бесплатна** через /premium
    """
    await update.message.reply_text(text, parse_mode='Markdown')

async def premium(update: Update, context):
    user_id = update.effective_user.id
    set_premium(user_id)

    # Удаляем старую задачу (если есть)
    job_name = f"job_{user_id}"
    if context.job_queue.get_jobs_by_name(job_name):
        for job in context.job_queue.get_jobs_by_name(job_name):
            job.schedule_removal()

    # Добавляем задачу с интервалом 15 сек (только для Premium)
    context.job_queue.run_repeating(
        check_arbitrage,
        interval=15,
        first=10,
        name=job_name,
        user_id=user_id
    )

    await update.message.reply_text(
        "🎉 Поздравляю! Ты — **Premium пользователь**!\n"
        "Теперь ты будешь получать алерты каждые 15 секунд.\n\n"
        "Чтобы отключить — перезапусти бота или напиши /stop"
    )

async def stop(update: Update, context):
    user_id = update.effective_user.id
    job_name = f"job_{user_id}"
    jobs = context.job_queue.get_jobs_by_name(job_name)
    for job in jobs:
        job.schedule_removal()
    await update.message.reply_text("🛑 Алерты остановлены.")

# === ЗАПУСК ===
def main():
    init_db()

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("prices", prices))
    app.add_handler(CommandHandler("subscribe", subscribe))
    app.add_handler(CommandHandler("premium", premium))
    app.add_handler(CommandHandler("stop", stop))

    scheduler.start()
    logger.info("Бот запущен и работает...")

    app.run_polling()

if __name__ == '__main__':
    main()
