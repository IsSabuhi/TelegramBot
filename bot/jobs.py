from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from telegram import Bot

from bot.models import Job

logger = logging.getLogger(__name__)

Processor = Callable[[Job, Bot], Awaitable[None]]


class JobManager:
    def __init__(self) -> None:
        self.processor: Processor | None = None
        self._lock = asyncio.Lock()
        self._waiting: dict[int, list[Job]] = {}
        self._current: dict[int, Job] = {}
        self._workers: dict[int, asyncio.Task[None]] = {}

    async def submit(self, job: Job, bot: Bot) -> int:
        async with self._lock:
            waiting = self._waiting.setdefault(job.user_id, [])
            waiting.append(job)
            position = len(waiting) + (1 if job.user_id in self._current else 0)
            task = self._workers.get(job.user_id)
            if task is None or task.done():
                self._workers[job.user_id] = asyncio.create_task(self._run(job.user_id, bot))
        return position

    def cancel(self, user_id: int, job_id: str) -> str:
        current = self._current.get(user_id)
        if current and current.id == job_id:
            current.cancel.set()
            return "current"
        waiting = self._waiting.get(user_id) or []
        for index, job in enumerate(waiting):
            if job.id == job_id:
                job.cancel.set()
                waiting.pop(index)
                return "queued"
        return "missing"

    def waiting_position(self, user_id: int, job_id: str) -> int | None:
        current = 1 if user_id in self._current else 0
        for index, job in enumerate(self._waiting.get(user_id) or []):
            if job.id == job_id:
                return current + index + 1
        return None

    async def _run(self, user_id: int, bot: Bot) -> None:
        processor = self.processor
        if processor is None:
            logger.error("Job processor is not set")
            return
        while True:
            async with self._lock:
                waiting = self._waiting.get(user_id) or []
                if not waiting:
                    self._current.pop(user_id, None)
                    self._workers.pop(user_id, None)
                    return
                job = waiting.pop(0)
                self._current[user_id] = job
            if job.cancel.is_set():
                continue
            try:
                await processor(job, bot)
            except Exception:
                logger.exception("Job %s failed", job.id)
            finally:
                async with self._lock:
                    if self._current.get(user_id) is job:
                        self._current.pop(user_id, None)


jobs = JobManager()
