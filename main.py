import os
import logging
import sqlite3
from sqlite3 import Error
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    CallbackQueryHandler,
    CallbackContext,
)
import aiohttp
from bs4 import BeautifulSoup
import datetime
import pytz
from cachetools import TTLCache
from dotenv import load_dotenv

load_dotenv()  # Загружаем переменные из .env

# Настройка логирования
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Конфигурация
TOKEN = os.getenv("BOT_TOKEN")
DATABASE = os.getenv("DATABASE", "bot_subscribers.db")

CACHE_TIMEOUT = 3600  # 1 час кэширования
CITIES = {
    'Москва': 'moscow',
    'Казань': 'kazan',
    'Уфа': 'ufa',
    'Санкт-Петербург': 'sankt-peterburg',
    'Астана': 'astana',
    'Алматы': 'almaty',
    'Актау': 'aktau',
    'Атырау': 'atyrau',
    'Караганда': 'karaganda',
    'Шымкент': 'shymkent',
    'Костанай': 'kostanay',
    'Уральск': 'uralsk',
}

CITY_TIMEZONES = {
    'moscow': 'Europe/Moscow',
    'kazan': 'Europe/Moscow',
    'ufa': 'Asia/Yekaterinburg',
    'sankt-peterburg': 'Europe/Moscow',
    'astana': 'Asia/Almaty',
    'almaty': 'Asia/Almaty',
    'aktau': 'Asia/Aqtau',
    'atyrau': 'Asia/Atyrau',
    'karaganda': 'Asia/Almaty',
    'shymkent': 'Asia/Almaty',
    'kostanay': 'Asia/Qostanay',
    'uralsk': 'Asia/Oral',
}

PRAYER_NAMES = ['Фаджр', 'Шурук', 'Зухр', 'Аср', 'Магриб', 'Иша']

# Глобальный кэш
cache = TTLCache(maxsize=100, ttl=CACHE_TIMEOUT)
daily_quotes = {'ayat': None}
sent_notifications = {}
sent_daily_schedules = {}


# Инициализация БД
def init_db():
    try:
        with sqlite3.connect(DATABASE) as conn:
            cursor = conn.cursor()
            cursor.execute('''CREATE TABLE IF NOT EXISTS subscribers
                             (chat_id INTEGER PRIMARY KEY,
                              city TEXT,
                              timezone TEXT,
                              subscribed INTEGER DEFAULT 1)''')
            conn.commit()
        logger.info("База данных успешно инициализирована")
    except Error as e:
        logger.error(f"Ошибка инициализации БД: {e}")


# Функции работы с БД
def get_subscribed_users():
    try:
        with sqlite3.connect(DATABASE) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT chat_id, city, timezone FROM subscribers WHERE subscribed = 1")
            users = [{'chat_id': row[0], 'city': row[1], 'tz': row[2]} for row in cursor.fetchall()]
            logger.info(f"Найдено {len(users)} подписанных пользователей")
            return users
    except Error as e:
        logger.error(f"Ошибка при получении подписчиков: {e}")
        return []


def update_user(chat_id: int, city: str, timezone: str):
    try:
        with sqlite3.connect(DATABASE) as conn:
            cursor = conn.cursor()
            cursor.execute('''INSERT OR REPLACE INTO subscribers 
                             (chat_id, city, timezone, subscribed) 
                             VALUES (?, ?, ?, 1)''', (chat_id, city, timezone))
            conn.commit()
        logger.info(f"Обновление данных о пользователе {chat_id}: город={city}, timezone={timezone}")
    except Error as e:
        logger.error(f"Ошибка при обновлении пользователя {chat_id}: {e}")


def unsubscribe_user(chat_id: int):
    try:
        with sqlite3.connect(DATABASE) as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE subscribers SET subscribed = 0 WHERE chat_id = ?", (chat_id,))
            conn.commit()
        logger.info(f"Пользователь {chat_id} отписан")
    except Error as e:
        logger.error(f"Ошибка при отписке пользователя {chat_id}: {e}")


# Парсинг данных
async def fetch_url(url: str) -> str:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers={'User-Agent': 'Mozilla/5.0'}) as response:
                return await response.text()
    except aiohttp.ClientError as e:
        logger.error(f"Ошибка при запросе {url}: {e}")
        return ""


async def parse_prayer_times(city: str, date: datetime.date) -> dict:
    cache_key = f"{city}_{date}"
    if cache_key in cache:
        return cache[cache_key]

    url = f"https://umma.ru/raspisanie-namaza/{city}"
    try:
        html = await fetch_url(url)
        if not html:
            logger.error(f"Не удалось загрузить страницу для {city}")
            return {}

        soup = BeautifulSoup(html, 'html.parser')
        table = soup.find('table', class_='PrayTimePage_table__wEx0t')
        if not table:
            logger.error(f"Таблица не найдена для {city}")
            return {}

        rows = table.find_all('tr')
        for i, row in enumerate(rows[:3]):
            cols = [col.text.strip() for col in row.find_all('td')]
            logger.debug(f"Строка {i} для {city}: {cols}")

        for row in rows:
            cols = [col.text.strip() for col in row.find_all('td')]
            if len(cols) >= 8 and cols[0] == str(date.day):
                schedule = {
                    'Фаджр': cols[2],
                    'Шурук': cols[3],
                    'Зухр': cols[4],
                    'Аср': cols[5],
                    'Магриб': cols[6],
                    'Иша': cols[7]
                }
                cache[cache_key] = schedule
                logger.info(f"Расписание для {city} на {date}: {schedule}")
                return schedule
        logger.warning(f"Расписание для {city} на день {date.day} не найдено")
        return {}
    except Exception as e:
        logger.error(f"Ошибка парсинга расписания для {city}: {e}")
        return {}


async def get_daily_quote(quote_type: str) -> dict:
    cache_key = f"{quote_type}_{datetime.date.today()}"
    if cache_key in cache:
        return cache[cache_key]

    urls = {'ayat': 'https://umma.ru/ayat-dnya', 'hadis': 'https://umma.ru/hadis-dnya'}
    try:
        html = await fetch_url(urls[quote_type])
        if not html:
            return {}
        soup = BeautifulSoup(html, 'html.parser')
        quote_block = soup.find('div', class_='DailyNews_dailyNewsText__5XStP')
        if not quote_block:
            logger.error(f"Блок цитаты не найден для {quote_type}")
            return {}
        link = quote_block.find('a').get('href')
        chapter = f"<a href='https://umma.ru{link}'><b>{quote_block.find('h3').text}</b></a>"
        quote_text = quote_block.find('p', class_='DailyNews_dailyNewsContent__Bq4aR').text
        text = f"{chapter}\n{quote_text.replace('Читать полностью', '').replace('[telegramline]', '')}"
        result = {'text': text}
        cache[cache_key] = result
        return result
    except Exception as e:
        logger.error(f"Ошибка получения {quote_type}: {e}")
        return {}


# Обновление данных
async def update_data(context=None):
    today = datetime.datetime.now(pytz.timezone('Europe/Moscow')).date()
    for city in CITIES.values():
        await parse_prayer_times(city, today)
    daily_quotes['ayat'] = await get_daily_quote('ayat')
    logger.info("Данные обновлены")


# Отправка ежедневного расписания перед Фаджр
async def send_daily_prayer_schedule(context: CallbackContext):
    logger.info("Проверка отправки ежедневного расписания")
    users = get_subscribed_users()
    if not users:
        logger.warning("Нет подписанных пользователей для ежедневного расписания")
        return

    now = datetime.datetime.now(pytz.utc)
    today = now.date()
    cutoff_date = now - datetime.timedelta(days=1)
    global sent_daily_schedules

    sent_daily_schedules = {
        k: v for k, v in sent_daily_schedules.items()
        if datetime.datetime.strptime(k.split('-')[0], '%Y-%m-%d') > cutoff_date
    }

    for user in users:
        try:
            city = user['city']
            tz = pytz.timezone(user['tz'])
            now = datetime.datetime.now(tz)
            current_time = now.strftime("%H:%M")
            schedule = await parse_prayer_times(city, today)
            if not schedule:
                logger.warning(f"Расписание для {city} пустое")
                continue

            fajr_time = schedule.get('Фаджр')
            if not fajr_time:
                logger.warning(f"Время Фаджр не найдено для {city}")
                continue

            fajr_minutes = sum(x * int(t) for x, t in zip([60, 1], fajr_time.split(":")))
            current_minutes = sum(x * int(t) for x, t in zip([60, 1], current_time.split(":")))
            time_diff = fajr_minutes - current_minutes

            schedule_id = f"{today.strftime('%Y-%m-%d')}-{user['chat_id']}"
            if time_diff == 10 and schedule_id not in sent_daily_schedules:
                schedule_text = "🕋 Расписание намазов на сегодня:\n" + "\n".join(
                    [f"• {name}: <b>{time}</b>" for name, time in schedule.items()]
                )
                await context.bot.send_message(
                    chat_id=user['chat_id'],
                    text=schedule_text,
                    parse_mode='HTML'
                )
                sent_daily_schedules[schedule_id] = True
                logger.info(f"Ежедневное расписание отправлено для {user['chat_id']} в {current_time}")
        except Exception as e:
            logger.error(f"Ошибка при отправке расписания для {user['chat_id']}: {e}")


# Отправка Аята дня в 8:00 утра
async def send_daily_quote(context: CallbackContext):
    logger.info("Отправка Аята дня")
    users = get_subscribed_users()
    if not users:
        logger.warning("Нет подписанных пользователей для Аята дня")
        return

    quote = await get_daily_quote('ayat')
    if not quote:
        logger.warning("Аят дня не доступен")
        return

    for user in users:
        try:
            chat_id = user['chat_id']
            tz = pytz.timezone(user['tz'])
            now = datetime.datetime.now(tz)
            current_time = now.strftime("%H:%M")
            if current_time == "08:00":  # Проверка на точное время 8:00
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"📖 Аят дня:\n{quote['text']}",
                    parse_mode='HTML'
                )
                logger.info(f"Аят дня отправлен для {chat_id} в 8:00")
        except Exception as e:
            logger.error(f"Ошибка при отправке Аята дня для {chat_id}: {e}")


# Обработчики Telegram
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [KeyboardButton("📅 Расписание на сегодня")],
        [KeyboardButton("📖 Аят дня")],
        [KeyboardButton("⚙️ Настройки")]
    ]
    user = update.effective_user
    await update.message.reply_html(
        f"Assalamu Alaikum, {user.mention_html()}! Я буду напоминать вам о времени намаза.\n"
        "Сначала выберите город:\n"
        "/set_city _- 🏠 Выбрать город вручную\n"
        "/status - ℹ️ Показать текущие настройки\n"
        "/daily_quote _- 📖 Аят дня\n"
        "/subscribe - ✅ Подписаться на уведомления\n"
        "/unsubscribe - ❌ Отписаться от уведомлений",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )


async def daily_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    try:
        with sqlite3.connect(DATABASE) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT city, timezone FROM subscribers WHERE chat_id = ?", (chat_id,))
            result = cursor.fetchone()
        if not result:
            await update.message.reply_text("❌ Сначала установите город с помощью /set_city")
            return
        city, tz = result
        today = datetime.datetime.now(pytz.timezone(tz)).date()
        schedule = await parse_prayer_times(city, today)
        if schedule:
            text = "🕋 Расписание на сегодня:\n" + "\n".join(
                [f"• <b>{time}</b> - {name}" for name, time in schedule.items()])
            await update.message.reply_text(text, parse_mode='HTML')
        else:
            await update.message.reply_text("❌ Расписание временно недоступно")
    except Exception as e:
        logger.error(f"Ошибка в daily_schedule для {chat_id}: {e}")
        await update.message.reply_text("❌ Произошла ошибка")


async def daily_quote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    quote = await get_daily_quote('ayat')
    if quote:
        await update.message.reply_text(f"📖 Аят дня:\n{quote['text']}", parse_mode='HTML')
    else:
        await update.message.reply_text("❌ Аят дня временно недоступен")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    try:
        with sqlite3.connect(DATABASE) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT city, timezone, subscribed FROM subscribers WHERE chat_id = ?", (chat_id,))
            result = cursor.fetchone()
        if result:
            city, tz, subscribed = result
            city_name = next((k for k, v in CITIES.items() if v == city), city)
            status_text = "✅ Подписан" if subscribed else "❌ Не подписан"
            await update.message.reply_text(
                f"🏠 Город: {city_name}\n⏰ Часовой пояс: {tz}\n📩 Статус: {status_text}"
            )
        else:
            await update.message.reply_text("❌ Настройки не найдены. Установите город с помощью /set_city")
    except Error as e:
        logger.error(f"Ошибка в status для {chat_id}: {e}")
        await update.message.reply_text("❌ Произошла ошибка")


async def get_city(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton(city, callback_data=f"set_city_{city}")] for city in CITIES]
    await update.message.reply_text("Выберите город:", reply_markup=InlineKeyboardMarkup(keyboard))


async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🏠 Изменить город", callback_data="change_city")],
        [InlineKeyboardButton("ℹ️ Текущие настройки", callback_data="show_settings")]
    ]
    commands_list = (
        "⚙️ *Настройки*\n"
        "Список доступных команд:\n"
        "/start - ℹ️ Начало работы с ботом\n"
        "/set_city _- 🏠 Выбрать город вручную\n"
        "/status - ℹ️ Показать текущие настройки\n"
        "/daily_quote _- 📖 Аят дня\n"
        "/subscribe - ✅ Подписаться на уведомления\n"
        "/unsubscribe - ❌ Отписаться от уведомлений\n\n"
        "Выберите действие ниже:"
    )
    await update.message.reply_text(commands_list, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')


async def set_city(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    city_name = query.data.split('_')[-1]
    city_code = CITIES[city_name]
    timezone = CITY_TIMEZONES[city_code]
    chat_id = query.message.chat_id
    update_user(chat_id, city_code, timezone)
    await query.answer(f"Город установлен: {city_name}")
    await query.edit_message_text(f"Город установлен: {city_name}")


async def handle_settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = query.message.chat_id
    data = query.data

    if data == "change_city":
        keyboard = [[InlineKeyboardButton(city, callback_data=f"set_city_{city}")] for city in CITIES]
        keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_to_settings")])
        await query.edit_message_text("Выберите город:", reply_markup=InlineKeyboardMarkup(keyboard))
    elif data == "show_settings":
        try:
            with sqlite3.connect(DATABASE) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT city, timezone, subscribed FROM subscribers WHERE chat_id = ?", (chat_id,))
                result = cursor.fetchone()
            if result:
                city, tz, subscribed = result
                city_name = next((k for k, v in CITIES.items() if v == city), city)
                status_text = "✅ Подписан" if subscribed else "❌ Не подписан"
                settings_text = f"🏠 Город: {city_name}\n⏰ Часовой пояс: {tz}\n📩 Статус: {status_text}"
            else:
                settings_text = "❌ Настройки не найдены. Установите город с помощью /set_city"
            await query.edit_message_text(settings_text)
        except Error as e:
            logger.error(f"Ошибка при показе настроек для {chat_id}: {e}")
            await query.edit_message_text("❌ Произошла ошибка")
    elif data == "back_to_settings":
        keyboard = [
            [InlineKeyboardButton("🏠 Изменить город", callback_data="change_city")],
            [InlineKeyboardButton("ℹ️ Текущие настройки", callback_data="show_settings")]
        ]
        commands_list = (
            "⚙️ *Настройки*\n"
            "Список доступных команд:\n"
            "/start - ℹ️ Начало работы с ботом\n"
            "/set_city _- 🏠 Выбрать город вручную\n"
            "/status - ℹ️ Показать текущие настройки\n"
            "/daily_quote _- 📖 Аят дня\n"
            "/subscribe - ✅ Подписаться на уведомления\n"
            "/unsubscribe - ❌ Отписаться от уведомлений\n\n"
            "Выберите действие ниже:"
        )
        await query.edit_message_text(commands_list, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

    await query.answer()


async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    try:
        with sqlite3.connect(DATABASE) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT city FROM subscribers WHERE chat_id = ?", (chat_id,))
            result = cursor.fetchone()
            if not result:
                await update.message.reply_text("❌ Сначала установите город с помощью /set_city")
                return
            cursor.execute("UPDATE subscribers SET subscribed = 1 WHERE chat_id = ?", (chat_id,))
            conn.commit()
        await update.message.reply_text("✅ Вы подписаны на уведомления!")
        logger.info(f"Пользователь {chat_id} подписан на уведомления")
    except Error as e:
        logger.error(f"Ошибка при подписке {chat_id}: {e}")
        await update.message.reply_text("❌ Произошла ошибка")


async def unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    unsubscribe_user(chat_id)
    await update.message.reply_text("❌ Вы отписались от уведомлений!")


async def check_prayer_times(context: CallbackContext):
    logger.info("Запуск проверки времени намаза")
    users = get_subscribed_users()
    if not users:
        logger.warning("Нет подписанных пользователей")
        return

    now = datetime.datetime.now(pytz.utc)
    cutoff_date = now - datetime.timedelta(days=1)
    global sent_notifications

    cleaned_notifications = {}
    for k, v in sent_notifications.items():
        try:
            date_part = k.split('-')[0]
            if len(date_part) == 10 and date_part[4] == '-' and date_part[7] == '-':
                if datetime.datetime.strptime(date_part, '%Y-%m-%d') > cutoff_date:
                    cleaned_notifications[k] = v
            else:
                logger.warning(f"Удален некорректный ключ уведомления: {k}")
        except ValueError as e:
            logger.error(f"Ошибка обработки ключа {k}: {e}")
    sent_notifications = cleaned_notifications

    for user in users:
        try:
            city = user['city']
            tz = pytz.timezone(user['tz'])
            now = datetime.datetime.now(tz)
            today = now.date()
            current_time = now.strftime("%H:%M")
            logger.info(f"Проверка для {user['chat_id']}: текущее время {current_time}")

            schedule = await parse_prayer_times(city, today)
            if not schedule:
                logger.warning(f"Расписание для {city} пустое")
                continue

            logger.debug(f"Расписание для {city}: {schedule}")
            for prayer, time in schedule.items():
                prayer_minutes = sum(x * int(t) for x, t in zip([60, 1], time.split(":")))
                current_minutes = sum(x * int(t) for x, t in zip([60, 1], current_time.split(":")))
                time_diff = prayer_minutes - current_minutes

                notification_id = f"{today.strftime('%Y-%m-%d')}-{prayer}-{user['chat_id']}"
                if time_diff == 0 and notification_id not in sent_notifications:
                    await context.bot.send_message(
                        chat_id=user['chat_id'],
                        text=f"🕌 Время <u>{prayer}</u> намаза: <b>{time}</b>",
                        parse_mode='HTML'
                    )
                    sent_notifications[notification_id] = True
                    logger.info(f"Уведомление о {prayer} отправлено для {user['chat_id']} в {time}")
        except Exception as e:
            logger.error(f"Ошибка для пользователя {user['chat_id']}: {e}")


async def post_init(application: Application):
    global sent_notifications, sent_daily_schedules
    sent_notifications = {}
    sent_daily_schedules = {}
    await update_data()
    logger.info("Данные успешно загружены при старте")


def main():
    init_db()
    try:
        application = (
            Application.builder()
            .token("8090552462:AAE3m8cIvTZwkkBjHL5PCAA4Iv1rztl5PsU")
            .post_init(post_init)
            .build()
        )

        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("set_city", get_city))
        application.add_handler(CommandHandler("status", status))
        application.add_handler(CommandHandler("daily_quote", daily_quote))
        application.add_handler(CommandHandler("subscribe", subscribe))
        application.add_handler(CommandHandler("unsubscribe", unsubscribe))
        application.add_handler(MessageHandler(filters.Text("📅 Расписание на сегодня"), daily_schedule))
        application.add_handler(MessageHandler(filters.Text("📖 Аят дня"), daily_quote))
        application.add_handler(MessageHandler(filters.Text("⚙️ Настройки"), settings))

        application.add_handler(CallbackQueryHandler(set_city, pattern="^set_city_"))
        application.add_handler(
            CallbackQueryHandler(handle_settings_callback, pattern="^(change_city|show_settings|back_to_settings)$"))

        job_queue = application.job_queue
        if job_queue:
            job_queue.run_repeating(check_prayer_times, interval=60)
            job_queue.run_repeating(send_daily_prayer_schedule, interval=60)
            job_queue.run_repeating(send_daily_quote, interval=60)  # Проверка каждые 60 секунд для 8:00
            job_queue.run_daily(update_data, time=datetime.time(1, 0, 0, tzinfo=pytz.timezone('Europe/Moscow')))

        application.run_polling()
    except Exception as e:
        logger.error(f"Ошибка запуска бота: {e}")


if __name__ == "__main__":
    main()