import os
import re
import io
import logging
import asyncio
import tempfile
import shutil
import urllib.parse
import subprocess
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

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
API_ID    = int(os.environ.get("TELEGRAM_API_ID", "0"))
API_HASH  = os.environ.get("TELEGRAM_API_HASH", "")

MAX_FILE_SIZE  = 2 * 1024 * 1024 * 1024   # 2 GB — Pyrogram/MTProto limit
MAX_DL_SIZE   = 1800 * 1024 * 1024       # 1.8 GB — safe /tmp headroom

# Global lock — only 1 download at a time to protect /tmp space
_download_lock = asyncio.Lock()

# ── Pattern matchers ──────────────────────────────────────────────────────────

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

# Sites handled by yt-dlp (video/audio platforms)
YTDLP_DOMAINS = [
    "youtube.com", "youtu.be",
    "instagram.com",
    "twitter.com", "x.com",
    "tiktok.com",
    "facebook.com", "fb.watch",
    "reddit.com",
    "dailymotion.com",
    "vimeo.com",
    "twitch.tv",
    "soundcloud.com",
    "pinterest.com",
    "streamable.com",
    "bilibili.com",
    "rumble.com",
    "odysee.com",
    "kick.com",
]

MAGNET_PATTERN = re.compile(r"magnet:\?xt=urn:[a-zA-Z0-9]+:[a-fA-F0-9]{32,40}", re.IGNORECASE)


# ── Link type detection ───────────────────────────────────────────────────────

STREAM_EXTS = {".m3u8", ".m3u", ".mpd", ".f4m"}

def detect_link_type(text: str):
    """
    Returns (identifier, type) where type is one of:
      'gdrive_folder', 'gdrive_file', 'ytdlp', 'magnet', 'direct', 'unknown'
    """
    text = text.strip()

    # Magnet link
    if MAGNET_PATTERN.match(text):
        return text, "magnet"

    # Google Drive folder
    for p in FOLDER_PATTERNS:
        m = re.search(p, text)
        if m:
            return m.group(1), "gdrive_folder"

    # Google Drive file
    if "drive.google.com" in text or "docs.google.com" in text:
        for p in GDRIVE_PATTERNS:
            m = re.search(p, text)
            if m:
                return m.group(1), "gdrive_file"
        return None, "unknown"

    # yt-dlp supported sites
    try:
        parsed = urllib.parse.urlparse(text)
        domain = parsed.netloc.lower().lstrip("www.")
        if any(domain == d or domain.endswith("." + d) for d in YTDLP_DOMAINS):
            return text, "ytdlp"
    except Exception:
        pass

    # Generic direct HTTP/HTTPS link
    if re.match(r"https?://", text):
        # Check if it's a stream URL → route to yt-dlp
        parsed_path = urllib.parse.urlparse(text).path.lower()
        if any(parsed_path.endswith(ext) for ext in STREAM_EXTS):
            return text, "ytdlp"
        return text, "direct"

    return None, "unknown"


# ── Utility helpers ───────────────────────────────────────────────────────────

def get_real_filename(file_id):
    try:
        url  = f"https://drive.google.com/uc?id={file_id}&export=download"
        resp = requests.head(url, allow_redirects=True, timeout=10)
        cd   = resp.headers.get("Content-Disposition", "")
        m    = re.search(r'filename\*?=["\']?(?:UTF-8\'\')?([^"\';\n]+)', cd, re.IGNORECASE)
        if m:
            name = urllib.parse.unquote(m.group(1).strip().strip('"\''))
            if name:
                return name
        ct  = resp.headers.get("Content-Type", "")
        ext = content_type_to_ext(ct)
        if ext:
            return f"file{ext}"
    except Exception as e:
        logger.warning(f"Filename fetch failed: {e}")
    return None


def get_direct_filename(url: str) -> str | None:
    """Try to determine filename from URL or Content-Disposition header."""
    try:
        resp = requests.head(url, allow_redirects=True, timeout=10)
        cd   = resp.headers.get("Content-Disposition", "")
        m    = re.search(r'filename\*?=["\']?(?:UTF-8\'\')?([^"\';\n]+)', cd, re.IGNORECASE)
        if m:
            name = urllib.parse.unquote(m.group(1).strip().strip('"\''))
            if name:
                return name
        # Fallback: last segment of URL path
        path = urllib.parse.urlparse(url).path
        name = urllib.parse.unquote(path.rstrip("/").split("/")[-1])
        if name and "." in name:
            return name
        # Try Content-Type
        ct  = resp.headers.get("Content-Type", "")
        ext = content_type_to_ext(ct)
        return f"file{ext}" if ext else "downloaded_file"
    except Exception as e:
        logger.warning(f"Direct filename fetch failed: {e}")
    path = urllib.parse.urlparse(url).path
    name = urllib.parse.unquote(path.rstrip("/").split("/")[-1])
    return name if name else "downloaded_file"


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
            if h.startswith(magic):
                return ext
    except Exception:
        pass
    return ""


def human_size(b):
    if b < 1024 ** 2: return f"{b / 1024:.1f} KB"
    if b < 1024 ** 3: return f"{b / 1024 ** 2:.1f} MB"
    return f"{b / 1024 ** 3:.2f} GB"


def fix_filename(fp: Path) -> Path:
    if "." not in fp.name:
        ext = sniff_extension(str(fp))
        if ext:
            new = fp.parent / (fp.name + ext)
            fp.rename(new)
            return new
    return fp


def get_tmp_usage() -> str:
    """Return human-readable /tmp disk usage."""
    try:
        stat = shutil.disk_usage("/tmp")
        used = stat.total - stat.free
        return f"{human_size(used)} used / {human_size(stat.total)} total"
    except Exception:
        return "unknown"


def get_remote_file_size(url: str) -> int:
    """Return Content-Length in bytes, or 0 if unknown."""
    try:
        resp = requests.head(url, allow_redirects=True, timeout=10)
        return int(resp.headers.get("Content-Length", 0))
    except Exception:
        return 0


def check_aria2c():
    try:
        subprocess.run(["aria2c", "--version"], capture_output=True, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


def check_ytdlp():
    try:
        subprocess.run(["yt-dlp", "--version"], capture_output=True, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


# ── Bot setup ─────────────────────────────────────────────────────────────────

app = Client("gdrive_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)


@app.on_message(filters.command("start"))
async def start(client, message: Message):
    await message.reply_text(
        "👋 **Universal Downloader Bot**\n\n"
        "Send me any supported link and I'll download and send the file!\n\n"
        "✅ **Google Drive** (files & folders)\n"
        "✅ **Direct links** (HTTP/HTTPS — any file)\n"
        "✅ **Video sites** — YouTube, Instagram, Twitter/X, TikTok, Facebook, Reddit, Vimeo, Twitch, and 1000+ more\n"
        "✅ **Magnet links** (requires aria2c installed)\n\n"
        "⚠️ Max file size: **2 GB**\n"
        "Use /help for more details."
    )


@app.on_message(filters.command("help"))
async def help_cmd(client, message: Message):
    await message.reply_text(
        "📖 **Supported link types:**\n\n"
        "**1. Google Drive**\n"
        "   • Share file → Anyone with link\n"
        "   • Paste the link here\n\n"
        "**2. Direct file links**\n"
        "   • Any `http://` or `https://` link that points to a file\n"
        "   • Example: `https://example.com/file.zip`\n\n"
        "**3. Video/Media platforms**\n"
        "   • YouTube, Instagram, Twitter/X, TikTok, Facebook\n"
        "   • Reddit, Vimeo, Twitch, Dailymotion, SoundCloud and more\n"
        "   • Powered by **yt-dlp**\n\n"
        "**4. Magnet links**\n"
        "   • Paste a `magnet:?xt=...` link\n"
        "   • Requires **aria2c** installed on server\n\n"
        "⚠️ All links must be publicly accessible."
    )


@app.on_message(filters.text & ~filters.command(["start", "help"]))
async def handle_message(client, message: Message):
    text = message.text.strip()

    identifier, link_type = detect_link_type(text)

    if link_type == "unknown" or identifier is None:
        await message.reply_text(
            "❓ Unsupported link.\n\n"
            "Send a **Google Drive**, **direct file**, **video site**, or **magnet** link.\n"
            "Use /help to see all supported sources."
        )
        return

    if _download_lock.locked():
        await message.reply_text(
            "⏳ Ek download already chal raha hai. Please wait karein."
        )
        return

    status  = await message.reply_text("⏳ Processing link...")
    tmp_dir = tempfile.mkdtemp(dir="/tmp")

    try:
        async with _download_lock:
            if link_type == "gdrive_folder":
                await handle_gdrive_folder(client, message, status, identifier, tmp_dir)
            elif link_type == "gdrive_file":
                await handle_gdrive_file(client, message, status, identifier, tmp_dir)
            elif link_type == "ytdlp":
                await handle_ytdlp(client, message, status, identifier, tmp_dir)
            elif link_type == "direct":
                await handle_direct(client, message, status, identifier, tmp_dir)
            elif link_type == "magnet":
                await handle_magnet(client, message, status, identifier, tmp_dir)
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        await status.edit_text(f"❌ **Error:** {str(e)}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        logger.info(f"/tmp after cleanup: {get_tmp_usage()}")


# ── Handlers ──────────────────────────────────────────────────────────────────

async def handle_gdrive_file(client, message, status, file_id, tmp_dir):
    await status.edit_text("⬇️ Downloading from Google Drive...")
    loop = asyncio.get_event_loop()

    real_name  = await loop.run_in_executor(None, lambda: get_real_filename(file_id))
    url        = f"https://drive.google.com/uc?id={file_id}&export=download"
    downloaded = await loop.run_in_executor(
        None, lambda: gdown.download(url, output=tmp_dir + "/", quiet=False, fuzzy=True)
    )

    if not downloaded or not os.path.exists(downloaded):
        raise Exception("Download failed. File may be private or the link is invalid.")

    fp          = Path(downloaded)
    is_generic  = fp.name == file_id or fp.name == "downloaded_file" or "." not in fp.name

    if is_generic and real_name:
        new = fp.parent / real_name
        fp.rename(new)
        fp = new
    else:
        fp = fix_filename(fp)

    file_size = fp.stat().st_size
    if file_size > MAX_FILE_SIZE:
        raise Exception(f"File is {human_size(file_size)} — exceeds 2 GB limit.")

    await send_file(client, message, status, fp)


async def handle_gdrive_folder(client, message, status, folder_id, tmp_dir):
    url        = f"https://drive.google.com/drive/folders/{folder_id}"
    folder_dir = os.path.join(tmp_dir, "folder")
    os.makedirs(folder_dir, exist_ok=True)
    loop = asyncio.get_event_loop()

    await status.edit_text("⬇️ Fetching folder contents from Google Drive...")
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
        fp   = fix_filename(fp)
        size = fp.stat().st_size
        await status.edit_text(f"📤 {i}/{len(all_files)}: **{fp.name}** ({human_size(size)})")
        await send_file(client, message, None, fp)

    await status.edit_text(f"✅ Done! Sent all {len(all_files)} file(s).")


async def handle_direct(client, message, status, url, tmp_dir):
    """Stream file directly from URL to Telegram via MTProto — no disk needed."""
    loop = asyncio.get_event_loop()

    # ── Pre-check size ────────────────────────────────────────────────────────
    await status.edit_text("🔍 Checking file info...")
    remote_size = await loop.run_in_executor(None, lambda: get_remote_file_size(url))
    if remote_size > MAX_DL_SIZE:
        raise Exception(
            f"File too large: **{human_size(remote_size)}**\n"
            f"Max allowed: {human_size(MAX_DL_SIZE)}"
        )

    filename = await loop.run_in_executor(None, lambda: get_direct_filename(url))
    ext      = Path(filename).suffix.lower()

    await status.edit_text(f"📡 Streaming **{filename}** ({human_size(remote_size) if remote_size else '?'})...")

    # ── MTProto stream — no disk, chunks seedha upload ───────────────────────
    class StreamReader(io.RawIOBase):
        def __init__(self):
            self.resp = requests.get(url, stream=True, timeout=60, allow_redirects=True)
            self.resp.raise_for_status()
            self._iter = self.resp.iter_content(4 * 1024 * 1024)  # 4MB chunks

        def readinto(self, b):
            try:
                chunk = next(self._iter)
                n = len(chunk)
                b[:n] = chunk
                return n
            except StopIteration:
                return 0

        def readable(self):
            return True

        def close(self):
            try:
                self.resp.close()
            except Exception:
                pass
            super().close()

    reader  = io.BufferedReader(StreamReader(), buffer_size=8 * 1024 * 1024)
    caption = f"✅ **{filename}**" + (f"\n📦 {human_size(remote_size)}" if remote_size else "")

    try:
        if ext in VIDEO_EXTS:
            await client.send_video(
                chat_id=message.chat.id,
                video=reader,
                file_name=filename,
                caption=caption,
                supports_streaming=True,
                progress=upload_progress,
                progress_args=(status, filename),
            )
        elif ext in AUDIO_EXTS:
            await client.send_audio(
                chat_id=message.chat.id,
                audio=reader,
                file_name=filename,
                caption=caption,
                progress=upload_progress,
                progress_args=(status, filename),
            )
        else:
            await client.send_document(
                chat_id=message.chat.id,
                document=reader,
                file_name=filename,
                caption=caption,
                progress=upload_progress,
                progress_args=(status, filename),
            )
    except Exception as e:
        reader.close()
        # Fallback: agar stream seek issue aaye toh disk pe download karke bhejo
        logger.warning(f"Stream upload failed ({e}), falling back to disk...")
        await status.edit_text("⚠️ Stream failed, disk fallback use ho raha hai...")
        await _direct_disk_fallback(client, message, status, url, filename, tmp_dir)
        return

    reader.close()

    if status:
        try:
            await status.delete()
        except Exception:
            pass


async def _direct_disk_fallback(client, message, status, url, filename, tmp_dir):
    """Disk-based fallback agar MTProto stream seek kare."""
    dest_path = os.path.join(tmp_dir, filename)
    loop      = asyncio.get_event_loop()

    def _download():
        with requests.get(url, stream=True, timeout=60, allow_redirects=True) as r:
            r.raise_for_status()
            with open(dest_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=4 * 1024 * 1024):
                    if chunk:
                        f.write(chunk)

    await loop.run_in_executor(None, _download)
    fp = fix_filename(Path(dest_path))
    await send_file(client, message, status, fp)


async def handle_ytdlp(client, message, status, url, tmp_dir):
    """Download video/audio/streams from 1000+ sites using yt-dlp."""
    if not check_ytdlp():
        raise Exception(
            "yt-dlp is not installed on this server.\n"
            "Install: `pip install yt-dlp`"
        )

    await status.edit_text("🔍 Fetching media info...")
    loop = asyncio.get_event_loop()

    # Raw stream URLs (.m3u8 etc) won't have a title → use timestamp as name
    parsed_path = urllib.parse.urlparse(url).path.lower()
    is_stream   = any(parsed_path.endswith(ext) for ext in STREAM_EXTS)
    outtmpl     = os.path.join(
        tmp_dir,
        "stream_%(id)s.%(ext)s" if is_stream else "%(title).60s.%(ext)s"
    )

    cmd = [
        "yt-dlp",
        "--no-playlist",
        "-f", (
            f"bestvideo[ext=mp4][filesize<{MAX_DL_SIZE}]"
            f"+bestaudio[ext=m4a]/best[ext=mp4][filesize<{MAX_DL_SIZE}]/best"
        ),
        "--merge-output-format", "mp4",
        "--max-filesize", str(MAX_DL_SIZE),
        "--output", outtmpl,
        "--no-warnings",
        "--hls-prefer-ffmpeg",   # better HLS/m3u8 stream handling
        url,
    ]

    await status.edit_text(
        "⬇️ Downloading stream (HLS)..." if is_stream else "⬇️ Downloading via yt-dlp..."
    )

    def _run():
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            raise Exception(result.stderr.strip() or "yt-dlp failed.")
        return result

    await loop.run_in_executor(None, _run)

    files = [f for f in Path(tmp_dir).iterdir() if f.is_file()]
    if not files:
        raise Exception("yt-dlp ran but no output file was created.")

    for fp in sorted(files, key=lambda f: f.stat().st_size, reverse=True):
        size = fp.stat().st_size
        if size > MAX_FILE_SIZE:
            raise Exception(f"Downloaded file is {human_size(size)} — exceeds 2 GB limit.")
        await send_file(client, message, status, fp)


async def handle_magnet(client, message, status, magnet, tmp_dir):
    """Download torrent via magnet link using aria2c."""
    if not check_aria2c():
        raise Exception(
            "aria2c is not installed on this server.\n"
            "Install it: `apt install aria2` (Linux) or `brew install aria2` (Mac)"
        )

    await status.edit_text(
        "🧲 Starting magnet download via aria2c...\n"
        "⚠️ This may take time depending on seeders."
    )
    loop = asyncio.get_event_loop()

    cmd = [
        "aria2c",
        "--dir", tmp_dir,
        "--seed-time=0",            # don't seed after download
        "--max-connection-per-server=4",
        "--split=4",
        "--bt-stop-timeout=300",    # stop if no progress in 5 min
        "--timeout=60",
        "--follow-metalink=true",
        magnet,
    ]

    def _run():
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        if result.returncode != 0:
            raise Exception(result.stderr.strip() or "aria2c failed.")
        return result

    try:
        await loop.run_in_executor(None, _run)
    except subprocess.TimeoutExpired:
        raise Exception("Magnet download timed out (15 min). Try a magnet with more seeders.")

    files = [f for f in Path(tmp_dir).rglob("*") if f.is_file()]
    if not files:
        raise Exception("No files downloaded from magnet. Possibly no seeders or timeout.")

    await status.edit_text(f"📦 Torrent downloaded: {len(files)} file(s). Sending...")

    for i, fp in enumerate(sorted(files, key=lambda f: f.name.lower()), 1):
        size = fp.stat().st_size
        if size > MAX_FILE_SIZE:
            await status.edit_text(
                f"⚠️ Skipping **{fp.name}** ({human_size(size)}) — exceeds 2 GB limit."
            )
            continue
        await status.edit_text(f"📤 {i}/{len(files)}: **{fp.name}** ({human_size(size)})")
        await send_file(client, message, None, fp)

    await status.edit_text(f"✅ Done! Sent {len(files)} file(s) from magnet.")


# ── Shared send logic ─────────────────────────────────────────────────────────

VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".m4v", ".3gp"}
AUDIO_EXTS = {".mp3", ".m4a", ".ogg", ".flac", ".wav", ".aac", ".opus"}


async def send_file(client, message, status, fp: Path):
    file_size = fp.stat().st_size
    filename  = fp.name
    ext       = fp.suffix.lower()

    if status:
        await status.edit_text(f"📤 Sending **{filename}** ({human_size(file_size)})...")

    caption = f"✅ **{filename}**\n📦 {human_size(file_size)}"

    if ext in VIDEO_EXTS:
        # Send as proper video — prevents Telegram converting to GIF
        await client.send_video(
            chat_id=message.chat.id,
            video=str(fp),
            file_name=filename,
            caption=caption,
            supports_streaming=True,
            progress=upload_progress,
            progress_args=(status, filename),
        )
    elif ext in AUDIO_EXTS:
        await client.send_audio(
            chat_id=message.chat.id,
            audio=str(fp),
            file_name=filename,
            caption=caption,
            progress=upload_progress,
            progress_args=(status, filename),
        )
    else:
        await client.send_document(
            chat_id=message.chat.id,
            document=str(fp),
            file_name=filename,
            caption=caption,
            progress=upload_progress,
            progress_args=(status, filename),
        )

    # ── Delete immediately after upload to free /tmp ──────────────────────────
    try:
        fp.unlink()
        logger.info(f"Deleted after upload: {filename} | /tmp: {get_tmp_usage()}")
    except Exception as e:
        logger.warning(f"Could not delete {filename}: {e}")

    if status:
        try:
            await status.delete()
        except Exception:
            pass


async def upload_progress(current, total, status, filename):
    if total == 0 or status is None:
        return
    pct = current * 100 // total
    if pct % 20 == 0:
        try:
            bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
            await status.edit_text(
                f"📤 Uploading **{filename}**\n{bar} {pct}%"
            )
        except Exception:
            pass


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    if not BOT_TOKEN: raise ValueError("BOT_TOKEN not set!")
    if not API_ID:    raise ValueError("TELEGRAM_API_ID not set!")
    if not API_HASH:  raise ValueError("TELEGRAM_API_HASH not set!")

    logger.info("Checking optional dependencies...")
    if check_ytdlp():
        logger.info("✅ yt-dlp found — video site downloads enabled")
    else:
        logger.warning("⚠️  yt-dlp not found — install with: pip install yt-dlp")

    if check_aria2c():
        logger.info("✅ aria2c found — magnet/torrent downloads enabled")
    else:
        logger.warning("⚠️  aria2c not found — install with: apt install aria2")

    logger.info("Bot starting...")
    app.run()


if __name__ == "__main__":
    main()
