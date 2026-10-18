import asyncio, json, logging, os, re, tempfile
from pathlib import Path
from typing import Optional

from aiogram import Bot
from deep_translator import GoogleTranslator
from telethon import TelegramClient, events
from telethon.sessions import StringSession

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("news-translator")

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SESSION_STRING = os.environ["SESSION_STRING"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
SOURCE_CHANNEL = os.getenv("SOURCE_CHANNEL", "HoopoeAr").lstrip("@")
TARGET_CHANNEL = os.getenv("TARGET_CHANNEL", "@rasedpersian")
CATCHUP_LIMIT = max(0, int(os.getenv("CATCHUP_LIMIT", "20")))
TRANSLATE_REQUIRED = os.getenv("TRANSLATE_REQUIRED", "true").lower() not in {"0", "false", "no", "off"}
DATA_DIR = Path(os.getenv("DATA_DIR", "/tmp/news-translator"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
SEEN_FILE = DATA_DIR / "seen_posts.json"

proxy = None
if os.getenv("PROXY_HOST") and os.getenv("PROXY_PORT"):
    proxy = ("socks5", os.getenv("PROXY_HOST"), int(os.getenv("PROXY_PORT")))

bot = Bot(token=BOT_TOKEN)
client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH, proxy=proxy)
translator = GoogleTranslator(source="auto", target="fa")
seen_ids = set()

def load_seen():
    global seen_ids
    try:
        if SEEN_FILE.exists():
            seen_ids = {int(x) for x in json.loads(SEEN_FILE.read_text(encoding="utf-8"))}
    except Exception:
        log.exception("Could not load seen post IDs")

def save_seen():
    try:
        SEEN_FILE.write_text(json.dumps(sorted(seen_ids)), encoding="utf-8")
    except Exception:
        log.exception("Could not save seen post IDs")

def clean_text(text: Optional[str]) -> str:
    if not text:
        return ""
    lines = []
    for line in text.splitlines():
        if re.search(r"(?:منبع|source)\s*[:：]", line, re.I):
            continue
        if re.search(r"https?://t\.me/(?:HoopoeAr|rasedpersian)\b", line, re.I):
            continue
        lines.append(line)
    return "\n".join(lines).strip()

def split_text(text: str, limit: int = 4000):
    text = text.strip()
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        yield text[:cut].strip()
        text = text[cut:].strip()
    if text:
        yield text

async def translate_text(text: str) -> str:
    text = clean_text(text)
    if not text:
        return ""
    result = await asyncio.to_thread(translator.translate, text)
    result = clean_text(result or "")
    if not result and TRANSLATE_REQUIRED:
        raise RuntimeError("Translation returned an empty result")
    return result

async def send_translated_message(message):
    if message.id in seen_ids:
        return
    original = clean_text(message.message or "")
    translated = ""
    if original:
        try:
            translated = await translate_text(original)
        except Exception:
            log.exception("Translation failed for message %s", message.id)
            if TRANSLATE_REQUIRED:
                return
            translated = original

    caption = translated[:1024] if translated else None
    remainder = translated[1024:].strip() if len(translated) > 1024 else ""
    media_path = None
    try:
        if message.media:
            suffix = ""
            mime = (getattr(message.file, "mime_type", "") or "").lower() if message.file else ""
            suffix = { "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
                       "video/mp4": ".mp4", "audio/mpeg": ".mp3", "audio/ogg": ".ogg" }.get(mime, "")
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                media_path = tmp.name
            downloaded = await message.download_media(file=media_path)
            if downloaded:
                handle = open(downloaded, "rb")
                try:
                    if mime.startswith("image/"):
                        await bot.send_photo(TARGET_CHANNEL, photo=handle, caption=caption)
                    elif mime.startswith("video/"):
                        await bot.send_video(TARGET_CHANNEL, video=handle, caption=caption, supports_streaming=True)
                    elif mime.startswith("audio/"):
                        await bot.send_audio(TARGET_CHANNEL, audio=handle, caption=caption)
                    else:
                        await bot.send_document(TARGET_CHANNEL, document=handle, caption=caption)
                finally:
                    handle.close()
            elif translated:
                for chunk in split_text(translated):
                    await bot.send_message(TARGET_CHANNEL, chunk)
        elif translated:
            for chunk in split_text(translated):
                await bot.send_message(TARGET_CHANNEL, chunk)

        if remainder:
            for chunk in split_text(remainder):
                await bot.send_message(TARGET_CHANNEL, chunk)
        seen_ids.add(message.id)
        save_seen()
        log.info("Relayed message %s without source footer", message.id)
    except Exception:
        log.exception("Failed to relay message %s", message.id)
    finally:
        if media_path:
            try:
                Path(media_path).unlink(missing_ok=True)
            except Exception:
                pass

async def main():
    load_seen()
    await client.start()
    me = await client.get_me()
    log.info("Telethon connected as %s", getattr(me, "username", None) or me.id)
    source = await client.get_entity(SOURCE_CHANNEL)
    log.info("Watching @%s -> %s", SOURCE_CHANNEL, TARGET_CHANNEL)

    @client.on(events.NewMessage(chats=source))
    async def on_new_message(event):
        await send_translated_message(event.message)

    if CATCHUP_LIMIT:
        for message in reversed(await client.get_messages(source, limit=CATCHUP_LIMIT)):
            if message.id not in seen_ids:
                await send_translated_message(message)
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
