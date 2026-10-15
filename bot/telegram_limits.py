from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass
from datetime import timedelta
from typing import Callable

from telegram.error import RetryAfter
from telegram.ext import BaseRateLimiter

from bot.models import DownloadCancelled

logger = logging.getLogger(__name__)


def retry_seconds(exc: RetryAfter) -> float:
    delay = exc.retry_after
    return delay.total_seconds() if isinstance(delay, timedelta) else float(delay)


@dataclass
class RequestControl:
    cancel: threading.Event | None = None
    before_retry: Callable[[], object] | None = None


class TelegramRateLimiter(BaseRateLimiter[RequestControl]):
    """Space chat requests and share Telegram's flood cooldown across all workers."""

    def __init__(self, chat_interval: float = 3.2, global_interval: float = 0.05) -> None:
        self.chat_interval = chat_interval
        self.global_interval = global_interval
        self._lock = asyncio.Lock()
        self._chat_next: dict[int | str, float] = {}
        self._global_next = 0.0
        self._blocked_until = 0.0

    async def initialize(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    async def _slot(self, chat_id, control: RequestControl | None) -> None:
        loop = asyncio.get_running_loop()
        while True:
            if control and control.cancel and control.cancel.is_set():
                raise DownloadCancelled
            async with self._lock:
                now = loop.time()
                ready = max(
                    self._blocked_until,
                    self._global_next,
                    self._chat_next.get(chat_id, 0) if chat_id is not None else 0,
                )
                if now >= ready:
                    self._global_next = now + self.global_interval
                    if chat_id is not None:
                        # Discard expired chat reservations to avoid retaining every user ID.
                        self._chat_next = {k: v for k, v in self._chat_next.items() if v > now}
                        self._chat_next[chat_id] = now + self.chat_interval
                    return
            # Short waits let cancellation stop an upload even in a long flood cooldown.
            await asyncio.sleep(min(ready - now, 0.5))

    async def process_request(self, callback, args, kwargs, endpoint, data, rate_limit_args):
        control = rate_limit_args
        retry = False
        while True:
            await self._slot(data.get("chat_id"), control)
            if retry and control and control.before_retry:
                control.before_retry()
            try:
                return await callback(*args, **kwargs)
            except RetryAfter as exc:
                delay = max(0.0, retry_seconds(exc)) + 1.0
                self._blocked_until = max(
                    self._blocked_until, asyncio.get_running_loop().time() + delay,
                )
                logger.warning(
                    "Telegram flood control: %s paused for %.1f seconds; request will retry",
                    endpoint, delay,
                )
                retry = True
