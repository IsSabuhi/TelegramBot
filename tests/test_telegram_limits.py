from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.error import BadRequest, NetworkError, RetryAfter
from telegram.ext import ExtBot
from telegram.request import BaseRequest

from bot.handlers import _send_file
from bot.models import DownloadCancelled, Job
from bot.progress import ProgressEvent, StatusProgress
from bot.telegram_limits import RequestControl, TelegramRateLimiter, retry_seconds


class Clock:
    def __init__(self):
        self.now = 100.0
        self.waits = []

    def time(self):
        return self.now

    async def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)

    def patch(self):
        return patch("bot.telegram_limits.asyncio", SimpleNamespace(
            get_running_loop=lambda: self, sleep=self.sleep,
        ))


class LimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_retry_waits_for_requested_cooldown_and_rewinds(self):
        clock = Clock()
        limiter = TelegramRateLimiter(chat_interval=0, global_interval=0)
        callback = AsyncMock(side_effect=[RetryAfter(157), "sent"])
        rewind = unittest.mock.Mock()
        with clock.patch():
            result = await limiter.process_request(
                callback, (), {}, "sendVideo", {"chat_id": 1}, RequestControl(before_retry=rewind),
            )
        self.assertEqual(result, "sent")
        self.assertEqual(callback.await_count, 2)
        rewind.assert_called_once_with()
        self.assertGreaterEqual(sum(clock.waits), 158)

    async def test_cooldown_also_applies_to_other_requests(self):
        clock = Clock()
        limiter = TelegramRateLimiter(chat_interval=0, global_interval=0)
        limiter._blocked_until = 258.0
        times = []

        async def callback():
            times.append(clock.time())
            return True

        with clock.patch():
            await asyncio.gather(*[
                limiter.process_request(callback, (), {}, "editMessageText", {"chat_id": chat}, None)
                for chat in (1, 2)
            ])
        self.assertTrue(all(t >= 258 for t in times))

    async def test_requests_in_one_chat_are_spaced(self):
        clock = Clock()
        limiter = TelegramRateLimiter()
        callback = AsyncMock(return_value=True)
        with clock.patch():
            await limiter.process_request(callback, (), {}, "editMessageText", {"chat_id": 1}, None)
            await limiter.process_request(callback, (), {}, "sendVideo", {"chat_id": 1}, None)
        self.assertGreaterEqual(sum(clock.waits), 3.2 - 1e-9)

    async def test_cancel_during_cooldown_does_not_retry(self):
        clock = Clock()
        limiter = TelegramRateLimiter()
        cancelled = threading.Event()

        async def callback():
            cancelled.set()
            raise RetryAfter(157)

        with clock.patch():
            with self.assertRaises(DownloadCancelled):
                await limiter.process_request(
                    callback, (), {}, "sendVideo", {"chat_id": 1}, RequestControl(cancel=cancelled),
                )
        self.assertEqual(clock.waits, [])

    async def test_network_failure_is_not_retried(self):
        limiter = TelegramRateLimiter()
        callback = AsyncMock(side_effect=NetworkError("connection lost"))
        with self.assertRaises(NetworkError):
            await limiter.process_request(callback, (), {}, "sendVideo", {"chat_id": 1}, None)
        self.assertEqual(callback.await_count, 1)

    async def test_timedelta_retry_after(self):
        self.assertEqual(retry_seconds(RetryAfter(timedelta(seconds=157))), 157)


class ProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_continuous_events_are_coalesced_and_throttled(self):
        calls = []
        loop = asyncio.get_running_loop()

        async def edit(text, **kwargs):
            calls.append((loop.time(), text))

        async with StatusProgress(edit, "Video", "720p", interval=0.04) as progress:
            await asyncio.sleep(0.01)
            for percent in range(1, 100):
                progress.emit(ProgressEvent("download", percent=percent))
                await asyncio.sleep(0.001)
            await asyncio.sleep(0.08)
            self.assertLessEqual(progress._queue.qsize(), 1)
        self.assertLess(len(calls), 15)
        self.assertTrue(all(b[0] - a[0] >= 0.035 for a, b in zip(calls, calls[1:])))
        self.assertIn("99%", calls[-1][1])

    async def test_progress_honors_flood_cooldown(self):
        edit = AsyncMock(side_effect=[RetryAfter(157), True])
        progress = StatusProgress(edit, "Video", "720p")
        sleep = AsyncMock()
        with patch("bot.progress.asyncio", SimpleNamespace(sleep=sleep)):
            await progress._flush(ProgressEvent("download", percent=38))
        sleep.assert_awaited_once_with(158)
        self.assertEqual(progress._last_text, "")
        await progress._flush(ProgressEvent("download", percent=50))
        self.assertIn("50%", progress._last_text)


class SendingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "video.mp4"
        self.content = bytes(range(100))
        self.path.write_bytes(self.content)
        self.job = Job(
            id="test", user_id=1, chat_id=1, message_id=1, url="https://example.com/video",
            title="Video", extractor="DION", duration=10, height=720, label="720p", fallback_heights=[],
        )

    async def test_file_retry_resends_all_bytes_without_document_fallback(self):
        limiter = TelegramRateLimiter(chat_interval=0, global_interval=0)
        clock = Clock()
        attempts = []
        progress = []

        async def send_video(**kwargs):
            async def callback():
                self.assertTrue(self.path.exists())
                reader = kwargs["video"].input_file_content
                attempts.append(reader.read(38 if not attempts else -1))
                if len(attempts) == 1:
                    raise RetryAfter(157)
                return True
            return await limiter.process_request(
                callback, (), {}, "sendVideo", {"chat_id": 1}, kwargs["rate_limit_args"],
            )

        bot = SimpleNamespace(send_video=send_video, send_document=AsyncMock())
        with clock.patch():
            await _send_file(1, bot, self.path, self.job, is_audio=False, thumbnail=None, on_progress=progress.append)
        self.assertEqual(attempts, [self.content[:38], self.content])
        bot.send_document.assert_not_called()
        self.assertGreaterEqual(sum(clock.waits), 158)
        self.assertTrue(self.path.exists())
        self.assertEqual(progress[-1].percent, 100)

    async def test_rate_limit_is_not_mistaken_for_bad_video(self):
        bot = SimpleNamespace(send_video=AsyncMock(side_effect=RetryAfter(157)), send_document=AsyncMock())
        progress = []
        with self.assertRaises(RetryAfter):
            await _send_file(1, bot, self.path, self.job, is_audio=False, thumbnail=None, on_progress=progress.append)
        bot.send_document.assert_not_called()
        self.assertEqual(progress, [])

    async def test_real_telegram_client_rebuilds_multipart_on_retry(self):
        attempts = []

        class MemoryRequest(BaseRequest):
            @property
            def read_timeout(self):
                return None

            async def initialize(self):
                pass

            async def shutdown(self):
                pass

            async def do_request(self, url, method, request_data=None, **kwargs):
                if url.endswith("getMe"):
                    return 200, json.dumps({"ok": True, "result": {
                        "id": 123456, "first_name": "Test", "is_bot": True,
                    }}).encode()
                if not url.endswith("sendVideo"):
                    raise AssertionError("Unexpected Telegram request")
                parts = list(request_data.multipart_data.values())
                reader = parts[0][1]
                attempts.append(reader.read(38 if not attempts else -1))
                if len(attempts) == 1:
                    return 429, json.dumps({
                        "ok": False, "description": "Flood control", "parameters": {"retry_after": 157},
                    }).encode()
                return 200, b'{"ok":true,"result":true}'

        request = MemoryRequest()
        clock = Clock()
        bot = ExtBot(
            token="123456:test", request=request, get_updates_request=MemoryRequest(),
            rate_limiter=TelegramRateLimiter(chat_interval=0, global_interval=0),
        )
        with clock.patch():
            async with bot:
                await _send_file(1, bot, self.path, self.job, is_audio=False, thumbnail=None)
        self.assertEqual(attempts, [self.content[:38], self.content])
        self.assertGreaterEqual(sum(clock.waits), 158)

    async def test_failed_upload_does_not_emit_fake_100_percent(self):
        progress = []

        async def send_video(**kwargs):
            kwargs["video"].input_file_content.read(38)
            raise NetworkError("connection lost")

        bot = SimpleNamespace(send_video=send_video, send_document=AsyncMock())
        with self.assertRaises(NetworkError):
            await _send_file(1, bot, self.path, self.job, is_audio=False, thumbnail=None, on_progress=progress.append)
        self.assertEqual([event.percent for event in progress], [38])
        bot.send_document.assert_not_called()

    async def test_bad_video_still_falls_back_to_document(self):
        seen = []

        async def send_video(**kwargs):
            kwargs["video"].input_file_content.read(38)
            raise BadRequest("unsupported video")

        async def send_document(**kwargs):
            seen.append(kwargs["document"].input_file_content.read())

        bot = SimpleNamespace(send_video=send_video, send_document=send_document)
        await _send_file(1, bot, self.path, self.job, is_audio=False, thumbnail=None)
        self.assertEqual(seen, [self.content])


if __name__ == "__main__":
    unittest.main()
