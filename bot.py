# bot.py
import logging
import sqlite3
from telegram.ext import Application, CommandHandler, ContextTypes, JobQueue
from telegram import Update
import ccxt
import os
from contextlib import contextmanager

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# === НАСТРОЙКИ ===
TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TOKEN:
    raise ValueError("Требуется переменная окружения TELEGRAM_TOKEN")

THRESHOLD = 1.5  # Минимальная разница в %
symbols = ['BTC/USDT', 'ETH/USDT']

# Биржи
exchanges = {
    'binance': ccxt.binance(),
    'bybit': ccxt.bybit(),
    'kucoin': ccxt.kucoin(),
}

# Путь к БД (Render позволяет писать только в /tmp)
DB_PATH = '/tmp/users.db'

# === КОНТЕКСТНЫЙ МЕНЕДЖЕР ДЛЯ БД ===
@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        yield conn
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

# === РАБОТА С БАЗОЙ ДАННЫХ ===
def init_db():
    try:
        with get_db() as conn:
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
            logger.info("✅ База данных инициализирована")
    except Exception as e:
        logger.error(f"❌ Ошибка при инициализации БД: {e}")

def add_user(user_id: int, username: str):
    try:
        with get_db() as conn:
            c = conn.cursor()
            c.execute('''
                INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)
            ''', (user_id, username))
            conn.commit()
    except Exception as e:
        logger.error(f"❌ Ошибка добавления пользователя {user_id}: {e}")

def is_premium(user_id: int) -> bool:
    try:
        with get_db() as conn:
            c = conn.cursor()
            c.execute('SELECT is_premium FROM users WHERE user_id = ?', (user_id,))
            row = c.fetchone()
            return bool(row[0]) if row else False
    except Exception as e:
        logger.error(f"❌ Ошибка проверки премиума для {user_id}: {e}")
        return False

def set_premium(user_id: int):
    try:
        with get_db() as conn:
            c = conn.cursor()
            c.execute('UPDATE users SET is_premium = 1 WHERE user_id = ?', (user_id,))
            conn.commit()
            logger.info(f"🎉 Пользователь {user_id} стал Premium")
    except Exception as e:
        logger.error(f"❌ Ошибка при установке Premium для {user_id}: {e}")

# === АЛЕРТ: АРБИТРАЖ ===
async def check_arbitrage(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    user_id = job.user_id

    # Проверяем, что пользователь всё ещё премиум
    if not is_premium(user_id):
        logger.info(f"🚫 Пользователь {user_id} больше не Premium — останавливаем алерты")
        context.job.schedule_removal()
        return

    for symbol in symbols:
        prices = {}
        for name, exchange in exchanges.items():
            try:
                ticker = exchange.fetch_ticker(symbol)
                if ticker and 'last' in ticker:
                    prices[name] = ticker['last']
            except Exception as e:
                logger.warning(f"⚠️ Не удалось получить цену с {name}: {e}")

        if len(prices) < 2:
            continue

        min_price = min(prices.values())
        max_price = max(prices.values())
        spread = (max_price - min_price) / min_price * 100

        if spread > THRESHOLD:
            min_ex = [k for k, v in prices.items() if v == min_price][0]
            max_ex = [k for k, v in prices.items() if v == max_price][0]

            message = f"""
🚨 **АРБИТРАЖ ОБНАРУЖЕН** ({symbol})
📉 Дешевле: {min_ex.upper()} — ${min_price:,.2f}
📈 Дороже: {max_ex.upper()} — ${max_price:,.2f}
📊 Разница: **{spread:.2f}%**
🕒 {context.job.next_t.strftime('%H:%M:%S')}
🔗 [Купить на {min_ex}]({exchanges[min_ex].urls['www']})
            """
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=message.strip(),
                    parse_mode='Markdown',
                    disable_web_page_preview=False
                )
                logger.info(f"✅ Алерт отправлен {user_id}: {spread:.2f}% на {symbol}")
            except Exception as e:
                logger.error(f"❌ Не удалось отправить {user_id}: {e}")
                if "blocked" in str(e).lower() or "kicked" in str(e).lower():
                    context.job.schedule_removal()

# === КОМАНДЫ ===
async def start(update: Update, context):
    user = update.effective_user
    add_user(user.id, user.username)
    await update.message.reply_text(
        f"👋 Привет, {user.first_name}!\n\n"
        "Я — **ArbHunterBot**. Слежу за ценами на криптовалюты и присылаю сигналы арбитража.\n\n"
        "🔹 /prices — текущие цены\n"
        "🔸 /subscribe — тарифы\n"
        "💎 /premium — активировать Premium (временно бесплатно)"
    )

async def prices(update: Update, context):
    reply = "📊 Текущие цены:\n\n"
    for symbol in symbols:
        reply += f"**{symbol}**\n"
        for name, exchange in exchanges.items():
            try:
                price = exchange.fetch_ticker(symbol)['last']
                reply += f"  {name}: ${price:,.2f}\n"
            except Exception as e:
                reply += f"  {name}: ошибка\n"
        reply += "\n"
    await update.message.reply_text(reply.strip(), parse_mode='Markdown')

async def subscribe(update: Update, context):
    text = """
💎 **Premium подписка** — $9.99/мес

✅ Алерты каждые 15 секунд  
✅ Все монеты и биржи  
✅ Приоритетная обработка  
✅ API доступ (скоро)

👉 Пока что активация временно **бесплатна** через /premium
    """
    await update.message.reply_text(text.strip(), parse_mode='Markdown')

async def premium(update: Update, context):
    user_id = update.effective_user.id

    # Удаляем старые задачи
    job_name = f"arb_alert_{user_id}"
    current_jobs = context.job_queue.get_jobs_by_name(job_name)
    for job in current_jobs:
        job.schedule_removal()

    # Даём премиум
    set_premium(user_id)

    # Запускаем алерты
    context.job_queue.run_repeating(
        check_arbitrage,
        interval=15,
        first=5,
        name=job_name,
        user_id=user_id
    )

    await update.message.reply_text(
        "🎉 Поздравляю! Ты — **Premium пользователь**!\n"
        "Теперь ты будешь получать алерты каждые 15 секунд.\n\n"
        "Чтобы отключить — напиши /stop"
    )

async def stop(update: Update, context):
    user_id = update.effective_user.id
    job_name = f"arb_alert_{user_id}"
    jobs = context.job_queue.get_jobs_by_name(job_name)
    for job in jobs:
        job.schedule_removal()
    await update.message.reply_text("🛑 Алерты остановлены.")

# === ЗАПУСК ===
def main():
    # Инициализация БД
    init_db()

    # Создаём приложение
    try:
        app = Application.builder().token(TOKEN).build()
        logger.info("🤖 Бот: токен загружен")
    except Exception as e:
        logger.critical(f"❌ Ошибка токена: {e}")
        raise

    # Хэндлеры
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("prices", prices))
    app.add_handler(CommandHandler("subscribe", subscribe))
    app.add_handler(CommandHandler("premium", premium))
    app.add_handler(CommandHandler("stop", stop))

    # Запуск
    logger.info("🚀 Бот запускается...")
    app.run_polling()

if __name__ == '__main__':
    main()
