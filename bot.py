import logging
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters

from config import BOT_TOKEN, MANAGER_USERNAME, ADMIN_CHAT_ID, SHOP_NAME
from prices import PRICES, EXTRA_PRICES, PRICE_CATEGORIES

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "orders.db"
orders = {}

MAIN_MENU = InlineKeyboardMarkup([
    [InlineKeyboardButton("💰 Прайсы", callback_data="prices")],
    [InlineKeyboardButton("🛒 Заказать услугу", callback_data="cleanup")],
    [InlineKeyboardButton("📞 Связь", callback_data="contact")],
])

TIER_NAMES = ["0–100%", "50–100%", "80–100%"]
TIER_KEYS = ["0-100", "50-100", "80-100"]

RANK57_PRICES = {
    "Крутки": "КРУТКИ💫\n\n💫1 крутка — 30 рублей💫\n💫10 круток — 300 рублей💫\n💫100 круток — 3000 рублей💫",
    "ЭНДГЕЙМ": "ЭНДГЕЙМ🌟\n\n🌱10 этаж бездны — 120 рублей\n🌙11 этаж бездны — 150 рублей\n🌙12 этаж бездны — 300 рублей",
    "Натиск": "НАТИСК🌟\n\n🎂4 уровень сложности — 300 рублей\n🎂5 уровень сложности — 600 рублей\n\n(Дочистку временно не принимаю)",
    "Театр": "Театр🌟\n\nОбычная сложность (3 этапа) — 200₽🤩\nСредняя сложность (6 этапов) — 350₽🤩\nСложная сложность (9 этапов) — 450₽🤩🤩\nАрканы — 1000₽🤩",
}
RANK57_CATEGORIES = list(RANK57_PRICES.keys())
STATUS_LABELS = {"new": "🆕 Новый", "taken": "🟡 Взято", "working": "🔵 В работе", "done": "✅ Выполнен", "cancelled": "❌ Отменён"}


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            client_telegram_id INTEGER NOT NULL,
            client_name TEXT NOT NULL,
            uid TEXT NOT NULL,
            server TEXT NOT NULL,
            region TEXT NOT NULL,
            tier TEXT NOT NULL,
            price INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'new',
            manager_name TEXT DEFAULT '',
            service TEXT DEFAULT '',
            details TEXT DEFAULT ''
        )
    """)
    # Upgrade databases created by older versions.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(orders)").fetchall()}
    if "service" not in columns:
        conn.execute("ALTER TABLE orders ADD COLUMN service TEXT DEFAULT ''")
    if "details" not in columns:
        conn.execute("ALTER TABLE orders ADD COLUMN details TEXT DEFAULT ''")
    conn.commit()
    return conn


def region_keyboard(callback_prefix="region"):
    rows = [[InlineKeyboardButton(region, callback_data=f"{callback_prefix}:{region}")] for region in PRICES]
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="back")])
    return InlineKeyboardMarkup(rows)


def tier_keyboard(region):
    prices = PRICES[region]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"0–100% — {prices['0-100']} ₽", callback_data=f"tier:{region}:0")],
        [InlineKeyboardButton(f"50–100% — {prices['50-100']} ₽", callback_data=f"tier:{region}:1")],
        [InlineKeyboardButton(f"80–100% — {prices['80-100']} ₽", callback_data=f"tier:{region}:2")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="cleanup")],
    ])


def service_category_keyboard():
    """Сохраняет старую структуру меню услуг.

    Кнопка «Для 57+ рангов» открывает отдельный список из четырёх разделов.
    """
    rows = [
        [InlineKeyboardButton("🧹 Зачистка по регионам", callback_data="cleanup_regions")],
        [InlineKeyboardButton("Крутки", callback_data="service_category:0")],
        [InlineKeyboardButton("Задания легенд", callback_data="service_category:1")],
        [InlineKeyboardButton("Сюжет (одна глава)", callback_data="service_category:2")],
        [InlineKeyboardButton("Задания", callback_data="service_category:3")],
        [InlineKeyboardButton("Священный призыв семерых", callback_data="service_category:4")],
        [InlineKeyboardButton("Телепорты", callback_data="service_category:5")],
        [InlineKeyboardButton("Окулы", callback_data="service_category:6")],
        [InlineKeyboardButton("Эхо", callback_data="service_category:7")],
        [InlineKeyboardButton("Диковинки", callback_data="service_category:8")],
        [InlineKeyboardButton("Для 57+ рангов", callback_data="service_57")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back")],
    ]
    return InlineKeyboardMarkup(rows)


def rank57_keyboard(prefix):
    rows = [[InlineKeyboardButton(name, callback_data=f"{prefix}:{i}")] for i, name in enumerate(RANK57_CATEGORIES)]
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="cleanup" if prefix == "service57_category" else "prices")])
    return InlineKeyboardMarkup(rows)


def rank57_item_keyboard(category_index):
    category = RANK57_CATEGORIES[category_index]
    lines = [line.strip() for line in RANK57_PRICES[category].split("\n") if line.strip()]
    rows = []
    import re
    for i, line in enumerate(lines):
        if line == category or line.startswith("("):
            continue
        if re.search(r"\d+(?:\.5)?\s*(?:₽|руб(?:лей|ля)?|рубл(?:ей|я)?)", line, re.I):
            rows.append([InlineKeyboardButton(line[:60], callback_data=f"rank57_item:{category_index}:{i}")])
    rows.append([InlineKeyboardButton("⬅️ К услугам", callback_data="cleanup")])
    return InlineKeyboardMarkup(rows)


def rank57_item_data(category_index, item_index):
    import re
    if category_index < 0 or category_index >= len(RANK57_CATEGORIES):
        return None
    category = RANK57_CATEGORIES[category_index]
    lines = [line.strip() for line in RANK57_PRICES[category].split("\n") if line.strip()]
    if item_index < 0 or item_index >= len(lines):
        return None
    line = lines[item_index]
    if line == category or line.startswith("("):
        return None
    match = re.search(r"(\d+(?:\.5)?)\s*(?:₽|руб(?:лей|ля)?|рубл(?:ей|я)?)", line, re.I)
    if not match:
        return None
    price = int(float(match.group(1)))
    return category, line, price


def service_item_keyboard(category_index):
    category = PRICE_CATEGORIES[category_index]
    # Every line in the source price list becomes a selectable service.
    lines = [line.strip() for line in EXTRA_PRICES[category].split("\n") if line.strip()]
    rows = []
    for i, line in enumerate(lines):
        if line.startswith("Уточняйте в лс") or line.startswith("(Дочистку") or line.startswith("(Дочистку"):
            continue
        # Headers inside the 57+ section are not orderable items.
        if line in {"КРУТКИ", "ЭНДГЕЙМ", "НАТИСК", "Театр🌟"}:
            continue
        rows.append([InlineKeyboardButton(line[:60], callback_data=f"service_item:{category_index}:{i}")])
    rows.append([InlineKeyboardButton("⬅️ К услугам", callback_data="cleanup")])
    return InlineKeyboardMarkup(rows)


def service_item_data(category_index, item_index):
    category = PRICE_CATEGORIES[category_index]
    lines = [line.strip() for line in EXTRA_PRICES[category].split("\n") if line.strip()]
    if item_index < 0 or item_index >= len(lines):
        return None
    line = lines[item_index]
    # The source uses a few non-orderable explanatory lines.
    if line.startswith("Уточняйте в лс") or line.startswith("(") or line in {"КРУТКИ", "ЭНДГЕЙМ", "НАТИСК", "Театр🌟"}:
        return None
    import re
    match = re.search(r"(\d+(?:\.5)?)\s*(?:₽|руб(?:лей|ля)?|рубл(?:ей|я)?)", line, re.I)
    if not match:
        return None
    price = int(float(match.group(1)))
    return category, line, price


def cleanup_region_keyboard():
    return region_keyboard()


def price_region_keyboard():
    """Старое меню прайсов; «Для 57+ рангов» открывает четыре раздела."""
    rows = [
        [InlineKeyboardButton("🧹 Зачистка по регионам", callback_data="price_cleanup")],
        [InlineKeyboardButton("Крутки", callback_data="price_category:0")],
        [InlineKeyboardButton("Задания легенд", callback_data="price_category:1")],
        [InlineKeyboardButton("Сюжет (одна глава)", callback_data="price_category:2")],
        [InlineKeyboardButton("Задания", callback_data="price_category:3")],
        [InlineKeyboardButton("Священный призыв семерых", callback_data="price_category:4")],
        [InlineKeyboardButton("Телепорты", callback_data="price_category:5")],
        [InlineKeyboardButton("Окулы", callback_data="price_category:6")],
        [InlineKeyboardButton("Эхо", callback_data="price_category:7")],
        [InlineKeyboardButton("Диковинки", callback_data="price_category:8")],
        [InlineKeyboardButton("Для 57+ рангов", callback_data="price_57")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="back")],
    ]
    return InlineKeyboardMarkup(rows)


def price_category_keyboard():
    rows = []
    for i, category in enumerate(PRICE_CATEGORIES):
        rows.append([InlineKeyboardButton(category, callback_data=f"price_category:{i}")])
    rows.append([InlineKeyboardButton("⬅️ К прайсам", callback_data="prices")])
    return InlineKeyboardMarkup(rows)


def admin_keyboard(order_id, status="new"):
    rows = []
    if status == "new":
        rows.append([InlineKeyboardButton("🟡 Взять заказ", callback_data=f"order_status:{order_id}:taken")])
    if status in ("new", "taken"):
        rows.append([InlineKeyboardButton("🔵 В работу", callback_data=f"order_status:{order_id}:working")])
    if status in ("taken", "working"):
        rows.append([InlineKeyboardButton("✅ Выполнен", callback_data=f"order_status:{order_id}:done")])
        rows.append([InlineKeyboardButton("❌ Отменить", callback_data=f"order_status:{order_id}:cancelled")])
    return InlineKeyboardMarkup(rows) if rows else None


def format_admin_order(row):
    status = STATUS_LABELS.get(row["status"], row["status"])
    manager = f"\n👨‍💼 Менеджер: {row['manager_name']}" if row["manager_name"] else ""
    return (
        f"🧾 *Заказ №{row['id']}*\n\n"
        f"{status}\n"
        f"🕐 Создан: `{row['created_at']}`\n"
        f"👤 Клиент: {row['client_name']}\n"
        f"🆔 Telegram ID: `{row['client_telegram_id']}`\n"
        f"📍 Регион: *{row['region']}*\n"
        f"🛒 Услуга: *{row['service'] or 'Зачистка по регионам'}*\n"
        f"📦 Вариант: *{row['tier']}*\n"
        f"💰 Цена: *{row['price']} ₽*"
        + (f"\n📝 Детали: {row['details']}" if row['details'] else "")
        + manager
    )


def is_admin_chat(update: Update):
    return bool(ADMIN_CHAT_ID and update.effective_chat and update.effective_chat.id == ADMIN_CHAT_ID)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    orders.pop(update.effective_user.id, None)
    context.user_data.clear()
    await update.message.reply_text(f"🎮 *{SHOP_NAME}*\n\nВыбери нужный раздел:", reply_markup=MAIN_MENU, parse_mode="Markdown")


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    orders.pop(update.effective_user.id, None)
    context.user_data.clear()
    await update.message.reply_text(f"🎮 *{SHOP_NAME}*\n\nВыбери нужный раздел:", reply_markup=MAIN_MENU, parse_mode="Markdown")


async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id

    if data == "back":
        orders.pop(user_id, None)
        context.user_data.clear()
        await query.edit_message_text(f"🎮 *{SHOP_NAME}*\n\nВыбери нужный раздел:", reply_markup=MAIN_MENU, parse_mode="Markdown")
        return

    if data == "prices":
        await query.edit_message_text("💰 *Прайсы*\n\nВыбери раздел:", reply_markup=price_region_keyboard(), parse_mode="Markdown")
        return

    if data == "price_cleanup":
        await query.edit_message_text("🧹 *Прайс зачистки по регионам*\n\nВыбери регион:", reply_markup=region_keyboard("price_region"), parse_mode="Markdown")
        return

    if data.startswith("price_region:"):
        region = data.split(":", 1)[1]
        if region not in PRICES:
            await query.edit_message_text("❌ Регион не найден.", reply_markup=MAIN_MENU)
            return
        prices = PRICES[region]
        text = (f"💰 *{region}*\n\n"
                f"0–100% — *{prices['0-100']} ₽*\n"
                f"50–100% — *{prices['50-100']} ₽*\n"
                f"80–100% — *{prices['80-100']} ₽*")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ К зачистке", callback_data="price_cleanup")], [InlineKeyboardButton("🏠 Главное меню", callback_data="back")]])
        await query.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")
        return

    if data.startswith("price_category:"):
        try:
            index = int(data.split(":", 1)[1])
            category = PRICE_CATEGORIES[index]
        except (ValueError, IndexError):
            await query.edit_message_text("❌ Раздел не найден.", reply_markup=MAIN_MENU)
            return
        text = f"💰 *{category}*\n\n{EXTRA_PRICES[category]}"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ К прайсам", callback_data="prices")], [InlineKeyboardButton("🏠 Главное меню", callback_data="back")]])
        await query.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")
        return

    if data == "price_57":
        await query.edit_message_text(
            "💰 *Для 57+ рангов*\n\nВыбери раздел:",
            reply_markup=rank57_keyboard("rank57_price_category"),
            parse_mode="Markdown"
        )
        return

    if data.startswith("rank57_price_category:"):
        try:
            index = int(data.split(":", 1)[1])
            category = RANK57_CATEGORIES[index]
        except (ValueError, IndexError):
            await query.edit_message_text("❌ Раздел не найден.", reply_markup=price_region_keyboard())
            return
        await query.edit_message_text(
            f"💰 *{category}*\n\n{RANK57_PRICES[category]}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ К 57+", callback_data="price_57")], [InlineKeyboardButton("🏠 Главное меню", callback_data="back")]]),
            parse_mode="Markdown"
        )
        return

    if data == "service_57":
        orders[user_id] = {}
        context.user_data.clear()
        await query.edit_message_text(
            "🛒 *Для 57+ рангов*\n\nВыбери раздел:",
            reply_markup=rank57_keyboard("service57_category"),
            parse_mode="Markdown"
        )
        return

    if data.startswith("service57_category:"):
        try:
            index = int(data.split(":", 1)[1])
            category = RANK57_CATEGORIES[index]
        except (ValueError, IndexError):
            await query.edit_message_text("❌ Раздел не найден.", reply_markup=service_category_keyboard())
            return
        await query.edit_message_text(
            f"🛒 *{category}*\n\n{RANK57_PRICES[category]}\n\nВыбери вариант для заказа:",
            reply_markup=rank57_item_keyboard(index),
            parse_mode="Markdown"
        )
        return

    if data.startswith("rank57_item:"):
        try:
            _, category_index, item_index = data.split(":", 2)
            category_index, item_index = int(category_index), int(item_index)
            item = rank57_item_data(category_index, item_index)
        except (ValueError, IndexError):
            item = None
        if not item:
            await query.edit_message_text("❌ Услуга не найдена.", reply_markup=service_category_keyboard())
            return
        category, item_name, price = item
        orders[user_id] = {"service": category, "service_item": item_name, "price": price}
        context.user_data.clear()
        context.user_data["waiting_for_login"] = True
        await query.edit_message_text(
            f"🛒 *Заказ услуги*\n\n*{category}*\n{item_name}\n\n"
            "🔐 *Шаг 1 из 3*\n\n"
            "Отправь почту (email), привязанную к аккаунту.\n\n"
            "⚠️ Отправляй только данные от аккаунта, который используется для заказа.",
            parse_mode="Markdown"
        )
        return

    if data == "contact":
        username = MANAGER_USERNAME.strip().lstrip("@")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("💬 Написать менеджеру", url=f"https://t.me/{username}")], [InlineKeyboardButton("⬅️ Назад", callback_data="back")]])
        await query.edit_message_text(f"📞 *Связь с менеджером*\n\nМенеджер: @{username}", reply_markup=kb, parse_mode="Markdown")
        return

    if data == "cleanup":
        orders[user_id] = {}
        context.user_data.clear()
        await query.edit_message_text("🛒 *Заказать услугу*\n\nВыбери нужную услугу:", reply_markup=service_category_keyboard(), parse_mode="Markdown")
        return

    if data == "cleanup_regions":
        orders[user_id] = {"service": "Зачистка по регионам"}
        context.user_data.clear()
        await query.edit_message_text("🧹 *Зачистка по регионам*\n\nВыбери регион:", reply_markup=region_keyboard(), parse_mode="Markdown")
        return

    if data.startswith("service_category:"):
        try:
            category_index = int(data.split(":", 1)[1])
            category = PRICE_CATEGORIES[category_index]
        except (ValueError, IndexError):
            await query.edit_message_text("❌ Услуга не найдена.", reply_markup=MAIN_MENU)
            return
        await query.edit_message_text(
            f"🛒 *{category}*\n\n{EXTRA_PRICES[category]}\n\nВыбери вариант для заказа:",
            reply_markup=service_item_keyboard(category_index),
            parse_mode="Markdown"
        )
        return

    if data.startswith("service_item:"):
        try:
            _, category_index, item_index = data.split(":", 2)
            category_index, item_index = int(category_index), int(item_index)
            item = service_item_data(category_index, item_index)
        except (ValueError, IndexError):
            item = None
        if not item:
            await query.edit_message_text("❌ Этот пункт нельзя оформить автоматически. Свяжись с менеджером для уточнения.", reply_markup=service_category_keyboard())
            return
        category, item_name, price = item
        orders[user_id] = {"service": category, "service_item": item_name, "price": price}
        context.user_data.clear()
        context.user_data["waiting_for_login"] = True
        await query.edit_message_text(
            f"🛒 *Заказ услуги*\n\n*{category}*\n{item_name}\n\n"
            "🔐 *Шаг 1 из 3*\n\n"
            "Отправь почту (email), привязанную к аккаунту.\n\n"
            "⚠️ Отправляй только данные от аккаунта, который используется для заказа.",
            parse_mode="Markdown"
        )
        return

    if data.startswith("region:"):
        region = data.split(":", 1)[1]
        if region not in PRICES:
            await query.edit_message_text("❌ Регион не найден. Нажми /start.", reply_markup=MAIN_MENU)
            return
        orders.setdefault(user_id, {})["region"] = region
        if orders[user_id].get("service") == "Зачистка по регионам":
            await query.edit_message_text(f"🧹 *{region}*\n\nВыбери объём зачистки:", reply_markup=tier_keyboard(region), parse_mode="Markdown")
        else:
            await query.edit_message_text(f"🧹 *{region}*\n\nВыбери объём зачистки:", reply_markup=tier_keyboard(region), parse_mode="Markdown")
        return

    if data.startswith("tier:"):
        parts = data.split(":", 2)
        if len(parts) != 3 or parts[1] not in PRICES or parts[2] not in ("0", "1", "2"):
            await query.edit_message_text("❌ Ошибка выбора. Нажми /start.", reply_markup=MAIN_MENU)
            return
        region, tier_index = parts[1], int(parts[2])
        orders.setdefault(user_id, {})
        orders[user_id].update(region=region, tier_index=tier_index, price=PRICES[region][TIER_KEYS[tier_index]])
        orders[user_id].setdefault("service", "Зачистка по регионам")
        orders[user_id]["service_item"] = f"Зачистка {TIER_NAMES[tier_index]}"
        context.user_data.clear()
        context.user_data["waiting_for_login"] = True
        await query.edit_message_text(
            "🔐 *Шаг 1 из 3*\n\n"
            "Отправь почту (email), привязанную к аккаунту.\n\n"
            "⚠️ Отправляй только данные от аккаунта, который используется для заказа.",
            parse_mode="Markdown"
        )
        return


async def receive_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the three order data steps: email, password, Telegram username."""
    user_id = update.effective_user.id
    text = (update.message.text or "").strip()
    order = orders.get(user_id)

    if not order:
        return

    # 1. Email/login
    if context.user_data.get("waiting_for_login"):
        email_pattern = r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
        if len(text) > 254 or not re.fullmatch(email_pattern, text):
            await update.message.reply_text(
                "❌ Это не похоже на корректный адрес электронной почты.\n\n"
                "Отправь почту аккаунта ещё раз, например: `example@mail.com`",
                parse_mode="Markdown",
            )
            return
        order["login"] = text
        context.user_data["waiting_for_login"] = False
        context.user_data["waiting_for_password"] = True
        await update.message.reply_text(
            "🔐 *Шаг 2 из 3*\n\nОтправь пароль аккаунта.",
            parse_mode="Markdown",
        )
        return

    # 2. Password
    if context.user_data.get("waiting_for_password"):
        if not text or len(text) > 256:
            await update.message.reply_text("❌ Некорректный пароль. Отправь пароль ещё раз.")
            return
        order["password"] = text
        context.user_data["waiting_for_password"] = False
        context.user_data["waiting_for_tg_username"] = True
        await update.message.reply_text(
            "🔐 *Шаг 3 из 3*\n\nОтправь свой Telegram-юзернейм.\n\n"
            "Например: @username",
            parse_mode="Markdown",
        )
        return

    # 3. Telegram username
    if context.user_data.get("waiting_for_tg_username"):
        username = text.strip()
        normalized = username if username.startswith("@") else "@" + username
        username_without_at = normalized[1:]
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", username_without_at):
            await update.message.reply_text(
                "❌ Некорректный Telegram-юзернейм.\n\n"
                "Отправь юзернейм в формате @username.\n"
                "Допустимы латинские буквы, цифры и символ _."
            )
            return

        order["telegram_username"] = normalized
        context.user_data["waiting_for_tg_username"] = False

        display_name = f"@{update.effective_user.username}" if update.effective_user.username else update.effective_user.full_name
        if "tier_index" in order:
            service_line = f"🧹 Зачистка: *{TIER_NAMES[order['tier_index']]}*"
        else:
            service_line = f"🛒 Услуга: *{order.get('service', '')}*\n📦 Вариант: *{order.get('service_item', '')}*"
        details_line = f"\n📝 Детали: {order.get('details')}" if order.get("details") and order.get("details").upper() != "НЕТ" else ""
        summary = (
            "🧾 *Проверь заказ*\n\n"
            f"👤 Клиент: {display_name}\n"
            f"💬 Telegram: *{order['telegram_username']}*\n"
            f"📍 Регион: *{order.get('region', 'Не требуется')}*\n"
            f"{service_line}\n"
            f"💰 Цена: *{order['price']} ₽*"
            f"{details_line}\n\n"
            "🔐 Данные для входа будут переданы менеджерам только после подтверждения заказа.\n\n"
            "⚠️ После выполнения заказа рекомендуется изменить пароль.\n\n"
            "Всё верно?"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Подтвердить заказ", callback_data="confirm_order")],
            [InlineKeyboardButton("❌ Отменить", callback_data="cancel_order")],
        ])
        await update.message.reply_text(summary, reply_markup=kb, parse_mode="Markdown")
        return

    logger.info("Ignoring text from user %s: no active order step", user_id)


async def confirm_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    order = orders.get(user_id)
    if not order:
        await query.edit_message_text("❌ Заказ не найден. Нажми /start.", reply_markup=MAIN_MENU)
        return
    display_name = f"@{update.effective_user.username}" if update.effective_user.username else update.effective_user.full_name
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = db()
    tier_name = TIER_NAMES[order["tier_index"]] if "tier_index" in order else order.get("service_item", order.get("service", "Услуга"))
    cur = conn.execute("INSERT INTO orders (created_at, client_telegram_id, client_name, uid, server, region, tier, price, service, details) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (now, user_id, display_name, "", "", order.get("region", "Не требуется"), tier_name, order["price"], order.get("service", "Зачистка по регионам"), order.get("details", "")))
    order_id = cur.lastrowid
    conn.commit()
    row = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    conn.close()

    admin_status = ""
    if ADMIN_CHAT_ID:
        try:
            await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=format_admin_order(row), parse_mode="Markdown", reply_markup=admin_keyboard(order_id))
            credentials_text = (
                f"🔐 *Данные для входа — заказ №{order_id}*\n\n"
                f"👤 Клиент: {display_name}\n"
                f"🔑 Логин: `{order['login']}`\n"
                f"🔒 Пароль: `{order['password']}`\n"
                f"💬 Telegram-юзернейм: `{order['telegram_username']}`\n\n"
                "⚠️ Эти данные не сохраняются в orders.db. Они находятся только в памяти бота до оформления заказа и затем очищаются. "
                "После выполнения заказа рекомендуется удалить сообщение с данными для входа и изменить пароль."
            )
            await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=credentials_text, parse_mode="Markdown")
            admin_status = "Заявка и данные для входа отправлены менеджерам."
        except Exception:
            logger.exception("Failed to send order to ADMIN_CHAT_ID")
            admin_status = "⚠️ Заказ сохранён, но не удалось отправить его в чат менеджеров. Проверь ADMIN_CHAT_ID."
    else:
        admin_status = "⚠️ ADMIN_CHAT_ID пока не настроен."

    username_mgr = MANAGER_USERNAME.strip().lstrip("@")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("💬 Связаться с менеджером", url=f"https://t.me/{username_mgr}")], [InlineKeyboardButton("🏠 Главное меню", callback_data="back")]])
    await query.edit_message_text("✅ *Заказ оформлен!*\n\n" + admin_status + "\n\nОжидай ответа менеджера.", reply_markup=kb, parse_mode="Markdown")
    orders.pop(user_id, None)
    context.user_data.clear()


async def cancel_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Заказ отменён")
    orders.pop(update.effective_user.id, None)
    context.user_data.clear()
    await query.edit_message_text(f"🎮 *{SHOP_NAME}*\n\nЗаказ отменён. Выбери раздел:", reply_markup=MAIN_MENU, parse_mode="Markdown")


async def orders_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_chat(update):
        return
    conn = db()
    rows = conn.execute("SELECT * FROM orders WHERE status IN ('new','taken','working') ORDER BY id DESC LIMIT 20").fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("📭 Активных заказов нет.")
        return
    for row in rows:
        await update.message.reply_text(format_admin_order(row), parse_mode="Markdown", reply_markup=admin_keyboard(row["id"], row["status"]))


async def all_orders_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_chat(update):
        return
    conn = db()
    rows = conn.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 20").fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("📭 Заказов пока нет.")
        return
    lines = ["📋 *Последние 20 заказов*\n"]
    for row in rows:
        lines.append(f"№{row['id']} — {STATUS_LABELS.get(row['status'], row['status'])} — {row['service'] or 'Зачистка'} — {row['tier']} — {row['price']} ₽")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def order_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin_chat(update):
        await query.answer("Недоступно", show_alert=True)
        return
    await query.answer()
    parts = query.data.split(":")
    if len(parts) != 3:
        return
    try:
        order_id = int(parts[1])
    except ValueError:
        return
    new_status = parts[2]
    if new_status not in STATUS_LABELS:
        return
    manager = f"@{query.from_user.username}" if query.from_user.username else query.from_user.full_name
    conn = db()
    row = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not row:
        conn.close()
        await query.edit_message_text("❌ Заказ не найден.")
        return
    conn.execute("UPDATE orders SET status=?, manager_name=? WHERE id=?", (new_status, manager, order_id))
    conn.commit()
    row = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    conn.close()

    # Notify the client when a manager changes the order status.
    client_messages = {
        "taken": f"🟡 Заказ №{order_id} взят менеджером {manager}.",
        "working": f"🔵 Заказ №{order_id} взят в работу менеджером {manager}.",
        "done": f"✅ Заказ №{order_id} выполнен. Спасибо за заказ!",
        "cancelled": f"❌ Заказ №{order_id} отменён. Если это произошло по ошибке, свяжись с менеджером.",
    }
    if new_status in client_messages:
        try:
            await context.bot.send_message(chat_id=row["client_telegram_id"], text=client_messages[new_status])
        except Exception:
            logger.exception("Failed to notify client for order %s", order_id)

    await query.edit_message_text(format_admin_order(row), parse_mode="Markdown", reply_markup=admin_keyboard(order_id, new_status))


async def chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat:
        await update.message.reply_text(f"ID этого чата: `{update.effective_chat.id}`", parse_mode="Markdown")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Unhandled exception:", exc_info=context.error)


def main():
    db().close()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CommandHandler("orders", orders_command))
    app.add_handler(CommandHandler("allorders", all_orders_command))
    app.add_handler(CommandHandler("chatid", chat_id))
    app.add_handler(CallbackQueryHandler(confirm_order, pattern=r"^confirm_order$"))
    app.add_handler(CallbackQueryHandler(cancel_order, pattern=r"^cancel_order$"))
    app.add_handler(CallbackQueryHandler(order_status, pattern=r"^order_status:"))
    app.add_handler(CallbackQueryHandler(button))
    # One text handler dispatches to the correct step. Two separate catch-all
    # handlers would both see the same message and could corrupt the flow.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receive_text))
    app.add_error_handler(error_handler)
    print(f"{SHOP_NAME} bot started.")
    app.run_polling()


if __name__ == "__main__":
    main()
