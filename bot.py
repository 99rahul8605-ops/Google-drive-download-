import os
import re
import logging
import asyncio
import tempfile
import shutil
import urllib.parse
from pathlib import Path

import requests
from pyrogram import Client, filters
from pyrogram.types import Message

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
API_ID    = int(os.environ.get("TELEGRAM_API_ID", "0"))
API_HASH  = os.environ.get("TELEGRAM_API_HASH", "")

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

MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024  # 2 GB

GDRIVE_DOWNLOAD_URL = "https://drive.google.com/uc"
GDRIVE_API_URL      = "https://www.googleapis.com/drive/v3/files"


# ── Helpers ───────────────────────────────────────────────────────────────────

def extract_file_id(url):
    for p in FOLDER_PATTERNS:
        m = re.search(p, url)
        if m: return m.group(1), "folder"
    for p in GDRIVE_PATTERNS:
        m = re.search(p, url)
        if m: return m.group(1), "file"
    return None, "unknown"


def human_size(b):
    if b < 1024**2:  return f"{b/1024:.1f} KB"
    if b < 1024**3:  return f"{b/1024**2:.1f} MB"
    return f"{b/1024**3:.2f} GB"


def content_type_to_ext(ct):
    ct = ct.split(";")[0].strip().lower()
    return {
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


def fix_filename(fp: Path) -> Path:
    if "." not in fp.name:
        ext = sniff_extension(str(fp))
        if ext:
            new = fp.parent / (fp.name + ext)
            fp.rename(new)
            return new
    return fp


# ── Google Drive Downloader (no gdown) ───────────────────────────────────────

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0.0.0 Safari/537.36"
})


def _get_confirm_token(response):
    # New-style: look for download_warning cookie
    for k, v in response.cookies.items():
        if k.startswith("download_warning"):
            return v
    # Also check for form-based confirm token in HTML
    m = re.search(r'confirm=([0-9A-Za-z_\-]+)', response.text)
    if m:
        return m.group(1)
    # New 2024 style: &uuid= token
    m = re.search(r'"downloadUrl":"([^"]+)"', response.text)
    if m:
        return ("__direct__", urllib.parse.unquote(m.group(1).replace("\\u003d", "=").replace("\\u0026", "&")))
    return None


def download_gdrive_file(file_id: str, dest_dir: str) -> str:
    """
    Download a Google Drive file robustly without gdown.
    Returns the path to the downloaded file.
    """
    url = f"{GDRIVE_DOWNLOAD_URL}?id={file_id}&export=download&confirm=t&uuid=1"

    # First request
    resp = SESSION.get(url, stream=True, timeout=30)

    filename = None
    # Try to get filename from Content-Disposition
    cd = resp.headers.get("Content-Disposition", "")
    m  = re.search(r'filename\*?=["\']?(?:UTF-8\'\')?([^"\';\n]+)', cd, re.IGNORECASE)
    if m:
        filename = urllib.parse.unquote(m.group(1).strip().strip('"\''))

    # If we got HTML instead of a file, handle virus-scan warning page
    ct = resp.headers.get("Content-Type", "")
    if "text/html" in ct:
        token = _get_confirm_token(resp)

        if token is None:
            raise Exception(
                "Google Drive returned a restriction page. "
                "Make sure the file is shared as 'Anyone with the link'."
            )

        if isinstance(token, tuple) and token[0] == "__direct__":
            # Got a direct download URL from the page
            resp = SESSION.get(token[1], stream=True, timeout=30)
        else:
            # Use confirm token
            confirm_url = (
                f"{GDRIVE_DOWNLOAD_URL}?id={file_id}&export=download"
                f"&confirm={token}&uuid=1"
            )
            resp = SESSION.get(confirm_url, stream=True, timeout=30)

        # Re-read filename from new response
        cd2 = resp.headers.get("Content-Disposition", "")
        m2  = re.search(r'filename\*?=["\']?(?:UTF-8\'\')?([^"\';\n]+)', cd2, re.IGNORECASE)
        if m2:
            filename = urllib.parse.unquote(m2.group(1).strip().strip('"\''))

    resp.raise_for_status()

    # Determine filename
    if not filename:
        ct2 = resp.headers.get("Content-Type", "")
        ext = content_type_to_ext(ct2)
        filename = f"{file_id}{ext}" if ext else file_id

    # Sanitize filename
    filename = re.sub(r'[\\/*?:"<>|]', "_", filename).strip()
    dest = os.path.join(dest_dir, filename)

    # Stream write
    with open(dest, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1024 * 1024):
            if chunk:
                f.write(chunk)

    return dest


def list_gdrive_folder(folder_id: str) -> list[dict]:
    """
    List files in a public Google Drive folder using the web scrape method.
    Returns list of {id, name} dicts.
    """
    url  = f"https://drive.google.com/drive/folders/{folder_id}"
    resp = SESSION.get(url, timeout=15)

    # Extract file IDs and names from the page JSON blob
    # Google embeds file data as: ["filename","","id",...]
    files = []
    # Pattern for file entries in the embedded JSON
    pattern = re.findall(
        r'\["([^"]+)","[^"]*","([a-zA-Z0-9_-]{25,})"',
        resp.text
    )
    seen = set()
    for name, fid in pattern:
        if fid not in seen and len(fid) > 20:
            seen.add(fid)
            files.append({"id": fid, "name": name})

    return files


# ── Bot ───────────────────────────────────────────────────────────────────────

app = Client("gdrive_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)


@app.on_message(filters.command("start"))
async def start(client, message: Message):
    await message.reply_text(
        "👋 **Google Drive Downloader Bot**\n\n"
        "Send me any Google Drive link and I'll send the file directly!\n\n"
        "✅ Supports up to **2GB** files\n"
        "✅ Files & folders supported\n"
        "⚠️ File must be **'Anyone with the link'**"
    )


@app.on_message(filters.command("help"))
async def help_cmd(client, message: Message):
    await message.reply_text(
        "📖 **How to use:**\n\n"
        "1. Open Google Drive → right-click file → Share\n"
        "2. Set to **'Anyone with the link'**\n"
        "3. Paste the link here ✅"
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

    status  = await message.reply_text("⏳ Starting...")
    tmp_dir = tempfile.mkdtemp()

    try:
        if link_type == "folder":
            await handle_folder(client, message, status, file_id, tmp_dir)
        else:
            await handle_file(client, message, status, file_id, tmp_dir)
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        await status.edit_text(
            f"❌ **Error:** {str(e)}\n\n"
            "Make sure the file is publicly shared as 'Anyone with the link'."
        )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


async def handle_file(client, message, status, file_id, tmp_dir):
    await status.edit_text("⬇️ Downloading from Google Drive...")
    loop = asyncio.get_event_loop()

    dest = await loop.run_in_executor(
        None, lambda: download_gdrive_file(file_id, tmp_dir)
    )

    fp        = fix_filename(Path(dest))
    file_size = fp.stat().st_size

    if file_size > MAX_FILE_SIZE:
        raise Exception(f"File is {human_size(file_size)} — exceeds 2GB limit.")

    await send_file(client, message, status, fp)


async def send_file(client, message, status, fp):
    file_size = fp.stat().st_size
    filename  = fp.name

    if status:
        await status.edit_text(f"📤 Uploading **{filename}** ({human_size(file_size)})...")

    await client.send_document(
        chat_id=message.chat.id,
        document=str(fp),
        file_name=filename,
        caption=f"✅ **{filename}**\n📦 {human_size(file_size)}",
        progress=upload_progress,
        progress_args=(status, filename) if status else (None, filename),
    )

    if status:
        await status.delete()


async def upload_progress(current, total, status, filename):
    if not status or total == 0:
        return
    pct = current * 100 // total
    if pct % 25 == 0:
        try:
            bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
            await status.edit_text(f"📤 **{filename}**\n{bar} {pct}%")
        except Exception:
            pass


async def handle_folder(client, message, status, folder_id, tmp_dir):
    await status.edit_text("🔍 Reading folder...")
    loop = asyncio.get_event_loop()

    files = await loop.run_in_executor(None, lambda: list_gdrive_folder(folder_id))

    if not files:
        raise Exception(
            "No files found in folder.\n"
            "Make sure it's shared as 'Anyone with the link'."
        )

    await status.edit_text(f"📦 Found **{len(files)}** file(s). Downloading & sending...")

    for i, f in enumerate(files, 1):
        await status.edit_text(
            f"⬇️ {i}/{len(files)}: **{f['name']}**"
        )
        try:
            dest = await loop.run_in_executor(
                None, lambda fid=f["id"]: download_gdrive_file(fid, tmp_dir)
            )
            fp = fix_filename(Path(dest))
            await send_file(client, message, None, fp)
            os.remove(fp)  # free space immediately
        except Exception as e:
            await message.reply_text(f"⚠️ Skipped **{f['name']}**: {e}")

    await status.edit_text(f"✅ Done! Sent all {len(files)} file(s).")


def main():
    if not BOT_TOKEN: raise ValueError("BOT_TOKEN not set!")
    if not API_ID:    raise ValueError("TELEGRAM_API_ID not set!")
    if not API_HASH:  raise ValueError("TELEGRAM_API_HASH not set!")
    logger.info("Bot starting (Pyrogram MTProto, 2GB, no gdown)...")
    app.run()


if __name__ == "__main__":
    main()
