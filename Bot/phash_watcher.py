import os
import json
import asyncio
import sqlite3
from datetime import datetime
from PIL import Image
import imagehash
import cv2
from telethon import events

# ================= НАСТРОЙКИ =================
DB_PATH = "photos.db"
PHOTO_DIR = "photos"
VIDEO_DIR = "videos"
PHASH_DISTANCE = 6

TRIGGER_TEXT = "Кому-то понравилась твоя анкета"
CONFIRM_TEXT = "Начинай общаться"

ATTACHED_ACCOUNTS = set()
HANDLER_COUNT = {}

os.makedirs(PHOTO_DIR, exist_ok=True)
os.makedirs(VIDEO_DIR, exist_ok=True)

# ================= СОСТОЯНИЯ =================
ACCOUNT_STATE = {}      # account_name -> "ACTIVE" | "WAIT_CONFIRM"
PENDING_RESULT = {}    # account_name -> {"hash": str, "type": "photo|video"}

# ================= БАЗА =================
db_lock = asyncio.Lock()
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

async def save_hash(hash_value: str, media_type: str):
    async with db_lock:
        cursor.execute(
            "INSERT INTO media (hash, type, created_at) VALUES (?, ?, ?)",
            (hash_value, media_type, datetime.utcnow().isoformat())
        )
        conn.commit()

# ================= СОСТОЯНИЕ PHASH =================
def is_phash_enabled(account_name: str) -> bool:
    try:
        with open("phash_state.json", "r", encoding="utf-8") as f:
            state = json.load(f)
        return state.get(account_name, True)
    except Exception:
        return True

# ================= HANDLER =================
def attach_phash_handler(client, account_name: str, target_chat_ids=None, allowed_senders=None):
    if account_name in ATTACHED_ACCOUNTS:
        print(f"[PHASH] handler already attached for {account_name}")
        return

    ATTACHED_ACCOUNTS.add(account_name)
    HANDLER_COUNT[account_name] = HANDLER_COUNT.get(account_name, 0) + 1

    ACCOUNT_STATE.setdefault(account_name, "ACTIVE")
    PENDING_RESULT.setdefault(account_name, None)

    print(f"[PHASH] handler attached for {account_name}")

    if isinstance(target_chat_ids, int):
        target_chat_ids = [target_chat_ids]
    if isinstance(allowed_senders, int):
        allowed_senders = [allowed_senders]

    @client.on(events.NewMessage)
    async def handler(event):
        msg = event.message

        # ===== фильтры =====
        if not is_phash_enabled(account_name):
            return
        if target_chat_ids and msg.chat_id not in target_chat_ids:
            return
        if allowed_senders and msg.sender_id not in allowed_senders:
            return
        if not msg.message:
            return

        text = msg.message.strip()
        state = ACCOUNT_STATE.get(account_name, "ACTIVE")

        # =====================================================
        # 1️⃣ ОЖИДАНИЕ ПОДТВЕРЖДЕНИЯ
        # =====================================================
        if state == "WAIT_CONFIRM":
            if CONFIRM_TEXT.lower() in text.lower():
                pending = PENDING_RESULT.get(account_name)
                if pending:
                    await save_hash(pending["hash"], pending["type"])
                    PENDING_RESULT[account_name] = None

                ACCOUNT_STATE[account_name] = "ACTIVE"
                print(f"[PHASH] {account_name} → CONFIRMED, back to ACTIVE")
            return

        # =====================================================
        # 2️⃣ АКТИВНОЕ СОСТОЯНИЕ — ЖДЁМ АНКЕТУ
        # =====================================================
        if TRIGGER_TEXT.lower() not in text.lower():
            return

        # ===== ФОТО =====
        if msg.photo:
            file_path = os.path.join(PHOTO_DIR, f"{account_name}_{msg.id}.jpg")
            await client.download_media(msg.photo, file_path)

            try:
                phash = calculate_image_phash(file_path)
                is_dup = await is_duplicate(phash, "photo")

                await client.send_message(
                    event.chat_id,
                    "👎" if is_dup else "❤️"
                )

                PENDING_RESULT[account_name] = {
                    "hash": phash,
                    "type": "photo"
                }
                ACCOUNT_STATE[account_name] = "WAIT_CONFIRM"
                print(f"[PHASH] {account_name} → WAIT_CONFIRM (photo)")

            finally:
                os.remove(file_path)

        # ===== ВИДЕО =====
        elif msg.video:
            file_path = os.path.join(VIDEO_DIR, f"{account_name}_{msg.id}.mp4")
            await client.download_media(msg.video, file_path)

            try:
                vhash = calculate_video_phash(file_path)
                is_dup = await is_duplicate(vhash, "video")

                await client.send_message(
                    event.chat_id,
                    "👎" if is_dup else "❤️"
                )

                PENDING_RESULT[account_name] = {
                    "hash": vhash,
                    "type": "video"
                }
                ACCOUNT_STATE[account_name] = "WAIT_CONFIRM"
                print(f"[PHASH] {account_name} → WAIT_CONFIRM (video)")

            finally:
                os.remove(file_path)
