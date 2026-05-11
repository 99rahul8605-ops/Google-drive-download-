import os
import re
import logging
import asyncio
import tempfile
import shutil
import urllib.parse
from pathlib import Path

import gdown
import requests
from pyrogram import Client, filters
from pyrogram.types import Message

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN    = os.environ.get("BOT_TOKEN", "")
API_ID       = int(os.environ.get("TELEGRAM_API_ID", "0"))
API_HASH     = os.environ.get("TELEGRAM_API_HASH", "")

GDRIVE_PATTERNS = [
    r"https://drive\.google\.com/file/d/([a-zA-Z0-9_-]+)",
    r"https://drive\.google\.com/open\?id=([a-zA-Z0-9_-]+)",
    r"https://drive\.google\.com/uc\?id=([a-zA-Z0-9_-]+)",
    r"https://docs\.google\.com/.*?/d/([a-zA-Z0-9_-]+)",
    r"id=([a-zA-Z0-9_-]+)",
]
FOLDER_PATTERNS = [
    r"https://drive\.google\.com/drive/folders/([a-zA-Z0-9_-]+)",
]

MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024  # 2 GB — Pyrogram/MTProto limit


# ── Helpers ───────────────────────────────────────────────────────────────────

def extract_file_id(url):
    for p in FOLDER_PATTERNS:
        m = re.search(p, url)
        if m: return m.group(1), "folder"
    for p in GDRIVE_PATTERNS:
        m = re.search(p, url)
        if m: return m.group(1), "file"
    return None, "unknown"


def get_real_filename(file_id):
    try:
        url  = f"https://drive.google.com/uc?id={file_id}&export=download"
        resp = requests.head(url, allow_redirects=True, timeout=10)
        cd   = resp.headers.get("Content-Disposition", "")
        m    = re.search(r'filename\*?=["\']?(?:UTF-8\'\')?([^"\';\n]+)', cd, re.IGNORECASE)
        if m:
            name = urllib.parse.unquote(m.group(1).strip().strip('"\''))
            if name: return name
        ct  = resp.headers.get("Content-Type", "")
        ext = content_type_to_ext(ct)
        if ext: return f"file{ext}"
    except Exception as e:
        logger.warning(f"Filename fetch failed: {e}")
    return None


def content_type_to_ext(ct):
    ct = ct.split(";")[0].strip().lower()
    return {
        "application/pdf": ".pdf",
        "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp",
        "video/mp4": ".mp4", "video/x-matroska": ".mkv", "video/quicktime": ".mov",
        "video/x-msvideo": ".avi", "video/webm": ".webm",
        "audio/mpeg": ".mp3", "audio/ogg": ".ogg", "audio/wav": ".wav", "audio/flac": ".flac",
        "application/zip": ".zip", "application/x-rar-compressed": ".rar",
        "application/x-7z-compressed": ".7z", "application/x-tar": ".tar", "application/gzip": ".gz",
        "application/vnd.ms-excel": ".xls",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
        "application/msword": ".doc",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
        "application/vnd.ms-powerpoint": ".ppt",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
        "text/plain": ".txt", "text/csv": ".csv", "application/json": ".json", "text/html": ".html",
    }.get(ct, "")


def sniff_extension(filepath):
    sigs = {
        b"%PDF": ".pdf", b"\x89PNG": ".png", b"\xff\xd8\xff": ".jpg",
        b"GIF8": ".gif", b"PK\x03\x04": ".zip", b"Rar!": ".rar",
        b"\x1f\x8b": ".gz", b"ID3": ".mp3", b"fLaC": ".flac",
    }
    try:
        with open(filepath, "rb") as f:
            h = f.read(8)
        for magic, ext in sigs.items():
            if h.startswith(magic): return ext
    except Exception:
        pass
    return ""


def human_size(b):
    if b < 1024**2:    return f"{b/1024:.1f} KB"
    if b < 1024**3:    return f"{b/1024**2:.1f} MB"
    return f"{b/1024**3:.2f} GB"


def fix_filename(fp: Path) -> Path:
    """Rename file if it has no extension."""
    if "." not in fp.name:
        ext = sniff_extension(str(fp))
        if ext:
            new = fp.parent / (fp.name + ext)
            fp.rename(new)
            return new
    return fp


# ── Bot logic ─────────────────────────────────────────────────────────────────

app = Client("gdrive_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)


@app.on_message(filters.command("start"))
async def start(client, message: Message):
    await message.reply_text(
        "👋 **Google Drive Downloader Bot**\n\n"
        "Send me any Google Drive link and I'll send the file directly to you!\n\n"
        "✅ Supports up to **2GB** files\n"
        "✅ Files, folders, docs, sheets\n"
        "⚠️ File must be set to **'Anyone with the link'**"
    )


@app.on_message(filters.command("help"))
async def help_cmd(client, message: Message):
    await message.reply_text(
        "📖 **How to use:**\n\n"
        "1. Open Google Drive → right-click file → Share\n"
        "2. Set to **'Anyone with the link'**\n"
        "3. Copy & paste the link here\n"
        "4. Bot downloads and sends the file directly ✅"
    )


@app.on_message(filters.text & ~filters.command(["start", "help"]))
async def handle_message(client, message: Message):
    text = message.text.strip()

    if "drive.google.com" not in text and "docs.google.com" not in text:
        await message.reply_text("❓ Please send a Google Drive link. Use /help for instructions.")
        return

    file_id, link_type = extract_file_id(text)
    if not file_id:
        await message.reply_text("❌ Couldn't extract file ID from that link.")
        return

    status = await message.reply_text("⏳ Starting download...")
    tmp_dir = tempfile.mkdtemp()

    try:
        if link_type == "folder":
            await handle_folder(client, message, status, file_id, tmp_dir)
        else:
            await handle_file(client, message, status, file_id, tmp_dir)
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        await status.edit_text(f"❌ **Error:** {str(e)}\n\nMake sure the file is publicly shared.")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


async def handle_file(client, message, status, file_id, tmp_dir):
    await status.edit_text("⬇️ Downloading from Google Drive...")
    loop = asyncio.get_event_loop()

    real_name  = await loop.run_in_executor(None, lambda: get_real_filename(file_id))
    url        = f"https://drive.google.com/uc?id={file_id}&export=download"
    downloaded = await loop.run_in_executor(
        None, lambda: gdown.download(url, output=tmp_dir + "/", quiet=False, fuzzy=True)
    )

    if not downloaded or not os.path.exists(downloaded):
        raise Exception("Download failed. File may be private or link is invalid.")

    fp = Path(downloaded)
    is_generic = fp.name == file_id or fp.name == "downloaded_file" or "." not in fp.name

    if is_generic and real_name:
        new = fp.parent / real_name; fp.rename(new); fp = new
    else:
        fp = fix_filename(fp)

    file_size = fp.stat().st_size
    if file_size > MAX_FILE_SIZE:
        raise Exception(f"File is {human_size(file_size)} — exceeds 2GB limit.")

    await send_file(client, message, status, fp)


async def send_file(client, message, status, fp):
    file_size = fp.stat().st_size
    filename  = fp.name

    await status.edit_text(f"📤 Sending **{filename}** ({human_size(file_size)})...")

    await client.send_document(
        chat_id=message.chat.id,
        document=str(fp),
        file_name=filename,
        caption=f"✅ **{filename}**\n📦 {human_size(file_size)}",
        progress=upload_progress,
        progress_args=(status, filename),
    )

    await status.delete()


async def upload_progress(current, total, status, filename):
    if total == 0: return
    pct = current * 100 // total
    # Update every 20% to avoid flood limits
    if pct % 20 == 0:
        try:
            bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
            await status.edit_text(
                f"📤 Uploading **{filename}**\n{bar} {pct}%"
            )
        except Exception:
            pass


async def handle_folder(client, message, status, folder_id, tmp_dir):
    url = f"https://drive.google.com/drive/folders/{folder_id}"
    await status.edit_text("⬇️ Fetching folder contents...")

    folder_dir = os.path.join(tmp_dir, "folder")
    os.makedirs(folder_dir, exist_ok=True)
    loop = asyncio.get_event_loop()

    await loop.run_in_executor(
        None, lambda: gdown.download_folder(url, output=folder_dir, quiet=True, remaining_ok=True)
    )

    all_files = sorted(
        [f for f in Path(folder_dir).rglob("*") if f.is_file()],
        key=lambda f: f.name.lower()
    )

    if not all_files:
        raise Exception("No files found or folder is private.")

    await status.edit_text(f"📦 Found {len(all_files)} file(s). Sending one by one...")

    for i, fp in enumerate(all_files, 1):
        fp = fix_filename(fp)
        size = fp.stat().st_size
        await status.edit_text(f"📤 {i}/{len(all_files)}: **{fp.name}** ({human_size(size)})")
        await send_file(client, message, None, fp)

    await status.edit_text(f"✅ Done! Sent all {len(all_files)} file(s).")


def main():
    if not BOT_TOKEN:    raise ValueError("BOT_TOKEN not set!")
    if not API_ID:       raise ValueError("TELEGRAM_API_ID not set!")
    if not API_HASH:     raise ValueError("TELEGRAM_API_HASH not set!")
    logger.info("Bot starting with Pyrogram (MTProto, 2GB limit)...")
    app.run()


if __name__ == "__main__":
    main()
