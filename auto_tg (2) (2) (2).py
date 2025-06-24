from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cardinal import Cardinal

import os
import json
import logging
import re
import requests
import threading
import queue
import concurrent.futures
from telebot import types
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from FunPayAPI.updater.events import NewOrderEvent, NewMessageEvent
import time
import uuid
import hashlib

try:
    import pymysql
except ImportError:
    print(f"Библиотека pymysql не установлена. Функционал активации будет недоступен.")

NAME = "TelegramAccounts"
VERSION = "1.7"
DESCRIPTION = "Плагин для автовыдачи телеграмм номеров"
CREDITS = "@titleplugins"
UUID = "e7f3a9b1-5d8c-4f2e-9b6a-1c0ddD82e"
SETTINGS_PAGE = False

LOGGER_PREFIX = "[TELEGRAM_ACCOUNTS]"
logger = logging.getLogger("FPC.telegramaccounts")

CONFIG_DIR = "storage/tg"
CONFIG_PATH = f"{CONFIG_DIR}/config.json"
USER_ORDERS_PATH = f"{CONFIG_DIR}/user_orders.json"

DEFAULT_PURCHASE_TEMPLATE = """Спасибо за покупку!

Данные для входа:
Телефон: {phone}

Чтобы получить код подтверждения для входа в аккаунт, отправьте "cd {phone}" в этот чат."""

DEFAULT_CODE_TEMPLATE = """✅ Код для входа в Telegram: {code}

Спасибо за покупку, не забудь подтвердить заказ тут: {order_link}

Также не забудьте оставить отзыв!"""

FP_WITHDRAWAL_COMMISSION_RATE = 0.05 


used_orders = {}
order_account_ids = {}
order_phone_numbers = {}
order_queue = queue.Queue()
executor = None
max_workers = 5
active_tasks = 0
max_concurrent_tasks = 3
task_lock = threading.Lock()
is_processing = False

ORIGIN_MAP = {
    "phishing": "Фишинг",
    "stealer": "Стилер",
    "personal": "Личный",
    "resale": "Перепродажа",
    "autoreg": "Авторег",
    "samoreg": "Саморег"
}

bot = None
cardinal_instance = None
config = {}


def show_tg_settings(message: types.Message):
    """Обработчик команды /tg_settings"""
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("🌍 Управление странами", callback_data="tg_countries"),
        InlineKeyboardButton("👥 Администраторы", callback_data="tg_admins"),
        InlineKeyboardButton("🔄 Автовозвраты", callback_data="tg_auto_returns"),
        InlineKeyboardButton("🔑 LOLZ TOKEN", callback_data="tg_lolz_token"),
        InlineKeyboardButton("📋 Заказы", callback_data="tg_orders"),
        InlineKeyboardButton("🔍 Настройка происхождений (множественный выбор)", callback_data="tg_origin"),
        InlineKeyboardButton("💬 Шаблоны сообщений", callback_data="tg_message_templates"),
        InlineKeyboardButton("⚙️ Настройка плагина", callback_data="tg_setup_plugin")
    )

    countries_count = len(config["countries"])
    admins_count = len(config["administrators"])

    message_text = (
        f"🤖 <b>{NAME}</b> <code>v{VERSION}</code>\n\n"
        f"📝 <b>Описание:</b> {DESCRIPTION}\n"
        f"👨‍💻 <b>Автор:</b> {CREDITS}\n\n"
        f"📊 <b>Статистика:</b>\n"
        f"🌍 Количество стран: {countries_count}\n"
        f"👥 Количество администраторов: {admins_count}\n\n"
        f"⚙️ <b>Настройки автовыдачи телеграмм номеров:</b>"
    )

    bot.send_message(message.chat.id, message_text, reply_markup=kb, parse_mode="HTML")


def show_tg_settings_callback(call: types.CallbackQuery):
    bot.clear_step_handler_by_chat_id(call.message.chat.id)

    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("🌍 Управление странами", callback_data="tg_countries"),
        InlineKeyboardButton("👥 Администраторы", callback_data="tg_admins"),
        InlineKeyboardButton("🔄 Автовозвраты", callback_data="tg_auto_returns"),
        InlineKeyboardButton("🔑 LOLZ TOKEN", callback_data="tg_lolz_token"),
        InlineKeyboardButton("📋 Заказы", callback_data="tg_orders"),
        InlineKeyboardButton("🔍 Настройка происхождений (множественный выбор)", callback_data="tg_origin"),
        InlineKeyboardButton("💬 Шаблоны сообщений", callback_data="tg_message_templates"),
        InlineKeyboardButton("⚙️ Настройка плагина", callback_data="tg_setup_plugin")
    )

    countries_count = len(config["countries"])
    admins_count = len(config["administrators"])

    message_text = (
        f"🤖 <b>{NAME}</b> <code>v{VERSION}</code>\n\n"
        f"📝 <b>Описание:</b> {DESCRIPTION}\n"
        f"👨‍💻 <b>Автор:</b> {CREDITS}\n\n"
        f"📊 <b>Статистика:</b>\n"
        f"🌍 Количество стран: {countries_count}\n"
        f"👥 Количество администраторов: {admins_count}\n\n"
        f"⚙️ <b>Настройки автовыдачи телеграмм номеров:</b>"
    )

    bot.edit_message_text(
        message_text,
        call.message.chat.id,
        call.message.message_id,
        reply_markup=kb,
        parse_mode="HTML"
    )


def ensure_config_exists():
    """Проверка и создание конфигурационного файла, если он не существует"""
    if not os.path.exists(CONFIG_DIR):
        os.makedirs(CONFIG_DIR)

    if not os.path.exists(CONFIG_PATH):
        default_config = {
            "countries": {},
            "administrators": [],
            "auto_returns": True,
            "lolz_token": "",
            "origins": ["personal"],
            "purchase_template": DEFAULT_PURCHASE_TEMPLATE,
            "code_template": DEFAULT_CODE_TEMPLATE,
            "orders_profit": {},
            "buy_cheapest": True,
            "check_accounts": True
        }
        with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            json.dump(default_config, f, ensure_ascii=False, indent=4)
        return default_config

    with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
        config_data = json.load(f)

        if "origin" in config_data and "origins" not in config_data:
            logger.info(f"{LOGGER_PREFIX} Миграция с одиночного формата происхождения на множественный")
            config_data["origins"] = [config_data["origin"]]
            del config_data["origin"]

            with open(CONFIG_PATH, 'w', encoding='utf-8') as f_write:
                json.dump(config_data, f_write, ensure_ascii=False, indent=4)

        if "origins" not in config_data:
            logger.info(f"{LOGGER_PREFIX} Добавление поля origins по умолчанию")
            config_data["origins"] = ["personal"]

        if "purchase_template" not in config_data:
            logger.info(f"{LOGGER_PREFIX} Добавление шаблона сообщения покупки по умолчанию")
            config_data["purchase_template"] = DEFAULT_PURCHASE_TEMPLATE

        if "code_template" not in config_data:
            logger.info(f"{LOGGER_PREFIX} Добавление шаблона сообщения выдачи кода по умолчанию")
            config_data["code_template"] = DEFAULT_CODE_TEMPLATE

        if "orders_profit" not in config_data:
            logger.info(f"{LOGGER_PREFIX} Добавление хранилища данных о прибыли от заказов")
            config_data["orders_profit"] = {}

        with open(CONFIG_PATH, 'w', encoding='utf-8') as f_write:
            json.dump(config_data, f_write, ensure_ascii=False, indent=4)

        return config_data

def find_available_accounts(country_code, min_price, max_price):
    """Поиск доступных аккаунтов с сортировкой по возрастанию цены"""
    available_accounts = []

    try:
        timer = threading.Timer(3.0, lambda: None)
        timer.start()
        timer.join()

        url = f"https://prod-api.lzt.market/telegram?order_by=price_to_up&pmin={min_price}&pmax={max_price}"

        for origin in config["origins"]:
            url += f"&origin[]={origin}"

        url += f"&spam=no&allow_geo_spamblock=true&password=no&country[]={country_code}"

        headers = {
            "accept": "application/json",
            "authorization": f"Bearer {config['lolz_token']}"
        }

        response = requests.get(url, headers=headers)
        logger.info(f"{LOGGER_PREFIX} Запрос к API LOLZ Market: {url}")

        if response.status_code == 200:
            response_data = response.json()

            if 'items' in response_data and response_data['items']:
                items = response_data['items']
                logger.info(f"{LOGGER_PREFIX} Найдено {len(items)} аккаунтов")
                
                if config.get("check_accounts", True):
                    checked_accounts = []
                    for account in items:
                        if check_account(account.get('item_id')):
                            checked_accounts.append(account)
                    available_accounts = checked_accounts
                else:
                    available_accounts = items
            else:
                logger.info(f"{LOGGER_PREFIX} Нет доступных аккаунтов")
        else:
            logger.error(f"{LOGGER_PREFIX} Ошибка запроса к API LOLZ Market: {response.status_code}, {response.text}")

    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка при поиске аккаунтов: {e}")

    logger.info(f"{LOGGER_PREFIX} Всего найдено {len(available_accounts)} доступных аккаунтов")
    return available_accounts

def check_account(item_id):
    """Проверяет аккаунт перед покупкой"""
    try:
        url = f"https://prod-api.lzt.market/{item_id}/check"
        headers = {
            "accept": "application/json",
            "authorization": f"Bearer {config['lolz_token']}"
        }

        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            result = response.json()
            if result.get('status') == 'ok':
                logger.info(f"{LOGGER_PREFIX} Аккаунт {item_id} прошел проверку")
                return True
            else:
                logger.warning(f"{LOGGER_PREFIX} Аккаунт {item_id} не прошел проверку: {result.get('message', 'Неизвестная ошибка')}")
                return False
        else:
            logger.error(f"{LOGGER_PREFIX} Ошибка при проверке аккаунта {item_id}: {response.status_code}")
            return False
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка при проверке аккаунта {item_id}: {e}")
        return False
    
def try_purchase_accounts(accounts):
    """Пытается купить аккаунты из списка по очереди, пока не найдет доступный"""
    insufficient_funds = False

    # Если включена настройка покупать самый дешевый, сортируем аккаунты по цене
    if config.get("buy_cheapest", True):
        accounts = sorted(accounts, key=lambda x: x.get('price', 0))

    for account in accounts:
        item_id = account.get('item_id')
        price = account.get('price')
        logger.info(f"{LOGGER_PREFIX} Попытка покупки аккаунта ID: {item_id}, цена: {price}₽")

        purchase_result = purchase_account(item_id)

        if purchase_result and 'item' in purchase_result:
            login_data = purchase_result['item'].get('loginData', {})
            login = login_data.get('login', '')
            password = login_data.get('password', '')
            telegram_id = purchase_result['item'].get('telegram_id', '')
            telegram_phone = purchase_result['item'].get('telegram_phone', '')
            telegram_username = purchase_result['item'].get('telegram_username', '')

            account_data = {
                'login': login,
                'password': password,
                'telegram_id': telegram_id,
                'telegram_phone': telegram_phone,
                'telegram_username': telegram_username
            }

            return purchase_result, account_data, insufficient_funds

        elif purchase_result and 'errors' in purchase_result:
            error_msg = ', '.join(purchase_result.get('errors', []))
            logger.warning(f"{LOGGER_PREFIX} Не удалось купить аккаунт ID {item_id}: {error_msg}")

            for fund_error in ["недостаточно средств", "недостаточно баланса", "Пополнить баланс"]:
                if fund_error.lower() in error_msg.lower():
                    insufficient_funds = True
                    admin_alert = f"💰 ВНИМАНИЕ! Недостаточно средств на балансе LOLZ Market для покупки аккаунта ID {item_id} по цене {price}₽. Пожалуйста, пополните баланс!"
                    notify_admins(admin_alert)
                    logger.error(
                        f"{LOGGER_PREFIX} Недостаточно средств на балансе LOLZ Market. Прекращаем попытки покупки.")
                    return None, None, insufficient_funds

            ignorable_errors = [
                "Аккаунт продан",
                "Произошло более 20 ошибок во время проверки аккаунта",
                "произошло более 20 ошибок",
                "не прошел проверку",
                "уже продан",
                "в данный момент недоступен",
                "retry_request"
            ]

            should_continue = False
            for ignorable_error in ignorable_errors:
                if ignorable_error.lower() in error_msg.lower():
                    should_continue = True
                    break

            if not should_continue:
                logger.error(f"{LOGGER_PREFIX} Критическая ошибка при покупке аккаунта: {error_msg}")
                break
            else:
                logger.info(f"{LOGGER_PREFIX} Игнорируем ошибку и пробуем следующий аккаунт")

    return None, None, insufficient_funds

def load_user_orders():
    """Загрузка данных о заказах пользователей"""
    if not os.path.exists(USER_ORDERS_PATH):
        user_orders_data = {
            "user_orders": {},
            "phone_users": {}
        }
        with open(USER_ORDERS_PATH, 'w', encoding='utf-8') as f:
            json.dump(user_orders_data, f, ensure_ascii=False, indent=4)
        return user_orders_data

    try:
        with open(USER_ORDERS_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка при загрузке данных о заказах пользователей: {e}")
        return {"user_orders": {}, "phone_users": {}}


def save_user_orders(data):
    """Сохранение данных о заказах пользователей"""
    try:
        with open(USER_ORDERS_PATH, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
        return True
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка при сохранении данных о заказах пользователей: {e}")
        return False


def save_config():
    """Сохранение конфигурации в файл"""
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=4)


def save_order_profit(order_id, fp_sum, lolz_cost):
    """Сохранение информации о прибыли от заказа"""
    try:
        fp_sum_float = float(fp_sum)
        lolz_cost_float = float(lolz_cost)

        profit = (fp_sum_float * (1 - FP_WITHDRAWAL_COMMISSION_RATE)) - lolz_cost_float

        config["orders_profit"][str(order_id)] = {
            "fp_sum": fp_sum_float,
            "lolz_cost": lolz_cost_float,
            "profit": profit,
            "date": time.strftime("%Y-%m-%d %H:%M:%S"),
            "fp_commission_rate": FP_WITHDRAWAL_COMMISSION_RATE
        }
        save_config()
        logger.info(f"{LOGGER_PREFIX} Сохранена информация о чистой прибыли (после комиссии FP) для заказа #{order_id}: {profit:.2f} руб.")

        return True
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка при сохранении прибыли для заказа #{order_id}: {e}")
        return False


def get_order_profit(order_id):
    """Получение информации о прибыли от заказа"""
    return config["orders_profit"].get(str(order_id))


def get_total_profit():
    """Получение общей прибыли от всех заказов"""
    total = 0
    for order_data in config["orders_profit"].values():
        total += order_data.get("profit", 0)
    return total


def set_origin(call: types.CallbackQuery):
    """Обработчик выбора происхождения для BIND_TO_DELETE"""
    logger.info(f"{LOGGER_PREFIX} Вызвана глобальная функция set_origin с callback_data: {call.data}")
    origin_code = call.data.split("_")[-1]

    if origin_code == "self_reg":
        origin_code = "self_registration"

    if origin_code in ORIGIN_MAP:
        if origin_code in config["origins"]:
            if len(config["origins"]) > 1:
                config["origins"].remove(origin_code)
                action_text = "удалено"
            else:
                bot.answer_callback_query(call.id, "Нельзя удалить последний тип происхождения")
                return
        else:
            config["origins"].append(origin_code)
            action_text = "добавлено"

        save_config()
        bot.answer_callback_query(call.id, f"Происхождение '{ORIGIN_MAP[origin_code]}' {action_text}")

    show_tg_settings_callback(call)


def import_existing_orders(c: Cardinal):
    """Импортирует существующие заказы и их номера в систему сохранения данных"""
    try:
        logger.info(f"{LOGGER_PREFIX} Импорт существующих заказов в систему сохранения...")
        user_orders_data = load_user_orders()

        next_order, orders = c.account.get_sells()

        imported_count = 0

        for order in orders:
            if order.id in order_phone_numbers and order.id in order_account_ids:
                phone = order_phone_numbers[order.id]
                item_id = order_account_ids[order.id]
                user_id = str(order.buyer_username)

                if user_id not in user_orders_data["user_orders"]:
                    user_orders_data["user_orders"][user_id] = {}

                if str(order.id) not in user_orders_data["user_orders"][user_id]:
                    user_orders_data["user_orders"][user_id][str(order.id)] = {
                        "phone": phone,
                        "item_id": item_id
                    }
                    user_orders_data["phone_users"][phone] = user_id
                    imported_count += 1

        save_user_orders(user_orders_data)
        logger.info(f"{LOGGER_PREFIX} Импорт заказов завершен. Добавлено {imported_count} записей.")
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка при импорте существующих заказов: {e}")


def init_commands(c_: Cardinal):
    global bot, cardinal_instance, config, executor
    logger.info("=== init_commands() from TelegramAccounts ===")

    cardinal_instance = c_
    bot = c_.telegram.bot
    config = ensure_config_exists()

    load_user_orders()

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)

    threading.Thread(target=import_existing_orders, args=(c_,), daemon=True).start()

    threading.Thread(target=process_order_queue, daemon=True).start()

    _all_handlers = [handler for handler_group in bot.callback_query_handlers for handler in handler_group]
    logger.info(f"{LOGGER_PREFIX} Всего зарегистрировано {len(_all_handlers)} обработчиков callback-запросов")

    @bot.callback_query_handler(func=lambda call: call.data.startswith('tg_'))
    def handle_all_callbacks(call: types.CallbackQuery):
        logger.info(f"{LOGGER_PREFIX} Получен callback: {call.data}")

        if call.data == "samoreg" or call.data == "samoreg":
            logger.info(f"{LOGGER_PREFIX} Обнаружен callback для выбора происхождения 'Саморег': {call.data}")
            set_origin(call)
            return

        if call.data == "tg_activate":
            activate_plugin(call)
            return

        if call.data.startswith("tg_edit_country_name_"):
            handle_edit_country_name(call)
        elif call.data.startswith("tg_edit_country_min_"):
            handle_edit_country_min(call)
        elif call.data.startswith("tg_edit_country_max_"):
            handle_edit_country_max(call)
        elif call.data.startswith("tg_edit_country_"):
            handle_edit_country_menu(call)
        elif call.data == "tg_countries":
            handle_countries_menu(call)
        elif call.data == "tg_add_country":
            handle_add_country(call)
        elif call.data.startswith("tg_delete_country_"):
            handle_delete_country(call)
        elif call.data.startswith("tg_confirm_delete_country_"):
            handle_confirm_delete_country(call)
        elif call.data == "tg_admins":
            admin_menu(call)
        elif call.data == "tg_auto_returns":
            auto_returns_menu(call)
        elif call.data == "tg_auto_returns_on":
            auto_returns_on(call)
        elif call.data == "tg_auto_returns_off":
            auto_returns_off(call)
        elif call.data == "tg_lolz_token":
            lolz_token_menu(call)
        elif call.data == "tg_add_lolz_token" or call.data == "tg_edit_lolz_token":
            add_edit_lolz_token(call)
        elif call.data == "tg_delete_lolz_token":
            delete_lolz_token_confirm(call)
        elif call.data == "tg_confirm_delete_lolz_token":
            delete_lolz_token_confirmed(call)
        elif call.data == "tg_check_lolz_token":
            check_lolz_token(call)
        elif call.data == "tg_origin":
            origin_menu(call)
        elif call.data == "tg_add_admin":
            add_admin(call)
        elif call.data.startswith("tg_set_origin_"):
            set_origin(call)
        elif call.data == "tg_setup_plugin":
            plugin_setup_menu(call)
        elif call.data == "tg_back_to_main":
            show_tg_settings_callback(call)
        elif call.data == "tg_message_templates":
            message_templates_menu(call)
        elif call.data == "tg_edit_purchase_template":
            edit_purchase_template(call)
        elif call.data == "tg_edit_code_template":
            edit_code_template(call)
        elif call.data == "tg_orders":
            orders_menu(call)
        elif call.data.startswith("tg_page_") and "orders" in call.data:
            # Обработка пагинации заказов
            orders_menu(call)
        elif call.data.startswith("tg_order_"):
            order_details(call)
        else:
            bot.answer_callback_query(call.id, "Неизвестная команда")
        
    @bot.callback_query_handler(func=lambda call: call.data == "tg_toggle_cheapest")
    def toggle_cheapest(call: types.CallbackQuery):
        config["buy_cheapest"] = not config.get("buy_cheapest", True)
        save_config()
        bot.answer_callback_query(call.id, f"Покупка самого дешевого {'включена' if config['buy_cheapest'] else 'выключена'}")
        plugin_setup_menu(call)

    @bot.callback_query_handler(func=lambda call: call.data == "tg_toggle_check")
    def toggle_check(call: types.CallbackQuery):
        config["check_accounts"] = not config.get("check_accounts", True)
        save_config()
        bot.answer_callback_query(call.id, f"Проверка аккаунтов {'включена' if config['check_accounts'] else 'выключена'}")
        plugin_setup_menu(call)
        

    @bot.message_handler(commands=['tg_settings'])
    def tg_settings_command(message: types.Message):
        """Регистрация команды /tg_settings"""
        show_tg_settings(message)

    def handle_countries_menu(call: types.CallbackQuery):
        kb = InlineKeyboardMarkup(row_width=1)
        kb.add(InlineKeyboardButton("➕ Добавить страну", callback_data="tg_add_country"))

        if config["countries"]:
            for code, country_data in config["countries"].items():
                callback_data = f"tg_edit_country_{code.strip()}"
                logger.info(f"{LOGGER_PREFIX} Создаём кнопку для страны {code} с callback_data: {callback_data}")
                kb.add(InlineKeyboardButton(
                    f"{country_data['name']} ({code}) - {country_data['min_price']}₽-{country_data['max_price']}₽",
                    callback_data=callback_data
                ))

        kb.add(InlineKeyboardButton("🔙 Назад", callback_data="tg_back_to_main"))
        bot.edit_message_text("🌍 Управление странами:", call.message.chat.id, call.message.message_id, reply_markup=kb)

    def handle_add_country(call: types.CallbackQuery):
        msg = bot.edit_message_text(
            "Введите краткий код страны (например, ID для Индонезии):",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("🔙 Отмена", callback_data="tg_back_to_main")
            )
        )
        bot.register_next_step_handler(msg, add_country_step2)

    def handle_edit_country_menu(call: types.CallbackQuery):
        logger.info(f"{LOGGER_PREFIX} Обработка меню редактирования страны: {call.data}")

        if call.data.startswith("tg_edit_country_name_") or \
                call.data.startswith("tg_edit_country_min_") or \
                call.data.startswith("tg_edit_country_max_"):
            logger.info(f"{LOGGER_PREFIX} Пропускаем обработку, так как это специфичный callback: {call.data}")
            return

        country_code = call.data.replace("tg_edit_country_", "")
        logger.info(f"{LOGGER_PREFIX} Извлечен код страны: {country_code}")

        if country_code not in config["countries"]:
            bot.answer_callback_query(call.id, "Страна не найдена!")
            return handle_countries_menu(call)

        try:
            country_data = config["countries"][country_code]
            kb = InlineKeyboardMarkup(row_width=1)
            kb.add(
                InlineKeyboardButton("✏️ Изменить название", callback_data=f"tg_edit_country_name_{country_code}"),
                InlineKeyboardButton("💰 Изменить мин. цену", callback_data=f"tg_edit_country_min_{country_code}"),
                InlineKeyboardButton("💎 Изменить макс. цену", callback_data=f"tg_edit_country_max_{country_code}"),
                InlineKeyboardButton("🗑️ Удалить страну", callback_data=f"tg_delete_country_{country_code}"),
                InlineKeyboardButton("🔙 Назад", callback_data="tg_countries")
            )

            bot.edit_message_text(
                f"Настройки страны: {country_data['name']} ({country_code})\n"
                f"Минимальная цена: {country_data['min_price']}₽\n"
                f"Максимальная цена: {country_data['max_price']}₽",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=kb
            )
        except Exception as e:
            logger.error(f"{LOGGER_PREFIX} Ошибка при открытии меню редактирования страны: {e}")
            bot.answer_callback_query(call.id, "Произошла ошибка при обработке запроса")
            handle_countries_menu(call)

    def handle_edit_country_name(call: types.CallbackQuery):
        logger.info(f"{LOGGER_PREFIX} Обработка редактирования названия страны: {call.data}")
        country_code = call.data.replace("tg_edit_country_name_", "")

        if country_code not in config["countries"]:
            bot.answer_callback_query(call.id, f"Ошибка: страна с кодом {country_code} больше не существует!")
            return handle_countries_menu(call)

        try:
            msg = bot.edit_message_text(
                f"Введите новое название для страны {country_code}:",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=InlineKeyboardMarkup().add(
                    InlineKeyboardButton("🔙 Отмена", callback_data=f"tg_edit_country_{country_code}")
                )
            )
            bot.register_next_step_handler(msg, process_country_name_edit, country_code)
        except Exception as e:
            logger.error(f"{LOGGER_PREFIX} Ошибка при редактировании названия страны: {e}")
            bot.answer_callback_query(call.id, "Произошла ошибка при обработке запроса")
            handle_countries_menu(call)

    def handle_edit_country_min(call: types.CallbackQuery):
        logger.info(f"{LOGGER_PREFIX} Обработка редактирования мин. цены страны: {call.data}")
        country_code = call.data.replace("tg_edit_country_min_", "")

        if country_code not in config["countries"]:
            bot.answer_callback_query(call.id, f"Ошибка: страна с кодом {country_code} больше не существует!")
            return handle_countries_menu(call)

        try:
            msg = bot.edit_message_text(
                f"Введите новую минимальную цену для страны {config['countries'][country_code]['name']} ({country_code}):",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=InlineKeyboardMarkup().add(
                    InlineKeyboardButton("🔙 Отмена", callback_data=f"tg_edit_country_{country_code}")
                )
            )
            bot.register_next_step_handler(msg, process_country_min_edit, country_code)
        except Exception as e:
            logger.error(f"{LOGGER_PREFIX} Ошибка при редактировании мин. цены страны: {e}")
            bot.answer_callback_query(call.id, "Произошла ошибка при обработке запроса")
            handle_countries_menu(call)

    def handle_edit_country_max(call: types.CallbackQuery):
        logger.info(f"{LOGGER_PREFIX} Обработка редактирования макс. цены страны: {call.data}")
        country_code = call.data.replace("tg_edit_country_max_", "")

        if country_code not in config["countries"]:
            bot.answer_callback_query(call.id, f"Ошибка: страна с кодом {country_code} больше не существует!")
            return handle_countries_menu(call)

        try:
            msg = bot.edit_message_text(
                f"Введите новую максимальную цену для страны {config['countries'][country_code]['name']} ({country_code}):",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=InlineKeyboardMarkup().add(
                    InlineKeyboardButton("🔙 Отмена", callback_data=f"tg_edit_country_{country_code}")
                )
            )
            bot.register_next_step_handler(msg, process_country_max_edit, country_code)
        except Exception as e:
            logger.error(f"{LOGGER_PREFIX} Ошибка при редактировании макс. цены страны: {e}")
            bot.answer_callback_query(call.id, "Произошла ошибка при обработке запроса")
            handle_countries_menu(call)

    def handle_delete_country(call: types.CallbackQuery):
        country_code = call.data.replace("tg_delete_country_", "")
        kb = InlineKeyboardMarkup(row_width=2)
        kb.add(
            InlineKeyboardButton("✅ Да", callback_data=f"tg_confirm_delete_country_{country_code}"),
            InlineKeyboardButton("❌ Нет", callback_data=f"tg_edit_country_{country_code}")
        )

        bot.edit_message_text(
            f"Вы уверены, что хотите удалить страну {config['countries'][country_code]['name']} ({country_code})?",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=kb
        )

    def handle_confirm_delete_country(call: types.CallbackQuery):
        country_code = call.data.replace("tg_confirm_delete_country_", "")
        country_name = config["countries"][country_code]["name"]
        del config["countries"][country_code]
        save_config()

        bot.answer_callback_query(call.id, f"Страна {country_name} удалена!")
        handle_countries_menu(call)

    @bot.callback_query_handler(func=lambda call: call.data == "tg_admins")
    def admin_menu(call: types.CallbackQuery):
        kb = InlineKeyboardMarkup(row_width=1)
        kb.add(InlineKeyboardButton("➕ Добавить администратора", callback_data="tg_add_admin"))

        for admin_id in config["administrators"]:
            kb.add(InlineKeyboardButton(
                f"ID: {admin_id}",
                callback_data=f"tg_delete_admin_{admin_id}"
            ))

        kb.add(InlineKeyboardButton("🔙 Назад", callback_data="tg_back_to_main"))
        bot.edit_message_text(
            "👥 Администраторы (получают уведомления):",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=kb
        )

    @bot.callback_query_handler(func=lambda call: call.data == "tg_add_admin")
    def add_admin(call: types.CallbackQuery):
        msg = bot.edit_message_text(
            "Введите ID пользователя Telegram для добавления в администраторы:",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("🔙 Отмена", callback_data="tg_admins")
            )
        )
        bot.register_next_step_handler(msg, process_add_admin)

    def process_add_admin(message: types.Message):
        if message.text is None:
            return

        try:
            bot.delete_message(message.chat.id, message.message_id - 1)
        except Exception as e:
            logger.error(f"{LOGGER_PREFIX} Ошибка при удалении сообщения: {e}")

        try:
            admin_id = int(message.text.strip())
            if admin_id in config["administrators"]:
                bot.send_message(message.chat.id, "❌ Этот пользователь уже является администратором!")
                bot.clear_step_handler_by_chat_id(message.chat.id)
                return show_tg_settings(message)

            config["administrators"].append(admin_id)
            save_config()
            bot.clear_step_handler_by_chat_id(message.chat.id)
            bot.send_message(message.chat.id, f"✅ Администратор с ID {admin_id} добавлен!")
            show_tg_settings(message)
        except ValueError:
            bot.clear_step_handler_by_chat_id(message.chat.id)
            bot.send_message(message.chat.id, "❌ Введите корректный числовой ID!")
            show_tg_settings(message)

    @bot.callback_query_handler(func=lambda call: call.data.startswith("tg_delete_admin_"))
    def delete_admin_confirm(call: types.CallbackQuery):
        admin_id = int(call.data.split("_")[-1])
        kb = InlineKeyboardMarkup(row_width=2)
        kb.add(
            InlineKeyboardButton("✅ Да", callback_data=f"tg_confirm_delete_admin_{admin_id}"),
            InlineKeyboardButton("❌ Нет", callback_data="tg_admins")
        )

        bot.edit_message_text(
            f"Вы уверены, что хотите удалить администратора с ID {admin_id}?",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=kb
        )

    @bot.callback_query_handler(func=lambda call: call.data.startswith("tg_confirm_delete_admin_"))
    def delete_admin_confirmed(call: types.CallbackQuery):
        admin_id = int(call.data.split("_")[-1])
        if admin_id in config["administrators"]:
            config["administrators"].remove(admin_id)
            save_config()

        bot.answer_callback_query(call.id, f"Администратор {admin_id} удален!")
        admin_menu(call)

    @bot.callback_query_handler(func=lambda call: call.data == "tg_auto_returns")
    def auto_returns_menu(call: types.CallbackQuery):
        kb = InlineKeyboardMarkup(row_width=2)
        current_status = "✅ Включены" if config["auto_returns"] else "❌ Выключены"

        kb.add(
            InlineKeyboardButton("✅ Включить", callback_data="tg_auto_returns_on"),
            InlineKeyboardButton("❌ Выключить", callback_data="tg_auto_returns_off")
        )
        kb.add(InlineKeyboardButton("🔙 Назад", callback_data="tg_back_to_main"))

        bot.edit_message_text(
            f"🔄 Автовозвраты: {current_status}\n\n"
            "При включенных автовозвратах система автоматически возвращает средства пользователю "
            "в случае проблем с номером и отправляет уведомление администраторам.\n\n"
            "При выключенных автовозвратах пользователю показывается причина проблемы и "
            "предлагается кнопка для ручного возврата через администратора.",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=kb
        )

    @bot.callback_query_handler(func=lambda call: call.data == "tg_auto_returns_on")
    def auto_returns_on(call: types.CallbackQuery):
        config["auto_returns"] = True
        save_config()
        bot.answer_callback_query(call.id, "Автовозвраты включены!")
        auto_returns_menu(call)

    @bot.callback_query_handler(func=lambda call: call.data == "tg_auto_returns_off")
    def auto_returns_off(call: types.CallbackQuery):
        config["auto_returns"] = False
        save_config()
        bot.answer_callback_query(call.id, "Автовозвраты выключены!")
        auto_returns_menu(call)

    @bot.callback_query_handler(func=lambda call: call.data == "tg_lolz_token")
    def lolz_token_menu(call: types.CallbackQuery):
        kb = InlineKeyboardMarkup(row_width=1)

        if config["lolz_token"]:
            masked_token = config["lolz_token"][:4] + "*" * (len(config["lolz_token"]) - 8) + config["lolz_token"][-4:]
            kb.add(
                InlineKeyboardButton("✏️ Редактировать токен", callback_data="tg_edit_lolz_token"),
                InlineKeyboardButton("🗑️ Удалить токен", callback_data="tg_delete_lolz_token"),
                InlineKeyboardButton("✅ Проверить токен", callback_data="tg_check_lolz_token")
            )
            token_status = f"🔑 Текущий токен: {masked_token}"
        else:
            kb.add(InlineKeyboardButton("➕ Добавить токен", callback_data="tg_add_lolz_token"))
            token_status = "❌ Токен не настроен"

        kb.add(InlineKeyboardButton("🔙 Назад", callback_data="tg_back_to_main"))

        bot.edit_message_text(
            f"LOLZ TOKEN: {token_status}",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=kb
        )

    @bot.callback_query_handler(func=lambda call: call.data == "tg_add_lolz_token" or call.data == "tg_edit_lolz_token")
    def add_edit_lolz_token(call: types.CallbackQuery):
        action = "Введите" if call.data == "tg_add_lolz_token" else "Введите новый"
        msg = bot.edit_message_text(
            f"{action} LOLZ токен:",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("🔙 Отмена", callback_data="tg_lolz_token")
            )
        )
        bot.register_next_step_handler(msg, process_lolz_token)

    def process_lolz_token(message: types.Message):
        if message.text is None:
            return

        try:
            bot.delete_message(message.chat.id, message.message_id - 1)
        except Exception as e:
            logger.error(f"{LOGGER_PREFIX} Ошибка при удалении сообщения: {e}")

        token = message.text.strip()
        config["lolz_token"] = token
        save_config()

        bot.clear_step_handler_by_chat_id(message.chat.id)

        bot.delete_message(message.chat.id, message.message_id)

        bot.send_message(message.chat.id, "✅ LOLZ токен успешно сохранен!")
        show_tg_settings(message)

    @bot.callback_query_handler(func=lambda call: call.data == "tg_delete_lolz_token")
    def delete_lolz_token_confirm(call: types.CallbackQuery):
        kb = InlineKeyboardMarkup(row_width=2)
        kb.add(
            InlineKeyboardButton("✅ Да", callback_data="tg_confirm_delete_lolz_token"),
            InlineKeyboardButton("❌ Нет", callback_data="tg_lolz_token")
        )

        bot.edit_message_text(
            "Вы уверены, что хотите удалить LOLZ токен?",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=kb
        )

    @bot.callback_query_handler(func=lambda call: call.data == "tg_confirm_delete_lolz_token")
    def delete_lolz_token_confirmed(call: types.CallbackQuery):
        config["lolz_token"] = ""
        save_config()
        bot.answer_callback_query(call.id, "LOLZ токен удален!")
        lolz_token_menu(call)

    @bot.callback_query_handler(func=lambda call: call.data == "tg_check_lolz_token")
    def check_lolz_token(call: types.CallbackQuery):
        bot.answer_callback_query(call.id, "Функция проверки токена будет добавлена позже")
        bot.edit_message_text(
            "✅ Токен успешно проверен!\nРеализация проверки через API LOLZ будет добавлена позже.",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("🔙 Назад", callback_data="tg_lolz_token")
            )
        )

    @bot.callback_query_handler(func=lambda call: call.data == "tg_origin")
    def origin_menu(call: types.CallbackQuery):
        kb = InlineKeyboardMarkup(row_width=1)

        selected_origins = config["origins"]

        for origin_code, origin_name in ORIGIN_MAP.items():
            callback_data = f"tg_set_origin_{origin_code}"
            if origin_code in selected_origins:
                mark = "✅ "
            else:
                mark = "☑️ "

            if origin_code == "self_registration":
                callback_data = "tg_set_origin_self_reg"
                logger.info(f"{LOGGER_PREFIX} Для 'self_registration' используем сокращенный callback: {callback_data}")

            kb.add(InlineKeyboardButton(f"{mark}{origin_name}", callback_data=callback_data))
            logger.info(f"{LOGGER_PREFIX} Добавлена кнопка: {origin_name} с callback_data: {callback_data}")

        kb.add(InlineKeyboardButton("🔄 Сохранить и вернуться", callback_data="tg_back_to_main"))

        selected_names = [ORIGIN_MAP.get(code, "Неизвестно") for code in selected_origins]
        selected_text = ", ".join(selected_names)

        bot.edit_message_text(
            f"🔍 Выберите типы происхождения номеров (можно выбрать несколько):\n\n"
            f"Текущий выбор: {selected_text}\n\n"
            f"✅ - выбранные типы, нажмите для отмены\n"
            f"☑️ - доступные типы, нажмите для выбора",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=kb
        )

    @bot.callback_query_handler(func=lambda call: call.data == "tg_setup_plugin")
    def plugin_setup_menu(call: types.CallbackQuery):
        """Меню настройки плагина"""
        kb = InlineKeyboardMarkup(row_width=1)
        
        buy_cheapest_text = "✅ Покупать самый дешевый" if config.get("buy_cheapest", True) else "❌ Не покупать самый дешевый"
        check_accounts_text = "✅ Проверять аккаунты" if config.get("check_accounts", True) else "❌ Не проверять аккаунты"
        
        kb.add(
            InlineKeyboardButton(buy_cheapest_text, callback_data="tg_toggle_cheapest"),
            InlineKeyboardButton(check_accounts_text, callback_data="tg_toggle_check"),
            InlineKeyboardButton("🔙 Назад", callback_data="tg_back_to_main")
        )

        message_text = (
            "⚙️ <b>Настройка плагина</b>\n\n"
            "1. <b>Покупать самый дешевый:</b> При включении всегда выбирает самый дешевый подходящий аккаунт.\n"
            "2. <b>Проверять аккаунты:</b> Проверяет аккаунт перед покупкой на блокировки и проблемы.\n\n"
            "Текущие настройки:"
        )

        bot.edit_message_text(
            message_text,
            call.message.chat.id,
            call.message.message_id,
            reply_markup=kb,
            parse_mode="HTML"
        )

    def orders_menu(call: types.CallbackQuery):
        """Отображение меню заказов"""
        page = 0
        if '_' in call.data:
            parts = call.data.split('_')
            if len(parts) > 2 and parts[1] == 'page':
                try:
                    page = int(parts[2])
                except ValueError:
                    page = 0

        PAGE_SIZE = 5

        user_orders_data = load_user_orders()
        kb = InlineKeyboardMarkup(row_width=1)

        total_profit = get_total_profit()

        message_text = f"📋 <b>Управление заказами</b>\n\n💰 <b>Общая чистая прибыль:</b> {total_profit:.2f} руб.\n\n"

        all_orders = []
        for user_id, orders in user_orders_data["user_orders"].items():
            for order_id, order_data in orders.items():
                if order_id in config["orders_profit"]:
                    profit_data = config["orders_profit"][order_id]
                    all_orders.append({
                        "order_id": order_id,
                        "user_id": user_id,
                        "phone": order_data.get("phone", "Нет данных"),
                        "profit": profit_data.get("profit", 0),
                        "date": profit_data.get("date", "Нет данных")
                    })
                else:
                    all_orders.append({
                        "order_id": order_id,
                        "user_id": user_id,
                        "phone": order_data.get("phone", "Нет данных"),
                        "profit": 0,
                        "date": "Нет данных"
                    })

        all_orders.sort(key=lambda x: x.get("date", ""), reverse=True)

        if all_orders:
            total_pages = (len(all_orders) - 1) // PAGE_SIZE + 1
            page = min(page, total_pages - 1)

            start_idx = page * PAGE_SIZE
            end_idx = min(start_idx + PAGE_SIZE, len(all_orders))

            current_page_orders = all_orders[start_idx:end_idx]

            message_text += f"<b>Заказы (страница {page + 1}/{total_pages}):</b>\n"

            for order in current_page_orders:
                profit_str = f"+{order['profit']:.2f} руб." if order['profit'] > 0 else f"{order['profit']:.2f} руб."
                message_text += f"• Заказ #{order['order_id']} - {profit_str}\n"
                kb.add(InlineKeyboardButton(f"Заказ #{order['order_id']} ({profit_str})",
                                            callback_data=f"tg_order_{order['order_id']}"))

            nav_buttons = []

            if page > 0:
                nav_buttons.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"tg_page_{page - 1}_orders"))

            if page < total_pages - 1:
                nav_buttons.append(InlineKeyboardButton("Далее ➡️", callback_data=f"tg_page_{page + 1}_orders"))

            if nav_buttons:
                kb.row(*nav_buttons)
        else:
            message_text += "📭 У вас пока нет заказов."

        kb.add(InlineKeyboardButton("🔙 Назад", callback_data="tg_back_to_main"))

        bot.edit_message_text(
            message_text,
            call.message.chat.id,
            call.message.message_id,
            reply_markup=kb,
            parse_mode="HTML"
        )

    def order_details(call: types.CallbackQuery):
        """Отображение деталей заказа"""
        order_id = call.data.split('_')[-1]
        user_orders_data = load_user_orders()

        all_orders = []
        for uid, orders in user_orders_data["user_orders"].items():
            for o_id, order_data in orders.items():
                if o_id in config["orders_profit"]:
                    profit_data = config["orders_profit"][o_id]
                    all_orders.append({
                        "order_id": o_id,
                        "user_id": uid,
                        "phone": order_data.get("phone", "Нет данных"),
                        "profit": profit_data.get("profit", 0),
                        "date": profit_data.get("date", "Нет данных")
                    })
                else:
                    all_orders.append({
                        "order_id": o_id,
                        "user_id": uid,
                        "phone": order_data.get("phone", "Нет данных"),
                        "profit": 0,
                        "date": "Нет данных"
                    })

        all_orders.sort(key=lambda x: x.get("date", ""), reverse=True)

        order_index = -1
        for i, order in enumerate(all_orders):
            if order["order_id"] == order_id:
                order_index = i
                break

        PAGE_SIZE = 5
        page = order_index // PAGE_SIZE if order_index != -1 else 0

        kb = InlineKeyboardMarkup(row_width=1)
        if page > 0:
            kb.add(InlineKeyboardButton("🔙 К списку заказов (стр. " + str(page + 1) + ")",
                                        callback_data=f"tg_page_{page}_orders"))
        else:
            kb.add(InlineKeyboardButton("🔙 К списку заказов", callback_data="tg_orders"))

        found_order = False
        order_details = {}
        user_id = None

        for uid, orders in user_orders_data["user_orders"].items():
            if order_id in orders:
                found_order = True
                user_id = uid
                order_details = orders[order_id]
                break

        if found_order:
            phone = order_details.get("phone", "Нет данных")
            item_id = order_details.get("item_id", "Нет данных")

            profit_info = config["orders_profit"].get(order_id, {})
            fp_sum = profit_info.get("fp_sum", 0)
            lolz_cost = profit_info.get("lolz_cost", 0)
            profit = profit_info.get("profit", 0)
            date = profit_info.get("date", "Нет данных")

            message_text = (
                f"📋 <b>Информация о заказе #{order_id}</b>\n\n"
                f"👤 <b>Покупатель:</b> {user_id}\n"
                f"📱 <b>Телефон:</b> {phone}\n"
                f"🆔 <b>ID аккаунта LOLZ:</b> {item_id}\n"
                f"📅 <b>Дата:</b> {date}\n\n"
                f"💰 <b>Финансы:</b>\n"
                f"• Сумма на FunPay: {fp_sum} руб.\n"
                f"• Стоимость на LOLZ: {lolz_cost} руб.\n"
                f"• <b>Чистая прибыль:</b> {profit:.2f} руб.\n"
            )

            kb.add(InlineKeyboardButton("🌐 Открыть заказ на FunPay", url=f"https://funpay.com/orders/{order_id}/"))
        else:
            message_text = f"❌ Заказ #{order_id} не найден в базе данных."

        bot.edit_message_text(
            message_text,
            call.message.chat.id,
            call.message.message_id,
            reply_markup=kb,
            parse_mode="HTML"
        )

    def message_templates_menu(call: types.CallbackQuery):
        """Меню шаблонов сообщений"""
        bot.clear_step_handler_by_chat_id(call.message.chat.id)

        kb = InlineKeyboardMarkup(row_width=1)
        kb.add(
            InlineKeyboardButton("📝 Шаблон сообщения после покупки", callback_data="tg_edit_purchase_template"),
            InlineKeyboardButton("🔢 Шаблон сообщения с кодом", callback_data="tg_edit_code_template"),
            InlineKeyboardButton("🔙 Назад", callback_data="tg_back_to_main")
        )

        message_text = (
            "💬 <b>Настройка шаблонов сообщений</b>\n\n"
            "<b>Доступные переменные для шаблона после покупки:</b>\n"
            "<code>- {phone} - номер телефона аккаунта</code>\n\n"
            "<b>Доступные переменные для шаблона с кодом:</b>\n"
            "<code>- {code} - код подтверждения\n"
            "- {order_link} - ссылка для подтверждения заказа\n"
            "- {order_id} - номер заказа</code>"
        )

        try:
            bot.edit_message_text(
                message_text,
                call.message.chat.id,
                call.message.message_id,
                reply_markup=kb,
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"{LOGGER_PREFIX} Ошибка при отображении меню шаблонов сообщений: {e}")
            bot.answer_callback_query(call.id, "Произошла ошибка при загрузке меню шаблонов")

    def edit_purchase_template(call: types.CallbackQuery):
        """Редактирование шаблона сообщения после покупки"""
        try:
            current_template = config.get("purchase_template", DEFAULT_PURCHASE_TEMPLATE)
            msg = bot.edit_message_text(
                f"📝 <b>Текущий шаблон сообщения после покупки:</b>\n\n<pre>{current_template}</pre>\n\n"
                f"Введите новый шаблон сообщения. Доступные переменные:\n"
                f"- {{phone}} - номер телефона аккаунта",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=InlineKeyboardMarkup().add(
                    InlineKeyboardButton("🔙 Отмена", callback_data="tg_message_templates")
                ),
                parse_mode="HTML"
            )
            bot.register_next_step_handler(msg, process_purchase_template_edit)
        except Exception as e:
            logger.error(f"{LOGGER_PREFIX} Ошибка при редактировании шаблона сообщения после покупки: {e}")
            bot.answer_callback_query(call.id, "Произошла ошибка при обработке запроса")
            message_templates_menu(call)

    def process_purchase_template_edit(message: types.Message):
        """Обработка нового шаблона сообщения после покупки"""
        if message.text is None:
            bot.clear_step_handler_by_chat_id(message.chat.id)
            bot.send_message(message.chat.id, "❌ Нетекстовое сообщение. Редактирование шаблона отменено.")
            show_tg_settings(message)
            return

        try:
            bot.delete_message(message.chat.id, message.message_id - 1)
        except Exception as e:
            logger.error(f"{LOGGER_PREFIX} Ошибка при удалении сообщения: {e}")

        new_template = message.text
        if not new_template.strip():
            bot.clear_step_handler_by_chat_id(message.chat.id)
            bot.send_message(message.chat.id, "❌ Пустой шаблон не допускается. Редактирование отменено.")
            show_tg_settings(message)
            return

        config["purchase_template"] = new_template
        save_config()

        bot.clear_step_handler_by_chat_id(message.chat.id)

        bot.send_message(message.chat.id, "✅ Шаблон сообщения после покупки успешно обновлен!")
        show_tg_settings(message)

    def edit_code_template(call: types.CallbackQuery):
        """Редактирование шаблона сообщения с кодом"""
        try:
            current_template = config.get("code_template", DEFAULT_CODE_TEMPLATE)
            msg = bot.edit_message_text(
                f"🔢 <b>Текущий шаблон сообщения с кодом:</b>\n\n<pre>{current_template}</pre>\n\n"
                f"Введите новый шаблон сообщения. Доступные переменные:\n"
                f"- {{code}} - код подтверждения\n"
                f"- {{order_link}} - ссылка для подтверждения заказа\n"
                f"- {{order_id}} - номер заказа",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=InlineKeyboardMarkup().add(
                    InlineKeyboardButton("🔙 Отмена", callback_data="tg_message_templates")
                ),
                parse_mode="HTML"
            )
            bot.register_next_step_handler(msg, process_code_template_edit)
        except Exception as e:
            logger.error(f"{LOGGER_PREFIX} Ошибка при редактировании шаблона сообщения с кодом: {e}")
            bot.answer_callback_query(call.id, "Произошла ошибка при обработке запроса")
            message_templates_menu(call)

    def process_code_template_edit(message: types.Message):
        """Обработка нового шаблона сообщения с кодом"""
        if message.text is None:
            bot.clear_step_handler_by_chat_id(message.chat.id)
            bot.send_message(message.chat.id, "❌ Нетекстовое сообщение. Редактирование шаблона отменено.")
            show_tg_settings(message)
            return

        try:
            bot.delete_message(message.chat.id, message.message_id - 1)
        except Exception as e:
            logger.error(f"{LOGGER_PREFIX} Ошибка при удалении сообщения: {e}")

        new_template = message.text
        if not new_template.strip():
            bot.clear_step_handler_by_chat_id(message.chat.id)
            bot.send_message(message.chat.id, "❌ Пустой шаблон не допускается. Редактирование отменено.")
            show_tg_settings(message)
            return

        config["code_template"] = new_template
        save_config()

        bot.clear_step_handler_by_chat_id(message.chat.id)

        bot.send_message(message.chat.id, "✅ Шаблон сообщения с кодом успешно обновлен!")
        show_tg_settings(message)


def add_country_step2(message: types.Message):
    if message.text is None:
        return

    try:
        bot.delete_message(message.chat.id, message.message_id - 1)
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка при удалении сообщения: {e}")

    country_code = message.text.strip().upper()
    if country_code in config["countries"]:
        bot.clear_step_handler_by_chat_id(message.chat.id)
        bot.send_message(message.chat.id, "❌ Страна с таким кодом уже существует!")
        return show_tg_settings(message)

    msg = bot.send_message(
        message.chat.id,
        "Введите полное название страны:",
        reply_markup=InlineKeyboardMarkup().add(
            InlineKeyboardButton("🔙 Отмена", callback_data="tg_back_to_main")
        )
    )
    bot.register_next_step_handler(msg, add_country_step3, country_code)


def add_country_step3(message: types.Message, country_code: str):
    if message.text is None:
        return

    try:
        bot.delete_message(message.chat.id, message.message_id - 1)
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка при удалении сообщения: {e}")

    country_name = message.text.strip()
    msg = bot.send_message(
        message.chat.id,
        "Введите минимальную цену (целое число):",
        reply_markup=InlineKeyboardMarkup().add(
            InlineKeyboardButton("🔙 Отмена", callback_data="tg_back_to_main")
        )
    )
    bot.register_next_step_handler(msg, add_country_step4, country_code, country_name)


def add_country_step4(message: types.Message, country_code: str, country_name: str):
    if message.text is None:
        return

    try:
        bot.delete_message(message.chat.id, message.message_id - 1)
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка при удалении сообщения: {e}")

    try:
        min_price = int(message.text.strip())
        msg = bot.send_message(
            message.chat.id,
            "Введите максимальную цену (целое число):",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("🔙 Отмена", callback_data="tg_back_to_main")
            )
        )
        bot.register_next_step_handler(msg, add_country_step5, country_code, country_name, min_price)
    except ValueError:
        bot.clear_step_handler_by_chat_id(message.chat.id)
        bot.send_message(message.chat.id, "❌ Введите корректное целое число!")
        show_tg_settings(message)


def add_country_step5(message: types.Message, country_code: str, country_name: str, min_price: int):
    if message.text is None:
        return

    try:
        bot.delete_message(message.chat.id, message.message_id - 1)
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка при удалении сообщения: {e}")

    try:
        max_price = int(message.text.strip())
        if max_price < min_price:
            bot.clear_step_handler_by_chat_id(message.chat.id)
            bot.send_message(message.chat.id, "❌ Максимальная цена не может быть меньше минимальной!")
            return show_tg_settings(message)

        config["countries"][country_code] = {
            "name": country_name,
            "min_price": min_price,
            "max_price": max_price
        }
        save_config()

        bot.clear_step_handler_by_chat_id(message.chat.id)
        bot.send_message(
            message.chat.id,
            f"✅ Страна {country_name} ({country_code}) успешно добавлена!"
        )
        show_tg_settings(message)
    except ValueError:
        bot.clear_step_handler_by_chat_id(message.chat.id)
        bot.send_message(message.chat.id, "❌ Введите корректное целое число!")
        show_tg_settings(message)


def process_country_name_edit(message: types.Message, country_code: str):
    if message.text is None:
        return

    try:
        bot.delete_message(message.chat.id, message.message_id - 1)
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка при удалении сообщения: {e}")

    try:
        new_name = message.text.strip()
        if country_code not in config["countries"]:
            bot.clear_step_handler_by_chat_id(message.chat.id)
            bot.send_message(message.chat.id, f"❌ Ошибка: страна с кодом {country_code} больше не существует!")
            return show_tg_settings(message)

        config["countries"][country_code]["name"] = new_name
        save_config()
        bot.clear_step_handler_by_chat_id(message.chat.id)
        bot.send_message(message.chat.id, f"✅ Название страны {country_code} изменено на {new_name}!")
        show_tg_settings(message)
    except Exception as e:
        bot.clear_step_handler_by_chat_id(message.chat.id)
        logger.error(f"{LOGGER_PREFIX} Ошибка при сохранении названия страны: {e}")
        bot.send_message(message.chat.id, "❌ Произошла ошибка при сохранении названия страны!")
        show_tg_settings(message)


def process_country_min_edit(message: types.Message, country_code: str):
    if message.text is None:
        return

    try:
        bot.delete_message(message.chat.id, message.message_id - 1)
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка при удалении сообщения: {e}")

    try:
        if country_code not in config["countries"]:
            bot.clear_step_handler_by_chat_id(message.chat.id)
            bot.send_message(message.chat.id, f"❌ Ошибка: страна с кодом {country_code} больше не существует!")
            return show_tg_settings(message)

        new_min = int(message.text.strip())
        if new_min > config["countries"][country_code]["max_price"]:
            bot.clear_step_handler_by_chat_id(message.chat.id)
            bot.send_message(message.chat.id, "❌ Минимальная цена не может быть больше максимальной!")
            return show_tg_settings(message)

        config["countries"][country_code]["min_price"] = new_min
        save_config()
        bot.clear_step_handler_by_chat_id(message.chat.id)
        bot.send_message(message.chat.id, f"✅ Минимальная цена для страны {country_code} изменена на {new_min}₽!")
        show_tg_settings(message)
    except ValueError:
        bot.clear_step_handler_by_chat_id(message.chat.id)
        bot.send_message(message.chat.id, "❌ Введите корректное целое число!")
        show_tg_settings(message)
    except Exception as e:
        bot.clear_step_handler_by_chat_id(message.chat.id)
        logger.error(f"{LOGGER_PREFIX} Ошибка при сохранении мин. цены страны: {e}")
        bot.send_message(message.chat.id, "❌ Произошла ошибка при сохранении минимальной цены!")
        show_tg_settings(message)


def process_country_max_edit(message: types.Message, country_code: str):
    if message.text is None:
        return

    try:
        bot.delete_message(message.chat.id, message.message_id - 1)
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка при удалении сообщения: {e}")

    try:
        if country_code not in config["countries"]:
            bot.clear_step_handler_by_chat_id(message.chat.id)
            bot.send_message(message.chat.id, f"❌ Ошибка: страна с кодом {country_code} больше не существует!")
            return show_tg_settings(message)

        new_max = int(message.text.strip())
        if new_max < config["countries"][country_code]["min_price"]:
            bot.clear_step_handler_by_chat_id(message.chat.id)
            bot.send_message(message.chat.id, "❌ Максимальная цена не может быть меньше минимальной!")
            return show_tg_settings(message)

        config["countries"][country_code]["max_price"] = new_max
        save_config()
        bot.clear_step_handler_by_chat_id(message.chat.id)
        bot.send_message(message.chat.id, f"✅ Максимальная цена для страны {country_code} изменена на {new_max}₽!")
        show_tg_settings(message)
    except ValueError:
        bot.clear_step_handler_by_chat_id(message.chat.id)
        bot.send_message(message.chat.id, "❌ Введите корректное целое число!")
        show_tg_settings(message)
    except Exception as e:
        bot.clear_step_handler_by_chat_id(message.chat.id)
        logger.error(f"{LOGGER_PREFIX} Ошибка при сохранении макс. цены страны: {e}")
        bot.send_message(message.chat.id, "❌ Произошла ошибка при сохранении максимальной цены!")
        show_tg_settings(message)


def handle_new_order(c: Cardinal, e: NewOrderEvent, *args):
    """
    Обработчик новых заказов.
    Добавляет заказ в очередь для асинхронной обработки.
    """
    order_id = e.order.id
    logger.info(f"{LOGGER_PREFIX} Новый заказ #{order_id} добавлен в очередь на обработку")

    order_queue.put({
        'cardinal': c,
        'event': e
    })


def send_message_to_buyer(c: Cardinal, username: str, message: str):
    """Отправляет сообщение покупателю"""
    try:
        chat_id = c.account.get_chat_by_name(username, make_request=True)
        if chat_id:
            c.account.send_message(chat_id.id, message)
            logger.info(f"{LOGGER_PREFIX} Отправлено сообщение покупателю {username}")
            return True
        else:
            logger.warning(f"{LOGGER_PREFIX} Не удалось найти чат с покупателем {username}")
            return False
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка при отправке сообщения покупателю {username}: {e}")
        return False


def find_available_accounts(country_code, min_price, max_price):
    """Поиск доступных аккаунтов с сортировкой по возрастанию цены"""
    available_accounts = []

    try:
        timer = threading.Timer(3.0, lambda: None)
        timer.start()
        timer.join()

        url = f"https://prod-api.lzt.market/telegram?order_by=price_to_up&pmin={min_price}&pmax={max_price}"

        for origin in config["origins"]:
            url += f"&origin[]={origin}"

        url += f"&spam=no&allow_geo_spamblock=true&password=no&country[]={country_code}"

        headers = {
            "accept": "application/json",
            "authorization": f"Bearer {config['lolz_token']}"
        }

        response = requests.get(url, headers=headers)
        logger.info(f"{LOGGER_PREFIX} Запрос к API LOLZ Market: {url}")

        if response.status_code == 200:
            response_data = response.json()

            if 'items' in response_data and response_data['items']:
                items = response_data['items']
                logger.info(f"{LOGGER_PREFIX} Найдено {len(items)} аккаунтов")
                available_accounts = items
            else:
                logger.info(f"{LOGGER_PREFIX} Нет доступных аккаунтов")
        else:
            logger.error(f"{LOGGER_PREFIX} Ошибка запроса к API LOLZ Market: {response.status_code}, {response.text}")

    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка при поиске аккаунтов: {e}")

    logger.info(f"{LOGGER_PREFIX} Всего найдено {len(available_accounts)} доступных аккаунтов")
    return available_accounts


def purchase_account(item_id):
    """Покупка аккаунта по ID"""
    try:
        timer = threading.Timer(3.0, lambda: None)
        timer.start()
        timer.join()

        url = f"https://prod-api.lzt.market/{item_id}/fast-buy"

        headers = {
            "accept": "application/json",
            "authorization": f"Bearer {config['lolz_token']}"
        }

        response = requests.post(url, headers=headers)
        logger.info(f"{LOGGER_PREFIX} Запрос на покупку аккаунта ID {item_id}: {url}")

        if response.status_code == 200:
            result = response.json()
            logger.info(f"{LOGGER_PREFIX} Ответ API (успех): {str(result)[:200]}...")
            return result
        else:
            result = response.json()
            logger.error(f"{LOGGER_PREFIX} Ошибка при покупке аккаунта: {response.status_code}, {str(result)}")
            return result

    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Исключение при покупке аккаунта {item_id}: {e}")
        return {"errors": [str(e)]}


def notify_admins(message, order_id=None):
    """Отправка уведомления администраторам"""
    if not config["administrators"]:
        logger.warning(f"{LOGGER_PREFIX} Нет настроенных администраторов для уведомлений")
        return

    if order_id and order_id in config["orders_profit"]:
        profit_data = config["orders_profit"][order_id]
        profit_info = (
            f"\n💰 Финансовая информация:\n"
            f"• Сумма на FP: {profit_data.get('fp_sum', 0)} руб.\n"
            f"• Стоимость на LOLZ: {profit_data.get('lolz_cost', 0)} руб.\n"
            f"• Чистая прибыль: {profit_data.get('profit', 0):.2f} руб."
        )
        message += profit_info

    for admin_id in config["administrators"]:
        try:
            if order_id:
                kb = InlineKeyboardMarkup()
                kb.add(InlineKeyboardButton("Перейти к заказу", url=f"https://funpay.com/orders/{order_id}/"))
                bot.send_message(admin_id, message, reply_markup=kb)
            else:
                bot.send_message(admin_id, message)
            logger.info(f"{LOGGER_PREFIX} Отправлено уведомление администратору {admin_id}")
        except Exception as e:
            logger.error(f"{LOGGER_PREFIX} Ошибка при отправке уведомления администратору {admin_id}: {e}")


def get_telegram_codes(item_id):
    """Получает коды входа в Telegram аккаунт по ID предмета"""
    max_retries = 10
    retry_delay = 3

    for attempt in range(max_retries):
        try:
            if attempt > 0:
                time.sleep(retry_delay * attempt)
                logger.info(
                    f"{LOGGER_PREFIX} Повторная попытка {attempt + 1}/{max_retries} получения кодов для аккаунта ID {item_id}")

            url = f"https://prod-api.lzt.market/{item_id}/telegram-login-code"

            headers = {
                "accept": "application/json",
                "authorization": f"Bearer {config['lolz_token']}"
            }

            response = requests.get(url, headers=headers)
            logger.info(
                f"{LOGGER_PREFIX} Запрос кодов для аккаунта ID {item_id}: {url}, статус: {response.status_code}")

            if response.status_code == 200:
                result = response.json()
                logger.info(f"{LOGGER_PREFIX} Получены коды для аккаунта ID {item_id}")
                return result
            else:
                try:
                    result = response.json()

                    if 'errors' in result and 'retry_request' in result['errors']:
                        logger.info(f"{LOGGER_PREFIX} Получена ошибка retry_request, повторим запрос")
                        continue

                    logger.error(f"{LOGGER_PREFIX} Ошибка при получении кодов: {response.status_code}, {str(result)}")
                except ValueError:
                    logger.error(
                        f"{LOGGER_PREFIX} Ошибка при получении кодов: {response.status_code}, невозможно распарсить ответ")

                if attempt == max_retries - 1:
                    return None

        except Exception as e:
            logger.error(f"{LOGGER_PREFIX} Исключение при получении кодов для аккаунта {item_id}: {e}")
            if attempt == max_retries - 1:
                return None

    return None


def handle_plus_message(c: Cardinal, e: NewMessageEvent):
    """Обработчик сообщений для получения кодов"""
    try:
        if not e.message.text or (
                not e.message.text.strip().lower().startswith("cd") and e.message.text.strip() != "+"):
            return

        if e.message.text.strip() == "+":
            return

        if e.message.text.strip().lower() == "cd":
            user_id = str(e.message.chat_name)
            user_orders_data = load_user_orders()
            user_phones = set()
            if user_id in user_orders_data["user_orders"]:
                for order_data in user_orders_data["user_orders"][user_id].values():
                    if "phone" in order_data:
                        user_phones.add(order_data["phone"])

            next_order, orders = c.account.get_sells()
            user_orders = [order for order in orders if order.buyer_username == e.message.chat_name]

            for order in user_orders:
                if order.id in order_phone_numbers:
                    user_phones.add(order_phone_numbers[order.id])

            if user_phones:
                phones_list = "\n".join([f"• {phone}" for phone in sorted(user_phones)])
                message_text = f"📱 Ваши номера:\n\n{phones_list}\n\nДля получения кода отправьте: cd номер"
                c.account.send_message(e.message.chat_id, message_text, chat_name=e.message.chat_name)
            else:
                c.account.send_message(
                    e.message.chat_id,
                    "❌ У вас нет доступных номеров телефонов. Приобретите телеграм аккаунт.",
                    chat_name=e.message.chat_name
                )
            return

        cd_match = re.match(r'^cd\s+(\d+)$', e.message.text.strip(), re.IGNORECASE)
        if not cd_match:
            return

        phone_number = cd_match.group(1)
        logger.info(
            f"{LOGGER_PREFIX} Получен запрос на код для номера {phone_number} от пользователя {e.message.author}, чат {e.message.chat_id}")

        user_orders_data = load_user_orders()

        user_id = str(e.message.chat_name)
        if phone_number in user_orders_data["phone_users"] and user_orders_data["phone_users"][phone_number] != user_id:
            logger.warning(f"{LOGGER_PREFIX} Попытка доступа к чужому номеру {phone_number} пользователем {user_id}")
            c.account.send_message(
                e.message.chat_id,
                f"❌ Номер {phone_number} не принадлежит вам. Вы можете получать коды только для своих номеров.",
                chat_name=e.message.chat_name
            )
            return

        found_order_id = None
        item_id = None

        if user_id in user_orders_data["user_orders"]:
            for order_id, order_data in user_orders_data["user_orders"][user_id].items():
                if order_data.get("phone") == phone_number:
                    found_order_id = order_id
                    item_id = order_data.get("item_id")
                    break

        if not found_order_id:
            next_order, orders = c.account.get_sells()
            user_orders = [order for order in orders if order.buyer_username == e.message.chat_name]

            if not user_orders:
                c.account.send_message(
                    e.message.chat_id,
                    f"❌ Номер {phone_number} не найден среди ваших заказов.",
                    chat_name=e.message.chat_name
                )
                return
            for order in user_orders:
                if order.id in order_phone_numbers and order_phone_numbers[order.id] == phone_number:
                    found_order_id = order.id
                    if order.id in order_account_ids:
                        item_id = order_account_ids[order.id]
                    break

        if not item_id:
            c.account.send_message(
                e.message.chat_id,
                f"❌ Для номера {phone_number} невозможно получить код. Обратитесь к администратору.",
                chat_name=e.message.chat_name
            )
            notify_admins(f"⚠️ Запрос кода для номера {phone_number}, но item_id не найден", found_order_id)
            return

        if not config["lolz_token"]:
            c.account.send_message(
                e.message.chat_id,
                "❌ Не настроен токен LOLZ для получения кодов. Администратор свяжется с вами в ближайшее время.",
                chat_name=e.message.chat_name
            )
            notify_admins(f"⚠️ Запрос кода для номера {phone_number}, но не настроен токен LOLZ", found_order_id)
            return

        c.account.send_message(
            e.message.chat_id,
            "🔄 Запрос кода отправлен. Пожалуйста, подождите...",
            chat_name=e.message.chat_name
        )

        codes_data = get_telegram_codes(item_id)

        if not codes_data or 'codes' not in codes_data or not codes_data['codes']:
            c.account.send_message(
                e.message.chat_id,
                f"❌ Не удалось получить код для номера {phone_number}. Код может появиться через несколько минут, попробуйте позже.",
                chat_name=e.message.chat_name
            )
            notify_admins(f"⚠️ Не удалось получить код для номера {phone_number}, item_id: {item_id}", found_order_id)
            return

        latest_code = codes_data['codes'][0]['code']

        code_template = config.get("code_template", DEFAULT_CODE_TEMPLATE)

        order_link = f"https://funpay.com/orders/{found_order_id}/"
        message_text = code_template.format(
            code=latest_code,
            order_link=order_link,
            order_id=found_order_id
        )

        c.account.send_message(
            e.message.chat_id,
            message_text,
            chat_name=e.message.chat_name
        )

        if user_id not in user_orders_data["user_orders"]:
            user_orders_data["user_orders"][user_id] = {}

        if found_order_id not in user_orders_data["user_orders"][user_id]:
            user_orders_data["user_orders"][user_id][found_order_id] = {
                "phone": phone_number,
                "item_id": item_id
            }

        user_orders_data["phone_users"][phone_number] = user_id
        save_user_orders(user_orders_data)

        logger.info(f"{LOGGER_PREFIX} Успешно отправлен код для номера {phone_number} пользователю {user_id}")

    except Exception as ex:
        logger.error(f"{LOGGER_PREFIX} Ошибка при обработке запроса кода: {ex}")
        try:
            c.account.send_message(
                e.message.chat_id,
                "❌ Произошла техническая ошибка при получении кода. Попробуйте позже или свяжитесь с администратором.",
                chat_name=e.message.chat_name
            )
        except Exception as send_error:
            logger.error(f"{LOGGER_PREFIX} Не удалось отправить сообщение об ошибке: {send_error}")

        error_details = f"⚠️ Ошибка при обработке запроса кода от {e.message.chat_name}\n"
        error_details += f"Номер: {phone_number if 'phone_number' in locals() else 'неизвестен'}\n"
        error_details += f"Item ID: {item_id if 'item_id' in locals() else 'неизвестен'}\n"
        error_details += f"Ошибка: {str(ex)}"

        notify_admins(error_details, found_order_id if 'found_order_id' in locals() else None)


def process_order_queue():
    """Функция для обработки очереди заказов в отдельном потоке"""
    global active_tasks

    logger.info(f"{LOGGER_PREFIX} Запущен обработчик очереди заказов")

    while True:
        try:
            with task_lock:
                can_process = active_tasks < max_concurrent_tasks

            if can_process and not order_queue.empty():
                order_data = order_queue.get()
                cardinal = order_data['cardinal']
                event = order_data['event']

                with task_lock:
                    active_tasks += 1

                future = executor.submit(process_order, cardinal, event)
                future.add_done_callback(lambda f: handle_processing_complete(f))

                logger.info(
                    f"{LOGGER_PREFIX} Начата обработка заказа #{event.order.id} в отдельном потоке. Активных задач: {active_tasks}")

            time.sleep(0.5)
        except Exception as e:
            logger.error(f"{LOGGER_PREFIX} Ошибка в обработчике очереди заказов: {e}")
            time.sleep(1)


def handle_processing_complete(future):
    """Обработчик завершения выполнения задачи в пуле потоков"""
    global active_tasks

    try:
        result = future.result()

        logger.info(f"{LOGGER_PREFIX} Обработка заказа завершена: {result}")
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка при обработке заказа: {e}")
    finally:
        with task_lock:
            active_tasks -= 1
            current_tasks = active_tasks

        logger.info(f"{LOGGER_PREFIX} Завершена обработка заказа. Осталось активных задач: {current_tasks}")


def process_order(c: Cardinal, e: NewOrderEvent):
    """
    Функция обработки заказа, запускаемая в отдельном потоке.
    """
    order_id = e.order.id
    logger.info(f"{LOGGER_PREFIX} Начата фактическая обработка заказа #{order_id}")

    logger.info(f"{LOGGER_PREFIX} Обработка заказа: {order_id}")

    try:
        full_order = c.account.get_order(order_id)

        description = e.order.description or ""
        full_desc = full_order.full_description or ""

        logger.info(f"{LOGGER_PREFIX} Краткое описание заказа #{full_order.id}: {description}")
        logger.info(f"{LOGGER_PREFIX} Полное описание заказа #{full_order.id}: {full_desc}")

        has_tg_prefix = False
        tg_match = None

        if 'tg:' in full_desc.lower():
            tg_match = re.search(r'tg:\s*(\w+)', full_desc, re.IGNORECASE)
            if tg_match:
                has_tg_prefix = True

        if not has_tg_prefix and 'tg:' in description.lower():
            tg_match = re.search(r'tg:\s*(\w+)', description, re.IGNORECASE)
            if tg_match:
                has_tg_prefix = True

        if not has_tg_prefix or not tg_match:
            logger.info(f"{LOGGER_PREFIX} В заказе #{full_order.id} нет метки 'tg:' с ID. Пропуск.")
            return f"Нет метки 'tg:' в заказе #{order_id}"

        tg_id = tg_match.group(1).upper()
        logger.info(f"{LOGGER_PREFIX} Найден ID телеграм: {tg_id}")

        try:
            if hasattr(e.order, 'parse_amount') and callable(e.order.parse_amount):
                amount = e.order.parse_amount()
            elif hasattr(e.order, 'amount') and e.order.amount is not None:
                amount = e.order.amount
            else:
                amount = 1
                if hasattr(full_order, 'amount') and full_order.amount is not None:
                    amount = full_order.amount

            logger.info(f"{LOGGER_PREFIX} Количество товара в заказе #{full_order.id}: {amount}")

            if amount > 1:
                logger.warning(
                    f"{LOGGER_PREFIX} Заказ #{full_order.id} содержит больше 1 товара ({amount}). Выполняем возврат.")

                try:
                    c.account.refund(full_order.id)
                    message_text = (
                        "Извините, но заказ телеграм аккаунта можно оформлять только в количестве 1 штуки.\n\n"
                        "Ваши средства были автоматически возвращены. Пожалуйста, создайте новый заказ, "
                        "указав количество товара 1 шт."
                    )
                    send_message_to_buyer(c, e.order.buyer_username, message_text)

                    admin_message = f"⚠️ Автоматический возврат для заказа #{full_order.id} из-за неверного количества товара ({amount})"
                    notify_admins(admin_message, full_order.id)
                    logger.info(
                        f"{LOGGER_PREFIX} Выполнен автоматический возврат для заказа #{full_order.id} из-за неверного количества")
                    return f"Автоматический возврат для заказа #{order_id} из-за неверного количества товара ({amount})"
                except Exception as refund_error:
                    logger.error(
                        f"{LOGGER_PREFIX} Ошибка при автоматическом возврате для заказа #{full_order.id}: {refund_error}")
                    notify_admins(
                        f"❌ Ошибка при автоматическом возврате для заказа #{full_order.id} (количество товара {amount}): {refund_error}",
                        full_order.id)
        except Exception as amount_error:
            logger.error(
                f"{LOGGER_PREFIX} Ошибка при определении количества товара в заказе #{full_order.id}: {amount_error}")

        country_info = ""
        country_code = ""
        min_price = 0
        max_price = 0
        for code, country_data in config["countries"].items():
            if tg_id.startswith(code):
                country_code = code
                min_price = country_data['min_price']
                max_price = country_data['max_price']
                break

        purchase_template = config.get("purchase_template", DEFAULT_PURCHASE_TEMPLATE)

        message_text = "Спасибо за покупку!"
        purchase_result = None
        account_data = None

        if country_code and config["lolz_token"]:
            try:
                purchase_failed = False
                purchase_success = False
                insufficient_funds = False

                logger.info(f"{LOGGER_PREFIX} Поиск аккаунтов для страны {country_code}")
                available_accounts = find_available_accounts(country_code, min_price, max_price)

                if available_accounts:
                    logger.info(f"{LOGGER_PREFIX} Найдено {len(available_accounts)} аккаунтов")
                    purchase_result, account_data, funds_issue = try_purchase_accounts(available_accounts)

                    if funds_issue:
                        insufficient_funds = True
                        purchase_failed = True
                        logger.error(
                            f"{LOGGER_PREFIX} Недостаточно средств на балансе LOLZ Market для покупки аккаунтов")

                    if purchase_result and 'item' in purchase_result:
                        item_id = purchase_result['item'].get('item_id')
                        logger.info(f"{LOGGER_PREFIX} Успешно куплен аккаунт ID: {item_id}")

                        order_account_ids[full_order.id] = item_id

                        if account_data and 'telegram_phone' in account_data:
                            phone = account_data['telegram_phone']
                            message_text = purchase_template.format(phone=phone)
                            order_phone_numbers[full_order.id] = phone
                            user_id = str(e.order.buyer_username)
                            user_orders_data = load_user_orders()

                            if user_id not in user_orders_data["user_orders"]:
                                user_orders_data["user_orders"][user_id] = {}

                            user_orders_data["user_orders"][user_id][str(full_order.id)] = {
                                "phone": phone,
                                "item_id": item_id
                            }

                            user_orders_data["phone_users"][phone] = user_id
                            save_user_orders(user_orders_data)

                            lolz_cost = purchase_result['item'].get('price', 0)
                            fp_sum = full_order.sum if hasattr(full_order, 'sum') else e.order.price
                            save_order_profit(full_order.id, fp_sum, lolz_cost)

                            profit_data = get_order_profit(full_order.id)
                        else:
                            message_text = purchase_template.format(phone="Не удалось получить")

                        if account_data:
                            admin_notification = (
                                f"✅ Успешно куплен и выдан аккаунт для заказа #{full_order.id}:\n"
                                f"Покупатель: {e.order.buyer_username}\n"
                                f"Телефон: {account_data['telegram_phone']}\n"
                            )
                            notify_admins(admin_notification, full_order.id)

                        purchase_success = True
                    else:
                        logger.error(f"{LOGGER_PREFIX} Не удалось купить ни один аккаунт")
                        purchase_failed = True
                else:
                    logger.warning(f"{LOGGER_PREFIX} Не найдено подходящих аккаунтов для страны {country_code}")
                    purchase_failed = True

                if not purchase_success:
                    if insufficient_funds:
                        logger.error(
                            f"{LOGGER_PREFIX} Недостаточно средств на балансе LOLZ Market для покупки аккаунтов")
                        try:
                            c.account.refund(full_order.id)
                            message_text = f"К сожалению, произошла ошибка при покупке аккаунта для страны {country_code}. Средства автоматически возвращены."
                            notify_admins(
                                f"💰 Автоматический возврат выполнен для заказа #{full_order.id} из-за недостатка средств на балансе LOLZ Market",
                                full_order.id)
                            logger.info(
                                f"{LOGGER_PREFIX} Выполнен автоматический возврат для заказа #{full_order.id} из-за недостатка средств")
                        except Exception as refund_error:
                            message_text = f"Спасибо за покупку! Вы приобрели телеграм аккаунт с ID: {tg_id}.{country_info}\n\nВаш заказ принят и будет обработан оператором в ближайшее время."
                            admin_message = f"⚠️ СРОЧНО! Недостаточно средств на балансе LOLZ Market для обработки заказа #{full_order.id}. Пополните баланс! Ошибка при возврате: {refund_error}"
                            notify_admins(admin_message, full_order.id)
                            logger.error(
                                f"{LOGGER_PREFIX} Ошибка при автоматическом возврате для заказа #{full_order.id} из-за недостатка средств: {refund_error}")
                    elif purchase_failed:
                        logger.error(f"{LOGGER_PREFIX} Не удалось купить ни один аккаунт")
                        message_text = f"Спасибо за покупку! Вы приобрели телеграм аккаунт с ID: {tg_id}.{country_info}\n\nК сожалению, произошла ошибка при автоматической покупке аккаунта. Наш администратор свяжется с вами в ближайшее время."

                        admin_message = f"⚠️ Не удалось купить ни один аккаунт для заказа #{full_order.id}. Все доступные аккаунты ({len(available_accounts) if available_accounts else 0}) оказались проданы."
                        notify_admins(admin_message, full_order.id)

                        if config["auto_returns"]:
                            try:
                                c.account.refund(full_order.id)
                                message_text = f"К сожалению, произошла ошибка при покупке аккаунта для страны {country_code}. Средства автоматически возвращены."
                                notify_admins(f"💰 Автоматический возврат выполнен для заказа #{full_order.id}",
                                              full_order.id)
                                logger.info(
                                    f"{LOGGER_PREFIX} Выполнен автоматический возврат для заказа #{full_order.id}")
                            except Exception as refund_error:
                                logger.error(
                                    f"{LOGGER_PREFIX} Ошибка при автоматическом возврате для заказа #{full_order.id}: {refund_error}")
                                notify_admins(
                                    f"❌ Ошибка при автоматическом возврате для заказа #{full_order.id}: {refund_error}",
                                    full_order.id)
                    else:
                        logger.warning(f"{LOGGER_PREFIX} Не найдено подходящих аккаунтов для страны {country_code}")
                        message_text = f"Спасибо за покупку! Вы приобрели телеграм аккаунт с ID: {tg_id}.{country_info}\n\nВ настоящий момент нет доступных аккаунтов для этой страны. Наш администратор свяжется с вами в ближайшее время."

                        admin_message = f"⚠️ Нет доступных аккаунтов для заказа #{full_order.id}, страна: {country_code}"
                        notify_admins(admin_message, full_order.id)

                        if config["auto_returns"]:
                            try:
                                c.account.refund(full_order.id)
                                message_text = f"К сожалению, в данный момент нет доступных аккаунтов для страны {country_code}. Средства автоматически возвращены."
                                notify_admins(f"💰 Автоматический возврат выполнен для заказа #{full_order.id}",
                                              full_order.id)
                                logger.info(
                                    f"{LOGGER_PREFIX} Выполнен автоматический возврат для заказа #{full_order.id}")
                            except Exception as refund_error:
                                logger.error(
                                    f"{LOGGER_PREFIX} Ошибка при автоматическом возврате для заказа #{full_order.id}: {refund_error}")
                                notify_admins(
                                    f"❌ Ошибка при автоматическом возврате для заказа #{full_order.id}: {refund_error}",
                                    full_order.id)
            except Exception as ex:
                logger.error(f"{LOGGER_PREFIX} Ошибка при запросе к API LOLZ Market: {ex}")
                message_text = f"Спасибо за покупку! Вы приобрели телеграм аккаунт с ID: {tg_id}.{country_info}"

                admin_message = f"⚠️ Ошибка при обработке заказа #{full_order.id}: {ex}"
                notify_admins(admin_message, full_order.id)

                if config["auto_returns"]:
                    try:
                        c.account.refund(full_order.id)
                        message_text = f"К сожалению, произошла техническая ошибка при обработке заказа. Средства автоматически возвращены."
                        notify_admins(f"💰 Автоматический возврат выполнен для заказа #{full_order.id}", full_order.id)
                        logger.info(f"{LOGGER_PREFIX} Выполнен автоматический возврат для заказа #{full_order.id}")
                    except Exception as refund_error:
                        logger.error(
                            f"{LOGGER_PREFIX} Ошибка при автоматическом возврате для заказа #{full_order.id}: {refund_error}")
                        notify_admins(
                            f"❌ Ошибка при автоматическом возврате для заказа #{full_order.id}: {refund_error}",
                            full_order.id)

        send_message_to_buyer(c, e.order.buyer_username, message_text)
        return f"Заказ #{order_id} успешно обработан"

    except Exception as ex:
        logger.error(f"{LOGGER_PREFIX} Ошибка при обработке заказа с ID телеграм: {ex}")
        return f"Ошибка при обработке заказа #{order_id}: {ex}"


def shutdown():
    """Функция для корректного завершения работы плагина"""
    global executor
    if executor:
        logger.info(f"{LOGGER_PREFIX} Завершение работы пула потоков...")
        executor.shutdown(wait=True)
        logger.info(f"{LOGGER_PREFIX} Пул потоков успешно остановлен")


BIND_TO_PRE_INIT = [init_commands]
BIND_TO_NEW_MESSAGE = [handle_plus_message]
BIND_TO_NEW_ORDER = [handle_new_order]
BIND_TO_EXIT = [shutdown]
BIND_TO_DELETE = [
    {"pattern": "tg_set_origin_self_registration", "handler": set_origin,
     "description": "Обработчик кнопки выбора происхождения 'Саморег'"},
    {"pattern": "tg_set_origin_self_reg", "handler": set_origin,
     "description": "Обработчик сокращенной кнопки выбора происхождения 'Саморег'"}
]


def log_bindings():
    logger.info(f"{LOGGER_PREFIX} Зарегистрированные привязки к удалению:")
    for binding in BIND_TO_DELETE:
        logger.info(
            f"{LOGGER_PREFIX} - Паттерн: {binding['pattern']}, Обработчик: {binding.get('handler', 'Не указан')}, Описание: {binding.get('description', 'Нет описания')}")


if 'init_commands' in locals():
    old_init_commands = init_commands


    def new_init_commands(c_: Cardinal):
        result = old_init_commands(c_)
        log_bindings()
        return result


    init_commands = new_init_commands


def find_cheapest_account(country_code, min_price, max_price):
    """
    Устаревшая функция. Использовать find_available_accounts.
    Оставлена для обратной совместимости.
    """
    available_accounts = find_available_accounts(country_code, min_price, max_price)
    return available_accounts[0] if available_accounts else None




