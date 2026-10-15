from __future__ import annotations

from html import escape

from bot.config import MAX_FILE_MB, USING_LOCAL_API
from bot.models import VideoInfo


def format_duration(seconds: int | None) -> str:
    if not seconds or seconds < 0:
        return ""
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_caption(
    title: str,
    *,
    extractor: str,
    duration: int | None,
    quality: str,
) -> str:
    lines = [title.strip()[:400] or "video"]
    if extractor:
        lines.append(f"Источник: {extractor}")
    length = format_duration(duration)
    if length:
        lines.append(f"Длительность: {length}")
    if quality:
        lines.append(f"Качество: {quality}")
    caption = "\n".join(lines)
    return caption[:1024]


def info_message(info: VideoInfo) -> str:
    title = escape(info.title[:200])
    source = escape(info.extractor)
    length = format_duration(info.duration)
    lines = [f"<b>{title}</b>", f"Источник: {source}"]
    if length:
        lines.append(f"Длительность: {length}")
    lines.append("")
    lines.append(f"Лимит файла: {MAX_FILE_MB} МБ.")
    lines.append("Выберите качество или «Авто» — возьму лучшее, которое влезет.")
    return "\n".join(lines)


START_TEXT = (
    "Пришлите ссылку на <b>публичное</b> видео — покажу доступные качества "
    "и скачаю выбранное.\n\n"
    "Работает с открытыми роликами (YouTube, TikTok, Instagram Reels, X, DION и другие "
    "площадки, которые отдаёт yt-dlp). Список: /sites\n\n"
    f"Ограничение Telegram: файл не больше {MAX_FILE_MB} МБ"
    + (
        " (локальный Bot API, до ~2 ГБ)."
        if USING_LOCAL_API
        else " (облачный Bot API). Для гигабайтных файлов нужен локальный Bot API — см. README."
    )
    + "\nМожно кидать следующую ссылку, пока качается предыдущая — встанет в очередь.\n"
    "Плейлисты и сериалы целиком не качаю.\n"
    "Приватные аккаунты, сторис и контент с логином <b>не поддерживаются</b>."
)

HELP_TEXT = (
    "1. Скопируйте ссылку на открытое видео\n"
    "2. Отправьте её боту\n"
    "3. Выберите качество или «Авто»\n"
    "4. При необходимости нажмите «Отмена»\n\n"
    "Команды: /start /help /sites\n"
    "Не присылайте ссылки на чужой закрытый контент — бот его не скачает.\n"
    "Скачивайте только то, на что у вас есть право."
)

SITES_TEXT = (
    "Публичные ссылки, которые <b>обычно</b> работают:\n\n"
    "• YouTube\n"
    "• TikTok\n"
    "• Instagram Reels и открытые посты с видео\n"
    "• X (Twitter)\n"
    "• Facebook (открытые ролики)\n"
    "• VK\n"
    "• Vimeo\n"
    "• Reddit\n"
    "• DION — завершённые записи по ссылке dion.vc/video/… или HLS .m3u8\n\n"
    "Не работает: приватные аккаунты, сторис, закрытые профили, "
    "контент с логином, плейлисты целиком.\n\n"
    "Instagram и TikTok часто меняют защиту — если открытая ссылка вдруг "
    "не открывается, обновите yt-dlp: <code>pip install -U yt-dlp</code>."
)
