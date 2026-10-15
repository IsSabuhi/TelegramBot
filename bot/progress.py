from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from html import escape

from telegram.constants import ParseMode
from telegram.error import RetryAfter, TelegramError

from bot.telegram_limits import retry_seconds

logger = logging.getLogger(__name__)

EditFn = Callable[..., Awaitable[object]]


@dataclass(slots=True)
class ProgressEvent:
    stage: str
    percent: float | None = None
    downloaded: int | None = None
    total: int | None = None
    eta: int | None = None
    speed: float | None = None
    detail: str = ""


def progress_bar(percent: float | None, width: int = 10) -> str:
    if percent is None:
        return "░" * width
    clamped = max(0.0, min(100.0, percent))
    filled = int(round(clamped / 100 * width))
    filled = min(width, filled)
    return "█" * filled + "░" * (width - filled)


def _fmt_bytes(size: int | None) -> str:
    if not size or size < 0:
        return "?"
    mb = size / (1024 * 1024)
    if mb >= 100:
        return f"{mb:.0f} МБ"
    if mb >= 1:
        return f"{mb:.1f} МБ"
    kb = size / 1024
    return f"{kb:.0f} КБ"


def _fmt_eta(eta: int | None) -> str:
    if eta is None or eta < 0:
        return ""
    if eta < 60:
        return f"осталось ~{eta} сек"
    minutes, seconds = divmod(int(eta), 60)
    if minutes < 60:
        return f"осталось ~{minutes} мин {seconds:02d} сек"
    hours, minutes = divmod(minutes, 60)
    return f"осталось ~{hours} ч {minutes} мин"


def _fmt_speed(speed: float | None) -> str:
    if not speed or speed <= 0:
        return ""
    return f"{_fmt_bytes(int(speed))}/с"


def render_progress(title: str, quality: str, event: ProgressEvent) -> str:
    safe_title = escape(title[:80])
    bar = progress_bar(event.percent)
    pct = f"{event.percent:.0f}%" if event.percent is not None else "…"
    bar_line = f"<code>[{bar}] {pct}</code>"

    if event.stage == "merge":
        heading = f"Склеиваю видео и звук «{safe_title}»"
        return f"{heading}\n\n{bar_line}"

    if event.stage == "upload":
        heading = f"Отправляю в Telegram «{safe_title}» ({escape(quality)})"
        extra = ""
        if event.downloaded is not None and event.total:
            extra = f"\n{_fmt_bytes(event.downloaded)} / {_fmt_bytes(event.total)}"
        if event.percent is not None and event.percent >= 100:
            extra += "\nФайл передан в Bot API. Ожидаю подтверждения Telegram…"
        return f"{heading}\n\n{bar_line}{extra}"

    detail = f" · {escape(event.detail)}" if event.detail else ""
    heading = f"Скачиваю «{safe_title}» ({escape(quality)}){detail}"
    bits = []
    if event.downloaded is not None:
        if event.total:
            bits.append(f"{_fmt_bytes(event.downloaded)} / {_fmt_bytes(event.total)}")
        else:
            bits.append(f"уже {_fmt_bytes(event.downloaded)}")
    speed = _fmt_speed(event.speed)
    if speed:
        bits.append(speed)
    eta = _fmt_eta(event.eta)
    if eta:
        bits.append(eta)
    extra = f"\n{' · '.join(bits)}" if bits else ""
    return f"{heading}\n\n{bar_line}{extra}"


class StatusProgress:
    """Thread-safe progress updates for a Telegram status message."""

    def __init__(
        self,
        edit: EditFn,
        title: str,
        quality: str,
        interval: float = 4.0,
        reply_markup=None,
    ) -> None:
        self._edit = edit
        self.title = title
        self.quality = quality
        self.interval = interval
        self.reply_markup = reply_markup
        self._queue: asyncio.Queue[ProgressEvent] = asyncio.Queue(maxsize=1)
        self._task: asyncio.Task[None] | None = None
        self._last_text = ""
        self._closed = False
        self.loop = asyncio.get_running_loop()

    def emit(self, event: ProgressEvent) -> None:
        if self._closed:
            return
        try:
            self.loop.call_soon_threadsafe(self._offer, event)
        except RuntimeError:
            pass

    def _offer(self, event: ProgressEvent) -> None:
        if self._closed:
            return
        if self._queue.full():
            self._queue.get_nowait()
        self._queue.put_nowait(event)

    async def __aenter__(self) -> StatusProgress:
        self.emit(ProgressEvent(stage="download", percent=0, detail="начало"))
        self._task = asyncio.create_task(self._pump())
        return self

    async def __aexit__(self, *_exc: object) -> None:
        self._closed = True
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass

    async def _pump(self) -> None:
        while True:
            latest = await self._queue.get()
            await self._flush(latest)
            # A timeout on queue.get() is not a throttle: a busy queue returns immediately.
            await asyncio.sleep(self.interval)

    async def _flush(self, event: ProgressEvent) -> None:
        if self._closed:
            return
        text = render_progress(self.title, self.quality, event)
        if text == self._last_text:
            return
        try:
            await self._edit(
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=self.reply_markup,
            )
            self._last_text = text
        except RetryAfter as exc:
            # Also respect cooldown if StatusProgress is used with a plain Bot/test adapter.
            delay = retry_seconds(exc) + 1.0
            logger.warning("Telegram progress paused for %.1f seconds", delay)
            await asyncio.sleep(delay)
        except TelegramError as exc:
            if "not modified" in str(exc).lower():
                self._last_text = text
