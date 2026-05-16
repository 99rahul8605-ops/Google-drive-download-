import os
import re
import sys
import json
import logging
import asyncio
import tempfile
import shutil
import urllib.parse
import subprocess
import time
from pathlib import Path

import gdown
import requests

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ── Env ───────────────────────────────────────────────────────────────────────

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
API_ID    = int(os.environ.get("TELEGRAM_API_ID", "0"))
API_HASH  = os.environ.get("TELEGRAM_API_HASH", "")

# ── Settings ──────────────────────────────────────────────────────────────────

SETTINGS_FILE    = "settings.json"
DEFAULT_SETTINGS = {
    "library":   "pyrogram",
    "workers":   4,
    "max_dl_gb": 1.95,          # ~2GB safe limit
}

def load_settings() -> dict:
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE) as f:
                s = json.load(f)
            for k, v in DEFAULT_SETTINGS.items():
                s.setdefault(k, v)
            return s
        except Exception:
            pass
    return DEFAULT_SETTINGS.copy()

def save_settings(s: dict):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(s, f, indent=2)

SETTINGS      = load_settings()
MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024          # 2 GB — Telegram free limit
MAX_DL_SIZE   = 10 * 1024 * 1024 * 1024         # 10 GB — will be split if > 2GB

# ── Patterns ──────────────────────────────────────────────────────────────────

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
YTDLP_DOMAINS = [
    "youtube.com", "youtu.be", "instagram.com", "twitter.com", "x.com",
    "tiktok.com", "facebook.com", "fb.watch", "reddit.com", "dailymotion.com",
    "vimeo.com", "twitch.tv", "soundcloud.com", "pinterest.com", "pin.it",
    "pinterest.co.uk", "pinterest.in", "streamable.com",
    "bilibili.com", "rumble.com", "odysee.com", "kick.com",
]
MAGNET_PATTERN = re.compile(r"magnet:\?xt=urn:[a-zA-Z0-9]+:[a-fA-F0-9]{32,40}", re.IGNORECASE)
STREAM_EXTS    = {".m3u8", ".m3u", ".mpd", ".f4m"}
VIDEO_EXTS     = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".m4v", ".3gp"}
AUDIO_EXTS     = {".mp3", ".m4a", ".ogg", ".flac", ".wav", ".aac", ".opus"}

HTTP             = requests.Session()
HTTP.headers.update({"User-Agent": "Mozilla/5.0"})
_download_lock   = asyncio.Lock()


# ── Link detection ────────────────────────────────────────────────────────────

def detect_link_type(text: str):
    text = text.strip()
    if MAGNET_PATTERN.match(text):
        return text, "magnet"
    for p in FOLDER_PATTERNS:
        m = re.search(p, text)
        if m: return m.group(1), "gdrive_folder"
    if "drive.google.com" in text or "docs.google.com" in text:
        for p in GDRIVE_PATTERNS:
            m = re.search(p, text)
            if m: return m.group(1), "gdrive_file"
        return None, "unknown"
    try:
        domain = urllib.parse.urlparse(text).netloc.lower().lstrip("www.")
        if any(domain == d or domain.endswith("." + d) for d in YTDLP_DOMAINS):
            return text, "ytdlp"
    except Exception:
        pass
    if re.match(r"https?://", text):
        path = urllib.parse.urlparse(text).path.lower()
        if any(path.endswith(ext) for ext in STREAM_EXTS):
            return text, "ytdlp"
        return text, "direct"
    return None, "unknown"


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_real_filename(file_id):
    try:
        resp = HTTP.head(f"https://drive.google.com/uc?id={file_id}&export=download",
                         allow_redirects=True, timeout=10)
        cd = resp.headers.get("Content-Disposition", "")
        m  = re.search(r'filename\*?=["\']?(?:UTF-8\'\')?([^"\';\n]+)', cd, re.IGNORECASE)
        if m:
            name = urllib.parse.unquote(m.group(1).strip().strip('"\''))
            if name: return name
        ext = content_type_to_ext(resp.headers.get("Content-Type", ""))
        if ext: return f"file{ext}"
    except Exception as e:
        logger.warning(f"GDrive filename failed: {e}")
    return None

def get_direct_filename(url: str) -> str:
    try:
        resp = HTTP.head(url, allow_redirects=True, timeout=10)
        cd   = resp.headers.get("Content-Disposition", "")
        m    = re.search(r'filename\*?=["\']?(?:UTF-8\'\')?([^"\';\n]+)', cd, re.IGNORECASE)
        if m:
            name = urllib.parse.unquote(m.group(1).strip().strip('"\''))
            if name: return name
        path = urllib.parse.urlparse(url).path
        name = urllib.parse.unquote(path.rstrip("/").split("/")[-1])
        if name and "." in name: return name
        ext = content_type_to_ext(resp.headers.get("Content-Type", ""))
        return f"file{ext}" if ext else "downloaded_file"
    except Exception:
        pass
    name = urllib.parse.unquote(urllib.parse.urlparse(url).path.rstrip("/").split("/")[-1])
    return name if name else "downloaded_file"

def get_remote_file_size(url: str) -> int:
    try:
        return int(HTTP.head(url, allow_redirects=True, timeout=10).headers.get("Content-Length", 0))
    except Exception:
        return 0

def content_type_to_ext(ct):
    ct = ct.split(";")[0].strip().lower()
    return {
        "application/pdf": ".pdf",
        "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp",
        "video/mp4": ".mp4", "video/x-matroska": ".mkv", "video/quicktime": ".mov",
        "video/x-msvideo": ".avi", "video/webm": ".webm",
        "audio/mpeg": ".mp3", "audio/ogg": ".ogg", "audio/wav": ".wav", "audio/flac": ".flac",
        "application/zip": ".zip", "application/x-rar-compressed": ".rar",
        "application/x-7z-compressed": ".7z",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
        "text/plain": ".txt", "text/csv": ".csv", "application/json": ".json",
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
    if b < 1024**2: return f"{b/1024:.1f} KB"
    if b < 1024**3: return f"{b/1024**2:.1f} MB"
    return f"{b/1024**3:.2f} GB"

def fix_filename(fp: Path) -> Path:
    if "." not in fp.name:
        ext = sniff_extension(str(fp))
        if ext:
            new = fp.parent / (fp.name + ext)
            fp.rename(new)
            return new
    return fp


SPLIT_SIZE = 1950 * 1024 * 1024   # 1.95 GB per part — safe under 2GB limit

async def split_and_send(send_fn, edit, fp: Path):
    """Split file into 1.95GB parts and send each one."""
    file_size = fp.stat().st_size

    if file_size <= MAX_FILE_SIZE:
        await send_fn(fp)
        return

    total_parts = (file_size + SPLIT_SIZE - 1) // SPLIT_SIZE
    await edit(f"✂️ File is {human_size(file_size)} — splitting into {total_parts} parts...")

    stem = fp.stem
    ext  = fp.suffix
    part_paths = []

    with open(fp, "rb") as f:
        for i in range(1, total_parts + 1):
            part_name = fp.parent / f"{stem}.part{i:02d}of{total_parts:02d}{ext}"
            chunk     = f.read(SPLIT_SIZE)
            if not chunk:
                break
            with open(part_name, "wb") as pf:
                pf.write(chunk)
            part_paths.append(part_name)
            await edit(f"✂️ Part {i}/{total_parts} ready ({human_size(len(chunk))}). Sending...")
            await send_fn(part_name)   # send immediately — don't wait for all parts
            # part file deleted inside send_fn after upload

    # Delete original large file
    try:
        fp.unlink()
    except Exception:
        pass

    await edit(f"✅ Sent all {total_parts} parts of **{fp.name}**")

def get_tmp_usage() -> str:
    try:
        stat = shutil.disk_usage("/tmp")
        return f"{human_size(stat.total - stat.free)} / {human_size(stat.total)}"
    except Exception:
        return "unknown"

def auto_restart():
    """Restart the bot process with same arguments."""
    logger.info("Restarting bot...")
    os.execv(sys.executable, [sys.executable] + sys.argv)

def check_cmd(name):
    try:
        subprocess.run([name, "--version"], capture_output=True, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False

def settings_text() -> str:
    s   = load_settings()
    lib = s["library"]
    return (
        f"⚙️ **Bot Settings**\n\n"
        f"📦 **Library:** `{lib}`\n"
        f"👷 **Workers:** `{s['workers']}`\n"
        f"💾 **Max Download:** `{s['max_dl_gb']} GB`\n\n"
        f"**Change settings:**\n"
        f"`/set library pyrogram` — Switch to Pyrogram\n"
        f"`/set library telethon` — Switch to Telethon\n"
        f"`/set workers 4` — Upload workers (1–8)\n"
        f"`/set maxdl 1.5` — Max download size in GB\n\n"
        f"⚠️ Restart bot after changing library/workers."
    )


# ── Download handlers (library-agnostic) ─────────────────────────────────────

async def handle_gdrive_file(send_fn, edit, file_id, tmp_dir):
    await edit("⬇️ Downloading from Google Drive...")
    loop      = asyncio.get_event_loop()
    real_name = await loop.run_in_executor(None, lambda: get_real_filename(file_id))
    downloaded = await loop.run_in_executor(
        None, lambda: gdown.download(
            f"https://drive.google.com/uc?id={file_id}&export=download",
            output=tmp_dir + "/", quiet=False, fuzzy=True
        )
    )
    if not downloaded or not os.path.exists(downloaded):
        raise Exception("Download failed. File may be private.")
    fp = Path(downloaded)
    if fp.name == file_id or "." not in fp.name:
        if real_name:
            new = fp.parent / real_name; fp.rename(new); fp = new
    fp = fix_filename(fp)
    await split_and_send(send_fn, edit, fp)


async def handle_gdrive_folder(send_fn, edit, folder_id, tmp_dir):
    folder_dir = os.path.join(tmp_dir, "folder")
    os.makedirs(folder_dir, exist_ok=True)
    loop = asyncio.get_event_loop()
    await edit("⬇️ Fetching Google Drive folder...")
    await loop.run_in_executor(
        None, lambda: gdown.download_folder(
            f"https://drive.google.com/drive/folders/{folder_id}",
            output=folder_dir, quiet=True, remaining_ok=True
        )
    )
    all_files = sorted([f for f in Path(folder_dir).rglob("*") if f.is_file()], key=lambda f: f.name.lower())
    if not all_files:
        raise Exception("No files found or folder is private.")
    await edit(f"📦 {len(all_files)} file(s) found. Sending...")
    for i, fp in enumerate(all_files, 1):
        fp = fix_filename(fp)
        await edit(f"📤 {i}/{len(all_files)}: **{fp.name}** ({human_size(fp.stat().st_size)})")
        await send_fn(fp)


async def handle_direct(send_fn, edit, url, tmp_dir):
    loop = asyncio.get_event_loop()
    await edit("🔍 Checking file info...")
    remote_size = await loop.run_in_executor(None, lambda: get_remote_file_size(url))
    if remote_size > MAX_DL_SIZE:
        raise Exception(f"File too large: {human_size(remote_size)} (max {human_size(MAX_DL_SIZE)})")

    tmp_free = shutil.disk_usage("/tmp").free
    if remote_size > 0 and remote_size > tmp_free:
        raise Exception(f"Not enough /tmp space. Need {human_size(remote_size)}, free {human_size(tmp_free)}")
    filename  = await loop.run_in_executor(None, lambda: get_direct_filename(url))
    dest_path = os.path.join(tmp_dir, filename)
    await edit(f"⬇️ Downloading **{filename}**...")
    def _dl():
        with HTTP.get(url, stream=True, timeout=60, allow_redirects=True) as r:
            r.raise_for_status()
            with open(dest_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8 * 1024 * 1024):
                    if chunk: f.write(chunk)
    await loop.run_in_executor(None, _dl)
    fp = fix_filename(Path(dest_path))
    if not fp.exists():
        raise Exception("Download failed.")
    await split_and_send(send_fn, edit, fp)


async def handle_ytdlp(send_fn, edit, url, tmp_dir):
    if not check_cmd("yt-dlp"):
        raise Exception("yt-dlp not installed. Run: pip install yt-dlp")
    await edit("🔍 Fetching media info...")
    loop = asyncio.get_event_loop()
    is_stream = any(urllib.parse.urlparse(url).path.lower().endswith(e) for e in STREAM_EXTS)
    outtmpl   = os.path.join(tmp_dir, "stream_%(id)s.%(ext)s" if is_stream else "%(title).60s.%(ext)s")
    cmd = [
        "yt-dlp", "--no-playlist",
        "-f", f"bestvideo[ext=mp4][filesize<{MAX_DL_SIZE}]+bestaudio[ext=m4a]/best[ext=mp4][filesize<{MAX_DL_SIZE}]/best",
        "--merge-output-format", "mp4",
        "--max-filesize", str(MAX_DL_SIZE),
        "--output", outtmpl, "--no-warnings", "--hls-prefer-ffmpeg", url,
    ]
    await edit("⬇️ Downloading stream..." if is_stream else "⬇️ Downloading via yt-dlp...")
    def _run():
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if r.returncode != 0: raise Exception(r.stderr.strip() or "yt-dlp failed.")
    await loop.run_in_executor(None, _run)
    files = [f for f in Path(tmp_dir).iterdir() if f.is_file()]
    if not files: raise Exception("yt-dlp: no output file created.")
    for fp in sorted(files, key=lambda f: f.stat().st_size, reverse=True):
        await split_and_send(send_fn, edit, fp)


async def handle_magnet(send_fn, edit, magnet, tmp_dir):
    if not check_cmd("aria2c"):
        raise Exception("aria2c not installed. Run: apt install aria2")

    await edit("🧲 Starting magnet download...\n⏳ Connecting to peers...")

    cmd = [
        "aria2c",
        "--dir", tmp_dir,
        "--seed-time=0",
        "--max-connection-per-server=4",
        "--split=4",
        "--bt-stop-timeout=300",
        "--timeout=60",
        "--summary-interval=3",      # progress line every 3 seconds
        "--human-readable=true",
        magnet,
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    # aria2c progress line looks like:
    # [#ab1234 10MiB/100MiB(10%) CN:4 DL:1.2MiB ETA:1m30s]
    progress_re = re.compile(
        r"\[#\w+\s+([\d.]+\w+)/([\d.]+\w+)\((\d+)%\).*?DL:([\d.]+\w+).*?ETA:(\S+)\]"
    )
    last_edit = 0.0

    async def read_output():
        nonlocal last_edit
        async for line in proc.stdout:
            text = line.decode(errors="ignore").strip()
            if not text:
                continue
            logger.debug(f"aria2c: {text}")

            m = progress_re.search(text)
            if m:
                downloaded, total, pct, speed, eta = m.groups()
                now = asyncio.get_event_loop().time()
                if now - last_edit >= 3:   # update every 3s max
                    bar = "█" * (int(pct) // 10) + "░" * (10 - int(pct) // 10)
                    try:
                        await edit(
                            f"🧲 **Magnet Download**\n"
                            f"{bar} {pct}%\n"
                            f"📥 {downloaded} / {total}\n"
                            f"⚡ Speed: {speed}/s\n"
                            f"⏱ ETA: {eta}"
                        )
                        last_edit = now
                    except Exception:
                        pass

    try:
        await asyncio.wait_for(
            asyncio.gather(proc.wait(), read_output()),
            timeout=1800   # 30 min max
        )
    except asyncio.TimeoutError:
        proc.kill()
        raise Exception("Magnet timed out (30 min). Try with more seeders.")

    if proc.returncode != 0:
        raise Exception("aria2c failed. Check magnet link or seeders.")

    files = [f for f in Path(tmp_dir).rglob("*") if f.is_file()]
    if not files:
        raise Exception("No files downloaded from magnet.")

    await edit(f"📦 Download complete! {len(files)} file(s). Sending...")

    for i, fp in enumerate(sorted(files, key=lambda f: f.name.lower()), 1):
        await edit(f"📤 {i}/{len(files)}: **{fp.name}** ({human_size(fp.stat().st_size)})")
        await split_and_send(send_fn, edit, fp)


# ── Common set command logic ──────────────────────────────────────────────────

async def handle_set_cmd(parts, reply_fn):
    if len(parts) < 3:
        await reply_fn("Usage: `/set library pyrogram|telethon` | `/set workers 4` | `/set maxdl 1.5`")
        return
    key, val = parts[1].lower(), parts[2].lower()
    s = load_settings()

    if key == "library":
        if val not in ("pyrogram", "telethon"):
            await reply_fn("❌ Library must be `pyrogram` or `telethon`"); return
        if s["library"] == val:
            await reply_fn(f"ℹ️ Already using `{val}`"); return
        s["library"] = val; save_settings(s)
        await reply_fn(f"✅ Library switched to `{val}`\n🔄 Restarting bot...")
        await asyncio.sleep(1)   # reply bhejne ka waqt do
        auto_restart()

    elif key == "workers":
        try:
            n = int(val); assert 1 <= n <= 8
        except Exception:
            await reply_fn("❌ Workers must be 1–8"); return
        s["workers"] = n; save_settings(s)
        await reply_fn(f"✅ Workers set to `{n}`\n🔄 Restarting bot...")
        await asyncio.sleep(1)
        auto_restart()

    elif key == "maxdl":
        try:
            n = float(val); assert 0.1 <= n <= 10.0
        except Exception:
            await reply_fn("❌ Max download must be 0.1–10.0 GB"); return
        s["max_dl_gb"] = n; save_settings(s)
        await reply_fn(f"✅ Max download set to `{n} GB` (files > 2GB will be split)")

    else:
        await reply_fn("❌ Unknown key. Use: `library`, `workers`, `maxdl`")


# ═════════════════════════════════════════════════════════════════════════════
#  PYROGRAM BOT
# ═════════════════════════════════════════════════════════════════════════════

def run_pyrogram():
    from pyrogram import Client, filters
    from pyrogram.types import Message

    workers = SETTINGS.get("workers", 4)
    bot = Client(
        "gdrive_bot_pyrogram",
        api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN,
        workers=workers,
        max_concurrent_transmissions=workers,
    )

    async def pg_progress(current, total, status_msg, filename):
        if total == 0 or status_msg is None: return
        pct = current * 100 // total
        if pct in (0, 50, 100):
            try:
                bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
                await status_msg.edit_text(f"📤 **{filename}**\n{bar} {pct}%")
            except Exception:
                pass

    async def pg_send(client, message, status_msg, fp: Path):
        ext     = fp.suffix.lower()
        caption = f"✅ **{fp.name}**\n📦 {human_size(fp.stat().st_size)}"
        kw = dict(
            chat_id=message.chat.id, file_name=fp.name, caption=caption,
            progress=pg_progress, progress_args=(status_msg, fp.name),
        )
        if ext in VIDEO_EXTS:
            await client.send_video(video=str(fp), supports_streaming=True, **kw)
        elif ext in AUDIO_EXTS:
            await client.send_audio(audio=str(fp), **kw)
        else:
            await client.send_document(document=str(fp), **kw)
        try: fp.unlink()
        except Exception: pass
        logger.info(f"Sent {fp.name} | /tmp: {get_tmp_usage()}")
        if status_msg:
            try: await status_msg.delete()
            except Exception: pass

    @bot.on_message(filters.command("start"))
    async def start(_, msg: Message):
        await msg.reply_text(
            f"👋 **Universal Downloader Bot**\n"
            f"🔧 Engine: `Pyrogram` | Workers: `{workers}`\n\n"
            f"✅ Google Drive • Direct links • YouTube/Instagram/TikTok/etc • Magnets\n"
            f"/help — usage | /settings — config"
        )

    @bot.on_message(filters.command("help"))
    async def help_cmd(_, msg: Message):
        await msg.reply_text(
            "📖 **Supported links:**\n\n"
            "• Google Drive (file/folder)\n"
            "• Direct HTTP/HTTPS file links\n"
            "• YouTube, Instagram, Twitter/X, TikTok + 1000 more (yt-dlp)\n"
            "• `.m3u8` / HLS streams\n"
            "• Magnet links (aria2c required)\n\n"
            "⚠️ Max 2 GB | One download at a time"
        )

    @bot.on_message(filters.command("settings"))
    async def settings_cmd(_, msg: Message):
        await msg.reply_text(settings_text())

    @bot.on_message(filters.command("set"))
    async def set_cmd(_, msg: Message):
        parts = msg.text.strip().split()
        await handle_set_cmd(parts, msg.reply_text)

    @bot.on_message(filters.text & ~filters.command(["start", "help", "settings", "set"]))
    async def handle_message(client, msg: Message):
        text = msg.text.strip()
        identifier, link_type = detect_link_type(text)
        if link_type == "unknown" or not identifier:
            await msg.reply_text("❓ Unsupported link. Use /help."); return
        if _download_lock.locked():
            await msg.reply_text("⏳ Another download in progress. Please wait."); return

        status  = await msg.reply_text("⏳ Processing...")
        tmp_dir = tempfile.mkdtemp(dir="/tmp")

        async def send_fn(fp): await pg_send(client, msg, status, fp)
        async def edit(t):
            try: await status.edit_text(t)
            except Exception: pass

        try:
            async with _download_lock:
                if   link_type == "gdrive_folder": await handle_gdrive_folder(send_fn, edit, identifier, tmp_dir)
                elif link_type == "gdrive_file":   await handle_gdrive_file(send_fn, edit, identifier, tmp_dir)
                elif link_type == "ytdlp":         await handle_ytdlp(send_fn, edit, identifier, tmp_dir)
                elif link_type == "direct":        await handle_direct(send_fn, edit, identifier, tmp_dir)
                elif link_type == "magnet":        await handle_magnet(send_fn, edit, identifier, tmp_dir)
        except Exception as e:
            logger.error(f"Error: {e}", exc_info=True)
            try: await status.edit_text(f"❌ **Error:** {e}")
            except Exception: pass
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            logger.info(f"/tmp after cleanup: {get_tmp_usage()}")

    logger.info("Starting with Pyrogram...")
    bot.run()


# ═════════════════════════════════════════════════════════════════════════════
#  TELETHON BOT
# ═════════════════════════════════════════════════════════════════════════════

def run_telethon():
    from telethon import TelegramClient, events

    workers = SETTINGS.get("workers", 4)
    bot     = TelegramClient("gdrive_bot_telethon", API_ID, API_HASH)

    async def tl_send(client, chat_id, status_msg, fp: Path):
        ext     = fp.suffix.lower()
        caption = f"✅ **{fp.name}**\n📦 {human_size(fp.stat().st_size)}"
        last    = [0.0]

        async def progress(sent, total):
            now = time.time()
            if now - last[0] < 4: return
            pct = sent * 100 // total if total else 0
            bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
            try: await status_msg.edit(f"📤 **{fp.name}**\n{bar} {pct}%"); last[0] = now
            except Exception: pass

        await client.send_file(
            chat_id, str(fp), caption=caption,
            supports_streaming=ext in VIDEO_EXTS,
            force_document=ext not in (VIDEO_EXTS | AUDIO_EXTS),
            part_size_kb=512,
            progress_callback=progress,
        )
        try: fp.unlink()
        except Exception: pass
        logger.info(f"Sent {fp.name} | /tmp: {get_tmp_usage()}")
        try: await status_msg.delete()
        except Exception: pass

    @bot.on(events.NewMessage(pattern="/start"))
    async def start(event):
        await event.reply(
            f"👋 **Universal Downloader Bot**\n"
            f"🔧 Engine: `Telethon` | Workers: `{workers}`\n\n"
            f"✅ Google Drive • Direct links • YouTube/Instagram/TikTok/etc • Magnets\n"
            f"/help — usage | /settings — config"
        )

    @bot.on(events.NewMessage(pattern="/help"))
    async def help_cmd(event):
        await event.reply(
            "📖 **Supported links:**\n\n"
            "• Google Drive (file/folder)\n"
            "• Direct HTTP/HTTPS file links\n"
            "• YouTube, Instagram, Twitter/X, TikTok + 1000 more (yt-dlp)\n"
            "• `.m3u8` / HLS streams\n"
            "• Magnet links (aria2c required)\n\n"
            "⚠️ Max 2 GB | One download at a time"
        )

    @bot.on(events.NewMessage(pattern="/settings"))
    async def settings_cmd(event):
        await event.reply(settings_text())

    @bot.on(events.NewMessage(pattern="/set"))
    async def set_cmd(event):
        parts = event.raw_text.strip().split()
        await handle_set_cmd(parts, event.reply)

    @bot.on(events.NewMessage())
    async def handle_message(event):
        text = event.raw_text.strip()
        if not text or text.startswith("/"): return

        identifier, link_type = detect_link_type(text)
        if link_type == "unknown" or not identifier:
            await event.reply("❓ Unsupported link. Use /help."); return
        if _download_lock.locked():
            await event.reply("⏳ Another download in progress. Please wait."); return

        status  = await event.reply("⏳ Processing...")
        tmp_dir = tempfile.mkdtemp(dir="/tmp")
        chat_id = event.chat_id

        async def send_fn(fp): await tl_send(bot, chat_id, status, fp)
        async def edit(t):
            try: await status.edit(t)
            except Exception: pass

        try:
            async with _download_lock:
                if   link_type == "gdrive_folder": await handle_gdrive_folder(send_fn, edit, identifier, tmp_dir)
                elif link_type == "gdrive_file":   await handle_gdrive_file(send_fn, edit, identifier, tmp_dir)
                elif link_type == "ytdlp":         await handle_ytdlp(send_fn, edit, identifier, tmp_dir)
                elif link_type == "direct":        await handle_direct(send_fn, edit, identifier, tmp_dir)
                elif link_type == "magnet":        await handle_magnet(send_fn, edit, identifier, tmp_dir)
        except Exception as e:
            logger.error(f"Error: {e}", exc_info=True)
            try: await status.edit(f"❌ **Error:** {e}")
            except Exception: pass
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            logger.info(f"/tmp after cleanup: {get_tmp_usage()}")

    async def _run():
        await bot.start(bot_token=BOT_TOKEN)
        logger.info("Starting with Telethon...")
        await bot.run_until_disconnected()

    asyncio.run(_run())


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    if not BOT_TOKEN: raise ValueError("BOT_TOKEN not set!")
    if not API_ID:    raise ValueError("TELEGRAM_API_ID not set!")
    if not API_HASH:  raise ValueError("TELEGRAM_API_HASH not set!")

    lib = SETTINGS.get("library", "pyrogram")
    logger.info(f"Library: {lib} | Workers: {SETTINGS['workers']} | Max DL: {SETTINGS['max_dl_gb']} GB")

    if check_cmd("yt-dlp"):  logger.info("✅ yt-dlp found")
    else:                     logger.warning("⚠️  yt-dlp missing — pip install yt-dlp")
    if check_cmd("aria2c"):  logger.info("✅ aria2c found")
    else:                     logger.warning("⚠️  aria2c missing — apt install aria2")

    if lib == "telethon":
        run_telethon()
    else:
        run_pyrogram()


if __name__ == "__main__":
    main()
