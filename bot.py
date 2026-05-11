import os
import re
import logging
import asyncio
import tempfile
import shutil
from pathlib import Path

import gdown
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


async def download_file(update, context, file_id, tmp_dir, status_msg):
    url = f"https://drive.google.com/uc?id={file_id}&export=download"
    output_path = os.path.join(tmp_dir, "downloaded_file")

    await status_msg.edit_text("⬇️ Downloading file...")

    loop = asyncio.get_event_loop()
    downloaded = await loop.run_in_executor(
        None,
        lambda: gdown.download(url, output_path, quiet=True, fuzzy=True)
    )

    if not downloaded or not os.path.exists(downloaded):
        raise Exception("Download failed. The file may be private or the link is invalid.")

    file_size = os.path.getsize(downloaded)
    file_name = Path(downloaded).name

    if file_size > MAX_FILE_SIZE:
        size_mb = file_size / (1024 * 1024)
        raise Exception(f"File is too large ({size_mb:.1f}MB). Telegram limit is 50MB.")

    await status_msg.edit_text("📤 Uploading to Telegram...")

    with open(downloaded, "rb") as f:
        await update.message.reply_document(
            document=f,
            filename=file_name,
            caption=f"✅ Here's your file!\n📁 `{file_name}`\n📦 {file_size / 1024:.1f} KB",
            parse_mode=ParseMode.MARKDOWN,
        )

    await status_msg.delete()


async def download_folder(update, context, folder_id, tmp_dir, status_msg):
    url = f"https://drive.google.com/drive/folders/{folder_id}"

    await status_msg.edit_text("⬇️ Downloading folder contents...")

    folder_dir = os.path.join(tmp_dir, "folder")
    os.makedirs(folder_dir, exist_ok=True)

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None,
        lambda: gdown.download_folder(url, output=folder_dir, quiet=True)
    )

    files = list(Path(folder_dir).rglob("*"))
    files = [f for f in files if f.is_file()]

    if not files:
        raise Exception("No files found in the folder or folder is private.")

    total_size = sum(f.stat().st_size for f in files)
    if total_size > MAX_FILE_SIZE:
        size_mb = total_size / (1024 * 1024)
        raise Exception(f"Folder total size ({size_mb:.1f}MB) exceeds 50MB limit.")

    await status_msg.edit_text(f"📤 Uploading {len(files)} file(s)...")

    for i, file_path in enumerate(files, 1):
        file_size = file_path.stat().st_size
        with open(file_path, "rb") as f:
            await update.message.reply_document(
                document=f,
                filename=file_path.name,
                caption=f"📁 `{file_path.name}` ({i}/{len(files)})\n📦 {file_size / 1024:.1f} KB",
                parse_mode=ParseMode.MARKDOWN,
            )

    await status_msg.edit_text(f"✅ Done! Sent {len(files)} file(s) from the folder.")


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
