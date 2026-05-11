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

TELEGRAM_LIMIT = 50 * 1024 * 1024


def extract_file_id(url):
    for pattern in FOLDER_PATTERNS:
        match = re.search(pattern, url)
        if match:
            return match.group(1), "folder"
    for pattern in GDRIVE_PATTERNS:
        match = re.search(pattern, url)
        if match:
            return match.group(1), "file"
    return None, "unknown"


def get_real_filename(file_id):
    try:
        url = f"https://drive.google.com/uc?id={file_id}&export=download"
        resp = requests.head(url, allow_redirects=True, timeout=10)
        cd = resp.headers.get("Content-Disposition", "")
        match = re.search(r'filename\*?=["\']?(?:UTF-8\'\')?([^"\';\n]+)', cd, re.IGNORECASE)
        if match:
            name = urllib.parse.unquote(match.group(1).strip().strip('"\''))
            if name:
                return name
        ct = resp.headers.get("Content-Type", "")
        ext = content_type_to_ext(ct)
        if ext:
            return f"file{ext}"
    except Exception as e:
        logger.warning(f"Could not fetch filename: {e}")
    return None


def content_type_to_ext(content_type):
    ct = content_type.split(";")[0].strip().lower()
    mapping = {
        "application/pdf": ".pdf",
        "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp",
        "video/mp4": ".mp4", "video/x-matroska": ".mkv", "video/quicktime": ".mov",
        "video/x-msvideo": ".avi", "video/webm": ".webm",
        "audio/mpeg": ".mp3", "audio/ogg": ".ogg", "audio/wav": ".wav", "audio/flac": ".flac",
        "application/zip": ".zip", "application/x-rar-compressed": ".rar",
        "application/x-7z-compressed": ".7z", "application/x-tar": ".tar",
        "application/gzip": ".gz",
        "application/vnd.ms-excel": ".xls",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
        "application/msword": ".doc",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
        "application/vnd.ms-powerpoint": ".ppt",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
        "text/plain": ".txt", "text/csv": ".csv",
        "application/json": ".json", "text/html": ".html",
    }
    return mapping.get(ct, "")


def sniff_extension(filepath):
    signatures = {
        b"%PDF": ".pdf", b"\x89PNG": ".png", b"\xff\xd8\xff": ".jpg",
        b"GIF8": ".gif", b"PK\x03\x04": ".zip", b"Rar!": ".rar",
        b"\x1f\x8b": ".gz", b"ID3": ".mp3", b"\xff\xfb": ".mp3", b"fLaC": ".flac",
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


def human_size(size_bytes):
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024*1024):.1f} MB"
    else:
        return f"{size_bytes / (1024*1024*1024):.2f} GB"


# ── Upload services for files >50MB ──────────────────────────────────────────

def upload_to_gofile(filepath, filename):
    """gofile.io — free, no login, no expiry, up to 10GB."""
    try:
        server_resp = requests.get("https://api.gofile.io/servers", timeout=10)
        server = server_resp.json()["data"]["servers"][0]["name"]
        with open(filepath, "rb") as f:
            resp = requests.post(
                f"https://{server}.gofile.io/contents/uploadfile",
                files={"file": (filename, f)},
                timeout=600,
            )
        data = resp.json()
        if data.get("status") == "ok":
            return ("gofile.io", data["data"]["downloadPage"])
    except Exception as e:
        logger.warning(f"gofile.io failed: {e}")
    return None


def upload_to_fileio(filepath, filename):
    """file.io — free, expires after 1 download, up to 2GB."""
    try:
        with open(filepath, "rb") as f:
            resp = requests.post(
                "https://file.io",
                files={"file": (filename, f)},
                data={"expires": "7d"},
                timeout=300,
            )
        data = resp.json()
        if data.get("success"):
            return ("file.io", data["link"])
    except Exception as e:
        logger.warning(f"file.io failed: {e}")
    return None


def upload_to_0x0(filepath, filename):
    """0x0.st — free, permanent, up to 512MB."""
    try:
        with open(filepath, "rb") as f:
            resp = requests.post(
                "https://0x0.st",
                files={"file": (filename, f)},
                timeout=300,
            )
        if resp.status_code == 200 and resp.text.strip().startswith("http"):
            return ("0x0.st", resp.text.strip())
    except Exception as e:
        logger.warning(f"0x0.st failed: {e}")
    return None


def upload_large_file(filepath, filename, size_bytes):
    """Try upload services in order until one succeeds."""
    # 1. gofile — best overall, handles up to 10GB
    result = upload_to_gofile(filepath, filename)
    if result:
        return result

    # 2. file.io — up to 2GB fallback
    if size_bytes <= 2 * 1024 * 1024 * 1024:
        result = upload_to_fileio(filepath, filename)
        if result:
            return result

    # 3. 0x0.st — up to 512MB last resort
    if size_bytes <= 512 * 1024 * 1024:
        result = upload_to_0x0(filepath, filename)
        if result:
            return result

    return None


# ── Bot handlers ──────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Google Drive Downloader Bot*\n\n"
        "Send me any Google Drive link!\n\n"
        "📦 *How files are delivered:*\n"
        "• ≤ 50MB → sent directly as a Telegram file\n"
        "• \> 50MB → uploaded to gofile.io, download link sent\n\n"
        "✅ Supports files, folders, docs, sheets, and more.\n"
        "⚠️ Files must be set to *'Anyone with the link'*.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *How to use:*\n\n"
        "1. Open Google Drive → right-click file → Share\n"
        "2. Set to *'Anyone with the link'*\n"
        "3. Copy and paste the link here\n\n"
        "Large files are auto-uploaded to gofile.io (free, no account needed).",
        parse_mode=ParseMode.MARKDOWN,
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if "drive.google.com" not in text and "docs.google.com" not in text:
        await update.message.reply_text("❓ Send a valid Google Drive link. Use /help for instructions.")
        return

    file_id, link_type = extract_file_id(text)
    if not file_id:
        await update.message.reply_text("❌ Couldn't extract a file ID from that link.")
        return

    status_msg = await update.message.reply_text("⏳ Starting...")
    tmp_dir = tempfile.mkdtemp()

    try:
        if link_type == "folder":
            await handle_folder(update, file_id, tmp_dir, status_msg)
        else:
            await handle_file(update, file_id, tmp_dir, status_msg)
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        await status_msg.edit_text(
            f"❌ *Error:* {str(e)}\n\nMake sure the file is publicly shared.",
            parse_mode=ParseMode.MARKDOWN,
        )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


async def handle_file(update, file_id, tmp_dir, status_msg):
    await status_msg.edit_text("⬇️ Downloading from Google Drive...")
    loop = asyncio.get_event_loop()

    real_name = await loop.run_in_executor(None, lambda: get_real_filename(file_id))
    url = f"https://drive.google.com/uc?id={file_id}&export=download"
    downloaded = await loop.run_in_executor(
        None, lambda: gdown.download(url, output=tmp_dir + "/", quiet=False, fuzzy=True)
    )

    if not downloaded or not os.path.exists(downloaded):
        raise Exception("Download failed. File may be private or invalid.")

    file_path = Path(downloaded)
    name = file_path.name
    is_generic = name == file_id or name == "downloaded_file" or "." not in name

    if is_generic and real_name:
        new_path = file_path.parent / real_name
        file_path.rename(new_path)
        file_path = new_path
    elif is_generic:
        ext = sniff_extension(str(file_path))
        if ext:
            new_path = file_path.parent / f"file{ext}"
            file_path.rename(new_path)
            file_path = new_path

    file_size = file_path.stat().st_size
    await deliver(update, status_msg, str(file_path), file_path.name, file_size, loop)


async def deliver(update, status_msg, filepath, filename, file_size, loop):
    """Send directly if ≤50MB, else upload to host and send link."""
    if file_size <= TELEGRAM_LIMIT:
        await status_msg.edit_text("📤 Sending file to you...")
        with open(filepath, "rb") as f:
            await update.message.reply_document(
                document=f,
                filename=filename,
                caption=f"✅ *{filename}*\n📦 {human_size(file_size)}",
                parse_mode=ParseMode.MARKDOWN,
            )
        await status_msg.delete()
    else:
        await status_msg.edit_text(
            f"📁 *{filename}* is *{human_size(file_size)}*\n"
            f"⬆️ Too large for Telegram — uploading to file host...",
            parse_mode=ParseMode.MARKDOWN,
        )
        result = await loop.run_in_executor(
            None, lambda: upload_large_file(filepath, filename, file_size)
        )
        if result:
            service, link = result
            note = "⚠️ _This link expires after 1 download._" if service == "file.io" else "🔗 _Link does not expire._"
            await status_msg.edit_text(
                f"✅ *{filename}*\n"
                f"📦 {human_size(file_size)}  •  🌐 {service}\n\n"
                f"👇 *Download link:*\n{link}\n\n{note}",
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True,
            )
        else:
            await status_msg.edit_text(
                f"❌ Failed to upload *{filename}* ({human_size(file_size)}) to any file host.\n"
                f"Please try again later.",
                parse_mode=ParseMode.MARKDOWN,
            )


async def handle_folder(update, folder_id, tmp_dir, status_msg):
    url = f"https://drive.google.com/drive/folders/{folder_id}"
    await status_msg.edit_text("⬇️ Fetching folder contents...")

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
        raise Exception("No files found in folder or folder is private.")

    await status_msg.edit_text(f"📦 Found {len(all_files)} file(s). Processing...")

    for i, file_path in enumerate(all_files, 1):
        file_size = file_path.stat().st_size
        final_name = file_path.name

        if "." not in final_name:
            ext = sniff_extension(str(file_path))
            if ext:
                final_name += ext
                new_path = file_path.parent / final_name
                file_path.rename(new_path)
                file_path = new_path

        await status_msg.edit_text(
            f"📤 {i}/{len(all_files)}: `{final_name}` ({human_size(file_size)})",
            parse_mode=ParseMode.MARKDOWN,
        )
        await deliver(update, None, str(file_path), final_name, file_size, loop)

    await status_msg.edit_text(f"✅ Done! Processed all {len(all_files)} file(s).")


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
