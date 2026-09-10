from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path

import yt_dlp

from bot.config import DOWNLOADS_DIR, MAX_FILE_BYTES, MAX_FILE_MB, USING_LOCAL_API
from bot.models import (
    DownloadCancelled,
    DownloadError,
    FileTooLargeError,
    PrivateContentError,
    QualityOption,
    VideoInfo,
)
from bot.progress import ProgressEvent

logger = logging.getLogger(__name__)

URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)

_PRIVATE_MARKERS = (
    "private video",
    "this video is private",
    "this account is private",
    "only available for registered users",
    "join this channel",
    "members-only",
    "members only",
    "subscribers only",
    "subscriber-only",
    "premium only",
    "login required to access this",
)

_COMMON_HEIGHTS = (144, 240, 360, 480, 720, 1080, 1440, 2160)


def find_url(text: str) -> str | None:
    match = URL_RE.search(text or "")
    return match.group(0).rstrip(").,]}>") if match else None


def _is_private_error(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in _PRIVATE_MARKERS)


def _best_format() -> str:
    # Instagram/TikTok often give one muxed file with no height — keep a bare `best`.
    return "bv*[ext=mp4]+ba/bv*+ba/b[ext=mp4]/b/best"


def _base_opts() -> dict:
    return {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "skip_download": True,
        "ignoreconfig": True,
        "no_color": True,
        "geo_bypass": False,
        "format": _best_format(),
        "logger": _YtLogger(),
        # Never use cookies / stored sessions — public links only.
        "cookiefile": None,
        "cookiesfrombrowser": None,
        "username": None,
        "password": None,
        "usenetrc": False,
        "extract_flat": False,
        "socket_timeout": 60,
        "retries": 5,
    }


class _YtLogger:
    def debug(self, msg: str) -> None:
        return

    def info(self, msg: str) -> None:
        return

    def warning(self, msg: str) -> None:
        if msg:
            logger.debug("%s", msg)

    def error(self, msg: str) -> None:
        if msg:
            logger.debug("%s", msg)


def _format_for_height(height: int) -> str:
    if height <= 0:
        return _best_format()
    # Prefer H.264 + AAC so Telegram can stream, but always fall back to `best`.
    return (
        f"bv*[height<={height}][ext=mp4][vcodec^=avc1]+ba[ext=m4a]/"
        f"bv*[height<={height}][vcodec^=avc1]+ba/"
        f"bv*[height<={height}]+ba/"
        f"b[height<={height}][ext=mp4]/"
        f"b[height<={height}]/"
        f"{_best_format()}"
    )


def _audio_format() -> str:
    return "ba[ext=m4a]/ba/b/best"


def _filesize(fmt: dict) -> int | None:
    size = fmt.get("filesize") or fmt.get("filesize_approx")
    return int(size) if size else None


def extract_info(url: str) -> VideoInfo:
    opts = _base_opts()
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as exc:
        message = str(exc)
        if _is_private_error(message):
            raise PrivateContentError from exc
        raise DownloadError(
            "Не удалось прочитать ссылку. Проверьте, что ролик открытый "
            "и площадка поддерживается."
        ) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("extract_info failed")
        raise DownloadError("Не получилось обработать эту ссылку.") from exc

    if not info:
        raise DownloadError("По ссылке не найден ролик.")

    if info.get("_type") == "playlist":
        raise DownloadError("Плейлисты не поддерживаются. Пришлите ссылку на одно видео.")

    availability = (info.get("availability") or "").lower()
    if availability in {"private", "needs_auth", "subscriber_only", "premium_only"}:
        raise PrivateContentError

    formats = info.get("formats") or []
    heights: dict[int, int | None] = {}
    has_audio = False

    for fmt in formats:
        vcodec = fmt.get("vcodec") or "none"
        acodec = fmt.get("acodec") or "none"
        if acodec != "none":
            has_audio = True
        height = fmt.get("height")
        if not height or vcodec == "none":
            continue
        height = int(height)
        size = _filesize(fmt)
        prev = heights.get(height)
        if prev is None or (size and (prev is None or size > prev)):
            heights[height] = size

    qualities: list[QualityOption] = []
    for height in sorted(set(heights) & set(_COMMON_HEIGHTS)):
        size = heights.get(height)
        over_limit = bool(size and size > MAX_FILE_BYTES)
        label = f"{height}p"
        if size:
            mb = size / (1024 * 1024)
            if over_limit:
                label = f"{height}p · не влезет (~{mb:.0f} МБ)"
            else:
                label = f"{height}p (~{mb:.0f} МБ)"
        qualities.append(
            QualityOption(
                height=height,
                label=label,
                approx_bytes=size,
                over_limit=over_limit,
            )
        )

    if not qualities:
        qualities.append(QualityOption(height=0, label="Лучшее доступное"))

    thumb = info.get("thumbnail")
    if not thumb:
        thumbs = info.get("thumbnails") or []
        if thumbs:
            thumb = thumbs[-1].get("url")

    return VideoInfo(
        url=info.get("webpage_url") or url,
        title=(info.get("title") or "video").strip(),
        duration=info.get("duration"),
        extractor=info.get("extractor_key") or info.get("extractor") or "generic",
        qualities=qualities,
        has_audio=has_audio,
        thumbnail_url=thumb,
    )


def _raise_if_cancelled(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise DownloadCancelled


def _progress_hook(
    d: dict,
    on_progress: Callable[[ProgressEvent], None] | None,
    cancel_event: threading.Event | None,
) -> None:
    _raise_if_cancelled(cancel_event)
    if on_progress is None:
        return
    status = d.get("status")
    if status == "downloading":
        downloaded = int(d.get("downloaded_bytes") or 0)
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        total_i = int(total) if total else None
        percent = None
        if total_i:
            percent = min(100.0, downloaded * 100 / total_i)
        info = d.get("info_dict") or {}
        vcodec = info.get("vcodec") or "none"
        acodec = info.get("acodec") or "none"
        detail = ""
        if vcodec == "none" and acodec != "none":
            detail = "аудио"
        elif acodec == "none" and vcodec != "none":
            detail = "видео"
        on_progress(
            ProgressEvent(
                stage="download",
                percent=percent,
                downloaded=downloaded,
                total=total_i,
                eta=int(d["eta"]) if isinstance(d.get("eta"), (int, float)) else None,
                speed=d.get("speed") if isinstance(d.get("speed"), (int, float)) else None,
                detail=detail,
            )
        )
    elif status == "finished":
        on_progress(ProgressEvent(stage="download", percent=100.0, detail="файл получен"))


def _postprocessor_hook(
    d: dict,
    on_progress: Callable[[ProgressEvent], None] | None,
    cancel_event: threading.Event | None,
) -> None:
    _raise_if_cancelled(cancel_event)
    if on_progress is None:
        return
    status = d.get("status")
    if status in {"started", "processing"}:
        on_progress(ProgressEvent(stage="merge", percent=None, detail="склеиваю"))
    elif status == "finished":
        on_progress(ProgressEvent(stage="merge", percent=100.0, detail="склеено"))


def download(
    url: str,
    dest_dir: Path,
    height: int | None,
    on_progress: Callable[[ProgressEvent], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> tuple[Path, Path | None]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    if USING_LOCAL_API:
        try:
            os.chmod(DOWNLOADS_DIR, 0o755)
            os.chmod(dest_dir.parent, 0o755)
            os.chmod(dest_dir, 0o755)
        except OSError:
            pass
    outtmpl = str(dest_dir / "%(title).80s.%(ext)s")

    if height is None:
        format_spec = _audio_format()
        merge = "m4a"
    else:
        format_spec = _format_for_height(height)
        merge = "mp4"

    opts = {
        **_base_opts(),
        "skip_download": False,
        "format": format_spec,
        "outtmpl": outtmpl,
        "merge_output_format": merge,
        "restrictfilenames": True,
        "overwrites": True,
        "max_filesize": MAX_FILE_BYTES,
        "windowsfilenames": False,
        "writethumbnail": True,
    }
    if on_progress is not None or cancel_event is not None:
        opts["progress_hooks"] = [
            lambda d: _progress_hook(d, on_progress, cancel_event)
        ]
        opts["postprocessor_hooks"] = [
            lambda d: _postprocessor_hook(d, on_progress, cancel_event)
        ]

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
    except DownloadCancelled:
        raise
    except yt_dlp.utils.DownloadError as exc:
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelled from exc
        message = str(exc)
        if _is_private_error(message):
            raise PrivateContentError from exc
        if "larger than max-filesize" in message.lower() or "file is larger" in message.lower():
            raise FileTooLargeError(
                MAX_FILE_MB + 1, MAX_FILE_MB, using_local_api=USING_LOCAL_API
            ) from exc
        if "requested format is not available" in message.lower():
            if height and height > 0:
                logger.debug("Format %s missing, retrying with best", height)
                return download(url, dest_dir, 0, on_progress, cancel_event)
            raise DownloadError(
                "Площадка не отдала поток (часто Instagram/TikTok). "
                "Ролик мог быть закрыт, удалён или временно недоступен без входа. "
                "Если ссылка открытая — обновите yt-dlp: pip install -U yt-dlp."
            ) from exc
        raise DownloadError(
            "Скачать не получилось. Попробуйте другое качество или другую ссылку."
        ) from exc

    path = Path(filename)
    if not path.exists():
        stem = path.with_suffix("")
        for candidate in dest_dir.glob(stem.name + ".*"):
            path = candidate
            break

    # After merge yt-dlp may rewrite extension to mp4/m4a.
    if not path.exists():
        for ext in (".mp4", ".m4a", ".mkv", ".webm"):
            alt = path.with_suffix(ext)
            if alt.exists():
                path = alt
                break

    if not path.exists():
        raise DownloadError("Файл скачался, но не найден на диске.")

    size = path.stat().st_size
    if size > MAX_FILE_BYTES:
        size_mb = size / (1024 * 1024)
        path.unlink(missing_ok=True)
        raise FileTooLargeError(size_mb, MAX_FILE_MB, using_local_api=USING_LOCAL_API)
    if USING_LOCAL_API:
        try:
            os.chmod(path, 0o644)
            os.chmod(dest_dir, 0o755)
            os.chmod(DOWNLOADS_DIR, 0o755)
        except OSError:
            pass
    if size == 0:
        path.unlink(missing_ok=True)
        raise DownloadError("Получился пустой файл.")
    if cancel_event is not None and cancel_event.is_set():
        raise DownloadCancelled

    thumb = prepare_thumbnail(_find_thumbnail(path, dest_dir), dest_dir)
    return path, thumb


def _find_thumbnail(video_path: Path, dest_dir: Path) -> Path | None:
    candidates = [
        video_path.with_suffix(ext)
        for ext in (".jpg", ".jpeg", ".png", ".webp")
    ]
    candidates.extend(
        p
        for p in dest_dir.iterdir()
        if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
    )
    for path in candidates:
        if path.exists() and path != video_path:
            return path
    return None


def prepare_thumbnail(src: Path | None, dest_dir: Path) -> Path | None:
    if src is None or not src.exists():
        return None
    dest = dest_dir / "thumb.jpg"
    if src.suffix.lower() in {".jpg", ".jpeg"} and src.stat().st_size <= 180_000:
        if src.resolve() != dest.resolve():
            shutil.copy2(src, dest)
        return dest
    if not ffmpeg_available():
        return src if src.suffix.lower() in {".jpg", ".jpeg"} else None
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(src),
                "-vf",
                "scale=320:-2",
                "-q:v",
                "6",
                str(dest),
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    return None


def cleanup_dir(path: Path) -> None:
    if path.exists() and path.resolve().is_relative_to(DOWNLOADS_DIR.resolve()):
        shutil.rmtree(path, ignore_errors=True)


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def ensure_downloads_dir() -> None:
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    # Prevent other users on the machine from reading downloaded files.
    try:
        os.chmod(DOWNLOADS_DIR, 0o755 if USING_LOCAL_API else 0o700)
    except OSError:
        pass
