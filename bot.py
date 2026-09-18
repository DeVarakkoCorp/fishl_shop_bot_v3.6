import base64
import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters

from config import (
    BOT_TOKEN, MANAGER_USERNAME, ADMIN_CHAT_ID, SHOP_NAME,
    GITHUB_TOKEN, GITHUB_REPO, GITHUB_BACKUP_BRANCH, GITHUB_BACKUP_PATH,
    GITHUB_BACKUP_INTERVAL_MINUTES,
)
from prices import PRICES, EXTRA_PRICES, PRICE_CATEGORIES

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent

# Railway Volume: при наличии тома Railway задаёт RAILWAY_VOLUME_MOUNT_PATH
# автоматически. Если том ещё не подключён локально, используем ./data.
VOLUME_PATH = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", str(BASE_DIR / "data"))
DATA_DIR = Path(VOLUME_PATH)
DB_PATH = DATA_DIR / "orders.db"
orders = {}


def ensure_persistent_databases():
    """Создаёт каталог данных и один раз переносит DB из репозитория в Volume."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for seed in sorted(BASE_DIR.glob("orders*.db")):
        destination = DATA_DIR / seed.name
        if destination.exists():
            continue
        try:
            shutil.copy2(seed, destination)
            logger.info("Скопирована исходная БД в persistent storage: %s", destination)
        except OSError:
            logger.exception("Не удалось скопировать %s в %s", seed, destination)


def github_backup_enabled():
    return bool(GITHUB_TOKEN and GITHUB_REPO and GITHUB_BACKUP_BRANCH)


def github_api(method, path, payload=None):
    url = "https://api.github.com" + path
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "X-GitHub-Api-Version": "2026-03-10",
            "User-Agent": "Fishl-Shop-Bot",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        body = response.read()
        return response.status, json.loads(body.decode("utf-8")) if body else {}


def ensure_github_backup_branch():
    """Создаёт ветку бэкапов один раз, если её ещё нет."""
    owner, repo = GITHUB_REPO.split("/", 1)
    ref_path = f"/repos/{owner}/{repo}/git/ref/heads/{GITHUB_BACKUP_BRANCH}"
    try:
        github_api("GET", ref_path)
        return
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise

    default_ref = f"/repos/{owner}/{repo}/git/ref/heads/main"
    try:
        _, data = github_api("GET", default_ref)
    except urllib.error.HTTPError:
        default_ref = f"/repos/{owner}/{repo}/git/ref/heads/master"
        _, data = github_api("GET", default_ref)

    sha = data["object"]["sha"]
    github_api("POST", f"/repos/{owner}/{repo}/git/refs", {
        "ref": f"refs/heads/{GITHUB_BACKUP_BRANCH}",
        "sha": sha,
    })


def create_sqlite_backup_bytes():
    """Делает согласованный snapshot SQLite через backup API."""
    if not DB_PATH.exists():
        return None
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "orders.db"
        source = sqlite3.connect(DB_PATH)
        destination = sqlite3.connect(target)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
        return target.read_bytes()


def upload_github_backup():
    """Загружает новый snapshot БД в отдельную ветку GitHub."""
    if not github_backup_enabled():
        return False
    content = create_sqlite_backup_bytes()
    if not content:
        logger.warning("GitHub backup skipped: orders.db отсутствует")
        return False

    ensure_github_backup_branch()
    owner, repo = GITHUB_REPO.split("/", 1)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    path = f"{GITHUB_BACKUP_PATH.strip('/').strip()}/orders_{timestamp}.db"
    payload = {
        "message": f"Backup orders.db {timestamp}",
        "content": base64.b64encode(content).decode("ascii"),
        "branch": GITHUB_BACKUP_BRANCH,
    }
    github_api("PUT", f"/repos/{owner}/{repo}/contents/{path}", payload)
    logger.info("GitHub backup uploaded: %s:%s", GITHUB_BACKUP_BRANCH, path)
    return True


async def scheduled_github_backup(context: ContextTypes.DEFAULT_TYPE):
    try:
        upload_github_backup()
    except Exception:
        logger.exception("GitHub backup failed")

MAIN_MENU = InlineKeyboardMarkup([
    [InlineKeyboardButton("💰 Прайсы", callback_data="prices")],
    [InlineKeyboardButton("🛒 Заказать услугу", callback_data="cleanup")],
    [InlineKeyboardButton("📋 Мои заказы", callback_data="my_orders")],
    [InlineKeyboardButton("📞 Связь", callback_data="contact")],
])

TIER_NAMES = ["0–100%", "50–100%", "80–100%"]
TIER_KEYS = ["0-100", "50-100", "80-100"]

DIKOVINKI_PRICES = {
    "Мондштадт": 1.0,
    "Ли Юэ": 1.5,
    "Инадзума": 1.5,
    "Сумеру": 2.0,
    "Фонтейн": 1.5,
    "Натлан, Снежная": 2.0,
}

OKULY_PRICES = [
    ("Анемокулы", 200),
    ("Геокулы", 1000),
    ("Багровые агаты", 500),
    ("Электрокулы", 700),
    ("Адъюванты светоносного камня", 550),
    ("Гидрокулы", 1200),
    ("Оперенья очищающего света", 690),
    ("Кои карпы", 1100),
    ("Пирокулы", 550),
    ("Лунокулы", 1500),
    ("Лунокулы — на данный момент", 1000),
    ("Криокулы", 1500),
]

RANK57_PRICES = {
    "Крутки": "КРУТКИ💫\n\n💫1 крутка — 30 рублей💫\n💫10 круток — 300 рублей💫\n💫100 круток — 3000 рублей💫",
    "ЭНДГЕЙМ": "ЭНДГЕЙМ🌟\n\n🌱10 этаж бездны — 120 рублей\n🌙11 этаж бездны — 150 рублей\n🌙12 этаж бездны — 300 рублей",
    "Натиск": "НАТИСК🌟\n\n🎂4 уровень сложности — 300 рублей\n🎂5 уровень сложности — 600 рублей\n\n(Дочистку временно не принимаю)",
    "Театр": "Театр🌟\n\nОбычная сложность (3 этапа) — 200₽🤩\nСредняя сложность (6 этапов) — 350₽🤩\nСложная сложность (9 этапов) — 450₽🤩🤩\nАрканы — 1000₽🤩",
}
RANK57_CATEGORIES = list(RANK57_PRICES.keys())
STATUS_LABELS = {"new": "🆕 Новый", "taken": "🟡 Взято", "working": "🔵 В работе", "done": "✅ Выполнен", "cancelled": "❌ Отменён"}

# Доступ к панели менеджера только для этих Telegram-юзернеймов.
MANAGER_USERNAMES = {"devarapq", "fishlme"}


def normalize_username(username):
    return (username or "").strip().lstrip("@").lower()


def is_manager(update: Update):
    user = update.effective_user
    return bool(user and normalize_username(user.username) in MANAGER_USERNAMES)


def format_price(value):
    value = float(value)
    if value.is_integer():
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")

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
            details TEXT DEFAULT '',
            original_price REAL DEFAULT NULL,
            discount_amount REAL DEFAULT 0,
            discount_mode TEXT DEFAULT ''
        )
    """)
    # Upgrade databases created by older versions.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(orders)").fetchall()}
    if "service" not in columns:
        conn.execute("ALTER TABLE orders ADD COLUMN service TEXT DEFAULT ''")
    if "details" not in columns:
        conn.execute("ALTER TABLE orders ADD COLUMN details TEXT DEFAULT ''")
    if "original_price" not in columns:
        conn.execute("ALTER TABLE orders ADD COLUMN original_price REAL DEFAULT NULL")
    if "discount_amount" not in columns:
        conn.execute("ALTER TABLE orders ADD COLUMN discount_amount REAL DEFAULT 0")
    if "discount_mode" not in columns:
        conn.execute("ALTER TABLE orders ADD COLUMN discount_mode TEXT DEFAULT ''")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS discounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            threshold REAL DEFAULT 0,
            service TEXT DEFAULT '',
            percent REAL NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            expires_at TEXT DEFAULT NULL
        )
    """)
    dcols = {row[1] for row in conn.execute("PRAGMA table_info(discounts)").fetchall()}
    if "expires_at" not in dcols:
        conn.execute("ALTER TABLE discounts ADD COLUMN expires_at TEXT DEFAULT NULL")
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
    if category == "Крутки":
        rows.append([InlineKeyboardButton("✏️ Ввести своё количество", callback_data="custom_quantity:spins:57")])
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

    if category == "Крутки":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("1 крутка — 24 ₽", callback_data="service_item:0:1")],
            [InlineKeyboardButton("10 круток — 240 ₽", callback_data="service_item:0:2")],
            [InlineKeyboardButton("100 круток — 2400 ₽", callback_data="service_item:0:3")],
            [InlineKeyboardButton("✏️ Ввести своё количество", callback_data="custom_quantity:spins:old")],
            [InlineKeyboardButton("⬅️ К услугам", callback_data="cleanup")],
        ])

    if category == "Диковинки":
        rows = [[InlineKeyboardButton(region, callback_data=f"custom_quantity:dikovinki:{i}")]
                for i, region in enumerate(DIKOVINKI_PRICES)]
        rows.append([InlineKeyboardButton("⬅️ К услугам", callback_data="cleanup")])
        return InlineKeyboardMarkup(rows)

    if category == "Окулы":
        rows = [[InlineKeyboardButton(f"{name} — {price} ₽/шт.", callback_data=f"custom_quantity:okuly:{i}")]
                for i, (name, price) in enumerate(OKULY_PRICES)]
        rows.append([InlineKeyboardButton("⬅️ К услугам", callback_data="cleanup")])
        return InlineKeyboardMarkup(rows)

    lines = [line.strip() for line in EXTRA_PRICES[category].split("\n") if line.strip()]
    rows = []
    for i, line in enumerate(lines):
        if line.startswith("Уточняйте в лс") or line.startswith("(Дочистку"):
            continue
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


def manager_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📦 Все заказы", callback_data="mgr_all_orders")],
        [InlineKeyboardButton("➕ Добавить скидку", callback_data="mgr_discount_add")],
        [InlineKeyboardButton("📋 Активные скидки", callback_data="mgr_discount_list")],
        [InlineKeyboardButton("🗑 Управление скидками", callback_data="mgr_discount_manage")],
        [InlineKeyboardButton("💾 Сделать бэкап", callback_data="mgr_backup")],
    ])


def discount_kind_label(row):
    if row["kind"] == "threshold":
        return f"На заказ от {format_price(row['threshold'])} ₽"
    return f"На услугу: {row['service']}"

def discount_period_label(row):
    return "без срока" if not row["expires_at"] else f"до {row['expires_at']}"


def get_active_discounts():
    conn = db()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("UPDATE discounts SET active=0 WHERE active=1 AND expires_at IS NOT NULL AND expires_at <= ?", (now,))
    conn.commit()
    rows = conn.execute("SELECT * FROM discounts WHERE active=1 ORDER BY id DESC").fetchall()
    conn.close()
    return rows


def get_applicable_discounts(order):
    total = float(order.get("price", 0))
    service = str(order.get("service", "")).strip().lower()
    result = []
    for row in get_active_discounts():
        if row["kind"] == "threshold" and total >= float(row["threshold"]):
            result.append(row)
        elif row["kind"] == "service" and service and service == str(row["service"]).strip().lower():
            result.append(row)
    return result


def calculate_discount(base_price, discounts, mode="sequential"):
    """Calculate discount. sequential = general first, then service; summed = sum percentages."""
    base = float(base_price)
    if not discounts:
        return base, 0.0
    threshold = [d for d in discounts if d["kind"] == "threshold"]
    service = [d for d in discounts if d["kind"] == "service"]
    if mode == "summed":
        percent = sum(float(d["percent"]) for d in discounts)
        percent = min(percent, 100.0)
        final = base * (1 - percent / 100)
    else:
        final = base
        for d in threshold + service:
            final *= (1 - min(max(float(d["percent"]), 0), 100) / 100)
    final = round(max(final, 0.0), 2)
    return final, round(base - final, 2)


def discount_description(rows):
    return "\n".join(
        f"#{row['id']} — {discount_kind_label(row)} — *{format_price(row['percent'])}%* — {discount_period_label(row)}"
        for row in rows
    )


async def show_manager_panel(update: Update, edit=False):
    if not is_manager(update):
        if edit:
            await update.callback_query.answer("Недоступно", show_alert=True)
        else:
            await update.message.reply_text("❌ Доступ запрещён.")
        return
    text = "👨‍💼 *Панель менеджера*\n\nВыбери действие:"
    if edit:
        await update.callback_query.edit_message_text(text, reply_markup=manager_keyboard(), parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=manager_keyboard(), parse_mode="Markdown")


def format_discount_choice(order_id, rows):
    lines = [f"🏷 *Для заказа №{order_id} доступны скидки:*", ""]
    for row in rows:
        lines.append(f"• {discount_kind_label(row)} — *{format_price(row['percent'])}%*")
    lines += [
        "",
        "Выбери, как применить скидки:",
        "• *Сложить проценты* — например 10% + 5% = 15%.",
        "• *Последовательно* — сначала скидка на весь заказ, затем на конкретную услугу.",
    ]
    return "\n".join(lines)


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
        f"💰 Цена: *{format_price(row['price'])} ₽*"
        + (f"\n💸 Скидка: *{format_price(row['discount_amount'])} ₽*" if row.get('discount_amount') else "")
        + (f"\n💰 Цена без скидки: *{format_price(row['original_price'])} ₽*" if row.get('original_price') is not None and float(row['original_price']) != float(row['price']) else "")
        + (f"\n📝 Детали: {row['details']}" if row['details'] else "")
        + manager
    )


def is_admin_chat(update: Update):
    return bool(ADMIN_CHAT_ID and update.effective_chat and update.effective_chat.id == ADMIN_CHAT_ID)


def format_client_order(row):
    status = STATUS_LABELS.get(row["status"], row["status"])
    service = row["service"] or "Зачистка по регионам"
    tier = row["tier"] or "Не указан"
    region = row["region"] or "Не указан"
    details = f"\n📝 {row['details']}" if row["details"] else ""
    order_label = ("архив-" + str(row["id"])) if row.get("_is_archive") else str(row["id"])
    discount = (f"\n💸 Скидка: *{format_price(row['discount_amount'])} ₽*" if row.get("discount_amount") else "")
    return (
        f"🧾 *Заказ №{order_label}*\n"
        f"{status}\n"
        f"🕐 {row['created_at']}\n"
        f"🛒 {service}\n"
        f"📦 {tier}\n"
        f"📍 {region}\n"
        f"💰 *{format_price(row['price'])} ₽*"
        f"{discount}"
        f"{details}"
    )


def get_order_database_paths():
    """Все DB внутри persistent storage Volume."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    paths = sorted(DATA_DIR.glob("orders*.db"), key=lambda p: (p.name != "orders.db", p.name))
    if DB_PATH not in paths:
        paths.insert(0, DB_PATH)
    return paths


def get_client_orders(user_id, limit=20):
    """Find the customer's orders across the live DB and all order archives."""
    found = []
    seen = set()

    for path in get_order_database_paths():
        try:
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            table_exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='orders'"
            ).fetchone()
            if not table_exists:
                conn.close()
                continue

            rows = conn.execute(
                "SELECT * FROM orders WHERE client_telegram_id=?",
                (user_id,),
            ).fetchall()
            conn.close()

            for row in rows:
                data = dict(row)
                data.setdefault("service", "")
                data.setdefault("details", "")

                fingerprint = (
                    data.get("client_telegram_id"),
                    data.get("created_at"),
                    data.get("client_name"),
                    data.get("price"),
                    data.get("service", ""),
                    data.get("details", ""),
                    data.get("region", ""),
                    data.get("tier", ""),
                    data.get("uid", ""),
                    data.get("server", ""),
                )
                if fingerprint in seen:
                    continue
                seen.add(fingerprint)

                data["_source_db"] = path.name
                data["_is_archive"] = path != DB_PATH
                found.append(data)

        except (sqlite3.Error, OSError) as exc:
            logger.warning("Не удалось прочитать базу заказов %s: %s", path, exc)

    found.sort(key=lambda row: str(row.get("created_at", "")), reverse=True)
    return found[:limit]

def get_all_manager_orders(limit=50):
    """Return orders from the live DB and every archived orders*.db, deduplicated."""
    found = []
    seen = set()

    for path in get_order_database_paths():
        try:
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            table_exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='orders'"
            ).fetchone()
            if not table_exists:
                conn.close()
                continue
            rows = conn.execute("SELECT * FROM orders ORDER BY created_at DESC").fetchall()
            conn.close()
            for row in rows:
                data = dict(row)
                data.setdefault("service", "")
                data.setdefault("details", "")
                data.setdefault("original_price", None)
                data.setdefault("discount_amount", 0)
                data.setdefault("discount_mode", "")
                fingerprint = (
                    data.get("client_telegram_id"), data.get("created_at"),
                    data.get("client_name"), data.get("price"),
                    data.get("service", ""), data.get("details", ""),
                    data.get("region", ""), data.get("tier", ""),
                    data.get("uid", ""), data.get("server", ""),
                )
                if fingerprint in seen:
                    continue
                seen.add(fingerprint)
                data["_source_db"] = path.name
                data["_is_archive"] = path != DB_PATH
                found.append(data)
        except (sqlite3.Error, OSError) as exc:
            logger.warning("Не удалось прочитать базу заказов %s: %s", path, exc)

    found.sort(key=lambda row: str(row.get("created_at", "")), reverse=True)
    return found[:limit]


def manager_all_orders_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Обновить", callback_data="mgr_all_orders")],
        [InlineKeyboardButton("⬅️ Панель менеджера", callback_data="mgr_panel")],
    ])


async def show_manager_all_orders(update: Update, context: ContextTypes.DEFAULT_TYPE, edit=False):
    if not is_manager(update):
        if edit:
            await update.callback_query.answer("Недоступно", show_alert=True)
        else:
            await update.message.reply_text("❌ Доступ запрещён.")
        return

    rows = get_all_manager_orders(50)
    if not rows:
        text = "📦 *Все заказы*\n\nЗаказов пока нет."
    else:
        blocks = ["📦 *Все заказы из всех доступных баз*\n"]
        for row in rows:
            blocks.append(format_admin_order(row))
            if row.get("_is_archive"):
                blocks.append(f"🗄 Источник: `{row['_source_db']}`\n⚠️ Архивный заказ — изменение статуса из этой панели недоступно.")
        blocks.append("\nПоказаны последние 50 уникальных заказов из всех доступных баз.")
        text = "\n\n".join(blocks)

    markup = manager_all_orders_keyboard()
    if edit:
        await update.callback_query.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=markup, parse_mode="Markdown")


def client_orders_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Обновить", callback_data="my_orders")],
        [InlineKeyboardButton("🛒 Заказать услугу", callback_data="cleanup")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="back")],
    ])


async def show_my_orders(update: Update, context: ContextTypes.DEFAULT_TYPE, edit=False):
    user_id = update.effective_user.id
    rows = get_client_orders(user_id)
    if not rows:
        text = "📋 *Мои заказы*\n\nУ тебя пока нет оформленных заказов."
    else:
        blocks = ["📋 *Мои заказы*\n"]
        for row in rows:
            blocks.append(format_client_order(row))
        blocks.append("\nПоказаны последние 20 заказов из всех доступных баз.")
        text = "\n\n".join(blocks)

    if edit:
        await update.callback_query.edit_message_text(
            text, reply_markup=client_orders_keyboard(), parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            text, reply_markup=client_orders_keyboard(), parse_mode="Markdown"
        )


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

    if data.startswith("mgr_"):
        if not is_manager(update):
            await query.answer("Недоступно", show_alert=True)
            return
        if data == "mgr_panel":
            await show_manager_panel(update, context, edit=True)
            return
        if data == "mgr_all_orders":
            await show_manager_all_orders(update, context, edit=True)
            return

        if data == "mgr_backup":
            if not is_manager(update):
                await query.answer("Недоступно", show_alert=True)
                return
            await query.answer("Делаю бэкап...")
            try:
                ok = upload_github_backup()
                if ok:
                    await query.edit_message_text("✅ Бэкап orders.db загружен на GitHub.", reply_markup=manager_keyboard())
                else:
                    await query.edit_message_text("⚠️ GitHub-бэкап не настроен. Добавь переменные GITHUB_* в Railway.", reply_markup=manager_keyboard())
            except Exception as exc:
                logger.exception("Manual GitHub backup failed")
                await query.edit_message_text(f"❌ Не удалось сделать бэкап: {exc}", reply_markup=manager_keyboard())
            return
        if data == "mgr_discount_add":
            context.user_data["manager_discount_step"] = "type"
            await query.edit_message_text(
                "➕ *Добавление скидки*\n\nВыбери тип скидки:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💰 От суммы заказа", callback_data="mgr_discount_type:threshold")],
                    [InlineKeyboardButton("🛒 На конкретную услугу", callback_data="mgr_discount_type:service")],
                    [InlineKeyboardButton("⬅️ Назад", callback_data="mgr_panel")],
                ]), parse_mode="Markdown")
            return
        if data.startswith("mgr_discount_type:"):
            kind = data.split(":", 1)[1]
            if kind == "threshold":
                context.user_data["manager_discount_step"] = "threshold"
                context.user_data["manager_discount_kind"] = kind
                await query.edit_message_text("💰 Введи минимальную сумму заказа в рублях, например `1000`.", parse_mode="Markdown")
            elif kind == "service":
                context.user_data["manager_discount_step"] = "service"
                context.user_data["manager_discount_kind"] = kind
                await query.edit_message_text("🛒 Введи точное название услуги, например `Крутки` или `Диковинки`.")
            return
        if data.startswith("mgr_discount_period:"):
            choice = data.split(":", 1)[1]
            if choice == "custom":
                context.user_data["manager_discount_step"] = "expires_at"
                await query.edit_message_text("🗓 Введи дату окончания в формате `ДД.ММ.ГГГГ ЧЧ:ММ`, например `30.09.2026 23:59`.", parse_mode="Markdown")
                return
            expires_at = None
            if choice != "none":
                try:
                    expires_at = (datetime.now() + timedelta(days=int(choice))).strftime("%Y-%m-%d %H:%M:%S")
                except ValueError:
                    expires_at = None
            context.user_data["manager_discount_expires_at"] = expires_at
            active = get_active_discounts()
            if active:
                context.user_data["manager_discount_step"] = "replace_choice"
                await query.edit_message_text(
                    "⚠️ Уже есть активные скидки.\n\nОставить их или удалить перед добавлением новой?",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("✅ Оставить старые", callback_data="mgr_discount_keep")],
                        [InlineKeyboardButton("🗑 Удалить старые", callback_data="mgr_discount_replace")],
                    ])
                )
            else:
                await finalize_discount_creation(update, context, delete_old=False)
            return
        if data == "mgr_discount_keep":
            await finalize_discount_creation(update, context, delete_old=False)
            return
        if data == "mgr_discount_replace":
            await finalize_discount_creation(update, context, delete_old=True)
            return

        if data == "mgr_discount_list":
            rows = get_active_discounts()
            text = "📋 *Активные скидки*\n\n" + (discount_description(rows) if rows else "Активных скидок нет.")
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="mgr_panel")]]), parse_mode="Markdown")
            return
        if data == "mgr_discount_manage":
            rows = get_active_discounts()
            buttons = [[InlineKeyboardButton(f"❌ Отключить #{r['id']}", callback_data=f"mgr_discount_disable:{r['id']}")] for r in rows]
            buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="mgr_panel")])
            text = "🗑 *Управление скидками*\n\n" + (discount_description(rows) if rows else "Активных скидок нет.")
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
            return
        if data.startswith("mgr_discount_disable:"):
            try:
                discount_id = int(data.split(":", 1)[1])
            except ValueError:
                return
            conn = db()
            conn.execute("UPDATE discounts SET active=0 WHERE id=?", (discount_id,))
            conn.commit()
            conn.close()
            await query.answer("Скидка отключена")
            rows = get_active_discounts()
            buttons = [[InlineKeyboardButton(f"❌ Отключить #{r['id']}", callback_data=f"mgr_discount_disable:{r['id']}")] for r in rows]
            buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="mgr_panel")])
            text = "🗑 *Управление скидками*\n\n" + (discount_description(rows) if rows else "Активных скидок нет.")
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
            return

    if data == "prices":
        await query.edit_message_text("💰 *Прайсы*\n\nВыбери раздел:", reply_markup=price_region_keyboard(), parse_mode="Markdown")
        return

    if data == "my_orders":
        await show_my_orders(update, context, edit=True)
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

    if data.startswith("custom_quantity:"):
        parts = data.split(":")
        if len(parts) != 3:
            await query.edit_message_text("❌ Ошибка выбора.", reply_markup=service_category_keyboard())
            return
        kind, value = parts[1], parts[2]

        if kind == "spins":
            unit_price = 30.0 if value == "57" else 24.0
            order = {"service": "Крутки", "quantity_unit": "круток", "unit_price": unit_price}
            prompt = (
                f"✏️ *Крутки*\n\n"
                f"Цена: *{format_price(unit_price)} ₽ за 1 крутку*.\n\n"
                "Введи нужное количество круток:"
            )
        elif kind == "dikovinki":
            try:
                region = list(DIKOVINKI_PRICES.keys())[int(value)]
            except (ValueError, IndexError):
                await query.edit_message_text("❌ Регион не найден.", reply_markup=service_category_keyboard())
                return
            unit_price = DIKOVINKI_PRICES[region]
            order = {"service": "Диковинки", "region": region, "quantity_unit": "шт.", "unit_price": unit_price}
            prompt = (
                f"✏️ *Диковинки — {region}*\n\n"
                f"Цена: *{format_price(unit_price)} ₽ за 1 шт.*\n\n"
                "Введи нужное количество:"
            )
        elif kind == "okuly":
            try:
                item_name, unit_price = OKULY_PRICES[int(value)]
            except (ValueError, IndexError):
                await query.edit_message_text("❌ Окулы не найдены.", reply_markup=service_category_keyboard())
                return
            order = {"service": "Окулы", "service_item": item_name, "quantity_unit": "шт.", "unit_price": unit_price}
            prompt = (
                f"✏️ *{item_name}*\n\n"
                f"Цена: *{format_price(unit_price)} ₽ за 1 шт.*\n\n"
                "Введи нужное количество:"
            )
        else:
            await query.edit_message_text("❌ Неизвестный тип услуги.", reply_markup=service_category_keyboard())
            return

        orders[user_id] = order
        context.user_data.clear()
        context.user_data["waiting_for_quantity"] = True
        await query.edit_message_text(prompt, parse_mode="Markdown")
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


async def finalize_discount_creation(update: Update, context: ContextTypes.DEFAULT_TYPE, delete_old=False):
    kind = context.user_data.get("manager_discount_kind")
    threshold = float(context.user_data.get("manager_discount_threshold", 0))
    service = context.user_data.get("manager_discount_service", "")
    percent = float(context.user_data.get("manager_discount_percent", 0))
    expires_at = context.user_data.get("manager_discount_expires_at")
    conn = db()
    if delete_old:
        conn.execute("UPDATE discounts SET active=0 WHERE active=1")
    conn.execute("INSERT INTO discounts (kind, threshold, service, percent, active, created_at, expires_at) VALUES (?, ?, ?, ?, 1, ?, ?)",
                 (kind, threshold, service, percent, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), expires_at))
    conn.commit()
    conn.close()
    period = "без срока" if not expires_at else f"до {expires_at}"
    context.user_data.clear()
    msg = "🗑 Старые скидки отключены.\n\n" if delete_old else ""
    msg += f"✅ Скидка добавлена.\n⏱ Период: {period}"
    if getattr(update, "callback_query", None):
        await update.callback_query.edit_message_text(msg, reply_markup=manager_keyboard())
    else:
        await update.message.reply_text(msg, reply_markup=manager_keyboard())


async def receive_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the three order data steps: email, password, Telegram username."""
    user_id = update.effective_user.id
    text = (update.message.text or "").strip()
    # Manager discount setup is independent of customer orders.
    if is_manager(update) and context.user_data.get("manager_discount_step"):
        step = context.user_data.get("manager_discount_step")
        if step == "threshold":
            try:
                threshold = float(text.replace(",", "."))
            except ValueError:
                await update.message.reply_text("❌ Введи сумму числом, например 1000.")
                return
            if threshold <= 0:
                await update.message.reply_text("❌ Сумма должна быть больше 0.")
                return
            context.user_data["manager_discount_threshold"] = threshold
            context.user_data["manager_discount_step"] = "percent"
            await update.message.reply_text("📉 Введи размер скидки в процентах, например `10`.", parse_mode="Markdown")
            return
        if step == "service":
            if len(text) > 100:
                await update.message.reply_text("❌ Слишком длинное название услуги.")
                return
            context.user_data["manager_discount_service"] = text
            context.user_data["manager_discount_step"] = "percent"
            await update.message.reply_text("📉 Введи размер скидки в процентах, например `15`.", parse_mode="Markdown")
            return
        if step == "percent":
            try:
                percent = float(text.replace(",", "."))
            except ValueError:
                await update.message.reply_text("❌ Введи процент числом, например 10.")
                return
            if percent <= 0 or percent > 100:
                await update.message.reply_text("❌ Процент должен быть от 0.01 до 100.")
                return
            context.user_data["manager_discount_percent"] = percent
            context.user_data["manager_discount_step"] = "period"
            await update.message.reply_text(
                "⏱ *Период действия скидки*\n\nВыбери срок:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("♾ Без срока", callback_data="mgr_discount_period:none")],
                    [InlineKeyboardButton("📅 1 день", callback_data="mgr_discount_period:1")],
                    [InlineKeyboardButton("📅 7 дней", callback_data="mgr_discount_period:7")],
                    [InlineKeyboardButton("📅 30 дней", callback_data="mgr_discount_period:30")],
                    [InlineKeyboardButton("🗓 До указанной даты", callback_data="mgr_discount_period:custom")],
                ]), parse_mode="Markdown")
            return

        if step == "expires_at":
            try:
                dt = datetime.strptime(text, "%d.%m.%Y %H:%M")
                if dt <= datetime.now():
                    await update.message.reply_text("❌ Дата окончания должна быть в будущем.")
                    return
                context.user_data["manager_discount_expires_at"] = dt.strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                await update.message.reply_text("❌ Неверный формат. Пример: 30.09.2026 23:59")
                return
            active = get_active_discounts()
            if active:
                context.user_data["manager_discount_step"] = "replace_choice"
                await update.message.reply_text(
                    "⚠️ Уже есть активные скидки. Оставить их или удалить перед добавлением новой?",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("✅ Оставить старые", callback_data="mgr_discount_keep")],
                        [InlineKeyboardButton("🗑 Удалить старые", callback_data="mgr_discount_replace")],
                    ])
                )
            else:
                await finalize_discount_creation(update, context, delete_old=False)
            return

    order = orders.get(user_id)

    if not order:
        return

    # 0. Произвольное количество
    if context.user_data.get("waiting_for_quantity"):
        if not re.fullmatch(r"[1-9]\d{0,5}", text):
            await update.message.reply_text("❌ Введи целое положительное количество, например: 37.")
            return
        quantity = int(text)
        if quantity > 100000:
            await update.message.reply_text("❌ Максимальное количество — 100000 шт.")
            return

        order["quantity"] = quantity
        order["price"] = round(quantity * float(order["unit_price"]), 2)
        context.user_data["waiting_for_quantity"] = False
        context.user_data["waiting_for_login"] = True

        if order.get("service") == "Диковинки":
            item_line = f"{order['region']} — {quantity} шт. × {format_price(order['unit_price'])} ₽"
        elif order.get("service") == "Окулы":
            item_line = f"{order['service_item']} — {quantity} шт. × {format_price(order['unit_price'])} ₽"
        else:
            item_line = f"{quantity} круток × {format_price(order['unit_price'])} ₽"
        order["service_item"] = item_line

        await update.message.reply_text(
            f"✅ Количество: *{quantity}*\n"
            f"💰 Стоимость: *{format_price(order['price'])} ₽*\n\n"
            "🔐 *Шаг 1 из 3*\n\n"
            "Отправь почту (email), привязанную к аккаунту.",
            parse_mode="Markdown",
        )
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
            f"💰 Цена: *{format_price(order['price'])} ₽*"
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
    base_price = float(order["price"])
    applicable = get_applicable_discounts(order)
    # If several discounts apply, the manager chooses the method. The order is
    # created at base price first; credentials are sent only after the choice.
    if len(applicable) > 1:
        final_price = base_price
        discount_amount = 0.0
        discount_mode = "pending"
    elif len(applicable) == 1:
        final_price, discount_amount = calculate_discount(base_price, applicable, mode="sequential")
        discount_mode = "automatic"
    else:
        final_price, discount_amount, discount_mode = base_price, 0.0, "none"

    conn = db()
    tier_name = TIER_NAMES[order["tier_index"]] if "tier_index" in order else order.get("service_item", order.get("service", "Услуга"))
    cur = conn.execute("INSERT INTO orders (created_at, client_telegram_id, client_name, uid, server, region, tier, price, service, details, original_price, discount_amount, discount_mode) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (now, user_id, display_name, "", "", order.get("region", "Не требуется"), tier_name, final_price, order.get("service", "Зачистка по регионам"), order.get("details", ""), base_price, discount_amount, discount_mode))
    order_id = cur.lastrowid
    conn.commit()
    row = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    conn.close()

    credentials_text = (
        f"🔐 *Данные для входа — заказ №{order_id}*\n\n"
        f"👤 Клиент: {display_name}\n"
        f"🔑 Логин: `{order['login']}`\n"
        f"🔒 Пароль: `{order['password']}`\n"
        f"💬 Telegram-юзернейм: `{order['telegram_username']}`\n\n"
        "⚠️ Эти данные не сохраняются в orders.db. Они находятся только в памяти бота до оформления заказа и затем очищаются. "
        "После выполнения заказа рекомендуется удалить сообщение с данными для входа и изменить пароль."
    )

    admin_status = ""
    if ADMIN_CHAT_ID:
        try:
            await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=format_admin_order(row), parse_mode="Markdown", reply_markup=admin_keyboard(order_id))
            if len(applicable) > 1:
                context.application.bot_data.setdefault("pending_credentials", {})[order_id] = credentials_text
                choice_kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("➕ Сложить проценты", callback_data=f"discount_sum:{order_id}")],
                    [InlineKeyboardButton("🔄 Последовательно", callback_data=f"discount_seq:{order_id}")],
                ])
                await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=format_discount_choice(order_id, applicable), parse_mode="Markdown", reply_markup=choice_kb)
                admin_status = "Заявка отправлена менеджеру. Менеджер выберет способ применения скидок."
            else:
                await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=credentials_text)
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


async def my_orders_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_my_orders(update, context, edit=False)


async def manager_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_manager_panel(update, context, edit=False)


async def orders_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_manager(update):
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
    await show_manager_all_orders(update, context, edit=False)


async def backup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_manager(update):
        return
    try:
        ok = upload_github_backup()
        await update.message.reply_text(
            "✅ Бэкап orders.db загружен на GitHub." if ok else "⚠️ GitHub-бэкап не настроен. Проверь GITHUB_* в Railway.",
            reply_markup=manager_keyboard(),
        )
    except Exception as exc:
        logger.exception("Manual GitHub backup command failed")
        await update.message.reply_text(f"❌ Не удалось сделать бэкап: {exc}")


async def apply_order_discount_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_manager(update):
        await query.answer("Недоступно", show_alert=True)
        return
    await query.answer()
    parts = query.data.split(":")
    if len(parts) != 2:
        return
    try:
        order_id = int(parts[1])
    except ValueError:
        return
    mode = "summed" if query.data.startswith("discount_sum:") else "sequential"
    conn = db()
    row = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not row:
        conn.close()
        await query.edit_message_text("❌ Заказ не найден.")
        return
    # Rebuild the applicable rules from the current active discount set.
    order_for_calc = {"price": row["original_price"] if row["original_price"] is not None else row["price"], "service": row["service"]}
    applicable = get_applicable_discounts(order_for_calc)
    base = float(order_for_calc["price"])
    final, amount = calculate_discount(base, applicable, mode=mode)
    conn.execute("UPDATE orders SET price=?, original_price=?, discount_amount=?, discount_mode=? WHERE id=?", (final, base, amount, mode, order_id))
    conn.commit()
    row = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    conn.close()
    await query.edit_message_text(format_admin_order(row), parse_mode="Markdown", reply_markup=admin_keyboard(order_id, row["status"]))

    # Credentials are kept only in process memory until the manager resolves the discount.
    pending = context.application.bot_data.get("pending_credentials", {}).pop(order_id, None)
    if pending and ADMIN_CHAT_ID:
        try:
            await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=pending)
        except Exception:
            logger.exception("Failed to send delayed credentials for order %s", order_id)


async def order_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_manager(update):
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
    ensure_persistent_databases()
    db().close()
    app = Application.builder().token(BOT_TOKEN).build()
    if github_backup_enabled():
        interval = max(15, int(GITHUB_BACKUP_INTERVAL_MINUTES))
        app.job_queue.run_repeating(scheduled_github_backup, interval=interval * 60, first=30)
        logger.info("GitHub backups enabled: every %s minutes, branch=%s", interval, GITHUB_BACKUP_BRANCH)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CommandHandler("myorders", my_orders_command))
    app.add_handler(CommandHandler("manager", manager_command))
    app.add_handler(CommandHandler("orders", orders_command))
    app.add_handler(CommandHandler("allorders", all_orders_command))
    app.add_handler(CommandHandler("backup", backup_command))
    app.add_handler(CommandHandler("chatid", chat_id))
    app.add_handler(CallbackQueryHandler(confirm_order, pattern=r"^confirm_order$"))
    app.add_handler(CallbackQueryHandler(cancel_order, pattern=r"^cancel_order$"))
    app.add_handler(CallbackQueryHandler(apply_order_discount_choice, pattern=r"^discount_(sum|seq):"))
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
