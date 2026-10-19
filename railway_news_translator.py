import asyncio, json, logging, os, re, tempfile, urllib.request, urllib.error
from pathlib import Path
from typing import Optional

from aiogram import Bot
from aiogram.types import FSInputFile, InputMediaPhoto, InputMediaVideo, InputMediaDocument
from telethon import TelegramClient, events
from telethon.sessions import StringSession

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("news-translator")

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SESSION_STRING = os.environ["SESSION_STRING"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
# Accept either SOURCE_CHANNELS (comma-separated) or the older SOURCE_CHANNEL variable.
# SOURCE_CHANNELS takes priority if both are set.
_sources_raw = os.getenv("SOURCE_CHANNELS", "").strip()
if not _sources_raw:
    _sources_raw = os.getenv("SOURCE_CHANNEL", "HoopoeAr,kataebAlqasam").strip()
SOURCE_CHANNELS = list(dict.fromkeys(
    x.strip().strip().lstrip("@").strip()
    for x in re.split(r"[,;\n]+", _sources_raw)
    if x.strip().strip().lstrip("@")
))
TARGET_CHANNEL = os.getenv("TARGET_CHANNEL", "@rasedpersian")
TAG_TEXT = os.getenv("TAG_TEXT", "✨ @rasedpersian").strip()
CATCHUP_LIMIT = max(0, int(os.getenv("CATCHUP_LIMIT", "0")))
TRANSLATE_REQUIRED = os.getenv("TRANSLATE_REQUIRED", "true").lower() not in {"0", "false", "no", "off"}
DATA_DIR = Path(os.getenv("DATA_DIR", "/tmp/news-translator"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
SEEN_FILE = DATA_DIR / "seen_posts.json"

proxy = None
if os.getenv("PROXY_HOST") and os.getenv("PROXY_PORT"):
    proxy = ("socks5", os.getenv("PROXY_HOST"), int(os.getenv("PROXY_PORT")))

bot = Bot(token=BOT_TOKEN)
client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH, proxy=proxy)
LIBRETRANSLATE_URL = os.getenv("LIBRETRANSLATE_URL", "http://libretranslate.railway.internal:5000/translate")
LIBRETRANSLATE_API_KEY = os.getenv("LIBRETRANSLATE_API_KEY", "")
seen_ids = set()

def load_seen():
    global seen_ids
    try:
        if SEEN_FILE.exists():
            seen_ids = {str(x) for x in json.loads(SEEN_FILE.read_text(encoding="utf-8"))}
    except Exception:
        log.exception("Could not load seen post IDs")

def save_seen():
    try:
        SEEN_FILE.write_text(json.dumps(sorted(seen_ids)), encoding="utf-8")
    except Exception:
        log.exception("Could not save seen post IDs")

def remove_emojis(text: str) -> str:
    # Remove common emoji, pictographs, flags, and variation selectors.
    emoji_pattern = re.compile(
        "["
        "\U0001F1E6-\U0001F1FF"
        "\U0001F300-\U0001FAFF"
        "\U00002600-\U000027BF"
        "\U0000FE0F"
        "\U0000200D"
        "]+",
        flags=re.UNICODE,
    )
    return emoji_pattern.sub("", text)


def clean_text(text: Optional[str]) -> str:
    if not text:
        return ""
    text = remove_emojis(text)
    lines = []
    for line in text.splitlines():
        if re.search(r"(?:منبع|source)\s*[:：]", line, re.I):
            continue
        if re.search(r"https?://t\.me/(?:HoopoeAr|rasedpersian)\b", line, re.I):
            continue
        lines.append(line)
    return "\n".join(lines).strip()

def add_tag(text: str) -> str:
    """Append the configured channel tag once; premium custom emoji IDs need separate entities."""
    text = (text or "").strip()
    tag = TAG_TEXT
    if not tag:
        return text
    if tag in text:
        return text
    return f"{text}\n\n{tag}" if text else tag


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

def _libretranslate_request(text: str) -> str:
    payload = {
        "q": text,
        "source": "auto",
        "target": "fa",
        "format": "text",
    }
    if LIBRETRANSLATE_API_KEY:
        payload["api_key"] = LIBRETRANSLATE_API_KEY
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        LIBRETRANSLATE_URL,
        data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=45) as response:
        body = json.loads(response.read().decode("utf-8"))
    return body.get("translatedText", "")


async def translate_text(text: str) -> str:
    text = clean_text(text)
    if not text:
        return ""
    last_error = None
    for attempt in range(5):
        try:
            result = await asyncio.to_thread(_libretranslate_request, text)
            result = clean_text(result or "")
            if not result and TRANSLATE_REQUIRED:
                raise RuntimeError("LibreTranslate returned an empty result")
            return result
        except Exception as exc:
            last_error = exc
            if attempt < 4:
                wait_seconds = min(2 ** attempt, 16)
                log.warning("LibreTranslate attempt %s/5 failed; retrying in %s seconds: %s",
                            attempt + 1, wait_seconds, exc)
                await asyncio.sleep(wait_seconds)
    raise RuntimeError(f"LibreTranslate failed after 5 attempts: {last_error}") from last_error

async def send_translated_message(message, source_key: str):
    post_key = f"{source_key}:{message.id}"
    if post_key in seen_ids or str(message.id) in seen_ids:
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

    caption_text = add_tag(translated) if translated else add_tag("")
    caption = caption_text[:1024] if caption_text else None
    remainder = caption_text[1024:].strip() if len(caption_text) > 1024 else ""
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
                upload_file = FSInputFile(downloaded)
                if mime.startswith("image/"):
                    await bot.send_photo(TARGET_CHANNEL, photo=upload_file, caption=caption)
                elif mime.startswith("video/"):
                    await bot.send_video(TARGET_CHANNEL, video=upload_file, caption=caption, supports_streaming=True)
                elif mime.startswith("audio/"):
                    await bot.send_audio(TARGET_CHANNEL, audio=upload_file, caption=caption)
                else:
                    await bot.send_document(TARGET_CHANNEL, document=upload_file, caption=caption)
            elif translated:
                for chunk in split_text(translated):
                    await bot.send_message(TARGET_CHANNEL, chunk)
        elif translated:
            for chunk in split_text(translated):
                await bot.send_message(TARGET_CHANNEL, chunk)

        if remainder:
            for chunk in split_text(remainder):
                await bot.send_message(TARGET_CHANNEL, chunk)
        seen_ids.add(post_key)
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

async def send_translated_album(messages, source_key: str):
    """Send a Telegram media group as one album, with translated caption on the first item."""
    if not messages:
        return
    messages = sorted(messages, key=lambda item: item.id)
    album_key = f"{source_key}:album:{messages[0].grouped_id or messages[0].id}"
    if album_key in seen_ids:
        return

    original = ""
    for item in messages:
        if item.message:
            original = clean_text(item.message)
            if original:
                break
    translated = ""
    if original:
        try:
            translated = await translate_text(original)
        except Exception:
            log.exception("Album translation failed for %s", album_key)
            if TRANSLATE_REQUIRED:
                return

    media_items = []
    paths = []
    try:
        for index, item in enumerate(messages):
            if not item.media:
                continue
            mime = (getattr(item.file, "mime_type", "") or "").lower() if item.file else ""
            suffix = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "video/mp4": ".mp4"}.get(mime, ".bin")
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                path = tmp.name
            paths.append(path)
            downloaded = await item.download_media(file=path)
            if not downloaded:
                continue
            upload = FSInputFile(downloaded)
            caption = (add_tag(translated)[:1024] if index == 0 else None)
            if mime.startswith("video/"):
                media_items.append(InputMediaVideo(media=upload, caption=caption, supports_streaming=True))
            elif mime.startswith("image/"):
                media_items.append(InputMediaPhoto(media=upload, caption=caption))
            else:
                media_items.append(InputMediaDocument(media=upload, caption=caption))

        if len(media_items) >= 2:
            await bot.send_media_group(TARGET_CHANNEL, media=media_items)
        elif len(media_items) == 1:
            only = media_items[0]
            if isinstance(only, InputMediaVideo):
                await bot.send_video(TARGET_CHANNEL, video=only.media, caption=only.caption, supports_streaming=True)
            elif isinstance(only, InputMediaPhoto):
                await bot.send_photo(TARGET_CHANNEL, photo=only.media, caption=only.caption)
            else:
                await bot.send_document(TARGET_CHANNEL, document=only.media, caption=only.caption)
        elif translated:
            for chunk in split_text(translated):
                await bot.send_message(TARGET_CHANNEL, chunk)

        caption_text = add_tag(translated) if translated else add_tag("")
        remainder = caption_text[1024:].strip() if len(caption_text) > 1024 else ""
        if remainder:
            for chunk in split_text(remainder):
                await bot.send_message(TARGET_CHANNEL, chunk)
        seen_ids.add(album_key)
        for item in messages:
            seen_ids.add(f"{source_key}:{item.id}")
        save_seen()
        log.info("Relayed album %s (%s items)", album_key, len(media_items))
    except Exception:
        log.exception("Failed to relay album %s", album_key)
    finally:
        for path in paths:
            try:
                Path(path).unlink(missing_ok=True)
            except Exception:
                pass


async def main():
    load_seen()
    await client.start()
    me = await client.get_me()
    log.info("Telethon connected as %s", getattr(me, "username", None) or me.id)
    sources = []
    for source_name in SOURCE_CHANNELS:
        try:
            entity = await client.get_entity(source_name)
            sources.append((source_name, entity))
            log.info("Watching @%s -> %s", source_name, TARGET_CHANNEL)
        except Exception:
            log.exception("Could not access source channel @%s", source_name)

    if not sources:
        raise RuntimeError("No source channels could be opened. Check SOURCE_CHANNELS and account membership.")

    source_entities = [entity for _, entity in sources]
    log.info("Active source channels (%s): %s", len(sources), ", ".join("@" + name for name, _ in sources))

    @client.on(events.Album(chats=source_entities))
    async def on_album(event):
        await send_translated_album(event.messages, str(event.chat_id))

    @client.on(events.NewMessage(chats=source_entities))
    async def on_new_message(event):
        # Telegram sends each album item as a NewMessage too; the Album handler
        # publishes the group together, so skip individual album items here.
        if getattr(event.message, "grouped_id", None):
            return
        await send_translated_message(event.message, str(event.chat_id))

    if CATCHUP_LIMIT:
        for source_name, source in sources:
            source_key = str(getattr(source, "id", source_name))
            for message in reversed(await client.get_messages(source, limit=CATCHUP_LIMIT)):
                post_key = f"{source_key}:{message.id}"
                if post_key not in seen_ids and str(message.id) not in seen_ids:
                    await send_translated_message(message, source_key)
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
