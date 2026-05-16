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
OWNER_ID  = int(os.environ.get("OWNER_ID", "0"))   # apna Telegram user ID daalo

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
BOT_START_TIME   = time.time()   # used to skip stale messages on startup
_download_lock: asyncio.Lock | None = None
_cancel_event:  asyncio.Event | None = None
_active_tmp_dir: str | None = None   # track current download tmp dir for cleanup

def get_download_lock() -> asyncio.Lock:
    """Always return a Lock tied to the current running event loop."""
    global _download_lock
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if _download_lock is None or getattr(_download_lock, "_loop", None) is not loop:
        _download_lock = asyncio.Lock()
    return _download_lock

def get_cancel_event() -> asyncio.Event:
    """Always return a cancel Event tied to the current running event loop."""
    global _cancel_event
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if _cancel_event is None or getattr(_cancel_event, "_loop", None) is not loop:
        _cancel_event = asyncio.Event()
    return _cancel_event


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

def progress_bar(current: int, total: int) -> str:
    """Returns a unified progress line: bar + % + transferred/total + speed placeholder."""
    pct = min(int(current * 100 / total), 100) if total else 0
    filled = pct // 5          # 20-block bar (each block = 5%)
    bar = "█" * filled + "░" * (20 - filled)
    return f"{bar} {pct}%\n📥 {human_size(current)} / {human_size(total)}"

def upload_bar(current: int, total: int) -> str:
    pct = min(int(current * 100 / total), 100) if total else 0
    filled = pct // 5
    bar = "█" * filled + "░" * (20 - filled)
    return f"{bar} {pct}%\n📤 {human_size(current)} / {human_size(total)}"

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
    logger.info(f"/tmp after split+send: {get_tmp_usage()}")

def get_tmp_usage() -> str:
    try:
        stat = shutil.disk_usage("/tmp")
        return f"{human_size(stat.total - stat.free)} / {human_size(stat.total)}"
    except Exception:
        return "unknown"

def get_tmp_free_bytes() -> int:
    try:
        return shutil.disk_usage("/tmp").free
    except Exception:
        return 0

def cleanup_stale_tmp(min_free_bytes: int = 500 * 1024 * 1024):
    """Delete old bot tmp dirs if free space is below min_free_bytes (default 500 MB)."""
    if get_tmp_free_bytes() >= min_free_bytes:
        return
    logger.warning(f"/tmp low on space ({get_tmp_usage()}), cleaning stale dirs...")
    try:
        for entry in sorted(Path("/tmp").iterdir(), key=lambda p: p.stat().st_mtime):
            if entry.is_dir() and entry.name.startswith("tmp"):
                try:
                    shutil.rmtree(entry, ignore_errors=True)
                    logger.info(f"Cleaned stale dir: {entry}")
                except Exception:
                    pass
                if get_tmp_free_bytes() >= min_free_bytes:
                    break
    except Exception as e:
        logger.warning(f"cleanup_stale_tmp failed: {e}")

def auto_restart():
    """Restart the bot process with same arguments."""
    logger.info("Restarting bot...")
    os.execv(sys.executable, [sys.executable] + sys.argv)


def start_health_server():
    """Flask health server in a background daemon thread."""
    from flask import Flask
    flask_app = Flask(__name__)

    @flask_app.route("/")
    def home():
        s = load_settings()
        return f"Bot running | Engine: {s['library']} | Workers: {s['workers']}", 200

    @flask_app.route("/health")
    def health():
        return "OK", 200

    port = int(os.environ.get("PORT", 8080))
    import threading
    t = threading.Thread(
        target=lambda: flask_app.run(host="0.0.0.0", port=port, use_reloader=False),
        daemon=True,
    )
    t.start()
    logger.info(f"Flask health server started on port {port}")

async def run_health_server():
    """Async wrapper — starts Flask in background thread (non-blocking)."""
    start_health_server()

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
    cleanup_stale_tmp()
    logger.info(f"[GDRIVE] Download start: file_id={file_id}")
    await edit("⬇️ Downloading from Google Drive...")
    loop      = asyncio.get_running_loop()
    real_name = await loop.run_in_executor(None, lambda: get_real_filename(file_id))
    logger.info(f"[GDRIVE] Resolved filename: {real_name}")
    downloaded = await loop.run_in_executor(
        None, lambda: gdown.download(
            f"https://drive.google.com/uc?id={file_id}&export=download",
            output=tmp_dir + "/", quiet=False, fuzzy=True
        )
    )
    if get_cancel_event().is_set(): raise asyncio.CancelledError()
    if not downloaded or not os.path.exists(downloaded):
        raise Exception("Download failed. File may be private.")
    fp = Path(downloaded)
    logger.info(f"[GDRIVE] Downloaded: {fp.name} size={human_size(fp.stat().st_size)}")
    if fp.name == file_id or "." not in fp.name:
        if real_name:
            new = fp.parent / real_name; fp.rename(new); fp = new
    fp = fix_filename(fp)
    await split_and_send(send_fn, edit, fp)


async def handle_gdrive_folder(send_fn, edit, folder_id, tmp_dir):
    cleanup_stale_tmp()
    folder_dir = os.path.join(tmp_dir, "folder")
    os.makedirs(folder_dir, exist_ok=True)
    loop = asyncio.get_running_loop()
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
    """Stream directly from URL to Telegram — no /tmp disk usage for files under 2 GB.
    Falls back to disk for files that need splitting (> 2 GB)."""
    import io
    loop     = asyncio.get_running_loop()
    cancel   = get_cancel_event()

    await edit("🔍 Checking file info...")
    remote_size = await loop.run_in_executor(None, lambda: get_remote_file_size(url))
    if remote_size > MAX_DL_SIZE:
        raise Exception(f"File too large: {human_size(remote_size)} (max {human_size(MAX_DL_SIZE)})")

    filename = await loop.run_in_executor(None, lambda: get_direct_filename(url))
    ext      = Path(filename).suffix.lower()

    size_str = f" ({human_size(remote_size)})" if remote_size else ""
    await edit(f"📡 Streaming **{filename}**{size_str} → Telegram...")

    # ── files ≤ 2 GB: stream into memory pipe, upload without touching disk ──
    if remote_size <= MAX_FILE_SIZE:

        class StreamingReader(io.RawIOBase):
            """Wraps requests streaming response as a readable file-like object."""
            def __init__(self):
                self._resp  = HTTP.get(url, stream=True, timeout=(10, 300), allow_redirects=True)
                self._resp.raise_for_status()
                self._iter  = self._resp.iter_content(chunk_size=512 * 1024)
                self._buf   = b""
                self.uploaded = 0

            def readable(self):
                return True

            def readinto(self, b):
                if cancel.is_set():
                    return 0          # signals EOF → Pyrogram/Telethon will stop
                while not self._buf:
                    try:
                        self._buf = next(self._iter)
                    except StopIteration:
                        return 0      # EOF
                n = min(len(b), len(self._buf))
                b[:n] = self._buf[:n]
                self._buf = self._buf[n:]
                self.uploaded += n
                return n

            def close(self):
                try: self._resp.close()
                except Exception: pass
                super().close()

        logger.info(f"[DIRECT] Stream start: {filename} size={human_size(remote_size) if remote_size else '?'} url={url}")

        _dl_last_edit = [0.0]

        async def _update_dl_progress(transferred: int):
            """Edit status message with download progress (max once per 3s)."""
            now = time.time()
            if now - _dl_last_edit[0] < 3:
                return
            _dl_last_edit[0] = now
            try:
                if remote_size:
                    line = progress_bar(transferred, remote_size)
                    await edit(f"⬇️ **{filename}**\n{line}")
                else:
                    await edit(f"⬇️ **{filename}**\n📥 {human_size(transferred)}")
            except Exception:
                pass

        class LoggingReader(io.RawIOBase):
            """Wraps requests streaming response with Telegram progress updates."""
            def __init__(self):
                self._resp     = HTTP.get(url, stream=True, timeout=(10, 300), allow_redirects=True)
                self._resp.raise_for_status()
                self._iter     = self._resp.iter_content(chunk_size=512 * 1024)
                self._buf      = b""
                self.uploaded  = 0
                self._loop     = asyncio.get_event_loop()

            def readable(self): return True

            def readinto(self, b):
                if cancel.is_set():
                    return 0
                while not self._buf:
                    try:
                        self._buf = next(self._iter)
                    except StopIteration:
                        logger.info(f"[DIRECT] Stream EOF: {filename} total={human_size(self.uploaded)}")
                        return 0
                n = min(len(b), len(self._buf))
                b[:n] = self._buf[:n]
                self._buf = self._buf[n:]
                self.uploaded += n
                asyncio.run_coroutine_threadsafe(_update_dl_progress(self.uploaded), self._loop)
                return n

            def close(self):
                try: self._resp.close()
                except Exception: pass
                super().close()

        class NamedBufferedReader(io.BufferedReader):
            """BufferedReader with a writable .name property (io.BufferedReader.name is read-only)."""
            @property
            def name(self):
                return self._name
            def __init__(self, raw, buffer_size, name):
                super().__init__(raw, buffer_size=buffer_size)
                self._name = name

        reader = LoggingReader()
        bio    = NamedBufferedReader(reader, buffer_size=4 * 1024 * 1024, name=filename)

        await send_fn(bio, filename=filename, file_size=remote_size if remote_size else None)
        logger.info(f"[DIRECT] Stream upload done: {filename}")

        if cancel.is_set():
            raise asyncio.CancelledError()
        return

    # ── files > 2 GB: must save to disk first then split ──────────────────────
    cleanup_stale_tmp()
    tmp_free = get_tmp_free_bytes()
    if remote_size > 0 and remote_size > tmp_free:
        raise Exception(f"Not enough /tmp space. Need {human_size(remote_size)}, free {human_size(tmp_free)}")

    dest_path  = os.path.join(tmp_dir, filename)
    await edit(f"⬇️ Downloading **{filename}** to disk (>{human_size(MAX_FILE_SIZE)}, will split)...")

    logger.info(f"[DIRECT] Disk download start: {filename} size={human_size(remote_size) if remote_size else '?'}")
    cancelled  = [False]
    _disk_last = [0.0]

    async def _update_disk_progress(downloaded: int):
        now = time.time()
        if now - _disk_last[0] < 3:
            return
        _disk_last[0] = now
        try:
            if remote_size:
                line = progress_bar(downloaded, remote_size)
                await edit(f"⬇️ **{filename}**\n{line}")
            else:
                await edit(f"⬇️ **{filename}**\n📥 {human_size(downloaded)}")
        except Exception:
            pass

    _disk_loop = asyncio.get_running_loop()
    def _dl():
        downloaded = 0
        with HTTP.get(url, stream=True, timeout=(10, 300), allow_redirects=True) as r:
            r.raise_for_status()
            with open(dest_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=4 * 1024 * 1024):
                    if cancel.is_set():
                        cancelled[0] = True
                        return
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        asyncio.run_coroutine_threadsafe(_update_disk_progress(downloaded), _disk_loop)
        logger.info(f"[DIRECT] Disk download complete: {filename} total={human_size(downloaded)}")
    await loop.run_in_executor(None, _dl)
    if cancelled[0]:
        raise asyncio.CancelledError()
    fp = fix_filename(Path(dest_path))
    if not fp.exists():
        raise Exception("Download failed.")
    await split_and_send(send_fn, edit, fp)


async def handle_ytdlp(send_fn, edit, url, tmp_dir):
    if not check_cmd("yt-dlp"):
        raise Exception("yt-dlp not installed. Run: pip install yt-dlp")
    cleanup_stale_tmp()
    logger.info(f"[YTDLP] Download start: {url}")
    await edit("🔍 Fetching media info...")
    loop = asyncio.get_running_loop()
    is_stream    = any(urllib.parse.urlparse(url).path.lower().endswith(e) for e in STREAM_EXTS)
    is_instagram = "instagram.com" in url or "instagr.am" in url
    outtmpl      = os.path.join(tmp_dir, "stream_%(id)s.%(ext)s" if is_stream else "%(title).60s.%(ext)s")

    # Format priority:
    # 1. Best mp4 video + best m4a audio (merged)              — ideal
    # 2. Best mp4 video + any best audio                       — common Instagram fallback
    # 3. Best video + best m4a audio                           — cross-format merge
    # 4. Best video + any audio                                — generic merge
    # 5. Pre-muxed file with both streams                      — Reels/small clips
    # 6. Best file with audio codec                            — last resort with audio
    # 7. Absolute best                                         — no audio filter
    # NOTE: avoid filesize filters — Instagram/TikTok don't report sizes in manifests.
    fmt = (
        "bestvideo[ext=mp4]+bestaudio[ext=m4a]/"
        "bestvideo[ext=mp4]+bestaudio/"
        "bestvideo+bestaudio[ext=m4a]/"
        "bestvideo+bestaudio/"
        "best[acodec!=none][vcodec!=none]/"
        "best[acodec!=none]/"
        "best"
    )
    cmd = [
        "yt-dlp", "--no-playlist",
        "-f", fmt,
        "--merge-output-format", "mp4",
        "--max-filesize", str(MAX_DL_SIZE),
        "--output", outtmpl, "--no-warnings", "--hls-prefer-ffmpeg",
    ]
    if is_instagram:
        # Instagram small Reels often come as a single muxed stream —
        # using a mobile UA makes the API return proper muxed mp4 with audio.
        cmd += [
            "--add-header",
            "User-Agent:Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
        ]
    cmd.append(url)
    await edit("⬇️ Downloading stream..." if is_stream else "⬇️ Downloading via yt-dlp...")
    cancel = get_cancel_event()
    proc_holder = [None]

    def _run():
        proc_holder[0] = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        _, stderr = proc_holder[0].communicate()
        if cancel.is_set():
            return
        if proc_holder[0].returncode != 0:
            raise Exception(stderr.strip() or "yt-dlp failed.")

    fut = loop.run_in_executor(None, _run)
    while not fut.done():
        if cancel.is_set():
            try:
                if proc_holder[0]: proc_holder[0].terminate()
            except Exception: pass
            raise asyncio.CancelledError()
        await asyncio.sleep(1)
    fut.result()  # re-raise any exception from _run
    if cancel.is_set(): raise asyncio.CancelledError()
    files = [f for f in Path(tmp_dir).iterdir() if f.is_file()]
    if not files: raise Exception("yt-dlp: no output file created.")
    for fp in sorted(files, key=lambda f: f.stat().st_size, reverse=True):
        logger.info(f"[YTDLP] Sending: {fp.name} size={human_size(fp.stat().st_size)}")
        await split_and_send(send_fn, edit, fp)


async def handle_magnet(send_fn, edit, magnet, tmp_dir):
    if not check_cmd("aria2c"):
        raise Exception("aria2c not installed. Run: apt install aria2")
    cleanup_stale_tmp()
    await edit("🧲 Magnet download shuru ho raha hai...")

    RPC_PORT = 6800
    RPC_URL  = f"http://localhost:{RPC_PORT}/jsonrpc"
    RPC_SECRET = "aria2secret"

    # ── aria2c daemon RPC mode mein start karo ────────────────────────────────
    daemon_cmd = [
        "aria2c",
        "--enable-rpc",
        f"--rpc-listen-port={RPC_PORT}",
        f"--rpc-secret={RPC_SECRET}",
        "--daemon=true",
        "--dir", tmp_dir,
        "--seed-time=0",
        "--max-connection-per-server=4",
        "--split=4",
        "--bt-stop-timeout=300",
        "--log-level=error",
    ]
    proc = await asyncio.create_subprocess_exec(
        *daemon_cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await asyncio.sleep(2)  # daemon start hone do

    loop = asyncio.get_running_loop()

    def rpc(method, params=None):
        payload = {
            "jsonrpc": "2.0", "id": "bot",
            "method": method,
            "params": [f"token:{RPC_SECRET}"] + (params or []),
        }
        r = HTTP.post(RPC_URL, json=payload, timeout=10)
        return r.json().get("result")

    # ── Magnet add karo ───────────────────────────────────────────────────────
    try:
        gid = await loop.run_in_executor(None, lambda: rpc("aria2.addUri", [[magnet]]))
    except Exception as e:
        raise Exception(f"aria2c RPC error: {e}. Check if port {RPC_PORT} is free.")

    await edit(f"🧲 Magnet queued! Peers dhundh raha hai...\n🔑 GID: `{gid}`")

    # ── Progress polling ──────────────────────────────────────────────────────
    start_time = loop.time()
    TIMEOUT    = 1800  # 30 min

    while True:
        await asyncio.sleep(3)

        if loop.time() - start_time > TIMEOUT:
            await loop.run_in_executor(None, lambda: rpc("aria2.remove", [gid]))
            raise Exception("Magnet timed out (30 min). Try with more seeders.")

        try:
            status = await loop.run_in_executor(None, lambda: rpc("aria2.tellStatus", [gid]))
        except Exception:
            continue

        if not status:
            continue

        dl_state  = status.get("status", "")
        completed = int(status.get("completedLength", 0))
        total     = int(status.get("totalLength", 0))
        speed     = int(status.get("downloadSpeed", 0))
        seeders   = status.get("numSeeders", "0")
        name      = status.get("bittorrent", {}).get("info", {}).get("name", "Unknown")

        if dl_state == "error":
            err = status.get("errorMessage", "Unknown error")
            raise Exception(f"aria2c error: {err}")

        if dl_state == "complete":
            await edit(f"✅ Download complete!\n📁 **{name}**\n📦 {human_size(total)}")
            break

        pct = (completed * 100 // total) if total > 0 else 0
        bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
        eta_sec = ((total - completed) // speed) if speed > 0 else 0
        eta_str = f"{eta_sec // 60}m {eta_sec % 60}s" if speed > 0 else "..."

        state_icon = {
            "active":  "⬇️",
            "waiting": "⏳",
            "paused":  "⏸",
        }.get(dl_state, "🔄")

        try:
            await edit(
                f"🧲 **{name or 'Magnet Download'}**\n"
                f"{bar} {pct}%\n"
                f"{state_icon} {human_size(completed)} / {human_size(total) if total else '?'}\n"
                f"⚡ Speed: {human_size(speed)}/s\n"
                f"🌱 Seeders: {seeders}\n"
                f"⏱ ETA: {eta_str}"
            )
        except Exception:
            pass

    # ── Aria2c daemon band karo ───────────────────────────────────────────────
    try:
        await loop.run_in_executor(None, lambda: rpc("aria2.shutdown"))
    except Exception:
        pass

    # ── Files bhejo ──────────────────────────────────────────────────────────
    files = [f for f in Path(tmp_dir).rglob("*") if f.is_file()]
    if not files:
        raise Exception("No files downloaded from magnet.")

    logger.info(f"[MAGNET] Download complete. {len(files)} file(s) found.")
    await edit(f"📦 {len(files)} file(s) mili. Bhej raha hoon...")
    for i, fp in enumerate(sorted(files, key=lambda f: f.name.lower()), 1):
        logger.info(f"[MAGNET] Sending {i}/{len(files)}: {fp.name} size={human_size(fp.stat().st_size)}")
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


def get_video_meta(filepath: str) -> dict:
    """Extract width, height, duration, has_audio from video using ffprobe."""
    try:
        import json as _json
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", filepath],
            capture_output=True, text=True, timeout=15,
        )
        data    = _json.loads(result.stdout)
        streams = data.get("streams", [])
        vstream = next((s for s in streams if s.get("codec_type") == "video"), {})
        has_audio = any(s.get("codec_type") == "audio" for s in streams)
        width    = int(vstream.get("width") or 0)
        height   = int(vstream.get("height") or 0)
        dur_str  = vstream.get("duration") or "0"
        duration = int(float(dur_str)) if dur_str else 0
        return {
            "width":     max(0, width),
            "height":    max(0, height),
            "duration":  max(0, duration),
            "has_audio": has_audio,
        }
    except Exception:
        return {"width": 0, "height": 0, "duration": 0, "has_audio": False}

def ensure_audio_track(filepath: str) -> str:
    """If video has no audio stream, add a silent audio track via ffmpeg.
    Telegram converts silent short videos to GIF regardless of size — this prevents that.
    Returns path to fixed file (may be a new temp file), or original if ffmpeg unavailable."""
    try:
        meta = get_video_meta(filepath)
        if meta.get("has_audio", True):
            return filepath   # already has audio, nothing to do
        p        = Path(filepath)
        out_path = str(p.parent / (p.stem + "_audio" + p.suffix))
        result   = subprocess.run(
            [
                "ffmpeg", "-y", "-i", filepath,
                "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                "-c:v", "copy", "-c:a", "aac", "-shortest",
                "-movflags", "+faststart",
                out_path,
            ],
            capture_output=True, timeout=120,
        )
        if result.returncode == 0 and Path(out_path).exists():
            logger.info(f"[FFMPEG] Added silent audio track: {Path(filepath).name}")
            try: Path(filepath).unlink()
            except Exception: pass
            return out_path
    except Exception as e:
        logger.warning(f"[FFMPEG] ensure_audio_track failed: {e}")
    return filepath


# ═════════════════════════════════════════════════════════════════════════════
#  PYROGRAM BOT
# ═════════════════════════════════════════════════════════════════════════════

def run_pyrogram():
    import signal
    from pyrogram import Client, filters
    from pyrogram.types import Message

    async def main():
        workers = SETTINGS.get("workers", 4)

        # Client MUST be created inside the running event loop
        bot = Client(
            "gdrive_bot_pyrogram",
            api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN,
            workers=workers,
            max_concurrent_transmissions=workers,
        )

        # ── progress & send helpers ───────────────────────────────────────────

        _pg_prog_last: dict = {}
        async def pg_progress(current, total, status_msg, filename):
            if total == 0 or status_msg is None: return
            now = time.time()
            key = id(status_msg)
            if now - _pg_prog_last.get(key, 0.0) < 3:
                return
            _pg_prog_last[key] = now
            try:
                line = upload_bar(current, total)
                await status_msg.edit_text(f"📤 **{filename}**\n{line}")
            except Exception:
                pass

        async def pg_send(client, message, status_msg, fp, filename=None, file_size=None):
            # fp can be a Path (disk file) or a file-like object (streaming)
            is_path  = isinstance(fp, Path)
            fname    = filename or (fp.name if is_path else "file")
            ext      = Path(fname).suffix.lower()
            fsize    = fp.stat().st_size if is_path else (file_size or 0)
            caption  = f"✅ **{fname}**\n📦 {human_size(fsize)}" if fsize else f"✅ **{fname}**"
            src      = str(fp) if is_path else fp
            kw = dict(
                chat_id=message.chat.id, file_name=fname, caption=caption,
                progress=pg_progress, progress_args=(status_msg, fname),
            )
            if ext in VIDEO_EXTS:
                # Pyrogram send_video internally calls seek() — fails on live HTTP streams.
                # Buffer the stream to a temp file first so we can pass a seekable file
                # with full metadata, preventing Telegram from rendering it as a GIF.
                if not is_path:
                    import tempfile as _tf
                    _tmp = _tf.NamedTemporaryFile(delete=False, suffix=ext, dir="/tmp")
                    try:
                        try: await status_msg.edit_text(f"⬇️ Buffering **{fname}** for upload...")
                        except Exception: pass
                        _loop = asyncio.get_running_loop()
                        def _buf():
                            while True:
                                chunk = fp.read(4 * 1024 * 1024)
                                if not chunk: break
                                _tmp.write(chunk)
                            _tmp.flush()
                        await _loop.run_in_executor(None, _buf)
                        _tmp.close()
                        _disk = Path(_tmp.name)
                        # Add silent audio track if missing — prevents GIF conversion
                        _fixed = await _loop.run_in_executor(None, ensure_audio_track, str(_disk))
                        _disk  = Path(_fixed)
                        _meta = get_video_meta(str(_disk))
                        await client.send_video(
                            video=str(_disk), supports_streaming=True,
                            width=int(_meta.get("width") or 0),
                            height=int(_meta.get("height") or 0),
                            duration=int(_meta.get("duration") or 0),
                            **kw,
                        )
                    finally:
                        try: Path(_tmp.name).unlink()
                        except Exception: pass
                else:
                    # Ensure video has an audio track — Telegram converts silent
                    # videos to GIF regardless of file size or metadata.
                    _loop2 = asyncio.get_running_loop()
                    src = await _loop2.run_in_executor(None, ensure_audio_track, src)
                    meta = get_video_meta(src)
                    await client.send_video(
                        video=src, supports_streaming=True,
                        width=int(meta.get("width") or 0),
                        height=int(meta.get("height") or 0),
                        duration=int(meta.get("duration") or 0),
                        **kw,
                    )
            elif ext in AUDIO_EXTS:
                await client.send_audio(audio=src, **kw)
            else:
                await client.send_document(document=src, **kw)
            if is_path:
                try: fp.unlink()
                except Exception: pass
            logger.info(f"Sent {fname} | /tmp: {get_tmp_usage()}")
            if status_msg:
                try: await status_msg.delete()
                except Exception: pass

        # ── handlers ─────────────────────────────────────────────────────────

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
                "⚠️ Max 2 GB | One download at a time\n"
                "/cancel — stop current download"
            )

        @bot.on_message(filters.command("settings"))
        async def settings_cmd(_, msg: Message):
            await msg.reply_text(settings_text())

        @bot.on_message(filters.command("set"))
        async def set_cmd(_, msg: Message):
            parts = msg.text.strip().split()
            await handle_set_cmd(parts, msg.reply_text)

        @bot.on_message(filters.command("cancel"))
        async def cancel_cmd(_, msg: Message):
            lock = get_download_lock()
            if not lock.locked():
                await msg.reply_text("ℹ️ No download is currently running.")
                return
            get_cancel_event().set()
            await msg.reply_text("🚫 Cancel signal sent. Download will stop shortly...")

        @bot.on_message(filters.text & ~filters.bot & ~filters.command(["start", "help", "settings", "set", "cancel"]))
        async def handle_message(client, msg: Message):
            if not msg.text:
                return
            # Skip messages that arrived before bot started (queued while offline)
            if msg.date and msg.date.timestamp() < BOT_START_TIME:
                return
            text = msg.text.strip()
            identifier, link_type = detect_link_type(text)
            if link_type == "unknown" or not identifier:
                await msg.reply_text("❓ Unsupported link. Use /help."); return
            lock = get_download_lock()
            if lock.locked():
                wait_msg = await msg.reply_text("⏳ Another download in progress. You are queued — please wait...")
                await lock.acquire()
                try: await wait_msg.delete()
                except Exception: pass
            else:
                await lock.acquire()

            logger.info(f"[REQ] user={msg.from_user.id if msg.from_user else '?'} type={link_type} id={identifier[:60]}")
            status  = await msg.reply_text("⏳ Processing...")
            tmp_dir = tempfile.mkdtemp(dir="/tmp")

            async def send_fn(fp, **kw): await pg_send(client, msg, status, fp, **kw)
            async def edit(t):
                try: await status.edit_text(t)
                except Exception: pass

            global _active_tmp_dir
            _active_tmp_dir = tmp_dir
            cancel = get_cancel_event()
            cancel.clear()
            try:
                if True:  # lock already acquired above
                    if   link_type == "gdrive_folder": await handle_gdrive_folder(send_fn, edit, identifier, tmp_dir)
                    elif link_type == "gdrive_file":   await handle_gdrive_file(send_fn, edit, identifier, tmp_dir)
                    elif link_type == "ytdlp":         await handle_ytdlp(send_fn, edit, identifier, tmp_dir)
                    elif link_type == "direct":        await handle_direct(send_fn, edit, identifier, tmp_dir)
                    elif link_type == "magnet":        await handle_magnet(send_fn, edit, identifier, tmp_dir)
            except asyncio.CancelledError:
                try: await status.edit_text("🚫 Download cancelled.")
                except Exception: pass
            except Exception as e:
                logger.error(f"Error: {e}", exc_info=True)
                try: await status.edit_text(f"❌ **Error:** {e}")
                except Exception: pass
            finally:
                cancel.clear()
                _active_tmp_dir = None
                shutil.rmtree(tmp_dir, ignore_errors=True)
                try: lock.release()
                except RuntimeError: pass
                logger.info(f"/tmp after cleanup: {get_tmp_usage()}")

        # ── start bot ────────────────────────────────────────────────────────

        await bot.start()
        logger.info("Pyrogram bot started and listening...")

        if OWNER_ID:
            s   = load_settings()
            txt = (
                f"✅ **Bot Started!**\n\n"
                f"🔧 Engine: `Pyrogram`\n"
                f"👷 Workers: `{s['workers']}`\n"
                f"💾 Max DL: `{s['max_dl_gb']} GB`\n"
                f"✂️ Split: `1.95 GB` per part\n"
                f"{'✅' if check_cmd('yt-dlp') else '❌'} yt-dlp | "
                f"{'✅' if check_cmd('aria2c') else '❌'} aria2c\n\n"
                f"🕐 `{__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`"
            )
            try:
                await bot.send_message(OWNER_ID, txt)
            except Exception as e:
                logger.warning(f"Startup msg failed: {e}")

        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()

        def _sig_handler():
            logger.info("Shutdown signal received")
            stop_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _sig_handler)
            except (NotImplementedError, RuntimeError):
                pass

        await stop_event.wait()
        try:
            await bot.stop()
        except Exception as e:
            logger.warning(f"bot.stop() warning (safe to ignore): {e}")

    logger.info("Starting with Pyrogram...")
    start_health_server()   # bind port BEFORE asyncio.run so Render health check passes
    asyncio.run(main())


# ═════════════════════════════════════════════════════════════════════════════
#  TELETHON BOT
# ═════════════════════════════════════════════════════════════════════════════

def run_telethon():
    from telethon import TelegramClient, events

    workers = SETTINGS.get("workers", 4)
    bot     = TelegramClient("gdrive_bot_telethon", API_ID, API_HASH)

    async def tl_send(client, chat_id, status_msg, fp, filename=None, file_size=None):
        is_path = isinstance(fp, Path)
        fname   = filename or (fp.name if is_path else "file")
        ext     = Path(fname).suffix.lower()
        fsize   = fp.stat().st_size if is_path else (file_size or 0)
        caption = f"✅ **{fname}**\n📦 {human_size(fsize)}" if fsize else f"✅ **{fname}**"
        src     = str(fp) if is_path else fp
        _tl_last = [0.0]

        async def progress(sent, total):
            now = time.time()
            if now - _tl_last[0] < 3: return
            _tl_last[0] = now
            try:
                line = upload_bar(sent, total) if total else f"📤 {human_size(sent)}"
                await status_msg.edit(f"📤 **{fname}**\n{line}")
            except Exception: pass

        # For video files: buffer stream to disk if needed (send_file may seek),
        # then attach DocumentAttributeVideo so Telegram never misidentifies as GIF.
        import json as _json
        import tempfile as _tf2

        actual_src  = src
        tmp_vid_tl  = None
        import asyncio as _aio

        # Telethon's send_file() cannot stream from a non-seekable file-like object —
        # it reads the whole thing into memory. Always buffer to disk first to avoid OOM.
        if not is_path:
            _t = _tf2.NamedTemporaryFile(delete=False, suffix=ext, dir="/tmp")
            _buf_last  = [0.0]
            _buf_total = file_size or 0
            _buf_written = [0]

            async def _update_buf_progress(written: int):
                now = time.time()
                if now - _buf_last[0] < 3: return
                _buf_last[0] = now
                try:
                    if _buf_total:
                        line = progress_bar(written, _buf_total)
                        await status_msg.edit(f"⬇️ **{fname}**\n{line}")
                    else:
                        await status_msg.edit(f"⬇️ **{fname}**\n📥 {human_size(written)}")
                except Exception: pass

            _buf_loop = _aio.get_running_loop()
            def _buf_tl():
                while True:
                    chunk = fp.read(4 * 1024 * 1024)   # 4 MB chunks — low RAM footprint
                    if not chunk: break
                    _t.write(chunk)
                    _buf_written[0] += len(chunk)
                    asyncio.run_coroutine_threadsafe(_update_buf_progress(_buf_written[0]), _buf_loop)
                _t.flush()
            await _aio.get_running_loop().run_in_executor(None, _buf_tl)
            _t.close()
            tmp_vid_tl = _t.name
            actual_src = tmp_vid_tl

        if ext in VIDEO_EXTS:
            if not is_path:
                pass  # already buffered above

            # Add silent audio track if missing — prevents GIF conversion
            _fixed_tl  = await _aio.get_running_loop().run_in_executor(None, ensure_audio_track, actual_src)
            if _fixed_tl != actual_src:
                if tmp_vid_tl: 
                    try: Path(tmp_vid_tl).unlink()
                    except Exception: pass
                tmp_vid_tl = _fixed_tl
                actual_src = _fixed_tl

            try:
                from telethon.tl.types import DocumentAttributeVideo
                result = subprocess.run(
                    ["ffprobe", "-v", "quiet", "-print_format", "json",
                     "-show_streams", "-select_streams", "v:0", actual_src],
                    capture_output=True, text=True, timeout=15,
                )
                stream   = _json.loads(result.stdout).get("streams", [{}])[0]
                width    = int(stream.get("width") or 0) or 1280
                height   = int(stream.get("height") or 0) or 720
                duration = int(float(stream.get("duration") or "0"))
                attributes = [DocumentAttributeVideo(
                    duration=duration, w=width, h=height,
                    supports_streaming=True,
                )]
            except Exception:
                attributes = []
        else:
            attributes = None   # let Telethon auto-detect for audio/documents

        try:
            await client.send_file(
                chat_id, actual_src, caption=caption,
                attributes=attributes,
                supports_streaming=ext in VIDEO_EXTS,
                force_document=ext not in (VIDEO_EXTS | AUDIO_EXTS),
                part_size_kb=512,
                progress_callback=progress,
            )
        finally:
            if tmp_vid_tl:
                try: Path(tmp_vid_tl).unlink()
                except Exception: pass
        if is_path:
            try: fp.unlink()
            except Exception: pass
        logger.info(f"Sent {fname} | /tmp: {get_tmp_usage()}")
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
            "⚠️ Max 2 GB | One download at a time\n"
            "/cancel — stop current download"
        )

    @bot.on(events.NewMessage(pattern="/settings"))
    async def settings_cmd(event):
        await event.reply(settings_text())

    @bot.on(events.NewMessage(pattern=r"^/set(?:\s|$)"))
    async def set_cmd(event):
        parts = event.raw_text.strip().split()
        await handle_set_cmd(parts, event.reply)

    @bot.on(events.NewMessage(pattern="/cancel"))
    async def cancel_cmd(event):
        lock = get_download_lock()
        if not lock.locked():
            await event.reply("ℹ️ No download is currently running.")
            return
        get_cancel_event().set()
        await event.reply("🚫 Cancel signal sent. Download will stop shortly...")

    @bot.on(events.NewMessage(func=lambda e: e.is_private or e.is_group))
    async def handle_message(event):
        text = event.raw_text.strip()
        if not text or text.startswith("/"): return
        # Skip messages that arrived before bot started (queued while offline)
        if event.message.date and event.message.date.timestamp() < BOT_START_TIME:
            return
        # Ignore messages from other bots
        sender = await event.get_sender()
        if getattr(sender, "bot", False): return

        identifier, link_type = detect_link_type(text)
        if link_type == "unknown" or not identifier:
            await event.reply("❓ Unsupported link. Use /help."); return
        lock = get_download_lock()
        if lock.locked():
            wait_msg = await event.reply("⏳ Another download in progress. You are queued — please wait...")
            await lock.acquire()
            try: await wait_msg.delete()
            except Exception: pass
        else:
            await lock.acquire()

        status  = await event.reply("⏳ Processing...")
        tmp_dir = tempfile.mkdtemp(dir="/tmp")
        chat_id = event.chat_id

        async def send_fn(fp, **kw): await tl_send(bot, chat_id, status, fp, **kw)
        async def edit(t):
            try: await status.edit(t)
            except Exception: pass

        global _active_tmp_dir
        _active_tmp_dir = tmp_dir
        cancel = get_cancel_event()
        cancel.clear()
        try:
            if True:  # lock already acquired above
                if   link_type == "gdrive_folder": await handle_gdrive_folder(send_fn, edit, identifier, tmp_dir)
                elif link_type == "gdrive_file":   await handle_gdrive_file(send_fn, edit, identifier, tmp_dir)
                elif link_type == "ytdlp":         await handle_ytdlp(send_fn, edit, identifier, tmp_dir)
                elif link_type == "direct":        await handle_direct(send_fn, edit, identifier, tmp_dir)
                elif link_type == "magnet":        await handle_magnet(send_fn, edit, identifier, tmp_dir)
        except asyncio.CancelledError:
            try: await status.edit("🚫 Download cancelled.")
            except Exception: pass
        except Exception as e:
            logger.error(f"Error: {e}", exc_info=True)
            try: await status.edit(f"❌ **Error:** {e}")
            except Exception: pass
        finally:
            cancel.clear()
            _active_tmp_dir = None
            shutil.rmtree(tmp_dir, ignore_errors=True)
            try: lock.release()
            except RuntimeError: pass
            logger.info(f"/tmp after cleanup: {get_tmp_usage()}")

    async def _run():
        await bot.start(bot_token=BOT_TOKEN)
        logger.info("Starting with Telethon...")

        if OWNER_ID:
            s   = load_settings()
            txt = (
                f"✅ **Bot Started!**\n\n"
                f"🔧 Engine: `Telethon`\n"
                f"👷 Workers: `{s['workers']}`\n"
                f"💾 Max DL: `{s['max_dl_gb']} GB`\n"
                f"✂️ Split: `1.95 GB` per part\n"
                f"{'✅' if check_cmd('yt-dlp') else '❌'} yt-dlp | "
                f"{'✅' if check_cmd('aria2c') else '❌'} aria2c\n\n"
                f"🕐 `{__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`"
            )
            try:
                await bot.send_message(OWNER_ID, txt)
            except Exception as e:
                logger.warning(f"Startup msg failed: {e}")

        await bot.run_until_disconnected()

    start_health_server()   # bind port before asyncio.run
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
