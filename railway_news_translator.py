import os
import re
import json
import asyncio
import logging
import hashlib
from pathlib import Path
from datetime import datetime, timezone

from aiogram import Bot
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from deep_translator import GoogleTranslator

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("news-translator")

API_ID = int(os.getenv("API_ID", "0").strip() or "0")
API_HASH = os.getenv("API_HASH", "").strip()
SESSION_STRING = os.getenv("SESSION_STRING", "").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
SOURCE_CHANNEL = os.getenv("SOURCE_CHANNEL", "@HoopoeAr").strip()
TARGET_CHANNEL = os.getenv("TARGET_CHANNEL", "@rasedpersian").strip()
POLL_INTERVAL = max(30, int(os.getenv("POLL_INTERVAL", "60")))

if not API_ID or not API_HASH or not SESSION_STRING or not BOT_TOKEN:
    missing = [name for name, value in {
        "API_ID": API_ID,
        "API_HASH": API_HASH,
        "SESSION_STRING": SESSION_STRING,
        "BOT_TOKEN": BOT_TOKEN,
    }.items() if not value]
    raise RuntimeError("Missing Railway Variables: " + ", ".join(missing))

# Railway's filesystem may be ephemeral; mount a volume and set DATA_DIR to its mount path
# if you need the deduplication database to persist across redeploys.
DATA_DIR = Path(os.getenv("DATA_DIR", "/tmp/news-translator"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
SEEN_FILE = DATA_DIR / "seen_posts.json"

try:
    seen_posts = set(json.loads(SEEN_FILE.read_text("utf-8")))
except (OSError, ValueError, TypeError):
    seen_posts = set()

seen_lock = asyncio.Lock()


def save_seen():
    tmp = SEEN_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(sorted(seen_posts), ensure_ascii=False), "utf-8")
    tmp.replace(SEEN_FILE)


def post_key(message):
    raw = f"{message.chat_id}:{message.id}"
    return hashlib.sha256(raw.encode()).hexdigest()


def translate_text(text: str) -> str:
    """Translate Arabic to Persian; preserve the original if translation fails."""
    if not text or not text.strip():
        return ""
    # Translate in chunks to stay below common translation-service limits.
    chunks = re.split(r"(\n+)", text)
    out = []
    translator = GoogleTranslator(source="auto", target="fa")
    for chunk in chunks:
        if not chunk or chunk.isspace():
            out.append(chunk)
            continue
        if len(chunk) <= 4500:
            out.append(translator.translate(chunk) or chunk)
        else:
            pieces = [chunk[i:i + 4000] for i in range(0, len(chunk), 4000)]
            out.extend(translator.translate(piece) or piece for piece in pieces)
    return "".join(out)


def append_source(text: str, source_url: str) -> str:
    text = (text or "").strip()
    footer = f"\n\n📰 منبع: {source_url}" if source_url else ""
    return (text + footer).strip()


async def main():
    bot = Bot(BOT_TOKEN)
    client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
    await client.start()

    try:
        source = await client.get_entity(SOURCE_CHANNEL)
        # Validate bot access to the destination before listening.
        try:
            await bot.get_chat(TARGET_CHANNEL)
        except Exception as exc:
            raise RuntimeError(
                f"Bot cannot access target {TARGET_CHANNEL}. Add the bot as an admin with permission to post."
            ) from exc

        log.info("Listening for new posts from %s; publishing to %s", SOURCE_CHANNEL, TARGET_CHANNEL)

        async def publish_message(message):
            key = post_key(message)
            async with seen_lock:
                if key in seen_posts:
                    return
                # Reserve before sending to avoid duplicate concurrent processing.
                seen_posts.add(key)
                save_seen()

            source_url = f"https://t.me/{SOURCE_CHANNEL.lstrip('@')}/{message.id}" if SOURCE_CHANNEL.startswith("@") else ""
            original = message.message or ""
            try:
                translated = await asyncio.to_thread(translate_text, original)
            except Exception:
                log.exception("Translation failed for source post %s; keeping original text", message.id)
                translated = original

            caption = append_source(translated, source_url)
            try:
                if message.media:
                    # Download and re-upload media so the target post is independent of source forwarding.
                    media_path = await message.download_media(file=str(DATA_DIR))
                    if media_path:
                        try:
                            if caption:
                                await bot.send_document(TARGET_CHANNEL, document=__import__('aiogram').types.FSInputFile(media_path), caption=caption[:1024])
                            else:
                                await bot.send_document(TARGET_CHANNEL, document=__import__('aiogram').types.FSInputFile(media_path))
                            # For common image/video files, send as photo/video instead of document.
                            # The document fallback above keeps file quality and supports most media types.
                        finally:
                            try:
                                os.remove(media_path)
                            except OSError:
                                pass
                    elif caption:
                        await bot.send_message(TARGET_CHANNEL, caption[:4096])
                elif caption:
                    await bot.send_message(TARGET_CHANNEL, caption[:4096], disable_web_page_preview=False)
            except Exception:
                async with seen_lock:
                    seen_posts.discard(key)
                    save_seen()
                log.exception("Publishing failed for source post %s", message.id)

        @client.on(events.NewMessage(chats=source))
        async def on_new_message(event):
            # Telegram albums arrive as separate messages; each item is preserved as a post.
            await publish_message(event.message)

        # Catch up on a small recent window after restarts; deduplication prevents repeats.
        async for message in client.iter_messages(source, limit=20, reverse=True):
            if message.date and (datetime.now(timezone.utc) - message.date.replace(tzinfo=timezone.utc)).total_seconds() < 24 * 3600:
                await publish_message(message)

        await client.run_until_disconnected()
    finally:
        await client.disconnect()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
