from __future__ import annotations

import io
import threading
from collections.abc import Callable
from pathlib import Path

from bot.models import DownloadCancelled
from bot.progress import ProgressEvent

EmitFn = Callable[[ProgressEvent], None]


class ProgressReader(io.RawIOBase):
    """File wrapper that reports read progress while Telegram uploads the bytes."""

    def __init__(
        self,
        path: Path,
        emit: EmitFn,
        cancel_event: threading.Event | None = None,
    ) -> None:
        super().__init__()
        self._path = path
        self._fh = path.open("rb")
        self._size = path.stat().st_size
        self._seen = 0
        self._emit = emit
        self._cancel = cancel_event
        self.name = path.name

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        self._abort_if_cancelled()
        data = self._fh.read(size)
        self._note(len(data))
        return data

    def readinto(self, b) -> int:  # type: ignore[no-untyped-def]
        n = self._fh.readinto(b)
        if n:
            self._note(n)
        return n or 0

    def seek(self, offset: int, whence: int = 0) -> int:
        pos = self._fh.seek(offset, whence)
        self._seen = pos
        return pos

    def tell(self) -> int:
        return self._fh.tell()

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()
        super().close()

    @property
    def closed(self) -> bool:
        return self._fh.closed

    def fileno(self) -> int:
        # Avoid sendfile() so read() keeps reporting progress.
        raise io.UnsupportedOperation("fileno")

    def _abort_if_cancelled(self) -> None:
        if self._cancel is not None and self._cancel.is_set():
            raise DownloadCancelled()

    def _note(self, n: int) -> None:
        self._abort_if_cancelled()
        if n <= 0:
            return
        self._seen += n
        percent = 100.0 if not self._size else min(100.0, self._seen * 100 / self._size)
        self._emit(
            ProgressEvent(
                stage="upload",
                percent=percent,
                downloaded=self._seen,
                total=self._size,
            )
        )
