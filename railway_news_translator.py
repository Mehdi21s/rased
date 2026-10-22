import asyncio, json, logging, os, re, tempfile
from pathlib import Path
from typing import Optional

from aiogram import Bot
from aiogram.types import (
    FSInputFile, InputMediaPhoto, InputMediaVideo, InputMediaDocument, MessageEntity
)
from telethon import TelegramClient, events
from telethon.sessions import StringSession

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("persian-relay")

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SESSION_STRING = os.environ["SESSION_STRING"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
SOURCE_CHANNELS = list(dict.fromkeys(x.strip().lstrip("@").strip() for x in os.getenv("SOURCE_CHANNELS", "kataebAlqasam,Ali_bk_8").replace(";", ",").split(",") if x.strip()))
REQUIRED_SOURCES = {"kataebAlqasam", "Ali_bk_8"}
STARTUP_MARKER = "RASED_PERSIAN_ONLY_RELAY_V1"
TARGET_CHANNEL = os.getenv("TARGET_CHANNEL", "@rasedpersian")
CATCHUP_LIMIT = max(0, int(os.getenv("CATCHUP_LIMIT", "0")))
TRANSLATE_REQUIRED = False  # Kept for backwards compatibility; translation is disabled.
DATA_DIR = Path(os.getenv("DATA_DIR", "/tmp/persian-relay"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
SEEN_FILE = DATA_DIR / "seen_posts.json"

proxy = None
if os.getenv("PROXY_HOST") and os.getenv("PROXY_PORT"):
    proxy = ("socks5", os.getenv("PROXY_HOST"), int(os.getenv("PROXY_PORT")))

bot = Bot(token=BOT_TOKEN)
client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH, proxy=proxy)
# Caption footer. Set POST_TAG in Railway Variables to customize it.
POST_TAG = os.getenv("POST_TAG", "✨ @rasedpersian ✨").strip()
# Optional Telegram custom-emoji document ID. Leave empty until the ID is known.
CUSTOM_EMOJI_ID = os.getenv("CUSTOM_EMOJI_ID", "").strip()
CUSTOM_EMOJI_CHAR = os.getenv("CUSTOM_EMOJI_CHAR", "✨")
seen_ids = set()


def add_post_tag(text: str) -> str:
    text = (text or "").strip()
    tag = POST_TAG
    if not tag:
        return text
    if text:
        return f"{text}\n\n{tag}"
    return tag


def caption_entities(caption: str):
    """Build Telegram custom-emoji entities if an ID was configured."""
    if not CUSTOM_EMOJI_ID or not CUSTOM_EMOJI_CHAR:
        return None
    entities = []
    search_from = 0
    utf16_length = len(CUSTOM_EMOJI_CHAR.encode("utf-16-le")) // 2
    while True:
        offset = caption.find(CUSTOM_EMOJI_CHAR, search_from)
        if offset < 0:
            break
        utf16_offset = len(caption[:offset].encode("utf-16-le")) // 2
        entities.append(MessageEntity(
            type="custom_emoji",
            offset=utf16_offset,
            length=utf16_length,
            custom_emoji_id=CUSTOM_EMOJI_ID,
        ))
        search_from = offset + len(CUSTOM_EMOJI_CHAR)
    return entities or None

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
    """Clean source/footer noise without stripping Persian text or emojis."""
    if not text:
        return ""
    lines = []
    for line in text.splitlines():
        if re.search(r"(?:منبع|source)\s*[:：]", line, re.I):
            continue
        if re.search(r"https?://t\.me/(?:HoopoeAr|kataebAlqasam|Ali_bk_8|rasedpersian)\b", line, re.I):
            continue
        lines.append(line.rstrip())
    return "\n".join(lines).strip()


def extract_persian_text(text: Optional[str]) -> str:
    """Keep the Persian section and discard the English translation below it.

    Stops at a clearly English-only line/paragraph or common translation heading.
    Mixed-script lines containing Persian are retained to avoid losing names/URLs.
    """
    text = clean_text(text)
    if not text:
        return ""
    output = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            output.append("")
            continue
        if re.match(r"^(?:english(?: translation)?|translation|translated into english)\s*[:：-]?\s*$", stripped, re.I):
            break
        persian_count = len(re.findall(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]", stripped))
        latin_count = len(re.findall(r"[A-Za-z]", stripped))
        # A Latin-only line after the Persian section is likely the English version.
        if latin_count >= 8 and persian_count == 0:
            break
        # Explicit separator followed by an English translation.
        if re.match(r"^(?:[-_=]{3,}|\*{3,})\s*$", stripped) and output:
            break
        output.append(line)
    return "\n".join(output).strip()

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


async def send_translated_message(message, source_key: str):
    post_key = f"{source_key}:{message.id}"
    if post_key in seen_ids or str(message.id) in seen_ids:
        return
    original = clean_text(message.message or "")
    translated = ""
    if original:
        try:
            translated = extract_persian_text(original)
        except Exception:
            log.exception("Persian text extraction failed for message %s", message.id)
            if TRANSLATE_REQUIRED:
                return
            translated = original

    tagged_translation = add_post_tag(translated) if translated else ""
    caption = tagged_translation[:1024] if tagged_translation else None
    remainder = tagged_translation[1024:].strip() if len(tagged_translation) > 1024 else ""
    entities = caption_entities(caption) if caption else None
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
                    await bot.send_photo(TARGET_CHANNEL, photo=upload_file, caption=caption, caption_entities=entities)
                elif mime.startswith("video/"):
                    await bot.send_video(TARGET_CHANNEL, video=upload_file, caption=caption, caption_entities=entities, supports_streaming=True)
                elif mime.startswith("audio/"):
                    await bot.send_audio(TARGET_CHANNEL, audio=upload_file, caption=caption, caption_entities=entities)
                else:
                    await bot.send_document(TARGET_CHANNEL, document=upload_file, caption=caption, caption_entities=entities)
            elif tagged_translation:
                for chunk in split_text(tagged_translation):
                    await bot.send_message(TARGET_CHANNEL, chunk)
        elif tagged_translation:
            for chunk in split_text(tagged_translation):
                await bot.send_message(TARGET_CHANNEL, chunk)

        if remainder:
            for chunk in split_text(remainder):
                await bot.send_message(TARGET_CHANNEL, chunk)
        seen_ids.add(post_key)
        save_seen()
        log.info("Relayed Persian-only message %s", message.id)
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
            translated = extract_persian_text(original)
        except Exception:
            log.exception("Persian text extraction failed for %s", album_key)
            if TRANSLATE_REQUIRED:
                return

    tagged_translation = add_post_tag(translated) if translated else ""
    media_items = []
    paths = []
    caption_used = False
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
            # Put the caption on the first media that was actually downloaded.
            caption = tagged_translation[:1024] if not caption_used and tagged_translation else None
            entities = caption_entities(caption) if caption else None
            if caption:
                caption_used = True
            if mime.startswith("video/"):
                media_items.append(InputMediaVideo(media=upload, caption=caption, caption_entities=entities, supports_streaming=True))
            elif mime.startswith("image/"):
                media_items.append(InputMediaPhoto(media=upload, caption=caption, caption_entities=entities))
            else:
                media_items.append(InputMediaDocument(media=upload, caption=caption, caption_entities=entities))

        if len(media_items) >= 2:
            await bot.send_media_group(TARGET_CHANNEL, media=media_items)
        elif len(media_items) == 1:
            only = media_items[0]
            if isinstance(only, InputMediaVideo):
                await bot.send_video(TARGET_CHANNEL, video=only.media, caption=only.caption, caption_entities=caption_entities(only.caption) if only.caption else None, supports_streaming=True)
            elif isinstance(only, InputMediaPhoto):
                await bot.send_photo(TARGET_CHANNEL, photo=only.media, caption=only.caption, caption_entities=caption_entities(only.caption) if only.caption else None)
            else:
                await bot.send_document(TARGET_CHANNEL, document=only.media, caption=only.caption, caption_entities=caption_entities(only.caption) if only.caption else None)
        elif tagged_translation:
            for chunk in split_text(tagged_translation):
                await bot.send_message(TARGET_CHANNEL, chunk)

        remainder = tagged_translation[1024:].strip() if len(tagged_translation) > 1024 else ""
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
    log.info("STARTUP_MARKER=%s", STARTUP_MARKER)
    log.info("Configured source channels (%s): %s", len(SOURCE_CHANNELS), ", ".join("@" + n for n in SOURCE_CHANNELS))
    missing_config = REQUIRED_SOURCES - set(SOURCE_CHANNELS)
    if missing_config:
        raise RuntimeError("SOURCE_CHANNELS is missing required sources: " + ", ".join("@" + n for n in sorted(missing_config)))

    await client.start()
    me = await client.get_me()
    log.info("Telethon connected as %s", getattr(me, "username", None) or me.id)

    sources = []
    failed = []
    for source_name in SOURCE_CHANNELS:
        try:
            entity = await client.get_entity(source_name)
            sources.append((source_name, entity))
            log.info("Resolved source @%s (entity_id=%s) -> %s", source_name, getattr(entity, "id", "unknown"), TARGET_CHANNEL)
        except Exception as exc:
            failed.append(source_name)
            log.exception("Could not access source channel @%s: %s", source_name, exc)

    # Fail loudly rather than silently running with only one source.
    if failed:
        raise RuntimeError("Could not resolve required source channel(s): " + ", ".join("@" + n for n in failed))
    if len(sources) < 2:
        raise RuntimeError(f"Expected both source channels, but resolved only {len(sources)}")

    for source_name, source_entity in sources:
        source_key = str(getattr(source_entity, "id", source_name))

        async def on_album(event, _source_key=source_key, _source_name=source_name):
            log.info("Album received from @%s (%s messages)", _source_name, len(event.messages))
            await send_translated_album(event.messages, _source_key)

        async def on_new_message(event, _source_key=source_key, _source_name=source_name):
            message = event.message
            if getattr(message, "grouped_id", None):
                return
            log.info("Message received from @%s (message_id=%s)", _source_name, message.id)
            await send_translated_message(message, _source_key)

        client.add_event_handler(on_album, events.Album(chats=source_entity))
        client.add_event_handler(on_new_message, events.NewMessage(chats=source_entity))
        log.info("Watching @%s (entity_id=%s) -> %s", source_name, getattr(source_entity, "id", "unknown"), TARGET_CHANNEL)

    log.info("Active source channels (%s): %s", len(sources), ", ".join("@" + name for name, _ in sources))

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
