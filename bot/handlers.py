from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from uuid import uuid4

from telegram import Bot, InputFile, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from bot.config import (
    ALLOWED_USER_IDS,
    DOWNLOADS_DIR,
    MAX_FILE_MB,
    UPLOAD_TIMEOUT,
    USING_LOCAL_API,
)
from bot.downloader import (
    DownloadCancelled,
    DownloadError,
    FileTooLargeError,
    cleanup_dir,
    download,
    extract_info,
    find_url,
)
from bot.fileio import ProgressReader
from bot.jobs import jobs
from bot.keyboards import best_fitting, job_cancel_keyboard, quality_keyboard
from bot.models import Job
from bot.progress import ProgressEvent, StatusProgress
from bot.text import (
    HELP_TEXT,
    SITES_TEXT,
    START_TEXT,
    format_caption,
    info_message,
)

logger = logging.getLogger(__name__)


def _is_allowed(user_id: int | None) -> bool:
    if user_id is None:
        return False
    if not ALLOWED_USER_IDS:
        return True
    return user_id in ALLOWED_USER_IDS


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not update.effective_user or not _is_allowed(update.effective_user.id):
        await update.message.reply_text("Этот бот закрытый.")
        return
    await update.message.reply_text(START_TEXT, parse_mode=ParseMode.HTML)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not update.effective_user or not _is_allowed(update.effective_user.id):
        return
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML)


async def sites_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not update.effective_user or not _is_allowed(update.effective_user.id):
        return
    await update.message.reply_text(SITES_TEXT, parse_mode=ParseMode.HTML)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    if not _is_allowed(update.effective_user.id):
        await update.message.reply_text("Этот бот закрытый.")
        return

    url = find_url(update.message.text or "")
    if not url:
        await update.message.reply_text("Пришлите ссылку, начинающуюся с http:// или https://")
        return

    status = await update.message.reply_text("Смотрю доступные качества…")
    try:
        info = await asyncio.to_thread(extract_info, url)
        context.user_data["pending"] = {
            "url": info.url,
            "title": info.title,
            "extractor": info.extractor,
            "duration": info.duration,
            "qualities": info.qualities,
        }
        auto = best_fitting(info.qualities)
        auto_label = None
        if auto:
            auto_label = auto.label if auto.height <= 0 else f"{auto.height}p"
        await status.edit_text(
            info_message(info),
            parse_mode=ParseMode.HTML,
            reply_markup=quality_keyboard(info, auto_label),
        )
    except DownloadError as exc:
        await status.edit_text(str(exc))
    except Exception:
        logger.exception("Failed to list formats")
        await status.edit_text("Не получилось прочитать эту ссылку.")


async def on_quality(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not update.effective_user or query.message is None:
        return

    user_id = update.effective_user.id
    if not _is_allowed(user_id):
        await query.answer("Нет доступа", show_alert=True)
        return

    data = query.data or ""
    if data == "dl:cancel":
        context.user_data.pop("pending", None)
        await query.answer("Отменено")
        await query.edit_message_text("Отменено.")
        return

    pending = context.user_data.get("pending")
    if not pending:
        await query.answer("Ссылка устарела — пришлите её ещё раз.", show_alert=True)
        return

    if not data.startswith("dl:"):
        await query.answer()
        return

    qualities = pending.get("qualities") or []
    choice = data.split(":", 1)[1]
    notice = ""
    height: int | None
    auto = choice == "auto"

    if choice == "audio":
        height = None
        label = "аудио"
    elif choice == "auto":
        fitting = best_fitting(qualities)
        if fitting is None:
            await query.answer(
                "Не вижу качества, которое точно влезет. Выберите вручную.",
                show_alert=True,
            )
            return
        height = fitting.height
        label = fitting.label if fitting.height <= 0 else f"{fitting.height}p"
        notice = f"Авто: беру {label} — лучшее, что влезает в {MAX_FILE_MB} МБ."
    else:
        try:
            height = int(choice)
        except ValueError:
            await query.answer("Неизвестное качество", show_alert=True)
            return
        label = "лучшее" if height <= 0 else f"{height}p"
        selected = next((q for q in qualities if q.height == height), None)
        if selected and selected.over_limit:
            fitting = best_fitting(qualities)
            if fitting is None:
                await query.answer(
                    f"{label} больше лимита {MAX_FILE_MB} МБ, меньшего качества нет.",
                    show_alert=True,
                )
                return
            notice = (
                f"{label} не влезает в {MAX_FILE_MB} МБ, качаю {fitting.height}p."
            )
            height = fitting.height
            label = f"{fitting.height}p"
            auto = True

    context.user_data.pop("pending", None)
    fallback = sorted(
        {q.height for q in qualities if not q.over_limit},
        reverse=True,
    )
    job = Job(
        id=uuid4().hex[:8],
        user_id=user_id,
        chat_id=query.message.chat_id,
        message_id=query.message.message_id,
        url=pending["url"],
        title=pending["title"],
        extractor=pending.get("extractor") or "",
        duration=pending.get("duration"),
        height=height,
        label=label,
        fallback_heights=fallback,
        auto=auto,
        notice=notice,
    )
    position = await jobs.submit(job, context.bot)
    if position > 1:
        await query.answer(f"В очереди: {position}")
        await query.edit_message_text(
            f"В очереди: {position}\n«{job.title[:80]}» · {job.label}\n"
            "Можно отменить или прислать ещё ссылку.",
            reply_markup=job_cancel_keyboard(job.id),
        )
    else:
        await query.answer(f"Качаю {label}…")


async def on_cancel_job(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not update.effective_user:
        return
    if not _is_allowed(update.effective_user.id):
        await query.answer("Нет доступа", show_alert=True)
        return
    job_id = (query.data or "").split(":", 1)[-1]
    result = jobs.cancel(update.effective_user.id, job_id)
    if result == "missing":
        await query.answer("Уже не в очереди")
        return
    await query.answer("Отменяю…" if result == "current" else "Убрал из очереди")
    if result == "queued":
        try:
            await query.edit_message_text("Убрано из очереди.")
        except TelegramError:
            pass


async def process_job(job: Job, bot: Bot) -> None:
    if job.cancel.is_set():
        await _edit_job(bot, job, "Отменено.")
        return

    workdir = DOWNLOADS_DIR / str(job.user_id) / job.id
    stop_action = asyncio.Event()
    action_task = asyncio.create_task(
        _keep_chat_action(
            bot,
            job.chat_id,
            ChatAction.UPLOAD_DOCUMENT if job.height is None else ChatAction.UPLOAD_VIDEO,
            stop_action,
        )
    )

    async def edit(text: str, **kwargs) -> None:
        await bot.edit_message_text(
            chat_id=job.chat_id,
            message_id=job.message_id,
            text=text,
            **kwargs,
        )

    try:
        if job.notice:
            try:
                await edit(job.notice, reply_markup=job_cancel_keyboard(job.id))
            except TelegramError:
                pass

        async with StatusProgress(
            edit,
            job.title,
            job.label,
            reply_markup=job_cancel_keyboard(job.id),
        ) as progress:
            path, thumb = await _download_with_fallback(job, workdir, progress.emit)
            if job.cancel.is_set():
                raise DownloadCancelled
            progress.emit(
                ProgressEvent(
                    stage="upload",
                    percent=0,
                    downloaded=0,
                    total=path.stat().st_size,
                )
            )
            await _send_file(
                job.chat_id,
                bot,
                path,
                job,
                is_audio=job.height is None,
                thumbnail=thumb,
                on_progress=progress.emit,
                cancel_event=job.cancel,
            )
        await _edit_job(bot, job, f"Готово: {job.title[:80]}")
    except DownloadCancelled:
        await _edit_job(bot, job, "Отменено.")
    except DownloadError as exc:
        await _edit_job(bot, job, str(exc))
    except TelegramError as exc:
        logger.exception("Telegram send failed")
        text = (
            str(FileTooLargeError(MAX_FILE_MB + 1, MAX_FILE_MB, using_local_api=USING_LOCAL_API))
            if _is_too_large(exc)
            else f"Скачал файл, но Telegram его не принял: {exc}"
        )
        await _edit_job(bot, job, text)
    except Exception:
        logger.exception("Job failed")
        await _edit_job(bot, job, "Что-то сломалось при скачивании.")
    finally:
        stop_action.set()
        action_task.cancel()
        try:
            await action_task
        except asyncio.CancelledError:
            pass
        cleanup_dir(workdir)


async def _download_with_fallback(job: Job, workdir: Path, emit) -> tuple[Path, Path | None]:
    heights: list[int | None]
    if job.height is None:
        heights = [None]
    else:
        ordered = [job.height] + [h for h in job.fallback_heights if h < job.height]
        heights = list(dict.fromkeys(ordered)) or [job.height]

    last_error: DownloadError | None = None
    for index, height in enumerate(heights):
        if job.cancel.is_set():
            raise DownloadCancelled
        label = "аудио" if height is None else ("лучшее" if height <= 0 else f"{height}p")
        if index > 0:
            job.label = label
            emit(ProgressEvent(stage="download", percent=0, detail=f"беру {label}"))
        try:
            return await asyncio.to_thread(
                download,
                job.url,
                workdir,
                height,
                emit,
                job.cancel,
            )
        except FileTooLargeError as exc:
            last_error = exc
            cleanup_dir(workdir)
            workdir.mkdir(parents=True, exist_ok=True)
            continue
    if last_error:
        raise last_error
    raise DownloadError("Не удалось скачать ни в одном качестве.")


async def _edit_job(bot: Bot, job: Job, text: str) -> None:
    try:
        await bot.edit_message_text(
            chat_id=job.chat_id,
            message_id=job.message_id,
            text=text,
        )
    except TelegramError:
        try:
            await bot.send_message(job.chat_id, text)
        except TelegramError:
            pass


def _is_too_large(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "too large" in text or "request entity too large" in text or "413" in text


async def _keep_chat_action(
    bot: Bot,
    chat_id: int,
    action: str,
    stop: asyncio.Event,
) -> None:
    while not stop.is_set():
        try:
            await bot.send_chat_action(chat_id=chat_id, action=action)
        except TelegramError:
            return
        try:
            await asyncio.wait_for(stop.wait(), timeout=4)
        except TimeoutError:
            continue


async def _send_file(
    chat_id: int,
    bot: Bot,
    path: Path,
    job: Job,
    *,
    is_audio: bool,
    thumbnail: Path | None,
    on_progress=None,
    cancel_event=None,
) -> None:
    size_mb = path.stat().st_size / (1024 * 1024)
    if size_mb > MAX_FILE_MB:
        raise FileTooLargeError(size_mb, MAX_FILE_MB, using_local_api=USING_LOCAL_API)

    caption = format_caption(
        job.title,
        extractor=job.extractor,
        duration=job.duration,
        quality=job.label,
    )
    send_kwargs: dict = {
        "read_timeout": UPLOAD_TIMEOUT,
        "write_timeout": UPLOAD_TIMEOUT,
        "connect_timeout": 60,
        "caption": caption,
    }
    thumb_file = None
    if thumbnail and thumbnail.exists():
        send_kwargs["thumbnail"] = InputFile(thumbnail.read_bytes(), filename="thumb.jpg")
    if job.duration:
        send_kwargs["duration"] = int(job.duration)

    emit = on_progress or (lambda _event: None)
    reader = ProgressReader(path, emit, cancel_event)
    media = InputFile(reader, filename=path.name, read_file_handle=False)
    try:
        if is_audio or path.suffix.lower() in {".m4a", ".mp3", ".ogg"}:
            await bot.send_audio(
                chat_id=chat_id,
                audio=media,
                title=job.title[:64],
                **send_kwargs,
            )
            return
        try:
            await bot.send_video(
                chat_id=chat_id,
                video=media,
                supports_streaming=True,
                **send_kwargs,
            )
        except TelegramError as exc:
            if _is_too_large(exc):
                raise FileTooLargeError(
                    size_mb, MAX_FILE_MB, using_local_api=USING_LOCAL_API
                ) from exc
            if send_kwargs.get("thumbnail"):
                send_kwargs.pop("thumbnail", None)
                reader.seek(0)
                try:
                    await bot.send_video(
                        chat_id=chat_id,
                        video=InputFile(reader, filename=path.name, read_file_handle=False),
                        supports_streaming=True,
                        **send_kwargs,
                    )
                    return
                except TelegramError:
                    pass
            reader.seek(0)
            await bot.send_document(
                chat_id=chat_id,
                document=InputFile(reader, filename=path.name, read_file_handle=False),
                caption=caption,
                read_timeout=UPLOAD_TIMEOUT,
                write_timeout=UPLOAD_TIMEOUT,
                connect_timeout=60,
            )
    except DownloadCancelled:
        raise
    except TelegramError as exc:
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelled from exc
        if _is_too_large(exc):
            raise FileTooLargeError(size_mb, MAX_FILE_MB, using_local_api=USING_LOCAL_API) from exc
        raise
    finally:
        reader.close()
        emit(
            ProgressEvent(
                stage="upload",
                percent=100,
                downloaded=path.stat().st_size,
                total=path.stat().st_size,
            )
        )


jobs.processor = process_job
