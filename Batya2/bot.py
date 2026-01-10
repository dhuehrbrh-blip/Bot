import asyncio
import random
import re
import os
import json
import sqlite3
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.filters import Command
from aiogram.types import FSInputFile
from telethon import events
from PIL import Image
import imagehash
from datetime import datetime
from aiogram import Bot, Dispatcher, types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from telethon import TelegramClient, errors
import phash_watcher

# ====== НАСТРОЙКИ ======
TARGET_CHAT_IDS = ["@leomatchbot"]
BOT_TOKEN = "8370317657:AAFzRV0IP1uY_we_FUhbVhbv62EGrLs73oE"
API_ID = 37610683
API_HASH = "c93f23137fd651f517e17c182ef99465"
ADMIN_ID = 7676178737   # <<<<< ТВОЙ TELEGRAM ID

OPERATORS = {
    7676178737,   # ты
    5652700066,   # второй пользователь
}
# ====== Сессия бота ======
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

async def notify_admin(text: str):
    try:
        await bot.send_message(ADMIN_ID, text, parse_mode="HTML")
    except Exception:
        pass

OPERATORS_FILE = "operators.json"

if os.path.exists(OPERATORS_FILE):
    with open(OPERATORS_FILE, "r", encoding="utf-8") as f:
        OPERATORS = set(json.load(f))
else:
    OPERATORS = set()

SESSION_FOLDER = "sessions"
os.makedirs(SESSION_FOLDER, exist_ok=True)

PHASH_STATE_FILE = "phash_state.json"
PERMISSIONS_FILE = "permissions.json"
if os.path.exists(PERMISSIONS_FILE):
    with open(PERMISSIONS_FILE, "r", encoding="utf-8") as f:
        permissions = json.load(f)
else:
    permissions = {}

PHASH_STATE_FILE = "phash_state.json"

if os.path.exists(PHASH_STATE_FILE):
    with open(PHASH_STATE_FILE, "r", encoding="utf-8") as f:
        phash_state = json.load(f)
else:
    phash_state = {}

def save_phash_state():
    with open(PHASH_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(phash_state, f, ensure_ascii=False, indent=2)

# Хранилище клиентов и кодов
clients = {}
pending_auth = {}
last_codes = {}
code_requests = {}  # {session_name: [user_ids]}

# === Меню (только список аккаунтов) ===
menu_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📂 Список аккаунтов")],
    ],
    resize_keyboard=True
)

def save_operators():
    with open(OPERATORS_FILE, "w", encoding="utf-8") as f:
        json.dump(list(OPERATORS), f, indent=2)

def save_operators():
    with open(OPERATORS_FILE, "w", encoding="utf-8") as f:
        json.dump(list(OPERATORS), f, indent=2)

def is_operator(user_id: int) -> bool:
    return user_id == ADMIN_ID or user_id in OPERATORS

@dp.message(Command("operators"))
async def operators_cmd(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Только админ")
        return

    if not OPERATORS:
        await message.answer("📭 Операторов нет")
        return

    text = "👥 <b>Операторы:</b>\n"
    for uid in OPERATORS:
        text += f"• {uid}\n"

    await message.answer(text, parse_mode="HTML")

@dp.message(Command("operators_add"))
async def operators_add(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Только админ")
        return

    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("❌ Используй: /operators_add <user_id>")
        return

    uid = int(parts[1])
    if uid in OPERATORS:
        await message.answer("⚠️ Уже оператор")
        return

    OPERATORS.add(uid)
    save_operators()
    await message.answer(f"✅ Пользователь {uid} добавлен в операторы")

@dp.message(Command("operators_remove"))
async def operators_remove(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Только админ")
        return

    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("❌ Используй: /operators_remove <user_id>")
        return

    uid = int(parts[1])
    if uid not in OPERATORS:
        await message.answer("⚠️ Не является оператором")
        return

    OPERATORS.remove(uid)
    save_operators()
    await message.answer(f"🗑 Пользователь {uid} удалён из операторов")


@dp.message(Command(commands=["db_size"]))
async def db_size_cmd(message):
    if not is_operator(message.from_user.id):
        await message.answer("⛔ Только админ может использовать эту команду")
        return

    db_path = "photos.db"
    if not os.path.exists(db_path):
        await message.answer("⚠️ База данных не найдена")
        return

    # размер файла
    size_bytes = os.path.getsize(db_path)
    size_mb = size_bytes / (1024 * 1024)

    # количество записей по таблице media
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM media")
        count = cursor.fetchone()[0]
        conn.close()
    except Exception as e:
        await message.answer(f"❌ Ошибка при подсчёте записей: {e}")
        return

    await message.answer(
        f"📦 Размер базы: {size_bytes} байт ({size_mb:.2f} MB)\n"
        f"📝 Количество записей: {count}"
    )
@dp.message(Command("import_db"))
async def import_db(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Только админ может загружать базу")
        return

    await message.answer(
        "📥 Отправь файл базы SQLite (`.db`), я добавлю данные в текущую базу"
    )

@dp.message(lambda m: m.document and m.from_user.id == ADMIN_ID)
async def handle_db_upload(message: types.Message):
    document = message.document

    if not document.file_name.endswith(".db"):
        await message.answer("❌ Это не файл базы `.db`")
        return

    temp_path = f"import_{document.file_name}"
    await bot.download(document, destination=temp_path)

    try:
        src_conn = sqlite3.connect(temp_path)
        src_cursor = src_conn.cursor()

        from phash_watcher import conn as main_conn, cursor as main_cursor

        imported = 0
        skipped = 0

        # 🔎 Проверяем, какие таблицы есть
        src_cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        tables = {row[0] for row in src_cursor.fetchall()}

        # ===== СТАРАЯ БАЗА (photos) =====
        if "photos" in tables:
            src_cursor.execute("SELECT phash FROM photos")
            rows = src_cursor.fetchall()

            for (hash_value,) in rows:
                main_cursor.execute(
                    "SELECT 1 FROM media WHERE hash = ? AND type = 'photo'",
                    (hash_value,)
                )
                if main_cursor.fetchone():
                    skipped += 1
                else:
                    main_cursor.execute(
                        "INSERT INTO media (hash, type, created_at) VALUES (?, 'photo', datetime('now'))",
                        (hash_value,)
                    )
                    imported += 1

        # ===== НОВАЯ БАЗА (media) =====
        elif "media" in tables:
            src_cursor.execute("SELECT hash, type FROM media")
            rows = src_cursor.fetchall()

            for hash_value, media_type in rows:
                main_cursor.execute(
                    "SELECT 1 FROM media WHERE hash = ? AND type = ?",
                    (hash_value, media_type)
                )
                if main_cursor.fetchone():
                    skipped += 1
                else:
                    main_cursor.execute(
                        "INSERT INTO media (hash, type, created_at) VALUES (?, ?, datetime('now'))",
                        (hash_value, media_type)
                    )
                    imported += 1
        else:
            await message.answer("❌ В базе нет подходящих таблиц (photos / media)")
            return

        main_conn.commit()
        src_conn.close()

        await message.answer(
            f"✅ Импорт завершён\n"
            f"➕ Добавлено: {imported}\n"
            f"⏭ Пропущено: {skipped}"
        )

    except Exception as e:
        await message.answer(f"❌ Ошибка импорта: {e}")

    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)

@dp.message(Command("export_db"))
async def export_db(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Только админ может выгружать базу")
        return

    db_path = "photos.db"
    if not os.path.exists(db_path):
        await message.answer("⚠️ База данных не найдена")
        return

    await message.answer_document(
        FSInputFile(db_path),
        caption="📦 Экспорт базы phash"
    )

# ====== ФУНКЦИИ СЕССИЙ ======
async def load_sessions():
    files = [f for f in os.listdir(SESSION_FOLDER) if f.endswith(".session")]
    for file in files:
        name = os.path.splitext(file)[0]
        path = os.path.join(SESSION_FOLDER, name)

        # 🔹 Прокси для России
        proxy = ('socks5', 'pool.proxy.market', 10014, True, '7abJSMc5umQJ', 'PoH5f3Xy')

        client = TelegramClient(
            path, API_ID, API_HASH,
            proxy=proxy,
            device_model=f"Device_{name}",
            system_version=f"Android {random.randint(6, 13)}",
            app_version=f"{random.randint(7, 9)}.{random.randint(1, 9)}.{random.randint(1, 9)}",
            lang_code="ru"
        )

        await client.connect()
        if await client.is_user_authorized():
            clients[name] = client
            await client.start()
        else:
            await client.disconnect()

async def add_account(phone: str, user_id: int):
    name = phone.replace("+", "")
    path = os.path.join(SESSION_FOLDER, name)

    # 🔹 Российский SOCKS5 прокси
    proxy = ('socks5', 'pool.proxy.market', 10014, True, '7abJSMc5umQJ', 'PoH5f3Xy')

    client = TelegramClient(
        path,
        API_ID,
        API_HASH,
        proxy=proxy,
        device_model=f"Device_{name}",
        system_version=f"Android {random.randint(6, 13)}",
        app_version=f"{random.randint(7, 9)}.{random.randint(1, 9)}.{random.randint(1, 9)}",
        lang_code="ru",
        system_lang_code="ru",
        use_ipv6=False
    )

    await client.connect()
    try:
        await client.send_code_request(phone)
        pending_auth[name] = {"client": client, "phone": phone, "user_id": user_id}
        return f"✅ Код отправлен на {phone}. Введи его командой: /code {name} 12345"
    except Exception as e:
        print(f"[DEBUG] Ошибка при отправке кода на {phone}: {e}")
        return f"⚠️ Ошибка: {e}"


async def confirm_code(name: str, code: str):
    if name not in pending_auth:
        return "⚠️ Нет ожидающей авторизации для этого аккаунта"
    client = pending_auth[name]["client"]
    try:
        await client.sign_in(code=code)
        if await client.is_user_authorized():
            clients[name] = client
            pending_auth.pop(name)
            await client.start()
            return f"✅ Аккаунт {name} успешно авторизован"
        else:
            return "❌ Авторизация не удалась"
    except errors.SessionPasswordNeededError:
        return "⚠️ У аккаунта включена 2FA. Используй /password"
    except Exception as e:
        return f"❌ Ошибка: {e}"

async def get_last_code(name: str):
    if name not in clients:
        return "⚠️ Аккаунт не найден"
    client = clients[name]
    try:
        messages = await client.get_messages(777000, limit=5)
        for msg in messages:
            match = re.search(r"\d{5}", msg.message)
            if match:
                code = match.group(0)
                last_codes[name] = code
                return f"{code}"
        return f"❌ Код для {name} не найден"
    except Exception as e:
        return f"⚠️ Ошибка при получении кода: {e}"


# ====== СИСТЕМА ДОСТУПОВ ======
def save_permissions():
    with open(PERMISSIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(permissions, f, ensure_ascii=False, indent=2)

def check_access(user_id, session_name=None):
    if user_id == ADMIN_ID:
        return True
    if str(user_id) in permissions:
        if session_name:
            return session_name in permissions[str(user_id)]
        return True
    return False

# ====== КОМАНДЫ ======
@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    help_text = (
        "📖 Доступные команды:\n"
        "/operators\n"
        "/operators_add\n"
        "/operators_remove\n"
        "/clear_permissions\n"
        "/help – показать это меню\n"
        "/db_size – Размер базы\n"
        "/export_db – Скачать текущую базу\n"
        "/import_db – Подготовка к загрузке новой базы\n"
        "/add <номер> – добавить аккаунт (только админ)\n"
        "/delete <имя_сессии> – удалить аккаунт (только админ)\n"
        "/code <имя_сессии> <код> – подтвердить код входа (только админ)\n"
        "/grant <user_id> <имя_сессии> – выдать доступ пользователю (только админ)\n"
    )
    await message.answer(help_text)

@dp.message(Command("add"))
async def add_account_cmd(message: types.Message):
    if not is_operator(message.from_user.id):

        await message.answer("⛔ Только админ может добавлять аккаунты")
        return
    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("❌ Используй формат: /add +79998887766")
        return
    phone = parts[1].strip()
    result = await add_account(phone, message.from_user.id)

    # 🔐 если добавляет оператор — сразу выдаём доступ к аккаунту
    if is_operator(message.from_user.id) and message.from_user.id != ADMIN_ID:
        session_name = phone.replace("+", "")
        uid = str(message.from_user.id)

        if uid not in permissions:
            permissions[uid] = []

        if session_name not in permissions[uid]:
            permissions[uid].append(session_name)
            save_permissions()

    if is_operator(message.from_user.id) and message.from_user.id != ADMIN_ID:
        await notify_admin(
            f"➕ <b>Оператор добавил аккаунт</b>\n"
            f"👤 ID: {message.from_user.id}\n"
            f"📞 Номер: {phone}"
        )

    await message.answer(result, reply_markup=menu_kb)


def build_account_keyboard(user_id: int, account_name: str):
    state = phash_state.get(account_name, True)
    state_text = "🟢 База: ВКЛ" if state else "🔴 База: ВЫКЛ"

    kb_buttons = [
        [
            InlineKeyboardButton(
                text=state_text,
                callback_data=f"toggle_phash:{account_name}"
            )
        ]
    ]

    if is_operator(user_id):
        kb_buttons.append([
            InlineKeyboardButton(
                text="🗑 Удалить сессию",
                callback_data=f"delete:{account_name}"
            )
        ])

    return InlineKeyboardMarkup(inline_keyboard=kb_buttons)


@dp.message(Command("delete"))
async def delete_account_cmd(message: types.Message):
    if not is_operator(message.from_user.id):

        await message.answer("⛔ Только админ может удалять аккаунты")
        return

    parts = message.text.split()
    if len(parts) != 2:
        await message.answer("❌ Используй формат: /delete <имя_сессии>\n"
                             f"Доступные: {', '.join(clients.keys()) if clients else 'нет'}")
        return

    name = parts[1]
    if name not in clients:
        await message.answer(f"⚠️ Аккаунт {name} не найден")
        return

    # Отключаем и удаляем сессию
    client = clients.pop(name)
    await client.disconnect()
    session_path = os.path.join(SESSION_FOLDER, f"{name}.session")
    if os.path.exists(session_path):
        os.remove(session_path)
    last_codes.pop(name, None)

    # ⚙️ Удаляем аккаунт из permissions
    removed_from = []
    for user_id in list(permissions.keys()):
        if name in permissions[user_id]:
            permissions[user_id].remove(name)
            if not permissions[user_id]:  # если у пользователя больше нет доступов — удаляем полностью
                del permissions[user_id]
            removed_from.append(user_id)

    save_permissions()

@dp.callback_query(lambda c: c.data.startswith("toggle_phash:"))
async def toggle_phash(callback: types.CallbackQuery):
    account = callback.data.split(":", 1)[1]

    if not check_access(callback.from_user.id, account):
        await callback.answer("⛔ Нет доступа", show_alert=True)
        return

    current = phash_state.get(account, True)
    phash_state[account] = not current
    save_phash_state()

    new_kb = build_account_keyboard(callback.from_user.id, account)

    try:
        await callback.message.edit_reply_markup(reply_markup=new_kb)
    except Exception:
        pass  # Telegram message is not modified — это нормально

    status = "🟢 ВКЛ" if phash_state[account] else "🔴 ВЫКЛ"
    await callback.answer(f"База {status}")


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "👋 Привет!\n"
        "/add <номер> – добавить аккаунт (только админ)\n"
        "/delete <имя_сессии> – удалить аккаунт (только админ)\n"
        "/code <имя_сессии> <код> – подтвердить код входа (только админ)\n"
        "/db_size – Размер базы\n",
        reply_markup=menu_kb
    )

@dp.message(lambda m: m.text == "📂 Список аккаунтов")
async def list_accounts(message: types.Message):
    global phash_state
    try:
        with open(PHASH_STATE_FILE, "r", encoding="utf-8") as f:
            phash_state = json.load(f)
    except Exception:
        phash_state = {}
    user_id = message.from_user.id
    user_id_str = str(user_id)

    # какие аккаунты доступны
    if user_id == ADMIN_ID:
        available = list(clients.keys())
    elif user_id_str in permissions:
        available = [name for name in permissions[user_id_str] if name in clients]
    else:
        available = []

    if not available:
        await message.answer("⚠️ Нет доступных аккаунтов", reply_markup=menu_kb)
        return

    for name in available:
        info_text = ""

        # ===== ВАЖНО: state_text объявляется ВСЕГДА =====
        state = phash_state.get(name, True)
        state_text = "🟢 База: ВКЛ" if state else "🔴 База: ВЫКЛ"

        kb_buttons = [
            [
                InlineKeyboardButton(
                    text=state_text,
                    callback_data=f"toggle_phash:{name}"
                )
            ]
        ]
        # инфо для админа
        if user_id == ADMIN_ID:
            granted_users = [uid for uid, accs in permissions.items() if name in accs]
            if granted_users:
                info_text += "👥 Доступ:\n"
                for uid in granted_users:
                    info_text += f"• {uid}\n"
            else:
                info_text += "🚫 Нет выданных доступов\n"

        # кнопки


        if is_operator(user_id):
            kb_buttons.append([
                InlineKeyboardButton(text="🗑 Удалить сессию", callback_data=f"delete:{name}")
            ])

        kb = InlineKeyboardMarkup(inline_keyboard=kb_buttons)

        await message.answer(
            f"🔹 <b>{name}</b>\n{info_text}",
            parse_mode="HTML",
            reply_markup=kb
        )




@dp.message(Command("clear_permissions"))
async def clear_permissions_cmd(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Только админ может использовать эту команду")
        return

    permissions.clear()  # очищаем все доступы
    save_permissions()   # сохраняем пустой файл

    await message.answer("🧹 Все пользовательские доступы удалены и permissions.json очищен")


# === УДАЛЕНИЕ СЕССИИ (через кнопку) ===
@dp.callback_query(lambda c: c.data.startswith("delete:"))
async def callback_delete_session(callback: types.CallbackQuery):
    if not is_operator(callback.from_user.id):
        await callback.answer("⛔ Только админ может удалять сессии")
        return

    name = callback.data.split(":", 1)[1]
    if name not in clients:
        await callback.message.answer(f"⚠️ Аккаунт {name} не найден")
        await callback.answer()
        return

    # Отключаем и удаляем клиента
    client = clients.pop(name)
    try:
        await client.disconnect()
    except Exception:
        pass

    # Удаляем .session файл
    session_path = os.path.join(SESSION_FOLDER, f"{name}.session")
    if os.path.exists(session_path):
        os.remove(session_path)
    last_codes.pop(name, None)

    # Удаляем из permissions
    removed_from = []
    for user_id in list(permissions.keys()):
        if name in permissions[user_id]:
            permissions[user_id].remove(name)
            if not permissions[user_id]:
                del permissions[user_id]
            removed_from.append(user_id)
    save_permissions()

    # Уведомляем
    text = f"🗑 Аккаунт <b>{name}</b> удалён."
    if removed_from:
        text += f"\n❎ Доступ удалён у пользователей: {', '.join(removed_from)}"

    if is_operator(callback.from_user.id) and callback.from_user.id != ADMIN_ID:
        await notify_admin(
            f"🗑 <b>Оператор удалил сессию</b>\n"
            f"👤 ID: {callback.from_user.id}\n"
            f"📂 Аккаунт: {name}"
        )


    await callback.message.answer(text, parse_mode="HTML", reply_markup=menu_kb)
    await callback.answer("✅ Удалено")


@dp.message(Command("code"))
async def enter_code(message: types.Message):
    if not is_operator(message.from_user.id):
        await message.answer("⛔ Только админ может вводить коды")
        return

    parts = message.text.split()
    if len(parts) != 3:
        await message.answer("❌ Используй формат: /code <имя_сессии> <код>")
        return

    name, code = parts[1], parts[2]
    result = await confirm_code(name, code)
    await message.answer(result, reply_markup=menu_kb)

    # 🔹 Если авторизация успешна — подключаем phash_watcher
    if "успешно авторизован" in result:
        client = clients.get(name)
        if client:
            try:
                bot_entity = await client.get_entity('@leomatchbot')
                BOT_CHAT_ID = bot_entity.id

                phash_watcher.attach_phash_handler(
                    client,
                    account_name=name,
                    target_chat_ids=[BOT_CHAT_ID],
                    allowed_senders=[BOT_CHAT_ID]
                )
                await message.answer(f"✅ PHASH обработчик подключен для {name}")
            except Exception as e:
                await message.answer(f"⚠️ Не удалось подключить PHASH для {name}: {e}")

    if is_operator(message.from_user.id) and message.from_user.id != ADMIN_ID:
        await notify_admin(
            f"🔐 <b>Оператор ввёл код</b>\n"
            f"👤 ID: {message.from_user.id}\n"
            f"📂 Аккаунт: {name}"
        )

@dp.message(Command("password"))
async def enter_password(message: types.Message):
    if not is_operator(message.from_user.id):
        await message.answer("⛔ Только админ может вводить пароль 2FA")
        return

    parts = message.text.split(maxsplit=2)
    if len(parts) != 3:
        await message.answer("❌ Используй формат: /password <имя_сессии> <пароль>")
        return

    name, password = parts[1], parts[2]

    if name not in pending_auth:
        await message.answer("⚠️ Нет ожидающей авторизации для этого аккаунта")
        return

    client = pending_auth[name]["client"]

    try:
        await client.sign_in(password=password)

        if not await client.is_user_authorized():
            await message.answer("❌ Авторизация не удалась")
            return

        # ✅ сохраняем клиента
        clients[name] = client
        pending_auth.pop(name)

        await client.start()

        # ✅ ПОДКЛЮЧАЕМ ОБРАБОТЧИКИ
        bot_entity = await client.get_entity("@leomatchbot")
        BOT_CHAT_ID = bot_entity.id

        phash_watcher.attach_phash_handler(
            client,
            account_name=name,
            target_chat_ids=[BOT_CHAT_ID],
            allowed_senders=[BOT_CHAT_ID],
        )

        await message.answer(
            f"✅ Аккаунт {name} успешно авторизован с 2FA\n"
            f"🧠 PHASH обработчик подключён",
            reply_markup=menu_kb
        )

    except Exception as e:
        await message.answer(f"⚠️ Ошибка при вводе пароля: {e}")


# ====== ЗАПУСК ======
async def main():
    # 1️⃣ Загружаем все сессии
    await load_sessions()
    print("✅ Все сессии загружены")

    # 2️⃣ Подключаем phash_watcher для каждого клиента
    for name, client in clients.items():
        # Проверяем авторизацию
        if not await client.is_user_authorized():
            print(f"[DEBUG] Аккаунт {name} не авторизован, пропускаем")
            continue

        # Получаем entity бота для этого клиента
        try:
            bot_entity = await client.get_entity('@leomatchbot')
            BOT_CHAT_ID = bot_entity.id
        except Exception as e:
            print(f"[DEBUG] Не удалось получить ID бота для {name}: {e}")
            continue

        # Подключаем обработчик
        phash_watcher.attach_phash_handler(
            client,
            account_name=name,
            target_chat_ids=[BOT_CHAT_ID],   # реагируем только в чате с ботом
            allowed_senders=[BOT_CHAT_ID]    # сообщения только от бота
        )
        print(f"[DEBUG] Обработчик подключен для {name}")

    # 3️⃣ Запускаем aiogram бота
    await dp.start_polling(bot)





if __name__ == "__main__":

    asyncio.run(main())




