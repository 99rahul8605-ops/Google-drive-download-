import os
import re
import logging
import asyncio
import tempfile
import shutil
import urllib.request
import urllib.parse
from pathlib import Path

import gdown
import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.constants import ParseMode

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

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

MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB Telegram limit


def extract_file_id(url: str) -> tuple[str | None, str]:
    """Extract file/folder ID from Google Drive URL. Returns (id, type)."""
    for pattern in FOLDER_PATTERNS:
        match = re.search(pattern, url)
        if match:
            return match.group(1), "folder"

    for pattern in GDRIVE_PATTERNS:
        match = re.search(pattern, url)
        if match:
            return match.group(1), "file"

    return None, "unknown"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Google Drive Downloader Bot*\n\n"
        "Send me any Google Drive link and I'll download and send the file back to you!\n\n"
        "✅ *Supported links:*\n"
        "• `drive.google.com/file/d/...`\n"
        "• `drive.google.com/open?id=...`\n"
        "• `drive.google.com/drive/folders/...` _(small folders)_\n"
        "• Shared docs & spreadsheets\n\n"
        "⚠️ *Note:* Files must be publicly shared. Max ~50MB per file.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *How to use:*\n\n"
        "1. Share a Google Drive file with 'Anyone with the link'\n"
        "2. Copy and paste the link here\n"
        "3. I'll download and send it to you!\n\n"
        "🔒 *Privacy:* Files are downloaded to a temporary location and deleted immediately after sending.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    text = message.text.strip()

    # Check if it looks like a Google Drive URL
    if "drive.google.com" not in text and "docs.google.com" not in text:
        await message.reply_text(
            "❓ Please send a valid Google Drive link.\n"
            "Use /help for instructions."
        )
        return

    file_id, link_type = extract_file_id(text)

    if not file_id:
        await message.reply_text(
            "❌ Couldn't extract a file ID from that link.\n"
            "Make sure you're sharing the correct Google Drive URL."
        )
        return

    status_msg = await message.reply_text("⏳ Downloading from Google Drive...")

    tmp_dir = tempfile.mkdtemp()
    try:
        if link_type == "folder":
            await download_folder(update, context, file_id, tmp_dir, status_msg)
        else:
            await download_file(update, context, file_id, tmp_dir, status_msg)
    except Exception as e:
        logger.error(f"Error processing link: {e}")
        await status_msg.edit_text(
            f"❌ *Error:* {str(e)}\n\n"
            "Make sure the file is:\n"
            "• Publicly shared\n"
            "• Not too large (>50MB)\n"
            "• Not a restricted Google Workspace file",
            parse_mode=ParseMode.MARKDOWN,
        )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def get_real_filename(file_id: str) -> str | None:
    """
    Fetch the real filename from Google Drive's Content-Disposition header.
    Falls back to None if unavailable.
    """
    try:
        url = f"https://drive.google.com/uc?id={file_id}&export=download"
        resp = requests.head(url, allow_redirects=True, timeout=10)

        cd = resp.headers.get("Content-Disposition", "")
        # Try: filename="foo.pdf" or filename*=UTF-8''foo.pdf
        match = re.search(r'filename\*?=["\']?(?:UTF-8\'\')?([^"\';\n]+)', cd, re.IGNORECASE)
        if match:
            name = urllib.parse.unquote(match.group(1).strip().strip('"\''))
            if name:
                return name

        # Fallback: check Content-Type for known types
        ct = resp.headers.get("Content-Type", "")
        ext = content_type_to_ext(ct)
        if ext:
            return f"file{ext}"

    except Exception as e:
        logger.warning(f"Could not fetch filename from headers: {e}")

    return None


def content_type_to_ext(content_type: str) -> str:
    """Map common MIME types to file extensions."""
    ct = content_type.split(";")[0].strip().lower()
    mapping = {
        "application/pdf": ".pdf",
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "video/mp4": ".mp4",
        "video/x-matroska": ".mkv",
        "video/quicktime": ".mov",
        "video/x-msvideo": ".avi",
        "audio/mpeg": ".mp3",
        "audio/ogg": ".ogg",
        "audio/wav": ".wav",
        "audio/flac": ".flac",
        "application/zip": ".zip",
        "application/x-rar-compressed": ".rar",
        "application/x-7z-compressed": ".7z",
        "application/x-tar": ".tar",
        "application/gzip": ".gz",
        "application/vnd.ms-excel": ".xls",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
        "application/msword": ".doc",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
        "application/vnd.ms-powerpoint": ".ppt",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
        "text/plain": ".txt",
        "text/csv": ".csv",
        "application/json": ".json",
        "application/x-python-code": ".py",
        "text/html": ".html",
    }
    return mapping.get(ct, "")


async def download_file(update, context, file_id, tmp_dir, status_msg):
    url = f"https://drive.google.com/uc?id={file_id}&export=download"

    await status_msg.edit_text("⬇️ Downloading file...")

    # Step 1: Try to get the real filename before downloading
    loop = asyncio.get_event_loop()
    real_name = await loop.run_in_executor(None, lambda: get_real_filename(file_id))
    logger.info(f"Resolved filename: {real_name}")

    # Step 2: Download — gdown renames the output to match the real filename
    # We use output=tmp_dir so gdown saves with the original filename inside the dir
    downloaded = await loop.run_in_executor(
        None,
        lambda: gdown.download(url, output=tmp_dir + "/", quiet=False, fuzzy=True)
    )

    if not downloaded or not os.path.exists(downloaded):
        raise Exception("Download failed. The file may be private or the link is invalid.")

    # Step 3: Determine final filename
    downloaded_path = Path(downloaded)
    downloaded_name = downloaded_path.name

    # gdown usually preserves the filename. If it's generic (e.g. just the ID), use real_name
    is_generic = (
        downloaded_name == file_id
        or downloaded_name == "downloaded_file"
        or "." not in downloaded_name
    )

    if is_generic and real_name:
        final_name = real_name
        final_path = downloaded_path.parent / final_name
        downloaded_path.rename(final_path)
        downloaded_path = final_path
    elif is_generic and not real_name:
        # Last resort: sniff the bytes
        ext = sniff_extension(str(downloaded_path))
        final_name = f"file{ext}" if ext else downloaded_name
        if ext:
            final_path = downloaded_path.parent / final_name
            downloaded_path.rename(final_path)
            downloaded_path = final_path
    else:
        final_name = downloaded_name  # gdown got the right name already

    file_size = downloaded_path.stat().st_size

    if file_size > MAX_FILE_SIZE:
        size_mb = file_size / (1024 * 1024)
        raise Exception(f"File is too large ({size_mb:.1f}MB). Telegram limit is 50MB.")

    await status_msg.edit_text("📤 Uploading to Telegram...")

    with open(downloaded_path, "rb") as f:
        await update.message.reply_document(
            document=f,
            filename=final_name,
            caption=f"✅ Here's your file!\n📁 `{final_name}`\n📦 {file_size / 1024:.1f} KB",
            parse_mode=ParseMode.MARKDOWN,
        )

    await status_msg.delete()


def sniff_extension(filepath: str) -> str:
    """Read the first few bytes and guess the extension from magic bytes."""
    signatures = {
        b"%PDF": ".pdf",
        b"\x89PNG": ".png",
        b"\xff\xd8\xff": ".jpg",
        b"GIF8": ".gif",
        b"RIFF": ".webp",  # could also be .wav, but best guess
        b"PK\x03\x04": ".zip",
        b"Rar!": ".rar",
        b"\x1f\x8b": ".gz",
        b"ID3": ".mp3",
        b"\xff\xfb": ".mp3",
        b"fLaC": ".flac",
        b"\x00\x00\x00": ".mp4",  # rough
    }
    try:
        with open(filepath, "rb") as f:
            header = f.read(8)
        for magic, ext in signatures.items():
            if header.startswith(magic):
                return ext
    except Exception:
        pass
    return ""


async def download_folder(update, context, folder_id, tmp_dir, status_msg):
    url = f"https://drive.google.com/drive/folders/{folder_id}"

    await status_msg.edit_text("⬇️ Fetching folder contents...")

    folder_dir = os.path.join(tmp_dir, "folder")
    os.makedirs(folder_dir, exist_ok=True)

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None,
        lambda: gdown.download_folder(url, output=folder_dir, quiet=True, remaining_ok=True)
    )

    # Collect ALL files recursively (preserving nested structure)
    all_files = sorted(
        [f for f in Path(folder_dir).rglob("*") if f.is_file()],
        key=lambda f: f.name.lower()
    )

    if not all_files:
        raise Exception("No files found in the folder or the folder is private.")

    await status_msg.edit_text(f"📦 Found {len(all_files)} file(s). Sending one by one...")

    sent = 0
    skipped = 0

    for i, file_path in enumerate(all_files, 1):
        file_size = file_path.stat().st_size

        # Fix extension if missing
        final_name = file_path.name
        if "." not in final_name:
            ext = sniff_extension(str(file_path))
            if ext:
                final_name = final_name + ext
                new_path = file_path.parent / final_name
                file_path.rename(new_path)
                file_path = new_path

        # Skip files over Telegram limit individually
        if file_size > MAX_FILE_SIZE:
            size_mb = file_size / (1024 * 1024)
            await update.message.reply_text(
                f"⚠️ Skipped `{final_name}` — {size_mb:.1f}MB exceeds 50MB Telegram limit.",
                parse_mode=ParseMode.MARKDOWN,
            )
            skipped += 1
            continue

        # Update status every file
        await status_msg.edit_text(
            f"📤 Sending file {i}/{len(all_files)}: `{final_name}`",
            parse_mode=ParseMode.MARKDOWN,
        )

        with open(file_path, "rb") as f:
            await update.message.reply_document(
                document=f,
                filename=final_name,
                caption=f"📁 `{final_name}`\n📦 {file_size / 1024:.1f} KB  •  {i}/{len(all_files)}",
                parse_mode=ParseMode.MARKDOWN,
            )
        sent += 1

    summary = f"✅ Done! Sent {sent} file(s)."
    if skipped:
        summary += f"\n⚠️ Skipped {skipped} file(s) over 50MB."
    await status_msg.edit_text(summary)


def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN environment variable is not set!")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot is starting...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
