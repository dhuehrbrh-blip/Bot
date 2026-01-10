import os
import json
import asyncio
import sqlite3
from datetime import datetime
from PIL import Image
import imagehash
import cv2
from telethon import events

db_lock = asyncio.Lock() 
PENDING_QUEUE = {}  # {account_name: [pending_item, ...]}
PENDING_DELAY = 3   # секунды задержки перед записью в БД
# ================= НАСТРОЙКИ =================
DB_PATH = "photos.db"
PHOTO_DIR = "photos"
VIDEO_DIR = "videos"
PHASH_DISTANCE = 6
TRIGGER_TEXT = "Кому-то понравилась твоя анкета"
ATTACHED_ACCOUNTS = set()
HANDLER_COUNT = {} 
os.makedirs(PHOTO_DIR, exist_ok=True)
os.makedirs(VIDEO_DIR, exist_ok=True)

# ================= БАЗА ДАННЫХ =================
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cursor = conn.cursor()
cursor.execute("""
CREATE TABLE IF NOT EXISTS media (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hash TEXT NOT NULL,
    type TEXT CHECK(type IN ('photo', 'video')),
    created_at TEXT
)
""")
conn.commit()

# ================= PHASH =================
def calculate_image_phash(path: str) -> str:
    img = Image.open(path).convert("RGB")
    return str(imagehash.phash(img))

def calculate_video_phash(path: str) -> str:
    cap = cv2.VideoCapture(path)
    success, frame = cap.read()
    cap.release()

    if not success:
        raise ValueError("Не удалось прочитать видео")

    frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    img = Image.fromarray(frame_rgb)
    return str(imagehash.phash(img))

async def is_duplicate(hash_value: str, media_type: str) -> bool:
    async with db_lock:
        cursor.execute(
            "SELECT hash FROM media WHERE type = ?",
            (media_type,)
        )
        for (db_hash,) in cursor.fetchall():
            if imagehash.hex_to_hash(hash_value) - imagehash.hex_to_hash(db_hash) <= PHASH_DISTANCE:
                return True
    return False

async def delayed_commit(account_name: str, pending_item: dict):
    try:
        await asyncio.sleep(PENDING_DELAY)

        # если элемент всё ещё в очереди — сохраняем
        queue = PENDING_QUEUE.get(account_name, [])
        if pending_item in queue:
            await save_hash(pending_item["hash"], pending_item["type"])
            queue.remove(pending_item)

    except asyncio.CancelledError:
        # 💤 отменил запись
        pass

async def save_hash(hash_value: str, media_type: str):
    async with db_lock:  # гарантируем последовательный доступ
        cursor.execute(
            "INSERT INTO media (hash, type, created_at) VALUES (?, ?, ?)",
            (hash_value, media_type, datetime.utcnow().isoformat())
        )
        conn.commit()

# ================= СОСТОЯНИЕ =================
def is_phash_enabled(account_name: str) -> bool:
    try:
        with open("phash_state.json", "r", encoding="utf-8") as f:
            state = json.load(f)
        return state.get(account_name, True)
    except Exception:
        return True

# ================= HANDLER =================
def attach_phash_handler(client, account_name: str, target_chat_ids=None, allowed_senders=None):
    # 🔒 защита от повторного подключения
    if account_name in ATTACHED_ACCOUNTS:
        print(f"[PHASH] handler already attached for {account_name}")
        return

    ATTACHED_ACCOUNTS.add(account_name)
    PENDING_QUEUE.setdefault(account_name, [])
    HANDLER_COUNT[account_name] = HANDLER_COUNT.get(account_name, 0) + 1
    print(f"[PHASH] handler attached for {account_name} (total: {HANDLER_COUNT[account_name]})")

    if isinstance(target_chat_ids, int):
        target_chat_ids = [target_chat_ids]
    if isinstance(allowed_senders, int):
        allowed_senders = [allowed_senders]

    @client.on(events.NewMessage)
    async def handler(event):
        msg = event.message

        # ===== базовые фильтры =====
        if not is_phash_enabled(account_name):
            return
        if target_chat_ids and msg.chat_id not in target_chat_ids:
            return
        if allowed_senders and msg.sender_id not in allowed_senders:
            return
        if not msg.message:
            return

        text = msg.message.strip()

        # ===== 💤 отмена последней анкеты =====
        if text == "💤":
            queue = PENDING_QUEUE.get(account_name, [])
            if queue:
                last = queue.pop()
                last["task"].cancel()
                print(f"[SLEEP] last pending cancelled for {account_name}")
            return

        # реагируем ТОЛЬКО на анкеты
        if TRIGGER_TEXT.lower() not in text.lower():
            return

        # ===== ФОТО =====
        if msg.photo:
            file_path = os.path.join(PHOTO_DIR, f"{account_name}_{msg.id}.jpg")
            await client.download_media(msg.photo, file_path)

            try:
                phash = calculate_image_phash(file_path)

                # проверка: база + pending
                is_dup = await is_duplicate(phash, "photo") or any(
                    p["hash"] == phash and p["type"] == "photo"
                    for p in PENDING_QUEUE[account_name]
                )

                await client.send_message(
                    event.chat_id,
                    "👎" if is_dup else "❤️"
                )

                # ⏳ кладём в pending
                pending_item = {
                    "hash": phash,
                    "type": "photo"
                }
                task = asyncio.create_task(
                    delayed_commit(account_name, pending_item)
                )
                pending_item["task"] = task
                PENDING_QUEUE[account_name].append(pending_item)

            finally:
                os.remove(file_path)

        # ===== ВИДЕО =====
        elif msg.video:
            file_path = os.path.join(VIDEO_DIR, f"{account_name}_{msg.id}.mp4")
            await client.download_media(msg.video, file_path)

            try:
                vhash = calculate_video_phash(file_path)

                is_dup = await is_duplicate(vhash, "video") or any(
                    p["hash"] == vhash and p["type"] == "video"
                    for p in PENDING_QUEUE[account_name]
                )

                await client.send_message(
                    event.chat_id,
                    "👎" if is_dup else "❤️"
                )

                pending_item = {
                    "hash": vhash,
                    "type": "video"
                }
                task = asyncio.create_task(
                    delayed_commit(account_name, pending_item)
                )
                pending_item["task"] = task
                PENDING_QUEUE[account_name].append(pending_item)

            finally:
                os.remove(file_path)
