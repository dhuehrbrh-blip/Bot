import os
import json
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

def is_duplicate(hash_value: str, media_type: str) -> bool:
    cursor.execute(
        "SELECT hash FROM media WHERE type = ?",
        (media_type,)
    )
    for (db_hash,) in cursor.fetchall():
        if imagehash.hex_to_hash(hash_value) - imagehash.hex_to_hash(db_hash) <= PHASH_DISTANCE:
            return True
    return False

def save_hash(hash_value: str, media_type: str):
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
    if isinstance(target_chat_ids, int):
        target_chat_ids = [target_chat_ids]
    if isinstance(allowed_senders, int):
        allowed_senders = [allowed_senders]

    @client.on(events.NewMessage)
    async def handler(event):
        msg = event.message

        if not is_phash_enabled(account_name):
            return

        if target_chat_ids and msg.chat_id not in target_chat_ids:
            return

        if allowed_senders and msg.sender_id not in allowed_senders:
            return

        if not msg.message or TRIGGER_TEXT.lower() not in msg.message.lower():
            return

        # ===== ФОТО =====
        if msg.photo:
            file_path = os.path.join(PHOTO_DIR, f"{account_name}_{msg.id}.jpg")
            await client.download_media(msg.photo, file_path)

            try:
                phash = calculate_image_phash(file_path)
                if is_duplicate(phash, "photo"):
                    await event.reply("👎")
                else:
                    save_hash(phash, "photo")
                    await event.reply("❤️")
            finally:
                os.remove(file_path)

        # ===== ВИДЕО =====
        elif msg.video:
            file_path = os.path.join(VIDEO_DIR, f"{account_name}_{msg.id}.mp4")
            await client.download_media(msg.video, file_path)

            try:
                vhash = calculate_video_phash(file_path)
                if is_duplicate(vhash, "video"):
                    await event.reply("👎")
                else:
                    save_hash(vhash, "video")
                    await event.reply("❤️")
            finally:
                os.remove(file_path)
